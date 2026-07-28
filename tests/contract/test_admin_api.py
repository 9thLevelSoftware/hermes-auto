"""The admin API's trust boundary, asserted rather than asserted-about.

The load-bearing test in this file is
:func:`test_inference_token_is_rejected_by_every_admin_endpoint`. Everything else
supports it. `docs/threat-model.md` records that **no `design.md` §21 row covers
admin-scope privilege separation** and assigns the mitigation to Phase 2:
`/admin/v1/sessions/{id}/reroute` and `/pin` redirect every subsequent turn to a
caller-chosen candidate, which is an unauthorized-routing and cost-abuse
primitive that bypasses the policy layer entirely. If an inference-scope token
reaches those endpoints, the separation is decorative.

Three levels of evidence, deliberately, because each catches what the others
cannot:

1. **``TestClient``** for the response contract -- statuses, bodies, envelope
   shapes, the absence of CORS headers. Fast, and it exercises the middleware
   stack exactly as a real server would.
2. **Two real ``uvicorn`` servers on one event loop**, mirroring ``main.py``'s
   structure, for the graceful drain. ``POST /admin/v1/shutdown`` has to reach a
   server object that no ASGI API exposes, and a test that stubbed that out would
   pass against a handler that reached nothing.
3. **A real subprocess running ``python -m hermes_auto.gateway.main``** for the
   wiring. Constructing the admin app in isolation proves nothing about whether
   the port in the runtime file names a listener that is accepting connections --
   and that is the specific defect this plan exists to close: before it, the
   supervision contract's primary stop path POSTed to a port nothing was bound
   to and silently fell through to a hard kill on the one platform with no
   ``SIGTERM``.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from hermes_auto.config import AutoRouterConfig, GatewayConfig, UpstreamConfig
from hermes_auto.gateway.admin import (
    ADMIN_TOKEN_FILENAME,
    NOT_IMPLEMENTED_TYPE,
    AdminAuthMiddleware,
    admin_token_path,
    admin_token_permissions_ok,
    create_admin_app,
    mint_admin_token,
    read_admin_token,
    require_admin,
)
from hermes_auto.gateway.app import create_app
from hermes_auto.gateway.auth import mint_token, read_token
from hermes_auto.gateway.errors import assert_error_body

# Windows allocates a fresh console window for a console-subsystem child when the
# parent has no console of its own. CREATE_NO_WINDOW suppresses it; it is absent on
# POSIX, hence getattr.
_NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

pytestmark = pytest.mark.contract

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Every route this app serves, with a concrete path and its method. Written out
#: rather than derived so that a route silently disappearing from the app is a
#: test failure rather than a shorter loop.
ADMIN_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET", "/admin/v1/status"),
    ("POST", "/admin/v1/shutdown"),
    ("GET", "/admin/v1/decisions/dec_123"),
    ("POST", "/admin/v1/sessions/sess_123/reroute"),
    ("POST", "/admin/v1/sessions/sess_123/pin"),
    ("POST", "/admin/v1/feedback"),
)

#: The four §5.3 endpoints that describe routing this phase does not have.
UNIMPLEMENTED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET", "/admin/v1/decisions/dec_123"),
    ("POST", "/admin/v1/sessions/sess_123/reroute"),
    ("POST", "/admin/v1/sessions/sess_123/pin"),
    ("POST", "/admin/v1/feedback"),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def state_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """An isolated state directory.

    ``HERMES_AUTO_STATE_DIR`` outranks configuration by design, so setting it is
    what makes a test run structurally incapable of minting a token into the
    developer's real install.
    """
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(directory))
    return directory


@pytest.fixture()
def client(state_dir: pathlib.Path) -> Any:
    """The admin app with its lifespan run, so the admin token has been minted."""
    with TestClient(create_admin_app()) as test_client:
        yield test_client


@pytest.fixture()
def admin_token(client: Any) -> str:
    token = read_admin_token()
    assert token is not None
    return token


@pytest.fixture()
def inference_token(state_dir: pathlib.Path) -> str:
    """A real inference-scope bearer token, minted the way the gateway mints it."""
    return mint_token()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Task 1 -- a distinct admin token scope
# ---------------------------------------------------------------------------


def test_admin_token_is_a_separate_file_from_the_inference_token(
    state_dir: pathlib.Path,
) -> None:
    admin = mint_admin_token()
    inference = mint_token()

    assert ADMIN_TOKEN_FILENAME == "admin-token"
    assert admin_token_path() == state_dir / "admin-token"
    assert admin_token_path() != state_dir / "token"
    assert (state_dir / "admin-token").read_text().strip() == admin
    assert (state_dir / "token").read_text().strip() == inference


def test_admin_and_inference_tokens_are_different_secrets(
    state_dir: pathlib.Path,
) -> None:
    """Both minted, both present, and provably not the same value.

    Asserting ``read_admin_token() != read_token()`` without minting both is
    satisfied by ``"abc" != None``, which is true of an implementation that
    never wrote an admin token at all.
    """
    admin = mint_admin_token()
    inference = mint_token()

    assert isinstance(admin, str) and isinstance(inference, str)
    assert len(admin) >= 32
    assert len(inference) >= 32
    assert admin != inference
    assert read_admin_token() == admin
    assert read_token() == inference


def test_minting_the_admin_token_does_not_disturb_the_inference_token(
    state_dir: pathlib.Path,
) -> None:
    inference = mint_token()
    mint_admin_token()
    mint_admin_token()
    assert read_token() == inference


def test_admin_token_file_permissions_are_restrictive(
    state_dir: pathlib.Path,
) -> None:
    mint_admin_token()
    ok, reason = admin_token_permissions_ok()
    assert ok, reason


def test_read_admin_token_returns_none_when_absent(state_dir: pathlib.Path) -> None:
    """Absent is not corrupt. ``doctor`` has to tell the two apart."""
    assert read_admin_token() is None


def test_read_admin_token_raises_on_a_damaged_file(state_dir: pathlib.Path) -> None:
    from hermes_auto.gateway.auth import AuthError

    (state_dir / ADMIN_TOKEN_FILENAME).write_bytes(b"   ")
    with pytest.raises(AuthError):
        read_admin_token()


def test_admin_module_never_reads_the_inference_token() -> None:
    """No code path in ``admin.py`` can reach the inference token.

    A source-level check rather than a behavioural one because the failure being
    designed out is a future edit that adds a convenience fallback -- "if there
    is no admin token, accept the inference one" -- which would pass every
    behavioural test written against a state directory where both exist.
    """
    import ast
    import inspect

    import hermes_auto.gateway.admin as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert "read_token" not in imported
    assert "mint_token" not in imported
    assert "token_path" not in imported

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "read_token" not in called
    assert "mint_token" not in called


def test_constant_time_comparison_is_reused_not_reimplemented() -> None:
    """``compare_token`` is called, and no ``==`` comparison stands in for it."""
    import ast
    import inspect

    import hermes_auto.gateway.admin as module

    tree = ast.parse(inspect.getsource(module))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "compare_token" in called, "admin auth must go through auth.compare_token"

    # No local reimplementation of the primitive.
    attribute_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "compare_digest" not in attribute_calls, (
        "admin.py must not call hmac.compare_digest directly; a second copy of "
        "the primitive is a second thing to get wrong"
    )


# ---------------------------------------------------------------------------
# Task 3 -- the scope-separation proof
# ---------------------------------------------------------------------------


def test_status_with_the_admin_token_succeeds(
    client: Any, admin_token: str, state_dir: pathlib.Path
) -> None:
    response = client.get("/admin/v1/status", headers=auth(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert "instance_id" in body
    assert body["pid"] == os.getpid()
    assert body["strict_validation"] is False


@pytest.mark.parametrize(("method", "path"), ADMIN_ENDPOINTS)
def test_inference_token_is_rejected_by_every_admin_endpoint(
    client: Any,
    admin_token: str,
    inference_token: str,
    method: str,
    path: str,
) -> None:
    """The whole point of the plan.

    A caller holding the inference bearer token -- the one every Hermes session
    has, because it is what the provider plugin sends on every request -- gets
    401 from every admin endpoint including ``shutdown``, ``reroute`` and
    ``pin``. The admin token is minted and valid at the same time, so this
    cannot pass merely because the app rejects everything.
    """
    assert inference_token != admin_token

    denied = client.request(method, path, headers=auth(inference_token))
    assert denied.status_code == 401, (method, path, denied.text)
    assert_error_body(denied.json())

    accepted = client.request(method, path, headers=auth(admin_token))
    assert accepted.status_code != 401, (
        f"{method} {path} rejected the admin token too, so the 401 above proves "
        f"nothing about scope separation"
    )


@pytest.mark.parametrize(("method", "path"), ADMIN_ENDPOINTS)
def test_every_admin_endpoint_rejects_a_missing_token(
    client: Any, method: str, path: str
) -> None:
    response = client.request(method, path)
    assert response.status_code == 401
    assert_error_body(response.json())


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic abcdef",
        "bearer",
        "Token abcdef",
        "Bearer  extra spaces",
        "BearerNoSpace",
    ],
)
def test_malformed_authorization_headers_are_rejected(
    client: Any, admin_token: str, header: str
) -> None:
    response = client.get(
        "/admin/v1/status", headers={"Authorization": header}
    )
    assert response.status_code == 401
    assert_error_body(response.json())


def test_the_admin_token_prefixed_into_a_wrong_scheme_is_rejected(
    client: Any, admin_token: str
) -> None:
    """A correct secret in the wrong scheme is still a failed authentication."""
    response = client.get(
        "/admin/v1/status", headers={"Authorization": f"Basic {admin_token}"}
    )
    assert response.status_code == 401


def test_every_401_body_is_identical(
    client: Any, inference_token: str
) -> None:
    """Absent, malformed, wrong, and inference-scope all produce one body.

    A body that differed would tell an unauthenticated local process whether an
    admin token exists and whether the credential it holds is the inference one.
    """
    bodies = {
        client.get("/admin/v1/status").text,
        client.get("/admin/v1/status", headers={"Authorization": "garbage"}).text,
        client.get("/admin/v1/status", headers=auth("wrong-token-value")).text,
        client.get("/admin/v1/status", headers=auth(inference_token)).text,
    }
    assert len(bodies) == 1, bodies


def test_unknown_admin_paths_return_401_not_404(client: Any) -> None:
    """An unauthenticated caller learns nothing about the admin surface."""
    assert client.get("/admin/v1/does-not-exist").status_code == 401
    assert client.get("/nowhere").status_code == 401


def test_authentication_runs_as_middleware_so_a_new_route_cannot_forget_it(
    state_dir: pathlib.Path,
) -> None:
    """The guarantee is structural, not per-handler.

    Proven by adding a route to a live admin app *after* construction and
    showing it is protected without anyone having written an auth call for it.
    """
    app = create_admin_app()

    async def added(request: Any) -> Any:
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("reached")

    app.router.routes.append(Route("/admin/v1/added-later", added, methods=["GET"]))

    with TestClient(app) as test_client:
        token = read_admin_token()
        assert token is not None
        assert test_client.get("/admin/v1/added-later").status_code == 401
        allowed = test_client.get("/admin/v1/added-later", headers=auth(token))
        assert allowed.status_code == 200
        assert allowed.text == "reached"


def test_a_deleted_admin_token_file_fails_closed_and_the_listener_stays_up(
    client: Any, admin_token: str, state_dir: pathlib.Path
) -> None:
    """Missing token file -> 401 everywhere, but the app still answers.

    Refusing the connection outright would leave ``doctor`` with nothing to
    report but "connection refused", which is indistinguishable from "not
    running".
    """
    assert client.get("/admin/v1/status", headers=auth(admin_token)).status_code == 200

    (state_dir / ADMIN_TOKEN_FILENAME).unlink()

    for method, path in ADMIN_ENDPOINTS:
        response = client.request(method, path, headers=auth(admin_token))
        assert response.status_code == 401, (method, path)
        assert_error_body(response.json())


def test_no_token_file_and_no_header_denies(state_dir: pathlib.Path) -> None:
    """An admin app that never minted a token authenticates nobody."""
    app = create_admin_app()  # constructed, never started, so nothing minted
    assert not admin_token_path(create=False).exists()

    transport_client = TestClient(app)
    assert transport_client.get("/admin/v1/status").status_code == 401
    assert (
        transport_client.get(
            "/admin/v1/status", headers={"Authorization": "Bearer "}
        ).status_code
        == 401
    )


def test_an_empty_expected_token_denies_rather_than_matching_an_empty_header(
    state_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``compare_digest(b"", b"")`` trap, pinned at the branch that guards it.

    ``hmac.compare_digest`` of two empty byte strings returns **True**. Today
    ``read_admin_token`` returns ``None`` for an absent file and raises for an
    empty one, so no empty string ever reaches the comparison -- which means the
    behavioural tests above pass with or without the guard, and the guard looks
    like dead code to the next reader.

    It is not dead code, it is the thing standing between the project and one
    very ordinary refactor: ``return path.read_text().strip()`` with ``""`` as
    the absent case. That is simulated here directly, because a test that cannot
    fail is worse than no test -- it certifies the branch it never reached.
    """
    import hmac

    import hermes_auto.gateway.admin as module

    assert hmac.compare_digest(b"", b"") is True, "the trap this test guards"

    monkeypatch.setattr(module, "_expected_admin_token", lambda configured: "")

    app = create_admin_app()
    transport_client = TestClient(app)

    assert transport_client.get("/admin/v1/status").status_code == 401
    assert (
        transport_client.get(
            "/admin/v1/status", headers={"Authorization": "Bearer "}
        ).status_code
        == 401
    )
    assert transport_client.get("/admin/v1/status", headers=auth("")).status_code == 401


