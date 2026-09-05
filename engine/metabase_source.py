"""Metabase-backed data source for RED redial scheduling.

Replaces the Formi lead-list pagination in `schedule_redials.py`. The old path
issued roughly `ceil(leads/100)` requests per campaign plus a separate
already-scheduled sweep — for 21 campaigns of ~10k leads that is >2,000 HTTP
round trips per run. This module answers the same questions with **two** SQL
statements against the warehouse, and computes the per-lead call history
(attempts, calls today, calls in the last 7 days, already-queued-today) inside
the database instead of in Python.

Why the schema is discovered at runtime
---------------------------------------
`public.leads_outlet_chola_v` is a view that has gained columns over time and
`public.campaigns` differs between environments. Rather than hardcode a column
list that fails the whole run with `UndefinedColumn`, `describe_schema()` reads
`information_schema.columns` once and the query builders project only columns
that actually exist. `schema_report()` turns that into an operator-facing
readiness check, surfaced by the console so a missing column is a visible
warning rather than a 500.

Config (environment only — this module never reads a .env file so it behaves
identically on Render and in a shell):

    METABASE_URL     e.g. https://metabase-internal.formi.co.in
    METABASE_API_KEY Metabase admin -> Authentication -> API Keys
    METABASE_DB_ID   e.g. 2
    CHOLA_OUTLET_ID  optional, defaults to 1497
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import requests

__all__ = [
    "MetabaseError",
    "MetabaseConfig",
    "load_config",
    "run_sql",
    "describe_schema",
    "schema_report",
    "build_campaign_stats_sql",
    "build_agent_campaigns_sql",
    "build_leads_sql",
    "fetch_campaign_stats",
    "fetch_agent_campaigns",
    "fetch_redial_leads",
    "red_parse_expression",
    "REQUIRED_LEAD_COLUMNS",
    "OPTIONAL_LEAD_COLUMNS",
    "LEAD_COLUMN_ALIASES",
    "LEAD_UUID_COLUMNS",
    "RED_PARSE_SQL",
    "REAL_DIAL_STAGES",
    "LEADS_VIEW",
]


class MetabaseError(RuntimeError):
    """Raised for configuration, transport, and query failures alike."""


DEFAULT_OUTLET_ID = 1497
LEADS_VIEW = "leads_outlet_chola_v"
INTERACTIONS_TABLE = "interactions"
CAMPAIGNS_TABLE = "campaigns"

# Metabase's /api/dataset caps a native query's result set at ~2,000 rows no
# matter what the SQL LIMIT says (the report SQL documents the same cap). Lead
# fetches therefore page with a keyset cursor instead of trusting one statement.
# Override with METABASE_ROW_CAP if a deployment raises the server-side limit.
ROW_CAP = int(os.environ.get("METABASE_ROW_CAP") or 2000)
MAX_PAGES = 500

# A row in `interactions` is a real dial attempt only for these call stages.
# An empty/NULL call_stage means the interaction is queued and was never dialled,
# which is exactly how we detect "already scheduled for today".
REAL_DIAL_STAGES = ("dnp", "complete", "completed", "telephony_failed", "follow_up_required")
CONNECTED_STAGES = ("complete", "completed")

# `connected_dials` is judged by disposition, not call_stage, matching the daily
# report. call_stage='complete' includes voicemail and calls nobody answered —
# 38% of stage-connected calls carry a not-contacted disposition — so a
# stage-based counter overstates connectivity on the console too.
#
# These two tuples are a verbatim copy of CONTACTED_LABELS / MACHINE_LABELS in
# scripts/reports/build_day_overview_daily.py, flattened to raw aliases. They
# are duplicated rather than imported because that module pulls in
# sharepoint_client at import time, which has no business loading on Render's
# redial path. Nothing checks the copy against the original -- this comment used
# to claim tests/test_redial_metabase_source.py did, and that file has never
# existed -- so drift here is silent. Compare by eye when either list moves.
CONTACTED_DISPOSITIONS = (
    "lead_appointment_fixed", "appointment_fixed", "lead_cmrl_interested",
    "cmrl_interested", "lead_directed_to_branch", "directed_to_branch",
    "lead_premium_quotation_required", "lead_premium_quotation",
    "share_premium_quotation", "premium_quotation_required",
    "lead_link_sent_online", "payment_link_sent", "link_sent_online",
    "positive_followup", "lead_positive_followup", "promise_to_renew",
    "committed_to_pay", "agreed_to_pay_with_date", "follow_up_required",
    "potentially_interested", "agent_number", "alternate_contact_given",
    "already_paid_to_chola", "call_back", "do_not_call", "hung_up",
    "hung_up_no_contact", "lost", "firm_decision_to_discontinue",
    "not_interested", "other_language", "wrong_number", "others", "other",
    "lead_transferred_to_sales", "requested_human_agent_connect",
)
# A machine answered. Vetoes the duration/transcript evidence below, because a
# voicemail greeting runs 60-80s and otherwise looks like a conversation.
MACHINE_DISPOSITIONS = (
    "voicemail", "voicemail_ivr", "unreachable",
    "beep_tone_number_busy_not_reachable_switched_off",
)

# Both tuples above are spelled as SUBS, so the column they are matched against
# has to be one. `LOWER(lead_stage_computed)`, which is what these queries used,
# is a sub in neither era:
#
#   from 31 Aug 2026  the sub moved into lead_stage_reasoning and `computed` was
#                     left holding the coarse group. On 3 Sep that is 3,179 rows
#                     reading `contacted` and 169 reading `not_contacted` out of
#                     7,268 -- none of which is in either tuple, so the label arm
#                     of _connected_predicate never fired and voicemail lost its
#                     veto, leaving a 70s greeting to count as a conversation.
#   until 30 Aug 2026 the sub WAS in `computed`, but under a `sub_` prefix that
#                     no entry above carries: `sub_hung_up`, not `hung_up`.
#
# So the label arm has been dead in one direction or the other for the whole
# history. Same expression as build_day_overview_daily.py :: DISPOSITION_SQL and
# dashboard/server/src/db/syncSql.ts, minus their `immediate_did_not_pick` arm --
# that one moves rows out of MACHINE_DISPOSITIONS and so changes this file's
# duration-evidence arm, which is redial's alone. Separate question, left open.
DISPOSITION_SQL = """regexp_replace(
           COALESCE(
             NULLIF(LOWER((regexp_match(i.lead_stage_reasoning, 'sub=([A-Za-z0-9_]+)'))[1]), ''),
             LOWER(COALESCE(i.lead_stage_computed, ''))
           ), '^sub_', '')"""


def _connected_predicate(alias: str) -> str:
    """SQL for "a human was actually reached", for rows of the `activity` CTE.

    Either the disposition says a person spoke, or there is billed duration with
    no machine-answer disposition contradicting it. The evidence arm matters
    because disposition coverage is patchy and sometimes plainly wrong —
    'did_not_pick' on a 'complete' stage averages 46.7s.

    Duration only, no transcript check, unlike the daily report. The report
    scopes to ONE day; this query scans an agent's entire interaction history
    with no date filter, and running jsonb_array_length over every row of it
    times the statement out at the Metabase gateway. Duration alone covers all
    but the handful of rows where post-call processing lost it, which the
    report's Discrepancy sheet already tracks.
    """
    contacted = ", ".join(f"'{d}'" for d in CONTACTED_DISPOSITIONS)
    machine = ", ".join(f"'{d}'" for d in MACHINE_DISPOSITIONS)
    stages = ", ".join(f"'{s}'" for s in CONNECTED_STAGES)
    # telephony_failed is excluded from BOTH arms. The evidence arm already
    # rules it out via CONNECTED_STAGES, but the contacted arm did not check
    # the stage at all, so 13 dials that never reached the network yet carry a
    # contacted disposition were counting as connected here while the report
    # had stopped counting them.
    return (f"({alias}.call_stage <> 'telephony_failed' AND ("
            f"{alias}.disposition IN ({contacted})"
            f" OR ({alias}.disposition NOT IN ({machine})"
            f" AND {alias}.call_stage IN ({stages})"
            f" AND {alias}.duration_sec > 0)))")


# Columns the leads query cannot work without.
REQUIRED_LEAD_COLUMNS = ("id", "red", "stage")
# Projected when present; each one unlocks a feature rather than being fatal.
#   lead_uuid / uuid -> lets the commit path POST a schedule without a Formi lookup
#   contact_id       -> cross-campaign duplicate suppression
#   policy_no        -> human-readable audit rows
#   phone            -> the dialable number shown on the plan (blank without it)
OPTIONAL_LEAD_COLUMNS = ("lead_uuid", "uuid", "contact_id", "policy_no", "connected",
                         "customer_name", "lead_name", "campaign_id", "dynamic_variables",
                         "phone", "mobile", "mobile_no", "phone_number")

# The live view spells the Formi lead UUID `uuid`, but the scheduler and the
# engine both expect `lead_uuid`. Alias on the way out so callers never have to
# care which spelling an environment uses.
LEAD_COLUMN_ALIASES = {"uuid": "lead_uuid",
                       "mobile": "phone", "mobile_no": "phone", "phone_number": "phone"}

# Whichever of these the view has provides the Formi lead UUID needed to commit.
LEAD_UUID_COLUMNS = ("lead_uuid", "uuid")


def _optional_projection(leads: Iterable[str]) -> list[tuple[str, str]]:
    """(view_column, output_name) pairs for the optional columns that exist.

    Guarantees a stable output name per feature: if a view happened to have both
    `lead_uuid` and `uuid`, only the first is projected so the result set never
    carries two columns called `lead_uuid`.
    """
    available = set(leads)
    pairs: list[tuple[str, str]] = []
    taken: set[str] = set()
    for column in OPTIONAL_LEAD_COLUMNS:
        if column not in available:
            continue
        output = LEAD_COLUMN_ALIASES.get(column, column)
        if output in taken:
            continue
        taken.add(output)
        pairs.append((column, output))
    return pairs

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# RED parsing, in SQL
# ---------------------------------------------------------------------------
# Kept byte-for-byte consistent with the report SQL (and with
# red_engine.parse_red) so a lead's RED resolves identically in the warehouse,
# in the bucket preview, and in the scheduler. `%(col)s`-free on purpose: this
# is spliced into native SQL, so the caller passes a validated identifier.

def _days_in_month_sql(year: str, month: str) -> str:
    """Length of a month, as cheap integer arithmetic.

    Deliberately NOT `EXTRACT(DAY FROM MAKE_DATE(...) + INTERVAL '1 month' - ...)`:
    that form is correct but it puts a date construction and interval arithmetic
    inside a guard that is evaluated for every row, and the surrounding CASE
    repeats it many times over. On the 21-campaign report that was enough to make
    Metabase time out at the gateway. A static lookup with a leap-year test costs
    a few integer comparisons instead.
    """
    leap = f"(({year}) % 4 = 0 AND (({year}) % 100 <> 0 OR ({year}) % 400 = 0))"
    return (f"CASE ({month}) WHEN 2 THEN (CASE WHEN {leap} THEN 29 ELSE 28 END) "
            f"WHEN 4 THEN 30 WHEN 6 THEN 30 WHEN 9 THEN 30 WHEN 11 THEN 30 "
            f"ELSE 31 END")


def _strict_ymd_sql(text: str, sep: str = "-") -> str:
    """Strictly yyyy-mm-dd, with no day/month ambiguity resolution.

    Used only for machine-written ISO timestamps, whose component order is not
    in doubt. Running those through the ambiguity rules would let the
    renewal-month heuristic reinterpret e.g. '2026-09-08 00:00:00' as
    yyyy-dd-mm. Still guarded so it can never raise.
    """
    year = f"SPLIT_PART({text}, '{sep}', 1)::int"
    month = f"SPLIT_PART({text}, '{sep}', 2)::int"
    day = f"SPLIT_PART({text}, '{sep}', 3)::int"
    return (f"CASE WHEN {year} BETWEEN 1900 AND 2200 AND {month} BETWEEN 1 AND 12"
            f" AND {day} BETWEEN 1 AND {_days_in_month_sql(year, month)}"
            f" THEN MAKE_DATE({year}, {month}, {day}) ELSE NULL END")


def _three_part_sql(text: str, sep: str, year_first: bool, month_first_default: bool,
                    month_first_expr: str | None = None) -> str:
    """Resolve a 3-part date whose day/month order is not guaranteed.

    Evidence order, identical to `red_engine._resolve_three_part`:
      1. a component > 12 must be the day
      2. both > 12 is impossible -> NULL
      3. `month_first_expr`, when supplied: that campaign's OWN proven
         convention, derived from its unambiguous values
      4. a component in the live renewal months (8, 9) is likely the month
      5. otherwise fall back to `month_first_default`

    Rule 3 is what makes `8/9/2026` resolvable. Format is consistent within a
    campaign but differs BETWEEN campaigns (verified: 0 of 42 campaigns are
    internally mixed, yet slash campaigns split both ways), so the campaign's
    own evidence beats any global heuristic.

    Structure note: the month and the day are each selected ONCE as an integer
    CASE, and there is exactly one MAKE_DATE call. The earlier version inlined a
    full date construction per branch, which the query planner had to evaluate
    repeatedly and which timed out on the report's campaign set.
    """
    a_index, b_index = (2, 3) if year_first else (1, 2)
    year_index = 1 if year_first else 3
    a = f"SPLIT_PART({text}, '{sep}', {a_index})::int"
    b = f"SPLIT_PART({text}, '{sep}', {b_index})::int"
    raw_year = f"SPLIT_PART({text}, '{sep}', {year_index})::int"
    year = (f"(CASE WHEN LENGTH(SPLIT_PART({text}, '{sep}', {year_index})) = 2 "
            f"THEN 2000 + {raw_year} ELSE {raw_year} END)")

    campaign_rule = ""
    if month_first_expr:
        campaign_rule = (f" WHEN {month_first_expr} IS TRUE THEN {{month_pick}}"
                         f" WHEN {month_first_expr} IS FALSE THEN {{day_pick}}")

    def pick(a_when_month_first: str, b_when_month_first: str) -> str:
        """Build the selector for either the month or the day component."""
        rule = campaign_rule.format(month_pick=a_when_month_first,
                                    day_pick=b_when_month_first)
        default = a_when_month_first if month_first_default else b_when_month_first
        return (f"(CASE WHEN {a} > 12 THEN {b_when_month_first}"
                f" WHEN {b} > 12 THEN {a_when_month_first}"
                f"{rule}"
                f" WHEN {b} IN (8, 9) THEN {b_when_month_first}"
                f" WHEN {a} IN (8, 9) THEN {a_when_month_first}"
                f" ELSE {default} END)")

    # month: 'a' when a is the month, 'b' when b is the month.
    month = pick(a, b)
    # day is the mirror image: 'b' when a is the month, 'a' when b is the month.
    day = pick(b, a)

    return (f"CASE WHEN {a} BETWEEN 1 AND 31 AND {b} BETWEEN 1 AND 31"
            f" AND NOT ({a} > 12 AND {b} > 12)"
            f" AND {year} BETWEEN 1900 AND 2200"
            f" AND {month} BETWEEN 1 AND 12"
            f" AND {day} BETWEEN 1 AND {_days_in_month_sql(year, month)}"
            f" THEN MAKE_DATE({year}, {month}, {day}) ELSE NULL END")


_MONTH_ABBR = ("jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec")


def _day_month_sql(text: str, ref: str) -> str:
    """`dd-Mon` carrying NO year -> the occurrence nearest `ref`.

    Chola's 29-Aug upload (campaigns 1703/1706) writes RED as bare '04-Sep'.
    The year is INFERRED rather than assumed to be the current one, so a
    '05-Jan' read in December resolves to next January instead of eleven months
    into the past — the difference between dialling that lead and never seeing it.

    The near/far test compares `mmdd` integers, not dates, because the year is
    exactly what is not yet known — and 600 (~6 months) is the halfway point, so
    each value lands on whichever side of `ref` it is closer to. Anything that
    far out is outside every dial window anyway, so the fuzziness of the
    boundary costs nothing.
    """
    day = f"SPLIT_PART({text}, '-', 1)::int"
    cases = " ".join(f"WHEN '{m}' THEN {i}" for i, m in enumerate(_MONTH_ABBR, 1))
    month = f"(CASE LOWER(LEFT(SPLIT_PART({text}, '-', 2), 3)) {cases} ELSE NULL END)"
    delta = (f"(({month}) * 100 + {day}"
             f" - EXTRACT(MONTH FROM {ref})::int * 100 - EXTRACT(DAY FROM {ref})::int)")
    year = (f"(EXTRACT(YEAR FROM {ref})::int"
            f" + CASE WHEN {delta} > 600 THEN -1 WHEN {delta} < -600 THEN 1 ELSE 0 END)")
    return (f"CASE WHEN {month} IS NOT NULL"
            f" AND {day} BETWEEN 1 AND {_days_in_month_sql(year, month)}"
            f" THEN MAKE_DATE({year}, {month}, {day}) ELSE NULL END")


def red_parse_expression(column: str = "v.red", month_first_expr: str | None = None,
                         today: date | None = None) -> str:
    """A CASE expression that turns Chola's free-text RED into a date.

    Kept in lockstep with `red_engine.parse_red`, verified against every distinct
    real value in the warehouse. Handles, in order:

      * ISO timestamps            2026-09-15 00:00:00  (machine-written, so ISO)
      * yyyy-A-B                  2026-09-15 AND 2026-15-09 (yyyy-dd-mm)
      * A-B-yyyy                  15-09-2026  (day-first: proven by 6,150 leads,
                                   with zero counter-examples)
      * A/B/yyyy                  15/9/2026 and 9/15/2026 -- this shape really is
                                   MIXED in production (1,064 leads prove
                                   day-first, 4,680 prove month-first), so the
                                   >12 and renewal-month rules decide each row
      * d-Mon-yyyy                1-Sep-2026
      * d-Mon                     4-Sep -- year missing, inferred from `today`
      * 2-digit years, '.' as a separator, and a SPACE where a named month's
        separator should be ('02 Sep', '02 September')

    Anything else, and anything self-contradictory, yields NULL rather than a
    wrong date. Nothing in here can raise.
    """
    text = f"NULLIF(BTRIM(REPLACE(({column})::text, '.', '-')), '')"
    slashed = f"NULLIF(BTRIM(({column})::text), '')"
    # Named months only. Spaces cannot be normalised in `text` because the ISO
    # branch matches the space between date and time ('2026-09-15 00:00:00').
    named = (f"NULLIF(REGEXP_REPLACE(BTRIM(({column})::text), "
             f"'[[:space:].]+', '-', 'g'), '')")
    ref = f"DATE '{today.isoformat()}'" if today else "CURRENT_DATE"
    return f"""CASE
    WHEN {column} IS NULL OR LOWER(BTRIM(({column})::text)) IN ('', 'null', 'none', 'nan', '-') THEN NULL
    -- Machine-written timestamp: always ISO. Parsed via MAKE_DATE so an
    -- out-of-range component cannot abort the query.
    WHEN {text} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}[ T]' THEN
      {_strict_ymd_sql(f"LEFT({text}, 10)")}
    WHEN {text} ~ '^[0-9]{{4}}-[0-9]{{1,2}}-[0-9]{{1,2}}$' THEN
      {_three_part_sql(text, "-", year_first=True, month_first_default=True,
                       month_first_expr=month_first_expr)}
    WHEN {text} ~ '^[0-9]{{1,2}}-[0-9]{{1,2}}-([0-9]{{2}}|[0-9]{{4}})$' THEN
      {_three_part_sql(text, "-", year_first=False, month_first_default=False,
                       month_first_expr=month_first_expr)}
    WHEN {named} ~ '^[0-9]{{1,2}}-[A-Za-z]{{3,}}-[0-9]{{4}}$' THEN TO_DATE({named}, 'FMDD-Mon-YYYY')
    -- Year-less named month ('04-Sep', '02 Sep'): the year is inferred, never
    -- assumed to be the current one. Matches red_engine.parse_red.
    WHEN {named} ~ '^[0-9]{{1,2}}-[A-Za-z]{{3,}}$' THEN {_day_month_sql(named, ref)}
    WHEN {slashed} ~ '^[0-9]{{1,2}}/[0-9]{{1,2}}/([0-9]{{2}}|[0-9]{{4}})$' THEN
      {_three_part_sql(slashed, "/", year_first=False, month_first_default=False,
                       month_first_expr=month_first_expr)}
    ELSE NULL
  END"""


# Extracts the two ambiguous components of a 3-part date so a campaign's own
# convention can be learned from its unambiguous rows. Separator-agnostic: '.'
# and '/' are normalised to '-' first, because per-campaign evidence makes the
# separator-specific default unnecessary.
def _order_votes_sql(column: str = "v.red") -> tuple[str, str]:
    """Return (first_component, second_component) SQL for day/month voting."""
    norm = f"BTRIM(REPLACE(REPLACE(({column})::text, '.', '-'), '/', '-'))"
    guard = f"{norm} ~ '^[0-9]{{1,2}}-[0-9]{{1,2}}-([0-9]{{2}}|[0-9]{{4}})$'"
    first = f"CASE WHEN {guard} THEN SPLIT_PART({norm}, '-', 1)::int END"
    second = f"CASE WHEN {guard} THEN SPLIT_PART({norm}, '-', 2)::int END"
    return first, second


RED_PARSE_SQL = red_parse_expression()


# ---------------------------------------------------------------------------
# Config + transport
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetabaseConfig:
    url: str
    api_key: str
    database_id: int
    outlet_id: int = DEFAULT_OUTLET_ID

    @property
    def dataset_url(self) -> str:
        return f"{self.url.rstrip('/')}/api/dataset"


ENV_FILE = Path(__file__).resolve().parent.parent / "reports" / "config" / ".env"

# Keys read from the environment, then from ENV_FILE as a local-dev fallback.
_CONFIG_KEYS = ("METABASE_URL", "METABASE_API_KEY", "METABASE_DB_ID", "CHOLA_OUTLET_ID")


def _read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse `KEY=value` lines from the shared reports .env, if it exists.

    On Render the values come from real environment variables and this file is
    absent, so this is purely a local-shell convenience. Precedence is
    `process env > .env file`, matching scripts/reports/metabase_client.py.
    Only the keys in `_CONFIG_KEYS` are read, so unrelated credentials in the
    same file are never loaded into memory.

    `path` is resolved at CALL time, not bound as a default, so the module-level
    ENV_FILE stays overridable.
    """
    path = ENV_FILE if path is None else path
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _CONFIG_KEYS:
            values[key] = value.strip().strip('"').strip("'")
    return values


