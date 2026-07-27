"""The ``hermes-auto`` CLI surface, and the command bodies behind it.

Two things here are worth more than the rest.

**The plan's own verification commands for this work are mostly substring
greps, and substring greps over a docstring prove nothing.** Plan 02-06
demonstrated this by building a deliberately wrong module whose docstring said
the right words; five of its checks passed. The same exercise was run against
this plan's checks and **all five** of Task 1's passed against a module that
imports ``psutil`` and pipes both output streams. The AST-based tests below are
the replacements, and each one names the grep it replaces.

**Everything runs against an isolated state directory.** ``HERMES_AUTO_STATE_DIR``
outranks configuration precisely so a test process is structurally incapable of
writing into the developer's real install (``state/paths.py``), and
``--hermes-home`` keeps ``setup`` away from the real Hermes home the same way.
"""

from __future__ import annotations

import ast
import inspect
import io
import pathlib
import textwrap

import pytest

from hermes_auto import commands, supervisor
from hermes_auto.cli import build_parser, main
from hermes_auto.commands import CONTROL_PLUGIN_NAME, LEVEL_FAIL, LEVEL_OK

REQUIRED_SUBCOMMANDS = {"setup", "start", "stop", "restart", "status", "doctor"}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Point every path this module touches at ``tmp_path``."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(state))
    config = tmp_path / "auto-config.yaml"
    config.write_text(
        'auto_router:\n  gateway:\n    url: "http://127.0.0.1:18999"\n'
        "    port: 18999\n    admin_port: 19000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_AUTO_CONFIG", str(config))
    return state


