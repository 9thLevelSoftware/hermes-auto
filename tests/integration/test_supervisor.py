"""Real process supervision: real subprocesses, real ports, real runtime files.

Supervision claims cannot be proved by grep. "Restart is safe mid-session and
leaves no stale PID" is a statement about a process that is running, so every
test here starts one. The gateway is spawned exactly the way ``hermes auto
start`` spawns it -- detached, output to a file -- and stopped the way ``stop``
stops it.

**Ephemeral ports throughout.** ``gateway.port: 0`` under
``HERMES_AUTO_TEST_EPHEMERAL=1`` means the port in the runtime file is a value
no test chose, so reaching a listener on it proves the *file* is right rather
than proving two constants match. It also makes these tests safe to run
alongside a developer's real gateway on 8787.

**Teardown always stops the sidecar**, including after a failure, because a
detached process that outlives a failed test holds a port and quietly breaks
every test after it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import pathlib
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest

from hermes_auto import supervisor
from hermes_auto.state.runtime import RuntimeFile, read_runtime, write_runtime

from .mock_upstream import MockUpstream

pytestmark = pytest.mark.supervisor

STARTUP_TIMEOUT = 60.0


@dataclasses.dataclass
class Harness:
    """One isolated install: its own state directory, config, and upstream."""

    state: pathlib.Path
    config_path: pathlib.Path
    upstream: MockUpstream

    @property
    def runtime_file(self) -> pathlib.Path:
        return self.state / "runtime" / "gateway.json"

    @property
    def log_file(self) -> pathlib.Path:
        return self.state / "logs" / supervisor.LOG_FILE_NAME

    def token(self) -> str:
        return (self.state / "token").read_text(encoding="utf-8").strip()

    def base_url(self, port: int) -> str:
        return f"http://127.0.0.1:{port}"


def _write_config(path: pathlib.Path, upstream_url: str) -> None:
    path.write_text(
        "auto_router:\n"
        "  gateway:\n"
        '    url: "http://127.0.0.1:0"\n'
        "    port: 0\n"
        "    admin_port: 0\n"
        f"    startup_timeout_seconds: {int(STARTUP_TIMEOUT)}\n"
        "  upstream:\n"
        f'    base_url: "{upstream_url}/v1"\n'
        '    model: "mock-model"\n'
        '    credential_ref: "none"\n',
        encoding="utf-8",
    )


@pytest.fixture
def harness(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Harness]:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(state))
    monkeypatch.setenv("HERMES_AUTO_TEST_EPHEMERAL", "1")

    with MockUpstream() as upstream:
        config_path = tmp_path / "config.yaml"
        _write_config(config_path, upstream.url)
        monkeypatch.setenv("HERMES_AUTO_CONFIG", str(config_path))
        try:
            yield Harness(state=state, config_path=config_path, upstream=upstream)
        finally:
            # Unconditional: a detached sidecar surviving a failed test holds a
            # port and breaks every test after it.
            with contextlib.suppress(Exception):
                supervisor.stop(timeout=30.0)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_start_produces_a_running_gateway_with_an_instance_id(harness: Harness) -> None:
    state = supervisor.start(timeout=STARTUP_TIMEOUT)
    assert state.running
    assert state.kind == supervisor.STATUS_RUNNING
    assert state.instance_id and len(state.instance_id) == 32
    assert state.port and state.port > 0
    assert state.pid and state.pid > 0

    record = read_runtime(None)
    assert record is not None
    assert record.instance_id == state.instance_id
    assert record.port == state.port
    # Both listeners came up: admin_port 0 is the sentinel for "no admin
    # listener", which would make graceful drain unreachable.
    assert record.admin_port != 0
    assert record.admin_port != record.port


def test_healthz_reports_the_answering_process_not_the_file(harness: Harness) -> None:
    """The property the whole PID-reuse defence rests on.

    If ``/healthz`` re-read the runtime file, every process sharing a state
    directory would echo the file's value, the mismatch branch would be
    unreachable, and the check would be decorative.
    """
    state = supervisor.start(timeout=STARTUP_TIMEOUT)
    response = httpx.get(f"http://127.0.0.1:{state.port}/healthz", timeout=10)
    assert response.status_code == 200
    assert response.json()["instance_id"] == state.instance_id

    # Corrupt the file's identity; /healthz must keep reporting the process's own.
    record = read_runtime(None)
    assert record is not None
    write_runtime(dataclasses.replace(record, instance_id="0" * 32), None)
    response = httpx.get(f"http://127.0.0.1:{state.port}/healthz", timeout=10)
    assert response.json()["instance_id"] == state.instance_id != "0" * 32

    # And supervision now correctly reports that the file names someone else.
    assert supervisor.status().kind == supervisor.STATUS_FOREIGN
    write_runtime(record, None)


def test_start_twice_is_a_no_op_returning_the_same_instance(harness: Harness) -> None:
    """A second Hermes session must not fail because the first started the gateway."""
    first = supervisor.start(timeout=STARTUP_TIMEOUT)
    second = supervisor.start(timeout=STARTUP_TIMEOUT)
    assert second.running
    assert second.instance_id == first.instance_id
    assert second.pid == first.pid


def test_stop_clears_the_runtime_file_and_reports_not_running(harness: Harness) -> None:
    supervisor.start(timeout=STARTUP_TIMEOUT)
    assert harness.runtime_file.exists()

    stopped = supervisor.stop(timeout=30.0)
    assert not stopped.running
    assert not harness.runtime_file.exists(), "the runtime file outlived its process"
    assert supervisor.status().kind == supervisor.STATUS_NOT_STARTED


def test_stop_uses_the_admin_api_before_any_signal(harness: Harness) -> None:
    """The primary stop path on every platform, because Windows has no SIGTERM."""
    supervisor.start(timeout=STARTUP_TIMEOUT)
    stopped = supervisor.stop(timeout=30.0)
    assert "admin shutdown accepted" in stopped.detail
    assert "servers_signalled=2" in stopped.detail
    # Graceful means no signal was needed at all.
    assert "terminate:" not in stopped.detail
    assert "kill:" not in stopped.detail


def test_stop_when_not_running_returns_cleanly(harness: Harness) -> None:
    result = supervisor.stop(timeout=10.0)
    assert not result.running


def test_restart_changes_the_instance_id(harness: Harness) -> None:
    """What makes "restart is safe mid-session, no stale PID" testable.

    Returning as soon as ``/healthz`` answers would pass against the *old*
    process during a slow drain, so the assertion is on identity, not liveness.
    """
    before = supervisor.start(timeout=STARTUP_TIMEOUT)
    after = supervisor.restart(timeout=STARTUP_TIMEOUT)

    assert after.running
    assert after.instance_id != before.instance_id
    assert after.pid != before.pid

    record = read_runtime(None)
    assert record is not None
    assert record.instance_id == after.instance_id
    assert record.pid == after.pid, "the runtime file still names the dead process"


def test_no_orphan_holds_the_port_after_stop(harness: Harness) -> None:
    """Rebinding the port is the proof; "status says stopped" is not.

    A process that lost its listener but is still alive, or one that exited
    without releasing the socket, both report as stopped. Only a successful
    bind proves the port was actually released.
    """
    state = supervisor.start(timeout=STARTUP_TIMEOUT)
    port = state.port
    assert port is not None
    supervisor.stop(timeout=30.0)

    deadline = time.monotonic() + 30.0
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
            return
        except OSError as exc:
            last_error = exc
        finally:
            sock.close()
        time.sleep(0.1)
    raise AssertionError(f"port {port} was never released after stop: {last_error}")


# ---------------------------------------------------------------------------
# The states supervision must distinguish without trusting a PID
# ---------------------------------------------------------------------------


def test_a_stale_runtime_file_is_detected_and_removed(harness: Harness) -> None:
    """The ordinary state of a machine after an unclean shutdown."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()

    write_runtime(
        RuntimeFile(
            pid=999_999,
            port=dead_port,
            admin_port=dead_port + 1,
            instance_id="a" * 32,
            started_at="2026-01-01T00:00:00+00:00",
            exe="python",
        ),
        None,
    )
    assert harness.runtime_file.exists()

    state = supervisor.status()
    assert not state.running
    assert state.kind == supervisor.STATUS_STALE_CLEANED
    assert not harness.runtime_file.exists(), "a stale runtime file must be removed"


