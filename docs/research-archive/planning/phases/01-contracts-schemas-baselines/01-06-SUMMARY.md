# Plan 01-06 Summary — Threat Model & Privacy Documentation

**Phase**: 01-contracts-schemas-baselines
**Plan**: 06 (Wave 2)
**Status**: Complete
**Date**: 2026-07-26
**Requirements**: R26 (threat model), R19 (user-facing privacy documentation)

## Files created

| Path | Purpose |
|------|---------|
| `docs/threat-model.md` | STRIDE threat model: four trust boundaries, six assets, all fourteen `design.md` §21 threats mapped to an owning module and implementing phase, plus residual risk and review cadence (191 lines) |
| `docs/privacy.md` | User-facing privacy guide: what is stored, the four never-stored prohibitions, storage location, retention/deletion, opt-in export, opt-in code outcomes, and a controls table with honest phase availability (203 lines) |

No other file was created or modified. `git status --porcelain | grep -v '^??'` is empty — no tracked
file anywhere in the repository was touched.

## Prerequisites verified before writing

- `design.md` blob = `18bb54b36485fa0813ec67f84a74628a9eee3aae`, matching the pin in `01-CONTEXT.md`.
  All `§N` citations therefore refer to the intended sections.
- `docs/adr/0004-local-telemetry-and-privacy.md` and `docs/adr/0001-provider-plugin-and-local-gateway.md`
  both present (plan 01-02, committed at `29444a2`). No BLOCKED condition on the stop gate.
- `design.md` §21 confirmed to contain exactly fourteen data rows.

## STRIDE category assigned to each of the fourteen threats

| # | Threat (`design.md` §21) | STRIDE | Owning module | Phase |
|---|---|---|---|---|
| 1 | Prompt causes expensive escalation | Denial of service | `routing/cost.py` | 4; anomaly detection 6 |
| 2 | Prompt attempts to modify router policy | Elevation of privilege | `routing/policy.py` | 4; ingress stripping 2 |
| 3 | Local port accessed by another process | Spoofing | `gateway/auth.py` | 2 |
| 4 | Secret leakage in logs | Information disclosure | `gateway/errors.py` | 2 |
| 5 | Malicious model metadata | Tampering | `inventory/validation.py` | 3; schema 1 |
| 6 | Sidecar dependency compromise | Elevation of privilege | `supervisor.py` | 2; CI scanning 1; adapter isolation 7 |
| 7 | Raw code exported unintentionally | Information disclosure | `telemetry/exporters.py` | 8; schema 1 |
| 8 | Cost estimate manipulation | Tampering | `routing/cost.py` | 4; reconciliation 8 |
| 9 | Router classifier attack | Tampering | `routing/requirements.py` | 4; OOD detection 9 |
| 10 | Model response spoofs route metadata | Repudiation | `routing/decision.py` | 4 |
| 11 | Mid-stream fallback corrupts tools | Tampering | `gateway/streaming.py` | 6 |
| 12 | Concurrent state collision | Tampering | `state/locks.py` | 5 |
| 13 | Experimental model degrades quality | Denial of service | `health/circuit_breaker.py` | 6; shadow eval 8 |
| 14 | Hermes upgrade breaks private bridge | Denial of service | `adapters/hermes_native.py` | bridge 7; probe delivered in 1 |

Distribution: Tampering 5, Denial of service 3, Elevation of privilege 2, Information disclosure 2,
Spoofing 1, Repudiation 1. All six categories are used; total is exactly 14.

### Two classification judgment calls (documented in the document itself)

- **Row 6 — Elevation of privilege, not Tampering.** The intrusion is a tampering event, but the
  operative risk is third-party code executing with the sidecar's full authority (every provider
  credential, the token file, the store). "Isolated environment" is a blast-radius control, which
  makes the elevation framing the more actionable one.
