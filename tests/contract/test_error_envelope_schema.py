"""Contract tests for ``wire/openai-error.v1``, the OpenAI error envelope.

Phase 1 shipped schemas for the chat request, the chat response, and the SSE
stream, but not for the body a provider returns on a non-2xx. That body is a
different shape -- no ``id``, ``object``, ``created``, ``model``, or
``choices`` -- so ``openai-chat-response.v1`` does not describe it and error
bodies went unvalidated. ``design.md`` 20.3 lists context errors in the
contract-test set; this file closes that gap.

Two properties are asserted together and neither alone is sufficient:

* the schema *rejects* a body missing the parts the gateway relies on, and
* the schema *accepts* a body carrying provider fields it has never seen.

The second is the one that is easy to get wrong. This is a relay schema: the
gateway forwards the error body verbatim, so a closed object would reject
traffic it is required to pass through. Phase 1's review found exactly that
defect in ``contentPart`` -- the one closed object inside a relay schema.

Schemas are loaded from the whole ``SCHEMA_ROOT`` rather than from
``SCHEMA_ROOT / "wire"``. ``$ref`` resolution is registry-scoped, and a subset
mapping raises ``referencing.exceptions.Unresolvable`` -- which is not a
``ValidationError`` and would not be caught by a handler expecting one.
"""

from __future__ import annotations

import copy

import jsonschema
import pytest

from hermes_auto.gateway.schemas import (
    EXPECTED_SCHEMA_IDS,
    SCHEMA_ROOT,
    build_validator,
    load_schemas,
)

pytestmark = pytest.mark.contract

ERROR_SCHEMA_ID = "https://hermes-auto-router.dev/schema/wire/openai-error.v1.json"


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict]:
    """Every packaged schema, keyed by ``$id``."""
    loaded = load_schemas(SCHEMA_ROOT)
    assert ERROR_SCHEMA_ID in loaded, sorted(loaded)
    return loaded


@pytest.fixture(scope="module")
def validator(schemas: dict[str, dict]) -> jsonschema.Draft202012Validator:
    """A hoisted validator.

    ``validate()`` re-runs ``check_schema`` on every call, which dominates the
    cost; building once is the pattern the gateway request path must also use.
    """
    return build_validator(ERROR_SCHEMA_ID, schemas)


# ---------------------------------------------------------------------------
# Realistic bodies that must validate
# ---------------------------------------------------------------------------

#: A 400 from the case design.md 20.3 names explicitly. Reproduced in the shape
#: OpenAI returns it, including the null ``param``.
CONTEXT_LENGTH_EXCEEDED = {
    "error": {
        "message": (
            "This model's maximum context length is 128000 tokens. However, "
            "your messages resulted in 131194 tokens. Please reduce the length "
            "of the messages."
        ),
        "type": "invalid_request_error",
        "param": "messages",
        "code": "context_length_exceeded",
    }
}

#: A 401. ``param`` is null here because no single request parameter is at
#: fault, which is why the schema makes it nullable rather than a bare string.
INVALID_API_KEY = {
    "error": {
        "message": (
            "Incorrect API key provided: sk-fake***. You can find your API key "
            "at https://platform.openai.com/account/api-keys."
        ),
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_api_key",
    }
}

#: A 429. Neither optional field is present at all -- absence and explicit null
#: are different encodings of the same fact and both occur in the wild.
RATE_LIMITED = {
    "error": {
        "message": "Rate limit reached for gpt-4o in organization org-x.",
        "type": "rate_limit_error",
    }
}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(CONTEXT_LENGTH_EXCEEDED, id="400-context-length-exceeded"),
        pytest.param(INVALID_API_KEY, id="401-invalid-api-key"),
        pytest.param(RATE_LIMITED, id="429-rate-limit-optional-fields-absent"),
    ],
)
def test_realistic_error_bodies_validate(body, validator):
    """The three error classes the phase must relay all validate as written."""
    validator.validate(body)


