"""Live smoke coverage across the five Hermes surfaces named by design.md §20.5.

The surfaces are **CLI, TUI, messaging gateway, desktop, and cron**. Three of
them are driven here against a real Hermes installation and a real sidecar; two
skip, each with a reason that names the specific missing piece rather than the
word "unavailable".

Read ``tests/e2e/README.md`` before trusting a green run of this file. The short
version: **a skip is not a pass**, and this module is engineered so that the two
cannot be confused -- every skip message says what would make it runnable.

Each drivable surface answers four questions, one per test, so a failure names
the broken property instead of "the CLI test failed":

1. Does Hermes resolve the ``hermes-auto`` provider and select a virtual model?
   That is the thing ``pip install`` alone does **not** do -- the provider is
   found by a directory scan of ``$HERMES_HOME/plugins/model-providers/``, so
   this is a test of the shim installer as much as of the profile.
2. Does a conversation complete end to end through the gateway?
3. Does a tool call fire, and is its result returned to the model?
4. Is the sidecar log free of raw prompt content after the exchange?

What this file deliberately does not do is compare bytes. Plan 02-08 owns
byte-identity between the gateway and a direct call, with a chunk-boundary
fuzzer; live subprocesses are far too slow and too coarse to be that oracle.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
import threading
from typing import Any

import pytest

from tests.e2e.conftest import (
    EXPECTED_REPLY,
    EXPECTED_TOOL_CALL_ID,
    EXPECTED_TOOL_NAME,
    UPSTREAM_ENV_VAR,
    VIRTUAL_MODEL,
    LiveStack,
    SurfaceRun,
    hermes_module_argv,
    pretty,
    read_log,
    require_live_stack_preconditions,
    run_child,
    script_upstream,
    snapshot_requests,
)

# Runs at import time so an unconfigured machine reports one specific reason for
# the whole module rather than repeating it per test.
HERMES = require_live_stack_preconditions()

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

#: Windows allocates a fresh console window for a console-subsystem child when
#: the parent has no console of its own. ``CREATE_NO_WINDOW`` suppresses it and
#: does not exist on POSIX, hence the ``getattr``.
_NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Keys the redaction boundary forbids in any log record (ROADMAP criterion 7).
BANNED_LOG_KEYS = (
    "messages",
    "content",
    "prompt",
    "tool_calls",
    "tool_result",
    "tool_output",
    "arguments",
    "authorization",
    "api_key",
    "token",
    "secret",
)

#: A sentence that appears in the prompt this suite sends and nowhere else, so
#: finding it in a log is unambiguous evidence of a leak rather than a
#: coincidence with a framework's own vocabulary.
CANARY_PROMPT = "zebra-canary-marker Say hello."

# Reasons. Each names the missing piece and what would make the surface
# runnable, because "skipped" alone tells a reader nothing about whether the
# suite is unconfigured or the product is broken.
GATEWAY_SKIP_REASON = (
    "messaging-gateway surface not exercised: `hermes gateway run` serves "
    "Telegram/Discord/Signal/WhatsApp/Weixin, and every platform in "
    "gateway/platform_registry.py declares required_env naming a third-party "
    "credential this repository does not hold. With none configured, Hermes "
    "prints 'No platforms configured' and starts no listener, so there is no "
    "conversation to drive. Runnable by exporting a bot token for one platform "
    "and pointing it at a scratch chat -- a credential decision, not a code one."
)

DESKTOP_SKIP_REASON = (
    "desktop surface not exercised: apps/desktop is an Electron application "
    "with no Python driver. Driving it needs `npm install`, an Electron "
    "package build, and a display server; its own harness is "
    "apps/desktop/playwright.config.ts, which is a Node test suite, not a "
    "pytest one. Its Python backend is the same tui_gateway dispatcher the TUI "
    "test below drives (over WebSocket rather than stdio), so the backend is "
    "covered and the renderer is not. Runnable by adding a Node e2e job that "
    "builds the app -- out of scope for a thin smoke suite."
)


def _scripted_corpus_skip(stack: LiveStack) -> str | None:
    """Why a surface that calls non-streaming cannot complete here, or None."""
    if stack.upstream is None:
        return None
    return (
        "conversation completion not provable on the scripted upstream: this "
        "surface calls chat/completions with streaming disabled (no `stream` "
        "key in the recorded body), and every fixture in tests/fixtures/sse/ is "
        "a text/event-stream body. The OpenAI SDK then fails to parse the "
        "reply with `vars() argument must have __dict__ attribute`. That is a "
        "gap in the fixture corpus, not a gateway defect -- the request reaches "
        f"the upstream intact, which the sibling test asserts. Runnable by "
        f"setting {UPSTREAM_ENV_VAR} to a real OpenAI-compatible endpoint, or "
        "by adding a non-streaming application/json fixture to the corpus "
        "(owned by plan 02-03)."
    )


# ---------------------------------------------------------------------------
# Surface drivers
# ---------------------------------------------------------------------------


def _drive_cli(stack: LiveStack, prompt: str, *, expect_tool: bool) -> SurfaceRun:
    """One non-interactive CLI conversation: `hermes -z <prompt> --cli`.

    ``--cli`` is explicit rather than implied. ``hermes_cli.main`` decides
    between the TUI and the CLI before argument parsing, and while a piped
    stdin already forces the CLI, relying on that would make this test's
    identity depend on how pytest happens to capture output.
    """
    argv = hermes_module_argv(stack, "hermes_cli.main", "-z", prompt, "--cli")
    if expect_tool:
        # Without it Hermes prompts for tool approval and there is no TTY to
        # answer, so the turn would stall until the timeout and the test would
        # report "tool never fired" for the wrong reason.
        argv.append("--yolo")
    returncode, stdout, stderr = run_child(
        stack, argv, stop_when_tool_result=expect_tool
    )
    return SurfaceRun(
        surface="cli",
        scenario="tool" if expect_tool else "text",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        upstream_requests=snapshot_requests(stack),
        log_text=read_log(stack),
    )


def _drive_tui(stack: LiveStack, prompt: str, *, expect_tool: bool) -> SurfaceRun:
    """One TUI conversation over the TUI gateway's own JSON-RPC protocol.

    ``tui_gateway/entry.py`` is the Python half of the TUI: it emits a
    ``gateway.ready`` event and then reads line-delimited JSON-RPC from stdin.
    The Ink/Node front end is a *client* of this protocol, so driving the
    protocol exercises the whole Hermes-side path -- session creation, agent
    build, provider resolution, the turn -- without a terminal. A curses-style
    screen driver would test the renderer and nothing else, and would not run
    headless at all.
    """
    argv = hermes_module_argv(stack, "tui_gateway.entry")
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        cwd=str(stack.hermes.root),
        env=dict(stack.child_env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    received: list[str] = []

    def pump(stream: Any, sink: list[str]) -> None:
        try:
            for line in stream:
                sink.append(line.rstrip("\r\n"))
        except (OSError, ValueError):  # pragma: no cover - stream closed early
            pass

    errors: list[str] = []
    threading.Thread(target=pump, args=(process.stdout, received), daemon=True).start()
    threading.Thread(target=pump, args=(process.stderr, errors), daemon=True).start()

    def send(payload: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def await_response(request_id: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in list(received):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
            if process.poll() is not None:
                return None
            time.sleep(0.2)
        return None

    try:
        # Wait for gateway.ready before speaking, so a slow import is not
        # mistaken for an unanswered request.
        ready_deadline = time.monotonic() + 90
        while time.monotonic() < ready_deadline:
            if any("gateway.ready" in line for line in received):
                break
            time.sleep(0.2)

        send(
            {
                "jsonrpc": "2.0",
                "id": "create",
                "method": "session.create",
                "params": {"cols": 100},
            }
        )
        created = await_response("create", 120)
        result = (created or {}).get("result") or {}
        session_id = result.get("session_id") or result.get("id") or result.get("sid")
        if not session_id:
            # Reported as a run, not raised: the assertions below name the
            # property that failed, and the raw exchange goes into the report.
            received.append(json.dumps({"e2e_error": "session.create returned no id"}))
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": "turn",
                    "method": "prompt.submit",
                    "params": {"session_id": session_id, "text": prompt},
                }
            )
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                if expect_tool:
                    from tests.e2e.conftest import saw_tool_result

                    if saw_tool_result(stack):
                        break
                elif any(EXPECTED_REPLY in line for line in list(received)):
                    break
                time.sleep(0.2)
    finally:
        if process.poll() is None:
            process.kill()
        with contextlib.suppress(Exception):
            process.communicate(timeout=60)

    return SurfaceRun(
        surface="tui",
        scenario="tool" if expect_tool else "text",
        returncode=process.returncode,
        stdout="\n".join(received),
        stderr="\n".join(errors),
        upstream_requests=snapshot_requests(stack),
        log_text=read_log(stack),
    )


def _drive_cron(stack: LiveStack, prompt: str, *, expect_tool: bool) -> SurfaceRun:
    """One scheduled run through Hermes's own cron machinery.

    Deliberately **not** the CLI path wearing a cron hat. ``hermes cron create``
    writes a durable job and ``hermes cron run <id>`` executes it through
    ``cron/`` -- a different, non-interactive entry point with no display
    consumer, which is exactly why it is worth a separate surface: it is the
    configuration and resolution chain a user never watches happen.
    """
    created = subprocess.run(  # noqa: S603 - fixed argv, no shell
        hermes_module_argv(
            stack,
            "hermes_cli.main",
            "cron",
            "create",
            "--name",
            "hermes-auto-e2e",
            "--deliver",
            "local",
            "1h",
            prompt,
        ),
        cwd=str(stack.hermes.root),
        env=dict(stack.child_env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    job_id = ""
    for line in (created.stdout or "").splitlines():
        if line.startswith("Created job:"):
            job_id = line.split(":", 1)[1].strip()
    if not job_id:
        return SurfaceRun(
            surface="cron",
            scenario="tool" if expect_tool else "text",
            returncode=created.returncode,
            stdout=created.stdout or "",
            stderr=(created.stderr or "") + "\n[e2e] no job id in `cron create` output",
            upstream_requests=snapshot_requests(stack),
            log_text=read_log(stack),
        )

    argv = hermes_module_argv(stack, "hermes_cli.main", "cron", "run", job_id)
    returncode, stdout, stderr = run_child(
        stack, argv, stop_when_tool_result=expect_tool
    )
    return SurfaceRun(
        surface="cron",
        scenario="tool" if expect_tool else "text",
        returncode=returncode,
        stdout=created.stdout + "\n" + stdout,
        stderr=stderr,
        upstream_requests=snapshot_requests(stack),
        log_text=read_log(stack),
    )


DRIVERS = {"cli": _drive_cli, "tui": _drive_tui, "cron": _drive_cron}

#: Surfaces driven here. ``gateway`` and ``desktop`` have their own tests below,
#: each skipping with its own reason -- they are absent from this map because
#: there is no driver to call, not because they were forgotten.
DRIVABLE_SURFACES = tuple(DRIVERS)


@pytest.fixture(scope="session")
def surface_run(live_stack: LiveStack):
    """Memoised driver: one subprocess per (surface, scenario), reused by tests.

    Memoised because each drive costs a real Hermes start -- tens of seconds --
    and four assertions about one exchange must not cost four exchanges. The
    cache also makes the tests order-independent despite the scripted upstream
    being sticky: whichever test asks first pays for the run and re-scripts the
    upstream itself.
    """
    cache: dict[tuple[str, str], SurfaceRun] = {}

    def run(surface: str, scenario: str) -> SurfaceRun:
        key = (surface, scenario)
        if key not in cache:
            fixture = "single_tool_call" if scenario == "tool" else "text_stream"
            script_upstream(live_stack, fixture)
            prompt = (
                f"{CANARY_PROMPT} Use the {EXPECTED_TOOL_NAME} tool for Testville."
                if scenario == "tool"
                else CANARY_PROMPT
            )
            cache[key] = DRIVERS[surface](
                live_stack, prompt, expect_tool=scenario == "tool"
            )
        return cache[key]

    return run


# ---------------------------------------------------------------------------
# 1. Provider resolution -- the thing pip install does not do
# ---------------------------------------------------------------------------


def test_hermes_resolves_the_installed_provider_shim(live_stack: LiveStack) -> None:
    """Hermes finds ``hermes-auto`` by directory scan and can build its profile.

    Asked of Hermes's **own** interpreter with the temporary ``HERMES_HOME``,
    because ``providers`` is not importable from this project's virtualenv --
    the isolation constraint working as designed, per 02-CONTEXT.md item 4.
    """
    probe = (
        "import json, providers\n"
        "p = providers.get_provider_profile('hermes-auto')\n"
        "print(json.dumps({\n"
        "    'name': p.name,\n"
        "    'base_url': p.base_url,\n"
        "    'models': list(p.fallback_models),\n"
        "    'extra': p.build_extra_body(session_id='e2e', model='auto:balanced'),\n"
        "}))\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(live_stack.hermes.python), "-c", probe],
        cwd=str(live_stack.hermes.root),
        env=dict(live_stack.child_env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["name"] == "hermes-auto"
    assert payload["base_url"] == live_stack.base_url
    assert VIRTUAL_MODEL in payload["models"]
    # An unbound-method registration -- ``register_provider(TheClass)`` -- passes
    # every check above and raises here, which is the failure 02-05 found is
    # worse than "unknown provider" because it survives setup.
    assert payload["extra"]["_hermes_auto"]["virtual_model"] == "auto:balanced"


# ---------------------------------------------------------------------------
# 2-4. Per-surface behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", DRIVABLE_SURFACES)
def test_surface_reaches_the_provider(
    surface: str, live_stack: LiveStack, surface_run
) -> None:
    """The surface's request arrived at the upstream through our gateway.

    The weakest of the four claims and the first to check: it separates "this
    surface never resolved hermes-auto" from "it resolved it and the exchange
    then failed", which are different bugs with different owners.
    """
    run = surface_run(surface, "text")
    chat = run.chat_requests()
    assert chat, (
        f"{surface}: no chat/completions request reached the upstream.\n"
        f"stdout:\n{run.stdout[-3000:]}\nstderr:\n{run.stderr[-3000:]}"
    )
    body = chat[0].get("json") or {}
    assert body.get("model") == VIRTUAL_MODEL, pretty(body.get("model"))
    # The gateway pops the private envelope before forwarding. Seeing it here
    # would mean a routing key leaked to a third-party provider.
    assert "_hermes_auto" not in body, pretty(sorted(body))


@pytest.mark.parametrize("surface", DRIVABLE_SURFACES)
def test_surface_completes_a_conversation(
    surface: str, live_stack: LiveStack, surface_run
) -> None:
    """The assistant's reply came back through the surface the user watches."""
    run = surface_run(surface, "text")
    if not run.chat_requests():
        pytest.fail(f"{surface}: never reached the upstream; see the sibling test")

    body = run.chat_requests()[0].get("json") or {}
    if not body.get("stream"):
        reason = _scripted_corpus_skip(live_stack)
        if reason:
            pytest.skip(f"{surface}: {reason}")

    assert EXPECTED_REPLY in run.combined_output, (
        f"{surface}: the assistant reply never surfaced.\n"
        f"stdout:\n{run.stdout[-4000:]}\nstderr:\n{run.stderr[-4000:]}"
    )


