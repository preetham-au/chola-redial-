"""Pull the REAL campaigns and leads out of the warehouse into redial.db.

`python -m engine.sync` replaces whatever is in the local store with live data:
the campaign names, ids and statuses agents 125/127 actually have in
`public.campaigns`, and their leads with the phone numbers `engine.seed` used to
invent. `engine.seed` stays as the credential-free offline fallback.

Bounds, because "every campaign x every lead" is ~30k rows of warehouse traffic
per run and the console does not need it:

  * only campaigns with leads AND at least one parseable RED, newest first,
  * --campaigns of them (default 250 — a ceiling, not a working limit; see
    DEFAULT_MAX_CAMPAIGNS), --leads (default 5000) leads each,
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
from typing import Any, Iterable, Optional, Sequence

from api.db import init_db, load_env, purge_campaigns, current_config

from . import metabase_source as ms
from .red_engine import config_from_settings
from .stage_ops import apply_red_overrides
from .seed import AGENTS, TEST_NUMBERS

# A ceiling against a warehouse that has grown without anyone noticing, NOT a
# working limit: in normal operation every eligible campaign is synced and this
# never binds. It was 20, and both callers overrode it with a hand-set 90 --
# which the warehouse quietly outgrew. From 05:15 on 12 Sep 2026 every hourly
# run printed "8 eligible campaign(s) NOT synced": eight campaigns that have
# leads with a RED went unrefreshed run after run, so the console showed
# whatever their counts were the last time they made the cut.
#
# The number lives here and nowhere else now. Two hand-set copies of a bound
# that has to track the warehouse is one copy too many -- the copies do not get
# revisited when it grows, and the growth is silent by definition. Callers take
# the default; the CAP line at the end of `sync` still shouts if this ever binds.
DEFAULT_MAX_CAMPAIGNS = 250
DEFAULT_MAX_LEADS = 5_000

# Campaigns whose name says they are not production.
#
# This used to remove them from the sync. It no longer does -- every campaign the
# agents have is synced and offered, because dropping one on a name guess also
# dropped real cohorts nobody could then see, and a console that hides campaigns
# is harder to trust than one that labels them.
#
# So the words below now only decide what `sync` PRINTS. That is a real reduction
# in safety and is worth naming: "newest first with leads and a RED" describes a
# test campaign perfectly, so `test 26` and `Dev_Test_06-08-2026` now sit in the
# list beside the real cohorts, and under DRY_RUN=0 approving one dials whatever
# real numbers are sitting in it. The name in the list is the only thing standing
# between an operator and that call -- read it before approving.
#
# ponytail: a printed warning, not a guard. If a test campaign is ever approved
# by accident, put the filter back on the approve path (routes_core) rather than
# on the sync, so the campaign stays visible but cannot be dialled.
#
# Matched on the name rather than a list of ids on purpose: an id list goes stale
# every time someone makes a new test campaign, a name pattern covers the one
# created tomorrow.
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

    Pages on `v.id`, for the reason `fetch_redial_leads` does: Metabase's
    /api/dataset stops at `ms.ROW_CAP` rows whatever the SQL says, and a short
    page is the only signal that the last one has arrived. One statement was
    enough until a campaign got bigger than the cap, and then it went wrong in
    the worst available way — silently, and only on NEW campaigns. This is the
    whole of a never-dialled campaign's lead list (the history path returns
    nothing for one), so campaign 1818 "CIFCO SEP" synced 2,000 of its 2,530
    leads on 12 Sep 2026 and the missing 530 were never dialled by anybody. No
    log line said so either: `sync` only cries CAPPED at `--leads` (5,000).
    """
    red_expr = ms.red_parse_expression("v.red")
    # Same "today only" scope as the history path: leads the engine could put on
    # today's clock, plus anything Formi already has scheduled for today.
    window = ""
    if dte_min is not None and dte_max is not None:
        today_sql = f"DATE '{(today or ms.ist_today()).isoformat()}'"
        window = (f"  AND ((red.d - {today_sql}) BETWEEN {int(dte_min)} AND {int(dte_max)}\n"
                  f"       OR COALESCE(q.queued_today, 0) > 0)\n")
    rows: list[dict[str, Any]] = []
    after_id = 0
    for _ in range(ms.MAX_PAGES):
        page = retry(f"fresh leads {campaign_id}", ms.run_sql, f"""
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
  AND v.id > {int(after_id)}
{window}ORDER BY v.id
LIMIT {int(ms.ROW_CAP)}""".strip(), config, timeout=120)
        rows.extend(page)
        # A short page is the last page. Asking for ROW_CAP and getting ROW_CAP
        # means the cap may have cut it, so there is another page to ask for.
        if len(page) < ms.ROW_CAP:
            break
        last = page[-1].get("warehouse_lead_id")
        if last is None or int(last) <= after_id:
            break               # no cursor, or it stalled -- stop rather than loop
        after_id = int(last)
    else:
        raise ms.MetabaseError(
            f"fresh leads {campaign_id}: paging exceeded {ms.MAX_PAGES} pages "
            f"({len(rows):,} rows)")
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

