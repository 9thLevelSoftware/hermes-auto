from __future__ import annotations

import warnings

import pytest

from hermes_auto.config import CandidateConfig, ConfigError, load_config


def write_config(tmp_path, candidate_text: str):
    path = tmp_path / "config.yaml"
    path.write_text(
        "auto_router:\n"
        "  candidates:\n"
        f"{candidate_text}",
        encoding="utf-8",
    )
    return path


def test_candidate_configuration_loads_in_declared_order(tmp_path) -> None:
    source = write_config(
        tmp_path,
        """    - id: fast-model
      provider: provider-name
      model: provider-model-id
      base_url: https://provider.example/v1
      credential_ref: env:PROVIDER_API_KEY
      tier: fast
      context_window: 128000
      supports_tools: true
      supports_vision: false
    - id: strong-model
      provider: provider-name
      model: provider-strong-id
      base_url: https://provider.example/v1
      credential_ref: none
      tier: strong
      context_window: 256000
      supports_tools: true
      supports_vision: true
""",
    )

    loaded = load_config(source)

    assert [item.id for item in loaded.candidates] == ["fast-model", "strong-model"]
    assert loaded.candidates[0] == CandidateConfig(
        id="fast-model",
        provider="provider-name",
        model="provider-model-id",
        base_url="https://provider.example/v1",
        credential_ref="env:PROVIDER_API_KEY",
        tier="fast",
        context_window=128_000,
        supports_tools=True,
        supports_vision=False,
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", "", r"candidates\[0\]\.id"),
        ("provider", "", r"candidates\[0\]\.provider"),
        ("model", "", r"candidates\[0\]\.model"),
        ("base_url", "", r"candidates\[0\]\.base_url"),
        ("credential_ref", "literal-secret", "credential reference"),
        ("tier", "premium", "fast, balanced, strong"),
        ("context_window", "0", "positive integer"),
        ("supports_tools", "\"yes\"", "expected a boolean"),
        ("supports_vision", "\"no\"", "expected a boolean"),
    ],
)
def test_candidate_fields_are_strictly_validated(
    tmp_path, field: str, value: str, message: str
) -> None:
    values = {
        "id": "candidate-a",
        "provider": "provider-name",
        "model": "model-name",
        "base_url": "https://provider.example/v1",
        "credential_ref": "none",
        "tier": "balanced",
        "context_window": "128000",
        "supports_tools": "true",
        "supports_vision": "false",
    }
    values[field] = value
    source = write_config(
        tmp_path,
        "".join(f"    - {key}: {item}\n" if index == 0 else f"      {key}: {item}\n"
                for index, (key, item) in enumerate(values.items())),
    )

    with pytest.raises(ConfigError, match=message):
        load_config(source)


def test_one_candidate_is_allowed_with_a_migration_warning(tmp_path) -> None:
    source = write_config(
        tmp_path,
        """    - id: only
      provider: test
      model: model
      base_url: http://127.0.0.1:11434/v1
      credential_ref: none
      tier: balanced
      context_window: 128000
      supports_tools: true
      supports_vision: false
""",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loaded = load_config(source)
    assert len(loaded.candidates) == 1
    assert "at least two" in str(caught[0].message)


@pytest.mark.parametrize(
    "base_url",
    ["provider.example/v1", "ftp://provider.example/v1", "https:///v1"],
)
def test_candidate_requires_an_absolute_http_compatible_endpoint(
    tmp_path, base_url: str
) -> None:
    source = write_config(
        tmp_path,
        f"""    - id: only
      provider: test
      model: model
      base_url: {base_url}
      credential_ref: none
      tier: balanced
      context_window: 128000
      supports_tools: true
      supports_vision: false
""",
    )

    with pytest.raises(ConfigError, match=r"absolute HTTP\(S\) URL"):
        load_config(source)


def test_legacy_upstream_is_migrated_to_one_candidate(tmp_path) -> None:
    source = tmp_path / "config.yaml"
    source.write_text(
        """auto_router:
  upstream:
    base_url: http://127.0.0.1:11434/v1
    model: legacy-model
    credential_ref: none
""",
        encoding="utf-8",
    )
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        loaded = load_config(source)

    assert loaded.candidates == (
        CandidateConfig(
            id="legacy-upstream",
            provider="openai-compatible",
            model="legacy-model",
            base_url="http://127.0.0.1:11434/v1",
            credential_ref="none",
            tier="balanced",
            context_window=128_000,
            supports_tools=True,
            supports_vision=True,
        ),
    )
