# Architecture

Hermes Auto Router uses the supported provider boundary and a local sidecar:

```text
Hermes
  │  /model auto resolves through model_aliases.auto
  ▼
home model-provider shim (hermes-auto)
  │  OpenAI Chat Completions + private session envelope
  ▼
loopback gateway
  ├─ authenticate
  ├─ strip private metadata
  ├─ classify and select an eligible candidate
  ├─ preserve turn/session stickiness
  ├─ rewrite model and relay opaquely
  └─ fall back before response commitment
  ▼
approved OpenAI-compatible provider candidate
```

## Hermes boundary

Setup writes two independent home-scoped plugins. The provider shim supplies a
stable `hermes-auto` provider profile and virtual names. The control shim
registers `hermes auto ...`, `/auto`, and the session-start hook. Both are
dependency-free because Hermes runs them in its own virtual environment.

The control shim invokes the absolute standalone executable. It never imports
`hermes_auto`, so setup remains valid when the package is absent from Hermes's
environment.

## Gateway boundary

The inference listener exposes:

```text
POST /v1/chat/completions
GET  /v1/models
GET  /healthz
GET  /readyz
```

The separately authenticated admin listener exposes:

```text
GET  /admin/v1/status
GET  /admin/v1/decisions/{session_id}
POST /admin/v1/shutdown
```

The admin API has no routing mutation, feedback, or telemetry endpoints.

## Request path

The gateway parses the request into a generic mapping. It validates the private
envelope, estimates message size, detects tools/images, and asks the deterministic
selector for an ordered candidate list. The selected candidate determines the
upstream client, credential environment variable, endpoint, and real model.

Immediately before transmission, only two transformations occur:

1. `_hermes_auto` is removed.
2. `model` is replaced with the selected candidate's model ID.

Unknown current or future OpenAI fields remain untouched. Responses and SSE
frames are relayed without response modeling.

## In-memory routing state

`DecisionRouter` owns bounded dictionaries for turn/session choices and recent
explanations. Root session IDs are salted and hashed; fingerprints are hashes of
message count plus the latest user message. Neither raw value is retained.

New user turns re-evaluate. Tool-result continuations reuse the turn route.
`auto:session` reuses a route for the session. Restarting clears state and causes
safe re-evaluation.

## Fallback boundary

`UpstreamClientPool` creates one reusable client per candidate. Attempt order is
computed once from eligible candidates. Retryable errors advance through that
order; validation errors return immediately.

For streams, the gateway reads the first upstream byte before committing the
response. An error before that byte can fall back. Once the first byte is
yielded, failure terminates the stream and never changes models.

## Configuration boundary

Behavior lives in `auto_router` within Hermes `config.yaml`. Provider secrets do
not: candidates contain `env:NAME` references only. Configuration loading is
strict for owned keys and preserves the legacy `upstream` shape as a one-model
migration path.

The complete active contract is [design.md](../design.md). Historical research
material is in [research-archive](research-archive/README.md).
