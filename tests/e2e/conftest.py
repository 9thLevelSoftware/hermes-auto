"""Session fixtures for the live Hermes surface suite, and its skip discipline.

**A skip is never a pass.** This module's whole reason to exist is that an
unavailable dependency must be visibly distinguishable from a working one. Every
precondition failure names *what was missing*, so a reader of CI output can tell
"nobody configured this" from "the product is broken". ``pytest.skip("not
available")`` would collapse both into a number and is exactly the failure mode
Phase 1's review removed twice.

Four things must be true before a surface test can run:

1. ``HERMES_AUTO_E2E=1``. Opt-in, because the suite drives real subprocesses.
2. A real Hermes installation, found and interrogated through **its own
   interpreter**.
3. An OpenAI-compatible upstream the sidecar can reach.
4. The surface itself must be drivable on this platform without a human.

Point 2 is where this file departs from the plan text, deliberately and with
evidence. The plan says to gate on ``importlib.metadata.version("hermes-agent")``
raising. Run from *this* project's virtualenv that call **always** raises --
``02-CONTEXT.md`` § VERIFIED HERMES FACTS item 4 records it -- so the gate would
skip on every machine forever, including one with a perfectly good Hermes. That
is a permanently vacuous suite wearing a green tick. Hermes is installed; it is
installed *into its own virtualenv*, and asking that interpreter answers the
question the plan meant to ask:

    <hermes-root>/venv/Scripts/python.exe -c "importlib.metadata.version(...)"

which returns ``0.19.0`` on the development machine.

What the suite does **not** claim: it does not prove byte-level identity between
the gateway and a direct call. That is plan 02-08's differential harness, which
is the right tool for it. Here each surface answers one narrow question -- does a
conversation complete, and do tool calls survive the round trip -- through the
entry point a user actually touches.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import pathlib
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Environment knobs
# ---------------------------------------------------------------------------

#: Opt-in switch. Unset means skip, with a reason naming this variable.
E2E_ENV_VAR = "HERMES_AUTO_E2E"

#: Overrides the Hermes source checkout this suite drives.
HERMES_ROOT_ENV_VAR = "HERMES_AUTO_E2E_HERMES_ROOT"

#: Optional. Names a real OpenAI-compatible base URL. When set, the suite dials
#: it instead of the in-repo scripted upstream, and skips when it is unreachable.
UPSTREAM_ENV_VAR = "HERMES_AUTO_E2E_UPSTREAM"

#: Optional. The ``credential_ref`` to pair with ``HERMES_AUTO_E2E_UPSTREAM``,
#: in the ``env:NAME`` form ``config.py`` accepts. Never a credential value --
#: only the *name* of the variable holding one.
UPSTREAM_CREDENTIAL_ENV_VAR = "HERMES_AUTO_E2E_UPSTREAM_CREDENTIAL_REF"

#: Model name forwarded upstream. Irrelevant to the scripted upstream, which
#: replays bytes; load-bearing when ``HERMES_AUTO_E2E_UPSTREAM`` names a real one.
UPSTREAM_MODEL_ENV_VAR = "HERMES_AUTO_E2E_UPSTREAM_MODEL"

#: The virtual model every surface is pointed at.
VIRTUAL_MODEL = "auto:balanced"

#: Assistant text the ``text_stream`` fixture emits, in one piece. The surface
#: drivers look for it to prove the whole stream was relayed and reassembled.
EXPECTED_REPLY = "Hello, synthetic world."

#: ``single_tool_call``'s call id. A tool-role message carrying it in a later
#: request proves Hermes reassembled the fragmented tool call and dispatched it.
EXPECTED_TOOL_CALL_ID = "call_test0001"
EXPECTED_TOOL_NAME = "get_forecast"

#: How long any one surface gets to produce its upstream traffic.
SURFACE_TIMEOUT_SECONDS = float(os.environ.get("HERMES_AUTO_E2E_TIMEOUT", "240"))

#: Windows allocates a fresh console window for a console-subsystem child when
#: the parent has no console of its own, so an unattended test run flashes a
#: window per spawn. ``CREATE_NO_WINDOW`` suppresses it and does not exist on
#: POSIX, hence the ``getattr``. Deliberately *not* applied to the supervisor's
#: own spawn, which passes ``DETACHED_PROCESS`` -- that already suppresses
#: console allocation and MSDN specifies this flag is ignored alongside it.
_NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Reachability probe budget. Short *and* meaningful: 02-07 found that this
#: machine's local security product drops the SYN to an unused loopback port, so
#: an unreachable upstream times out rather than being refused. Catching
#: ``OSError`` covers both, and the timeout is what makes the timeout case fast.
PROBE_TIMEOUT_SECONDS = 3.0


# ---------------------------------------------------------------------------
# Precondition 1: opt-in
# ---------------------------------------------------------------------------


def e2e_opted_in() -> bool:
    return os.environ.get(E2E_ENV_VAR, "").strip() == "1"


OPT_IN_SKIP_REASON = (
    f"set {E2E_ENV_VAR}=1 to run live surface tests "
    f"(they spawn real Hermes processes and a real sidecar)"
)


# ---------------------------------------------------------------------------
# Precondition 2: a real Hermes installation
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HermesInstall:
    """A Hermes checkout this suite can drive, and the interpreter that runs it."""

    root: pathlib.Path
    python: pathlib.Path
    version: str


def _candidate_roots() -> list[pathlib.Path]:
    """Where a Hermes checkout might be, most explicit first."""
    override = os.environ.get(HERMES_ROOT_ENV_VAR, "").strip()
    if override:
        return [pathlib.Path(override)]

    roots: list[pathlib.Path] = []
    # ``default_hermes_home()`` is $HERMES_HOME (or the platform default). The
    # checkout is its *sibling-by-name* ``hermes-agent`` child -- confirmed on
    # the development machine and recorded in 02-05's summary. Reading it here,
    # once, at collection time, is also what keeps the real HERMES_HOME out of
    # the running fixture: nothing below ever writes to this path.
    with contextlib.suppress(Exception):
        from hermes_auto.hermes_shim.installer import default_hermes_home

        roots.append(default_hermes_home() / "hermes-agent")
    return roots


def _interpreters_for(root: pathlib.Path) -> list[pathlib.Path]:
    names = ("venv", ".venv")
    subpaths = (("Scripts", "python.exe"), ("bin", "python"))
    return [root / name / a / b for name in names for a, b in subpaths]


def discover_hermes() -> tuple[HermesInstall | None, str]:
    """Find a drivable Hermes, or explain precisely what was missing.

    Returns ``(install, reason)``. ``reason`` is empty on success and is the
    skip message otherwise. It always names a path that was looked at, because
    "hermes-agent not installed" without a path sends the reader hunting.
    """
    roots = _candidate_roots()
    if not roots:
        return None, (
            f"hermes-agent not installed: no candidate checkout; "
            f"set {HERMES_ROOT_ENV_VAR} to a Hermes source root"
        )

    looked: list[str] = []
    for root in roots:
        if not root.is_dir():
            looked.append(f"{root} (no such directory)")
            continue
        for python in _interpreters_for(root):
            if not python.exists():
                continue
            version = _hermes_version(python, root)
            if version is not None:
                return HermesInstall(root=root, python=python, version=version), ""
            looked.append(f"{python} (hermes-agent metadata not found)")
        looked.append(f"{root} (no virtualenv interpreter under venv/ or .venv/)")

    return None, (
        "hermes-agent not installed: " + "; ".join(looked) + f". "
        f"Set {HERMES_ROOT_ENV_VAR} to a Hermes source root whose virtualenv "
        f"has hermes-agent installed."
    )


def _hermes_version(python: pathlib.Path, root: pathlib.Path) -> str | None:
    """Ask *python* for its own ``hermes-agent`` version, or None.

    Deliberately a subprocess. ``importlib.metadata.version("hermes-agent")``
    evaluated in *this* interpreter answers a question about the router's
    virtualenv, where Hermes is absent by design -- the isolation constraint
    working, not a missing install.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                str(python),
                "-c",
                "import importlib.metadata as m; print(m.version('hermes-agent'))",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=_NO_CONSOLE_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    version = completed.stdout.strip()
    return version or None


