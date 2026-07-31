# Plan 02-08 Summary — Differential Harness, Chunk-Boundary Property Tests, TTFT Gate

**Status**: Complete
**Wave**: 5 · **Agent**: testing-api-tester (+ testing-performance-benchmarker concerns)
**Requirements**: R5
**Verification**: 21 run / 21 passed, plus 12 non-vacuous replacements
**Tests**: 761 → 882 passing (+121)

## Files (5, all new)

`tests/differential/{__init__.py,harness.py}` (757 lines),
`tests/differential/test_frame_identity.py` (789 lines, 74 tests),
`tests/property/test_chunk_boundary_invariance.py` (406 lines, 38 tests),
`tests/performance/test_ttft_overhead.py` (660 lines, 9 tests).

## R5 verdict: byte-identity holds on all 12 fixtures

No divergence found. Every fixture matches direct-vs-gateway on both reductions, on status, on
transport-failure outcome, and on every non-hop-by-hop header — against a **real detached uvicorn
sidecar** started via `supervisor.start()` (asserted `record.pid != os.getpid()`).

- `fragmented_utf8` relays its raw 2/3/4-byte sequences with no replacement character and both lone
  surrogate halves intact
- `empty_deltas_and_keepalives` keeps CRLF while `text_stream` keeps LF **on the same gateway**
- `usage_only_final_chunk`'s `choices: []` frame survives
- `midstream_disconnect` fails identically both ways (`RemoteProtocolError`, same 498 bytes first,
  no `[DONE]`)

## The harness was proven able to go red

16 injected corruptions, each caught: dropped usage chunk, dropped `[DONE]`, reordered frames, flipped
byte, merged frame terminator, changed model / tool-call id, lost and corrupted argument fragments,
per-chunk decode at a mid-codepoint split, joined surrogates, swallowed keepalives, CRLF→LF, dropped
truncated tail.

Four of those (reorder, keepalive swallow, CRLF→LF, tool-call reassembly) are **deliberately invisible
to the semantic reduction** and listed as such — that pair of results *is* the diagnosis "reframed, not
corrupted." Transport re-chunking is invisible to both, and that is asserted too.

## Measurements (quiet machine, n=30 paired, 300 ms injected first-byte delay)

| | p50 delta | **p99 delta** | allowance |
|---|---|---|---|
| strict off, small body | 3.038 ms | **4.143 ms** (1.37% of direct p99) | 100 ms |
| strict on, small body | 3.243 ms | 4.237 ms | 100 ms |
| strict off, 155 KB body | 4.864 ms | 20.355 ms | 100 ms |
| strict on, 155 KB body | 25.736 ms | 29.740 ms | 100 ms |

**Independently confirms 02-04: keep `strict_validation` off.** Median added TTFT attributable to it on
a 155 KB / 161-message body is **20.9 ms — 21% of the whole budget** against a 2 ms flip condition,
consistent with 02-04's 29.06 ms in isolation. The flag was proven *live in the measured process* by an
off-schema probe (200 with it off, 400 with it on), not merely written into a config file. The default
was not touched.

## Fuzzer

`MAX_EXAMPLES = 60` × 4 fixtures × 2 strategies; the module runs in **~10 s** (11.7 ms/example). One
re-split class excluded: a zero-length chunk, because that *is* the HTTP chunked terminator and
`set_rechunk` rightly rejects it. Splits inside `data: [DONE]` are **not** excluded — pinned explicitly.
Re-chunking is proven observable end to end: the same 1311 bytes arrive in 8 / 1311 / 1 client chunks
with identical content.

## Auto-remediated

1. Harness switched to persistent `httpx.Client`s per deployment (was one per request) — cut the fuzzer
   25 s → 10 s and halved socket churn.
2. TTFT gate gained a **validity precondition** after it breached at 224 ms p99 while a sibling plan
   hammered the machine. Two controls — the gateway-free direct series and `GET /healthz` on the
   sidecar — must each keep p99 within 25 ms of their median, else the gate *skips* and prints. Not a
   weakening: a constructed test proves it fires on in-process jitter, fires on a starved sidecar, and
   **does not** fire on a uniformly 500 ms-slow relay.

## Decisions a reviewer should know

- `run_direct`/`run_through_gateway` take a second `Deployment` argument and return a `RunResult`
  (whose `.frames` is the specified `list[bytes]`). A module global would break the fuzzer, and
  "compare status/headers" and "both must fail the same way" are not expressible in a byte list.
- `byte_reduction`'s two rewrites are anchored to the start of a `data:` line, so a **tool-call** `"id"`
  stays under full comparison. Proven: changing `call_test0004` turns it red.
- `semantic_reduction` is deliberately blind to framing (no event count, no keepalive list). Including
  them would make both reductions say the same thing and cost the harness its ability to explain a
  difference.

## Issues raised (real, out of scope here)

1. **`error.code` integer — the schema is wrong, not the assertion.** All three error fixtures carry
   string codes; an integer `code` is rejected by `openai-error.v1`
   (`429 is not of type 'string', 'null'`). That schema's own description calls itself a relay schema
   whose closure "would reject traffic the gateway is required to pass through" — `["string","null"]`
   is exactly that closure on a named field. **Recommend widening to `["string","integer","null"]`.**
   `src/` was forbidden here. `test_an_integer_error_code_fails_the_schema_but_not_the_relay` is a
   labelled **tripwire** that fails the moment the schema is widened — delete it then.
2. **The TTFT gate is load-sensitive** — breached at 224 ms and 282 ms p99 during concurrent activity,
   passed at 4.1 / 4.5 / 5.6 / 6.3 ms in quiet runs. Already skipped under `CI`; the new controls now
   report contaminated runs as invalid rather than as failures.
3. Did **not** commit — plan 02-09's files were uncommitted in the same tree.
