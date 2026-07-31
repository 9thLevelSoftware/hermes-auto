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
import referencing.exceptions

from hermes_auto.gateway.schemas import (
    EXPECTED_SCHEMA_IDS,
    SCHEMA_ROOT,
    SchemaLoadError,
    build_registry,
    build_validator,
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


def test_unresolvable_ref_raises_schema_load_error(tmp_path: pathlib.Path):
    """An unresolvable ``$ref`` surfaces as ``SchemaLoadError``, naming the ref.

    Left unwrapped it escapes as ``jsonschema.exceptions._WrappedReferencingError``,
    which is neither a ``ValidationError`` nor any type this module declares, so
    a caller catching the documented error types lets it through. The error
    message must name both the missing ``$id`` and the ids that *were* supplied,
    because the fix is always "you passed too narrow a mapping".
    """
    _write(tmp_path / "parent.schema.json", _parent("urn:test:nowhere"))
    schemas = load_schemas(tmp_path)

    for call in (
        lambda: validate({"envelope": {}}, "urn:test:parent", schemas),
        lambda: build_validator("urn:test:parent", schemas),
    ):
        with pytest.raises(SchemaLoadError) as excinfo:
            call()
        message = str(excinfo.value)
        assert "urn:test:nowhere" in message
        assert "urn:test:parent" in message

    assert not isinstance(excinfo.value, jsonschema.ValidationError)


def test_unresolvable_ref_is_raised_eagerly(tmp_path: pathlib.Path):
    """The error does not depend on the instance reaching the ``$ref``.

    This is the whole defect. A ``$ref`` resolves lazily, so an instance that
    omits the referencing property never traverses it. A gateway that
    scope-loaded ``SCHEMA_ROOT / "wire"`` therefore validated every plain
    fixture cleanly and would have failed only on requests carrying
    ``_hermes_auto`` -- which in production is all of them. Both instances below
    must raise, including the one that never reaches ``envelope``.
    """
    _write(tmp_path / "parent.schema.json", _parent("urn:test:nowhere"))
    schemas = load_schemas(tmp_path)

    for instance in ({}, {"unrelated": 1}, {"envelope": {}}):
        with pytest.raises(SchemaLoadError):
            validate(instance, "urn:test:parent", schemas)


def test_unresolvable_ref_chains_the_public_referencing_error(tmp_path: pathlib.Path):
    """The wrapped cause is ``referencing.exceptions.Unresolvable``.

    ``jsonschema.exceptions._RefResolutionError`` is a private back-compat shim;
    asserting against it couples this contract to an underscore-prefixed name
    jsonschema is free to drop. ``Unresolvable`` is the public, stable type in
    the same MRO and is what the module documents and catches.
    """
    _write(tmp_path / "parent.schema.json", _parent("urn:test:nowhere"))
    schemas = load_schemas(tmp_path)

    with pytest.raises(SchemaLoadError) as excinfo:
        validate({"envelope": {}}, "urn:test:parent", schemas)

    assert isinstance(excinfo.value.__cause__, referencing.exceptions.Unresolvable)


def test_wire_only_load_is_not_a_silent_partial_validation():
    """Scope-loading ``wire`` fails loudly, on a plain request too.

    The request schema ``$ref``s the routing metadata envelope across
    directories. A caller who loads only ``wire`` gets a schema whose closure is
    incomplete, and must be told at once rather than on first production
    traffic.
    """
    wire_only = load_schemas(SCHEMA_ROOT / "wire")
    request_id = "https://hermes-auto-router.dev/schema/wire/openai-chat-request.v1.json"
    plain = {"model": "auto:balanced", "messages": [{"role": "user", "content": "hi"}]}

    with pytest.raises(SchemaLoadError) as excinfo:
        validate(plain, request_id, wire_only)
    assert "hermes-auto-metadata.v1.json" in str(excinfo.value)

    # The same instance against the full load is fine, proving the failure is
    # about the mapping's coverage and not about the instance.
    validate(plain, request_id, load_schemas())


# --------------------------------------------------------------------------
# Reusable validators and the schema-set identity gate
# --------------------------------------------------------------------------


def test_build_validator_is_reusable_and_agrees_with_validate():
    """A hoisted validator accepts and rejects exactly what ``validate`` does."""
    schemas = load_schemas()
    request_id = "https://hermes-auto-router.dev/schema/wire/openai-chat-request.v1.json"
    validator = build_validator(request_id, schemas)

    good = {"model": "auto:balanced", "messages": [{"role": "user", "content": "hi"}]}
    bad = {"messages": []}  # no `model`

    # Reuse is the point: the same validator serves many instances.
    for _ in range(3):
        validator.validate(good)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(bad)

    validate(good, request_id, schemas)
    with pytest.raises(jsonschema.ValidationError):
        validate(bad, request_id, schemas)


def test_build_validator_enforces_the_cross_file_ref():
    """A hoisted validator keeps the ``$ref``'d envelope rules, not just the shape."""
    request_id = "https://hermes-auto-router.dev/schema/wire/openai-chat-request.v1.json"
    validator = build_validator(request_id, load_schemas())

    def request(metadata):
        return {
            "model": "auto:balanced",
            "messages": [{"role": "user", "content": "hi"}],
            "_hermes_auto": metadata,
        }

    validator.validate(
        request(
            {
                "protocol_version": 1,
                "root_session_id": "sess-abc",
                "virtual_model": "auto:balanced",
                "plugin_version": "0.1.0",
            }
        )
    )

    for bad in (
        {"protocol_version": 99, "root_session_id": "s", "virtual_model": "m",
         "plugin_version": "v"},
        {"protocol_version": 1, "root_session_id": "s", "virtual_model": "m",
         "plugin_version": "v", "junk": "x"},
        {},
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(request(bad))


def test_build_validator_rejects_unknown_schema_id():
    with pytest.raises(KeyError):
        build_validator("urn:test:absent", load_schemas())


def test_packaged_schema_set_matches_expected_ids():
    """An identity gate, not a count.

    A count check alone passes when a stray schema is added
    in the same change that deletes or renames a required one. Comparing the set
    catches both halves. Adding or renaming a packaged schema must be a
    deliberate edit to ``EXPECTED_SCHEMA_IDS``.
    """
    assert set(load_schemas()) == set(EXPECTED_SCHEMA_IDS)
    assert len(EXPECTED_SCHEMA_IDS) == 5

    # Every declared id is genuinely loadable, so the constant cannot drift into
    # naming a schema that does not exist.
    for schema_id in EXPECTED_SCHEMA_IDS:
        build_validator(schema_id)


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
