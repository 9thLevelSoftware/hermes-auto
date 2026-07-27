"""Load and validate the versioned JSON Schemas packaged under ``hermes_auto/data/schema``.

Discovery is by **recursive glob** (``**/*.schema.json``), never by a hardcoded
filename list or an explicit registry. That is a deliberate design constraint,
not an implementation shortcut: a new schema directory -- ``routing/``,
``telemetry/``, or anything a later phase adds -- becomes loadable by dropping
files into it, with no edit to this module and therefore no write conflict
between plans that own different schema directories.

The module does exactly two things: it loads schemas and it validates instances
against them. It holds no routing, scoring, eligibility, canonicalization,
token-estimation, or authentication logic.

No caching, no ``lru_cache``, and no module-level singleton mapping: later
phases load schemas from temporary directories in tests, and a cached global
would silently return the packaged set instead.
"""

from __future__ import annotations

import json
import pathlib

import jsonschema
import referencing
import referencing.jsonschema

__all__ = ["SCHEMA_ROOT", "SchemaLoadError", "build_registry", "load_schemas", "validate"]

#: Root of the packaged schema tree. Every ``*.schema.json`` beneath it, at any
#: depth, is discoverable by :func:`load_schemas`.
SCHEMA_ROOT: pathlib.Path = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "schema"
)

#: Glob applied recursively under the load root.
_SCHEMA_GLOB = "**/*.schema.json"


class SchemaLoadError(Exception):
    """A schema file could not be loaded into the ``$id`` -> schema mapping.

    Raised for an unparseable file, a top-level JSON value that is not an
    object, a missing ``$id``, and a ``$id`` claimed by two files. Callers can
    catch this one type instead of ``json.JSONDecodeError``, ``TypeError``, and
    ``KeyError`` separately.
    """


def load_schemas(root: pathlib.Path | None = None) -> dict[str, dict]:
    """Return every schema under *root*, keyed by its ``$id``.

    Args:
        root: Directory to search. Defaults to :data:`SCHEMA_ROOT`. A missing
            or empty directory yields an empty mapping rather than an error,
            so an optional schema directory that a later phase has not created
            yet is not a failure.

    Returns:
        Mapping of ``$id`` string to the parsed schema object.

    Raises:
        SchemaLoadError: A file is unparseable, is not a JSON object at the top
            level, lacks ``$id``, or duplicates an ``$id`` already claimed.
    """
    search_root = SCHEMA_ROOT if root is None else root
    if not search_root.is_dir():
        return {}

    schemas: dict[str, dict] = {}
    origins: dict[str, pathlib.Path] = {}

    # sorted() makes load order -- and therefore which file of a duplicate pair
    # is reported as the original -- deterministic and reproducible.
    for path in sorted(search_root.glob(_SCHEMA_GLOB)):
        if not path.is_file():
            continue

        try:
            with path.open(encoding="utf-8") as handle:
                schema = json.load(handle)
        except json.JSONDecodeError as exc:
            raise SchemaLoadError(f"{path}: not parseable as JSON: {exc}") from exc
        except OSError as exc:
            raise SchemaLoadError(f"{path}: could not be read: {exc}") from exc

        # Guard before subscripting: a top-level array or scalar would make the
        # `"$id" not in schema` check below pass or throw misleadingly, and the
        # subsequent key access would surface a bare TypeError instead of this
        # module's declared error type.
        if not isinstance(schema, dict):
            raise SchemaLoadError(
                f"{path}: top-level JSON value must be an object, "
                f"found {type(schema).__name__}"
            )

        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise SchemaLoadError(f"{path}: missing a non-empty '$id' property")

        if schema_id in schemas:
            raise SchemaLoadError(
                f"duplicate $id {schema_id!r} declared by both "
                f"{origins[schema_id]} and {path}"
            )

        schemas[schema_id] = schema
        origins[schema_id] = path

    return schemas


def build_registry(schemas: dict[str, dict]) -> referencing.Registry:
    """Return a ``referencing`` registry covering every schema in *schemas*.

    Without this, a ``$ref`` naming another file by its ``$id`` is
    unresolvable, and a cross-file reference silently degrades: the referencing
    schema behaves as though the constraint were absent, or -- worse -- raises
    a ``referencing`` error that is neither :class:`SchemaLoadError` nor
    ``jsonschema.ValidationError``. Registering the whole mapping is what makes
    ``_hermes_auto`` in the request schema actually enforce the metadata
    envelope's rules rather than being a bare ``{"type": "object"}``.

    ``default_specification`` is supplied because a registered schema is not
    required to declare ``$schema``; the packaged ones do, and theirs wins.
    """
    return referencing.Registry().with_resources(
        (
            schema_id,
            referencing.Resource.from_contents(
                schema,
                default_specification=referencing.jsonschema.DRAFT202012,
            ),
        )
        for schema_id, schema in schemas.items()
    )


def validate(
    instance: object,
    schema_id: str,
    schemas: dict[str, dict] | None = None,
) -> None:
    """Validate *instance* against the schema registered under *schema_id*.

    Every schema in the mapping is registered as a resolvable resource first,
    so a ``$ref`` to another file's ``$id`` resolves. Which schemas the mapping
    contains therefore matters: validating a schema whose ``$ref`` targets a
    file the caller did not load raises
    ``jsonschema.exceptions._WrappedReferencingError`` (a subclass of
    ``jsonschema.exceptions._RefResolutionError``), not
    ``jsonschema.ValidationError``. Pass a mapping that covers the reference
    closure -- in practice a bare :func:`load_schemas` over the whole root.

    Args:
        instance: The parsed value to check.
        schema_id: The ``$id`` of the schema to check against.
        schemas: A mapping from :func:`load_schemas`. Loaded from
            :data:`SCHEMA_ROOT` when omitted. Pass an already-loaded mapping in
            hot paths to avoid re-reading every file.

    Raises:
        KeyError: *schema_id* is not in the mapping. The message lists the ids
            that are available.
        jsonschema.ValidationError: *instance* does not satisfy the schema.
            Propagated unchanged so callers keep the full error path.
        jsonschema.exceptions._RefResolutionError: The schema contains a
            ``$ref`` to an ``$id`` absent from *schemas*.
    """
    registry = load_schemas() if schemas is None else schemas

    if schema_id not in registry:
        raise KeyError(
            f"unknown schema id {schema_id!r}; available: {sorted(registry)}"
        )

    jsonschema.validate(
        instance=instance,
        schema=registry[schema_id],
        registry=build_registry(registry),
    )
