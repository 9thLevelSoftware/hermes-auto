# Hermes `/model auto` Design

## Product definition

Hermes Auto Router is a standalone Python package that makes the literal Hermes
command `/model auto` select an appropriate model from a user-approved shortlist.
It integrates only at Hermes's supported model-provider boundary and requires no
Hermes core patch.

The v1 product is intentionally deterministic. It contains no machine learning,
bandit, evaluation engine, model-card registry, persistent telemetry, outcome
optimization, provider-adapter framework, or remote routing service.

## Installed components

`hermes-auto setup` installs:

1. `plugins/model-providers/hermes-auto/__init__.py`, a dependency-free provider
   shim pointing Hermes at the local gateway.
2. `plugins/hermes-auto-control/plugin.yaml` and `__init__.py`, a dependency-free
   control plugin that delegates lifecycle and CLI commands to the absolute
   `hermes-auto` executable.
3. A literal `model_aliases.auto` mapping to provider `hermes-auto`, model
   `auto`, and the gateway `/v1` URL.

The control plugin is enabled idempotently. Managed files are atomically
replaced, ownership markers prevent accidental overwrite, and unrelated Hermes
configuration is preserved.

## Candidate contract

`auto_router.candidates` is an ordered, explicitly approved list. Every
candidate requires:

- `id`
- `provider`
- `model`
- `base_url`
- `credential_ref`
- `tier`
- `context_window`
- `supports_tools`
- `supports_vision`

The only tiers are `fast`, `balanced`, and `strong`. Credentials are `none` or
`env:NAME`; values are never copied into configuration. Candidates use
OpenAI-compatible Chat Completions endpoints. Configured order breaks ties
within a tier.

A legacy fixed `auto_router.upstream` block is treated as one balanced candidate
so existing installations remain usable until the user runs `configure`.

## Discovery and approval

`hermes-auto configure` runs the installed Hermes interpreter in a read-only
subprocess and requests configured, credentialed, OpenAI-compatible model
metadata. The subprocess returns provider/model identifiers, endpoint and
capability metadata, and the name of an available credential environment
variable. It never returns credential values.

Discovery supplies suggestions only. The user may add manual entries and must
confirm tiers and the complete final shortlist before an atomic write. Missing
Hermes or unsupported discovery metadata falls back to manual configuration.

## Eligibility

Input size is conservatively estimated as serialized message characters divided
by four. Ten percent of each context window is reserved for output.

A candidate is ineligible when:

- tools are declared and it lacks tool support;
- any message contains image input and it lacks vision support; or
- its context window cannot hold estimated input plus the output reserve.

## Complexity and selection

Balanced-mode complexity points:

- declared tools: +1
- latest user message at least 1,500 characters or contains fenced code: +1
- estimated input 2,000–7,999 tokens: +1
- estimated input at least 8,000 tokens: +2
- image input: +2
- at least 20 messages: +1

Scores 0–1 select `fast`, 2–3 select `balanced`, and 4+ select `strong`.
Selection takes the first eligible candidate at the requested tier, then tries
stronger tiers before weaker tiers, always preserving configured order.

Compatibility models:

- `auto` and `auto:balanced`: normal classification
- `auto:quality`: request `strong`
- `auto:economy`: request `fast`
- `auto:session`: normal classification pinned for the root session

`auto` is listed first by `/v1/models`.

## Turn stickiness

Each request is associated in memory with a salted hash of the root session.
A new request ending in a user message is classified again using a fingerprint
of message count and the latest user message. Requests ending in tool messages
reuse the turn's selection, so a tool loop stays on one model.

State is bounded process memory. It contains no prompt or message contents and
no raw session identifiers. After restart, the next request is safely
re-evaluated.

## Execution and fallback

The gateway has a reusable upstream client pool keyed by candidate. It rewrites
the outbound `model` to the selected real model and strips private Hermes Auto
metadata. All other request fields remain opaque.

Attempt order is selected candidate, eligible peers in its tier, stronger
tiers, then weaker tiers. Fallback is permitted for connection failures,
timeouts, missing credentials, `401`, `403`, `429`, and `5xx`. Ordinary
request-validation `4xx` responses are terminal.

For streaming, fallback is allowed only before the first response byte is
yielded. A committed stream is never spliced with another candidate. If all
candidates fail, the gateway returns an OpenAI-shaped error containing sanitized
candidate IDs and reason classes only.

## Explanations

The router retains a bounded set of prompt-free decisions in memory.
Authenticated `GET /admin/v1/decisions/{session_id}` exposes the latest matching
decision; `latest` retrieves the most recent decision overall.

`/auto` reports gateway health, configured candidate count, and the latest
decision. `/auto explain` and `hermes auto explain` report the selected
candidate/tier, detected tier, capability/context filters, fallback attempts,
and tool-loop or session pinning.

The product does not store prompts, messages, raw session identifiers, user
feedback, training data, outcome scores, or a telemetry database.

## Trust boundaries

- Inference and admin APIs bind to loopback only and use separate bearer tokens.
- Hermes-side shims contain no provider credentials and import no package code.
- Provider credentials are resolved from named environment variables only at
  the outbound request boundary.
- No CORS is enabled.
- URLs and upstream error bodies are excluded from aggregate fallback errors.

## Compatibility

The supported Hermes Agent range is `>=0.19,<1.0`. The focused package version
is `0.2.0`.
