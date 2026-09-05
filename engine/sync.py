"""Pull the REAL campaigns and leads out of the warehouse into redial.db.

`python -m engine.sync` replaces whatever is in the local store with live data:
the campaign names, ids and statuses agents 125/127 actually have in
`public.campaigns`, and their leads with the phone numbers `engine.seed` used to
invent. `engine.seed` stays as the credential-free offline fallback.

Bounds, because "every campaign x every lead" is ~30k rows of warehouse traffic
per run and the console does not need it:

  * only campaigns with leads AND at least one parseable RED, newest first,
  * --campaigns (default 20) of them, --leads (default 5000) leads each,
  * plus, always, the newest campaign per agent that holds a test number, so
    /api/test-call keeps resolving even though those campaigns are tiny.
  * only leads TODAY is about: inside the campaign's own RED window (what the
    engine can put on the clock today), plus anything already scheduled or
    dialled today. `--all-leads` restores the whole-campaign pull.

Everything it capped is printed. A truncated sync must never read as complete.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import date
from typing import Any, Iterable, Sequence

from api.db import init_db, load_env, purge_campaigns, current_config

from . import metabase_source as ms
from .red_engine import config_from_settings
from .seed import AGENTS, TEST_NUMBERS

DEFAULT_MAX_CAMPAIGNS = 20
DEFAULT_MAX_LEADS = 5_000

# Campaigns whose name says they are not production. "Newest first with leads
# and a RED" is otherwise a perfect description of a test campaign, so the
# console used to offer `test 26`, `test 1` and `Dev_Test_06-08-2026` alongside
# the real cohorts -- and under DRY_RUN=0 approving one of those dials whatever
# real numbers happen to be sitting in it.
#
# Matched on the name rather than a list of ids on purpose. The reports kept an
# explicit exclusion list and it went stale every time someone made a new test
# campaign; a name pattern covers the one that gets created tomorrow. A campaign
# that is genuinely production must not be named like a test, which is a rule
# worth having anyway. --force-campaigns overrides this for a specific id.
NON_PRODUCTION_WORDS = {
    "test", "tests", "testing", "dev", "demo", "dummy", "sample", "sandbox",
    "staging", "scratch",
    # Not test campaigns, but dead ones that were left dialable: 1574
    # "audit_redial (killed)" and 1421 "paymnet link (link plumbing)".
    "killed", "plumbing", "deprecated", "obsolete",
}


def is_production_campaign(name: Any) -> bool:
    """False when any word in the name marks it as not-for-customers.

    Split on non-alphanumerics rather than using \\b, because `_` is a word
    character to `re` -- `\\bdev\\b` does not match `Dev_Test_06-08-2026`, which
    is exactly the naming style these campaigns use.
    """
    words = re.split(r"[^a-z0-9]+", str(name or "").lower())
    return not (NON_PRODUCTION_WORDS & set(words))


def log(message: str) -> None:
    print(message, flush=True)


def retry(what: str, fn, *args: Any, attempts: int = 4, **kwargs: Any) -> Any:
    """Run a Metabase call, retrying the transport failures it hands out.

    The warehouse gateway drops connections often enough that a single
    ConnectTimeout is noise, not a result. A query that is genuinely wrong
    (bad SQL, 401) fails the same way every time, so it is raised immediately
    rather than burning four attempts on it.
    """
    transient = ("Could not reach Metabase", "504", "502", "Read timed out")
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except ms.MetabaseError as exc:
            if attempt == attempts or not any(t in str(exc) for t in transient):
                raise
            log(f"  ! {what}: {str(exc)[:90]} - retry {attempt}/{attempts - 1}")
            time.sleep(3 * attempt)


# ---------------------------------------------------------------------------
# Warehouse reads
# ---------------------------------------------------------------------------

def campaign_status_flags(status: Any) -> tuple[int, int]:
    """`public.campaigns.status` -> the console's (enabled, paused) pair.

    The warehouse has three states and the console has two flags:
        killed          -> enabled=0, paused=0   (retired; hidden from the roster)
        paused          -> enabled=1, paused=1   (live campaign, dialling stopped)
        active / other  -> enabled=1, paused=0
    An unknown status is treated as active-but-paused would be a silent stop, so
    it maps to enabled+running and shows up under its real name in the console.
    """
    text = str(status or "").strip().lower()
    if text == "killed":
        return 0, 0
    return 1, int(text == "paused")


def fetch_test_campaigns(config: ms.MetabaseConfig, agents: Sequence[int],
                         numbers: Sequence[str]) -> dict[int, int]:
    """{agent_id: newest campaign id holding one of `numbers`}.

    Looked up rather than hardcoded: the rehearsal campaign changes every time
    someone re-uploads the test lead, and a stale literal would silently point
    /api/test-call at a campaign that no longer exists.
    """
    if not agents or not numbers:
        return {}
    phones = ", ".join("'" + "".join(ch for ch in str(n) if ch.isdigit()) + "'"
                       for n in numbers)
    ids = ", ".join(str(int(a)) for a in agents)
    rows = retry("test-number lookup", ms.run_sql, f"""
