#!/usr/bin/env python3
"""Resolve the managed Hermes interpreter against the REAL runtime roots.

The Phase 2 launcher must know the Hermes-owning interpreter *before* it
transitions any child into the disposable shadow HOME. Managed-install layout
discovery (``$HERMES_HOME/hermes-agent/venv/bin/python``) only works against the
real roots; running it under the shadow home silently degrades to a PATH
interpreter that cannot import the Hermes resolver.

On success the resolved invocation path is printed on stdout (the launcher keeps
it private and forwards it only as an explicit argument). On failure only a
stable reason code is printed on stderr; no filesystem path is ever emitted on
the failure path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hermes_mcp_bridge.v2.hermes_runtime import (  # noqa: E402
    HermesRuntimeError,
    resolve_hermes_python,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-bin", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--path-env", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        resolved = resolve_hermes_python(
            args.hermes_bin,
            home=args.home,
            hermes_home=args.hermes_home,
            path_env=args.path_env,
        )
    except HermesRuntimeError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    print(resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
