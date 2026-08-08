#!/usr/bin/env python3
"""Mint an exact-scope GitHub App installation token for V2 DIRECT acceptance.

No private key, JWT, installation token or secret path is printed. On success the
command prints only a sanitized JSON summary and atomically writes the token and
provider attestation to caller-selected files in a private directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hermes_mcp_bridge.v2.github_app_mint import (  # noqa: E402
    GitHubAppMintError,
    mint_installation_token,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--issuer",
        required=True,
        help="GitHub App Client ID or App ID (non-secret JWT issuer)",
    )
    parser.add_argument("--private-key", required=True, help="restricted 0600 GitHub App PEM")
    parser.add_argument("--repository", required=True, help="exact owner/repository scope")
    parser.add_argument("--token-out", required=True, help="restricted installation-token output")
    parser.add_argument(
        "--attestation-out",
        required=True,
        help="sanitized Phase 2 provider-attestation output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = mint_installation_token(
            issuer=args.issuer,
            private_key_path=args.private_key,
            repository=args.repository,
            token_output_path=args.token_out,
            attestation_output_path=args.attestation_out,
        )
    except GitHubAppMintError as exc:
        print(json.dumps({"status": "GITHUB_APP_INSTALLATION_TOKEN_BLOCKED", "reason": exc.code}))
        return 2

    print(json.dumps(result.summary(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
