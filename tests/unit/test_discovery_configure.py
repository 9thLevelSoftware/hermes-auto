from __future__ import annotations

import io
import json
import subprocess
import sys

import yaml

from hermes_auto import commands
from hermes_auto.config import CandidateConfig, load_config
from hermes_auto.discovery import (
    DiscoveredModel,
    DiscoveryResult,
    discover_hermes_models,
    suggested_tier,
)


def test_discovery_accepts_metadata_but_never_credential_values(monkeypatch) -> None:
    output = {
        "models": [
            {
                "provider": "openrouter",
                "model": "google/gemini-flash",
                "base_url": "https://openrouter.ai/api/v1",
                "credential_env_var": "OPENROUTER_API_KEY",
                "credentialed": True,
                "context_window": 128000,
                "supports_tools": True,
                "supports_vision": True,
            },
            {
                "provider": "missing",
                "model": "unavailable",
                "base_url": "https://missing.example/v1",
                "credential_env_var": "MISSING_API_KEY",
                "credentialed": False,
                "context_window": 128000,
                "supports_tools": True,
                "supports_vision": False,
            },
        ]
    }

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(output),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = discover_hermes_models(hermes_python=sys.executable)

    assert result.supported
    assert result.models == (
        DiscoveredModel(
            provider="openrouter",
            model="google/gemini-flash",
            base_url="https://openrouter.ai/api/v1",
            credential_env_var="OPENROUTER_API_KEY",
            context_window=128_000,
            supports_tools=True,
            supports_vision=True,
        ),
    )
    assert "secret" not in repr(result).lower()


def test_discovery_falls_back_cleanly_when_hermes_is_missing(tmp_path) -> None:
    result = discover_hermes_models(hermes_python=tmp_path / "missing-python")
    assert not result.supported
    assert result.models == ()
    assert "manual" in result.warning.lower()


def test_model_name_tier_suggestions_are_only_suggestions() -> None:
    assert suggested_tier("gemini-3-flash") == "fast"
    assert suggested_tier("claude-opus-5") == "strong"
    assert suggested_tier("ordinary-model") == "balanced"


def test_configure_requires_confirmation_before_writing(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "unrelated:\n  keep: true\n",
        encoding="utf-8",
    )
    discovered = DiscoveryResult(
        supported=True,
        models=(
            DiscoveredModel(
                provider="test",
                model="quick-flash",
                base_url="https://test.example/v1",
                credential_env_var="TEST_API_KEY",
                context_window=128_000,
                supports_tools=True,
                supports_vision=False,
            ),
            DiscoveredModel(
                provider="test",
                model="quality-pro",
                base_url="https://test.example/v1",
                credential_env_var="TEST_API_KEY",
                context_window=256_000,
                supports_tools=True,
                supports_vision=True,
            ),
        ),
    )
    monkeypatch.setattr(commands, "discover_hermes_models", lambda **_: discovered)
    answers = iter(["1,2", "fast", "strong", "n"])
    output = io.StringIO()

    code = commands.cmd_configure(
        hermes_home=tmp_path,
        stream=output,
        input_fn=lambda _prompt: next(answers),
    )

    assert code == 1
    document = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert document == {"unrelated": {"keep": True}}


def test_configure_writes_confirmed_shortlist_without_credentials(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        """unrelated:
  keep: true
auto_router:
  upstream:
    base_url: http://127.0.0.1:11434/v1
    model: old
    credential_ref: none
""",
        encoding="utf-8",
    )
    discovered = DiscoveryResult(
        supported=True,
        models=(
            DiscoveredModel(
                provider="test",
                model="quick-flash",
                base_url="https://test.example/v1",
                credential_env_var="TEST_API_KEY",
                context_window=128_000,
                supports_tools=True,
                supports_vision=False,
            ),
            DiscoveredModel(
                provider="test",
                model="quality-pro",
                base_url="https://test.example/v1",
                credential_env_var="TEST_API_KEY",
                context_window=256_000,
                supports_tools=True,
                supports_vision=True,
            ),
        ),
    )
    monkeypatch.setattr(commands, "discover_hermes_models", lambda **_: discovered)
    answers = iter(["1,2", "fast", "strong", "y"])

    code = commands.cmd_configure(
        hermes_home=tmp_path,
        stream=io.StringIO(),
        input_fn=lambda _prompt: next(answers),
    )

    assert code == 0
    document = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert document["unrelated"] == {"keep": True}
    assert "upstream" not in document["auto_router"]
    serialized = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "actual-secret" not in serialized
    loaded = load_config(tmp_path / "config.yaml")
    assert loaded.candidates == (
        CandidateConfig(
            id="test-quick-flash",
            provider="test",
            model="quick-flash",
            base_url="https://test.example/v1",
            credential_ref="env:TEST_API_KEY",
            tier="fast",
            context_window=128_000,
            supports_tools=True,
            supports_vision=False,
        ),
        CandidateConfig(
            id="test-quality-pro",
            provider="test",
            model="quality-pro",
            base_url="https://test.example/v1",
            credential_ref="env:TEST_API_KEY",
            tier="strong",
            context_window=256_000,
            supports_tools=True,
            supports_vision=True,
        ),
    )
