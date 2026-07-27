"""Contract tests for the wire-protocol schemas in ``data/schema/wire``.

These prove the schemas actually constrain the OpenAI Chat Completions subset
the gateway speaks: valid fixtures pass, an invalid request fails, and the
awkward cases design.md 20.3 calls out -- parallel tool calls, fragmented
tool-call arguments, empty deltas, usage in final stream chunks, images -- all
validate.

Schemas are loaded from the whole ``SCHEMA_ROOT``, not from ``SCHEMA_ROOT /
"wire"``. That is required, not incidental: the request schema's
``_hermes_auto`` property is a ``$ref`` to the routing metadata envelope's
``$id``, and a wire-only mapping cannot resolve it. Scoping the load would make
every request test raise an unresolvable-reference error instead of validating.
"""

from __future__ import annotations

import copy
import json
import pathlib

import jsonschema
import pytest

from hermes_auto.gateway.schemas import SCHEMA_ROOT, load_schemas, validate

pytestmark = pytest.mark.contract

REQUEST_SCHEMA_ID = "https://hermes-auto-router.dev/schema/wire/openai-chat-request.v1.json"
RESPONSE_SCHEMA_ID = "https://hermes-auto-router.dev/schema/wire/openai-chat-response.v1.json"
STREAM_SCHEMA_ID = "https://hermes-auto-router.dev/schema/wire/sse-stream-contract.v1.json"


METADATA_SCHEMA_ID = (
    "https://hermes-auto-router.dev/schema/routing/hermes-auto-metadata.v1.json"
)


@pytest.fixture(scope="module")
def wire_schemas() -> dict[str, dict]:
    """Every packaged schema, keyed by ``$id``.

    Deliberately the full root: ``openai-chat-request`` ``$ref``s the routing
    metadata envelope across directories, so the mapping handed to
    :func:`validate` must cover the reference closure.
    """
    schemas = load_schemas(SCHEMA_ROOT)
    assert {REQUEST_SCHEMA_ID, RESPONSE_SCHEMA_ID, STREAM_SCHEMA_ID} <= set(schemas)
    assert METADATA_SCHEMA_ID in schemas
    return schemas


@pytest.fixture(scope="module")
def fixture_dir(repo_root: pathlib.Path) -> pathlib.Path:
    """Directory holding the synthetic wire fixtures."""
    return repo_root / "tests" / "fixtures" / "wire"