def test_unknown_provider_fields_are_relayed_not_rejected(validator):
    """Relay behavior, asserted at both nesting levels.

    The gateway forwards the error body verbatim. A schema that rejected an
    unrecognized field would make a body the gateway successfully relays fail
    its own contract test, and -- under ``gateway.strict_validation`` -- would
    turn a provider adding a field into a gateway outage. Both the top level
    and the ``error`` object must stay open, so this asserts both; a schema
    that closed only the inner object would pass a top-level-only check.
    """
    body = copy.deepcopy(CONTEXT_LENGTH_EXCEEDED)
    body["request_id"] = "req_2f18c0aa"  # extra at the top level
    body["error"]["metadata"] = {  # extra inside `error`
        "provider_name": "some-relay",
        "raw": "upstream said no",
    }
    body["error"]["failed_generation"] = None

    validator.validate(body)


def test_relay_openness_is_declared_not_incidental(schemas):
    """``additionalProperties: true`` is written down at both levels.

    ``test_unknown_provider_fields_are_relayed_not_rejected`` would also pass
    if the keyword were simply omitted, because omission defaults to open. It
    is stated explicitly so that a later edit adding
    ``additionalProperties: false`` is a visible change to a line that already
    exists rather than an addition to a schema that was silent on the point.
    """
    schema = schemas[ERROR_SCHEMA_ID]
    assert schema["additionalProperties"] is True
    assert schema["$defs"]["errorDetail"]["additionalProperties"] is True


# ---------------------------------------------------------------------------
# Malformed bodies that must be rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="error-key-absent"),
        pytest.param(
            {"message": "flat, not wrapped in `error`"},
            id="flat-body-not-an-envelope",
        ),
        pytest.param(
            {"error": {"type": "invalid_request_error", "code": "x"}},
            id="error-lacks-message",
        ),
        pytest.param(
            {"error": {"message": "no type field"}},
            id="error-lacks-type",
        ),
        pytest.param({"error": "just a string"}, id="error-not-an-object"),
        pytest.param({"error": None}, id="error-null"),
        pytest.param(
            {"error": {"message": 500, "type": "invalid_request_error"}},
            id="message-not-a-string",
        ),
        pytest.param(
            {"error": {"message": "m", "type": ["invalid_request_error"]}},
            id="type-not-a-string",
        ),
        pytest.param(
            {"error": {"message": "m", "type": "t", "param": 7}},
            id="param-neither-string-nor-null",
        ),
        pytest.param(
            # An integer code is *accepted* -- see
            # test_an_integer_code_is_accepted_because_relays_emit_one below.
            # A list is not: the field was widened by one type, not opened.
            {"error": {"message": "m", "type": "t", "code": [429]}},
            id="code-neither-string-integer-nor-null",
        ),
        pytest.param([CONTEXT_LENGTH_EXCEEDED], id="top-level-array"),
    ],
)
def test_malformed_error_bodies_are_rejected(body, validator):
    """Openness to unknown fields is not looseness about the known ones."""
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(body)


def test_an_integer_code_is_accepted_because_relays_emit_one(validator):
    """``error.code`` carries an HTTP status on some OpenAI-compatible relays.

    This schema's own description warns that a closure here "would reject
    traffic the gateway is required to pass through", and typing ``code`` as
    ``["string", "null"]`` was such a closure: the gateway relays an
    integer-coded body byte-for-byte, so the only thing that broke was the
    contract test. Widening the schema was the fix; weakening
    ``assert_error_body`` would have given up the checks on ``message`` and
    ``type``, which are the fields that actually have to be there.
    """
    validator.validate({"error": {"message": "m", "type": "t", "code": 429}})
    # Still nullable and still string-accepting: this widened the type union
    # rather than replacing it.
    validator.validate({"error": {"message": "m", "type": "t", "code": "429"}})
    validator.validate({"error": {"message": "m", "type": "t", "code": None}})


