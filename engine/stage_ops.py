"""Bulk lead-stage updates — vendored from scripts/mark_paid/.

Ported verbatim in behaviour from `mark_stage_by_policy.py` and
`mark_stage_by_red.py`:

  * a policy exists as several leads (one per campaign it was loaded into) and
    every one of them is marked;
  * `--red-before` is EXCLUSIVE, so the inclusive cutoff is the day before;
  * `DEFAULT_KEEP` outcomes contradict "expired" and are never overwritten;
  * writes go to Formi's bulk endpoint in chunks of 200, grouped by agent.

What is deliberately different: this module never POSTs while DRY_RUN is set.
`bulk_update` refuses before `requests` is even imported, so there is no code
path from a dry run to the network.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from .red_engine import parse_red

__all__ = ["DEFAULT_KEEP", "BULK_URL", "CHUNK", "read_policies", "preview_policies",
           "preview_expired", "apply_stage", "bulk_update"]

BULK_URL = "https://api.formi.co.in/v2/campaign/leads/{agent_id}/bulk-update-stage"
CHUNK = 200
# Outcomes that contradict "expired" — a renewed policy is not an expired one.
DEFAULT_KEEP = ("already_paid_to_chola", "renewed", "policy_expired")
SAMPLE = 25


def read_policies(text: str) -> list[str]:
    """One policy per line. Drops a header line and de-dupes, order preserved."""
    seen: dict[str, None] = {}
    for raw in (text or "").splitlines():
        policy = raw.strip().strip(",").strip('"')
        if not policy or policy.upper().replace(" ", "_") == "POLICY_NUMBER":
            continue
        seen.setdefault(policy, None)
    return list(seen)


def _result(rows: Sequence[dict[str, Any]], keep: set[str], target_stage: str,
            missing: Sequence[str] = ()) -> dict[str, Any]:
    marked = [r for r in rows if (r["stage"] or "") not in keep]
    sample = [{"lead_id": r["id"], "policy_no": r["policy_no"], "lead_name": r["lead_name"],
               "campaign_id": r["campaign_id"], "old_stage": r["stage"] or "",
               "new_stage": target_stage} for r in marked[:SAMPLE]]
    return {
        "would_change": len(marked),
        "unchanged": len(rows) - len(marked),
        "by_stage": dict(Counter((r["stage"] or "(blank)") for r in marked).most_common()),
        "sample": sample,
        "not_found": list(missing),
        "lead_ids": [r["id"] for r in marked],
        "target_stage": target_stage,
        "keep": sorted(keep),
    }


def preview_policies(conn: sqlite3.Connection, policies: Sequence[str],
                     target_stage: str, keep: Iterable[str] | None = None) -> dict[str, Any]:
    """policy_no -> every lead carrying it, with its agent and current stage."""
    keep_set = {s.lower() for s in (keep if keep is not None else [target_stage])}
    policies = [p for p in dict.fromkeys(policies) if p]
    rows: list[dict[str, Any]] = []
    for start in range(0, len(policies), 400):     # BATCH, as in the original
        chunk = policies[start:start + 400]
        marks = ",".join("?" * len(chunk))
        rows.extend(dict(r) for r in conn.execute(
            f"SELECT l.id, l.policy_no, l.lead_name, l.campaign_id, l.stage, c.agent_id "
            f"FROM leads l JOIN campaigns c ON c.id = l.campaign_id "
            f"WHERE l.policy_no IN ({marks}) ORDER BY l.id", chunk).fetchall())
    found = {r["policy_no"] for r in rows}
    return _result(rows, keep_set, target_stage, [p for p in policies if p not in found])


def preview_expired(conn: sqlite3.Connection, campaign_ids: Sequence[int], red_before: str,
                    target_stage: str = "policy_expired",
                    keep: Iterable[str] | None = None) -> dict[str, Any]:
    """Leads whose parsed RED is strictly before `red_before` (exclusive cutoff)."""
    cutoff = date.fromisoformat(str(red_before))
    red_to = cutoff - timedelta(days=1)            # red_to is inclusive
    keep_set = {s.lower() for s in (keep if keep is not None else DEFAULT_KEEP)}

    ids = [int(c) for c in campaign_ids]
    marks = ",".join("?" * len(ids)) or "NULL"
    rows = [dict(r) for r in conn.execute(
        f"SELECT l.id, l.policy_no, l.lead_name, l.campaign_id, l.stage, l.red, c.agent_id "
        f"FROM leads l JOIN campaigns c ON c.id = l.campaign_id "
        f"WHERE l.campaign_id IN ({marks}) ORDER BY l.id", ids).fetchall()]

    # RED is free text, so it is parsed with the same function the engine uses.
    # Unparseable RED is not "before the cutoff" — it is unknown, and unknown is
    # never a reason to expire someone.
    scoped = []
    for row in rows:
        parsed = parse_red(row.get("red"))
        if parsed is not None and parsed <= red_to:
            row["red_parsed"] = parsed.isoformat()
            scoped.append(row)
    out = _result(scoped, keep_set, target_stage)
    out["red_before"] = cutoff.isoformat()
    return out


def apply_stage(conn: sqlite3.Connection, lead_ids: Sequence[int], target_stage: str) -> int:
    """Write the new stage into the local dataset (LEADS_SOURCE=seed)."""
    if not lead_ids:
        return 0
    marks = ",".join("?" * len(lead_ids))
    cur = conn.execute(f"UPDATE leads SET stage=? WHERE id IN ({marks})",
                       [target_stage, *lead_ids])
    conn.commit()
    return cur.rowcount


def bulk_update(agent_id: int, lead_ids: Sequence[int], stage: str, reason: str,
                dry_run: bool = True) -> tuple[int, int]:
    """POST to Formi in chunks of 200. Returns (ok, failed).

    The DRY_RUN guard is the first statement and `requests` is imported after it,
    so a dry run cannot reach the network even if this is called by mistake.
    """
    if dry_run:
        raise RuntimeError("DRY_RUN is set — refusing to POST to Formi's bulk endpoint")

    import requests                                   # noqa: PLC0415 — see docstring
    from os import environ
    token = environ.get("FORMI_TOKEN")
    if not token:
        raise RuntimeError("FORMI_TOKEN is not set; cannot authenticate a live bulk update")

    ok = failed = 0
    for start in range(0, len(lead_ids), CHUNK):
        batch = list(lead_ids[start:start + CHUNK])
        response = requests.post(
            BULK_URL.format(agent_id=agent_id),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"lead_ids": batch, "stage": stage, "reason": reason},
            timeout=120,
        )
        if response.status_code == 200:
            ok += len(batch)
        else:
            failed += len(batch)
    return ok, failed
