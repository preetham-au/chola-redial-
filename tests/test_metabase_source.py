"""The disposition column the connected-dials counter is matched against.

CONTACTED_DISPOSITIONS and MACHINE_DISPOSITIONS are spelled as subs, so the
column has to be one. Both builders used `LOWER(lead_stage_computed)`, which is
a sub in neither era -- the coarse group after the 31 Aug 2026 cutover, the sub
under a `sub_` prefix before it -- so the label arm of `_connected_predicate`
never fired and a voicemail greeting could pass as a conversation.
"""
from datetime import date

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