# ---------------------------------------------------------------------------
# Precondition 3: a reachable upstream
# ---------------------------------------------------------------------------


def upstream_reachable(base_url: str) -> bool:
    """True when a TCP connection to *base_url*'s host and port succeeds.

    A connect, not an HTTP request: an authenticated endpoint answers 401 to an
    unauthenticated probe and that is still "reachable". ``OSError`` covers both
    a refusal and the timeout this machine produces instead of one.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    host = parts.hostname
    if not host:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Module-level gate
# ---------------------------------------------------------------------------


def require_live_stack_preconditions() -> HermesInstall:
    """Skip the module unless every precondition holds; else return the install.

    Called at module level by the surface suite so a whole file's worth of tests
    reports one specific reason instead of N copies of it.
    """
    if not e2e_opted_in():
        pytest.skip(OPT_IN_SKIP_REASON, allow_module_level=True)

    install, reason = discover_hermes()
    if install is None:
        pytest.skip(reason, allow_module_level=True)

    configured = os.environ.get(UPSTREAM_ENV_VAR, "").strip()
    if configured and not upstream_reachable(configured):
        pytest.skip(
            f"upstream unreachable at {configured} "
            f"(TCP connect failed or timed out within {PROBE_TIMEOUT_SECONDS}s)",
            allow_module_level=True,
        )
    return install


# ---------------------------------------------------------------------------
# The live stack
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LiveStack:
    """Everything a surface driver needs to talk to a running sidecar."""

    hermes: HermesInstall
    hermes_home: pathlib.Path
    state_dir: pathlib.Path
    base_url: str
    token: str
    port: int
    admin_port: int
    instance_id: str
    log_path: pathlib.Path
    upstream: Any
    child_env: Mapping[str, str]
    #: The resolved router configuration the sidecar was started from. Carried
    #: so a test can drive ``supervisor.restart`` against the *same* config the
    #: fixture used, rather than re-resolving it and risking a different
    #: state directory than the one actually running.
    config: Any


def _free_port() -> int:
    """An ephemeral port the OS has just released.

    Not ``port: 0``: Hermes's ``model.base_url`` is static configuration and
    cannot follow a port chosen after the config file is written, which is why
    ``02-CONTEXT.md`` forbids ephemeral binding outside the test-only escape
    hatch. A bind-then-release still races another process in principle; that is
    a transient, and a collision surfaces as a loud startup failure rather than
    a silent pass.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def live_stack(
    hermes_install: HermesInstall, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[LiveStack]:
    """Install the shim into a **temporary** HERMES_HOME and run a real sidecar.

    Teardown runs in ``finally`` and is unconditional. A failed e2e run that
    leaves a bound port and a stale runtime file does not fail the next run --
    it makes the next run fail *differently*, and the second bug is diagnosed
    instead of the first.

    Two environment hazards are handled explicitly, both found by plan 02-07:

    * ``HERMES_AUTO_STATE_DIR`` outranks configuration. If a developer has one
      exported, a fixture that only wrote ``state_dir`` into the config file
      would silently drive their real install. It is set, not merely trusted.
    * The real ``$HERMES_HOME`` is never written. The shim goes into a temp
      directory and the Hermes child processes are pointed at it.
    """
    import yaml

    from hermes_auto import supervisor
    from hermes_auto.config import load_config
    from hermes_auto.gateway.auth import read_token
    from hermes_auto.hermes_shim import install, uninstall
    from tests.integration.mock_upstream import MockUpstream

    # pytest's factory rather than ``tempfile.mkdtemp``: it keeps the last few
    # runs and prunes older ones, so a failed run's temporary HERMES_HOME, state
    # directory and sidecar log survive for post-mortem without the suite
    # littering the system temp directory once per invocation forever.
    tmp_root = tmp_path_factory.mktemp("hermes-auto-e2e")
    hermes_home = tmp_root / "hermes-home"
    state_dir = tmp_root / "state"
    hermes_home.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    port = _free_port()
    admin_port = _free_port()
    gateway_url = f"http://127.0.0.1:{port}"
    gateway_base_url = f"{gateway_url}/v1"

    scripted: MockUpstream | None = None
    configured_upstream = os.environ.get(UPSTREAM_ENV_VAR, "").strip()
    if configured_upstream:
        upstream_base_url = configured_upstream
        credential_ref = (
            os.environ.get(UPSTREAM_CREDENTIAL_ENV_VAR, "").strip() or "none"
        )
        upstream_model = os.environ.get(UPSTREAM_MODEL_ENV_VAR, "").strip() or "gpt-4o-mini"
    else:
        scripted = MockUpstream()
        upstream_base_url = scripted.url
        credential_ref = "none"
        upstream_model = "e2e-scripted-model"

    monkeypatch = pytest.MonkeyPatch()
    try:
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    # Read by Hermes: which provider and virtual model every
                    # surface should use, and where to dial it.
                    "model": {
                        "default": VIRTUAL_MODEL,
                        "provider": "hermes-auto",
                        "base_url": gateway_base_url,
                    },
                    # Read by this project. One file, two consumers -- which is
                    # exactly what design.md §16 describes.
                    "auto_router": {
                        "gateway": {
                            "url": gateway_url,
                            "port": port,
                            "admin_port": admin_port,
                            "state_dir": str(state_dir),
                        },
                        "upstream": {
                            "base_url": upstream_base_url,
                            "model": upstream_model,
                            "credential_ref": credential_ref,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        # The sidecar is spawned with ``sys.executable`` and inherits this
        # process's environment, so both variables must be set here rather than
        # passed. Restored by the MonkeyPatch undo below.
        monkeypatch.setenv("HERMES_AUTO_CONFIG", str(config_path))
        monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(state_dir))

        config = load_config(None)
        assert config.gateway.state_dir == state_dir, (
            "the resolved state dir is not the temporary one; refusing to run "
            "against a real install"
        )

        install(hermes_home, base_url=gateway_base_url)
        status = supervisor.start(config)
        try:
            token = read_token(config.gateway.state_dir) or ""
            assert token, "the gateway started without minting a bearer token"

            child_env = dict(os.environ)
            child_env["HERMES_HOME"] = str(hermes_home)
            child_env["HERMES_AUTO_ROUTER_TOKEN"] = token
            # Hermes has no business reading this project's own configuration,
            # and leaving them set would let a Hermes-side helper resolve the
            # router config by accident rather than through the shim.
            child_env.pop("HERMES_AUTO_CONFIG", None)
            child_env.pop("HERMES_AUTO_STATE_DIR", None)
            # Hermes prints box-drawing characters; the default Windows console
            # codepage cannot encode them and the child dies mid-write.
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["HERMES_ACCEPT_HOOKS"] = "1"

            yield LiveStack(
                hermes=hermes_install,
                hermes_home=hermes_home,
                state_dir=state_dir,
                base_url=gateway_base_url,
                token=token,
                port=port,
                admin_port=admin_port,
                instance_id=status.instance_id or "",
                log_path=supervisor.gateway_log_path(config),
                upstream=scripted,
                child_env=child_env,
                config=config,
            )
        finally:
            with contextlib.suppress(Exception):
                supervisor.stop(config)
            with contextlib.suppress(Exception):
                uninstall(hermes_home)
    finally:
        if scripted is not None:
            with contextlib.suppress(Exception):
                scripted.close()
        monkeypatch.undo()


@pytest.fixture(scope="session")
def hermes_install() -> HermesInstall:
    """The discovered Hermes installation. Skips with a specific reason."""
    return require_live_stack_preconditions()


# ---------------------------------------------------------------------------
# Driving a surface
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SurfaceRun:
    """What one drive of one surface produced."""

    surface: str
    scenario: str
    returncode: int | None
    stdout: str
    stderr: str
    upstream_requests: tuple[dict[str, Any], ...]
    log_text: str

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}"

    def chat_requests(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            request
            for request in self.upstream_requests
            if request.get("method") == "POST"
            and "chat/completions" in str(request.get("path", ""))
        )

    def tool_result_messages(self) -> tuple[dict[str, Any], ...]:
        found: list[dict[str, Any]] = []
        for request in self.chat_requests():
            body = request.get("json") or {}
            for message in body.get("messages", []) or []:
                if isinstance(message, dict) and message.get("role") == "tool":
                    found.append(message)
        return tuple(found)


def read_log(stack: LiveStack) -> str:
    """The sidecar's own log, or an empty string when it has written none yet."""
    try:
        return stack.log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def saw_tool_result(stack: LiveStack) -> bool:
    """True once the scripted upstream has received a tool-role message.

    The fixture corpus is *sticky*: ``MockUpstream.script`` replays the same
    bytes for every request, by design, because plan 02-08's fuzzer needs that.
    A tool-call fixture therefore answers the tool-result turn with another
    identical tool call, and an agent loop would run until its own iteration
    cap. Watching for the tool-result turn and stopping there is deterministic
    -- unlike re-scripting mid-conversation, which races the handler between
    recording a request and reading the script.
    """
    if stack.upstream is None:
        return False
    for request in stack.upstream.requests:
        body = request.get("json") or {}
        for message in body.get("messages", []) or []:
            if isinstance(message, dict) and message.get("role") == "tool":
                return True
    return False


def run_child(
    stack: LiveStack,
    argv: list[str],
    *,
    stop_when_tool_result: bool,
    timeout: float = SURFACE_TIMEOUT_SECONDS,
    stdin_lines: list[str] | None = None,
) -> tuple[int | None, str, str]:
    """Run a Hermes child to completion, or until the tool-result turn lands."""
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
        creationflags=_NO_CONSOLE_WINDOW,
    )
    if stdin_lines is not None and process.stdin is not None:
        for line in stdin_lines:
            process.stdin.write(line + "\n")
        process.stdin.flush()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if stop_when_tool_result and saw_tool_result(stack):
            break
        time.sleep(0.2)

    if process.poll() is None:
        process.kill()
    stdout, stderr = process.communicate(timeout=60)
    return process.returncode, stdout or "", stderr or ""