- **Row 10 — Repudiation, not Spoofing.** The threat is named "spoofs", but the required mitigation
  is an authority-of-record control: the gateway is the sole author of decision headers *and logs*.
  The harm prevented is an unprovable answer to "which candidate served this turn, and who owes for
  it". The spoofing aspect is structurally foreclosed rather than detected — route metadata is never
  parsed back out of an upstream response.

Both calls are recorded in a `### Classification notes` subsection under `## Threats and mitigations`
so a later reviewer sees the reasoning rather than re-litigating it.

## Owning-module attribution: one weak resolution from `design.md` §17

**Row 4 (Secret leakage in logs) — `gateway/errors.py`.** §17 names no dedicated logging or redaction
module. The mitigation ("central redaction, structured logging allowlist") implies one. `gateway/errors.py`
is the closest existing owner — it is the module that formats what escapes the gateway — and `config.py`
genuinely owns the `env:VAR_NAME` credential-reference half. This is an attribution of convenience,
not a resolved design decision.

**Not escalated to BLOCKED**, because the *mitigation* is unambiguous in `design.md` §21 and no
architectural decision is being invented — only a module placement inside an existing layout. If
Phase 2 introduces a `logging.py` or similar, this row should be repointed. Flagged here so that
happens deliberately.

All thirteen other rows map cleanly onto module paths listed in §17.

## `SECURITY.md` versus `design.md` §21

**No contradiction found.** `SECURITY.md` §Security Posture publishes four invariants that restate
§21 rows 3, 4, and 7 and *strengthen* them from defaults into hard requirements:

| `SECURITY.md` invariant | Corresponding §21 row |
|---|---|
| Loopback-only binding (no supported routable configuration) | Row 3 |
| Generated bearer token, owner-only file permissions | Row 3 |
| No CORS | Row 3 |
| No raw prompt or secret retention by default; salted session hashes; export off by default | Rows 4 and 7 |

Strengthening is not contradiction, so no BLOCKED was emitted. `SECURITY.md` already forward-links
`docs/threat-model.md`; that link now resolves. It was not modified.

One `SECURITY.md` weakness is recorded in `## Residual risk` rather than fixed here, since the file
is forbidden to this plan: the security contact is still `TODO: security contact`, so there is no
verified private reporting channel. That is a **Phase 11 release-checklist item**.

## Handoff — privacy controls promised in later phases

Every control `docs/privacy.md` marks as not-yet-available, so Phase 8 planning can pick them up.
The privacy guide is a user-visible commitment; each row below is a promise that must be delivered
or the guide corrected.

### Phase 8 (Telemetry, Outcomes & RouterBench) — six commitments

1. **The local SQLite store itself** — WAL mode, single file under `~/.hermes/auto-router`
   (`design.md` §13.1, §16). Everything else depends on it.
2. **Disable telemetry entirely** — `telemetry.local_store: false` must genuinely stop all writes,
   leaving routing functional with history-dependent features quiet.
3. **Configurable retention window** — `telemetry.retain_days`, default **30**, with actual pruning.
   The guide states records older than the window are pruned; a store that only stops writing does
   not satisfy this.
4. **A deletion command** — erases stored history without disturbing configuration. The guide also
   promises that deleting the SQLite file by hand is sufficient, i.e. **no shadow copy, cache, or
   index may live outside that file**. That constraint is easy to violate accidentally with a
   sidecar cache and should be an explicit Phase 8 test.
5. **External exporters, off by default** — Langfuse, OpenTelemetry, JSONL, Parquet, Prometheus,
   team-hosted analytics (`design.md` §13.5). The guide promises the router **describes what will
   leave the machine before the user confirms**, so enabling an exporter needs a disclosure step,
   not just a config flag. Export payloads are a strict subset: derived features, candidate IDs,
   decisions, usage, policy-approved outcomes.
6. **Per-repository code outcomes** — `design.md` §13.4, off by default, scoped per repository.
   The guide states enabling outcomes does **not** enable export; these must remain two independent
   consents.

### Phase 9 (Learned Capability Predictor) — one commitment