SELECT DISTINCT ON (l.agent_id) l.agent_id, l.campaign_id
FROM public.leads l
JOIN public.customer c ON c.id = l.customer_id
WHERE l.agent_id IN ({ids})
  AND RIGHT(REGEXP_REPLACE(c.phone_number, '[^0-9]', '', 'g'), 10) IN ({phones})
  AND l.campaign_id IS NOT NULL
ORDER BY l.agent_id, l.id DESC
""".strip(), config, timeout=120)
    return {int(r["agent_id"]): int(r["campaign_id"]) for r in rows}


def fetch_fresh_leads(campaign_id: int, config: ms.MetabaseConfig,
                      dte_min: int | None = None, dte_max: int | None = None,
                      today: date | None = None) -> list[dict[str, Any]]:
    """Direct-from-view lead list for a campaign with NO interaction history.

    `fetch_redial_leads` starts from `public.interactions` — brand-new "first
    dial" leads (stage=new, zero calls) never appear. For force-synced
    campaigns we shape the same output ourselves, so `store_leads` can eat it.

    "No history" means nobody has DIALLED them, not that the interactions table
    is empty: scheduling a call in Formi writes a row with a NULL call_stage. So
    `queued_today` is counted here with the same definition
    `metabase_source.candidate_sql` uses — a campaign uploaded this morning and
    part-scheduled by hand is exactly the case that would otherwise double-dial.
    """
    red_expr = ms.red_parse_expression("v.red")
    # Same "today only" scope as the history path: leads the engine could put on
    # today's clock, plus anything Formi already has scheduled for today.
    window = ""
    if dte_min is not None and dte_max is not None:
        today_sql = f"DATE '{(today or date.today()).isoformat()}'"
        window = (f"  AND ((red.d - {today_sql}) BETWEEN {int(dte_min)} AND {int(dte_max)}\n"
                  f"       OR COALESCE(q.queued_today, 0) > 0)\n")
    rows = retry(f"fresh leads {campaign_id}", ms.run_sql, f"""
SELECT v.id AS warehouse_lead_id, v.uuid AS lead_uuid, v.lead_name,
       LOWER(COALESCE(v.stage, '')) AS stage,
       v.red AS red_raw, red.d AS red, v.policy_no,
       0 AS total_interactions, 0 AS calls_today, 0 AS calls_last_7d,
       COALESCE(q.queued_today, 0) AS queued_today
FROM public.leads_outlet_chola_v v
JOIN public.leads l ON l.id = v.id
CROSS JOIN LATERAL (SELECT {red_expr} AS d) red
LEFT JOIN (
  SELECT i.lead_id, COUNT(*) AS queued_today
  FROM public.interactions i
  WHERE i.campaign_id = {int(campaign_id)}
    AND COALESCE(i.call_stage, '') = ''
    AND (i.scheduled_time AT TIME ZONE 'UTC'
         AT TIME ZONE 'Asia/Kolkata')::date = CURRENT_DATE
  GROUP BY i.lead_id
) q ON q.lead_id = v.id
WHERE l.campaign_id = {int(campaign_id)}
{window}""".strip(), config, timeout=120)
    for r in rows:
        red = r.get("red")
        if red is not None and not isinstance(red, str):
            r["red"] = str(red)
    return rows


def fetch_contacts(campaign_id: int, config: ms.MetabaseConfig) -> dict[int, dict[str, Any]]:
    """{lead_id: {phone, contact_id}} for one campaign.

    `public.leads_outlet_chola_v` carries neither, and `public.leads` has no
    phone column at all — the number lives on `public.customer`. Paged on
    `l.id` because /api/dataset truncates at ~2,000 rows whatever the LIMIT.
    """
    out: dict[int, dict[str, Any]] = {}
    after = 0
    while True:
        rows = retry(f"contacts {campaign_id}", ms.run_sql, f"""