@pytest.mark.parametrize("surface", DRIVABLE_SURFACES)
def test_surface_fires_a_tool_call_and_returns_its_result(
    surface: str, live_stack: LiveStack, surface_run
) -> None:
    """A fragmented tool call survives the relay, dispatches, and answers.

    The assertion is on the **next request**, not on rendered output: a tool
    result is returned to the model, and the model is upstream. Seeing
    ``tool_call_id`` come back proves the SDK reassembled the streamed
    ``tool_calls`` delta that the gateway relayed -- the single property most
    likely to break in an SSE relay.
    """
    text_run = surface_run(surface, "text")
    body = (text_run.chat_requests()[0].get("json") or {}) if text_run.chat_requests() else {}
    if not body.get("stream"):
        reason = _scripted_corpus_skip(live_stack)
        if reason:
            pytest.skip(f"{surface}: {reason}")

    run = surface_run(surface, "tool")
    results = run.tool_result_messages()
    assert results, (
        f"{surface}: no tool-role message was ever sent back upstream.\n"
        f"requests: {len(run.chat_requests())}\n"
        f"stdout:\n{run.stdout[-3000:]}\nstderr:\n{run.stderr[-3000:]}"
    )
    assert any(m.get("tool_call_id") == EXPECTED_TOOL_CALL_ID for m in results), pretty(
        [m.get("tool_call_id") for m in results]
    )
    assert any(m.get("name") == EXPECTED_TOOL_NAME for m in results), pretty(
        [m.get("name") for m in results]
    )


