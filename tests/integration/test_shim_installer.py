"""Prove the shim installs where Hermes scans, and registers when it does.

Every test writes under a temporary ``HERMES_HOME``. Nothing here touches the
real Hermes installation -- ``test_no_test_wrote_into_the_real_hermes_home``
pins that, because the whole project rests on making zero modifications to the
Hermes tree, and a test suite that quietly installed into it would be the first
violation.

The registration proof runs Hermes's own discovery in a subprocess. Hermes is a
source checkout, not an installed distribution, so ``import providers`` does
not resolve from this virtualenv's working directory; the subprocess runs with
its cwd at the checkout root, which is what puts ``providers`` on the path.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import types
from typing import Any

import pytest

from hermes_auto.gateway.schemas import build_validator
from hermes_auto.hermes_shim import installer
from hermes_auto.hermes_shim.installer import (
    InstallError,
    default_hermes_home,
    install,
    installed_path,
    uninstall,
)
from hermes_auto.hermes_shim.template import MARKER, SHIM_SOURCE, render
from hermes_auto.provider import ENVELOPE_KEY, build_envelope
from hermes_auto.version import __version__

METADATA_SCHEMA_ID = (
    "https://hermes-auto-router.dev/schema/routing/hermes-auto-metadata.v1.json"
)

DEFAULT_HERMES_CHECKOUT = pathlib.Path(
    r"C:/Users/dasbl/AppData/Local/hermes/hermes-agent"
)

EXPECTED_RELATIVE_PATH = ("plugins", "model-providers", "hermes-auto", "__init__.py")


def _hermes_checkout() -> pathlib.Path | None:
    configured = os.environ.get("HERMES_AGENT_REPO", "").strip()
    root = pathlib.Path(configured) if configured else DEFAULT_HERMES_CHECKOUT
    return root if (root / "providers" / "__init__.py").is_file() else None


def _hermes_interpreter(root: pathlib.Path) -> str:
    """Hermes's bundled interpreter when present, else this one.

    Either works: ``providers/__init__.py`` and ``providers/base.py`` import
    only the standard library. Hermes's own runtime is preferred because it is
    the interpreter the shim will really execute under.
    """
    found = sorted(root.glob(".hermes-runtime/python/*/*/python.exe"))
    return str(found[-1]) if found else sys.executable


@pytest.fixture()
def hermes_home(tmp_path: pathlib.Path) -> pathlib.Path:
    """An empty temporary ``HERMES_HOME``."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    return home


@pytest.fixture(scope="module")
def metadata_validator() -> Any:
    return build_validator(METADATA_SCHEMA_ID)


# ---------------------------------------------------------------------------
# Path resolution.
# ---------------------------------------------------------------------------


def test_installed_path_is_the_directory_hermes_scans(
    hermes_home: pathlib.Path,
) -> None:
    target = installed_path(hermes_home)

    assert target.relative_to(hermes_home).parts == EXPECTED_RELATIVE_PATH
    assert target.as_posix().endswith(
        "plugins/model-providers/hermes-auto/__init__.py"
    )


def test_installed_path_creates_nothing(hermes_home: pathlib.Path) -> None:
    """Asking where the shim goes must not put it there."""
    installed_path(hermes_home)

    assert list(hermes_home.iterdir()) == []