@pytest.fixture
def hermes_home(tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# Parser surface
# ---------------------------------------------------------------------------


def test_parser_exposes_all_six_subcommands() -> None:
    parser = build_parser()
    names = set(parser._subparsers._group_actions[0].choices)
    assert REQUIRED_SUBCOMMANDS <= names, sorted(REQUIRED_SUBCOMMANDS - names)


@pytest.mark.parametrize("name", sorted(REQUIRED_SUBCOMMANDS))
def test_every_subcommand_is_wired_to_a_handler(name: str) -> None:
    """Parsing a subcommand must yield something to call.

    A subparser registered without ``set_defaults(func=...)`` parses cleanly and
    then does nothing, which looks like success from the shell.
    """
    args = build_parser().parse_args([name])
    assert callable(getattr(args, "func", None)), f"{name} has no handler"


def test_bare_invocation_prints_help_and_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "COMMAND" in capsys.readouterr().out


def test_status_exits_zero_when_the_gateway_is_not_running() -> None:
    """Not running is a true answer, not a command failure."""
    assert main(["status"]) == 0


def test_status_reports_the_not_started_state() -> None:
    buffer = io.StringIO()
    assert commands.cmd_status(stream=buffer) == 0
    assert "not running" in buffer.getvalue()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_fails_when_the_shim_is_absent_but_still_runs_every_check(
    hermes_home: pathlib.Path,
) -> None:
    """The behaviour the plan names explicitly.

    A diagnostic that aborts on the first failure withholds the checks that
    explain it, so the assertion is not merely "exit code 1" -- it is that the
    checks *after* the failing one are present in the output.
    """
    buffer = io.StringIO()
    code = commands.cmd_doctor(hermes_home=hermes_home, stream=buffer)
    assert code == 1

    checks = commands.run_doctor(hermes_home=hermes_home)
    names = [check.name for check in checks]
    assert "shim" in names
    shim = next(check for check in checks if check.name == "shim")
    assert shim.level == LEVEL_FAIL

    # Checks that come after the failure must have run anyway.
    for later in ("token", "admin token", "gateway", "upstream"):
        assert later in names, f"doctor stopped before reaching {later!r}"
    assert names.index("shim") < names.index("gateway")


def test_doctor_reports_every_check_in_its_printed_output(
    hermes_home: pathlib.Path,
) -> None:
    buffer = io.StringIO()
    commands.cmd_doctor(hermes_home=hermes_home, stream=buffer)
    printed = buffer.getvalue()
    for check in commands.run_doctor(hermes_home=hermes_home):
        assert check.name in printed


def test_doctor_reads_effective_token_permissions_rather_than_assuming(
    hermes_home: pathlib.Path, isolated_state: pathlib.Path
) -> None:
    """``os.chmod`` is a no-op on Windows ACLs, so the write is not the proof.

    Non-vacuous by construction: the permission check must be *absent* before a
    token exists and *present* after, and its detail must come from the readback
    helper rather than from a constant.
    """
    before = {check.name for check in commands.run_doctor(hermes_home=hermes_home)}
    assert "token permissions" not in before

    from hermes_auto.gateway.auth import mint_token, token_permissions_ok

    mint_token(None)
    checks = {c.name: c for c in commands.run_doctor(hermes_home=hermes_home)}
    assert "token permissions" in checks
    expected_ok, expected_reason = token_permissions_ok(None)
    assert checks["token permissions"].detail == expected_reason
    assert checks["token permissions"].level == (LEVEL_OK if expected_ok else LEVEL_FAIL)


def test_doctor_mints_a_missing_admin_token_and_does_not_re_mint_it(
    hermes_home: pathlib.Path,
) -> None:
    """02-06 asked for ``setup``/``doctor`` to mint this, and why.

    A gateway started before the admin API existed never minted an admin token,
    so ``stop`` has no credential for ``POST /admin/v1/shutdown`` and silently
    degrades to a hard kill that severs in-flight streams. Safe under a running
    gateway because ``gateway/admin.py`` reads the token per request.
    """
    from hermes_auto.gateway.admin import read_admin_token

    assert read_admin_token(None) is None
    checks = {c.name: c for c in commands.run_doctor(hermes_home=hermes_home)}
    minted = read_admin_token(None)
    assert minted, "doctor left the gateway unable to shut down gracefully"
    assert "minted one now" in checks["admin token"].detail
    assert "admin token permissions" in checks

    # Idempotent: a second run must not rotate a token a running gateway is using.
    commands.run_doctor(hermes_home=hermes_home)
    assert read_admin_token(None) == minted


def test_doctor_never_re_mints_the_inference_token(hermes_home: pathlib.Path) -> None:
    """The asymmetry that matters.

    ``gateway/app.py``'s lifespan reads the inference token **once** at startup,
    so rotating it under a running gateway would lock out every caller until a
    restart. Only the admin token is read per request.
    """
    from hermes_auto.gateway.auth import mint_token, read_token

    original = mint_token(None)
    commands.run_doctor(hermes_home=hermes_home)
    assert read_token(None) == original


def test_doctor_flags_a_stale_shim_by_comparing_plugin_version(
    hermes_home: pathlib.Path,
) -> None:
    """The drift nothing else detects.

    The installed file carries the version that wrote it, and upgrading this
    distribution does not rewrite it. 02-05 and 02-06 both asked for this check
    by name.
    """
    from hermes_auto.hermes_shim.installer import install

    install(hermes_home)
    fresh = {c.name: c for c in commands.run_doctor(hermes_home=hermes_home)}
    assert fresh["shim version"].level == LEVEL_OK

    from hermes_auto.hermes_shim.installer import installed_path

    target = installed_path(hermes_home)
    text = target.read_text(encoding="utf-8")
    target.write_text(
        text.replace("PLUGIN_VERSION = ", "PLUGIN_VERSION = '0.0.1-stale'  # ", 1),
        encoding="utf-8",
    )
    stale = {c.name: c for c in commands.run_doctor(hermes_home=hermes_home)}
    assert stale["shim version"].level == LEVEL_FAIL
    assert "0.0.1-stale" in stale["shim version"].detail


def test_installed_shim_version_reads_the_real_installed_file(
    hermes_home: pathlib.Path,
) -> None:
    from hermes_auto.hermes_shim.installer import install
    from hermes_auto.version import __version__

    target = install(hermes_home)
    assert commands.installed_shim_version(target.read_text(encoding="utf-8")) == __version__


def test_installed_shim_version_survives_a_file_it_cannot_parse() -> None:
    assert commands.installed_shim_version("def (((") is None
    assert commands.installed_shim_version("X = 1") is None


# ---------------------------------------------------------------------------
# Enabling the control plugin -- the bootstrap step
# ---------------------------------------------------------------------------


def _write_config(home: pathlib.Path, text: str) -> pathlib.Path:
    path = home / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_enabling_adds_a_plugins_block_when_there_is_none(
    hermes_home: pathlib.Path,
) -> None:
    path = _write_config(hermes_home, 'model:\n  name: gpt-5\nterminal:\n  cwd: "~"\n')
    ok, detail = commands.enable_control_plugin(hermes_home)
    assert ok, detail
    assert commands.control_plugin_enabled(hermes_home)[0]

    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["plugins"]["enabled"] == [CONTROL_PLUGIN_NAME]
    # Everything that was there before is still there, unreflowed.
    assert document["model"] == {"name": "gpt-5"}
    assert 'cwd: "~"' in path.read_text(encoding="utf-8")


def test_enabling_appends_to_an_existing_enabled_list(hermes_home: pathlib.Path) -> None:
    path = _write_config(
        hermes_home,
        """\
        plugins:
          enabled:
            - some-other-plugin
        model:
          name: gpt-5
        """,
    )
    ok, _ = commands.enable_control_plugin(hermes_home)
    assert ok

    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["plugins"]["enabled"] == ["some-other-plugin", CONTROL_PLUGIN_NAME]
    assert document["model"] == {"name": "gpt-5"}


def test_enabling_adds_the_enabled_key_under_an_existing_plugins_block(
    hermes_home: pathlib.Path,
) -> None:
    path = _write_config(
        hermes_home,
        """\
        plugins:
          disabled:
            - nope
        """,
    )
    ok, _ = commands.enable_control_plugin(hermes_home)
    assert ok

    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["plugins"]["enabled"] == [CONTROL_PLUGIN_NAME]
    assert document["plugins"]["disabled"] == ["nope"]


def test_enabling_is_idempotent(hermes_home: pathlib.Path) -> None:
    _write_config(hermes_home, "plugins:\n  enabled:\n    - hermes-auto-control\n")
    ok, detail = commands.enable_control_plugin(hermes_home)
    assert ok
    assert "listed" in detail

    import yaml

    document = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert document["plugins"]["enabled"].count(CONTROL_PLUGIN_NAME) == 1


def test_enabling_refuses_an_inline_list_and_names_the_manual_command(
    hermes_home: pathlib.Path,
) -> None:
    """Refusing to edit is a supported outcome; guessing at someone's YAML is not."""
    original = "plugins: {enabled: [other]}\n"
    path = _write_config(hermes_home, original)
    ok, detail = commands.enable_control_plugin(hermes_home)
    assert not ok
    assert f"hermes plugins enable {CONTROL_PLUGIN_NAME}" in detail
    assert path.read_text(encoding="utf-8") == original, "the file must be untouched"


def test_enabling_refuses_a_missing_or_broken_config(hermes_home: pathlib.Path) -> None:
    ok, detail = commands.enable_control_plugin(hermes_home)
    assert not ok
    assert f"hermes plugins enable {CONTROL_PLUGIN_NAME}" in detail

    _write_config(hermes_home, "plugins: [this is\n  not: valid: yaml\n")
    ok, detail = commands.enable_control_plugin(hermes_home)
    assert not ok
    assert f"hermes plugins enable {CONTROL_PLUGIN_NAME}" in detail


def test_the_enabled_name_matches_the_entry_point_key() -> None:
    """Hermes matches ``plugins.enabled`` against the entry-point name.

    ``_scan_entry_points`` builds its manifest with ``name=ep.name``. If the
    key written into config.yaml and the key in ``pyproject.toml`` ever differ,
    the plugin is discovered, reported as "not enabled in config", and never
    registers -- while both files look individually correct.
    """
    import tomllib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    with (repo_root / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    entry_points = document["project"]["entry-points"]["hermes_agent.plugins"]
    assert CONTROL_PLUGIN_NAME in entry_points
    assert entry_points[CONTROL_PLUGIN_NAME] == "hermes_auto.plugin"

    from hermes_auto.plugin import PLUGIN_NAME

    assert PLUGIN_NAME == CONTROL_PLUGIN_NAME


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def test_setup_installs_the_shim_and_says_why_that_step_is_required(
    hermes_home: pathlib.Path,
) -> None:
    _write_config(hermes_home, "model:\n  name: gpt-5\n")
    buffer = io.StringIO()
    code = commands.cmd_setup(hermes_home=hermes_home, stream=buffer)
    printed = buffer.getvalue()

    from hermes_auto.hermes_shim.installer import installed_path

    assert installed_path(hermes_home).exists()
    assert code == 0, printed

    # The explanation is the point: a user who skips setup sees "unknown
    # provider hermes-auto" with nothing connecting it to this step.
    assert "registers the provider" in printed
    assert "scanning" in printed
    assert "pip install" in printed
    # And the bootstrap ordering, which is why the console script exists.
    assert "console script" in printed


def test_setup_mints_both_tokens_so_stop_can_reach_the_admin_api(
    hermes_home: pathlib.Path, isolated_state: pathlib.Path
) -> None:
    """02-06 asked for this by name.

    Nothing outside the gateway's own lifespan minted the admin token, so
    ``stop`` against a gateway started before that landed had no credential for
    ``POST /admin/v1/shutdown`` and degraded to a hard kill.
    """
    from hermes_auto.gateway.admin import read_admin_token
    from hermes_auto.gateway.auth import read_token

    assert read_token(None) is None
    assert read_admin_token(None) is None

    _write_config(hermes_home, "model:\n  name: gpt-5\n")
    commands.cmd_setup(hermes_home=hermes_home, stream=io.StringIO())

    inference = read_token(None)
    admin = read_admin_token(None)
    assert inference and admin
    assert inference != admin, "the admin scope must not share the inference secret"


def test_setup_does_not_re_mint_an_existing_inference_token(
    hermes_home: pathlib.Path,
) -> None:
    """Re-minting would invalidate the token a running gateway is enforcing.

    ``gateway/app.py``'s lifespan reads the inference token **once** at startup,
    unlike the admin token which is read per request.
    """
    from hermes_auto.gateway.auth import mint_token, read_token

    original = mint_token(None)
    _write_config(hermes_home, "model:\n  name: gpt-5\n")
    commands.cmd_setup(hermes_home=hermes_home, stream=io.StringIO())
    assert read_token(None) == original


def test_setup_reports_a_problem_when_the_plugin_cannot_be_enabled(
    hermes_home: pathlib.Path,
) -> None:
    _write_config(hermes_home, "plugins: {enabled: [other]}\n")
    buffer = io.StringIO()
    code = commands.cmd_setup(hermes_home=hermes_home, stream=buffer)
    assert code == 1
    assert f"hermes plugins enable {CONTROL_PLUGIN_NAME}" in buffer.getvalue()


# ---------------------------------------------------------------------------
# The control plugin's registration surface
# ---------------------------------------------------------------------------


class _RecordingContext:
    """Records what a plugin registers, the way Hermes's PluginContext would."""

    def __init__(self) -> None:
        self.cli: dict[str, dict[str, object]] = {}
        self.slash: dict[str, dict[str, object]] = {}
        self.hooks: dict[str, list[object]] = {}

    def register_cli_command(self, name: str, **kwargs: object) -> None:
        self.cli[name] = kwargs

    def register_command(self, name: str, handler: object, **kwargs: object) -> None:
        self.slash[name.lstrip("/")] = {"handler": handler, **kwargs}

    def register_hook(self, hook_name: str, callback: object) -> None:
        self.hooks.setdefault(hook_name, []).append(callback)


def test_register_wires_the_cli_the_slash_command_and_the_hook() -> None:
    from hermes_auto import plugin

    ctx = _RecordingContext()
    plugin.register(ctx)

    assert "auto" in ctx.cli
    assert callable(ctx.cli["auto"]["setup_fn"])
    assert callable(ctx.cli["auto"]["handler_fn"])
    assert "auto" in ctx.slash
    assert ctx.hooks[plugin.HOOK_NAME] == [plugin.on_session_start]


def test_the_plugin_cli_subparser_matches_the_console_script() -> None:
    """``hermes auto status`` and ``hermes-auto status`` must not drift."""
    import argparse

    from hermes_auto import plugin

    ctx = _RecordingContext()
    plugin.register(ctx)
    parser = argparse.ArgumentParser(prog="hermes auto")
    ctx.cli["auto"]["setup_fn"](parser)  # type: ignore[operator]
    names = set(parser._subparsers._group_actions[0].choices)
    assert REQUIRED_SUBCOMMANDS <= names
    assert names == set(build_parser()._subparsers._group_actions[0].choices)


def test_only_the_slash_command_backed_by_real_behaviour_is_registered() -> None:
    """Routing does not exist in this phase, so nothing may report it.

    A command that answered ``/auto explain`` today would invent a decision that
    was never made, and a user who trusts it once keeps trusting it after real
    routing lands and the answers change meaning.
    """
    from hermes_auto import plugin

    ctx = _RecordingContext()
    plugin.register(ctx)
    assert set(ctx.slash) == {"auto"}

    for unimplemented in ("mode", "explain", "candidates", "pin", "reroute", "feedback"):
        reply = plugin.slash_auto(unimplemented)
        assert "not available" in reply, unimplemented


def _hermes_valid_hooks() -> set[str] | None:
    """Read ``VALID_HOOKS`` out of the real Hermes checkout, without importing it.

    Parsed rather than imported: importing ``hermes_cli.plugins`` drags in the
    whole Hermes runtime, which is not importable from this virtualenv at all
    (02-CONTEXT § VERIFIED HERMES FACTS item 4).
    """
    from hermes_auto.hermes_shim.installer import default_hermes_home

    source = default_hermes_home() / "hermes-agent" / "hermes_cli" / "plugins.py"
    if not source.exists():
        return None
    tree = ast.parse(source.read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == "VALID_HOOKS" and isinstance(value, ast.Set):
            return {
                element.value
                for element in value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    return None


def test_the_hook_name_is_one_hermes_will_actually_fire() -> None:
    """A wrong hook name fails *silently*, which is why this test exists.

    ``register_hook`` logs a warning for a name outside ``VALID_HOOKS`` and then
    stores the callback anyway, where nothing will ever call it. The plugin
    would load cleanly, report a registered hook, and never auto-start the
    gateway.
    """
    valid = _hermes_valid_hooks()
    if valid is None:
        pytest.skip("the Hermes checkout is not available on this machine")

    from hermes_auto.plugin import HOOK_NAME

    assert HOOK_NAME in valid, sorted(valid)
    # Non-vacuous: the parse really did find a populated hook set.
    assert len(valid) > 5


def test_register_survives_a_context_missing_one_registration_method() -> None:
    """A partial Hermes API must not cost the registrations that do work."""
    from hermes_auto import plugin

    class Partial(_RecordingContext):
        def register_command(self, *args: object, **kwargs: object) -> None:
            raise AttributeError("this Hermes build has no slash commands")

    ctx = Partial()
    plugin.register(ctx)  # must not raise
    assert "auto" in ctx.cli
    assert ctx.hooks[plugin.HOOK_NAME]


# ---------------------------------------------------------------------------
# Replacements for the plan's vacuous grep checks
# ---------------------------------------------------------------------------


def _supervisor_tree() -> ast.Module:
    return ast.parse(pathlib.Path(inspect.getfile(supervisor)).read_text(encoding="utf-8"))


def test_supervisor_imports_no_process_table_dependency() -> None:
    """Replaces ``assert 'psutil' not in src``.

    That grep is wrong in both directions, and both were observed. It fired on
    the module's own docstring saying the dependency is *not* used, and it
    passes against ``importlib.import_module("ps" + "util")``. This asks the
    import graph.
    """
    banned = {"psutil", "pywin32", "win32api", "win32process", "wmi"}
    imported: set[str] = set()
    for node in ast.walk(_supervisor_tree()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # The dynamic-import escape the grep cannot see.
            if node.func.attr == "import_module":
                raise AssertionError("supervisor must not import modules dynamically")
    assert not (imported & banned), sorted(imported & banned)

    import tomllib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    with (repo_root / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    declared = " ".join(document["project"]["dependencies"])
    assert "psutil" not in declared


def test_supervisor_never_routes_child_output_to_a_pipe() -> None:
    """Replaces ``assert 'PIPE' not in src``.

    A detached sidecar has nobody draining a pipe, so a full buffer blocks it
    forever. The grep passes against ``getattr(subprocess, "PI" + "PE")``; this
    asserts what the ``Popen`` call is actually given.
    """
    tree = _supervisor_tree()
    popen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    assert popen_calls, "supervisor must spawn the sidecar with subprocess.Popen"
    for call in popen_calls:
        keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        for stream in ("stdout", "stderr"):
            assert stream in keywords, f"Popen must set {stream} explicitly"
            value = keywords[stream]
            # A Name (the opened file handle) is what we want; an attribute
            # access on subprocess would be PIPE/DEVNULL/STDOUT.
            assert isinstance(value, ast.Name), (
                f"{stream} must be an opened file object, not {ast.dump(value)}"
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "PIPE":
            raise AssertionError("subprocess.PIPE must never be referenced")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "getattr" and len(node.args) >= 2:
                second = node.args[1]
                if isinstance(second, ast.BinOp):
                    raise AssertionError("computed getattr name hides the target")


def test_supervisor_spawns_detached_with_the_job_object_fallback() -> None:
    """Replaces ``'DETACHED_PROCESS' in src or 'creationflags' in src``.

    The grep passes on a docstring. This asserts both Windows flag sets are
    actually constructed and that the breakaway variant is a *retry*, not the
    first attempt -- ``CREATE_BREAKAWAY_FROM_JOB`` fails with access denied when
    the containing job forbids breakaway, so leading with it breaks the common
    case to serve the CI one.
    """
    source = pathlib.Path(inspect.getfile(supervisor)).read_text(encoding="utf-8")
    first = source.index("detached | new_group")
    second = source.index("detached | new_group | breakaway")
    assert first < second, "the breakaway flag set must be the retry, not the first try"

    names = {
        node.args[1].value
        for node in ast.walk(_supervisor_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
    }
    assert {
        "DETACHED_PROCESS",
        "CREATE_NEW_PROCESS_GROUP",
        "CREATE_BREAKAWAY_FROM_JOB",
    } <= names


def test_stop_reaches_the_admin_shutdown_endpoint() -> None:
    """Replaces ``assert 'shutdown' in src``, which any docstring satisfies."""
    source = pathlib.Path(inspect.getfile(supervisor)).read_text(encoding="utf-8")
    assert "/admin/v1/shutdown" in source
    request = inspect.getsource(supervisor._request_admin_shutdown)
    assert 'method="POST"' in request
    assert "Authorization" in request
    # And it is tried before either signal.
    stop_source = inspect.getsource(supervisor.stop)
    assert stop_source.index("_request_admin_shutdown") < stop_source.index("_signal_pid")


def test_supervision_never_treats_the_pid_as_evidence_of_liveness() -> None:
    """Replaces ``assert 'instance_id' in src``.

    ``status`` must not consult the recorded pid at all; the whole design is
    that a recycled pid cannot masquerade as a live gateway.
    """
    status_source = inspect.getsource(supervisor.status)
    assert "instance_id" in status_source
    for node in ast.walk(ast.parse(textwrap.dedent(status_source))):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            assert name not in {"kill", "waitpid", "getpgid"}, (
                f"status() must not probe the process table ({name})"
            )
    # The pid is carried into the Status for reporting, and read nowhere else.
    signal_source = inspect.getsource(supervisor._signal_pid)
    assert "os.kill" in signal_source
    assert "os.kill" not in status_source
