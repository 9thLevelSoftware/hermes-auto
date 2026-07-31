from __future__ import annotations

import ast
import argparse
import io
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import yaml

from hermes_auto.commands import cmd_setup, enable_model_alias
from hermes_auto.hermes_shim.installer import (
    control_installed_paths,
    install_control,
    installed_path,
)


class Context:
    def __init__(self) -> None:
        self.cli: list[tuple[Any, ...]] = []
        self.commands: list[tuple[Any, ...]] = []
        self.hooks: list[tuple[Any, ...]] = []

    def register_cli_command(self, *args: Any, **kwargs: Any) -> None:
        self.cli.append((args, kwargs))

    def register_command(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append((args, kwargs))

    def register_hook(self, *args: Any, **kwargs: Any) -> None:
        self.hooks.append((args, kwargs))


def test_control_plugin_is_home_scoped_and_dependency_free(tmp_path) -> None:
    executable = pathlib.Path(sys.executable).resolve()
    init_path, manifest_path = install_control(
        tmp_path,
        executable=executable,
    )

    assert (init_path, manifest_path) == control_installed_paths(tmp_path)
    assert init_path == tmp_path / "plugins" / "hermes-auto-control" / "__init__.py"
    assert manifest_path == tmp_path / "plugins" / "hermes-auto-control" / "plugin.yaml"
    source = init_path.read_text(encoding="utf-8")
    compile(source, str(init_path), "exec")

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported <= {"os", "shlex", "subprocess"}
    assert "hermes_auto" not in imported

    spec = importlib.util.spec_from_file_location("installed_control", init_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.HERMES_AUTO_EXECUTABLE == str(executable)
    context = Context()
    module.register(context)
    assert context.cli[0][0][0] == "auto"
    assert context.commands[0][0][0] == "auto"
    assert context.hooks[0][0][0] == "on_session_start"
    parser = argparse.ArgumentParser()
    context.cli[0][1]["setup_fn"](parser)
    parsed = parser.parse_args(["explain", "--session-id", "session-1"])
    assert parsed.hermes_auto_arguments == [
        "explain",
        "--session-id",
        "session-1",
    ]


def test_control_plugin_loads_in_hermes_runtime_without_router_package(tmp_path) -> None:
    hermes = shutil_which_hermes()
    if hermes is None:
        return
    hermes_python = pathlib.Path(hermes).with_name(
        "python.exe" if os.name == "nt" else "python"
    )
    if not hermes_python.is_file():
        return

    init_path, _ = install_control(tmp_path, executable=pathlib.Path(sys.executable))
    script = """
import importlib.util, json, pathlib
path = pathlib.Path(__import__('sys').argv[1])
spec = importlib.util.spec_from_file_location('focused_control', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
class C:
    def __init__(self): self.names = []
    def register_cli_command(self, name, **kwargs): self.names.append(['cli', name])
    def register_command(self, name, *args, **kwargs): self.names.append(['slash', name])
    def register_hook(self, name, *args, **kwargs): self.names.append(['hook', name])
c = C()
module.register(c)
print(json.dumps(c.names))
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(hermes_python), "-I", "-c", script, str(init_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [
        ["cli", "auto"],
        ["slash", "auto"],
        ["hook", "on_session_start"],
    ]


def shutil_which_hermes() -> str | None:
    import shutil

    return shutil.which("hermes")


def test_model_alias_update_preserves_unrelated_configuration(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """# user comment
plugins:
  enabled:
    - existing-plugin
unrelated:
  nested: keep-me
model:
  provider: a-provider-the-user-selected
  default: its-model
""",
        encoding="utf-8",
    )

    changed, detail = enable_model_alias(
        tmp_path,
        base_url="http://127.0.0.1:8787/v1",
    )

    assert changed, detail
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["unrelated"] == {"nested": "keep-me"}
    assert document["model"] == {
        "provider": "a-provider-the-user-selected",
        "default": "its-model",
    }
    assert document["model_aliases"]["auto"] == {
        "model": "auto",
        "provider": "hermes-auto",
        "base_url": "http://127.0.0.1:8787/v1",
    }

    second_changed, _ = enable_model_alias(
        tmp_path,
        base_url="http://127.0.0.1:8787/v1",
    )
    assert second_changed
    second = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert second == document


def test_literal_auto_alias_resolves_away_from_current_provider(tmp_path) -> None:
    hermes = shutil_which_hermes()
    if hermes is None:
        return
    hermes_python = pathlib.Path(hermes).with_name(
        "python.exe" if os.name == "nt" else "python"
    )
    if not hermes_python.is_file():
        return

    (tmp_path / "config.yaml").write_text(
        """model:
  provider: other-provider
  default: other-model
""",
        encoding="utf-8",
    )
    changed, detail = enable_model_alias(
        tmp_path,
        base_url="http://127.0.0.1:8787/v1",
    )
    assert changed, detail

    script = """
import json
from hermes_cli.model_switch import _load_direct_aliases
a = _load_direct_aliases()['auto']
print(json.dumps({'model': a.model, 'provider': a.provider, 'base_url': a.base_url}))
"""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path)
    completed = subprocess.run(
        [str(hermes_python), "-I", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "model": "auto",
        "provider": "hermes-auto",
        "base_url": "http://127.0.0.1:8787/v1",
    }


def test_setup_uses_the_overridden_hermes_home_configuration(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "hermes-home"
    state = tmp_path / "state"
    home.mkdir()
    (home / "config.yaml").write_text(
        """unrelated:
  keep: true
auto_router:
  gateway:
    url: http://127.0.0.1:9898
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(state))

    output = io.StringIO()
    assert cmd_setup(hermes_home=home, stream=output) == 0, output.getvalue()

    provider = (
        home
        / "plugins"
        / "model-providers"
        / "hermes-auto"
        / "__init__.py"
    ).read_text(encoding="utf-8")
    assert "http://127.0.0.1:9898/v1" in provider
    document = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert document["unrelated"] == {"keep": True}
    assert document["model_aliases"]["auto"]["base_url"] == (
        "http://127.0.0.1:9898/v1"
    )
    assert "hermes-auto-control" in document["plugins"]["enabled"]


def test_setup_handles_pyyaml_indentless_enabled_sequence(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": ["existing-plugin"]},
                "auto_router": {
                    "candidates": [
                        {
                            "id": "local",
                            "provider": "local",
                            "model": "local-model",
                            "base_url": "http://127.0.0.1:11434/v1",
                            "credential_ref": "none",
                            "tier": "balanced",
                            "context_window": 128000,
                            "supports_tools": True,
                            "supports_vision": False,
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(tmp_path / "state"))

    output = io.StringIO()
    assert cmd_setup(hermes_home=home, stream=output) == 0, output.getvalue()

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["plugins"]["enabled"] == [
        "existing-plugin",
        "hermes-auto-control",
    ]


def test_setup_rolls_back_every_managed_file_when_one_upgrade_fails(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "hermes-home"
    state = tmp_path / "state"
    control_init, control_manifest = control_installed_paths(home)
    control_init.parent.mkdir(parents=True)
    unowned = "# user-owned control plugin\n"
    control_init.write_text(unowned, encoding="utf-8")
    home.mkdir(exist_ok=True)
    config_path = home / "config.yaml"
    original_config = "unrelated:\n  preserve: true\n"
    config_path.write_text(original_config, encoding="utf-8")
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(state))

    output = io.StringIO()
    assert cmd_setup(hermes_home=home, stream=output) == 1

    assert "restored to their previous state" in output.getvalue()
    assert config_path.read_text(encoding="utf-8") == original_config
    assert control_init.read_text(encoding="utf-8") == unowned
    assert not control_manifest.exists()
    assert not installed_path(home).exists()
    assert not (state / "token").exists()
    assert not (state / "admin-token").exists()
