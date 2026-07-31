# Hermes Auto Router

Hermes Auto Router makes `/model auto` select from a shortlist of
OpenAI-compatible models you approve. It is deterministic, local, and focused:
no learned router, outcome tracking, prompt database, or remote routing service.

It installs two home-scoped Hermes components and runs a local gateway:

- `hermes-auto` model provider, advertising literal `auto` plus compatibility
  names.
- `hermes-auto-control` plugin, delegating commands to the absolute
  `hermes-auto` executable without importing this package in Hermes's virtual
  environment.
- A loopback-only OpenAI-compatible gateway that selects a candidate, preserves
  a model throughout a tool loop, and falls back safely.

The target Hermes Agent range is `>=0.19,<1.0`.

## Install and set up

Install the standalone package into its own environment, make `hermes-auto`
available on `PATH`, then run:

```console
hermes-auto setup
hermes-auto configure
```

`setup` installs both Hermes components, enables the control plugin, and writes
this alias while preserving unrelated Hermes configuration:

```yaml
model_aliases:
  auto:
    provider: hermes-auto
    model: auto
    base_url: http://127.0.0.1:8787/v1
```

That literal alias is why `/model auto` switches to Auto even if another
provider is currently selected.

`configure` asks the installed Hermes runtime for safe model metadata in a
read-only subprocess. Discovery returns model and provider identifiers,
endpoint/capability metadata, and credential environment-variable names only.
It never returns or stores credential values. If discovery is unavailable, the
command continues with manual configuration. Nothing is written until you
confirm the final shortlist.

For a source checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\hermes-auto.exe setup
.\.venv\Scripts\hermes-auto.exe configure
```

## Candidate configuration

Candidates live under `auto_router.candidates` in the Hermes `config.yaml`.
Order is the tie-breaker within a tier.

```yaml
auto_router:
  candidates:
    - id: fast-model
      provider: provider-name
      model: provider-model-id
      base_url: https://provider.example/v1
      credential_ref: env:PROVIDER_API_KEY
      tier: fast
      context_window: 128000
      supports_tools: true
      supports_vision: false
```

Every field is required. `tier` is `fast`, `balanced`, or `strong`.
`credential_ref` is `none` or `env:NAME`; the environment contains the actual
credential. V1 candidates must expose an OpenAI-compatible Chat Completions
endpoint.

One candidate is accepted for migration and testing, with a warning because
Auto cannot make a meaningful choice without at least two. Existing
`auto_router.upstream` configurations migrate in memory to one candidate.

## Routing behavior

Auto filters candidates that cannot satisfy declared tools, image input, or the
estimated context requirement. It then classifies each new user request:

- Tools: +1
- Latest user message at least 1,500 characters or containing fenced code: +1
- Estimated input 2,000–7,999 tokens: +1
- Estimated input at least 8,000 tokens: +2
- Image input: +2
- At least 20 messages: +1

Scores 0–1 request `fast`, 2–3 request `balanced`, and 4+ request `strong`.
The first eligible candidate at that tier wins. If the tier is unavailable,
stronger tiers are tried before weaker ones.

The route is recalculated on a new user message. Tool-result requests reuse the
same candidate, keeping the entire tool loop on one model. `auto:session` keeps
the normal selection pinned for the session.

Available virtual names are:

- `auto` and `auto:balanced`: normal policy
- `auto:quality`: strongest eligible tier
- `auto:economy`: fastest eligible tier
- `auto:session`: normal policy, session-pinned

## Fallback and streaming

The selected candidate is attempted first, followed by eligible peers in the
same tier, stronger tiers, then weaker tiers. Connection errors, timeouts,
`401`, `403`, `429`, and `5xx` responses permit fallback. Ordinary validation
`4xx` responses do not.

Streaming fallback is allowed only before the first upstream response byte.
Once a byte has been yielded, the route is committed and another model is never
spliced into that stream.

The gateway changes only the outbound `model` and removes private
`_hermes_auto` metadata. Other request fields, tool calls, SSE framing,
response headers, and cancellation behavior pass through opaquely.

## Commands

```console
hermes-auto start
hermes-auto stop
hermes-auto restart
hermes-auto status
hermes-auto doctor
hermes-auto overview
hermes-auto explain [--session-id ID]
hermes-auto configure
```

After setup, the corresponding Hermes surface is `hermes auto ...`. `/auto`
shows gateway health, candidate count, and the latest decision; `/auto explain`
shows the selected candidate/tier, detected tier, filters, fallback attempts,
and pin reason.

## Privacy and security

Routing state and recent explanations are bounded process memory only. The
router stores no prompts, message contents, raw session identifiers, training
data, user feedback, outcome scores, or telemetry database. A restart safely
re-evaluates the next request.

Both inference and admin listeners bind to loopback and use separate generated
bearer tokens. Provider credentials remain environment-variable references.
See [privacy](docs/privacy.md), [threat model](docs/threat-model.md), and
[security policy](SECURITY.md).

The previous research roadmap, ADRs, and planning history are retained as
historical context under [docs/research-archive](docs/research-archive/README.md);
they do not describe the active product.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
git diff --check
```

The focused active design is [design.md](design.md). The current implementation
architecture is [docs/architecture.md](docs/architecture.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE).
