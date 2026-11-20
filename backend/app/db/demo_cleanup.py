"""Remove the demo rows that `seed_initial_data` wrote when SEED_DEMO_DATA was on (E13).

Only rows that carry a known demo marker are touched; the admin user, agents, providers, project,
settings and the audit log are never deleted. Dry run by default:

    python -m app.db.demo_cleanup            # report what would be removed
    python -m app.db.demo_cleanup --apply    # remove it
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.seed import (
    DEMO_APPROVAL_TITLE,
    DEMO_GIT_BRANCH,
    DEMO_GREETING_PREFIX,
    DEMO_NOTIFICATION_TITLE,
    DEMO_PROFILE_IDS,
    DEMO_TASK_SUFFIX,
    DEMO_USERSCRIPT_ID,
)
from app.models import (
    ApprovalRequest,
    BrowserProfile,
    ChatMessage,
    GitWorkflowRecord,
    Notification,
    Task,
    Userscript,
)


async def _sweep(db: AsyncSession, model: Any, *conditions: Any, apply: bool) -> int:
    rows = (await db.execute(select(model).where(*conditions))).scalars().all()
    if apply:
        for row in rows:
            await db.delete(row)
    return len(rows)


async def remove_demo_data(db: AsyncSession, *, apply: bool = False) -> dict[str, int]:
    """Return how many demo rows of each kind exist; delete them too when `apply` is true."""
    counts = {
        "tasks": await _sweep(
            db, Task, Task.description.like(f"Division % {DEMO_TASK_SUFFIX}"), apply=apply
        ),
        "greeting": await _sweep(
            db,
            ChatMessage,
            ChatMessage.sender_id == "manager-0",
            ChatMessage.content.like(f"{DEMO_GREETING_PREFIX}%"),
            apply=apply,
        ),
        "approvals": await _sweep(db, ApprovalRequest, ApprovalRequest.title == DEMO_APPROVAL_TITLE, apply=apply),
        "notifications": await _sweep(
            db, Notification, Notification.title == DEMO_NOTIFICATION_TITLE, apply=apply
        ),
        "git_records": await _sweep(db, GitWorkflowRecord, GitWorkflowRecord.branch_name == DEMO_GIT_BRANCH, apply=apply),
        "userscripts": await _sweep(db, Userscript, Userscript.id == DEMO_USERSCRIPT_ID, apply=apply),
        "browser_profiles": await _sweep(db, BrowserProfile, BrowserProfile.id.in_(sorted(DEMO_PROFILE_IDS)), apply=apply),
    }
    if apply:
        await db.commit()
    return counts


async def _main(apply: bool) -> None:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        counts = await remove_demo_data(db, apply=apply)
    verb = "removed" if apply else "would remove (dry run, add --apply)"
    for kind, count in counts.items():
        print(f"{verb}: {kind} = {count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="delete the demo rows")
    args = parser.parse_args()
    asyncio.run(_main(args.apply))