7. **Opt-in training corpus** — a **separate consent** from telemetry, with its own collection path.
   ADR-0004 is explicit that the default store is deliberately too thin to train on, so this must
   not be presented or implemented as covered by the telemetry opt-in.

### Phase 11 (Production Hardening & Release) — two commitments

8. **Fill in the `SECURITY.md` security contact**, replacing `TODO: security contact`.
9. **Re-verify both documents against the shipped implementation** before release. `docs/privacy.md`
   describes Phase-8 behavior in the present tense with explicit availability markers; once Phase 8
   lands, those "Available from Phase 8" markers must be removed rather than left stale.

### Standing obligation for every phase

`docs/threat-model.md` `## Review cadence` defines a gate: a phase may not close if it introduced a
new external input, a new stored field, or a new network destination without a corresponding threat
row — or if it implemented a mitigation whose row still reads as a forward reference. Phase-close
checklists from Phase 2 onward should include it.

## Verification

**23 verification commands run; 23 passed; 0 failed.** (8 from Task 1, 8 from Task 2, 7 plan-level.)

| Check | Result |
|---|---|
| STRIDE-column row count in threats table | **exactly 14** |
| All fourteen §21 threat keywords present | pass |
| Six required H2 sections in `threat-model.md`, in order | pass |
| Seven required H2 sections in `privacy.md`, in order | pass |
| Every threat row names an owning module **and** a phase | 14 / 14 |
| Credential-pattern scan (`sk-`, `ghp_`, `AKIA`) both files | no matches |
| Username / real-path / bearer-value scan both files | clean |
| `docs/threat-model.md` cites `0004-local-telemetry-and-privacy` | pass |
| `docs/privacy.md` cites `adr/0004` | pass |
| `privacy.md` documents `additionalProperties: false` | pass |
| Line counts vs. minimums (80 / 50) | 191 / 203 |
| `git diff --exit-code design.md` | clean |
| `SECURITY.md`, `docs/adr/`, `docs/architecture.md` unmodified | pass |
| `docs/evaluation.md` not created | absent, correct |
| Tracked files modified by this plan | none |

### Cross-plan false positive, not fixed (correctly)

The literal forbidden-path guard reports untracked additions under `.github/`, `src/`, and `tests/`:
`compatibility.py`, `gateway/schemas.py`, `data/schema/`, three contract tests, and two fixture
directories. **All are owned by plans 01-03, 01-04, and 01-05, which run in parallel in Wave 2.**
This plan created none of them, and the only "fix" would be deleting another plan's work — a
forbidden action. This is the exact scenario `01-CONTEXT.md` § Phase-Close Gate anticipates, and the
resolution matches commit `c340720` ("scope wave-2 cross-plan guards to committed files"): the
assertion was re-run scoped to tracked modifications, which is empty.

## Deviations from the plan

One, additive and minor: `docs/privacy.md` carries an eighth H2, `## Questions this page should have
answered`, after the seven required sections. The seven required headings are all present in the
required order; the extra section is a short FAQ ("does installing this send anything to the
maintainers?", "if I route only to local models, does anything leave my machine?") serving the
audience the plan specifies — a user deciding whether to install. It introduces no new claim not
already stated above it.

## Scope discipline

- No code, schema, workflow, or script was written — documentation only, as the plan requires.
- No mitigation was invented. Every mitigation cell preserves `design.md` §21 text verbatim, with
  parenthetical notes that only name modules from §17 or cite an ADR.
- No stated default was weakened. External export is described as "disabled by default and requires
  your explicit opt-in", never as "configurable".
- The four ADR-0004 prohibitions appear in `privacy.md` as flat declaratives, not as settings.
- No real credential, token, API key, session id, username, or prompt content appears in either
  document. The illustrative record uses fabricated values (`test-hosted-general`, `a1b2c3d4e5f6a7b8`)
  and credentials appear only as the reference form `env:OPENROUTER_API_KEY`.