def _load(fixture_dir: pathlib.Path, name: str) -> object:
    with (fixture_dir / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def test_valid_request_passes(fixture_dir, wire_schemas):
    instance = _load(fixture_dir, "valid-chat-request.json")
    validate(instance, REQUEST_SCHEMA_ID, wire_schemas)


def test_valid_request_with_tools_passes(fixture_dir, wire_schemas):
    """The hard cases from design.md 20.3 in one request.

    Parallel tool calls, role=tool results carrying tool_call_id, multipart
    content with an image part, streaming flags, and an opaque ``_hermes_auto``
    envelope must all validate together.
    """
    instance = _load(fixture_dir, "valid-chat-request-tools.json")
    validate(instance, REQUEST_SCHEMA_ID, wire_schemas)

    # Guard the fixture itself: if it stops exercising these cases the test
    # above keeps passing while proving nothing.
    assistant = [m for m in instance["messages"] if m["role"] == "assistant"]
    assert len(assistant[0]["tool_calls"]) == 2, "fixture must cover parallel tool calls"
    tool_msgs = [m for m in instance["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert all("tool_call_id" in m for m in tool_msgs)
    parts = [m for m in instance["messages"] if isinstance(m.get("content"), list)]
    assert any(
        p["type"] == "image_url" for m in parts for p in m["content"]
    ), "fixture must cover multipart image content"
    assert instance["stream"] is True
    assert instance["stream_options"]["include_usage"] is True
    assert "_hermes_auto" in instance
    # design.md 20.3 lists structured outputs as covered. Without a fixture
    # carrying a real json_schema payload that row was covered on paper only.
    assert instance["response_format"]["type"] == "json_schema"
    assert instance["response_format"]["json_schema"]["schema"]["type"] == "object"


def test_json_schema_response_format_requires_its_payload(fixture_dir, wire_schemas):
    """``{"type": "json_schema"}`` alone is not a relayable request.

    The provider has nothing to constrain generation with, so the gateway must
    reject it here rather than forward an incoherent body upstream.
    """
    instance = copy.deepcopy(_load(fixture_dir, "valid-chat-request-tools.json"))
    instance["response_format"] = {"type": "json_schema"}

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, REQUEST_SCHEMA_ID, wire_schemas)

    # The other two response-format types carry no payload and stay valid.
    for kind in ("text", "json_object"):
        instance["response_format"] = {"type": kind}
        validate(instance, REQUEST_SCHEMA_ID, wire_schemas)


def test_unknown_content_part_types_relay(fixture_dir, wire_schemas):
    """A relay schema must forward content parts it does not model.

    The previous closed ``oneOf`` rejected ``cache_control`` on a text part,
    ``mime_type`` on an image part, and the ``input_audio`` and ``file`` part
    types outright -- while the schema description simultaneously claimed the
    ``audio`` modality was accepted, which is only expressible as an
    ``input_audio`` part. Phase 2 built against that would 400 requests the
    upstream provider accepts.
    """
    instance = _load(fixture_dir, "valid-chat-request-unknown-content-part.json")
    validate(instance, REQUEST_SCHEMA_ID, wire_schemas)

    kinds = [part["type"] for part in instance["messages"][0]["content"]]
    assert {"input_audio", "file", "a_part_type_that_does_not_exist_yet"} <= set(kinds)
    text_part = instance["messages"][0]["content"][0]
    assert "cache_control" in text_part, "fixture must carry a provider extension key"


@pytest.mark.parametrize(
    "part",
    [
        pytest.param({"type": "text"}, id="text-missing-text"),
        pytest.param({"type": "text", "text": 42}, id="text-wrong-type"),
        pytest.param({"type": "image_url"}, id="image-missing-image_url"),
        pytest.param(
            {"type": "image_url", "image_url": {"detail": "low"}}, id="image-missing-url"
        ),
        pytest.param(
            {"type": "image_url", "image_url": {"url": "https://example.invalid/x.png", "detail": "ultra"}},
            id="image-bad-detail",
        ),
        pytest.param({"text": "no discriminator"}, id="part-missing-type"),
        pytest.param({"type": 7, "text": "x"}, id="type-not-a-string"),
    ],
)
def test_known_content_part_types_stay_strictly_typed(part, wire_schemas):
    """Opening the def to unknown part types must not loosen the known ones.

    ``text`` and ``image_url`` are the two the gateway itself reasons about, so
    they remain required-and-typed via ``if``/``then`` dispatch on ``type``.
    """
    instance = {
        "model": "test-model-c",
        "messages": [{"role": "user", "content": [part]}],
    }

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, REQUEST_SCHEMA_ID, wire_schemas)


def test_hermes_auto_envelope_is_validated_through_its_own_schema(
    fixture_dir, wire_schemas
):
    """``_hermes_auto`` ``$ref``s the envelope schema, so its rules apply here.

    Typed as a bare ``{"type": "object"}`` the request schema accepted
    ``{"protocol_version": 99, "junk": "x"}``, contradicting the envelope
    schema's own statement that a non-1 ``protocol_version`` MUST be rejected.
    Resolving that ``$ref`` is what closes the gap.
    """
    instance = copy.deepcopy(_load(fixture_dir, "valid-chat-request-tools.json"))
    instance["_hermes_auto"] = {"protocol_version": 99, "junk": "x"}

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, REQUEST_SCHEMA_ID, wire_schemas)

    # A well-formed envelope still passes, so the $ref is resolving rather
    # than failing everything.
    instance["_hermes_auto"] = {
        "protocol_version": 1,
        "root_session_id": "synthetic-session-0000",
        "virtual_model": "auto:balanced",
        "plugin_version": "0.1.0",
    }
    validate(instance, REQUEST_SCHEMA_ID, wire_schemas)


def test_invalid_request_missing_model_fails(fixture_dir, wire_schemas):
    """A request without ``model`` must be rejected.

    This is the test that proves the request schema constrains rather than
    accepting everything, despite ``additionalProperties: true``.
    """
    instance = _load(fixture_dir, "invalid-chat-request-missing-model.json")
    with pytest.raises(jsonschema.ValidationError):
        validate(instance, REQUEST_SCHEMA_ID, wire_schemas)


