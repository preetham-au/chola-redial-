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
           "preview_expired", "preview_red", "apply_red", "apply_red_overrides",
           "apply_stage", "batch_counts", "bulk_update"]

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


def _by_policy(conn: sqlite3.Connection, policies: Sequence[str],
               campaign_ids: Sequence[int] | None) -> tuple[list[dict[str, Any]], list[str], list[int]]:
    """(leads carrying those policies, policies with none, the scope applied).

    `campaign_ids` narrows "every lead carrying it". A policy is loaded into
    several campaigns and the default -- all of them -- is the behaviour ported
    from `mark_stage_by_policy.py`: renewing a policy retires it everywhere. Pass
    a list to touch the campaigns named and nowhere else, which is what an
    operator wants when only one campaign's copy is wrong.

    A policy that exists but not in the chosen campaigns comes back as missing,
    because within the scope asked for, it was not found.
    """
    policies = [p for p in dict.fromkeys(policies) if p]
    scope = [int(c) for c in (campaign_ids or [])]
    where = f" AND l.campaign_id IN ({','.join('?' * len(scope))})" if scope else ""
    rows: list[dict[str, Any]] = []
    for start in range(0, len(policies), 400):     # BATCH, as in the original
        chunk = policies[start:start + 400]
        marks = ",".join("?" * len(chunk))
        rows.extend(dict(r) for r in conn.execute(
            f"SELECT l.id, l.policy_no, l.lead_name, l.campaign_id, l.stage, l.red, c.agent_id "
            f"FROM leads l JOIN campaigns c ON c.id = l.campaign_id "
            f"WHERE l.policy_no IN ({marks}){where} ORDER BY l.id",
            [*chunk, *scope]).fetchall())
    found = {r["policy_no"] for r in rows}
    return rows, [p for p in policies if p not in found], scope


def preview_policies(conn: sqlite3.Connection, policies: Sequence[str], target_stage: str,
                     keep: Iterable[str] | None = None,
                     campaign_ids: Sequence[int] | None = None) -> dict[str, Any]:
    """policy_no -> every lead carrying it, with its agent and current stage."""
    keep_set = {s.lower() for s in (keep if keep is not None else [target_stage])}
    rows, missing, scope = _by_policy(conn, policies, campaign_ids)
    out = _result(rows, keep_set, target_stage, missing)
    out["campaign_ids"] = scope
    return out


def preview_red(conn: sqlite3.Connection, policies: Sequence[str], red: str,
                campaign_ids: Sequence[int] | None = None) -> dict[str, Any]:
    """policy_no -> the leads carrying it and the renewal expiry date they have now.

    The new date is parsed with the same function the scheduler uses and stored
    normalised. A RED the engine cannot read is a lead it can never schedule, so
    an unreadable date is refused here rather than written and discovered later.

    Leads already carrying this date count as unchanged, so a re-run writes
    nothing -- the same shape as the stage sweeps.
    """
    parsed = parse_red(red)
    if parsed is None:
        raise ValueError(f"cannot read {red!r} as a renewal expiry date")
    target = parsed.isoformat()
    rows, missing, scope = _by_policy(conn, policies, campaign_ids)
    changed = [r for r in rows if parse_red(r.get("red")) != parsed]
    return {
        "would_change": len(changed),
        "unchanged": len(rows) - len(changed),
        # Keyed like the stage sweeps so the same breakdown renders: what the
        # leads are moving FROM, which here is the date they carry today.
        "by_stage": dict(Counter((r["red"] or "(blank)") for r in changed).most_common()),
        "sample": [{"lead_id": r["id"], "policy_no": r["policy_no"], "lead_name": r["lead_name"],
                    "campaign_id": r["campaign_id"], "stage": r["stage"] or "",
                    "red": r["red"], "new_red": target} for r in changed[:SAMPLE]],
        "not_found": missing,
        "lead_ids": [r["id"] for r in changed],
        "rows": changed,
        # `stage_jobs.target_stage` is the one "what did this job aim at" column
        # there is; for a RED job that is the date.
        "target_stage": target,
        "red": target,
        "campaign_ids": scope,
    }


