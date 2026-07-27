"""Unit tests for the atomic runtime-file contract.

These tests are the executable form of the supervision contract. The properties
under test are the ones that make ``status`` trustworthy: an atomic write, an
identity that survives PID reuse, and a hard distinction between "not started"
and "damaged".
"""

from __future__ import annotations

import ast
import json
import os
import pathlib

import pytest

from hermes_auto.state import runtime as rt
from hermes_auto.state.runtime import (
    RuntimeFile,
    RuntimeFileError,
    clear_runtime,
    new_instance_id,
    read_runtime,
    runtime_path,
    write_runtime,
)
from hermes_auto.state.paths import STATE_DIR_ENV_VAR


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path / "state"))


def make(**overrides: object) -> RuntimeFile:
    fields: dict = {
        "pid": 4321,
        "port": 8787,
        "admin_port": 8788,
        "instance_id": new_instance_id(),
        "started_at": "2026-07-26T12:00:00+00:00",
        "exe": r"C:\Users\dev\hermes-auto\.venv\Scripts\python.exe",
    }
    fields.update(overrides)
    return RuntimeFile(**fields)


# ---------------------------------------------------------------------------
# Round trip and shape
# ---------------------------------------------------------------------------


def test_round_trip_preserves_every_field() -> None:
    original = make()

    write_runtime(original)

    assert read_runtime() == original


def test_on_disk_shape_matches_the_supervision_contract() -> None:
    """Plans 02-04 and 02-07 both parse this file; the key set is the contract.

    ``admin_port`` is present because the primary stop path on every platform is
    an authenticated POST to the admin listener -- Windows has no SIGTERM -- so a
    supervisor that cannot find the admin port degrades every stop to a kill.
    """
    write_runtime(make())

    document = json.loads(runtime_path(create=False).read_text(encoding="utf-8"))

    assert set(document) == {
        "pid",
        "port",
        "admin_port",
        "instance_id",
        "started_at",
        "exe",
    }


def test_serialization_is_deterministic() -> None:
    """Equal content must produce identical bytes, so any diff is a real change."""
    record = make()

    assert record.to_json() == record.to_json()
    assert json.loads(record.to_json())["instance_id"] == record.instance_id


def test_runtime_file_is_frozen() -> None:
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        make().pid = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# instance_id: the PID-reuse defense
# ---------------------------------------------------------------------------


def test_instance_ids_are_unique_across_calls() -> None:
    minted = {new_instance_id() for _ in range(256)}

    assert len(minted) == 256


def test_instance_id_is_not_derived_from_the_pid_or_the_clock() -> None:
    """A repeatable identity cannot distinguish this process from a dead one.

    If ``instance_id`` were a function of the PID, a recycled PID would reproduce
    the identity of the process it replaced and a stale runtime file would read
    as a live gateway -- the exact failure this field exists to prevent, and one
    that is routine on Windows.
    """
    identity = new_instance_id()

    assert str(os.getpid()) not in identity
    assert len(identity) == rt.INSTANCE_ID_BYTES * 2
    assert all(character in "0123456789abcdef" for character in identity)


# ---------------------------------------------------------------------------
# Absent versus corrupt
# ---------------------------------------------------------------------------


def test_missing_file_reads_as_not_started() -> None:
    assert read_runtime() is None


def test_reading_a_missing_file_does_not_create_an_install(
    tmp_path: pathlib.Path,
) -> None:
    """Asking "is it running?" on a clean machine must leave the machine clean."""
    read_runtime()

    assert not (tmp_path / "state" / "runtime").exists()


def test_truncated_file_raises_rather_than_reading_as_stopped() -> None:
    write_runtime(make())
    target = runtime_path(create=False)
    target.write_bytes(target.read_bytes()[:20])

    with pytest.raises(RuntimeFileError, match="not valid JSON"):
        read_runtime()


def test_empty_file_raises() -> None:
    write_runtime(make())
    runtime_path(create=False).write_bytes(b"")

    with pytest.raises(RuntimeFileError):
        read_runtime()


@pytest.mark.parametrize(
    "missing",
    ["pid", "port", "admin_port", "instance_id", "started_at", "exe"],
)
def test_valid_json_missing_a_required_field_raises(missing: str) -> None:
    write_runtime(make())
    target = runtime_path(create=False)
    document = json.loads(target.read_text(encoding="utf-8"))
    del document[missing]
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeFileError) as caught:
        read_runtime()

    assert missing in str(caught.value)


