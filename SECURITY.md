# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes — current pre-release development line |
| < 0.1   | No — no such release exists |

The project is pre-release. Until a 1.0 line exists, only the most recent `0.x` minor version
receives security fixes. Older `0.x` versions are not patched.

## Reporting a Vulnerability

Do **not** open a public GitHub issue for a security vulnerability.

Report privately to: `TODO: security contact`

Include, where possible:

- The affected version (`hermes_auto.__version__`) and platform.
- A description of the vulnerability and its impact.
- Reproduction steps or a proof of concept.
- Any suggested mitigation.

Expected handling:

1. **Acknowledgement** within 3 business days of receipt.
2. **Initial assessment** — severity and affected versions — within 10 business days.
3. **Fix or mitigation plan** communicated to the reporter before public disclosure.
4. **Coordinated disclosure** — a fix is released before details are made public. Reporters are
   credited unless they request otherwise.

## Security Posture

The router terminates a local HTTP listener that carries model traffic and provider credentials. The
following four invariants are hard requirements, not defaults to be relaxed by configuration:

1. **Loopback-only binding.** The gateway and the separately-scoped admin API bind to the loopback
   interface only. There is no supported configuration that exposes either listener on a routable
   address.
2. **Generated bearer token with restrictive file permissions.** Authentication uses a bearer token
   generated at setup time and stored in a token file with restrictive permissions (owner-only read
   and write). The token is never committed, logged, or included in diagnostics.
3. **No CORS.** The gateway sends no cross-origin resource sharing headers and does not honor
   cross-origin preflight requests. It is not a browser-reachable service.
4. **No raw prompt or secret retention by default.** The local telemetry store records routing
   evidence — decisions, usage, cost, health, and outcomes — but never raw prompts, tool-result
   bodies, or credential values. Session identifiers are salted hashes. External telemetry export is
   disabled by default and opt-in only.

Additional posture notes:

- Credentials are referenced by environment variable name (`env:VAR_NAME`) in configuration, never
  by value. No committed file in this repository contains a secret.
- Sidecar dependencies are installed into an environment isolated from the Hermes Agent environment.
- Route explanations are derived only from router inputs and must never leak credential material or
  hidden model reasoning.

The complete threat model, including the enumerated threats and their mitigating modules, lives in
[`docs/threat-model.md`](docs/threat-model.md).
