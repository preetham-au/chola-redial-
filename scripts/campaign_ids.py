"""Every campaign the warehouse has for an agent, and which of them this box is missing.

A read-only diff. Written because "Sync now" was answering 504 and nobody could
say what had gone missing: the console shows what it HAS, and the whole question
was what it does not.

The `view_leads` column is the point. `build_agent_campaigns_sql` counts a
campaign's leads through `public.interactions`, so a campaign that has been
uploaded but not yet dialled reports zero — and `sync` drops anything reporting
zero. This asks the leads view directly, so the two numbers can be compared.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api.main  # noqa: F401  -- loads .env exactly as the service does
from engine.metabase_source import (LEADS_VIEW, describe_schema, fetch_agent_campaigns,
                                    load_config, run_sql)

AGENTS = (125, 127)


def main() -> None:
    config = load_config()
    schema = describe_schema(config)
    db = sqlite3.connect(str(ROOT / "redial.db"))
    db.row_factory = sqlite3.Row
    local = {r["id"] for r in db.execute("SELECT id FROM campaigns")}

    # What `public.leads` says, independent of whether anything was dialled.
    # The leads VIEW carries no campaign_id -- `public.leads` is the only place
    # the mapping exists, which is why fetch_fresh_leads joins through it.
    view = {int(r["campaign_id"]): int(r["n"]) for r in run_sql(
        f"SELECT l.campaign_id, COUNT(*) AS n FROM public.leads l "
        f"JOIN public.{LEADS_VIEW} v ON v.id = l.id "
        "WHERE l.campaign_id IS NOT NULL GROUP BY l.campaign_id", config)}

    for agent in AGENTS:
        rows = fetch_agent_campaigns(agent, config, schema)
        ids = sorted(int(r["campaign_id"]) for r in rows)
        missing = [i for i in ids if i not in local]
        print(f"\n=== agent {agent} ===")
        print(f"warehouse {len(ids)} | in db {len(ids) - len(missing)} | MISSING {len(missing)}")
        print(f"missing: {missing}")
        print(f"{'id':>6}  {'name':<40} {'status':<8} {'q_leads':>8} {'view':>7} {'window':>7}")
        for r in sorted(rows, key=lambda r: -int(r["campaign_id"])):
            cid = int(r["campaign_id"])
            if cid not in missing:
                continue
            print(f"{cid:>6}  {str(r['campaign_name'])[:40]:<40} "
                  f"{str(r['campaign_status'])[:8]:<8} {r['leads']:>8} "
                  f"{view.get(cid, 0):>7} {r['leads_in_red_window']:>7}")

        stranded = [int(r["campaign_id"]) for r in rows
                    if not r["leads"] and view.get(int(r["campaign_id"]), 0) > 0]
        if stranded:
            print(f"NEVER-DIALLED but hold leads (query says 0, view disagrees): {sorted(stranded)}")


if __name__ == "__main__":
    main()
