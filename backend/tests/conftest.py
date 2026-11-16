"""Pytest configuration for AATM backend tests."""

import os

# In-sandbox unit/E2E workspace execution tests run without a Docker daemon;
# security tests explicitly verify that `Settings().AATM_SANDBOX_MODE == "docker"`
# and that production mode refuses un-exempted `process` execution.
os.environ.setdefault("AATM_SANDBOX_MODE", "process")
