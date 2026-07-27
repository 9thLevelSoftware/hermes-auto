"""Shared pytest fixtures for the hermes-auto-router test suite."""

import pathlib

import pytest


@pytest.fixture(scope="module")
def repo_root() -> pathlib.Path:
    """Absolute path to the repository root."""
    return pathlib.Path(__file__).resolve().parent.parent
