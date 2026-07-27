"""Unit tests for the fail-closed Hermes compatibility probe.

Every test here runs in a clean environment with Hermes absent. There are no
conditional skips: a test that quietly skips when Hermes is missing would make
the probe's most important property -- its behavior when Hermes is *not* there --
untested in exactly the environment where it matters most.
"""

from __future__ import annotations

import importlib.metadata

import pytest
from packaging.specifiers import SpecifierSet

from hermes_auto.compatibility import (
    HERMES_DISTRIBUTION_NAME,
    SUPPORTED_HERMES,
    CompatibilityResult,
    CompatibilityStatus,
    check_compatibility,
    detect_hermes_version,
)


@pytest.mark.parametrize("version", ["0.19.0", "0.25.1", "0.99.9"])
def test_in_range_version_is_compatible(version: str) -> None:
    """Versions inside the supported range are compatible and usable.

    "0.19.0" is the installed upstream baseline, so this case also documents
    where the lower bound came from.
    """
    result = check_compatibility(version)

    assert result.status is CompatibilityStatus.COMPATIBLE
    assert result.is_usable is True
    assert result.detected_version == version
    assert result.supported_range == SUPPORTED_HERMES


@pytest.mark.parametrize("version", ["0.18.9", "1.0.0"])
def test_out_of_range_version_is_unsupported(version: str) -> None:
    """Versions outside the range are unsupported, and say so actionably.

    "0.18.9" sits just below the floor; "1.0.0" is the exclusive ceiling. The
    detail must name both the detected version and the supported range so an
    operator reading a doctor report can act without opening the source.
    """
    result = check_compatibility(version)

    assert result.status is CompatibilityStatus.UNSUPPORTED_VERSION
    assert result.is_usable is False
    assert version in result.detail
    assert SUPPORTED_HERMES in result.detail


@pytest.mark.parametrize("version", ["not-a-version", "", "latest"])
def test_unparseable_version_is_unreadable(version: str) -> None:
    """A version that will not parse must never fall through to COMPATIBLE.

    This is the fail-closed test. An unreadable version is the case most likely
    to be "helpfully" turned into an optimistic assumption by a future edit.
    """
    result = check_compatibility(version)

    assert result.status is CompatibilityStatus.VERSION_UNREADABLE
    assert result.is_usable is False


def test_prerelease_inside_range_is_compatible() -> None:
    """A pre-release inside the range is in-range.

    Guards the ``prereleases=True`` argument: SpecifierSet excludes pre-releases
    by default, which would reject a Hermes the bridge can actually work with.
    """
    result = check_compatibility("0.20.0rc1")

    assert result.status is CompatibilityStatus.COMPATIBLE
    assert result.is_usable is True


def test_distribution_name_is_hermes_agent() -> None:
    """Regression guard for the distribution name.

    "hermes" is not a Hermes distribution -- importlib.metadata raises
    PackageNotFoundError for it. Shortening the constant would make the probe
    report HERMES_NOT_INSTALLED forever, silently disabling the gate.
    """
    assert HERMES_DISTRIBUTION_NAME == "hermes-agent"


def test_calver_tag_is_not_treated_as_a_version() -> None:
    """A Hermes git tag must not be mistaken for a distribution version.

    Hermes's git TAGS are CalVer (v2026.7.20, v2026.7.7.2, v2026.6.19) but its
    DISTRIBUTION version is semver -- importlib.metadata reports 0.19.0, never
    2026.7.20. A maintainer who reads the releases page may conclude
    SUPPORTED_HERMES is wrong and "fix" it to something like ">=2026.1". That
    specifier matches nothing, so the probe would report UNSUPPORTED_VERSION for
    every real Hermes install.

    This test exists so that mistake breaks a test with the explanation attached
    instead of silently disabling the probe.
    """
    result = check_compatibility("2026.7.20")

    assert result.status is CompatibilityStatus.UNSUPPORTED_VERSION
    assert result.is_usable is False


def test_detect_reads_the_configured_distribution_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HERMES_DISTRIBUTION_NAME must be the real indirection point.

    Proves the constant is actually passed to importlib.metadata rather than
    being a decorative duplicate of a hardcoded string in the call.
    """
    requested: list[str] = []

    def recorder(name: str) -> str:
        requested.append(name)
        return "0.19.0"

    monkeypatch.setattr(importlib.metadata, "version", recorder)

    assert detect_hermes_version() == "0.19.0"
    assert requested == [HERMES_DISTRIBUTION_NAME]


def test_missing_hermes_reports_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent Hermes is reported explicitly and is not usable.

    The gateway runs standalone, so this is information rather than an error --
    but the bridge is unusable, and this result type answers the bridge question.
    """
    monkeypatch.setattr(
        "hermes_auto.compatibility.detect_hermes_version", lambda: None
    )

    result = check_compatibility()

    assert result.status is CompatibilityStatus.HERMES_NOT_INSTALLED
    assert result.detected_version is None
    assert result.is_usable is False
    assert SUPPORTED_HERMES in result.detail


def test_detect_returns_none_when_distribution_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PackageNotFoundError becomes None, not an exception."""

    def raiser(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", raiser)

    assert detect_hermes_version() is None


def test_no_status_other_than_compatible_is_usable() -> None:
    """Only COMPATIBLE is usable -- for every status, present and future.

    Iterating the enum rather than listing statuses means adding a permissive
    status later fails this test instead of quietly widening what counts as
    usable.
    """
    for status in CompatibilityStatus:
        result = CompatibilityResult(
            status=status,
            detected_version=None,
            supported_range=SUPPORTED_HERMES,
            detail="constructed for the usability check",
        )
        expected = status is CompatibilityStatus.COMPATIBLE
        assert result.is_usable is expected, status


def test_supported_range_is_a_valid_specifier() -> None:
    """A typo in SUPPORTED_HERMES is caught here rather than at startup."""
    specifier = SpecifierSet(SUPPORTED_HERMES)

    assert str(specifier)