def test_valid_response_passes(fixture_dir, wire_schemas):
    instance = _load(fixture_dir, "valid-chat-response.json")
    validate(instance, RESPONSE_SCHEMA_ID, wire_schemas)


def test_refusal_response_passes(fixture_dir, wire_schemas):
    """design.md 20.3 lists refusals as covered; this is the fixture that covers it.

    Every other response fixture carries ``refusal: null``, so the non-null
    branch -- and the ``content_filter`` finish reason that accompanies it --
    was previously unexercised.
    """
    instance = _load(fixture_dir, "valid-chat-response-refusal.json")
    validate(instance, RESPONSE_SCHEMA_ID, wire_schemas)

    choice = instance["choices"][0]
    assert isinstance(choice["message"]["refusal"], str)
    assert choice["message"]["refusal"]
    assert choice["message"]["content"] is None
    assert choice["finish_reason"] == "content_filter"


def test_response_usage_carries_cache_and_reasoning_detail(fixture_dir, wire_schemas):
    """design.md 7.5 reconciles predicted cost against actual cached-token usage.

    Both the fixture and the schema must keep carrying that detail, so a later
    change that drops either is caught here.
    """
    usage = _load(fixture_dir, "valid-chat-response.json")["usage"]
    assert "cached_tokens" in usage["prompt_tokens_details"]
    assert "reasoning_tokens" in usage["completion_tokens_details"]

    usage_schema = wire_schemas[RESPONSE_SCHEMA_ID]["$defs"]["usage"]["properties"]
    assert "cached_tokens" in usage_schema["prompt_tokens_details"]["properties"]
    assert "reasoning_tokens" in usage_schema["completion_tokens_details"]["properties"]


def test_every_stream_chunk_validates(fixture_dir, wire_schemas):
    chunks = _load(fixture_dir, "valid-stream-chunks.json")
    assert isinstance(chunks, list) and chunks
    for position, chunk in enumerate(chunks):
        validate(chunk, STREAM_SCHEMA_ID, wire_schemas)
        assert chunk["object"] == "chat.completion.chunk", position


def test_final_stream_chunk_carries_usage(fixture_dir, wire_schemas):
    """design.md 20.3: usage arrives on the final chunk when include_usage was set."""
    chunks = _load(fixture_dir, "valid-stream-chunks.json")
    assert "usage" in chunks[-1]
    assert not any("usage" in c for c in chunks[:-1])
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_empty_delta_chunk_is_valid(fixture_dir, wire_schemas):
    """design.md 8.2: an empty delta is valid and must not be treated as an error."""
    chunks = _load(fixture_dir, "valid-stream-chunks.json")
    empty = [c for c in chunks if c["choices"] and c["choices"][0]["delta"] == {}]
    assert empty, "fixture must contain at least one empty-delta chunk"
    for chunk in empty:
        validate(chunk, STREAM_SCHEMA_ID, wire_schemas)


def test_fragmented_tool_arguments_concatenate_to_valid_json(fixture_dir, wire_schemas):
    """design.md 20.3: fragmented tool-call arguments parse only once reassembled.

    ``function.arguments`` is a JSON-encoded string, so fragments sharing a
    ``tool_calls[].index`` are valid JSON only after concatenation in arrival
    order. Each fragment must still validate on its own.
    """
    chunks = _load(fixture_dir, "valid-stream-chunks.json")

    fragments: dict[int, list[str]] = {}
    for chunk in chunks:
        validate(chunk, STREAM_SCHEMA_ID, wire_schemas)
        for choice in chunk["choices"]:
            for call in choice["delta"].get("tool_calls", []):
                piece = call.get("function", {}).get("arguments")
                if piece is not None:
                    fragments.setdefault(call["index"], []).append(piece)

    assert fragments, "fixture must contain fragmented tool-call arguments"
    for index, pieces in fragments.items():
        assert len(pieces) >= 2, f"index {index} must arrive in multiple fragments"
        assert json.loads("".join(pieces)) == {"region": "synthetic-region-1"}

        # A mid-stream fragment on its own is not parseable; that is the whole
        # reason assembly by index is required.
        with pytest.raises(json.JSONDecodeError):
            json.loads(pieces[1])
