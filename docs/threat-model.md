# Threat Model

## Trust boundaries

1. **Hermes to local gateway.** Untrusted request data crosses a loopback
   OpenAI-compatible endpoint authenticated by the inference token.
2. **Gateway to model providers.** Prompts and provider credentials cross to one
   of the explicitly approved upstream endpoints.
3. **Operator to admin API.** Health, explanations, and shutdown use a separate
   listener and separate bearer token.
4. **Hermes control plugin to standalone executable.** A dependency-free shim
   starts a fixed absolute executable path.

There is no persistent telemetry boundary: routing decisions are bounded
process memory only.

## Threats and mitigations

| Threat | Impact | Mitigation |
|---|---|---|
| Non-local access to inference or admin APIs | Prompt theft, spend, shutdown | Both listeners validate loopback binding; no routable bind option |
| Inference token used for administration | Information disclosure or shutdown | Separate token file and authentication middleware for the admin listener |
| Browser page calls loopback service | Cross-origin request abuse | No CORS middleware or access-control headers |
| Malformed or future OpenAI request fields are lost | Broken tools or behavior | Opaque mapping relay; only `model` and private metadata are changed |
| Private session metadata reaches a provider | Session disclosure | `_hermes_auto` is stripped immediately before upstream transmission |
| Credential copied into configuration or discovery | Secret disclosure | Only `none` or `env:NAME`; discovery returns names, never values |
| Credential or URL leaks through logs/errors | Secret disclosure | Central redaction, sanitized candidate IDs/reason classes, no upstream error bodies in aggregate failures |
| Candidate cannot satisfy tools, images, or context | Invalid or degraded completion | Deterministic eligibility filters before selection and fallback |
| Retry sends a validation-invalid request elsewhere | Duplicate invalid calls | Ordinary `4xx` is terminal; fallback is limited to transport, credential, `401`, `403`, `429`, and `5xx` failures |
| Model changes mid-tool-loop | Tool-call inconsistency | Turn-level route stickiness for requests ending in tool messages |
| Model changes after stream output begins | Corrupted mixed-model stream | First-byte commit barrier; no fallback after the first yielded byte |
| Candidate ordering is manipulated by prompt text | Spend or quality manipulation | Policy uses declared request structure and fixed thresholds, never instructions embedded in prompt content |
| Managed plugin file overwrites user code | Local data loss | Ownership markers, preflight validation, atomic replacement, rollback |
| Hermes environment imports missing package | Plugin startup failure | Home control shim uses only standard library and invokes an absolute executable |
| Decision explanations reveal prompts or session IDs | Privacy loss | Salted/hashes identifiers, bounded reason codes, no content fields |

## Residual risks

- An approved provider receives request content and applies its own security and
  retention policy.
- A malicious process running as the same OS user may read environment variables,
  token files, or process memory.
- Character-count token estimation is conservative but not tokenizer-exact; a
  provider may still reject an edge-case context.
- Model capability flags are user-approved metadata, not independently verified.
- A crash after a stream is committed terminates that response; it cannot safely
  fall back without splicing models.

See [SECURITY.md](../SECURITY.md) for private vulnerability reporting.
