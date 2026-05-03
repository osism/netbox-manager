# SPDX-License-Identifier: Apache-2.0

# Set dynaconf env vars before any test imports `netbox_manager.main`, since
# `Dynaconf(...)` and the validator registration run at import time. Defaults
# are used so tests do not depend on a real `settings.toml` being present.
import os

os.environ.setdefault("NETBOX_MANAGER_URL", "http://localhost:8000")
os.environ.setdefault("NETBOX_MANAGER_TOKEN", "test-token")
os.environ.setdefault("NETBOX_MANAGER_IGNORE_SSL_ERRORS", "false")

# Per-module fixtures (mock pynetbox device / interface / IP / VRF / cluster /
# config-context / prefix shapes; mocked `ansible_runner.run`; `git.Repo`;
# `subprocess.run` / `check_call`) are added by the per-module sub-issues
# tracked in #232.