def test_a_corrupt_admin_token_file_denies_rather_than_500(
    client: Any, admin_token: str, state_dir: pathlib.Path
) -> None:
    (state_dir / ADMIN_TOKEN_FILENAME).write_bytes(b"\xff\xfe not ascii")
    response = client.get("/admin/v1/status", headers=auth(admin_token))
    assert response.status_code == 401
    assert_error_body(response.json())


def test_require_admin_returns_none_on_success_and_a_response_on_failure(
    state_dir: pathlib.Path,
) -> None:
    from starlette.requests import Request

    token = mint_admin_token()

    def make(header: str | None) -> Request:
        headers = [(b"authorization", header.encode())] if header else []
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin/v1/status",
                "headers": headers,
                "app": None,
            }
        )

    assert require_admin(make(f"Bearer {token}"), configured=state_dir) is None
    denied = require_admin(make(None), configured=state_dir)
    assert denied is not None
    assert denied.status_code == 401


# ---------------------------------------------------------------------------
# The status payload carries no secret
# ---------------------------------------------------------------------------


def test_status_payload_leaks_neither_token_nor_upstream_credential(
    state_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scan the serialized body for every secret that exists on this machine."""
    secret = "sk-live-DO-NOT-LEAK-4f2b9c"
    monkeypatch.setenv("HERMES_TEST_UPSTREAM_KEY", secret)
    config = AutoRouterConfig(
        gateway=GatewayConfig(state_dir=state_dir),
        upstream=UpstreamConfig(
            base_url="http://127.0.0.1:11434/v1",
            credential_ref="env:HERMES_TEST_UPSTREAM_KEY",
        ),
    )
    inference = mint_token()

    with TestClient(create_admin_app(config)) as test_client:
        admin = read_admin_token()
        assert admin is not None
        response = test_client.get("/admin/v1/status", headers=auth(admin))

    assert response.status_code == 200
    raw = response.text
    assert secret not in raw
    assert admin not in raw
    assert inference not in raw
    assert "HERMES_TEST_UPSTREAM_KEY" not in raw

    body = response.json()
    for banned in ("token", "admin_token", "api_key", "secret", "authorization"):
        assert banned not in body


def test_status_strips_userinfo_from_the_upstream_url(
    state_dir: pathlib.Path,
) -> None:
    """A credential smuggled into ``base_url`` does not reach the response."""
    config = AutoRouterConfig(
        gateway=GatewayConfig(state_dir=state_dir),
        upstream=UpstreamConfig(base_url="https://alice:hunter2@api.example.com/v1"),
    )
    with TestClient(create_admin_app(config)) as test_client:
        admin = read_admin_token()
        assert admin is not None
        response = test_client.get("/admin/v1/status", headers=auth(admin))

    assert "hunter2" not in response.text
    assert "alice" not in response.text
    assert response.json()["upstream_base_url"] == "https://api.example.com/v1"


def test_status_reports_the_runtime_files_instance_id(
    state_dir: pathlib.Path,
) -> None:
    """Unlike ``/healthz``, which reports the answering process's own id.

    That difference is deliberate: plan 02-07 compares the two to defeat PID
    reuse, so a ``/healthz`` reading the file would make the mismatch branch
    unreachable. ``/admin/v1/status`` is the endpoint that answers "what does the
    supervisor think is running?", so the file is the right source here.
    """
    from hermes_auto.state.runtime import RuntimeFile, write_runtime

    write_runtime(
        RuntimeFile(
            pid=4321,
            port=18787,
            admin_port=18788,
            instance_id="a" * 32,
            started_at="2026-01-01T00:00:00+00:00",
            exe=sys.executable,
        )
    )
    with TestClient(create_admin_app()) as test_client:
        admin = read_admin_token()
        assert admin is not None
        body = test_client.get("/admin/v1/status", headers=auth(admin)).json()

    assert body["instance_id"] == "a" * 32
    assert body["port"] == 18787
    assert body["admin_port"] == 18788
    assert body["uptime_seconds"] > 0


def test_status_survives_a_damaged_runtime_file(state_dir: pathlib.Path) -> None:
    """A diagnostic endpoint must not fail on the thing it is diagnosing."""
    runtime = state_dir / "runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "gateway.json").write_text("{not json")

    with TestClient(create_admin_app()) as test_client:
        admin = read_admin_token()
        assert admin is not None
        response = test_client.get("/admin/v1/status", headers=auth(admin))

    assert response.status_code == 200
    assert response.json()["instance_id"] is None


# ---------------------------------------------------------------------------
# Honest 501s
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), UNIMPLEMENTED_ENDPOINTS)
def test_unimplemented_endpoints_return_a_parseable_501_naming_a_phase(
    client: Any, admin_token: str, method: str, path: str
) -> None:
    response = client.request(method, path, headers=auth(admin_token))
    assert response.status_code == 501, (method, path, response.text)

    body = response.json()
    assert_error_body(body)
    assert body["error"]["type"] == NOT_IMPLEMENTED_TYPE
    assert body["error"]["code"] == NOT_IMPLEMENTED_TYPE
    assert re.search(r"Phase \d+", body["error"]["message"]), body["error"]["message"]


@pytest.mark.parametrize(("method", "path"), UNIMPLEMENTED_ENDPOINTS)
def test_unimplemented_endpoints_do_not_reflect_the_callers_path_parameter(
    client: Any, admin_token: str, method: str, path: str
) -> None:
    """No stub success, and no reflection primitive either."""
    marked = path.replace("dec_123", "REFLECT_ME").replace("sess_123", "REFLECT_ME")
    response = client.request(method, marked, headers=auth(admin_token))
    assert response.status_code == 501
    assert "REFLECT_ME" not in response.text


def test_the_four_unimplemented_routes_are_the_design_53_set(client: Any) -> None:
    """§5.3's endpoint list, present in full and split the way this plan claims."""
    app = create_admin_app()
    paths = {route.path for route in app.routes}
    assert paths == {
        "/admin/v1/status",
        "/admin/v1/shutdown",
        "/admin/v1/decisions/{decision_id}",
        "/admin/v1/sessions/{session_id}/reroute",
        "/admin/v1/sessions/{session_id}/pin",
        "/admin/v1/feedback",
    }


# ---------------------------------------------------------------------------
# The admin surface serves no inference
# ---------------------------------------------------------------------------


def test_admin_app_serves_no_inference_route() -> None:
    paths = {route.path for route in create_admin_app().routes}
    assert "/v1/chat/completions" not in paths
    assert "/v1/models" not in paths
    assert "/healthz" not in paths
    assert "/readyz" not in paths
    assert all(path.startswith("/admin/v1/") for path in paths), sorted(paths)


def test_admin_and_inference_apps_share_no_path(state_dir: pathlib.Path) -> None:
    """The two surfaces cannot silently merge."""
    admin_paths = {route.path for route in create_admin_app().routes}
    inference_paths = {route.path for route in create_app().routes}
    assert admin_paths & inference_paths == set()


def test_admin_paths_404_on_the_inference_app(state_dir: pathlib.Path) -> None:
    inference = mint_token()
    with TestClient(create_app()) as test_client:
        response = test_client.get(
            "/admin/v1/status", headers=auth(read_token() or inference)
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# No CORS -- asserted here because nothing in the suite asserted it before
# ---------------------------------------------------------------------------


def test_admin_app_has_no_cors_middleware() -> None:
    app = create_admin_app()
    classes = [middleware.cls for middleware in app.user_middleware]
    assert CORSMiddleware not in classes
    assert classes == [AdminAuthMiddleware], classes


def test_inference_app_has_no_cors_middleware(state_dir: pathlib.Path) -> None:
    """02-CONTEXT § Phase-Wide Constraints item 2 says "no CORS" for the gateway.

    Asserted here rather than nowhere. This test reads ``create_app`` and changes
    nothing about it.
    """
    classes = [middleware.cls for middleware in create_app().user_middleware]
    assert CORSMiddleware not in classes


def test_no_access_control_headers_on_a_cross_origin_admin_request(
    client: Any, admin_token: str
) -> None:
    response = client.get(
        "/admin/v1/status",
        headers={**auth(admin_token), "Origin": "http://evil.example"},
    )
    assert response.status_code == 200
    leaked = [
        name
        for name in response.headers
        if name.lower().startswith("access-control-")
    ]
    assert leaked == [], leaked


def test_a_cors_preflight_is_not_answered(client: Any, admin_token: str) -> None:
    """A browser preflight must not be granted, with or without a token."""
    for headers in (
        {
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
        {
            **auth(admin_token),
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    ):
        response = client.request("OPTIONS", "/admin/v1/shutdown", headers=headers)
        assert response.status_code in (401, 405), response.status_code
        assert not any(
            name.lower().startswith("access-control-") for name in response.headers
        )


def test_cors_assertion_is_not_vacuous() -> None:
    """The assertions above fail on an app that *does* enable CORS.

    Without this, ``CORSMiddleware not in classes`` and "no access-control
    headers" would both pass on an app whose middleware list was simply never
    inspected correctly.
    """
    permissive = Starlette(routes=[])
    permissive.add_middleware(CORSMiddleware, allow_origins=["*"])
    classes = [middleware.cls for middleware in permissive.user_middleware]
    assert CORSMiddleware in classes

    with TestClient(permissive) as test_client:
        response = test_client.request(
            "OPTIONS",
            "/anything",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert any(
        name.lower().startswith("access-control-") for name in response.headers
    ), "the control app must emit CORS headers, or the negative test proves nothing"


# ---------------------------------------------------------------------------
# Shutdown: 202, and a drain that really drains
# ---------------------------------------------------------------------------


def test_shutdown_returns_202(client: Any, admin_token: str) -> None:
    response = client.post("/admin/v1/shutdown", headers=auth(admin_token))
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["servers_signalled"] == 0, (
        "TestClient hosts no uvicorn server; a nonzero count here would mean the "
        "handler found something it should not have"
    )


def test_shutdown_with_the_inference_token_is_401(
    client: Any, inference_token: str
) -> None:
    response = client.post("/admin/v1/shutdown", headers=auth(inference_token))
    assert response.status_code == 401
    assert_error_body(response.json())


def _free_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    return sock


class _TwoServerProcess:
    """Two uvicorn servers on one event loop, the way ``main.py`` runs them.

    Mirrors ``gateway/main.py``: one "inference" server and one admin server
    sharing a loop, with the secondary suppressing signal capture. The point is
    that ``POST /admin/v1/shutdown`` must stop *both*, and must let an in-flight
    stream on the other server finish first.
    """

    def __init__(self, admin_app: Any, slow_app: Any) -> None:
        self.admin_socket = _free_socket()
        self.slow_socket = _free_socket()
        self.admin_port = int(self.admin_socket.getsockname()[1])
        self.slow_port = int(self.slow_socket.getsockname()[1])
        self.admin_server = uvicorn.Server(
            uvicorn.Config(admin_app, log_level="warning", access_log=False)
        )
        self.slow_server = uvicorn.Server(
            uvicorn.Config(slow_app, log_level="warning", access_log=False)
        )
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        import asyncio

        async def both() -> None:
            await asyncio.gather(
                self.slow_server.serve(sockets=[self.slow_socket]),
                self.admin_server.serve(sockets=[self.admin_socket]),
            )

        asyncio.run(both())

    def __enter__(self) -> _TwoServerProcess:
        self._thread.start()
        deadline = time.monotonic() + 20
        while not (self.admin_server.started and self.slow_server.started):
            if time.monotonic() > deadline:
                raise RuntimeError("servers did not start")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc: object) -> None:
        self.admin_server.should_exit = True
        self.slow_server.should_exit = True
        self._thread.join(timeout=20)


def test_shutdown_drains_both_listeners_without_severing_an_inflight_stream(
    state_dir: pathlib.Path,
) -> None:
    """The graceful-drain property plan 02-07's ``stop`` is built on.

    A slow stream is opened on the second server, ``POST /admin/v1/shutdown`` is
    issued while it is still emitting, and the stream is then required to arrive
    **complete**. If the handler forced an exit, or if uvicorn's shutdown severed
    in-flight tasks, the final chunk would be missing.
    """
    import asyncio

    chunk_count = 12

    async def slow_stream(request: Any) -> Any:
        async def body() -> Any:
            for index in range(chunk_count):
                yield f"chunk-{index}\n".encode()
                await asyncio.sleep(0.05)
            yield b"END\n"

        return StreamingResponse(body(), media_type="text/plain")

    slow_app = Starlette(routes=[Route("/slow", slow_stream, methods=["GET"])])

    with TestClient(create_admin_app()) as _boot:
        pass
    admin = read_admin_token()
    assert admin is not None

    collected: list[bytes] = []
    error: list[BaseException] = []

    with _TwoServerProcess(create_admin_app(), slow_app) as process:

        def read_stream() -> None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{process.slow_port}/slow", timeout=30
                ) as response:
                    collected.append(response.read())
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                error.append(exc)

        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()
        time.sleep(0.15)  # let the stream get going before asking for a stop

        request = urllib.request.Request(
            f"http://127.0.0.1:{process.admin_port}/admin/v1/shutdown", method="POST"
        )
        request.add_header("Authorization", f"Bearer {admin}")
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status == 202
            payload = json.loads(response.read())

        assert payload["servers_signalled"] == 2, payload

        reader.join(timeout=30)
        assert not error, error
        assert collected, "the slow stream produced nothing"
        assert collected[0].endswith(b"END\n"), collected[0][-40:]
        assert collected[0].count(b"chunk-") == chunk_count

        deadline = time.monotonic() + 25
        while process._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process._thread.is_alive(), "servers did not exit after shutdown"

        # Checked here, inside the context manager: ``__exit__`` sets
        # ``should_exit`` itself as a cleanup, so the same assertion after the
        # block would hold no matter what the handler did.
        assert process.admin_server.should_exit
        assert process.slow_server.should_exit
        # ``force_exit`` is what cancels in-flight tasks. A handler that set it
        # would stop the process just as reliably and cut every stream in
        # flight -- which is the difference between a drain and a kill.
        assert not process.admin_server.force_exit
        assert not process.slow_server.force_exit


def test_uvicorn_graceful_shutdown_is_not_time_bounded(state_dir: pathlib.Path) -> None:
    """A bounded graceful timeout would cancel a long completion mid-stream.

    ``main.py`` leaves ``timeout_graceful_shutdown`` unset, which means uvicorn
    waits indefinitely for in-flight tasks. Asserted rather than assumed, because
    setting it is a one-line change whose consequence -- truncated responses only
    during shutdown -- is invisible in every other test.
    """
    from hermes_auto.gateway import main as gateway_main

    server = gateway_main._make_server(create_admin_app(), primary=False)
    assert server.config.timeout_graceful_shutdown is None


# ---------------------------------------------------------------------------
# The listener is real: one process, two ports, from the runtime file
# ---------------------------------------------------------------------------


def _wait_for(predicate: Any, timeout: float, message: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(message)


def _http(port: int, path: str, token: str | None = None, method: str = "GET") -> tuple[int, str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method
    )
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_the_recorded_admin_port_names_a_listener_that_is_accepting(
    tmp_path: pathlib.Path,
) -> None:
    """The defect this plan closes, tested end to end.

    Before this plan the admin listener was started by nothing: ``stop()``'s
    documented primary path POSTed to a port with no listener and silently fell
    through to a hard kill, and ``admin_port`` was recorded as the sentinel 0.
    A unit test that constructs the admin app in isolation cannot see any of
    that. So this starts the real supervised entry point in a real subprocess,
    reads the real runtime file, and connects to the port it names.

    Ephemeral ports are used on purpose: the port in the file is then a value
    nothing in this test chose, so reaching a listener on it proves the *file*
    is right rather than proving that two constants match.
    """
    state = tmp_path / "state"
    state.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "auto_router:\n"
        "  gateway:\n"
        '    url: "http://127.0.0.1:0"\n'
        "    port: 0\n"
        "    admin_port: 0\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["HERMES_AUTO_STATE_DIR"] = str(state)
    env["HERMES_AUTO_CONFIG"] = str(config_path)
    env["HERMES_AUTO_TEST_EPHEMERAL"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    process = subprocess.Popen(
        [sys.executable, "-m", "hermes_auto.gateway.main"],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    runtime_file = state / "runtime" / "gateway.json"
    try:
        def read_runtime_doc() -> Any:
            if process.poll() is not None:
                raise AssertionError(
                    f"gateway exited early: {process.stdout.read() if process.stdout else ''}"
                )
            if not runtime_file.exists():
                return None
            try:
                return json.loads(runtime_file.read_text())
            except json.JSONDecodeError:
                return None

        document = _wait_for(read_runtime_doc, 45, "runtime file never appeared")

        assert document["admin_port"] != 0, (
            "admin_port is the 0 sentinel, which means no admin listener was "
            "started -- stop() would fall through to a hard kill"
        )
        assert document["admin_port"] != document["port"]

        admin_port = document["admin_port"]
        inference_port = document["port"]

        # The listener answers -- and it fails closed, which is how we know we
        # reached the admin app rather than an open socket.
        _wait_for(
            lambda: _http(admin_port, "/admin/v1/status")[0] == 401,
            45,
            "nothing accepting on the recorded admin_port",
        )
        _wait_for(
            lambda: (state / ADMIN_TOKEN_FILENAME).exists(),
            45,
            "the admin token was never minted",
        )

        admin = (state / ADMIN_TOKEN_FILENAME).read_text().strip()
        inference = (state / "token").read_text().strip()
        assert admin != inference

        status, body = _http(admin_port, "/admin/v1/status", admin)
        assert status == 200, body
        assert json.loads(body)["instance_id"] == document["instance_id"]

        # It is the admin app on the admin port, and the inference app on the
        # inference port -- not one app answering both.
        assert _http(admin_port, "/admin/v1/status", inference)[0] == 401
        assert _http(admin_port, "/healthz")[0] == 401
        assert _http(inference_port, "/healthz")[0] == 200
        assert _http(inference_port, "/admin/v1/status", admin)[0] == 404

        # And the primary stop path really stops the process.
        status, body = _http(admin_port, "/admin/v1/shutdown", admin, method="POST")
        assert status == 202, body
        assert json.loads(body)["servers_signalled"] == 2

        process.wait(timeout=45)
        assert process.returncode == 0
        assert not runtime_file.exists(), "the runtime file outlived its process"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=20)
        if process.stdout is not None:
            process.stdout.close()


def test_main_binds_the_admin_listener_through_create_admin_app() -> None:
    """``main.py`` reaches this module, and by the name this module exports.

    The lazy import in ``main._build_admin_app`` is what makes the admin listener
    start at all. It catches ``ImportError`` and degrades to ``admin_port: 0``, so
    a rename here would not fail loudly -- it would produce a gateway that looks
    healthy and cannot be stopped gracefully.
    """
    import inspect

    from hermes_auto.gateway import main as gateway_main

    source = inspect.getsource(gateway_main._build_admin_app)
    assert "from .admin import create_admin_app" in source

    config = AutoRouterConfig()
    import logging

    built = gateway_main._build_admin_app(config, logging.getLogger("test-null"))
    assert built is not None
    assert {route.path for route in built.routes} >= {
        "/admin/v1/status",
        "/admin/v1/shutdown",
    }


# ---------------------------------------------------------------------------
# admin_main.py -- the debug entry, loopback only
# ---------------------------------------------------------------------------


def test_admin_main_refuses_a_non_loopback_url(state_dir: pathlib.Path) -> None:
    import asyncio

    from hermes_auto.gateway.admin_main import serve_admin
    from hermes_auto.gateway.main import StartupError

    for url in ("http://0.0.0.0:8788", "http://192.168.1.10:8788", "http://example.com"):
        config = AutoRouterConfig(
            gateway=GatewayConfig(url=url, port=8787, admin_port=8788)
        )
        with pytest.raises(StartupError):
            asyncio.run(serve_admin(config))


def test_admin_main_refuses_to_share_the_inference_port() -> None:
    from hermes_auto.gateway.admin_main import resolve_admin_port
    from hermes_auto.gateway.main import StartupError

    config = AutoRouterConfig(gateway=GatewayConfig(port=8787, admin_port=8787))
    with pytest.raises(StartupError):
        resolve_admin_port(config)

    ok = AutoRouterConfig(gateway=GatewayConfig(port=8787, admin_port=8788))
    assert resolve_admin_port(ok) == 8788


def test_admin_main_binds_loopback_and_serves_the_admin_app(
    state_dir: pathlib.Path,
) -> None:
    """The debug entry really binds, on a loopback address, and really serves."""
    import asyncio

    from hermes_auto.gateway import admin_main

    bound: dict[str, Any] = {}
    original = admin_main.bind_socket

    def spy(host: str, port: int) -> socket.socket:
        sock = original(host, port)
        bound["host"] = sock.getsockname()[0]
        bound["port"] = int(sock.getsockname()[1])
        return sock

    admin_main.bind_socket = spy  # type: ignore[assignment]
    try:
        config = AutoRouterConfig(
            gateway=GatewayConfig(url="http://127.0.0.1:0", port=0, admin_port=0)
        )

        result: list[int] = []

        def run() -> None:
            result.append(asyncio.run(admin_main.serve_admin(config)))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        _wait_for(lambda: bound.get("port"), 30, "admin_main never bound")
        _wait_for(
            lambda: _http(bound["port"], "/admin/v1/status")[0] == 401,
            30,
            "admin_main bound but never served",
        )

        assert bound["host"] == "127.0.0.1"

        admin = read_admin_token()
        assert admin is not None
        status, body = _http(bound["port"], "/admin/v1/status", admin)
        assert status == 200, body

        status, body = _http(
            bound["port"], "/admin/v1/shutdown", admin, method="POST"
        )
        assert status == 202
        thread.join(timeout=30)
        assert not thread.is_alive(), "admin_main did not exit after shutdown"
        assert result == [0]
    finally:
        admin_main.bind_socket = original  # type: ignore[assignment]


def test_admin_main_pins_the_loopback_constant() -> None:
    from hermes_auto.gateway import admin_main, main

    assert admin_main.LOOPBACK_HOST == "127.0.0.1"
    assert main.BIND_HOST == admin_main.LOOPBACK_HOST


# ---------------------------------------------------------------------------
# Nothing here logs a secret
# ---------------------------------------------------------------------------


#: Every module in this project that holds a logger. Scanning only
#: ``gateway.admin`` -- which is all this check used to do -- leaves the other
#: ten unexamined, and ``gateway/app.py`` is where a request payload is actually
#: in scope.
LOGGING_MODULES = (
    "hermes_auto.commands",
    "hermes_auto.gateway.admin",
    "hermes_auto.gateway.admin_main",
    "hermes_auto.gateway.app",
    "hermes_auto.gateway.errors",
    "hermes_auto.gateway.ingress",
    "hermes_auto.gateway.main",
    "hermes_auto.gateway.relay",
    "hermes_auto.gateway.upstream",
    "hermes_auto.health.probe",
    "hermes_auto.plugin",
    "hermes_auto.supervisor",
)

#: Logger method names whose arguments are a log payload.
LOGGING_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log"}
)

#: Near-miss spellings of a credential. ``BANNED_KEYS`` matches by exact name and
#: says so deliberately -- a substring rule on ``token`` would delete the
#: ``prompt_tokens`` counts the router legitimately keeps. The consequence is
#: that ``BANNED_KEYS`` alone does not cover a payload key spelled ``bearer``,
#: and it covers no *value* at all.
CREDENTIALISH_NAMES = frozenset(
    {"bearer", "credential", "credentials", "apikey", "password", "passwd"}
)

#: Suffixes that make an identifier a credential whatever it is prefixed with:
#: ``admin_token``, ``_credential``, ``read_token()``, ``mint_admin_token()``.
CREDENTIALISH_SUFFIXES = (
    "_token",
    "_secret",
    "_credential",
    "_credentials",
    "_api_key",
    "_apikey",
    "_password",
)


#: Builtins whose return is a number derived from their argument, not the
#: argument. ``{"response_bytes": len(content)}`` is a byte count and
#: ``docs/privacy.md`` lists exactly these derived counts among what the router
#: legitimately keeps, so the scan stops at the call rather than descending into
#: it. ``str``, ``repr`` and ``format`` are deliberately absent: they preserve
#: the value, so ``str(app.state.token)`` must stay visible.
#:
#: The cost of the carve-out is that ``len(app.state.token)`` would pass. A
#: length is not a credential, and the alternative is a check that fires on
#: correct code -- which is a check that gets deleted.
DERIVED_SCALAR_CALLS = frozenset({"len", "sum", "int", "float", "bool", "round", "abs"})


def _is_credentialish(name: str) -> bool:
    """Is *name* an identifier that holds, or returns, a live credential?"""
    from hermes_auto.telemetry.redaction import BANNED_KEYS

    folded = name.casefold()
    if folded in BANNED_KEYS or folded in CREDENTIALISH_NAMES:
        return True
    return folded.endswith(CREDENTIALISH_SUFFIXES)


def _value_identifiers(node: Any) -> Any:
    """Yield every identifier a value expression reads, minus derived counts."""
    import ast

    if isinstance(node, ast.Call):
        callee = node.func
        named = (
            callee.id
            if isinstance(callee, ast.Name)
            else callee.attr
            if isinstance(callee, ast.Attribute)
            else None
        )
        if named in DERIVED_SCALAR_CALLS:
            yield from _value_identifiers(callee)
            return

    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr

    for child in ast.iter_child_nodes(node):
        yield from _value_identifiers(child)


def _scan_logging_calls(source: str, where: str) -> tuple[set[str], list[str]]:
    """Return every payload key and every credential-valued expression in *source*.

    An AST walk rather than a substring scan: ``'token' not in source`` is false
    for a module that legitimately says ``read_admin_token``, and a scan loose
    enough to pass would be too loose to catch anything.

    Two separate answers, because they catch different mutations. The *keys* set
    catches a payload that names a banned field. The *values* list catches a
    payload that carries a credential under a key nobody thought to ban --
    ``{"bearer": app.state.token}`` is the one a reviewer actually planted, and
    a key-name check cannot see it because ``bearer`` is not a banned key.
    """
    import ast

    keys: set[str] = set()
    credential_values: list[str] = []

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOGGING_METHODS:
            continue

        payloads = [*node.args, *(keyword.value for keyword in node.keywords)]
        for payload in payloads:
            if not isinstance(payload, ast.Dict):
                continue
            for key, value in zip(payload.keys, payload.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value.casefold())

                # The whole value subtree, so `str(app.state.token)` and
                # `f"{read_token(d)}"` are as visible as a bare reference.
                for found in _value_identifiers(value):
                    if _is_credentialish(found):
                        rendered = (
                            key.value if isinstance(key, ast.Constant) else "<**>"
                        )
                        credential_values.append(
                            f"{where}:{node.lineno} key={rendered!r} value={found}"
                        )

    return keys, credential_values


#: The mutation a reviewer planted in a gateway log payload, which the previous
#: key-only check passed with the entire suite green.
CANARY_LOG_PAYLOAD = '''
def relay(app, logger, state_dir):
    logger.info(
        {
            "event": "relay.complete",
            "bearer": app.state.token,
            "user_prompt": "TOP-SECRET",
        }
    )
    logger.info({"event": "x", "who": read_token(state_dir)})
    logger.warning({"event": "y", "detail": self._credential})
    logger.info({"event": "clean", "response_bytes": len(content), "port": port})
'''


def test_the_log_payload_scanner_catches_the_mutation_it_exists_for() -> None:
    """Prove the scanner works before trusting it to report nothing.

    A structural test that silently stops matching is worse than no test, and
    this one has three ways to stop matching: the method-name set, the ``ast.Dict``
    shape, and the value walk. All three are exercised here against source that
    is known to be dirty.

    The first two assertions are the finding restated: neither ``bearer`` nor
    ``user_prompt`` is a banned key, so the key check -- the only check that used
    to exist -- passes on this payload.
    """
    from hermes_auto.telemetry.redaction import BANNED_KEYS

    keys, credential_values = _scan_logging_calls(CANARY_LOG_PAYLOAD, "<canary>")

    assert "bearer" not in BANNED_KEYS
    assert "user_prompt" not in BANNED_KEYS
    assert keys & BANNED_KEYS == set(), (
        "the canary is supposed to be invisible to the key check; if this fires "
        "the canary no longer reproduces the finding"
    )

    assert len(credential_values) == 3, credential_values
    joined = " ".join(credential_values)
    assert "value=token" in joined, "missed app.state.token"
    assert "value=read_token" in joined, "missed a read_token() return"
    assert "value=_credential" in joined, "missed a _credential attribute"

    # The carve-out, pinned from the other side: a derived count is not a leak,
    # and a scanner that flags `len(content)` is one that gets deleted for
    # crying wolf on correct code.
    assert "clean" not in joined, joined


def test_no_module_logs_a_banned_key_or_a_credential() -> None:
    """Every logging call in the project, keys and values both.

    Plan 02-04 checked this across all seven gateway modules by hand and shipped
    no test, so the property held once and was unenforced afterwards. This is
    that check, executed.
    """
    import importlib
    import inspect

    from hermes_auto.telemetry.redaction import BANNED_KEYS

    all_keys: set[str] = set()
    all_credential_values: list[str] = []
    modules_with_logging: list[str] = []

    for name in LOGGING_MODULES:
        module = importlib.import_module(name)
        keys, credential_values = _scan_logging_calls(inspect.getsource(module), name)
        if keys:
            modules_with_logging.append(name)
        all_keys |= keys
        all_credential_values += credential_values

    # Two self-checks. The first is the original: a walk that matches nothing
    # reports a clean project indistinguishably from a broken parser. The second
    # pins the module list, so deleting an entry to silence a finding is a
    # visible edit rather than a quiet narrowing of scope.
    assert all_keys, "no structured logging calls found; the walk is broken"
    assert len(LOGGING_MODULES) == 12, sorted(LOGGING_MODULES)
    assert "hermes_auto.gateway.app" in modules_with_logging, (
        "gateway/app.py is the module that handles request payloads; a scan "
        "that finds no logging call in it is not scanning it"
    )

    assert all_keys & BANNED_KEYS == set(), sorted(all_keys & BANNED_KEYS)
    assert all_credential_values == [], all_credential_values


def test_the_denied_log_record_carries_no_authorization_header(
    state_dir: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A real denial, with a real credential presented, logged and inspected."""
    import logging

    secret = "definitely-not-the-admin-token-9f8e7d"
    with TestClient(create_admin_app()) as test_client:
        with caplog.at_level(logging.WARNING, logger="hermes_auto.gateway.admin"):
            response = test_client.get(
                "/admin/v1/status", headers=auth(secret)
            )
    assert response.status_code == 401
    rendered = " ".join(str(record.msg) for record in caplog.records)
    assert secret not in rendered
    assert "authorization" not in rendered.casefold()
