"""Contract tests for the glob-based schema loader in ``hermes_auto.gateway.schemas``.

The loader is the seam every later phase validates through, so its failure
modes matter as much as its happy path: a duplicate ``$id``, an unparseable
file, a missing ``$id``, and a top-level JSON value that is not an object must
all surface as :class:`SchemaLoadError` rather than as a bare
``json.JSONDecodeError``, ``KeyError``, or ``TypeError``.

The positive test scopes itself to ``SCHEMA_ROOT / "wire"``. Loading the whole
schema root would couple this module's result to schema directories owned by
other plans.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from hermes_auto.gateway.schemas import SCHEMA_ROOT, SchemaLoadError, load_schemas

pytestmark = pytest.mark.contract

WIRE_SCHEMA_IDS = {
    "https://hermes-auto-router.dev/schema/wire/openai-chat-request.v1.json",
    "https://hermes-auto-router.dev/schema/wire/openai-chat-response.v1.json",
    "https://hermes-auto-router.dev/schema/wire/sse-stream-contract.v1.json",
}


def _write(path: pathlib.Path, payload: object) -> pathlib.Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_schemas_finds_wire_schemas():
    schemas = load_schemas(SCHEMA_ROOT / "wire")
    assert len(schemas) >= 3
    assert WIRE_SCHEMA_IDS <= set(schemas)
    assert all(isinstance(s, dict) for s in schemas.values())


def test_discovery_is_recursive(tmp_path: pathlib.Path):
    """Nested directories are discovered, which is what lets other plans add
    schema directories without editing the loader."""
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    _write(nested / "deep.schema.json", {"$id": "urn:test:deep", "type": "object"})
    assert set(load_schemas(tmp_path)) == {"urn:test:deep"}


def test_non_schema_json_is_ignored(tmp_path: pathlib.Path):
    """Only ``*.schema.json`` is picked up; other JSON in the tree is not."""
    _write(tmp_path / "notes.json", {"$id": "urn:test:ignored"})
    _write(tmp_path / "real.schema.json", {"$id": "urn:test:real"})
    assert set(load_schemas(tmp_path)) == {"urn:test:real"}


def test_missing_root_returns_empty(tmp_path: pathlib.Path):
    """A directory a later plan has not created yet is not an error."""
    assert load_schemas(tmp_path / "nope") == {}


def test_empty_root_returns_empty(tmp_path: pathlib.Path):
    assert load_schemas(tmp_path) == {}


def test_duplicate_id_raises(tmp_path: pathlib.Path):
    first = _write(tmp_path / "one.schema.json", {"$id": "urn:test:dup", "type": "object"})
    second = _write(tmp_path / "two.schema.json", {"$id": "urn:test:dup", "type": "object"})

    with pytest.raises(SchemaLoadError) as excinfo:
        load_schemas(tmp_path)

    message = str(excinfo.value)
    assert first.name in message
    assert second.name in message


def test_unparseable_file_raises(tmp_path: pathlib.Path):
    broken = tmp_path / "broken.schema.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(SchemaLoadError) as excinfo:
        load_schemas(tmp_path)

    assert broken.name in str(excinfo.value)


def test_missing_id_raises(tmp_path: pathlib.Path):
    noid = _write(tmp_path / "noid.schema.json", {"type": "object"})

    with pytest.raises(SchemaLoadError) as excinfo:
        load_schemas(tmp_path)

    assert noid.name in str(excinfo.value)


def test_non_object_schema_raises(tmp_path: pathlib.Path):
    """A top-level array must raise SchemaLoadError, not TypeError.

    Without an explicit isinstance guard, ``"$id" not in [1, 2, 3]`` evaluates
    cleanly and the subsequent key access raises ``TypeError``, breaking the
    module's stated error contract.
    """
    notobj = _write(tmp_path / "notanobject.schema.json", [1, 2, 3])

    with pytest.raises(SchemaLoadError) as excinfo:
        load_schemas(tmp_path)

    message = str(excinfo.value)
    assert notobj.name in message
    assert "list" in message