def load_config(env: dict[str, str] | None = None,
                use_env_file: bool | None = None) -> MetabaseConfig:
    """Read config from the environment, raising an operator-readable error.

    When reading the real process environment, any key it does not supply is
    filled from the shared reports `.env`, so local dry runs work without
    exporting variables by hand. Passing an explicit `env` mapping means "use
    exactly this" and does NOT consult the file — otherwise a caller (or a test)
    could not describe a genuinely unconfigured environment, and behaviour would
    silently depend on whether a secrets file happened to be present on disk.

    Set `use_env_file` to force either behaviour.
    """
    explicit = env is not None
    env = dict(env) if explicit else dict(os.environ)
    if use_env_file is None:
        use_env_file = not explicit

    if use_env_file:
        fallback = None
        for key in _CONFIG_KEYS:
            if (env.get(key) or "").strip():
                continue
            if fallback is None:
                fallback = _read_env_file()
            if fallback.get(key):
                env[key] = fallback[key]

    missing = [key for key in ("METABASE_URL", "METABASE_API_KEY", "METABASE_DB_ID")
               if not (env.get(key) or "").strip()]
    if missing:
        raise MetabaseError(
            "Metabase is not configured. Set " + ", ".join(missing) +
            " in the Render dashboard (METABASE_API_KEY comes from Metabase admin "
            "-> Authentication -> API Keys)."
        )
    try:
        database_id = int(str(env["METABASE_DB_ID"]).strip())
    except (TypeError, ValueError):
        raise MetabaseError("METABASE_DB_ID must be a whole number") from None
    try:
        outlet_id = int(str(env.get("CHOLA_OUTLET_ID") or DEFAULT_OUTLET_ID).strip())
    except (TypeError, ValueError):
        raise MetabaseError("CHOLA_OUTLET_ID must be a whole number") from None
    return MetabaseConfig(url=env["METABASE_URL"].strip(),
                          api_key=env["METABASE_API_KEY"].strip(),
                          database_id=database_id, outlet_id=outlet_id)


