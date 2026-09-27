"""Keep the suite off the checkout's live ``data/`` and ``config.json``.

services.config resolves DATA_DIR and CONFIG_FILE at import time, and several
module-level singletons (log_service, image storage, the account pool) open
files under them straight away. Without this every run wrote fixture images,
call-log rows and the cumulative account counter into the real data/.

Assigned rather than setdefault: a shell that exports CHATGPT2API_DATA_DIR for
a second instance must not become the suite's scratch space.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_ROOT = tempfile.mkdtemp(prefix="chatgpt2api-test-")
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)

os.environ["CHATGPT2API_DATA_DIR"] = os.path.join(_ROOT, "data")
os.environ["CHATGPT2API_CONFIG_FILE"] = os.path.join(_ROOT, "config.json")
# Same value as the CI job; the API tests send it as their bearer token.
os.environ["CHATGPT2API_AUTH_KEY"] = "chatgpt2api"