def test_a_foreign_gateway_on_the_recorded_port_is_reported_not_killed(
    harness: Harness,
) -> None:
    """The case ``instance_id`` exists for.

    A live gateway answers ``/healthz`` with an identity that is not the one
    recorded -- exactly what a supervisor sees when its own process died and an
    unrelated one took the port, possibly inheriting its PID. It must report the
    mismatch, keep the runtime file as evidence, and above all **not** stop a
    process this install does not own.

    The mismatch is created by rewriting the recorded identity rather than by
    standing up a second state directory: ``HERMES_AUTO_STATE_DIR`` outranks
    configuration by design (``state/paths.py``), so a second state dir is not
    reachable from inside this process at all.
    """
    running = supervisor.start(timeout=STARTUP_TIMEOUT)
    assert running.port is not None and running.instance_id is not None

    genuine = read_runtime(None)
    assert genuine is not None
    write_runtime(dataclasses.replace(genuine, instance_id="b" * 32), None)

    state = supervisor.status()
    assert not state.running
    assert state.kind == supervisor.STATUS_FOREIGN
    assert "b" * 32 in state.detail and running.instance_id in state.detail

    # The evidence is preserved rather than cleaned away.
    assert harness.runtime_file.exists()

    # stop() must refuse rather than signal a PID it cannot vouch for.
    with pytest.raises(supervisor.SupervisorError):
        supervisor.stop(timeout=10.0)

    # And the process is untouched: it still answers, with its own identity.
    response = httpx.get(f"http://127.0.0.1:{running.port}/healthz", timeout=10)
    assert response.status_code == 200
    assert response.json()["instance_id"] == running.instance_id

    write_runtime(genuine, None)
    assert supervisor.status().running


