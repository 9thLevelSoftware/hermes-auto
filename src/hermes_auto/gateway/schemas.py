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
import referencing.exceptions
import referencing.jsonschema

__all__ = [
    "EXPECTED_SCHEMA_IDS",
    "SCHEMA_ROOT",
    "SchemaLoadError",
    "build_registry",
    "build_validator",
    "load_schemas",
    "validate",
]

#: Root of the packaged schema tree. Every ``*.schema.json`` beneath it, at any
#: depth, is discoverable by :func:`load_schemas`.
SCHEMA_ROOT: pathlib.Path = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "schema"
)

#: Every ``$id`` the packaged tree is expected to contain.
#:
#: This is an *identity* gate, not a count. A count check ("there are seven
#: schemas") passes when a stray eighth schema is added at the same time a
#: required one is deleted or renamed; comparing the set catches both halves of
#: that swap. Asserted against ``set(load_schemas())`` in the contract tests, so
#: adding or renaming a packaged schema is a deliberate edit here rather than a
#: silent change in what the gateway validates against.
EXPECTED_SCHEMA_IDS: frozenset[str] = frozenset(
    {
        "https://hermes-auto-router.dev/schema/routing/hermes-auto-metadata.v1.json",
        "https://hermes-auto-router.dev/schema/routing/model-card.v1.json",
        "https://hermes-auto-router.dev/schema/routing/outcome-event.v1.json",
        "https://hermes-auto-router.dev/schema/routing/route-decision.v1.json",
        "https://hermes-auto-router.dev/schema/wire/openai-chat-request.v1.json",
        "https://hermes-auto-router.dev/schema/wire/openai-chat-response.v1.json",
        "https://hermes-auto-router.dev/schema/wire/sse-stream-contract.v1.json",
    }
)

#: Glob applied recursively under the load root.
_SCHEMA_GLOB = "**/*.schema.json"


class SchemaLoadError(Exception):
    """A schema mapping could not be assembled or is not self-contained.

    Raised for an unparseable file, a top-level JSON value that is not an
    object, a missing ``$id``, a ``$id`` claimed by two files, and a ``$ref``
    naming an ``$id`` the supplied mapping does not contain. Callers can catch
    this one type instead of ``json.JSONDecodeError``, ``TypeError``,
    ``KeyError``, and ``referencing.exceptions.Unresolvable`` separately.
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
    unresolvable and the reference fails at validation time with
    ``referencing.exceptions.Unresolvable`` -- which is not a
    ``jsonschema.ValidationError``, so a caller catching only that would let it
    escape. :func:`build_validator` converts it to :class:`SchemaLoadError`.
    Registering the whole mapping is what makes ``_hermes_auto`` in the request
    schema actually enforce the metadata envelope's rules rather than being a
    bare ``{"type": "object"}``.

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


def _external_refs(node: object):
    """Yield every non-local ``$ref`` string reachable from *node*.

    Local refs (``#/$defs/...``) resolve against the containing schema and need
    no registry entry, so they are skipped.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            yield ref
        for child in node.values():
            yield from _external_refs(child)
    elif isinstance(node, list):
        for child in node:
            yield from _external_refs(child)


def build_validator(
    schema_id: str,
    schemas: dict[str, dict] | None = None,
) -> jsonschema.Draft202012Validator:
    """Return a reusable validator for *schema_id* with the registry attached.

    Prefer this over :func:`validate` in any loop. ``jsonschema.validate`` runs
    ``check_schema`` on every call, which dominates the cost by roughly two
    orders of magnitude; a validator built once and reused amortizes that to
    nothing. Reading the schema files is *not* the expensive part -- building
    the registry is microseconds.

    The reference closure is checked eagerly here rather than on first use. That
    is the point of doing it at build time: a ``$ref`` resolves lazily during
    validation, and only when an instance actually reaches the referencing
    keyword. A gateway that scope-loaded ``SCHEMA_ROOT / "wire"`` would
    therefore validate every request that omits ``_hermes_auto`` cleanly -- as
    all plain fixtures do -- and fail only on requests that carry it, which in
    production is all of them. Resolving up front turns that into a startup
    error with a useful message.

    Args:
        schema_id: The ``$id`` of the schema to validate against.
        schemas: A mapping from :func:`load_schemas`. Loaded from
            :data:`SCHEMA_ROOT` when omitted.

    Returns:
        A ``Draft202012Validator`` whose ``validate`` and ``iter_errors`` may be
        called repeatedly.

    Raises:
        KeyError: *schema_id* is not in the mapping. The message lists the ids
            that are available.
        SchemaLoadError: The schema's reference closure is not covered by
            *schemas*. The message names the missing ``$id`` and the ids that
            were supplied.
        jsonschema.SchemaError: The schema itself is not a valid 2020-12 schema.
    """
    registry_source = load_schemas() if schemas is None else schemas

    if schema_id not in registry_source:
        raise KeyError(
            f"unknown schema id {schema_id!r}; available: {sorted(registry_source)}"
        )

    schema = registry_source[schema_id]
    registry = build_registry(registry_source)

    resolver = registry.resolver()
    for ref in _external_refs(schema):
        try:
            resolver.lookup(ref)
        except referencing.exceptions.Unresolvable as exc:
            raise SchemaLoadError(
                f"{schema_id}: $ref {ref!r} is not resolvable from the supplied "
                f"schema mapping; supplied: {sorted(registry_source)}. Pass a "
                f"mapping covering the reference closure -- in practice a bare "
                f"load_schemas() over the whole schema root."
            ) from exc

    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, registry=registry)


def validate(
    instance: object,
    schema_id: str,
    schemas: dict[str, dict] | None = None,
) -> None:
    """Validate *instance* against the schema registered under *schema_id*.

    A convenience wrapper over :func:`build_validator` for one-shot checks. It
    rebuilds the validator on every call, and that rebuild -- specifically the
    ``check_schema`` pass inside it -- is roughly two orders of magnitude more
    expensive than the validation itself. Anything validating more than a
    handful of instances should hoist :func:`build_validator` out of the loop
    and call ``.validate()`` on the result.

    Args:
        instance: The parsed value to check.
        schema_id: The ``$id`` of the schema to check against.
        schemas: A mapping from :func:`load_schemas`. Loaded from
            :data:`SCHEMA_ROOT` when omitted.

    Raises:
        KeyError: *schema_id* is not in the mapping.
        SchemaLoadError: The schema's reference closure is not covered by
            *schemas*. Raised eagerly, so it does not depend on whether this
            particular *instance* happens to reach the referencing keyword.
        jsonschema.ValidationError: *instance* does not satisfy the schema.
            Propagated unchanged so callers keep the full error path.
    """
    build_validator(schema_id, schemas).validate(instance)