def run_sql(sql: str, config: MetabaseConfig | None = None, timeout: int = 300) -> list[dict[str, Any]]:
    """POST /api/dataset and zip Metabase's cols/rows into dicts."""
    config = config or load_config()
    try:
        response = requests.post(
            config.dataset_url,
            json={"type": "native", "native": {"query": sql}, "database": config.database_id},
            headers={"x-api-key": config.api_key, "Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise MetabaseError(f"Could not reach Metabase: {type(exc).__name__}") from exc

    if response.status_code == 401:
        raise MetabaseError("Metabase rejected METABASE_API_KEY (401).")
    if response.status_code == 403:
        raise MetabaseError("The Metabase API key lacks permission for this database (403).")
    if response.status_code >= 400:
        raise MetabaseError(f"Metabase returned HTTP {response.status_code}: {response.text[:300]}")

    try:
        body = response.json()
    except ValueError as exc:
        raise MetabaseError("Metabase returned a non-JSON response") from exc

    if body.get("status") == "failed":
        # Metabase echoes the driver error; it can contain the SQL but not credentials.
        raise MetabaseError(f"Query failed: {str(body.get('error') or body)[:400]}")

    data = body.get("data") or {}
    columns = [col.get("name") for col in data.get("cols") or []]
    return [dict(zip(columns, row)) for row in data.get("rows") or []]


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

def describe_schema(config: MetabaseConfig | None = None) -> dict[str, set[str]]:
    """Return {relation_name: {column, ...}} for the relations we touch."""
    relations = (LEADS_VIEW, INTERACTIONS_TABLE, CAMPAIGNS_TABLE)
    names = ", ".join(f"'{name}'" for name in relations)
    rows = run_sql(
        "SELECT table_name, column_name FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name IN ({names})",
        config, timeout=60,
    )
    found: dict[str, set[str]] = {name: set() for name in relations}
    for row in rows:
        table = str(row.get("table_name") or "")
        column = str(row.get("column_name") or "")
        if table in found and column:
            found[table].add(column.lower())
    return found


def schema_report(config: MetabaseConfig | None = None) -> dict[str, Any]:
    """Operator-facing readiness check for the redial queries."""
    schema = describe_schema(config)
    leads = schema.get(LEADS_VIEW, set())
    interactions = schema.get(INTERACTIONS_TABLE, set())
    campaigns = schema.get(CAMPAIGNS_TABLE, set())

    missing_required = [c for c in REQUIRED_LEAD_COLUMNS if c not in leads]
    interaction_required = ("id", "lead_id", "campaign_id", "call_stage", "scheduled_time")
    missing_interaction = [c for c in interaction_required if c not in interactions]

    # The Formi lead UUID needed to commit a schedule is spelled `uuid` on the
    # live view and `lead_uuid` elsewhere; either is fine.
    uuid_column = next((c for c in LEAD_UUID_COLUMNS if c in leads), "")

    # Which relation actually supplies each optional feature. Reported so the
    # console can say "present, via leads_outlet_chola_v.uuid" instead of
    # claiming a column is both present and missing.
    sources: dict[str, str] = {}
    if uuid_column:
        sources["lead_uuid"] = f"{LEADS_VIEW}.{uuid_column}"
    if "contact_id" in leads:
        sources["contact_id"] = f"{LEADS_VIEW}.contact_id"
    elif "contact_id" in interactions:
        sources["contact_id"] = f"{INTERACTIONS_TABLE}.contact_id"
    for column in ("policy_no", "connected", "customer_name", "lead_name",
                   "campaign_id", "dynamic_variables"):
        if column in leads:
            sources[column] = f"{LEADS_VIEW}.{column}"

    # A feature is only "missing" when NOTHING provides it. `uuid` satisfies
    # lead_uuid, and interactions.contact_id satisfies contact_id, so neither is
    # reported as missing just because the view spells it differently.
    optional_features = ("lead_uuid", "contact_id", "policy_no", "connected",
                         "customer_name", "lead_name", "campaign_id", "dynamic_variables")
    missing_optional = [c for c in optional_features if c not in sources]

    warnings: list[str] = []
    if not uuid_column:
        warnings.append(
            f"public.{LEADS_VIEW} exposes neither lead_uuid nor uuid, so committed runs must "
            "resolve each lead's Formi UUID through the Formi API. Bucket previews and dry "
            "runs are unaffected."
        )
    if "contact_id" not in leads and "contact_id" not in interactions:
        warnings.append(
            "Neither the leads view nor public.interactions exposes contact_id, so the same "
            "customer appearing in two campaigns cannot be de-duplicated from the warehouse alone."
        )
    if "outlet_id" not in interactions:
        warnings.append("public.interactions has no outlet_id column; the Chola outlet filter "
                        "will be skipped and results may include other tenants.")

    return {
        "ok": not missing_required and not missing_interaction,
        "relations": {name: sorted(cols) for name, cols in schema.items()},
        "leads_view": LEADS_VIEW,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "optional_sources": sources,
        "missing_interaction_columns": missing_interaction,
        "has_campaign_status": bool(_status_column(campaigns)),
        "campaign_status_column": _status_column(campaigns),
        "has_lead_uuid": bool(uuid_column),
        "lead_uuid_column": uuid_column,
        # contact_id is usable from either relation; interactions is the fallback.
        "has_contact_id": "contact_id" in leads or "contact_id" in interactions,
        # Set when the warehouse can map agent -> campaigns and uuid <-> numeric id
        # itself, which is more reliable than matching campaigns by name.
        "has_campaign_agent_map": {"agent_id", "uuid", "id"} <= campaigns,
        "warnings": warnings,
    }


def _status_column(campaign_columns: Iterable[str]) -> str:
    """Pick whichever status-ish column this environment's campaigns table has."""
    columns = set(campaign_columns)
    for candidate in ("status", "campaign_status", "state", "is_active", "active"):
        if candidate in columns:
            return candidate
    return ""


def _safe_identifier(name: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise MetabaseError(f"Refusing to build SQL with the identifier {name!r}")
    return name


def _int_list(values: Iterable[Any], label: str) -> list[int]:
    """Coerce to ints so campaign IDs can never carry SQL text."""
    out: list[int] = []
    for value in values or ():
        try:
            out.append(int(str(value).strip()))
        except (TypeError, ValueError):
            raise MetabaseError(f"{label} must contain whole numbers; got {value!r}") from None
    if not out:
        raise MetabaseError(f"{label} cannot be empty")
    return out


# ---------------------------------------------------------------------------
# Campaign stats
# ---------------------------------------------------------------------------

def build_agent_campaigns_sql(
    config: MetabaseConfig,
    schema: dict[str, set[str]],
    agent_id: Any,
    today: date | None = None,
) -> str:
    """Campaigns belonging to one agent, straight from `public.campaigns`.

    This is strictly better than matching the Formi campaign list to warehouse
    rows by name: names are NOT unique (campaigns 1599 and 1602 are both called
    "10-08-Redial-CV" in production, on different agents), so a name join can
    attribute one agent's lead counts to another's campaign. Joining on the
    campaign `uuid` — which Formi also returns — is exact.
    """
    campaigns = schema.get(CAMPAIGNS_TABLE, set())
    required = {"id", "uuid", "agent_id"}
    if not required <= campaigns:
        raise MetabaseError(
            "public.campaigns needs id, uuid and agent_id to map an agent to its campaigns; "
            f"found {sorted(campaigns)}"
        )
    try:
        agent = int(str(agent_id).strip())
    except (TypeError, ValueError):
        raise MetabaseError("agent_id must be a whole number") from None

    leads = schema.get(LEADS_VIEW, set())
    interactions = schema.get(INTERACTIONS_TABLE, set())
    status_column = _status_column(campaigns)
    status_select = f"c.{_safe_identifier(status_column)}::text" if status_column else "NULL::text"
    name_select = "c.name" if "name" in campaigns else "NULL::text"
    outlet_filter = f"AND c.outlet_id = {config.outlet_id}" if "outlet_id" in campaigns else ""
    dial_stages = ", ".join(f"'{s}'" for s in REAL_DIAL_STAGES)
    connected = ", ".join(f"'{s}'" for s in CONNECTED_STAGES)
    connected_pred = _connected_predicate("a")
    today_sql = f"DATE '{(today or date.today()).isoformat()}'"
    red_expr = red_parse_expression("v.red") if "red" in leads else "NULL::date"
    interaction_outlet = f"AND i.outlet_id = {config.outlet_id}" if "outlet_id" in interactions else ""

    return f"""
-- Campaigns for agent {agent}, with status and RED readiness. One row per campaign.
WITH mine AS (
  SELECT c.id, c.uuid::text AS campaign_uuid, {name_select} AS campaign_name,
         {status_select} AS campaign_status
  FROM public.campaigns c
  WHERE c.agent_id = {agent}
    {outlet_filter}
),
activity AS (
  SELECT i.campaign_id, i.lead_id, i.call_stage, i.scheduled_time,
         {DISPOSITION_SQL} AS disposition,
         COALESCE((i.interaction_metadata->>'call_duration')::numeric, 0) AS duration_sec
  FROM public.interactions i
  JOIN mine m ON m.id = i.campaign_id
  WHERE TRUE
    {interaction_outlet}
),
per_lead AS (
  SELECT a.campaign_id, a.lead_id,
         MAX(CASE WHEN a.call_stage IN ({dial_stages}) THEN 1 ELSE 0 END) AS dialled,
         red.d AS red_date
  FROM activity a
  JOIN public.{LEADS_VIEW} v ON v.id = a.lead_id
  CROSS JOIN LATERAL (SELECT {red_expr} AS d) red
  GROUP BY a.campaign_id, a.lead_id, red.d
),
lead_totals AS (
  SELECT p.campaign_id,
         COUNT(*)                                                      AS leads,
         COUNT(*) FILTER (WHERE p.red_date IS NOT NULL)                AS leads_with_red,
         COUNT(*) FILTER (WHERE p.red_date - {today_sql} BETWEEN -3 AND 45)
                                                                       AS leads_in_red_window
  FROM per_lead p
  GROUP BY p.campaign_id
),
dial_stats AS (
  SELECT a.campaign_id,
         COUNT(*) FILTER (WHERE a.call_stage IN ({dial_stages}))       AS dials,
         COUNT(*) FILTER (WHERE {connected_pred})                      AS connected_dials,
         COUNT(*) FILTER (WHERE COALESCE(a.call_stage, '') = ''
                            AND (a.scheduled_time AT TIME ZONE 'UTC'
                                 AT TIME ZONE 'Asia/Kolkata')::date = {today_sql})
                                                                       AS queued_today,
         MAX(a.scheduled_time) FILTER (WHERE a.call_stage IN ({dial_stages}))
                                                                       AS last_dial_utc
  FROM activity a
  GROUP BY a.campaign_id
)
SELECT
  m.id                                    AS campaign_id,
  m.campaign_uuid                         AS campaign_uuid,
  m.campaign_name                         AS campaign_name,
  m.campaign_status                       AS campaign_status,
  {agent}                                 AS agent_id,
  COALESCE(t.leads, 0)                    AS leads,
  COALESCE(t.leads_with_red, 0)           AS leads_with_red,
  COALESCE(t.leads_in_red_window, 0)      AS leads_in_red_window,
  COALESCE(s.dials, 0)                    AS dials,
  COALESCE(s.connected_dials, 0)          AS connected_dials,
  COALESCE(s.queued_today, 0)             AS queued_today,
  (s.last_dial_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata') AS last_dial_at
FROM mine m
LEFT JOIN lead_totals t ON t.campaign_id = m.id
LEFT JOIN dial_stats  s ON s.campaign_id = m.id
ORDER BY leads_in_red_window DESC, m.id
""".strip()


def fetch_agent_campaigns(
    agent_id: Any,
    config: MetabaseConfig | None = None,
    schema: dict[str, set[str]] | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Warehouse-native agent -> campaign list, keyed by campaign uuid AND id."""
    config = config or load_config()
    schema = schema if schema is not None else describe_schema(config)
    return run_sql(build_agent_campaigns_sql(config, schema, agent_id, today), config)


def build_campaign_stats_sql(
    config: MetabaseConfig,
    schema: dict[str, set[str]],
    campaign_ids: Sequence[Any] | None = None,
    today: date | None = None,
) -> str:
    """Build the per-campaign readiness statement.

    Leads are counted from the leads view when it carries `campaign_id` (so a
    freshly uploaded campaign with no dials still reports its size) and from
    `interactions` otherwise.
    """
    leads = schema.get(LEADS_VIEW, set())
    interactions = schema.get(INTERACTIONS_TABLE, set())
    campaigns = schema.get(CAMPAIGNS_TABLE, set())

    filter_ids = ""
    if campaign_ids:
        ids = ",".join(str(v) for v in _int_list(campaign_ids, "campaign_ids"))
        filter_ids = f"AND i.campaign_id IN ({ids})"

    outlet_filter = f"AND i.outlet_id = {config.outlet_id}" if "outlet_id" in interactions else ""
    dial_stages = ", ".join(f"'{s}'" for s in REAL_DIAL_STAGES)
    connected = ", ".join(f"'{s}'" for s in CONNECTED_STAGES)
    connected_pred = _connected_predicate("a")
    today_sql = f"DATE '{(today or date.today()).isoformat()}'"
    red_expr = red_parse_expression("v.red") if "red" in leads else "NULL::date"

    status_column = _status_column(campaigns)
    status_select = (f"c.{_safe_identifier(status_column)}::text"
                     if status_column else "NULL::text")
    name_select = "c.name" if "name" in campaigns else "NULL::text"
    # Only join public.campaigns when it exists and gives us something to show.
    if campaigns and ("name" in campaigns or status_column):
        campaign_join = "LEFT JOIN public.campaigns c ON c.id = a.campaign_id"
    else:
        campaign_join = ""
        name_select = "NULL::text"
        status_select = "NULL::text"

    # Lead totals: prefer the view's own campaign_id so campaigns awaiting
    # their first dial are not reported as empty.
    if "campaign_id" in leads:
        lead_totals = f"""
lead_totals AS (
  SELECT
    v.campaign_id                                                        AS campaign_id,
    COUNT(*)                                                             AS leads,
    COUNT(*) FILTER (WHERE red.d IS NOT NULL)                            AS leads_with_red,
    COUNT(*) FILTER (WHERE red.d - {today_sql} BETWEEN -3 AND 45)        AS leads_in_red_window
  FROM public.{LEADS_VIEW} v
  CROSS JOIN LATERAL (SELECT {red_expr} AS d) red
  GROUP BY v.campaign_id
)"""
    else:
        lead_totals = f"""
lead_totals AS (
  SELECT
    a.campaign_id                                                        AS campaign_id,
    COUNT(DISTINCT a.lead_id)                                            AS leads,
    COUNT(DISTINCT CASE WHEN red.d IS NOT NULL THEN a.lead_id END)       AS leads_with_red,
    COUNT(DISTINCT CASE WHEN red.d - {today_sql} BETWEEN -3 AND 45
                        THEN a.lead_id END)                              AS leads_in_red_window
  FROM activity a
  JOIN public.{LEADS_VIEW} v ON v.id = a.lead_id
  CROSS JOIN LATERAL (SELECT {red_expr} AS d) red
  GROUP BY a.campaign_id
)"""

    return f"""
-- Per-campaign redial readiness. One row per campaign_id.
WITH activity AS (
  SELECT i.campaign_id, i.lead_id, i.call_stage, i.scheduled_time,
         {DISPOSITION_SQL} AS disposition,
         COALESCE((i.interaction_metadata->>'call_duration')::numeric, 0) AS duration_sec
  FROM public.interactions i
  WHERE TRUE
    {outlet_filter}
    {filter_ids}
),
dial_stats AS (
  SELECT
    a.campaign_id,
    COUNT(*) FILTER (WHERE a.call_stage IN ({dial_stages}))              AS dials,
    COUNT(*) FILTER (WHERE {connected_pred})                             AS connected_dials,
    COUNT(*) FILTER (WHERE COALESCE(a.call_stage, '') = ''
                       AND (a.scheduled_time AT TIME ZONE 'UTC'
                            AT TIME ZONE 'Asia/Kolkata')::date = {today_sql})
                                                                         AS queued_today,
    MAX(a.scheduled_time) FILTER (WHERE a.call_stage IN ({dial_stages})) AS last_dial_utc
  FROM activity a
  GROUP BY a.campaign_id
),{lead_totals}
SELECT
  a.campaign_id                                    AS campaign_id,
  {name_select}                                    AS campaign_name,
  {status_select}                                  AS campaign_status,
  COALESCE(t.leads, 0)                             AS leads,
  COALESCE(t.leads_with_red, 0)                    AS leads_with_red,
  COALESCE(t.leads_in_red_window, 0)               AS leads_in_red_window,
  COALESCE(s.dials, 0)                             AS dials,
  COALESCE(s.connected_dials, 0)                   AS connected_dials,
  COALESCE(s.queued_today, 0)                      AS queued_today,
  (s.last_dial_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata') AS last_dial_at
FROM (SELECT DISTINCT campaign_id FROM activity
      UNION SELECT campaign_id FROM lead_totals) a
LEFT JOIN dial_stats  s ON s.campaign_id = a.campaign_id
LEFT JOIN lead_totals t ON t.campaign_id = a.campaign_id
{campaign_join}
WHERE a.campaign_id IS NOT NULL
ORDER BY leads_in_red_window DESC, a.campaign_id
""".strip()


def fetch_campaign_stats(
    campaign_ids: Sequence[Any] | None = None,
    config: MetabaseConfig | None = None,
    schema: dict[str, set[str]] | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Per-campaign lead counts, dial activity, and RED spread.

    Used to enrich the Formi campaign list the console already fetches: Formi
    supplies the authoritative agent -> campaign mapping and the campaign UUID
    needed to schedule, while this adds warehouse truth (how many leads, how
    many still inside the RED window, when the campaign was last dialled).

    `campaign_ids` of None means every campaign for the configured outlet.
    """
    config = config or load_config()
    schema = schema if schema is not None else describe_schema(config)
    return run_sql(build_campaign_stats_sql(config, schema, campaign_ids, today), config)


# ---------------------------------------------------------------------------
# Lead selection
# ---------------------------------------------------------------------------

def build_leads_sql(
    campaign_ids: Sequence[Any],
    config: MetabaseConfig,
    schema: dict[str, set[str]],
    today: date | None = None,
    dte_min: int = -3,
    dte_max: int = 45,
    stages: Sequence[str] | None = None,
    exclude_stages: Sequence[str] | None = None,
    require_red: bool = True,
    keep_today: bool = False,
    include_queued_today: bool = True,
    limit: int = 200_000,
    after_id: int = 0,
    red_on: str | None = None,
    red_from: str | None = None,
    red_to: str | None = None,
) -> str:
    """Build the single statement that feeds the engine.

    Everything expensive is done in the database: RED parsing, the dial
    histogram (total / today / last 7 days), the last-dial timestamp, and the
    already-queued-today flag. Python then only runs `red_engine.decide` per row.

    `after_id` drives keyset pagination -- see `fetch_redial_leads`. Ordering is
    by `v.id` (unique, indexed) rather than by RED so that paging is stable and
    cannot skip or repeat a lead.
    """
    leads = schema.get(LEADS_VIEW, set())
    interactions = schema.get(INTERACTIONS_TABLE, set())

    missing = [c for c in REQUIRED_LEAD_COLUMNS if c not in leads]
    if missing:
        raise MetabaseError(
            f"public.{LEADS_VIEW} is missing required column(s): {', '.join(missing)}")

    ids = ",".join(str(v) for v in _int_list(campaign_ids, "campaign_ids"))
    outlet_filter = f"AND i.outlet_id = {config.outlet_id}" if "outlet_id" in interactions else ""
    dial_stages = ", ".join(f"'{s}'" for s in REAL_DIAL_STAGES)
    today_value = (today or date.today()).isoformat()
    today_sql = f"DATE '{today_value}'"

    # Project the optional columns this environment actually has, applying the
    # alias map so the caller always sees `lead_uuid` regardless of whether the
    # view spells it `lead_uuid` or `uuid`.
    optional_select = "".join(
        f"    v.{_safe_identifier(column)} AS {_safe_identifier(output)},\n"
        for column, output in _optional_projection(leads)
    )

    stage_filter = ""
    if stages:
        allowed = ", ".join(f"'{_slug(s)}'" for s in stages)
        stage_filter = f"  AND LOWER(COALESCE(v.stage, '')) IN ({allowed})\n"
    if exclude_stages:
        blocked = ", ".join(f"'{_slug(s)}'" for s in exclude_stages)
        stage_filter += f"  AND LOWER(COALESCE(v.stage, '')) NOT IN ({blocked})\n"

    vote_first, vote_second = _order_votes_sql("v.red")
    # Learn each campaign's day/month convention from its OWN unambiguous rows.
    # Format is consistent within a campaign but differs BETWEEN campaigns
    # (verified: 0 of 42 campaigns are internally mixed, yet slash campaigns
    # split both ways), so this turns tens of thousands of coin-flips on values
    # like 8/9/2026 into evidence-based reads. NULL means the campaign had no
    # unambiguous row to learn from, and the global fallback applies.
    campaign_order_cte = f""",
lead_campaign AS (
  SELECT s.lead_id, MIN(s.campaign_id) AS campaign_id FROM scoped s GROUP BY s.lead_id
),
order_votes AS MATERIALIZED (
  -- MATERIALIZED is load-bearing, not decoration. `campaign_order` is read once,
  -- from inside the LATERAL that parses every lead's RED, so Postgres 12+ inlines
  -- it by default and re-runs this whole aggregate PER LEAD. Campaign 1650 (4,162
  -- leads) took >60s and came back as a Metabase 504; materialised it is 0.9s.
  -- Votes are counted per LEAD, not per interaction row: a busy campaign has
  -- orders of magnitude more interactions than leads, and voting off the raw
  -- interaction rows is slow enough to hit the Metabase gateway timeout.
  SELECT lc.campaign_id,
         COUNT(*) FILTER (WHERE {vote_first} > 12 AND {vote_second} <= 12) AS day_first_votes,
         COUNT(*) FILTER (WHERE {vote_second} > 12 AND {vote_first} <= 12) AS month_first_votes
  FROM lead_campaign lc
  JOIN public.{LEADS_VIEW} v ON v.id = lc.lead_id
  GROUP BY lc.campaign_id
),
campaign_order AS MATERIALIZED (
  SELECT campaign_id,
         CASE WHEN month_first_votes > day_first_votes THEN TRUE
              WHEN day_first_votes > month_first_votes THEN FALSE
              ELSE NULL END AS month_first,
         day_first_votes, month_first_votes
  FROM order_votes
)"""
    red_expr = red_parse_expression("v.red", month_first_expr="co.month_first",
                                    today=today or date.today())

    # A specific RED date (or RED date range) is a more direct way to express
    # "dial only the 31 Aug expiries" than converting to a dte window by hand,
    # and it stays correct tomorrow when today's dte for that date has changed.
    # When given it REPLACES the dte window, since both filter the same column.
    #
    # Blank/whitespace means "no filter", matching how the API layer normalises
    # an empty text input, so the two layers cannot disagree.
    red_on = _blank_to_none(red_on)
    red_from = _blank_to_none(red_from)
    red_to = _blank_to_none(red_to)
    red_cond = ""
    if red_on:
        red_cond = f"red.d = DATE '{_iso_date(red_on, 'red_on')}'"
    elif red_from or red_to:
        clauses = []
        if red_from:
            clauses.append(f"red.d >= DATE '{_iso_date(red_from, 'red_from')}'")
        if red_to:
            clauses.append(f"red.d <= DATE '{_iso_date(red_to, 'red_to')}'")
        if red_from and red_to and _iso_date(red_from, "red_from") > _iso_date(red_to, "red_to"):
            raise MetabaseError("red_from cannot be later than red_to")
        red_cond = " AND ".join(clauses)
    elif require_red:
        red_cond = f"(red.d - {today_sql}) BETWEEN {int(dte_min)} AND {int(dte_max)}"

    # A lead the main system already has on today's clock has to come back even
    # when its RED puts it outside the window, or the local store loses the one
    # fact that stops the console booking a second call on top of it.
    if red_cond and keep_today:
        red_cond = (f"({red_cond}\n       OR COALESCE(h.queued_today, 0) > 0"
                    f"\n       OR COALESCE(h.calls_today, 0) > 0)")
    red_filter = f"  AND {red_cond}\n" if red_cond else ""
    # NOTE: this aggregate lives inside the `history` CTE, whose FROM is
    # `scoped s` — it must reference `s`, not `i`.
    queued_select = (
        f"    COUNT(*) FILTER (WHERE COALESCE(s.call_stage, '') = ''\n"
        f"                       AND (s.scheduled_time AT TIME ZONE 'UTC'\n"
        f"                            AT TIME ZONE 'Asia/Kolkata')::date = {today_sql})"
        if include_queued_today else "    0"
    )

    return f"""
-- RED redial candidate leads for campaigns ({ids}) as of {today_value}.
-- One row per lead. Call history is aggregated in the warehouse so the
-- scheduler never paginates the Formi lead API.
WITH scoped AS (
  SELECT
    i.lead_id,
    i.campaign_id,
    i.call_stage,
    i.scheduled_time
  FROM public.interactions i
  WHERE i.campaign_id IN ({ids})
    {outlet_filter}
),
history AS (
  SELECT
    s.lead_id,
    MIN(s.campaign_id)                                     AS campaign_id,
    COUNT(*) FILTER (WHERE s.call_stage IN ({dial_stages})) AS total_interactions,
    MAX(s.scheduled_time) FILTER (WHERE s.call_stage IN ({dial_stages}))
                                                            AS last_interaction_utc,
    COUNT(*) FILTER (WHERE s.call_stage IN ({dial_stages})
                       AND (s.scheduled_time AT TIME ZONE 'UTC'
                            AT TIME ZONE 'Asia/Kolkata')::date = {today_sql})
                                                            AS calls_today,
    COUNT(*) FILTER (WHERE s.call_stage IN ({dial_stages})
                       AND (s.scheduled_time AT TIME ZONE 'UTC'
                            AT TIME ZONE 'Asia/Kolkata')::date
                           > {today_sql} - INTERVAL '7 days')
                                                            AS calls_last_7d,
{queued_select}                                             AS queued_today
  FROM scoped s
  GROUP BY s.lead_id
){campaign_order_cte}
SELECT
    v.id                                        AS warehouse_lead_id,
{optional_select}    h.campaign_id                               AS campaign_id,
    LOWER(COALESCE(v.stage, ''))                AS stage,
    v.red                                       AS red_raw,
    red.d                                       AS red,
    (red.d - {today_sql})                       AS dte,
    COALESCE(h.total_interactions, 0)           AS total_interactions,
    COALESCE(h.calls_today, 0)                  AS calls_today,
    COALESCE(h.calls_last_7d, 0)                AS calls_last_7d,
    COALESCE(h.queued_today, 0)                 AS queued_today,
    -- The convention this campaign was PROVEN to use, so Python resolves any
    -- re-parse the same way the warehouse did. NULL = nothing to learn from.
    co.month_first                              AS campaign_month_first,
    (h.last_interaction_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')
                                                AS last_interaction_time
FROM public.{LEADS_VIEW} v
JOIN history h ON h.lead_id = v.id
LEFT JOIN campaign_order co ON co.campaign_id = h.campaign_id
CROSS JOIN LATERAL (SELECT {red_expr} AS d) red
WHERE v.id > {int(after_id)}
{stage_filter}{red_filter}ORDER BY v.id ASC
LIMIT {int(limit)}
""".strip()


def _slug(value: Any) -> str:
    """Normalise a disposition slug and reject anything that is not one."""
    text = re.sub(r"[^a-z0-9_]", "", str(value or "").strip().lower())
    if not text:
        raise MetabaseError(f"{value!r} is not a usable disposition slug")
    return text


def _blank_to_none(value: Any) -> Any:
    """Empty or whitespace-only text means "filter not set"."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _iso_date(value: Any, label: str) -> str:
    """Validate a YYYY-MM-DD date and re-emit it canonically.

    Round-tripping through `date.fromisoformat` means only a real calendar date
    can ever reach the SQL, so a RED filter cannot carry an injection payload.
    """
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except (TypeError, ValueError):
        raise MetabaseError(f"{label} must be a date in YYYY-MM-DD form; got {value!r}") from None


def fetch_redial_leads(
    campaign_ids: Sequence[Any],
    config: MetabaseConfig | None = None,
    schema: dict[str, set[str]] | None = None,
    limit: int = 200_000,
    page_size: int | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Run the candidate-lead query and normalise rows for `red_engine.decide`.

    Metabase's `/api/dataset` endpoint caps a native query at ~2,000 rows
    regardless of the SQL `LIMIT` (the same cap the report SQL documents as
    "the UI preview caps at 2,000 rows"). A single statement therefore SILENTLY
    truncates any campaign larger than that -- campaign 1650 alone has >4,000
    leads inside the RED window, so a naive fetch would score barely half of it
    and report a full result.

    This pages with a keyset cursor on `v.id` until a short page arrives, so the
    caller gets every lead. `truncated` is only ever the caller's own `limit`.
    """
    # Validate before any network I/O so a bad argument is a clear error rather
    # than a confusing transport failure. `is None` matters: page_size=0 must be
    # rejected, not silently treated as "use the default".
    page_size = ROW_CAP if page_size is None else int(page_size)
    if page_size < 1:
        raise MetabaseError("page_size must be at least 1")
    if int(limit) < 1:
        raise MetabaseError("limit must be at least 1")

    config = config or load_config()
    schema = schema if schema is not None else describe_schema(config)

    rows: list[dict[str, Any]] = []
    # Dedupe by lead id. Pagination must never hand the same lead back twice: a
    # duplicate row here becomes a duplicate call to a customer.
    seen_ids: set[Any] = set()
    after_id = int(kwargs.pop("after_id", 0) or 0)
    pages = 0
    while len(rows) < limit:
        want = min(page_size, limit - len(rows))
        sql = build_leads_sql(campaign_ids, config, schema, limit=want,
                              after_id=after_id, **kwargs)
        page = run_sql(sql, config)
        pages += 1

        for row in page:
            lead_id = row.get("warehouse_lead_id")
            if lead_id is not None:
                if lead_id in seen_ids:
                    continue
                seen_ids.add(lead_id)
            rows.append(row)
            if len(rows) >= limit:
                break

        if len(page) < want:
            break                      # short page => that was the last one
        last_id = page[-1].get("warehouse_lead_id")
        if last_id is None:
            break                      # no cursor column, cannot page safely
        next_after = int(last_id)
        if next_after <= after_id:
            break                      # cursor stalled; stop rather than loop
        after_id = next_after
        if pages >= MAX_PAGES:
            raise MetabaseError(
                f"Lead pagination exceeded {MAX_PAGES} pages ({len(rows):,} rows). "
                "Narrow the campaign selection or the RED window."
            )

    for row in rows:
        # decide() reads `red`; keep the raw text for the audit trail.
        if row.get("red") is None and row.get("red_raw") is not None:
            row["red"] = row["red_raw"]
        # Formi's schedule POST needs lead_uuid; surface its absence explicitly
        # rather than letting a None sneak into a URL.
        row.setdefault("lead_uuid", None)
    return rows
