"""Read-only Hermes model discovery for the interactive configure command."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import Any


@dataclasses.dataclass(frozen=True)
class DiscoveredModel:
    provider: str
    model: str
    base_url: str
    credential_env_var: str | None
    context_window: int
    supports_tools: bool
    supports_vision: bool


@dataclasses.dataclass(frozen=True)
class DiscoveryResult:
    supported: bool
    models: tuple[DiscoveredModel, ...]
    warning: str = ""


_DISCOVERY_SCRIPT = r"""
import json
import os

from hermes_cli.config import load_config
from providers import list_providers

config = load_config()
profiles = {}
for profile in list_providers():
    profiles[str(profile.name)] = profile
    for alias in getattr(profile, "aliases", ()) or ():
        profiles[str(alias)] = profile

entries = []

def add_entry(value, source_context=None):
    if not isinstance(value, dict):
        return
    provider = str(value.get("provider") or "").strip()
    model = str(value.get("model") or value.get("default") or "").strip()
    if not provider or not model:
        return
    entry = dict(value)
    if source_context and "context_length" not in entry:
        entry["context_length"] = source_context
    entries.append(entry)

main = config.get("model")
if isinstance(main, dict):
    add_entry(main, main.get("context_length"))

auxiliary = config.get("auxiliary")
if isinstance(auxiliary, dict):
    for value in auxiliary.values():
        add_entry(value)

fallbacks = config.get("fallback_providers")
if isinstance(fallbacks, list):
    for value in fallbacks:
        add_entry(value)

aliases = config.get("model_aliases")
if isinstance(aliases, dict):
    for value in aliases.values():
        add_entry(value)

# The selected provider's declared fallback models are configured metadata too.
if isinstance(main, dict):
    selected = profiles.get(str(main.get("provider") or ""))
    if selected is not None:
        for model in getattr(selected, "fallback_models", ()) or ():
            entries.append({
                "provider": selected.name,
                "model": model,
                "base_url": getattr(selected, "base_url", ""),
                "context_length": main.get("context_length"),
            })

models = []
seen = set()
for entry in entries:
    provider_name = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or entry.get("default") or "").strip()
    profile = profiles.get(provider_name)
    if profile is not None and getattr(profile, "api_mode", "") != "chat_completions":
        continue
    base_url = str(
        entry.get("base_url")
        or (getattr(profile, "base_url", "") if profile is not None else "")
    ).strip()
    if not base_url:
        continue
    env_vars = tuple(getattr(profile, "env_vars", ()) or ()) if profile is not None else ()
    credential_names = tuple(
        name for name in env_vars
        if not str(name).upper().endswith(("_BASE_URL", "_URL"))
    )
    credential_name = next(
        (str(name) for name in credential_names if os.environ.get(str(name))),
        None,
    )
    credentialed = credential_name is not None or not credential_names
    key = (provider_name, model, base_url)
    if key in seen:
        continue
    seen.add(key)
    try:
        context_window = int(entry.get("context_length") or 128000)
    except (TypeError, ValueError):
        context_window = 128000
    models.append({
        "provider": provider_name,
        "model": model,
        "base_url": base_url,
        "credential_env_var": credential_name,
        "credentialed": credentialed,
        "context_window": max(context_window, 1),
        "supports_tools": bool(entry.get("supports_tools", True)),
        "supports_vision": bool(
            entry.get(
                "supports_vision",
                getattr(profile, "supports_vision", False) if profile is not None else False,
            )
        ),
    })

print(json.dumps({"models": models}, separators=(",", ":")))
"""


def _find_hermes_python() -> pathlib.Path | None:
    executable = shutil.which("hermes")
    if not executable:
        return None
    sibling = pathlib.Path(executable).with_name(
        "python.exe" if os.name == "nt" else "python"
    )
    return sibling if sibling.is_file() else None


def discover_hermes_models(
    *,
    hermes_home: str | os.PathLike[str] | None = None,
    hermes_python: str | os.PathLike[str] | None = None,
    timeout: float = 15.0,
) -> DiscoveryResult:
    """Ask Hermes in a subprocess for safe model metadata, never credentials."""
    python_path = (
        pathlib.Path(hermes_python)
        if hermes_python is not None
        else _find_hermes_python()
    )
    if python_path is None or not python_path.is_file():
        return DiscoveryResult(
            False,
            (),
            "Hermes runtime was not found; continuing with manual configuration.",
        )

    environment = dict(os.environ)
    if hermes_home is not None:
        environment["HERMES_HOME"] = os.fspath(hermes_home)
    try:
        completed = subprocess.run(
            [str(python_path), "-I", "-c", _DISCOVERY_SCRIPT],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DiscoveryResult(
            False,
            (),
            f"Hermes discovery was unavailable ({type(exc).__name__}); "
            "continuing with manual configuration.",
        )
    if completed.returncode != 0:
        return DiscoveryResult(
            False,
            (),
            "This Hermes version did not provide compatible discovery metadata; "
            "continuing with manual configuration.",
        )
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return DiscoveryResult(
            False,
            (),
            "Hermes returned unreadable discovery metadata; continuing with "
            "manual configuration.",
        )

    models: list[DiscoveredModel] = []
    for item in payload.get("models", ()) if isinstance(payload, dict) else ():
        if not isinstance(item, dict) or item.get("credentialed") is not True:
            continue
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        base_url = str(item.get("base_url") or "").strip()
        variable = item.get("credential_env_var")
        if variable is not None:
            variable = str(variable).strip()
            if not re.fullmatch(r"[A-Z0-9_]+", variable):
                continue
        try:
            context_window = int(item.get("context_window"))
        except (TypeError, ValueError):
            continue
        if not provider or not model or not base_url or context_window <= 0:
            continue
        models.append(
            DiscoveredModel(
                provider=provider,
                model=model,
                base_url=base_url,
                credential_env_var=variable or None,
                context_window=context_window,
                supports_tools=item.get("supports_tools") is True,
                supports_vision=item.get("supports_vision") is True,
            )
        )
    warning = (
        ""
        if models
        else "Hermes exposed no configured OpenAI-compatible model with an "
        "available environment credential; continuing with manual configuration."
    )
    return DiscoveryResult(True, tuple(models), warning)


def suggested_tier(model: str) -> str:
    name = model.lower()
    if any(word in name for word in ("flash", "fast", "mini", "nano", "haiku")):
        return "fast"
    if any(
        word in name
        for word in ("strong", "pro", "opus", "sonnet", "gpt-5", "frontier")
    ):
        return "strong"
    return "balanced"


__all__ = [
    "DiscoveredModel",
    "DiscoveryResult",
    "discover_hermes_models",
    "suggested_tier",
]