@pytest.mark.parametrize("surface", DRIVABLE_SURFACES)
def test_surface_leaves_no_raw_prompt_in_the_sidecar_log(
    surface: str, live_stack: LiveStack, surface_run
) -> None:
    """No prompt text and no banned key reached the sidecar log (criterion 7).

    Three assertions, because the first two alone would prove nothing.

    The **control** comes first: the canary must appear in the request the
    upstream actually received. Without it, "the canary is absent from the log"
    is satisfied by a run where the prompt was never sent at all -- which is
    precisely how a leak test passes on a broken pipeline. Only once the canary
    is known to have travelled does its absence from the log mean anything.

    Then the canary scan, which catches raw prompt text logged verbatim, and the
    key scan, which catches a structured record that logged a whole request body
    under a key the canary happens not to fall inside.

    Read ``README.md`` § "Known weakness" before reading a pass here as proof of
    the redaction boundary: the sidecar logs very little today, so this is a
    tripwire rather than a proof. The redaction sink itself is tested directly
    in ``tests/unit`` and ``tests/contract``.
    """
    run = surface_run(surface, "text")
    log = run.log_text

    transmitted = any(
        CANARY_PROMPT in (request.get("body") or b"").decode("utf-8", "replace")
        for request in run.chat_requests()
    )
    assert transmitted, (
        f"{surface}: the canary never reached the upstream, so its absence from "
        f"the log proves nothing. This test is vacuous unless this holds."
    )

    assert CANARY_PROMPT not in log, (
        f"{surface}: the prompt this test sent appears verbatim in "
        f"{live_stack.log_path}"
    )
    for line in log.splitlines():
        if not line.strip().startswith("{"):
            continue  # uvicorn's own prose lines carry no request payload
        try:
            record = json.loads(line)
        except ValueError:
            continue
        leaked = sorted(set(_keys(record)) & set(BANNED_LOG_KEYS))
        assert not leaked, f"{surface}: banned keys {leaked} in a log record: {line[:400]}"


