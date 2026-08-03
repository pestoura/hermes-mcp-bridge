from __future__ import annotations

import atexit
import os
import tempfile

if os.environ.get("HERMES_API_KEY") is None:
    os.environ["HERMES_API_KEY"] = "test"

_tmpdir = tempfile.mkdtemp(prefix="hermes-mcp-bridge-tests-")
os.environ["BRIDGE_STATE_DB_PATH"] = os.path.join(_tmpdir, "state.sqlite3")


@atexit.register
def _cleanup() -> None:
    import shutil

    shutil.rmtree(_tmpdir, ignore_errors=True)
