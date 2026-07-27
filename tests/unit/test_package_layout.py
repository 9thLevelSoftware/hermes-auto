"""Prove the package layout declared in design.md §17 is importable and complete."""

import importlib
import pathlib

EXPECTED_SUBPACKAGES = (
    "gateway",
    "routing",
    "state",
    "inventory",
    "health",
    "adapters",
    "harnesses",
    "telemetry",
    "evaluation",
    "learning",
    "data",
)

EXPECTED_TEST_DIRS = (
    "unit",
    "property",
    "contract",
    "integration",
    "e2e",
    "fault",
    "performance",
    "fixtures",
)


def test_top_level_package_imports() -> None:
    """The distribution's import package exposes a non-empty version string."""
    hermes_auto = importlib.import_module("hermes_auto")

    assert isinstance(hermes_auto.__version__, str)
    assert hermes_auto.__version__


def test_all_subpackages_importable() -> None:
    """Every subpackage named in design.md §17 imports without error."""
    for name in EXPECTED_SUBPACKAGES:
        module = importlib.import_module(f"hermes_auto.{name}")
        assert module.__name__ == f"hermes_auto.{name}"


def test_test_tree_directories_exist(repo_root: pathlib.Path) -> None:
    """All eight tests/ subdirectories from design.md §17 exist."""
    missing = [
        name
        for name in EXPECTED_TEST_DIRS
        if not (repo_root / "tests" / name).is_dir()
    ]

    assert not missing, f"missing tests/ subdirectories: {missing}"