def test_a_corrupt_runtime_file_is_reported_and_not_deleted(harness: Harness) -> None:
    """Absent and corrupt are different answers, and deleting hides the bug."""
    harness.runtime_file.parent.mkdir(parents=True, exist_ok=True)
    harness.runtime_file.write_text("{not json", encoding="utf-8")

    state = supervisor.status()
    assert not state.running
    assert state.kind == supervisor.STATUS_CORRUPT
    assert harness.runtime_file.exists()


# ---------------------------------------------------------------------------
# Graceful drain, and what the log may contain
# ---------------------------------------------------------------------------


def _chat_payload(prompt: str) -> dict[str, object]:
    return {
        "model": "auto:balanced",
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }


def test_stop_completes_an_in_flight_stream_rather_than_severing_it(
    harness: Harness,
) -> None:
    """uvicorn's graceful path, end to end.

    The request is held open by delaying the upstream's first byte, so ``stop``
    is called with a stream genuinely in flight. A severed stream shows up as a
    transport error or a body missing its terminator; a drained one arrives
    complete.
    """
    state = supervisor.start(timeout=STARTUP_TIMEOUT)
    assert state.port is not None
    harness.upstream.script("text_stream")
    harness.upstream.set_first_byte_delay(2.0)

    outcome: dict[str, object] = {}
    upstream_reached = threading.Event()

    def consume() -> None:
        try:
            with httpx.Client(timeout=60.0) as client:
                with client.stream(
                    "POST",
                    f"http://127.0.0.1:{state.port}/v1/chat/completions",
                    json=_chat_payload("supervision drain probe"),
                    headers={"Authorization": f"Bearer {harness.token()}"},
                ) as response:
                    outcome["status"] = response.status_code
                    upstream_reached.set()
                    outcome["body"] = b"".join(response.iter_raw())
        except Exception as exc:  # noqa: BLE001 - recorded, asserted on below
            upstream_reached.set()
            outcome["error"] = repr(exc)

    reader = threading.Thread(target=consume, name="drain-probe")
    reader.start()
    try:
        # Wait until the gateway has actually forwarded the request upstream,
        # so the stop below lands mid-flight rather than before or after.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not harness.upstream.requests:
            time.sleep(0.05)
        assert harness.upstream.requests, "the gateway never reached the upstream"

        supervisor.stop(timeout=45.0)
    finally:
        reader.join(timeout=60.0)

    assert not reader.is_alive(), "the streaming client never finished"
    assert "error" not in outcome, outcome.get("error")
    assert outcome["status"] == 200
    body = outcome["body"]
    assert isinstance(body, bytes) and body
    assert b"[DONE]" in body, "the stream was severed before its terminator"