def test_hermes_home_env_var_wins(
    monkeypatch: pytest.MonkeyPatch, hermes_home: pathlib.Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert default_hermes_home() == hermes_home
    assert installed_path().relative_to(hermes_home).parts == EXPECTED_RELATIVE_PATH


def test_platform_default_matches_hermes_own_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Mirrors ``hermes_constants._get_platform_default_hermes_home``."""
    monkeypatch.delenv("HERMES_HOME", raising=False)

    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_hermes_home() == tmp_path / "hermes"
    else:
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
        assert default_hermes_home() == tmp_path / ".hermes"


def test_blank_hermes_home_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Hermes strips the value before testing it; so must this."""
    monkeypatch.setenv("HERMES_HOME", "   ")
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_hermes_home() == tmp_path / "hermes"
    else:
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
        assert default_hermes_home() == tmp_path / ".hermes"


# ---------------------------------------------------------------------------
# install / uninstall behaviour.
# ---------------------------------------------------------------------------


def test_install_writes_a_parseable_module(hermes_home: pathlib.Path) -> None:
    target = install(hermes_home)

    assert target == installed_path(hermes_home)
    assert target.is_file()
    ast.parse(target.read_text(encoding="utf-8"))


def test_installed_file_carries_the_marker_on_its_first_line(
    hermes_home: pathlib.Path,
) -> None:
    target = install(hermes_home)
    content = target.read_text(encoding="utf-8")

    assert content.startswith(MARKER)
    assert content.splitlines()[0] == MARKER


def test_install_substitutes_base_url_token_var_and_version(
    hermes_home: pathlib.Path,
) -> None:
    target = install(
        hermes_home,
        base_url="http://127.0.0.1:9999/v1",
        token_env_var="CUSTOM_TOKEN_VAR",
        plugin_version="9.9.9",
    )
    content = target.read_text(encoding="utf-8")

    assert "http://127.0.0.1:9999/v1" in content
    assert "CUSTOM_TOKEN_VAR" in content
    assert "9.9.9" in content


def test_no_placeholder_survives_into_the_written_file(
    hermes_home: pathlib.Path,
) -> None:
    """A dropped substitution would ship the literal ``__HERMES_AUTO_*__``."""
    content = install(hermes_home).read_text(encoding="utf-8")

    assert "__HERMES_AUTO_" not in content


def test_render_rejects_a_template_missing_a_placeholder() -> None:
    """The guard in ``render`` must actually fire, not merely exist."""
    with pytest.raises(ValueError, match="__HERMES_AUTO_BASE_URL__"):
        render(MARKER + "\nBASE_URL = 'gone'\n")


def test_install_renders_the_shipped_shim_source() -> None:
    """The installed file must be SHIM_SOURCE, not some other template.

    ``install`` passes :data:`SHIM_SOURCE` explicitly, so this pins that the
    two are the same artifact: every non-placeholder line of the template
    survives into the rendered output.
    """
    rendered = render(SHIM_SOURCE)
    template_lines = [
        line
        for line in SHIM_SOURCE.splitlines()
        if "__HERMES_AUTO_" not in line
    ]

    for line in template_lines:
        assert line in rendered


def test_install_is_idempotent(hermes_home: pathlib.Path) -> None:
    first = install(hermes_home)
    first_content = first.read_text(encoding="utf-8")

    second = install(hermes_home)

    assert second == first
    assert second.read_text(encoding="utf-8") == first_content


def test_reinstall_upgrades_a_file_this_project_owns(
    hermes_home: pathlib.Path,
) -> None:
    install(hermes_home, base_url="http://127.0.0.1:1111/v1")
    target = install(hermes_home, base_url="http://127.0.0.1:2222/v1")
    content = target.read_text(encoding="utf-8")

    assert "http://127.0.0.1:2222/v1" in content
    assert "http://127.0.0.1:1111/v1" not in content


def test_install_refuses_to_overwrite_an_unmarked_file(
    hermes_home: pathlib.Path,
) -> None:
    target = installed_path(hermes_home)
    target.parent.mkdir(parents=True)
    target.write_text("# somebody else's provider\n", encoding="utf-8")

    with pytest.raises(InstallError, match="marker"):
        install(hermes_home)

    assert target.read_text(encoding="utf-8") == "# somebody else's provider\n"


def test_install_refuses_a_file_whose_marker_is_not_first(
    hermes_home: pathlib.Path,
) -> None:
    """The marker must lead the file -- a mention further down proves nothing."""
    target = installed_path(hermes_home)
    target.parent.mkdir(parents=True)
    target.write_text(f"# someone else\n{MARKER}\n", encoding="utf-8")

    with pytest.raises(InstallError, match="marker"):
        install(hermes_home)


def test_install_reports_the_path_and_the_os_error_when_the_write_fails(
    hermes_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only plugin directory must surface as a typed, named failure.

    Simulated rather than chmod'd: ``os.chmod`` does not make a directory
    unwritable on Windows, so a permission-based test would silently pass here
    without ever exercising the branch.
    """

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(installer.tempfile, "mkstemp", refuse)

    with pytest.raises(InstallError) as excinfo:
        install(hermes_home)

    message = str(excinfo.value)
    assert str(installed_path(hermes_home)) in message
    assert "Permission denied" in message


def test_a_failed_write_leaves_no_temporary_file_behind(
    hermes_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = installed_path(hermes_home).parent

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(installer.os, "replace", refuse)

    with pytest.raises(InstallError):
        install(hermes_home)

    assert list(plugin_dir.iterdir()) == []


def test_uninstall_removes_the_shim_and_its_directory(
    hermes_home: pathlib.Path,
) -> None:
    target = install(hermes_home)

    assert uninstall(hermes_home) is True
    assert not target.exists()
    assert not target.parent.exists()


def test_uninstall_on_an_absent_install_returns_false(
    hermes_home: pathlib.Path,
) -> None:
    assert uninstall(hermes_home) is False


def test_second_uninstall_returns_false(hermes_home: pathlib.Path) -> None:
    install(hermes_home)

    assert uninstall(hermes_home) is True
    assert uninstall(hermes_home) is False


def test_uninstall_removes_the_bytecode_cache_hermes_leaves_behind(
    hermes_home: pathlib.Path,
) -> None:
    target = install(hermes_home)
    cache = target.parent / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-311.pyc").write_bytes(b"\x00")

    assert uninstall(hermes_home) is True
    assert not target.parent.exists()


def test_uninstall_keeps_a_directory_holding_someone_elses_file(
    hermes_home: pathlib.Path,
) -> None:
    target = install(hermes_home)
    stranger = target.parent / "notes.txt"
    stranger.write_text("mine", encoding="utf-8")

    assert uninstall(hermes_home) is True
    assert not target.exists()
    assert stranger.read_text(encoding="utf-8") == "mine"


def test_uninstall_refuses_to_delete_an_unmarked_file(
    hermes_home: pathlib.Path,
) -> None:
    """Deleting is clobbering; the ownership rule applies to removal too."""
    target = installed_path(hermes_home)
    target.parent.mkdir(parents=True)
    target.write_text("# somebody else's provider\n", encoding="utf-8")

    with pytest.raises(InstallError, match="marker"):
        uninstall(hermes_home)

    assert target.exists()


# ---------------------------------------------------------------------------
# The shim's contents.
# ---------------------------------------------------------------------------


def _shim_imports(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_shim_imports_stdlib_and_hermes_providers_only() -> None:
    unexpected = _shim_imports(SHIM_SOURCE) - set(sys.stdlib_module_names) - {"providers"}

    assert not unexpected, f"shim would need these in the Hermes venv: {unexpected}"


def test_shim_does_not_import_this_project() -> None:
    """The check the ``'hermes_auto' not in SHIM_SOURCE`` grep was reaching for.

    That grep cannot pass: the envelope key is literally ``_hermes_auto``, so
    the substring is present in any correct shim. What actually matters is that
    no *import* names this project, which is an AST question.
    """
    assert "hermes_auto" not in _shim_imports(SHIM_SOURCE)

    tree = ast.parse(SHIM_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert node.value.id != "hermes_auto"


def test_the_only_hermes_auto_substring_is_the_envelope_key() -> None:
    """Pin why the naive substring grep fails, so nobody 'fixes' it by hiding it."""
    tree = ast.parse(SHIM_SOURCE)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    offenders = {
        text
        for text in literals
        if "hermes_auto" in text and text != ENVELOPE_KEY
    }

    assert not offenders, offenders


def test_shim_registers_an_instance_built_with_keyword_arguments() -> None:
    """Structural proof of the form ``design.md`` §5.1 gets wrong.

    ``'register_provider(' in SHIM_SOURCE`` is satisfied by a docstring, and by
    ``register_provider(HermesAutoProfile)`` -- passing the class, which
    registers an object with no ``name`` and breaks every lookup. This walks the
    AST instead: the call's argument must be a module-level name bound to a
    constructor call carrying ``name="hermes-auto"`` as a keyword.
    """
    tree = ast.parse(SHIM_SOURCE)

    assignments = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }

    registrations = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "register_provider"
    ]
    assert len(registrations) == 1, "the shim must register exactly once"

    (argument,) = registrations[0].args
    assert isinstance(argument, ast.Name), "must register an instance, not a class"

    constructor = assignments.get(argument.id)
    assert isinstance(constructor, ast.Call), f"{argument.id} is not a constructor call"

    keywords = {kw.arg: kw.value for kw in constructor.keywords}
    assert keywords, "ProviderProfile is a dataclass -- fields must be kwargs"
    assert isinstance(keywords["name"], ast.Constant)
    assert keywords["name"].value == "hermes-auto"


def test_shim_declares_no_class_level_dataclass_fields() -> None:
    """Bare class attributes are the design.md defect; the subclass must have none."""
    tree = ast.parse(SHIM_SOURCE)
    (profile_class,) = [
        node for node in tree.body if isinstance(node, ast.ClassDef)
    ]
    class_attributes = [
        target.id
        for node in profile_class.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]

    assert not class_attributes, class_attributes


def test_shim_source_parses_and_leads_with_the_marker() -> None:
    ast.parse(SHIM_SOURCE)

    assert SHIM_SOURCE.startswith(MARKER)


def test_shim_documents_why_the_envelope_logic_is_duplicated() -> None:
    """Undocumented duplication is indistinguishable from an accident."""
    assert "DUPLICATED" in SHIM_SOURCE
    assert "drift test" in SHIM_SOURCE


# ---------------------------------------------------------------------------
# Drift between the shim's copy of the envelope and provider.py's.
# ---------------------------------------------------------------------------


def _load_shim_profile(monkeypatch: pytest.MonkeyPatch, **render_kwargs: Any) -> Any:
    """Execute the rendered shim against a stub ``providers`` package.

    A stub, not the real Hermes package, so this runs anywhere -- the drift
    question is about two copies of our own logic, and needs nothing from
    Hermes. ``monkeypatch.setitem`` restores ``sys.modules`` afterwards, so a
    real ``providers`` imported by another test is not shadowed beyond this one.
    """
    registered: list[Any] = []

    class StubProviderProfile:
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    providers_module = types.ModuleType("providers")
    providers_module.register_provider = registered.append  # type: ignore[attr-defined]
    base_module = types.ModuleType("providers.base")
    base_module.ProviderProfile = StubProviderProfile  # type: ignore[attr-defined]
    providers_module.base = base_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "providers", providers_module)
    monkeypatch.setitem(sys.modules, "providers.base", base_module)

    namespace: dict[str, Any] = {"__name__": "_shim_under_test"}
    exec(compile(render(SHIM_SOURCE, **render_kwargs), "<shim>", "exec"), namespace)

    assert len(registered) == 1, "the shim must register exactly one profile"
    return registered[0]


@pytest.mark.parametrize(
    "call_kwargs",
    [
        pytest.param({}, id="no-arguments"),
        pytest.param({"session_id": None}, id="none-session-id"),
        pytest.param({"session_id": "s-1"}, id="explicit-session-id"),
        pytest.param({"session_id": "s-1", "model": "auto:quality"}, id="with-model"),
        pytest.param({"model": ""}, id="empty-model"),
        pytest.param({"session_id": "x" * 5000}, id="oversized-session-id"),
        pytest.param({"model": "y" * 5000}, id="oversized-model"),
    ],
)
def test_shim_envelope_matches_provider_envelope(
    monkeypatch: pytest.MonkeyPatch,
    metadata_validator: Any,
    call_kwargs: dict[str, Any],
) -> None:
    """The duplicated logic must produce identical output, field for field.

    Compared on every input shape where the two could plausibly diverge: the
    absent and None session ids, an empty model, and values past the schema's
    length cap. Synthesized ids are unique by construction, so that one field
    is compared for shape rather than equality.
    """
    shim_profile = _load_shim_profile(monkeypatch)
    shim_envelope = shim_profile.build_extra_body(**call_kwargs)[ENVELOPE_KEY]

    provider_envelope = build_envelope(
        session_id=call_kwargs.get("session_id"),
        virtual_model=call_kwargs.get("model"),
    )

    metadata_validator.validate(shim_envelope)
    assert set(shim_envelope) == set(provider_envelope)

    for field in ("protocol_version", "virtual_model", "plugin_version"):
        assert shim_envelope[field] == provider_envelope[field], field

    if call_kwargs.get("session_id"):
        assert shim_envelope["root_session_id"] == provider_envelope["root_session_id"]
    else:
        assert shim_envelope["root_session_id"].startswith("no-session-")
        assert len(shim_envelope["root_session_id"]) == len(
            provider_envelope["root_session_id"]
        )


def test_shim_profile_fields_match_the_provider_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declarative half is duplicated too, and can drift just as quietly."""
    shim_profile = _load_shim_profile(monkeypatch)

    assert shim_profile.name == "hermes-auto"
    assert shim_profile.display_name == "Hermes Auto Router"
    assert shim_profile.api_mode == "chat_completions"
    assert shim_profile.supports_vision is True
    assert shim_profile.supports_health_check is True
    assert shim_profile.default_aux_model == "auto:balanced"
    assert shim_profile.fallback_models == (
        "auto:quality",
        "auto:balanced",
        "auto:economy",
        "auto:session",
    )
    assert shim_profile.env_vars == ("HERMES_AUTO_ROUTER_TOKEN",)
    assert shim_profile.base_url == "http://127.0.0.1:8787/v1"


def test_shim_carries_the_installed_plugin_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shim_profile = _load_shim_profile(monkeypatch)
    envelope = shim_profile.build_extra_body()[ENVELOPE_KEY]

    assert envelope["plugin_version"] == __version__


def test_render_cannot_inject_syntax_through_a_substituted_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Values are inserted as ``repr``, so quotes are data rather than syntax."""
    hostile = 'http://x/v1"\nimport os; os.environ["PWNED"] = "1"\n#'
    shim_profile = _load_shim_profile(monkeypatch, base_url=hostile)

    assert shim_profile.base_url == hostile
    assert "PWNED" not in os.environ


# ---------------------------------------------------------------------------
# Registration proof against the real Hermes.
# ---------------------------------------------------------------------------


_PROBE = """
import json
from providers import get_provider_profile
profile = get_provider_profile("hermes-auto")
print(json.dumps({
    "found": profile is not None,
    "name": getattr(profile, "name", None),
    "base_url": getattr(profile, "base_url", None),
    "fallback_models": list(getattr(profile, "fallback_models", ())),
    "envelope": profile.build_extra_body()["_hermes_auto"] if profile else None,
}))
"""


def test_real_hermes_discovers_the_installed_provider(
    hermes_home: pathlib.Path, metadata_validator: Any
) -> None:
    """Install into a temp ``HERMES_HOME`` and let Hermes's own scan find it.

    This is the only test that proves the plan's central claim end to end: that
    a directory-scan install registers a provider pip never would, and that the
    envelope the real profile emits satisfies the frozen schema. It is also the
    only one that would catch the bare-class-attribute form, which raises at
    import and would leave the provider silently absent -- Hermes logs a
    warning and continues.
    """
    root = _hermes_checkout()
    if root is None:
        pytest.skip(
            "Hermes source checkout not found -- set HERMES_AGENT_REPO or place "
            f"it at {DEFAULT_HERMES_CHECKOUT}. Registration cannot be proven "
            "without a real Hermes to run the discovery scan."
        )

    install(hermes_home, base_url="http://127.0.0.1:8787/v1")

    environment = dict(os.environ)
    environment["HERMES_HOME"] = str(hermes_home)
    completed = subprocess.run(
        [_hermes_interpreter(root), "-c", _PROBE],
        cwd=str(root),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result["found"], "Hermes's directory scan did not register hermes-auto"
    assert result["name"] == "hermes-auto"
    assert result["base_url"] == "http://127.0.0.1:8787/v1"
    assert set(result["fallback_models"]) == {
        "auto:quality",
        "auto:balanced",
        "auto:economy",
        "auto:session",
    }
    metadata_validator.validate(result["envelope"])


def test_real_hermes_does_not_know_the_provider_before_installation(
    hermes_home: pathlib.Path,
) -> None:
    """The negative case: without the shim, the provider does not exist.

    Without this, the test above passes for a provider that was already
    installed on the developer's machine, and proves nothing about the
    installer.
    """
    root = _hermes_checkout()
    if root is None:
        pytest.skip(
            "Hermes source checkout not found -- set HERMES_AGENT_REPO or place "
            f"it at {DEFAULT_HERMES_CHECKOUT}."
        )

    environment = dict(os.environ)
    environment["HERMES_HOME"] = str(hermes_home)
    completed = subprocess.run(
        [_hermes_interpreter(root), "-c", _PROBE],
        cwd=str(root),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert not result["found"], (
        "hermes-auto resolved from an empty HERMES_HOME -- it is bundled with "
        "Hermes or installed elsewhere, so the positive test proves nothing"
    )


# ---------------------------------------------------------------------------
# The hard constraint: zero modifications to the real Hermes installation.
# ---------------------------------------------------------------------------


def test_no_test_wrote_into_the_real_hermes_home() -> None:
    """Nothing in this suite may install into the developer's real Hermes."""
    real_home = default_hermes_home()
    real_target = real_home / pathlib.Path(*EXPECTED_RELATIVE_PATH)

    assert not real_target.exists(), (
        f"{real_target} exists -- a test installed into the real Hermes home, "
        "or a previous manual run left it behind"
    )


def test_installer_never_touches_the_hermes_checkout_itself() -> None:
    """The shim goes to ``$HERMES_HOME``, which is not the Hermes source tree.

    The distinction is load-bearing: writing under the checkout would be a
    core modification, which the project forbids outright. ``$HERMES_HOME`` is
    user data, and the plugin directory under it is the documented extension
    point.
    """
    root = _hermes_checkout()
    if root is None:
        pytest.skip("Hermes source checkout not found.")

    target = installed_path(default_hermes_home()).resolve()

    assert root.resolve() not in target.parents
