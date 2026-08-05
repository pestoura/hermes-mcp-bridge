from __future__ import annotations

import atexit
import os
import tempfile

if os.environ.get("HERMES_API_KEY") is None:
    os.environ["HERMES_API_KEY"] = "test"

# The suite runs in an explicitly declared test posture: unsigned manifests are
# allowed here (and always reported as such), while production/security_required
# stay fail-closed. Individual tests override this to exercise strict mode.
os.environ.setdefault("BRIDGE_SECURITY_MODE", "test")

_tmpdir = tempfile.mkdtemp(prefix="hermes-mcp-bridge-tests-")
os.environ["BRIDGE_STATE_DB_PATH"] = os.path.join(_tmpdir, "state.sqlite3")


@atexit.register
def _cleanup() -> None:
    import shutil

    shutil.rmtree(_tmpdir, ignore_errors=True)