def _keys(value: Any) -> list[str]:
    """Every mapping key anywhere inside *value*, however deeply nested."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append(str(key).lower())
            found.extend(_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_keys(child))
    return found


# ---------------------------------------------------------------------------
# Restart safety -- a claim about running processes, so proved with one
# ---------------------------------------------------------------------------


def test_sidecar_restart_mid_session_is_safe_and_leaves_no_stale_pid(
    live_stack: LiveStack,
) -> None:
    """Restart while a request is genuinely in flight, then prove three things.

    "Mid-session" is the load-bearing word and the reason this test is awkward.
    Against a scripted upstream that answers in microseconds there is no
    in-flight window to restart *into*, so a naive version of this test restarts
    an idle gateway and proves nothing. ``set_first_byte_delay`` holds the
    upstream response open, and the test does not restart until the upstream has
    actually recorded the request -- so the sidecar is demonstrably mid-relay
    when it is asked to stop.

    What is asserted afterwards:

    1. The ``instance_id`` changed. ``supervisor.restart`` waits for exactly
       this rather than for ``/healthz`` to answer, because a draining old
       process still answers and a first-successful-probe restart routinely
       returns the process it was asked to replace.
    2. No stale PID: the runtime file names the process that is answering now,
       and the old PID is not it.
    3. The restarted sidecar still serves traffic -- a fresh turn completes.

    What is deliberately **not** asserted: that the in-flight request survives.
    It does not, and it should not. A graceful drain finishes streams that are
    already flowing; this one is still waiting on its upstream's first byte, so
    the client sees a failure. "Safe" here means the supervision state is
    consistent and the next request works, not that a restart is invisible.
    """
    from hermes_auto import supervisor
    from hermes_auto.state.runtime import read_runtime

    if live_stack.upstream is None:
        pytest.skip(
            "restart-mid-session needs the scripted upstream's "
            "set_first_byte_delay to create an in-flight window; a real "
            f"upstream named by {UPSTREAM_ENV_VAR} answers on its own schedule "
            "and the restart would race an unknown latency"
        )

    before = supervisor.status(live_stack.config)
    assert before.running, before.detail
    old_instance, old_pid = before.instance_id, before.pid

    script_upstream(live_stack, "text_stream")
    live_stack.upstream.set_first_byte_delay(20.0)
    inflight = None
    try:
        inflight = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            hermes_module_argv(
                live_stack, "hermes_cli.main", "-z", "hold this open", "--cli"
            ),
            cwd=str(live_stack.hermes.root),
            env=dict(live_stack.child_env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_CONSOLE_WINDOW,
        )

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not live_stack.upstream.requests:
            if inflight.poll() is not None:
                break
            time.sleep(0.2)
        assert live_stack.upstream.requests, (
            "no request reached the upstream, so there was no in-flight window "
            "to restart into and this test would prove nothing"
        )

        after = supervisor.restart(live_stack.config)
    finally:
        live_stack.upstream.set_first_byte_delay(0.0)
        if inflight is not None:
            if inflight.poll() is None:
                inflight.kill()
            with contextlib.suppress(Exception):
                inflight.communicate(timeout=60)

    assert after.running, after.detail
    assert after.instance_id and after.instance_id != old_instance, (
        f"instance_id did not change across the restart: {old_instance}"
    )

    record = read_runtime(live_stack.state_dir)
    assert record is not None, "the runtime file is missing after a restart"
    assert record.instance_id == after.instance_id, (
        f"stale runtime file: it names {record.instance_id} while "
        f"{after.instance_id} is answering"
    )
    assert record.pid != old_pid, f"the runtime file still names the old pid {old_pid}"

    # The gateway that came back must actually serve, not merely answer /healthz.
    script_upstream(live_stack, "text_stream")
    returncode, stdout, stderr = run_child(
        live_stack,
        hermes_module_argv(live_stack, "hermes_cli.main", "-z", CANARY_PROMPT, "--cli"),
        stop_when_tool_result=False,
    )
    assert EXPECTED_REPLY in f"{stdout}\n{stderr}", (
        f"the restarted sidecar did not serve a fresh turn (rc={returncode}).\n"
        f"stdout:\n{stdout[-3000:]}\nstderr:\n{stderr[-3000:]}"
    )


# ---------------------------------------------------------------------------
# The two surfaces this environment cannot drive
# ---------------------------------------------------------------------------


def test_messaging_gateway_surface() -> None:
    """design.md §20.5's "Messaging gateway". Skips -- see the reason."""
    pytest.skip(GATEWAY_SKIP_REASON)


def test_desktop_surface() -> None:
    """design.md §20.5's "Desktop". Skips -- see the reason."""
    pytest.skip(DESKTOP_SKIP_REASON)
