from __future__ import annotations

from starlette.testclient import TestClient

from hermes_auto.config import AutoRouterConfig, CandidateConfig, GatewayConfig
from hermes_auto.gateway.admin import create_admin_app, read_admin_token
from hermes_auto.routing import DecisionRouter


def candidate(candidate_id: str, tier: str) -> CandidateConfig:
    return CandidateConfig(
        id=candidate_id,
        provider="test",
        model=f"real-{candidate_id}",
        base_url="https://provider.example/v1",
        credential_ref="none",
        tier=tier,
        context_window=128_000,
        supports_tools=True,
        supports_vision=True,
    )


def configured(tmp_path) -> AutoRouterConfig:
    return AutoRouterConfig(
        gateway=GatewayConfig(state_dir=tmp_path),
        candidates=(candidate("fast", "fast"), candidate("strong", "strong")),
    )


def authorization(config: AutoRouterConfig) -> dict[str, str]:
    token = read_admin_token(config.gateway.state_dir)
    assert token
    return {"authorization": f"Bearer {token}"}


def test_decision_endpoint_explains_without_prompt_or_session_content(tmp_path) -> None:
    config = configured(tmp_path)
    router = DecisionRouter(config.candidates)
    router.route(
        {
            "messages": [
                {"role": "user", "content": "TOP SECRET PROMPT " + "x" * 32_000}
            ]
        },
        root_session_id="secret-session-id",
        virtual_model="auto",
    )
    app = create_admin_app(config, decision_router=router)

    with TestClient(app) as client:
        response = client.get(
            "/admin/v1/decisions/secret-session-id",
            headers=authorization(config),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_candidate"] == "strong"
    assert payload["selected_tier"] == "strong"
    assert payload["detected_tier"] == "balanced"
    assert payload["pinned"] is None
    serialized = response.text
    assert "TOP SECRET PROMPT" not in serialized
    assert "secret-session-id" not in serialized


def test_latest_decision_and_status_summary_are_available(tmp_path) -> None:
    config = configured(tmp_path)
    router = DecisionRouter(config.candidates)
    router.route(
        {"messages": [{"role": "user", "content": "short"}]},
        root_session_id="session",
        virtual_model="auto",
    )
    app = create_admin_app(config, decision_router=router)

    with TestClient(app) as client:
        latest = client.get(
            "/admin/v1/decisions/latest",
            headers=authorization(config),
        )
        status = client.get(
            "/admin/v1/status",
            headers=authorization(config),
        )

    assert latest.status_code == 200
    assert latest.json()["selected_candidate"] == "fast"
    assert status.status_code == 200
    assert status.json()["configured_candidate_count"] == 2
    assert status.json()["most_recent_decision"]["selected_candidate"] == "fast"


def test_unknown_session_decision_is_an_openai_shaped_404(tmp_path) -> None:
    config = configured(tmp_path)
    app = create_admin_app(config, decision_router=DecisionRouter(config.candidates))

    with TestClient(app) as client:
        response = client.get(
            "/admin/v1/decisions/missing",
            headers=authorization(config),
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "decision_not_found"
