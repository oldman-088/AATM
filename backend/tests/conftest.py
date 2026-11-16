"""Pytest configuration for AATM backend tests."""

import os

# In-sandbox unit/E2E workspace execution tests run without a Docker daemon;
# security tests explicitly verify that `Settings().AATM_SANDBOX_MODE == "docker"`
# and that production mode refuses un-exempted `process` execution.
os.environ.setdefault("AATM_SANDBOX_MODE", "process")

# Phase 6 tests run the real bridge (repo-root package `bridge`) next to the backend.
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)