SELECT l.id AS lead_id, l.contact_id, c.phone_number
FROM public.leads l
LEFT JOIN public.customer c ON c.id = l.customer_id
WHERE l.campaign_id = {int(campaign_id)} AND l.id > {after}
ORDER BY l.id
LIMIT {ms.ROW_CAP}
""".strip(), config, timeout=120)
        for row in rows:
            digits = "".join(ch for ch in str(row.get("phone_number") or "") if ch.isdigit())
            out[int(row["lead_id"])] = {
                "phone": digits[-10:] if len(digits) > 10 else (digits or None),
                "contact_id": row.get("contact_id"),
            }
        if len(rows) < ms.ROW_CAP:
            return out
        after = int(rows[-1]["lead_id"])


# ---------------------------------------------------------------------------
# Local store
# ---------------------------------------------------------------------------

def upsert_campaign(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """The warehouse campaign id IS the local id — one less mapping to get wrong.

    `paused` is taken from the warehouse the FIRST time a campaign is seen and
    never again: it is the console's own stop switch, and copying the warehouse
    value on every sync meant an operator who paused a campaign here found it
    running again after the next sync — the console could not hold a decision.

    `enabled` IS still taken, because a campaign killed in Formi must leave the
    roster, and killing it also switches the autopilot off. That is the "or i
    delete it in the redial platform" stop, enforced where it cannot be missed.
    """
    enabled, paused = campaign_status_flags(row.get("campaign_status"))
    campaign_id = int(row["campaign_id"])
    conn.execute(
        "INSERT INTO campaigns (id, agent_id, warehouse_id, name, enabled, paused) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "agent_id=excluded.agent_id, warehouse_id=excluded.warehouse_id, "
        "name=excluded.name, enabled=excluded.enabled, "
        "autopilot=CASE WHEN excluded.enabled=0 THEN 0 ELSE campaigns.autopilot END, "
        "autopilot_note=CASE WHEN excluded.enabled=0 AND campaigns.autopilot=1 "
        "  THEN 'stopped: campaign killed in Formi' ELSE campaigns.autopilot_note END",
        (campaign_id, int(row["agent_id"]), campaign_id,
         str(row.get("campaign_name") or f"campaign {campaign_id}"), enabled, paused))


def _red(lead: dict[str, Any]) -> Any:
    """The RED to store: the warehouse's own reading when it managed one.

    `red_raw` like `4/8/2026` is ambiguous, and the warehouse resolved it using
    the convention it PROVED that campaign uses. Re-parsing the raw text locally
    would re-open the coin flip and could move the lead a bucket. `parse_red`
    takes an ISO *timestamp* on its unambiguous fast path, so that is the shape
    written; an unparseable RED keeps its raw text so the skip is still visible.
    """
    parsed = lead.get("red")
    if parsed in (None, "") or parsed == lead.get("red_raw"):
        return lead.get("red_raw")
    text = str(parsed).replace("T", " ")
    return f"{text.split(' ')[0]} 00:00:00"


def store_leads(conn: sqlite3.Connection, campaign_id: int, leads: Iterable[dict[str, Any]],
                contacts: dict[int, dict[str, Any]]) -> int:
    """Replace one campaign's leads. Delete-then-insert keeps re-runs idempotent."""
    rows = []
    for lead in leads:
        lead_id = lead.get("warehouse_lead_id")
        if lead_id is None:
            continue
        contact = contacts.get(int(lead_id), {})
        rows.append({
            "id": int(lead_id),
            "campaign_id": campaign_id,
            "lead_uuid": lead.get("lead_uuid"),
            "policy_no": lead.get("policy_no"),
            "contact_id": contact.get("contact_id") or lead.get("contact_id"),
            "lead_name": lead.get("lead_name") or lead.get("customer_name"),
            "phone": contact.get("phone"),
            "stage": str(lead.get("stage") or ""),
            "red": _red(lead),
            "last_interaction_time": lead.get("last_interaction_time"),
            "total_interactions": int(lead.get("total_interactions") or 0),
            "calls_today": int(lead.get("calls_today") or 0),
            "calls_last_7d": int(lead.get("calls_last_7d") or 0),
            # Calls somebody put on today's clock in Formi itself. Dropping this
            # is how the console double-books a lead the main system already has.
            "queued_today": int(lead.get("queued_today") or 0),
            # Neither is a column of the warehouse lead view; the engine treats
            # NULL as "no customer-named date", which is the truth here.
            "callback_date": None,
            "appointment_date": None,
        })
    conn.execute("DELETE FROM leads WHERE campaign_id=?", (campaign_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO leads (id, campaign_id, lead_uuid, policy_no, contact_id, "
        "lead_name, phone, stage, red, last_interaction_time, total_interactions, "
        "calls_today, calls_last_7d, queued_today, callback_date, appointment_date) VALUES "
        "(:id,:campaign_id,:lead_uuid,:policy_no,:contact_id,:lead_name,:phone,:stage,:red,"
        ":last_interaction_time,:total_interactions,:calls_today,:calls_last_7d,:queued_today,"
        ":callback_date,:appointment_date)", rows)
    conn.commit()
    return len(rows)


def refresh_campaign_leads(conn: sqlite3.Connection, campaign_id: int,
                           config: ms.MetabaseConfig, schema: Any,
                           today: date | None = None, max_leads: int = DEFAULT_MAX_LEADS,
                           all_leads: bool = False) -> int:
    """Re-pull ONE campaign's leads into the local store. Returns rows stored.

    Two reads, merged, because neither is complete on its own:

      * `fetch_redial_leads` starts from `public.interactions`, so it carries the
        cadence counters — and cannot see a lead nobody has dialled yet. On a
        campaign uploaded this morning that is every lead.
      * `fetch_fresh_leads` reads the lead view directly and sees everyone, but
        reports zero history for all of them, which would reset the counters of
        leads that HAVE been called and re-dial them today.

    So history wins and fresh only fills the gaps. This used to be an either/or
    branch on --force-campaigns, which meant an ordinary sync silently dropped
    every never-dialled lead in a new campaign.

    The RED window comes from the campaign's OWN saved strategy, not the engine
    defaults: an operator who widened the frequency table would otherwise find
    the leads they just asked for missing from the local store.
    """
    window = config_from_settings(current_config(conn, campaign_id))
    dte_min = None if all_leads else window.dte_min
    dte_max = None if all_leads else window.dte_max
    leads = retry(f"leads {campaign_id}", ms.fetch_redial_leads,
                  [campaign_id], config, schema, limit=max_leads, today=today,
                  require_red=not all_leads, keep_today=not all_leads,
                  dte_min=dte_min, dte_max=dte_max)
    seen = {row.get("warehouse_lead_id") for row in leads}
    leads = list(leads) + [row for row in fetch_fresh_leads(
        campaign_id, config, dte_min=dte_min, dte_max=dte_max, today=today)
        if row.get("warehouse_lead_id") not in seen]
    return store_leads(conn, campaign_id, leads, fetch_contacts(campaign_id, config))


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------

def sync(agents: Sequence[int] = tuple(AGENTS),
         max_campaigns: int = DEFAULT_MAX_CAMPAIGNS,
         max_leads: int = DEFAULT_MAX_LEADS,
         today: date | None = None,
         keep_local: bool = False,
         force_campaigns: Sequence[int] = (),
         all_leads: bool = False) -> dict[str, Any]:
    config = ms.load_config()
    schema = retry("schema", ms.describe_schema, config)

    everything: list[dict[str, Any]] = []
    for agent in agents:
        rows = retry(f"campaigns for agent {agent}", ms.fetch_agent_campaigns,
                     agent, config, schema, today)
        log(f"agent {agent}: {len(rows)} campaigns in the warehouse")
        everything.extend(rows)

    forced = fetch_test_campaigns(config, agents, TEST_NUMBERS)
    if forced:
        log("test-number campaigns (always synced): " +
            ", ".join(f"agent {a} -> {c}" for a, c in sorted(forced.items())))

    forced_ids = {int(c) for c in force_campaigns}
    if forced_ids:
        log(f"force-campaigns: {sorted(forced_ids)} (bypassing leads_with_red filter)")

    # Newest first: a redial console is about this week's cohorts, and campaign
    # ids are issued in creation order.
    has_leads = [r for r in everything if r["leads"] and r["leads_with_red"]]
    eligible = sorted((r for r in has_leads
                       if is_production_campaign(r.get("campaign_name"))),
                      key=lambda r: -int(r["campaign_id"]))
    kept_ids = {int(r["campaign_id"]) for r in eligible}
    dropped = [r for r in has_leads if int(r["campaign_id"]) not in kept_ids]
    if dropped:
        log(f"non-production: skipping {len(dropped)} campaign(s) whose name says "
            f"test/dev — " + ", ".join(
                f"{r['campaign_id']} {str(r.get('campaign_name'))[:24]!r}"
                for r in sorted(dropped, key=lambda r: -int(r["campaign_id"]))[:12]))
    chosen = eligible[:max_campaigns]
    chosen_ids = {int(r["campaign_id"]) for r in chosen}
    always = set(forced.values()) | forced_ids
    for row in everything:
        if int(row["campaign_id"]) in always and int(row["campaign_id"]) not in chosen_ids:
            chosen.append(row)
            chosen_ids.add(int(row["campaign_id"]))

    skipped = len(eligible) - min(len(eligible), max_campaigns)
    log(f"CAP: {len(everything)} campaigns seen, {len(eligible)} have leads with a RED, "
        f"syncing {len(chosen)} (--campaigns {max_campaigns}). "
        f"{skipped} eligible campaign(s) NOT synced.")

    conn = init_db()
    total_leads = truncated = 0
    per_campaign: list[tuple[int, str, int]] = []
    try:
        for row in chosen:
            upsert_campaign(conn, row)
        conn.commit()
        if not keep_local:
            dropped = purge_campaigns(conn, chosen_ids)
            log(f"dropped {dropped} campaign(s) that were not in this sync "
                f"(seed data and older syncs)")

        for row in chosen:
            campaign_id = int(row["campaign_id"])
            stored = refresh_campaign_leads(conn, campaign_id, config, schema, today=today,
                                            max_leads=max_leads, all_leads=all_leads)
            with_phone = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND phone IS NOT NULL",
                (campaign_id,)).fetchone()[0]
            capped = "  <-- CAPPED" if stored >= max_leads else ""
            log(f"  {campaign_id:>5} {str(row.get('campaign_name'))[:28]:<28} "
                f"agent {row['agent_id']}  {row.get('campaign_status')!s:<7} "
                f"{stored:>5} leads ({with_phone} with phone){capped}")
            total_leads += stored
            truncated += bool(capped)
            per_campaign.append((campaign_id, str(row.get("campaign_name")), stored))
    finally:
        conn.close()

    log(f"CAP: {total_leads} leads stored; {truncated} campaign(s) hit the "
        f"--leads {max_leads} ceiling and are INCOMPLETE.")
    log("SCOPE: every lead in the campaign (--all-leads)" if all_leads else
        "SCOPE: today only — leads inside each campaign's RED window, plus any "
        "already scheduled or dialled today. Leads outside it are NOT in the "
        "local store and cannot be planned; re-sync with --all-leads for those.")
    return {"campaigns": len(chosen), "leads": total_leads,
            "campaigns_skipped": skipped, "campaigns_truncated": truncated,
            "per_campaign": per_campaign}


def main(argv: Sequence[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--agents", default=",".join(str(a) for a in AGENTS))
    parser.add_argument("--campaigns", type=int, default=DEFAULT_MAX_CAMPAIGNS)
    parser.add_argument("--leads", type=int, default=DEFAULT_MAX_LEADS)
    parser.add_argument("--keep-local", action="store_true",
                        help="do not delete campaigns this sync did not touch")
    parser.add_argument("--force-campaigns", default="",
                        help="comma-separated campaign ids to sync regardless of RED filter")
    parser.add_argument("--all-leads", action="store_true",
                        help="pull every lead, not just the ones in play today")
    args = parser.parse_args(argv)

    agents = [int(a) for a in str(args.agents).split(",") if a.strip()]
    forced = [int(c) for c in str(args.force_campaigns).split(",") if c.strip()]
    try:
        result = sync(agents, args.campaigns, args.leads, keep_local=args.keep_local,
                      force_campaigns=forced, all_leads=args.all_leads)
    except ms.MetabaseError as exc:
        log(f"sync failed: {exc}")
        return 1
    log(f"done: {result['campaigns']} campaigns, {result['leads']} leads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