def apply_red(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]], red: str,
              now_iso: str, note: str = "") -> int:
    """Write the corrected date and remember it, in one transaction.

    Remembering is not optional: `engine.sync` DELETEs and re-inserts every lead
    of a campaign, so a date written into `leads.red` alone survives exactly
    until the next "Sync now". `apply_red_overrides` puts it back afterwards.

    This is a LOCAL correction. Formi has no endpoint that writes a renewal
    expiry date -- the only lead writes it exposes are the stage bulk update and
    the schedule call -- so this changes which slot THIS console picks and does
    not change what the agent reads out on the call.
    """
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO lead_red_overrides (lead_id, red, was, note, created_at) "
        "VALUES (?,?,?,?,?)",
        [(r["id"], red, r.get("red"), note, now_iso) for r in rows])
    conn.executemany("UPDATE leads SET red=? WHERE id=?", [(red, r["id"]) for r in rows])
    conn.commit()
    return len(rows)


def apply_red_overrides(conn: sqlite3.Connection, campaign_id: int) -> int:
    """Re-apply this campaign's corrected dates after a sync has replaced its leads.

    An override whose lead now reports a date matching neither the correction nor
    the value it replaced is dropped: the warehouse has changed its mind since,
    and a one-off correction must not clobber a real update for ever.
    """
    conn.execute(
        "DELETE FROM lead_red_overrides WHERE lead_id IN ("
        "  SELECT o.lead_id FROM lead_red_overrides o JOIN leads l ON l.id = o.lead_id"
        "  WHERE l.campaign_id = ? AND IFNULL(l.red,'') NOT IN (IFNULL(o.was,''), o.red))",
        (campaign_id,))
    cur = conn.execute(
        "UPDATE leads SET red = (SELECT o.red FROM lead_red_overrides o WHERE o.lead_id = leads.id) "
        "WHERE campaign_id = ? AND id IN (SELECT lead_id FROM lead_red_overrides)", (campaign_id,))
    conn.commit()
    return cur.rowcount


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


def _why(response: Any) -> str:
    """Formi's own explanation for a rejection.

    Its "Invalid stage" message already spells out the agent's configured
    stages, which is the whole answer to "why did nothing move", so it is
    passed through rather than re-assembled.
    """
    try:
        return str(response.json()["message"])
    except Exception:
        return f"HTTP {response.status_code}"


def batch_counts(response: Any, size: int) -> tuple[int, int]:
    """(ok, failed) for one bulk-update batch, read from the body — not the status.

    Formi answers a *partially* applied batch with HTTP 200 and
    `payload.successful_updates` / `failed_updates`: a lead on another agent or
    outlet is skipped and merely listed under `errors`. Trusting the 200 counts
    those skips as applied, which is the same silent success the seed-source bug
    produced. A 200 whose body we cannot read is not evidence of anything, so it
    counts as failed.
    """
    if response.status_code != 200:
        return 0, size
    try:
        body = response.json().get("payload") or {}
        ok = int(body["successful_updates"])
    except Exception:
        return 0, size
    return ok, size - ok


def bulk_update(agent_id: int, lead_ids: Sequence[int], stage: str, reason: str,
                dry_run: bool = True) -> tuple[int, int]:
    """POST to Formi in chunks of 200. Returns (ok, failed).

    The DRY_RUN guard is the first statement and `requests` is imported after it,
    so a dry run cannot reach the network even if this is called by mistake.
    """
    if dry_run:
        raise RuntimeError("DRY_RUN is set — refusing to POST to Formi's bulk endpoint")

    import requests                                   # noqa: PLC0415 — see docstring
    from api.db import NO_TOKEN, formi_token          # noqa: PLC0415 — after the guard
    token = formi_token()
    if not token:
        raise RuntimeError(f"{NO_TOKEN}; cannot authenticate a live bulk update")

    ok = failed = 0
    for start in range(0, len(lead_ids), CHUNK):
        batch = list(lead_ids[start:start + CHUNK])
        response = requests.post(
            BULK_URL.format(agent_id=agent_id),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"lead_ids": batch, "stage": stage, "reason": reason},
            timeout=120,
        )
        # 400/404 are not partial failures, they are "this request will never
        # work": an unconfigured stage, a wrong agent. Every remaining chunk
        # would be rejected identically, so stop and say why. Formi puts the
        # agent's actual stage list in the body — the one thing an operator
        # staring at "0 applied" needs.
        if response.status_code in (400, 404):
            raise RuntimeError(f"Formi rejected the stage write: {_why(response)}")
        applied, missed = batch_counts(response, len(batch))
        ok += applied
        failed += missed
    return ok, failed
