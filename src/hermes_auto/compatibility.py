"""Supported-Hermes version range and the fail-closed compatibility probe.

This module answers exactly one question: *is the Hermes installed alongside this
sidecar one whose internals the optional Tier-4 credential bridge may touch?*

Two properties are load-bearing and must survive every future edit:

1. **It must import cleanly when Hermes is absent.** The gateway runs standalone
   (design.md 11.3), so this module imports nothing from ``hermes`` and nothing
   from any other ``hermes_auto`` submodule. It is loadable before any other
   subsystem initializes, which is what makes a *startup* compatibility probe
   possible (design.md 10.2, Tier 4).
2. **It must never assume compatibility it has not verified.** design.md 10.2
   requires "clear fail-closed behavior" from the Tier-4 bridge. Accordingly
   there is no permissive fallback, no "assume compatible if unsure" branch, and
   no override environment variable. ``COMPATIBLE`` is returned only after a
   version string has been successfully parsed *and* matched against the
   supported specifier. Every other outcome is explicitly not usable.

An absent Hermes is reported as information, not as an error: the sidecar is
designed to run without one. ``HERMES_NOT_INSTALLED`` is still not *usable*,
because the question this module answers is about the bridge, not the gateway.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib.metadata

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

# Hermes upstream: https://github.com/NousResearch/hermes-agent
#
# The lower bound is the version NousResearch/hermes-agent declares in its own
# pyproject.toml (0.19.0), confirmed identical on PyPI and in the local install.
# The upper bound is a project decision: design.md states no version bounds.
#
# WARNING: Hermes's git TAGS are CalVer (v2026.7.20, v2026.7.7.2, ...) but its
# DISTRIBUTION version is semver 0.19.0. importlib.metadata reports 0.19.0, never
# 2026.7.20. Do not "correct" this range to CalVer after reading the releases page
# -- a >=2026.1 specifier matches nothing and would report UNSUPPORTED_VERSION for
# every real Hermes install.
#
# Widening this range requires updating tests/unit/test_compatibility.py and a
# CHANGELOG entry.
SUPPORTED_HERMES: str = ">=0.19,<1.0"

# The PyPI/importlib distribution name, verified against three independent
# sources: upstream's pyproject.toml declares name = "hermes-agent", PyPI
# publishes hermes-agent==0.19.0, and importlib.metadata.version("hermes-agent")
# returns 0.19.0 locally while version("hermes") raises PackageNotFoundError.
#
# Do NOT shorten this to "hermes". "hermes" is not a Hermes distribution, so the
# probe would report HERMES_NOT_INSTALLED forever -- silently disabling the very
# gate this module exists to provide.
HERMES_DISTRIBUTION_NAME: str = "hermes-agent"


class CompatibilityStatus(enum.Enum):
    """Outcome of a compatibility probe.

    Values are the lowercase member names so the status serializes readably into
    the ``hermes auto doctor`` diagnostic output.
    """

    COMPATIBLE = "compatible"
    UNSUPPORTED_VERSION = "unsupported_version"
    HERMES_NOT_INSTALLED = "hermes_not_installed"
    VERSION_UNREADABLE = "version_unreadable"


@dataclasses.dataclass(frozen=True)
class CompatibilityResult:
    """The probe's verdict, plus enough context to explain it to an operator."""

    status: CompatibilityStatus
    detected_version: str | None
    supported_range: str
    detail: str

    @property
    def is_usable(self) -> bool:
        """True only for ``COMPATIBLE``.

        Deliberately an equality check against a single member rather than a
        "not in this set of bad statuses" test: a status added later defaults to
        *not* usable, which is the fail-closed direction.
        """
        return self.status is CompatibilityStatus.COMPATIBLE


def detect_hermes_version() -> str | None:
    """Return the installed Hermes distribution version, or None if absent.

    Only ``PackageNotFoundError`` is caught. Any other metadata error -- a
    corrupt dist-info, a permissions failure -- propagates rather than being
    silently converted into "not installed", which would misreport a broken
    environment as a clean standalone one.
    """
    try:
        return importlib.metadata.version(HERMES_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def check_compatibility(version: str | None = None) -> CompatibilityResult:
    """Probe whether ``version`` (or the installed Hermes) is supported.

    Passing ``version`` explicitly is what makes this testable without Hermes
    installed; passing nothing performs the real startup probe.
    """
    if version is None:
        version = detect_hermes_version()

    if version is None:
        return CompatibilityResult(
            status=CompatibilityStatus.HERMES_NOT_INSTALLED,
            detected_version=None,
            supported_range=SUPPORTED_HERMES,
            detail=(
                f"No {HERMES_DISTRIBUTION_NAME!r} distribution found. The gateway "
                f"runs standalone, but the Hermes-native bridge requires "
                f"{HERMES_DISTRIBUTION_NAME} {SUPPORTED_HERMES}."
            ),
        )

    try:
        parsed = Version(version)
    except InvalidVersion:
        return CompatibilityResult(
            status=CompatibilityStatus.VERSION_UNREADABLE,
            detected_version=version,
            supported_range=SUPPORTED_HERMES,
            detail=(
                f"Could not parse {version!r} as a PEP 440 version. Treating as "
                f"incompatible; supported range is {SUPPORTED_HERMES}."
            ),
        )

    # prereleases=True so a Hermes pre-release that falls inside the range (for
    # example 0.20.0rc1) is treated as in-range. Without it, SpecifierSet would
    # silently exclude every pre-release and the probe would reject a Hermes the
    # bridge can actually work with.
    if SpecifierSet(SUPPORTED_HERMES).contains(parsed, prereleases=True):
        return CompatibilityResult(
            status=CompatibilityStatus.COMPATIBLE,
            detected_version=version,
            supported_range=SUPPORTED_HERMES,
            detail=(
                f"{HERMES_DISTRIBUTION_NAME} {version} satisfies {SUPPORTED_HERMES}."
            ),
        )

    return CompatibilityResult(
        status=CompatibilityStatus.UNSUPPORTED_VERSION,
        detected_version=version,
        supported_range=SUPPORTED_HERMES,
        detail=(
            f"{HERMES_DISTRIBUTION_NAME} {version} is outside the supported range "
            f"{SUPPORTED_HERMES}."
        ),
    )


__all__ = [
    "SUPPORTED_HERMES",
    "HERMES_DISTRIBUTION_NAME",
    "CompatibilityStatus",
    "CompatibilityResult",
    "detect_hermes_version",
    "check_compatibility",
]
