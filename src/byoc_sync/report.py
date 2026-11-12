"""Human and machine readable output: the per-user summary, unified diffs and the JSON report.

Nothing here ever prints a password: the vault appears as a list of variable changes with masked values.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from .planner import Materialized, RunPlan
from .publish import PublishResult
from .sources import ScanResult


def summary(run_plan: RunPlan) -> str:
    rows = [("E-MAIL", "NAME", "ACTION", "DETAILS")]
    for user in sorted(run_plan.users, key=lambda u: (u.action, u.email)):
        rows.append((user.email, user.name or "-", user.action, "; ".join(user.reasons)))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = ["  ".join(cell.ljust(widths[i]) if i < 3 else cell for i, cell in enumerate(row)).rstrip() for row in rows]
    if len(rows) == 1:
        lines.append("(no BYOC requests and no existing BYOC entries)")
    for name, places in sorted(run_plan.orphans.items()):
        lines.append(f"orphan: entries for '{name}' exist without a users.yml owner: {', '.join(places)}")
    return "\n".join(lines)


def diff(result: Materialized, vault_file: str) -> str:
    out: list[str] = []
    for path, file_plan in sorted(result.files.items()):
        out += difflib.unified_diff(
            file_plan.before.splitlines(),
            file_plan.text.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    if result.vault.changes:
        out += [
            f"--- a/{vault_file} (ansible vault, decrypted view)",
            f"+++ b/{vault_file} (ansible vault, decrypted view)",
        ]
        out += [
            ("-" if c.action == "delete" else "+" if c.action == "create" else "~") + " " + c.describe()
            for c in result.vault.changes
        ]
    return "\n".join(out)


def publish_summary(results: list[PublishResult]) -> str:
    if not results:
        return "nothing to publish"
    return "\n".join(f"{r.outcome:<22} {r.branch:<28} {r.url or ''} {r.detail}".rstrip() for r in results)


def json_report(
    *, scan: ScanResult, run_plan: RunPlan, result: Materialized | None, published: list[PublishResult], dry_run: bool
) -> str:
    data: dict[str, Any] = {
        "dry_run": dry_run,
        "galaxy_version": scan.galaxy_version,
        "scan": {
            "complete": scan.complete,
            "errors": scan.errors,
            "unreadable": sorted(scan.unreadable),
            "users_with_requests": len(scan.records),
        },
        "users": [
            {"email": u.email, "galaxy_id": u.galaxy_id, "name": u.name, "action": u.action, "reasons": u.reasons}
            for u in run_plan.users
        ],
        "orphans": run_plan.orphans,
        "changes": {
            path: [{"object_id": c.object_id, "action": c.action, "fields": c.fields} for c in fp.changes]
            for path, fp in sorted((result.files if result else {}).items())
        },
        "vault": [c.describe() for c in (result.vault.changes if result else [])],
        "published": [
            {
                "email": r.email,
                "name": r.name,
                "branch": r.branch,
                "outcome": r.outcome,
                "url": r.url,
                "detail": r.detail,
            }
            for r in published
        ],
    }
    return json.dumps(data, indent=2, sort_keys=True)