def test_empty_instance_id_raises() -> None:
    """An empty identity would make every live gateway look stale."""
    write_runtime(make())
    target = runtime_path(create=False)
    document = json.loads(target.read_text(encoding="utf-8"))
    document["instance_id"] = "   "
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeFileError, match="instance_id"):
        read_runtime()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", "4321"),
        ("port", True),
        ("admin_port", 8788.5),
        ("instance_id", 12345),
        ("exe", None),
    ],
)
def test_wrong_typed_field_raises_a_typed_error(field: str, value: object) -> None:
    """No bare ``TypeError`` escapes; ``bool`` is rejected despite subclassing int."""
    write_runtime(make())
    target = runtime_path(create=False)
    document = json.loads(target.read_text(encoding="utf-8"))
    document[field] = value
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeFileError) as caught:
        read_runtime()

    assert field in str(caught.value)


def test_json_array_at_top_level_raises() -> None:
    write_runtime(make())
    runtime_path(create=False).write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(RuntimeFileError, match="JSON object"):
        read_runtime()


def test_write_runtime_rejects_a_non_runtimefile() -> None:
    with pytest.raises(RuntimeFileError):
        write_runtime({"pid": 1})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_successful_write_leaves_no_temp_file_behind() -> None:
    write_runtime(make())

    leftovers = [
        entry.name
        for entry in runtime_path(create=False).parent.iterdir()
        if entry.name != rt.RUNTIME_FILE_NAME
    ]

    assert leftovers == []


def test_crash_between_temp_write_and_replace_leaves_the_previous_file_intact() -> None:
    """The failure the whole temp-plus-replace dance exists to prevent.

    Simulates a process dying after the temp file is written but before the
    rename. The previously published file must still be complete and readable --
    not truncated, not empty -- because a concurrent ``status`` reads it on a
    timer and would otherwise report a healthy gateway as corrupt.
    """
    first = make(instance_id="a" * 32, port=8787)
    write_runtime(first)
    target = runtime_path(create=False)
    directory = target.parent

    # A crashed write: the temp file exists, os.replace never happened.
    orphan = directory / f".{rt.RUNTIME_FILE_NAME}.crash.tmp"
    orphan.write_bytes(make(instance_id="b" * 32, port=9999).to_json().encode("utf-8")[:40])

    assert read_runtime() == first

    orphan.unlink()


def test_replace_overwrites_an_existing_file_in_place() -> None:
    write_runtime(make(instance_id="a" * 32))
    second = make(instance_id="b" * 32)

    write_runtime(second)

    assert read_runtime() == second


def test_write_is_atomic_by_construction_not_by_truncating_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the target is never opened for truncation.

    A ``open(target, "w")`` implementation passes a round-trip test and still
    exposes a zero-length window to every concurrent reader. This asserts the
    published path is only ever reached through ``os.replace``.
    """
    write_runtime(make(instance_id="a" * 32))
    target = runtime_path(create=False)
    observed: list[str] = []

    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        observed.append(str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    write_runtime(make(instance_id="b" * 32))

    assert observed == [str(target)]


def test_temp_file_is_created_in_the_target_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.replace`` is only atomic within one filesystem.

    A temp file in the system temp directory would silently downgrade the rename
    to a copy across a filesystem boundary, which is exactly not atomic.
    """
    seen: dict = {}
    real_mkstemp = rt.tempfile.mkstemp

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(rt.tempfile, "mkstemp", spy)
    target = write_runtime(make())

    assert pathlib.Path(seen["dir"]) == target.parent


def test_write_fsyncs_before_replacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without fsync the rename can land before the data does."""
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    monkeypatch.setattr(
        os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        os,
        "replace",
        lambda src, dst: (order.append("replace"), real_replace(src, dst))[1],
    )

    write_runtime(make())

    assert order == ["fsync", "replace"]


# ---------------------------------------------------------------------------
# clear_runtime
# ---------------------------------------------------------------------------


def test_clear_runtime_is_idempotent() -> None:
    write_runtime(make())

    assert clear_runtime() is True
    assert clear_runtime() is False
    assert read_runtime() is None


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_module_contains_no_liveness_probing() -> None:
    """Liveness is a /healthz question owned by plan 02-07; this module is storage.

    Checked against the parsed AST rather than the raw text, because the module
    docstring legitimately *names* the things it excludes ("no signal-zero check,
    no psutil"). A substring scan would fire on that documentation and would have
    to be deleted -- leaving the invariant unchecked -- so it is parsed instead:
    this asserts on what the module actually calls and imports.
    """
    tree = ast.parse(pathlib.Path(rt.__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute):
            called.add(node.attr)

    assert not imported & {"psutil", "signal", "subprocess", "ctypes"}, imported
    assert not called & {"kill", "waitpid", "OpenProcess", "terminate"}, called


def test_module_imports_no_third_party_package() -> None:
    source = pathlib.Path(rt.__file__).read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    ]

    assert imports == [
        "import dataclasses",
        "import json",
        "import os",
        "import pathlib",
        "import secrets",
        "import tempfile",
        "from typing import Any",
        "from .paths import runtime_dir",
    ], imports
