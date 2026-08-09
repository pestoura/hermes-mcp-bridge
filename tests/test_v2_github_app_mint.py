from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from hermes_mcp_bridge.v2 import github_app_mint as mintmod
from hermes_mcp_bridge.v2.github_attestation import (
    ATTESTATION_INPUT_SCHEMA,
    REQUIRED_PERMISSIONS,
)

REPOSITORY = "pestoura/hermes-mcp-bridge"
TOKEN = "ghs_" + ("opaque-token-material_" * 8)


def _private_dir(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path


def _key(tmp_path, mode: int = 0o600):
    path = tmp_path / "app.pem"
    path.write_text("dummy-private-key-material", encoding="utf-8")
    path.chmod(mode)
    return path


def _installation(*, permissions=None):
    return {
        "id": 4242,
        "repository_selection": "selected",
        "permissions": dict(REQUIRED_PERMISSIONS if permissions is None else permissions),
    }


def _minted(*, permissions=None, repositories=None):
    return {
        "token": TOKEN,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "permissions": dict(REQUIRED_PERMISSIONS if permissions is None else permissions),
        "repository_selection": "selected",
        "repositories": repositories if repositories is not None else [{"full_name": REPOSITORY}],
    }


def test_mint_writes_exact_scope_token_and_sanitized_attestation(tmp_path, monkeypatch):
    _private_dir(tmp_path)
    key = _key(tmp_path)
    token_out = tmp_path / "direct.token"
    attestation_out = tmp_path / "attestation.json"
    seen = []

    monkeypatch.setattr(mintmod, "build_app_jwt", lambda issuer, path: "test.jwt.value")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer test.jwt.value"
        assert request.headers["x-github-api-version"] == "2026-03-10"
        if request.method == "GET":
            assert request.url.path == "/repos/pestoura/hermes-mcp-bridge/installation"
            return httpx.Response(200, json=_installation())
        assert request.method == "POST"
        assert request.url.path == "/app/installations/4242/access_tokens"
        body = json.loads(request.content)
        assert body == {
            "repositories": ["hermes-mcp-bridge"],
            "permissions": REQUIRED_PERMISSIONS,
        }
        return httpx.Response(201, json=_minted())

    client = httpx.Client(
        base_url=mintmod.GITHUB_API_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    result = mintmod.mint_installation_token(
        issuer="Iv1.non-secret-client-id",
        private_key_path=key,
        repository=REPOSITORY,
        token_output_path=token_out,
        attestation_output_path=attestation_out,
        client=client,
    )

    assert len(seen) == 2
    assert token_out.read_text(encoding="utf-8").strip() == TOKEN
    assert stat.S_IMODE(token_out.stat().st_mode) == 0o600
    assert stat.S_IMODE(attestation_out.stat().st_mode) == 0o600

    attestation = json.loads(attestation_out.read_text(encoding="utf-8"))
    assert attestation == {
        "schema": ATTESTATION_INPUT_SCHEMA,
        "provider_type": "github_app",
        "permissions": REQUIRED_PERMISSIONS,
        "unexpected_permissions": [],
        "repository_scopes": [REPOSITORY],
        "confirmation": True,
        "confirmation_source": "installation_token_mint_response",
        "confirmed_at": result.confirmed_at,
    }
    summary = result.summary()
    assert "token" not in summary
    assert "jwt" not in summary
    assert "private_key" not in summary
    assert "authorization" not in summary
    assert summary["secret_values_stored_in_output"] is False
    assert summary["secret_paths_stored_in_output"] is False
    assert result.repository == REPOSITORY
    assert result.installation_id == 4242


@pytest.mark.parametrize(
    ("operation", "status_code", "expected"),
    [
        ("discovery", 401, "INSTALLATION_DISCOVERY_JWT_REJECTED"),
        ("discovery", 403, "INSTALLATION_DISCOVERY_FORBIDDEN"),
        ("discovery", 404, "INSTALLATION_NOT_FOUND_FOR_REPOSITORY"),
        ("discovery", 422, "INSTALLATION_DISCOVERY_REQUEST_REJECTED"),
        ("mint", 401, "INSTALLATION_TOKEN_JWT_REJECTED"),
        ("mint", 403, "INSTALLATION_TOKEN_FORBIDDEN"),
        ("mint", 404, "INSTALLATION_TOKEN_INSTALLATION_NOT_FOUND"),
        ("mint", 422, "INSTALLATION_TOKEN_SCOPE_REJECTED"),
        ("mint", 500, "MINT_HTTP_500"),
    ],
)
def test_http_failure_codes_are_actionable_and_secret_free(operation, status_code, expected):
    code = mintmod._http_failure_code(operation, status_code)
    assert code == expected
    assert code.isascii()
    assert all(char.isupper() or char.isdigit() or char == "_" for char in code)


def test_mint_rejects_discovery_404_with_specific_code(tmp_path, monkeypatch):
    _private_dir(tmp_path)
    key = _key(tmp_path)
    monkeypatch.setattr(mintmod, "build_app_jwt", lambda issuer, path: "test.jwt.value")

    client = httpx.Client(
        base_url=mintmod.GITHUB_API_BASE_URL,
        transport=httpx.MockTransport(lambda request: httpx.Response(404, json={})),
    )
    with pytest.raises(mintmod.GitHubAppMintError) as exc:
        mintmod.mint_installation_token(
            issuer="Iv1.non-secret-client-id",
            private_key_path=key,
            repository=REPOSITORY,
            token_output_path=tmp_path / "direct.token",
            attestation_output_path=tmp_path / "attestation.json",
            client=client,
        )
    assert exc.value.code == "INSTALLATION_NOT_FOUND_FOR_REPOSITORY"


def test_mint_rejects_unexpected_installation_permission_before_token_request(
    tmp_path, monkeypatch
):
    _private_dir(tmp_path)
    key = _key(tmp_path)
    calls = []
    permissions = {**REQUIRED_PERMISSIONS, "contents": "read"}
    monkeypatch.setattr(mintmod, "build_app_jwt", lambda issuer, path: "test.jwt.value")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=_installation(permissions=permissions))

    client = httpx.Client(
        base_url=mintmod.GITHUB_API_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(mintmod.GitHubAppMintError) as exc:
        mintmod.mint_installation_token(
            issuer="12345",
            private_key_path=key,
            repository=REPOSITORY,
            token_output_path=tmp_path / "direct.token",
            attestation_output_path=tmp_path / "attestation.json",
            client=client,
        )
    assert exc.value.code == "INSTALLATION_PERMISSIONS_NOT_EXACT"
    assert calls == ["GET"]


def test_mint_rejects_extra_repository_and_writes_nothing(tmp_path, monkeypatch):
    _private_dir(tmp_path)
    key = _key(tmp_path)
    token_out = tmp_path / "direct.token"
    attestation_out = tmp_path / "attestation.json"
    monkeypatch.setattr(mintmod, "build_app_jwt", lambda issuer, path: "test.jwt.value")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_installation())
        return httpx.Response(
            201,
            json=_minted(
                repositories=[
                    {"full_name": REPOSITORY},
                    {"full_name": "pestoura/unexpected"},
                ]
            ),
        )

    client = httpx.Client(
        base_url=mintmod.GITHUB_API_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(mintmod.GitHubAppMintError) as exc:
        mintmod.mint_installation_token(
            issuer="12345",
            private_key_path=key,
            repository=REPOSITORY,
            token_output_path=token_out,
            attestation_output_path=attestation_out,
            client=client,
        )
    assert exc.value.code == "MINTED_REPOSITORY_SET_NOT_EXACT"
    assert not token_out.exists()
    assert not attestation_out.exists()


def test_private_key_requires_0600_style_permissions(tmp_path):
    _private_dir(tmp_path)
    key = _key(tmp_path, mode=0o644)
    with pytest.raises(mintmod.GitHubAppMintError) as exc:
        mintmod._secure_private_key_fd(key)
    assert exc.value.code == "PRIVATE_KEY_PERMISSIONS_TOO_OPEN"


def test_output_parent_must_be_private(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(mintmod.GitHubAppMintError) as exc:
        mintmod._atomic_write(tmp_path / "secret", b"value\n")
    assert exc.value.code == "OUTPUT_PARENT_PERMISSIONS_TOO_OPEN"


def test_repository_scope_rejects_wildcards():
    with pytest.raises(mintmod.GitHubAppMintError) as exc:
        mintmod._validate_repository("pestoura/*")
    assert exc.value.code == "REPOSITORY_SCOPE_INVALID"
