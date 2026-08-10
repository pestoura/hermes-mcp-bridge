"""Acceptance for the canonical product build metric (``bridge_build_info``).

The bridge separates two identities on purpose: the frozen public contract
(``bridge_info.version == 1.0.0``) and the product build actually running.
These tests prove the second identity is derived from canonical metadata,
stays bounded, and never displaces the first.
"""

from __future__ import annotations

import pytest

from hermes_mcp_bridge import build_metadata as bm
from hermes_mcp_bridge.contracts import CURRENT_CONTRACT_VERSION, SCHEMA_VERSION
from hermes_mcp_bridge.observability.metrics import (
    ALLOWED_LABELS,
    BOUNDED_LABEL_VALUES,
    get_metrics,
    get_registry,
    render_prometheus,
    set_bridge_info,
    set_build_info,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    get_registry().reset()
    yield
    get_registry().reset()


def test_product_release_is_the_canonical_source() -> None:
    assert bm.PRODUCT_RELEASE == "2.0.1"


def test_release_resolves_from_canonical_constant(monkeypatch) -> None:
    monkeypatch.delenv(bm.RELEASE_ENV_VAR, raising=False)
    assert bm.resolve_release() == bm.PRODUCT_RELEASE


def test_release_env_override_is_accepted(monkeypatch) -> None:
    monkeypatch.setenv(bm.RELEASE_ENV_VAR, "2.1.0")
    assert bm.resolve_release() == "2.1.0"


def test_malformed_release_degrades_to_unknown(monkeypatch) -> None:
    monkeypatch.setenv(bm.RELEASE_ENV_VAR, "not a version")
    assert bm.resolve_release() == bm.UNKNOWN


def test_revision_is_shortened_from_build_provenance(monkeypatch) -> None:
    monkeypatch.delenv("BRIDGE_BUILD_REVISION", raising=False)
    monkeypatch.setenv("OCI_IMAGE_REVISION", "3717bd5469b061a44294b27e1a7510d477d3752b")
    assert bm.resolve_revision() == "3717bd5"


def test_explicit_revision_variable_wins(monkeypatch) -> None:
    monkeypatch.setenv("OCI_IMAGE_REVISION", "aaaaaaaaaa")
    monkeypatch.setenv("BRIDGE_BUILD_REVISION", "3717bd5")
    assert bm.resolve_revision() == "3717bd5"


def test_missing_revision_degrades_to_unknown(monkeypatch) -> None:
    for name in bm.REVISION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert bm.resolve_revision() == bm.UNKNOWN


def test_non_hex_revision_degrades_to_unknown(monkeypatch) -> None:
    monkeypatch.delenv("BRIDGE_BUILD_REVISION", raising=False)
    monkeypatch.setenv("OCI_IMAGE_REVISION", "zzzzzzz")
    assert bm.resolve_revision() == bm.UNKNOWN


def test_metadata_carries_the_frozen_contract_identity() -> None:
    metadata = bm.build_metadata()
    assert metadata.contract_version == CURRENT_CONTRACT_VERSION == "1.0.0"
    assert metadata.schema_version == SCHEMA_VERSION == "0.6.1"


def test_build_info_metric_is_rendered_with_bounded_labels() -> None:
    set_build_info()
    text = render_prometheus()
    resolved = bm.get_build_metadata()
    expected = (
        "bridge_build_info{"
        f'contract_version="{resolved.contract_version}",'
        f'release="{resolved.release}",'
        f'revision="{resolved.revision}",'
        f'schema_version="{resolved.schema_version}"'
        "} 1"
    )
    assert expected in text


def test_build_labels_are_allow_listed_and_bounded() -> None:
    for label in ("release", "revision", "contract_version", "schema_version"):
        assert label in ALLOWED_LABELS
        assert label in BOUNDED_LABEL_VALUES
        assert len(BOUNDED_LABEL_VALUES[label]) <= len(BOUNDED_LABEL_VALUES["tool"])


def test_out_of_domain_build_label_cannot_open_a_new_series() -> None:
    metrics = get_metrics()
    metrics.build_info.set(1.0, release="9.9.9-bogus", revision="deadbee")
    text = render_prometheus()
    assert 'release="9.9.9-bogus"' not in text
    assert 'release="unknown"' in text


def test_build_label_fallback_is_unknown_and_in_domain() -> None:
    from hermes_mcp_bridge.observability.metrics import (
        BOUNDED_LABEL_VALUES as domains,
    )
    from hermes_mcp_bridge.observability.metrics import (
        FALLBACK_LABEL_VALUES as fallbacks,
    )

    for label in ("release", "revision", "contract_version", "schema_version"):
        assert fallbacks[label] == bm.UNKNOWN
        assert fallbacks[label] in domains[label]
    # Descriptive labels keep the historical "other" sentinel.
    for label in ("tool", "outcome", "mode"):
        assert fallbacks[label] == "other"
        assert fallbacks[label] in domains[label]


def test_bridge_info_still_reports_the_contract_version() -> None:
    set_bridge_info(CURRENT_CONTRACT_VERSION)
    set_build_info()
    text = render_prometheus()
    assert 'bridge_info{version="1.0.0"} 1' in text
    assert "bridge_build_info{" in text
