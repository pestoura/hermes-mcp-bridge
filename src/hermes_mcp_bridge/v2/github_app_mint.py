"""Secure GitHub App installation-token minting for V2 DIRECT reads.

The private key is consumed only on the runtime host. Neither the key, the App
JWT nor the installation token is printed, returned in diagnostics or retained
in evidence. The mint is intentionally repository- and permission-scoped to the
Phase 2 ``github.read`` contract.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .github_attestation import ATTESTATION_INPUT_SCHEMA, REQUIRED_PERMISSIONS
from .github_direct import GITHUB_ACCEPT, GITHUB_API_BASE_URL, GITHUB_API_VERSION

_USER_AGENT = "hermes-mcp-bridge-v2-github-app-mint"
_MIN_SECRET_LENGTH = 20
_MAX_SECRET_BYTES = 8192


class GitHubAppMintError(RuntimeError):
    """Fail-closed mint failure carrying only a stable, secret-free code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return f"GitHub App mint failed: {self.code}"


@dataclass(frozen=True, slots=True)
class GitHubAppMintResult:
    """Sanitized result. Secret values and paths are deliberately absent."""

    installation_id: int
    repository: str
    expires_at: str
    confirmed_at: str

    def summary(self) -> dict[str, Any]:
        return {
            "status": "GITHUB_APP_INSTALLATION_TOKEN_MINTED",
            "provider_type": "github_app",
            "installation_id": self.installation_id,
            "repository_scopes": [self.repository],
            "permissions": dict(REQUIRED_PERMISSIONS),
            "expires_at": self.expires_at,
            "confirmed_at": self.confirmed_at,
            "token_file_updated": True,
            "attestation_file_updated": True,
            "secret_values_stored_in_output": False,
            "secret_paths_stored_in_output": False,
        }


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical_segment(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64url(raw)


def _secure_private_key_fd(path: str | Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise GitHubAppMintError("PRIVATE_KEY_UNREADABLE") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise GitHubAppMintError("PRIVATE_KEY_NOT_REGULAR")
        if info.st_mode & 0o077:
            raise GitHubAppMintError("PRIVATE_KEY_PERMISSIONS_TOO_OPEN")
        if info.st_size <= 0 or info.st_size > _MAX_SECRET_BYTES:
            raise GitHubAppMintError("PRIVATE_KEY_SIZE_INVALID")
        return fd
    except Exception:
        os.close(fd)
        raise


def _sign_rs256(signing_input: bytes, private_key_path: str | Path) -> bytes:
    openssl = shutil.which("openssl")
    if not openssl:
        raise GitHubAppMintError("OPENSSL_UNAVAILABLE")

    fd = _secure_private_key_fd(private_key_path)
    try:
        proc_fd = f"/proc/self/fd/{fd}"
        try:
            completed = subprocess.run(
                [openssl, "dgst", "-sha256", "-sign", proc_fd],
                input=signing_input,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                pass_fds=(fd,),
            )
        except OSError as exc:
            raise GitHubAppMintError("JWT_SIGN_FAILED") from exc
    finally:
        os.close(fd)

    if completed.returncode != 0 or not completed.stdout:
        raise GitHubAppMintError("JWT_SIGN_FAILED")
    return completed.stdout


def build_app_jwt(
    issuer: str,
    private_key_path: str | Path,
    *,
    now_epoch: int | None = None,
) -> str:
    """Build a short-lived RS256 GitHub App JWT without exposing key material."""

    clean_issuer = str(issuer).strip()
    if not clean_issuer or len(clean_issuer) > 128:
        raise GitHubAppMintError("APP_ISSUER_INVALID")
    now = int(time.time() if now_epoch is None else now_epoch)
    header = _canonical_segment({"alg": "RS256", "typ": "JWT"})
    payload = _canonical_segment({"exp": now + 540, "iat": now - 60, "iss": clean_issuer})
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _sign_rs256(signing_input, private_key_path)
    return f"{header}.{payload}.{_b64url(signature)}"


def _headers(jwt: str) -> dict[str, str]:
    return {
        "Accept": GITHUB_ACCEPT,
        "Authorization": f"Bearer {jwt}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": _USER_AGENT,
    }


def _json_object(response: httpx.Response, code: str) -> dict[str, Any]:
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GitHubAppMintError(code) from exc
    if not isinstance(value, dict):
        raise GitHubAppMintError(code)
    return value


def _normalize_permissions(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(level) for key, level in value.items()}


def _validate_repository(repository: str) -> tuple[str, str, str]:
    clean = str(repository).strip()
    if clean.count("/") != 1 or any(char in clean for char in "*?[]"):
        raise GitHubAppMintError("REPOSITORY_SCOPE_INVALID")
    owner, name = clean.split("/", 1)
    if not owner or not name or len(owner) > 100 or len(name) > 100:
        raise GitHubAppMintError("REPOSITORY_SCOPE_INVALID")
    return owner, name, clean.lower()


def _validate_secure_parent(path: Path) -> None:
    parent = path.parent
    try:
        info = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise GitHubAppMintError("OUTPUT_PARENT_UNAVAILABLE") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise GitHubAppMintError("OUTPUT_PARENT_NOT_DIRECTORY")
    if info.st_mode & 0o077:
        raise GitHubAppMintError("OUTPUT_PARENT_PERMISSIONS_TOO_OPEN")


def _atomic_write(path: str | Path, payload: bytes, *, mode: int = 0o600) -> None:
    target = Path(path)
    _validate_secure_parent(target)
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".v2-mint-", dir=target.parent)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise GitHubAppMintError("OUTPUT_WRITE_FAILED") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def _validate_expiry(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubAppMintError("TOKEN_EXPIRY_INVALID")
    clean = value.strip()
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubAppMintError("TOKEN_EXPIRY_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GitHubAppMintError("TOKEN_EXPIRY_INVALID")
    if parsed.astimezone(UTC) <= datetime.now(UTC):
        raise GitHubAppMintError("TOKEN_ALREADY_EXPIRED")
    return clean


def mint_installation_token(
    *,
    issuer: str,
    private_key_path: str | Path,
    repository: str,
    token_output_path: str | Path,
    attestation_output_path: str | Path,
    client: httpx.Client | None = None,
) -> GitHubAppMintResult:
    """Mint and persist one exact-scope installation token plus its attestation."""

    owner, repo_name, canonical_repo = _validate_repository(repository)
    jwt = build_app_jwt(issuer, private_key_path)
    owns_client = client is None
    http = client or httpx.Client(
        base_url=GITHUB_API_BASE_URL,
        timeout=30.0,
        follow_redirects=False,
    )
    try:
        installation_response = http.get(
            f"/repos/{owner}/{repo_name}/installation",
            headers=_headers(jwt),
        )
        if installation_response.status_code != 200:
            raise GitHubAppMintError("INSTALLATION_DISCOVERY_FAILED")
        installation = _json_object(installation_response, "INSTALLATION_RESPONSE_INVALID")

        installation_id = installation.get("id")
        if not isinstance(installation_id, int) or installation_id <= 0:
            raise GitHubAppMintError("INSTALLATION_ID_INVALID")
        if installation.get("repository_selection") != "selected":
            raise GitHubAppMintError("INSTALLATION_REPOSITORY_SELECTION_NOT_EXACT")
        if _normalize_permissions(installation.get("permissions")) != REQUIRED_PERMISSIONS:
            raise GitHubAppMintError("INSTALLATION_PERMISSIONS_NOT_EXACT")

        mint_response = http.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers=_headers(jwt),
            json={
                "repositories": [repo_name],
                "permissions": dict(REQUIRED_PERMISSIONS),
            },
        )
        if mint_response.status_code != 201:
            raise GitHubAppMintError("INSTALLATION_TOKEN_MINT_FAILED")
        minted = _json_object(mint_response, "INSTALLATION_TOKEN_RESPONSE_INVALID")

        token = minted.get("token")
        if not isinstance(token, str) or not token.startswith("ghs_"):
            raise GitHubAppMintError("INSTALLATION_TOKEN_FORMAT_INVALID")
        if len(token) < _MIN_SECRET_LENGTH or len(token.encode("utf-8")) > _MAX_SECRET_BYTES:
            raise GitHubAppMintError("INSTALLATION_TOKEN_SIZE_INVALID")
        if _normalize_permissions(minted.get("permissions")) != REQUIRED_PERMISSIONS:
            raise GitHubAppMintError("MINTED_PERMISSIONS_NOT_EXACT")
        if minted.get("repository_selection") != "selected":
            raise GitHubAppMintError("MINTED_REPOSITORY_SELECTION_NOT_EXACT")

        repositories = minted.get("repositories")
        if not isinstance(repositories, list) or len(repositories) != 1:
            raise GitHubAppMintError("MINTED_REPOSITORY_SET_NOT_EXACT")
        full_name = repositories[0].get("full_name") if isinstance(repositories[0], dict) else None
        if not isinstance(full_name, str) or full_name.lower() != canonical_repo:
            raise GitHubAppMintError("MINTED_REPOSITORY_SET_NOT_EXACT")

        expires_at = _validate_expiry(minted.get("expires_at"))
        confirmed_at = datetime.now(UTC).isoformat()
        attestation = {
            "schema": ATTESTATION_INPUT_SCHEMA,
            "provider_type": "github_app",
            "permissions": dict(REQUIRED_PERMISSIONS),
            "unexpected_permissions": [],
            "repository_scopes": [canonical_repo],
            "confirmation": True,
            "confirmation_source": "installation_token_mint_response",
            "confirmed_at": confirmed_at,
        }

        _atomic_write(token_output_path, (token + "\n").encode("utf-8"))
        _atomic_write(
            attestation_output_path,
            (json.dumps(attestation, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        del token
        return GitHubAppMintResult(
            installation_id=installation_id,
            repository=canonical_repo,
            expires_at=expires_at,
            confirmed_at=confirmed_at,
        )
    finally:
        del jwt
        if owns_client:
            http.close()
