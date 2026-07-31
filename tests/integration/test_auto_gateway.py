from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from hermes_auto.config import AutoRouterConfig, CandidateConfig, GatewayConfig
from hermes_auto.gateway.auth import read_token
from hermes_auto.gateway.errors import GatewayError
from hermes_auto.gateway.app import create_app
from hermes_auto.provider import build_envelope


def candidate(candidate_id: str, tier: str) -> CandidateConfig:
    return CandidateConfig(
        id=candidate_id,
        provider="test",
        model=f"real-{candidate_id}",
        base_url=f"https://{candidate_id}.example/v1",
        credential_ref="none",
        tier=tier,
        context_window=128_000,
        supports_tools=True,
        supports_vision=True,
    )


class FakePool:
    def __init__(self) -> None:
        self.complete_actions: dict[str, Any] = {}
        self.stream_actions: dict[str, Any] = {}
        self.complete_calls: list[tuple[str, dict[str, Any]]] = []
        self.stream_calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def complete(self, selected, body, headers):
        payload = json.loads(body)
        self.complete_calls.append((selected.id, payload))
        action = self.complete_actions[selected.id]
        if isinstance(action, Exception):
            raise action
        status, content = action
        return status, httpx.Headers({"x-candidate": selected.id}), content

    async def stream(self, selected, body, headers):
        payload = json.loads(body)
        self.stream_calls.append((selected.id, payload))
        action = self.stream_actions[selected.id]
        if isinstance(action, Exception):
            raise action
        status, chunks = action

        async def iterator() -> AsyncIterator[bytes]:
            for chunk in chunks:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk

        return status, httpx.Headers({"x-candidate": selected.id}), iterator()

    async def aclose(self) -> None:
        self.closed = True


def config(tmp_path) -> AutoRouterConfig:
    return AutoRouterConfig(
        gateway=GatewayConfig(state_dir=tmp_path),
        candidates=(candidate("fast", "fast"), candidate("strong", "strong")),
    )


def headers(configured: AutoRouterConfig) -> dict[str, str]:
    token = read_token(configured.gateway.state_dir)
    assert token
    return {"authorization": f"Bearer {token}"}


def request_body(*, stream: bool = False) -> dict[str, Any]:
    return {
        "model": "auto",
        "messages": [{"role": "user", "content": "short"}],
        "stream": stream,
        "opaque_future_field": {"keep": [1, 2, 3]},
        "_hermes_auto": build_envelope(
            session_id="root-session",
            virtual_model="auto",
        ),
    }


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_non_streaming_falls_back_for_retryable_statuses(
    tmp_path, status: int
) -> None:
    configured = config(tmp_path)
    pool = FakePool()
    pool.complete_actions = {
        "fast": (status, b'{"error":{"message":"first failed"}}'),
        "strong": (200, b'{"id":"from-strong"}'),
    }
    app = create_app(configured, client_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(configured),
            json=request_body(),
        )

    assert response.status_code == 200
    assert response.json() == {"id": "from-strong"}
    assert response.headers["x-candidate"] == "strong"
    assert [item[0] for item in pool.complete_calls] == ["fast", "strong"]


def test_non_streaming_falls_back_for_transport_failure(tmp_path) -> None:
    configured = config(tmp_path)
    pool = FakePool()
    pool.complete_actions = {
        "fast": GatewayError(
            502, "unreachable", "api_error", code="upstream_unavailable"
        ),
        "strong": (200, b'{"id":"from-strong"}'),
    }
    app = create_app(configured, client_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(configured),
            json=request_body(),
        )

    assert response.status_code == 200
    assert [item[0] for item in pool.complete_calls] == ["fast", "strong"]


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_non_streaming_does_not_fallback_for_validation_statuses(
    tmp_path, status: int
) -> None:
    configured = config(tmp_path)
    pool = FakePool()
    body = b'{"error":{"message":"invalid request"}}'
    pool.complete_actions = {
        "fast": (status, body),
        "strong": (200, b'{"id":"must-not-run"}'),
    }
    app = create_app(configured, client_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(configured),
            json=request_body(),
        )

    assert response.status_code == status
    assert response.content == body
    assert [item[0] for item in pool.complete_calls] == ["fast"]


def test_only_model_and_private_metadata_change_upstream(tmp_path) -> None:
    configured = config(tmp_path)
    pool = FakePool()
    pool.complete_actions = {"fast": (200, b'{"id":"ok"}')}
    app = create_app(configured, client_pool=pool)
    original = request_body()

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(configured),
            json=original,
        )

    assert response.status_code == 200
    forwarded = pool.complete_calls[0][1]
    expected = dict(original)
    expected.pop("_hermes_auto")
    expected["model"] = "real-fast"
    assert forwarded == expected


def test_all_failures_return_sanitized_candidate_reasons(tmp_path) -> None:
    configured = config(tmp_path)
    pool = FakePool()
    pool.complete_actions = {
        "fast": (401, b"credential body must not be relayed"),
        "strong": (503, b"provider body must not be relayed"),
    }
    app = create_app(configured, client_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(configured),
            json=request_body(),
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "all_candidates_failed"
    text = response.text
    assert "fast" in text and "http_401" in text
    assert "strong" in text and "http_503" in text
    assert "example/v1" not in text
    assert "credential body" not in text


def test_stream_falls_back_when_failure_occurs_before_first_byte(tmp_path) -> None:
    configured = config(tmp_path)
    pool = FakePool()
    pool.stream_actions = {
        "fast": (
            200,
            [
                GatewayError(
                    502,
                    "unreachable",
                    "api_error",
                    code="upstream_unavailable",
                )
            ],
        ),
        "strong": (200, [b"data: strong\n\n", b"data: [DONE]\n\n"]),
    }
    app = create_app(configured, client_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(configured),
            json=request_body(stream=True),
        )

    assert response.status_code == 200
    assert response.content == b"data: strong\n\ndata: [DONE]\n\n"
    assert [item[0] for item in pool.stream_calls] == ["fast", "strong"]


def test_stream_falls_back_for_retryable_status_before_body(tmp_path) -> None:
    configured = config(tmp_path)
    pool = FakePool()
    pool.stream_actions = {
        "fast": (429, [b"data: rate-limit-body\n\n"]),
        "strong": (200, [b"data: strong\n\n"]),
    }
    app = create_app(configured, client_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(configured),
            json=request_body(stream=True),
        )

    assert response.status_code == 200
    assert response.content == b"data: strong\n\n"
    assert [item[0] for item in pool.stream_calls] == ["fast", "strong"]


def test_stream_never_falls_back_after_first_byte(tmp_path) -> None:
    configured = config(tmp_path)
    pool = FakePool()
    pool.stream_actions = {
        "fast": (
            200,
            [
                b"data: committed\n\n",
                GatewayError(
                    502,
                    "midstream",
                    "api_error",
                    code="upstream_unavailable",
                ),
            ],
        ),
        "strong": (200, [b"data: must-not-run\n\n"]),
    }
    app = create_app(configured, client_pool=pool)

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/v1/chat/completions",
            headers=headers(configured),
            json=request_body(stream=True),
        )

    assert [item[0] for item in pool.stream_calls] == ["fast"]
