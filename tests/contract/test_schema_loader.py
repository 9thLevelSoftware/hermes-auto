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

import jsonschema
import jsonschema.exceptions
import pytest

from hermes_auto.gateway.schemas import (
    SCHEMA_ROOT,
    SchemaLoadError,
    build_registry,
    load_schemas,
    validate,
)

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


# --------------------------------------------------------------------------
# Cross-file ``$ref`` resolution
# --------------------------------------------------------------------------

_CHILD = {
    "$id": "urn:test:child",
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["version"],
    "properties": {"version": {"const": 1}},
    "additionalProperties": False,
}


def _parent(ref: str) -> dict:
    return {
        "$id": "urn:test:parent",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"envelope": {"$ref": ref}},
    }


def test_cross_file_ref_resolves_through_the_registry(tmp_path: pathlib.Path):
    """A ``$ref`` naming another loaded file's ``$id`` is enforced, not ignored.

    ``validate()`` passing a bare schema dict left every cross-file reference
    unresolvable, which is why the request schema had to inline
    ``_hermes_auto`` as a bare ``{"type": "object"}``.
    """
    _write(tmp_path / "child.schema.json", _CHILD)
    _write(tmp_path / "parent.schema.json", _parent("urn:test:child"))
    schemas = load_schemas(tmp_path)
    assert set(schemas) == {"urn:test:child", "urn:test:parent"}

    validate({"envelope": {"version": 1}}, "urn:test:parent", schemas)

    # The referenced constraints actually apply.
    for bad in ({"version": 2}, {"version": 1, "extra": "x"}, {}):
        with pytest.raises(jsonschema.ValidationError):
            validate({"envelope": bad}, "urn:test:parent", schemas)


def test_unresolvable_ref_raises_a_ref_resolution_error(tmp_path: pathlib.Path):
    """A ``$ref`` to an ``$id`` outside the mapping is a distinct, documented type.

    It is ``jsonschema.exceptions._WrappedReferencingError``, a subclass of
    both ``jsonschema.exceptions._RefResolutionError`` and
    ``referencing.exceptions.Unresolvable`` -- and notably NOT a
    ``ValidationError``, so a caller that only catches ``ValidationError``
    would let it escape. Callers must pass a mapping covering the reference
    closure; in practice a bare ``load_schemas()`` over the whole root.
    """
    _write(tmp_path / "parent.schema.json", _parent("urn:test:nowhere"))
    schemas = load_schemas(tmp_path)

    with pytest.raises(jsonschema.exceptions._RefResolutionError) as excinfo:
        validate({"envelope": {}}, "urn:test:parent", schemas)

    assert not isinstance(excinfo.value, jsonschema.ValidationError)
    assert "urn:test:nowhere" in str(excinfo.value)


def test_build_registry_covers_every_packaged_schema():
    """Every ``$id`` under the schema root is resolvable from the registry."""
    schemas = load_schemas()
    registry = build_registry(schemas)

    for schema_id in schemas:
        assert registry.contents(schema_id) == schemas[schema_id]


def test_packaged_schemas_have_no_dangling_refs():
    """Every absolute ``$ref`` in the packaged tree resolves against the full load.

    This is what keeps ``_hermes_auto``'s cross-directory reference honest: a
    renamed or removed ``$id`` fails here rather than at gateway runtime.
    """
    schemas = load_schemas()
    resolver = build_registry(schemas).resolver()

    def refs(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                yield ref
            for child in node.values():
                yield from refs(child)
        elif isinstance(node, list):
            for child in node:
                yield from refs(child)

    seen = 0
    for schema_id, schema in schemas.items():
        for ref in refs(schema):
            seen += 1
            resolver.lookup(ref)  # raises Unresolvable if dangling

    assert seen >= 1, "expected at least one cross-file $ref in the packaged tree"