def test_a_chat_completion_is_not_an_error_envelope(validator):
    """The two shapes are genuinely distinguishable.

    This is the whole reason the schema exists: a success body must not satisfy
    the error schema, or `openai-error.v1` would be an unconditional pass and
    validating against it would prove nothing.
    """
    chat_completion = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1770000000,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(chat_completion)


# ---------------------------------------------------------------------------
# Schema hygiene and the packaged set
# ---------------------------------------------------------------------------


def test_error_schema_is_meta_valid(schemas):
    """The schema is itself a valid 2020-12 schema."""
    jsonschema.Draft202012Validator.check_schema(schemas[ERROR_SCHEMA_ID])


def test_every_packaged_schema_is_still_meta_valid(schemas):
    """Adding the eighth schema did not disturb the other seven."""
    for schema_id, schema in schemas.items():
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$id"] == schema_id


def test_error_schema_declares_the_expected_id_and_dialect(schemas):
    schema = schemas[ERROR_SCHEMA_ID]
    assert schema["$id"] == ERROR_SCHEMA_ID
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["required"] == ["error"]


def test_packaged_set_is_now_eight_and_matches_expected_ids():
    """``EXPECTED_SCHEMA_IDS`` moved in the same change as the schema file.

    That constant is the single source of truth the CI identity check and the
    wheel-packaging gate both read. A schema added without updating it fails
    those gates rather than this test, which is why the assertion is an
    identity comparison and not merely a count.
    """
    loaded = set(load_schemas())
    assert loaded == set(EXPECTED_SCHEMA_IDS), sorted(loaded ^ set(EXPECTED_SCHEMA_IDS))
    assert len(EXPECTED_SCHEMA_IDS) == 8
    assert ERROR_SCHEMA_ID in EXPECTED_SCHEMA_IDS


def test_error_schema_ships_in_the_package_data_glob(repo_root):
    """The new file sits where ``package-data`` already collects schemas.

    ``[tool.setuptools.package-data]`` globs fail *silently*: a path the glob
    misses still loads from an editable install and from ``PYTHONPATH=src``,
    so every in-repo gate stays green while the built wheel ships nothing. The
    file is placed under the existing ``data/schema/**/*.json`` glob rather
    than needing a new entry, and this pins that placement.
    """
    packaged = (
        repo_root
        / "src"
        / "hermes_auto"
        / "data"
        / "schema"
        / "wire"
        / "openai-error.v1.schema.json"
    )
    assert packaged.is_file()


def test_description_states_bounds_without_claiming_containment(schemas):
    """House rule from Phase 1's review, applied to the new schema.

    A description must not assert that a value constraint establishes something
    only the write path can. The recurring defect was phrasing of the form "X
    cannot be written here"; a bounded field still holds whatever a writer puts
    in it. The banned list is reproduced from
    ``test_routing_schemas.py::_OVERCLAIMS``, whose own parametrization covers
    the three routing schemas only, so a wire schema is not otherwise checked.
    """
    overclaims = (
        "cannot round-trip",
        "cannot hold",
        "cannot be written here",
        "can never be written",
        "has room to carry",
        "structural guarantee",
        "impossible by design",
        "becomes structural rather than advisory",
        "never carries prompt text",
        "never contains a credential",
        "under any name, spelling, or value",
    )

    def walk(node, path="$"):
        if isinstance(node, dict):
            description = node.get("description")
            if isinstance(description, str):
                lowered = description.lower()
                for claim in overclaims:
                    assert claim not in lowered, f"{path}: {claim!r}"
            for name, child in node.items():
                walk(child, f"{path}.{name}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    schema = schemas[ERROR_SCHEMA_ID]
    walk(schema)
    # Removing a false claim is only half the fix: the description must also say
    # what it does bound, and name relaying as the reason it stays open.
    lowered = schema["description"].lower()
    assert "relay" in lowered
    assert "shape" in lowered
