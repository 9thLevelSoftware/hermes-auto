"""Contract tests for the wire-protocol schemas in ``data/schema/wire``.

These prove the schemas actually constrain the OpenAI Chat Completions subset
the gateway speaks: valid fixtures pass, an invalid request fails, and the
awkward cases design.md 20.3 calls out -- parallel tool calls, fragmented
tool-call arguments, empty deltas, usage in final stream chunks, images -- all
validate.

Schemas are loaded from ``SCHEMA_ROOT / "wire"`` rather than the whole schema
root on purpose: other schema directories belong to other plans, and a bare
``load_schemas()`` here would make this module's result depend on their state.
"""

from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

from hermes_auto.gateway.schemas import SCHEMA_ROOT, load_schemas, validate

pytestmark = pytest.mark.contract

REQUEST_SCHEMA_ID = "https://hermes-auto-router.dev/schema/wire/openai-chat-request.v1.json"
RESPONSE_SCHEMA_ID = "https://hermes-auto-router.dev/schema/wire/openai-chat-response.v1.json"
STREAM_SCHEMA_ID = "https://hermes-auto-router.dev/schema/wire/sse-stream-contract.v1.json"


@pytest.fixture(scope="module")
def wire_schemas() -> dict[str, dict]:
    """The three wire schemas, keyed by ``$id``."""
    return load_schemas(SCHEMA_ROOT / "wire")


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
