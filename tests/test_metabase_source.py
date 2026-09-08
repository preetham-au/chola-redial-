"""The disposition column the connected-dials counter is matched against.

CONTACTED_DISPOSITIONS and MACHINE_DISPOSITIONS are spelled as subs, so the
column has to be one. Both builders used `LOWER(lead_stage_computed)`, which is
a sub in neither era -- the coarse group after the 31 Aug 2026 cutover, the sub
under a `sub_` prefix before it -- so the label arm of `_connected_predicate`
never fired and a voicemail greeting could pass as a conversation.
"""
from datetime import date, datetime, timezone

import pytest

from engine.metabase_source import (
    CAMPAIGNS_TABLE,
    CONTACTED_DISPOSITIONS,
    INTERACTIONS_TABLE,
    LEADS_VIEW,
    MACHINE_DISPOSITIONS,
    MetabaseConfig,
    build_agent_campaigns_sql,
    build_campaign_stats_sql,
)

CONFIG = MetabaseConfig(url="https://mb.example", api_key="x" * 12, database_id=1,
                        outlet_id=1497)
SCHEMA = {
    CAMPAIGNS_TABLE: {"id", "uuid", "agent_id", "name", "status"},
    INTERACTIONS_TABLE: {"id", "campaign_id", "lead_id", "call_stage", "outlet_id",
                         "scheduled_time", "lead_stage_computed",
                         "lead_stage_reasoning", "interaction_metadata"},
    LEADS_VIEW: {"id", "red", "stage", "campaign_id"},
}
TODAY = date(2026, 9, 5)


@pytest.mark.parametrize("sql", [
    build_agent_campaigns_sql(CONFIG, SCHEMA, 125, today=TODAY),
    build_campaign_stats_sql(CONFIG, SCHEMA, [1734], today=TODAY),
], ids=["agent_campaigns", "campaign_stats"])
def test_disposition_is_the_sub_in_both_eras(sql):
    # Post-cutover the sub lives in the reasoning; on 3 Sep 2026 that is the
    # difference between reading `contacted` (in neither tuple, so the label arm
    # is dead) and reading the outcome, for 3,179 of 7,268 dials.
    assert "i.lead_stage_reasoning, 'sub=([A-Za-z0-9_]+)'" in sql
    # Pre-cutover it is in `computed` under a prefix no tuple entry carries.
    assert "'^sub_'" in sql
    # And the old expression is gone, not merely joined by the new one.
    assert "LOWER(COALESCE(i.lead_stage_computed, '')) AS disposition" not in sql


def test_the_two_tuples_are_disjoint():
    """A label cannot mean both `a human spoke` and `a machine answered`."""
    both = set(CONTACTED_DISPOSITIONS) & set(MACHINE_DISPOSITIONS)
    assert not both, both


def test_an_omitted_today_means_today_in_ist_not_on_the_host(monkeypatch):
    """`today=None` must resolve to the IST calendar day on a UTC box.

    The VM runs UTC. Every RED window in this module is `red - today`, so while
    the default was `date.today()` the five hourly syncs between 18:30 and 24:00
    UTC computed dte one day out and cut the window at the wrong lead. Pinned to
    a real instant rather than asserting against a live clock, so the test says
    the same thing at 09:00 as it does at 01:00.
    """
    import engine.metabase_source as ms

    # 2026-09-08 19:35 UTC is 01:05 IST on the 9th -- the hour the VM's suite
    # actually failed at. Both clocks are pinned, so a `date.today()` that has
    # crept back in reads the 8th here however the host is configured; without
    # pinning BOTH, this test passes on an IST laptop against the very bug it
    # is meant to catch.
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, 19, 35, tzinfo=tz or timezone.utc)

    class _HostDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 8)

    monkeypatch.setattr(ms, "datetime", _Clock)
    monkeypatch.setattr(ms, "date", _HostDate)
    assert ms.ist_today() == date(2026, 9, 9)
    sql = build_agent_campaigns_sql(CONFIG, SCHEMA, 125)
    assert "DATE '2026-09-09'" in sql
    assert "DATE '2026-09-08'" not in sql
