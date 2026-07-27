"""Shared pytest fixtures for the hermes-auto-router test suite."""

import pathlib

import pytest


@pytest.fixture(scope="module")
def repo_root() -> pathlib.Path:
    """Absolute path to the repository root."""
    return pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def schema_dir(repo_root: pathlib.Path) -> pathlib.Path:
    """Directory holding the versioned JSON Schemas.

    Deliberately does not assert existence: the directory is created by later
    Phase 1 plans (01-03, 01-04), not by the plan that defines this fixture.
    """
    return repo_root / "src" / "hermes_auto" / "data" / "schema"