def test_the_sidecar_log_captures_output_and_no_prompt_content(
    harness: Harness,
) -> None:
    """Output lands in a file, and the redaction boundary holds in a real process.

    The prompt is a string that appears nowhere else, so finding it in the log
    can only mean the gateway wrote it there.
    """
    state = supervisor.start(timeout=STARTUP_TIMEOUT)
    assert state.port is not None
    harness.upstream.script("text_stream")

    secret = "zx9-supervisor-prompt-canary-42"
    response = httpx.post(
        f"http://127.0.0.1:{state.port}/v1/chat/completions",
        json=_chat_payload(secret),
        headers={"Authorization": f"Bearer {harness.token()}"},
        timeout=60.0,
    )
    assert response.status_code == 200
    supervisor.stop(timeout=30.0)

    assert harness.log_file.exists(), "stdout/stderr never reached the log file"
    log = harness.log_file.read_text(encoding="utf-8", errors="replace")
    assert log.strip(), "the log file is empty"
    # It really is the sidecar's own output.
    assert "Started server process" in log or "Application startup complete" in log

    assert secret not in log, "raw prompt content reached the sidecar log"
    assert harness.token() not in log, "the bearer token reached the sidecar log"


def test_the_log_is_a_file_and_not_a_pipe_so_a_full_buffer_cannot_deadlock(
    harness: Harness,
) -> None:
    """A detached sidecar has nobody draining a pipe.

    Behavioural rather than structural: the log must keep growing across
    restarts, which a pipe -- with no reader -- could not do.
    """
    supervisor.start(timeout=STARTUP_TIMEOUT)
    first = harness.log_file.stat().st_size
    supervisor.restart(timeout=STARTUP_TIMEOUT)
    supervisor.stop(timeout=30.0)
    assert harness.log_file.stat().st_size > first


# ---------------------------------------------------------------------------
# The commands, against the same real process
# ---------------------------------------------------------------------------


def test_the_cli_drives_a_full_lifecycle(harness: Harness) -> None:
    from hermes_auto.cli import main

    assert main(["start"]) == 0
    assert supervisor.status().running
    assert main(["status"]) == 0
    assert main(["restart"]) == 0
    assert supervisor.status().running
    assert main(["stop"]) == 0
    assert not supervisor.status().running


def test_doctor_sees_a_running_gateway_and_probes_the_upstream(
    harness: Harness, tmp_path: pathlib.Path
) -> None:
    from hermes_auto import commands

    supervisor.start(timeout=STARTUP_TIMEOUT)
    checks = {c.name: c for c in commands.run_doctor(hermes_home=tmp_path / "no-hermes")}
    assert checks["gateway"].level == commands.LEVEL_OK
    assert "running on" in checks["gateway"].detail
    # /readyz reaches the mock upstream, which is up.
    assert checks["upstream"].level == commands.LEVEL_OK


def test_the_session_hook_starts_the_gateway_and_never_raises(
    harness: Harness,
) -> None:
    """``on_session_start`` must open a Hermes session even when it cannot help."""
    from hermes_auto import plugin

    assert not supervisor.status().running
    plugin.on_session_start(session_id="s-1")
    assert supervisor.status().running

    # Called again with a gateway already up: still a no-op, still silent.
    plugin.on_session_start(session_id="s-2")
    assert supervisor.status().running


def test_the_session_hook_swallows_a_broken_configuration(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_auto import plugin

    broken = harness.config_path.parent / "broken.yaml"
    broken.write_text("auto_router:\n  gateway:\n    port: not-a-number\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_AUTO_CONFIG", str(broken))

    # A configuration this broken makes the gateway unstartable. Hermes must
    # still open the session: the router degrades to an error on the next
    # inference request, which is where the user can act on it.
    assert plugin.on_session_start(session_id="s-1") is None
    assert not harness.runtime_file.exists(), "nothing should have been started"


def test_slash_auto_status_reports_real_state(harness: Harness) -> None:
    from hermes_auto import plugin

    assert "not running" in plugin.slash_auto("status")
    supervisor.start(timeout=STARTUP_TIMEOUT)
    assert "running on" in plugin.slash_auto("status")
    # Anything else says so rather than answering a different question.
    assert "not available" in plugin.slash_auto("explain")


def test_the_runtime_file_is_written_only_after_both_binds(harness: Harness) -> None:
    """Ordering is the contract: the file's existence means the ports answer."""
    state = supervisor.start(timeout=STARTUP_TIMEOUT)
    record = read_runtime(None)
    assert record is not None
    for port in (record.port, record.admin_port):
        sock = socket.socket()
        sock.settimeout(5.0)
        try:
            sock.connect(("127.0.0.1", port))
        finally:
            sock.close()
    assert json.loads(harness.runtime_file.read_text())["instance_id"] == state.instance_id