def script_upstream(stack: LiveStack, fixture: str) -> None:
    """Point the scripted upstream at *fixture* and forget prior traffic."""
    if stack.upstream is None:
        return
    stack.upstream.clear_requests()
    stack.upstream.script(fixture)


def snapshot_requests(stack: LiveStack) -> tuple[dict[str, Any], ...]:
    if stack.upstream is None:
        return ()
    return tuple(stack.upstream.requests)


def pretty(value: Any) -> str:
    with contextlib.suppress(Exception):
        return json.dumps(value, indent=2, default=str)[:4000]
    return repr(value)[:4000]


def hermes_module_argv(stack: LiveStack, module: str, *args: str) -> list[str]:
    return [str(stack.hermes.python), "-m", module, *args]


__all__ = [
    "E2E_ENV_VAR",
    "EXPECTED_REPLY",
    "EXPECTED_TOOL_CALL_ID",
    "EXPECTED_TOOL_NAME",
    "HERMES_ROOT_ENV_VAR",
    "OPT_IN_SKIP_REASON",
    "PROBE_TIMEOUT_SECONDS",
    "SURFACE_TIMEOUT_SECONDS",
    "UPSTREAM_ENV_VAR",
    "VIRTUAL_MODEL",
    "HermesInstall",
    "LiveStack",
    "SurfaceRun",
    "discover_hermes",
    "e2e_opted_in",
    "hermes_module_argv",
    "pretty",
    "read_log",
    "require_live_stack_preconditions",
    "run_child",
    "saw_tool_result",
    "script_upstream",
    "snapshot_requests",
    "upstream_reachable",
]
