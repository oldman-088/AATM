"""Enterprise platform API, split by domain (Phase 8).

This module only aggregates the domain routers so ``app.main`` keeps including
``platform.router`` / ``platform.public_router``.
"""

from fastapi import APIRouter

from app.api.routes import (
    platform_accounts,
    platform_browsers,
    platform_employees,
    platform_github,
    platform_ops,
    platform_workspaces,
)
from app.github_client.client import github_client  # noqa: F401 - kept for callers/tests using platform.github_client

router = APIRouter()
router.include_router(platform_github.router)
router.include_router(platform_employees.router)
router.include_router(platform_accounts.router)
router.include_router(platform_browsers.router)
router.include_router(platform_workspaces.router)
router.include_router(platform_ops.router)
public_router = platform_github.public_router