PLATFORM_PAUSE = "paused in the Formi platform"


def upsert_campaign(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    """The warehouse campaign id IS the local id — one less mapping to get wrong.

    Returns True when this sync just stopped the campaign, so the caller can take
    its queued calls off Formi's clock.

    Pausing in Formi is EDGE triggered, not copied. `campaigns.platform_status`
    holds the status the last sync saw, and only a transition INTO `paused` stops
    the campaign here:

        active -> paused   stop it here, and disarm the autopilot
        paused -> paused   do nothing; the operator may have resumed it here
        paused -> active   lift the pause THIS console applied on the platform's
                           behalf, and nothing else. A resume in Formi must NOT
                           restart calls here -- only arming it here does.

    Copying the value on every sync was the old behaviour and it could not hold
    a decision in either direction: an operator who paused a campaign here found
    it running again after the next sync, and — the bug the client reported — a
    campaign paused in Formi after it was first seen was never noticed at all,
    because `paused` was written on INSERT and never on UPDATE.

    `enabled` IS still copied every time: a campaign killed in Formi must leave
    the roster, and killing it also switches the autopilot off.

    A campaign SEEN FOR THE FIRST TIME and active in Formi arrives ARMED. That is
    the one place a sync writes `autopilot`, and only on the INSERT — the UPDATE
    below still never touches it except to switch it off on a kill, so an
    operator who disarms a campaign keeps it disarmed through every later sync.

    Without this, `autopilot` took its schema default of 0 and nothing ever
    raised it: a new campaign synced, appeared in the console with its leads, and
    then sat there. On 12 Sep 2026 campaigns 1818 and 1819 ("CIFCO SEP") were the
    only two of 101 still at 0 — created after the others were armed by hand, and
    never dialled. From the operator's seat that is "new campaigns never sync
    properly", and it is what the standing rule against campaign filters asks for:
    a campaign created in Formi is picked up and runs without anyone finding a
    switch for it.

    Arming is not dialling. An armed campaign is PLANNED by the daily passes and
    the plan still waits for a human to approve it (`api/day.py:approve_day`,
    which nothing calls automatically), so this puts a new campaign in front of
    the operator rather than on the phone. The loud test/dev name warning `sync`
    prints is what stands between an approval and a rehearsal campaign; there is
    deliberately no name filter here.
    """
    enabled, paused = campaign_status_flags(row.get("campaign_status"))
    status = str(row.get("campaign_status") or "").strip().lower()
    campaign_id = int(row["campaign_id"])
    was = conn.execute("SELECT platform_status FROM campaigns WHERE id=?",
                       (campaign_id,)).fetchone()
    seen = str(was["platform_status"] or "") if was else ""
    # Armed on arrival, and only on arrival -- see the docstring. Paused or
    # killed in Formi means it arrives disarmed and the operator switches it on.
    arm = int(bool(enabled) and not paused)
    conn.execute(
        "INSERT INTO campaigns (id, agent_id, warehouse_id, name, enabled, paused, "
        "                       platform_status, autopilot, autopilot_note) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "agent_id=excluded.agent_id, warehouse_id=excluded.warehouse_id, "
        "name=excluded.name, enabled=excluded.enabled, "
        "platform_status=excluded.platform_status, "
        "autopilot=CASE WHEN excluded.enabled=0 THEN 0 ELSE campaigns.autopilot END, "
        "autopilot_note=CASE WHEN excluded.enabled=0 AND campaigns.autopilot=1 "
        "  THEN 'stopped: campaign killed in Formi' ELSE campaigns.autopilot_note END",
        (campaign_id, int(row["agent_id"]), campaign_id,
         str(row.get("campaign_name") or f"campaign {campaign_id}"), enabled, paused,
         status, arm,
         "armed on arrival: new campaign, active in Formi" if arm else ""))
    if seen == "paused" and status != "paused" and enabled:
        # The other edge. "Do nothing" was right about the calls and wrong about
        # the pause: on 12 Sep 2026 the operator resumed a campaign in Formi and
        # the console went on showing it stopped, with the reason "paused in the
        # Formi platform" -- no longer true, and a one-way door, because nothing
        # in Formi could clear a flag only this console writes.
        #
        # So the pause this console applied ON THE PLATFORM'S BEHALF is lifted,
        # and only that one: `stopped_reason` is matched so an operator's own
        # pause here is never undone by a warehouse flag flipping back.
        #
        # The autopilot stays OFF -- a resume in Formi still must not start
        # dialling, which is the rule the old no-op was protecting. The latch
        # goes with the pause it belonged to: it means "this console's stop took
        # your autopilot, this console's resume gives it back", so once that stop
        # is lifted by another route a stale latch would let the NEXT console
        # resume re-arm a campaign nobody armed. Re-arming is a click, on purpose,
        # and the note says so where the operator is already looking.
        conn.execute(
            "UPDATE campaigns SET paused=0, stopped_reason='', autopilot_latched=0, "
            "autopilot_note='resumed in Formi: switch the autopilot back on here to dial' "
            "WHERE id=? AND paused=1 AND stopped_reason=?", (campaign_id, PLATFORM_PAUSE))
    # First sight of an already-paused campaign counts as the edge: it arrives
    # stopped, which is what the INSERT above wrote.
    return bool(paused) and (was is None or seen != "paused")


def refresh_campaign_status(conn: sqlite3.Connection, agents: Sequence[int],
                            config: ms.MetabaseConfig, schema: Any,
                            today: date | None = None) -> list[int]:
    """Re-read Formi's campaign status for `agents` and honour a fresh pause.

    `refresh_campaign_leads` deliberately touches only leads, so between two full
    syncs a campaign paused in Formi kept its local `paused=0` — and the afternoon
    wave planned it and dialled it. This is the same pair `sync` uses, on its own,
    so it can be run before a wave is built: `upsert_campaign` reports the pause
    EDGE and `apply_platform_pause` cancels that campaign's queued calls.

    Only campaigns the console already holds are touched. Upserting the rest would
    re-admit campaigns the sync capped out or dropped for having no parseable RED.
    Returns the ids this call just stopped.
    """
    known = {int(r["id"]) for r in conn.execute("SELECT id FROM campaigns")}
    if not known:
        return []
    stopped: list[int] = []
    for agent in agents:
        for row in ms.fetch_agent_campaigns(agent, config, schema, today):
            if int(row["campaign_id"]) in known and upsert_campaign(conn, row):
                stopped.append(int(row["campaign_id"]))
    conn.commit()
    for campaign_id in stopped:
        apply_platform_pause(conn, campaign_id)
    return stopped


def apply_platform_pause(conn: sqlite3.Connection, campaign_id: int) -> dict[str, Any]:
    """Stop a campaign here because Formi just paused it, calls included.

    Reuses the console's own stop, so a platform pause and an operator pause do
    exactly the same thing — cancel today's queued interactions, disarm the
    autopilot, latch it for the resume. The only difference is the reason
    recorded, which is what the console shows and why the operator, not a later
    sync, is the one who starts it again.
    """
    from api.routes_core import stop_campaign         # noqa: PLC0415 — import cycle

    campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if campaign is None:
        return {"cancelled": 0, "cancel_failed": 0}
    try:
        out = stop_campaign(conn, campaign, PLATFORM_PAUSE)
        conn.commit()
    except Exception as exc:            # a sync must not die on an unreachable Formi
        conn.rollback()
        log(f"  ! campaign {campaign_id} paused in Formi but its queued calls could "
            f"not be cancelled: {str(exc)[:120]}")
        conn.execute("UPDATE campaigns SET paused=1, autopilot=0, stopped_reason=? WHERE id=?",
                     (PLATFORM_PAUSE + " (queued calls NOT cancelled)", campaign_id))
        conn.commit()
        return {"cancelled": 0, "cancel_failed": -1}
    log(f"  campaign {campaign_id} {PLATFORM_PAUSE}: stopped here, "
        f"{out['cancelled']} queued call(s) cancelled")
    return out


def _duration(value: Any) -> Optional[float]:
    """Seconds of the last dial, keeping NULL distinct from zero.

    The warehouse hands this back as a Decimal, which sqlite3 will not bind, so
    it has to become a float on the way through. Anything unparseable is stored
    as NULL -- the same as "no call connected", which is the reading that leads
    to one extra dial rather than to a lead in the critical window being dropped.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
            # `or 0` would be wrong here: the warehouse's NULL is the signal that
            # the last dial never connected, and 0 would read as a call that did.
            "last_call_duration_sec": _duration(lead.get("last_call_duration_sec")),
            # Neither is a column of the warehouse lead view; the engine treats
            # NULL as "no customer-named date", which is the truth here.
            "callback_date": None,
            "appointment_date": None,
        })
    conn.execute("DELETE FROM leads WHERE campaign_id=?", (campaign_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO leads (id, campaign_id, lead_uuid, policy_no, contact_id, "
        "lead_name, phone, stage, red, last_interaction_time, total_interactions, "
        "calls_today, calls_last_7d, queued_today, last_call_duration_sec, "
        "callback_date, appointment_date) VALUES "
        "(:id,:campaign_id,:lead_uuid,:policy_no,:contact_id,:lead_name,:phone,:stage,:red,"
        ":last_interaction_time,:total_interactions,:calls_today,:calls_last_7d,:queued_today,"
        ":last_call_duration_sec,:callback_date,:appointment_date)", rows)
    conn.commit()
    # The DELETE above takes a renewal date corrected in the console with it, so
    # it is put back here. Overrides the warehouse has since moved past are
    # dropped inside — see `apply_red_overrides`.
    apply_red_overrides(conn, campaign_id)
    return len(rows)


def campaign_fingerprint(conn: sqlite3.Connection, row: dict[str, Any],
                         today: date | None, max_leads: int, all_leads: bool) -> str:
    """Everything that decides what a lead pull for this campaign would return.

    The point of it is to not make the pull. Each campaign costs two Metabase
    round trips -- `fetch_redial_leads` and `fetch_fresh_leads` -- about 1.7s,
    and on 12 Sep 2026 that was 2m50s of a 3m run across 99 campaigns, most of
    which had not changed since the sync an hour before. The campaigns query
    that opens every run already computes the aggregates below for all 121
    campaigns in about 9s, so the cheap read can tell us which expensive ones to
    skip.

    A COUNT on its own would not be safe -- the user's first instinct, and the
    reason this is a tuple rather than `leads`. A lead that gets dialled changes
    stage and disposition while the count stands still, and stage is most of
    what the console shows. So every counter that moves when a lead moves is in
    here: `dials`, `connected_dials` and `queued_today` all step on a dial, and
    `last_dial_at` moves even when a re-dial leaves all three equal.

    Also in here, and not from the warehouse at all:

      * `today`, because the RED window is relative to it. Two runs either side
        of midnight IST must not agree, and a rollover that happens to leave the
        same number of leads in the window would otherwise be invisible. This
        also buys a full, unconditional pull once a day.
      * the campaign's OWN saved window, because `refresh_campaign_leads` reads
        it from `current_config` rather than the engine defaults. An operator
        widening the frequency table changes which leads a pull returns while
        the warehouse says nothing at all.
      * `max_leads` and `all_leads`, which change the shape of the pull itself.

    What it still cannot see: a RED edited from one in-window date to another,
    or a stage changed in the Formi platform without a dial. Both leave every
    counter above standing. The daily `today` rollover is what bounds that, and
    `--full` is the way to force the question now.
    """
    window = config_from_settings(current_config(conn, campaign_id := int(row["campaign_id"])))
    stored = conn.execute("SELECT COUNT(*) FROM leads WHERE campaign_id=?",
                          (campaign_id,)).fetchone()[0]
    return "|".join(str(part) for part in (
        "v1", row.get("campaign_status"), row.get("leads"), row.get("leads_with_red"),
        row.get("leads_in_red_window"), row.get("dials"), row.get("connected_dials"),
        row.get("queued_today"), row.get("last_dial_at"),
        today or ms.ist_today(), window.dte_min, window.dte_max, max_leads, all_leads,
        # The local side of it: a store that lost its leads must re-pull even
        # though the warehouse has not moved a digit.
        stored,
    ))


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
         all_leads: bool = False,
         full: bool = False) -> dict[str, Any]:
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
    eligible = sorted(has_leads, key=lambda r: -int(r["campaign_id"]))

    # A campaign with leads but no PARSEABLE RED drops out of `has_leads` and
    # then out of the console with nothing said, which is how 1740/1744/1746
    # went missing on 5 Sep 2026: that upload wrote RED as 'eleventh september'
    # and every row parsed to NULL. The parser handles that shape now, but the
    # next upload will invent another one, so the silence is what gets fixed
    # here -- a campaign with real dials and zero REDs is a parser bug report,
    # not a normal state, and it says so on the way past.
    no_red = sorted((r for r in everything if r["leads"] and not r["leads_with_red"]),
                    key=lambda r: -int(r["leads"]))
    if no_red:
        log(f"WARNING: {len(no_red)} campaign(s) have leads but NOT ONE parseable RED, so "
            f"they are NOT synced and will not appear in the console. Check the RED format "
            f"against red_engine.parse_red — " + ", ".join(
                f"{r['campaign_id']} {str(r.get('campaign_name'))[:24]!r} ({r['leads']} leads)"
                for r in no_red[:12]))
    flagged = [r for r in eligible if not is_production_campaign(r.get("campaign_name"))]
    if flagged:
        log(f"WARNING: {len(flagged)} campaign(s) whose name says test/dev are being "
            f"synced and WILL be offered for approval — " + ", ".join(
                f"{r['campaign_id']} {str(r.get('campaign_name'))[:24]!r}"
                for r in flagged[:12]))
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
    total_leads = truncated = unchanged = 0
    per_campaign: list[tuple[int, str, int]] = []
    try:
        stopped = [row for row in chosen if upsert_campaign(conn, row)]
        conn.commit()
        for row in stopped:
            apply_platform_pause(conn, int(row["campaign_id"]))
        if not keep_local:
            dropped = purge_campaigns(conn, chosen_ids)
            log(f"dropped {dropped} campaign(s) that were not in this sync "
                f"(seed data and older syncs)")

        for row in chosen:
            campaign_id = int(row["campaign_id"])
            whole = all_leads or campaign_id in always
            fingerprint = campaign_fingerprint(conn, row, today, max_leads, whole)
            was = conn.execute("SELECT sync_fingerprint FROM campaigns WHERE id=?",
                               (campaign_id,)).fetchone()
            if not full and was and was[0] and was[0] == fingerprint:
                # Nothing the warehouse can tell us about this campaign has moved
                # since its leads were last pulled, so pulling them again buys two
                # round trips and the same rows back. See campaign_fingerprint for
                # what that claim does and does not cover.
                stored = conn.execute("SELECT COUNT(*) FROM leads WHERE campaign_id=?",
                                      (campaign_id,)).fetchone()[0]
                unchanged += 1
                total_leads += stored
                per_campaign.append((campaign_id, str(row.get("campaign_name")), stored))
                continue

            # A campaign is force-synced *because* of the lead we want in it —
            # the test number, or one an operator named. Applying the RED window
            # to it then drops that very lead and stores the campaign with zero,
            # which is how /api/test-call/numbers came to answer "no lead on this
            # number" for a number the sync had just gone out of its way to find.
            stored = refresh_campaign_leads(conn, campaign_id, config, schema, today=today,
                                            max_leads=max_leads, all_leads=whole)
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
            # Written only now, and only from the row that produced these leads:
            # stamping it before the pull would mark a campaign fresh on a run
            # that died halfway through it. Recomputed because the pull itself
            # changed the local lead count the fingerprint carries.
            conn.execute("UPDATE campaigns SET sync_fingerprint=? WHERE id=?",
                         (campaign_fingerprint(conn, row, today, max_leads, whole),
                          campaign_id))
            conn.commit()
    finally:
        conn.close()

    log(f"CAP: {total_leads} leads stored; {truncated} campaign(s) hit the "
        f"--leads {max_leads} ceiling and are INCOMPLETE.")
    # Said out loud every run: a fast sync and a broken sync look identical from
    # the outside, and "it finished in 20 seconds" should be a number somebody
    # can check rather than a thing to be relieved about.
    log(f"UNCHANGED: {unchanged} of {len(chosen)} campaign(s) had not moved since their "
        f"last pull, so their leads were not re-fetched ({len(chosen) - unchanged} "
        f"pulled). Re-run with --full to pull every campaign regardless.")
    log("SCOPE: every lead in the campaign (--all-leads)" if all_leads else
        "SCOPE: today only — leads inside each campaign's RED window, plus any "
        "already scheduled or dialled today. Leads outside it are NOT in the "
        "local store and cannot be planned; re-sync with --all-leads for those.")
    return {"campaigns": len(chosen), "leads": total_leads,
            "campaigns_skipped": skipped, "campaigns_truncated": truncated,
            "campaigns_unchanged": unchanged, "per_campaign": per_campaign}


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
    parser.add_argument("--full", action="store_true",
                        help="re-pull every campaign's leads even if nothing has changed")
    args = parser.parse_args(argv)

    agents = [int(a) for a in str(args.agents).split(",") if a.strip()]
    forced = [int(c) for c in str(args.force_campaigns).split(",") if c.strip()]
    try:
        result = sync(agents, args.campaigns, args.leads, keep_local=args.keep_local,
                      force_campaigns=forced, all_leads=args.all_leads, full=args.full)
    except ms.MetabaseError as exc:
        log(f"sync failed: {exc}")
        return 1
    log(f"done: {result['campaigns']} campaigns, {result['leads']} leads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
