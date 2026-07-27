# Hermes Auto Router

An out-of-tree Python distribution that gives [Hermes Agent](https://github.com/NousResearch/hermes-agent)
a capability-, cost-, cache-, and outcome-aware "Auto" model. It ships three cooperating pieces: a
`hermes-auto` model-provider plugin, a `hermes-auto-control` control plugin, and a local
OpenAI-compatible routing gateway that runs as a supervised sidecar. Hermes continues to believe it is
talking to one stable provider and one virtual model (`auto:balanced`); the gateway selects the real
model and provider behind that boundary, then normalizes the response.

The router combines GitHub HyDRA's safety architecture — predict model-independent capability
requirements, match against external model cards, pick the cheapest sufficiently capable candidate,
keep the learned layer subordinate to deterministic policy — with Cursor Router's practical agent
concerns: selectable cost-quality modes, domain affinity, cache-aware stickiness, model-specific
harnesses, and outcome-driven improvement. Critically, it does this at the **supported provider
boundary** rather than inside `AIAgent`: no Hermes core modification, no private runtime mutation, no
fork maintenance, and no conflict with upstream policy.

## Architecture

```text
Hermes CLI / TUI / Gateway / Desktop / Cron / Subagents
                         │
                         ▼
                 Hermes AIAgent
                         │
                         ▼
           ProviderProfile: hermes-auto
           Virtual model: auto:balanced
                         │
                  localhost HTTPS/HTTP
                         │
                         ▼
              Hermes Auto Router gateway
 ┌─────────────────────────────────────────────────────────────┐
 │ Request canonicalization                                    │
 │ Session, lane, turn, and cache-epoch tracking               │
 │ Hard policy and compatibility filters                       │
 │ Capability requirement prediction                           │
 │ Model-card matching and expected-cost calculation           │
 │ Cache-aware stickiness and switch hysteresis                │
 │ Provider/model health veto                                  │
 │ Model-specific request and response adapters                │
 │ Local telemetry, outcomes, replay, and explanations         │
 └─────────────────────────────────────────────────────────────┘
           │                 │                 │
           ▼                 ▼                 ▼
    Local models       Aggregators       Direct providers
 Ollama/vLLM/etc.      OpenRouter/etc.   OpenAI/Anthropic/etc.
```

## Status

**Phase 1 — contracts and schemas, pre-implementation.**

This repository currently contains the packaging contract, the package skeleton, versioned schemas,
architecture decision records, the threat model, and a fixed-model baseline harness. **No production
routing logic exists yet**: feature extraction, eligibility filtering, scoring, and candidate
selection arrive in later phases. The public contracts are being frozen first, deliberately, so that
routing is built against schemas that are already stable.

Nothing here is usable as a Hermes provider yet.

## Install

**Not yet available.** The distribution is not published to PyPI and has no working entry points.

Once a release exists, installation will be `pip install hermes-auto-router` into an environment
isolated from the Hermes Agent environment. Until then, only a development checkout is meaningful:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"   # .venv/Scripts/python.exe on Windows
.venv/bin/python -m pytest tests/ -q
```

## Documentation

- [Architecture and decision records](docs/architecture.md)
- [Threat model](docs/threat-model.md)
- [Privacy and telemetry](docs/privacy.md)

The full design specification lives in [`design.md`](design.md) at the repository root.

## Security

See [SECURITY.md](SECURITY.md) for the vulnerability disclosure process and the router's security
posture: loopback-only binding, generated bearer-token authentication, no CORS, and no raw prompt or
secret retention by default.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
