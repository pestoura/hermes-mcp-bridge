"""Resolve the Python interpreter that actually owns the installed Hermes CLI.

The Phase 2 acceptance launcher must introspect the exact Hermes runtime that
serves the connected API. Guessing ``$(dirname hermes)/python`` is unsafe:
console-script shims, symlinks and managed installer wrappers may live outside
the environment that owns ``hermes_cli``.

Resolution is deliberately bounded. We inspect the resolved console-script
shebang first, then known Hermes managed-install layouts derived from HOME and
HERMES_HOME, then adjacent/PATH candidates. A candidate is accepted only when
it can import the exact Hermes modules required by the connected shadow proof.
Candidate paths are never emitted by the public/sanitized launcher contract.

Virtual-environment interpreter paths are intentionally *not* dereferenced to
their underlying system Python. Invoking ``venv/bin/python`` (even when it is a
symlink) is what activates the virtual environment's prefix and site-packages;
resolving that symlink first would silently discard the Hermes environment.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


class HermesRuntimeError(RuntimeError):
    """Stable, secret-free Hermes runtime resolution failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _absolute_without_resolving(path: str | Path) -> Path:
    """Return an absolute invocation path while preserving symlink semantics."""
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _unique_existing_executables(values: Iterable[str | Path | None]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for raw in values:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        path = _absolute_without_resolving(text)
        try:
            # stat() intentionally follows the final symlink to prove that the
            # invocation path resolves to a regular executable. The path itself
            # is preserved so a virtualenv Python keeps its venv semantics.
            info = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _shebang_python(hermes_bin: Path, *, path_env: str) -> Path | None:
    try:
        first = hermes_bin.open("rb").readline(4096).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    try:
        parts = shlex.split(first[2:].strip())
    except ValueError:
        return None
    if not parts:
        return None

    command = parts[0]
    if Path(command).name == "env":
        tokens = parts[1:]
        if tokens[:1] == ["-S"]:
            tokens = tokens[1:]
        for token in tokens:
            if token.startswith("-"):
                continue
            if "python" not in Path(token).name.lower():
                return None
            hit = shutil.which(token, path=path_env)
            return _absolute_without_resolving(hit) if hit else None
        return None

    if "python" not in Path(command).name.lower():
        return None
    candidate = _absolute_without_resolving(command)
    try:
        candidate.stat()
    except OSError:
        return None
    return candidate


def _managed_install_candidates(
    *,
    executable: Path,
    original_executable: Path,
    home: Path,
    hermes_home: Path,
) -> list[Path]:
    """Return bounded interpreter candidates from supported Hermes layouts."""
    roots: list[Path] = [hermes_home, home]

    # The official installer uses ``$HERMES_HOME/hermes-agent`` for a normal
    # user install and may expose ``hermes`` through a wrapper/symlink elsewhere.
    # Existing Jarvas deployments also use ``venv`` rather than ``.venv``.
    relative = (
        Path("hermes-agent/venv/bin/python"),
        Path("hermes-agent/venv/bin/python3"),
        Path("hermes-agent/.venv/bin/python"),
        Path("hermes-agent/.venv/bin/python3"),
        Path("venv/bin/python"),
        Path("venv/bin/python3"),
        Path(".venv/bin/python"),
        Path(".venv/bin/python3"),
    )

    values: list[Path] = []
    for root in roots:
        values.extend(root / item for item in relative)

    # Bounded ancestry handles commands living inside the checkout/venv without
    # recursively scanning the filesystem.
    for command in (executable, original_executable):
        parent = command.parent
        for _ in range(5):
            values.extend(
                (
                    parent / "venv/bin/python",
                    parent / "venv/bin/python3",
                    parent / ".venv/bin/python",
                    parent / ".venv/bin/python3",
                )
            )
            if parent.parent == parent:
                break
            parent = parent.parent

    # FHS root installs are explicitly supported by the upstream installer.
    values.extend(
        (
            Path("/usr/local/lib/hermes-agent/venv/bin/python"),
            Path("/usr/local/lib/hermes-agent/venv/bin/python3"),
            Path("/usr/local/lib/hermes-agent/.venv/bin/python"),
            Path("/usr/local/lib/hermes-agent/.venv/bin/python3"),
        )
    )
    return values


def _supports_required_hermes_modules(candidate: Path, *, env: dict[str, str]) -> bool:
    code = (
        "from hermes_cli.tools_config import _get_platform_tools; "
        "import gateway.platforms.api_server; "
        "assert callable(_get_platform_tools)"
    )
    try:
        completed = subprocess.run(
            [str(candidate), "-c", code],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def resolve_hermes_python(
    hermes_bin: str | Path,
    *,
    home: str | Path,
    hermes_home: str | Path,
    path_env: str | None = None,
) -> Path:
    """Return the interpreter proven to own the installed Hermes runtime."""
    original = _absolute_without_resolving(hermes_bin)
    try:
        # Resolving the Hermes *console script* is safe and useful: it may be a
        # public shim/symlink pointing at the managed checkout. Python candidate
        # paths themselves are kept unresolved by the collector above.
        executable = original.resolve(strict=True)
        info = executable.stat()
    except OSError as exc:
        raise HermesRuntimeError("HERMES_RUNTIME_EXECUTABLE_INVALID") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
        raise HermesRuntimeError("HERMES_RUNTIME_EXECUTABLE_INVALID")

    home_path = Path(home).expanduser().resolve()
    hermes_home_path = Path(hermes_home).expanduser().resolve()
    path_value = path_env if path_env is not None else os.environ.get("PATH", "")
    shebang = _shebang_python(executable, path_env=path_value)
    sibling = executable.parent

    managed = _managed_install_candidates(
        executable=executable,
        original_executable=original,
        home=home_path,
        hermes_home=hermes_home_path,
    )
    candidates = _unique_existing_executables(
        [
            shebang,
            *managed,
            sibling / "python",
            sibling / "python3",
            shutil.which("python3", path=path_value),
            shutil.which("python", path=path_value),
            sys.executable,
        ]
    )
    probe_env = {
        "HOME": str(home_path),
        "HERMES_HOME": str(hermes_home_path),
        "PATH": path_value,
        "USER": os.environ.get("USER", "estourpm"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for candidate in candidates:
        if _supports_required_hermes_modules(candidate, env=probe_env):
            return candidate
    raise HermesRuntimeError("HERMES_RUNTIME_PYTHON_UNRESOLVED")
