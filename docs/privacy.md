# Privacy

This page explains, in plain terms, what the Hermes Auto Router keeps, what it refuses to keep, and
what you can turn off. It is written for someone deciding whether to install this.

The router sits between Hermes and the model providers you use. Everything you type passes through
it. That is a position of considerable trust, and the short version of how it is handled is this:

> **The router records what it decided, not what you said.** Nothing leaves your machine unless you
> explicitly turn on external export, which is off by default.

The reasoning behind these choices is recorded in
[`docs/adr/0004-local-telemetry-and-privacy.md`](adr/0004-local-telemetry-and-privacy.md), the
authoritative decision record. If you want the adversarial view — what an attacker would try and what
stops them — read [`docs/threat-model.md`](threat-model.md).

**What exists today.** The project is in Phase 1, which defines contracts and schemas. The local
store itself is built in Phase 8. Until then the router stores nothing at all, because there is
nothing to store into. This page therefore separates two things throughout: what the shipped
schemas already lock in, and what depends on code that arrives in a later phase. Each user control
is marked with the phase that delivers it. Phase numbers follow the project roadmap.

---

## What the router stores

The local store holds **derived facts about routing decisions** — the inputs the router computed, the
choice it made, and what happened as a result:

- The routing decision: which candidate was selected, the scores behind it, and the reason codes.
- Candidate identifiers — which model at which provider, as an identifier, not a copy of your request.
- Token counts (input, output, cached, reasoning), estimated and actual cost, and latency including
  time to first token.
- Health observations: success and failure rates, rate-limit responses, stream failures, empty
  responses.
- Tool-call counts and invalid tool-call counts — **counts**, never the calls themselves.
- Retry counts, fallback counts, whether context compression happened, whether you interrupted.
- Explicit feedback you choose to give, such as a thumbs-down on a turn.

A stored **outcome event** looks roughly like this. Every value here is fabricated for illustration,
but the record validates against the shipped schema exactly as written:

```json
{
  "event_id": "evt_9c41ba07",
  "event_type": "request_completed",
  "occurred_at": "2026-07-26T14:31:07Z",
  "root_session_hash": "4276dacc373e311a5b4bbb78176abba749a0a54352a93c2824c838d1d51f0675",
  "lane_id": "lane_main",
  "decision_id": "dec_9c41ba07",
  "candidate": "test-hosted-general",
  "ttft_ms": 480,
  "total_latency_ms": 3120,
  "input_tokens": 4210,
  "output_tokens": 260,
  "cached_tokens": 3968,
  "actual_cost_usd": 0.0031,
  "tool_call_count": 2,
  "invalid_tool_call_count": 0,
  "turn_succeeded": true
}
```

Two schemas divide a routing record between them, and it is worth being precise about which holds
what:

- **`outcome-event.v1`** — the block above. Identifiers and measurements of what actually
  **happened**: what was routed, how long it took, what it really cost, whether it worked.
- **`route-decision.v1`** — the companion explainability record, holding what the router
  **predicted**: the capability requirement vector across eight named dimensions (`reasoning`,
  `code_generation`, `debugging`, `tool_orchestration`, `long_horizon_execution`,
  `context_synthesis`, `structured_precision`, `multimodal_reasoning`); the ranked candidates, each
  carrying its `estimated_cost` and `estimated_ttft_ms`; and the reason codes drawn from a frozen
  twelve-value vocabulary — `CAPABILITY_FIT`, `COST_TIEBREAK`, `STICKY_CACHE`, `HEALTH_VETO`, and
  eight others.

So *estimated* cost and *estimated* time to first token live on the decision, and *actual* cost and
*actual* latency live on the outcome. The two are joined by **`decision_id`**, which is what lets a
prediction be scored against its result.

Neither shape is a convention a future contributor could quietly widen. Both schemas ship in Phase 1
and both set `additionalProperties: false`, so each **rejects anything outside its own list** — a
record containing a field the schema does not name fails validation and is not written. That is a
real guarantee, and it is worth making concrete: if someone later added code that tried to write a
`prompt` field into the store, the write would **fail validation rather than silently start
collecting**. The constraint is enforced by the data format, not by a reviewer remembering it.

It is just as important to be clear about the limit of that guarantee, because the rest of this page
rests on it. **A schema bounds the shape of what can be written, not the intent behind it.** It
fixes which fields exist and constrains each one to a type, a length, a character class, an enum, or
a numeric range. It cannot inspect whether the value a field was handed is the value that field was
meant to hold. `route-decision.v1` also carries one deliberate free-text field —
`excluded[].reasons[]`, router-authored diagnostics such as `context window 65536 < required 84211`,
capped at 200 characters per entry. That cap is far too small for a prompt, but note what it bounds:
each individual string, not the total text a record accumulates, which also depends on how many
exclusion entries the record carries. A per-entry limit is a weaker guarantee than it first reads
as.

The accurate summary is therefore a two-part one, and this page uses it throughout: **the schemas
guarantee shape today; the Phase 8 write path is what will guarantee content.** Phase 1 ships no
write path at all, so none of this is exploitable now — but any claim of the form "X can never be
stored" is partly a claim about code that has not been written yet. Where that is the case below,
this page says so and names the module that will carry the invariant.

---

## What the router never stores

Four prohibitions. These are hard defaults — how the product behaves out of the box, with no
configuration — not settings you need to find and enable. For each one, this page states separately
what the shipped schemas enforce and what the Phase 8 write path must enforce, because those are
different guarantees with different maturity.

1. **Raw prompt text is never stored.** Not truncated, not summarized, not hashed-and-kept. The
   router computes numeric features from your request, uses them to pick a model, and discards the
   text. What survives is a score, not a sentence.

   *Enforced by the schemas today:* neither record defines a field for prompt text, and
   `additionalProperties: false` means one cannot be added without a schema version bump. In
   `outcome-event.v1` every string field is additionally value-constrained: `event_id`, `lane_id`
   and `candidate` are bounded identifier tokens, `event_type` and `feedback` are enums,
   `occurred_at` is an anchored timestamp pattern, `root_session_hash` a fixed-width digest, and
   `error_class` a classification capped at 64 characters. None of them is a free-text channel.
   What that buys is real but narrow: it means prompt text has nowhere obvious to go, not that the
   schema has checked where each value came from.
   *Left to Phase 8:* the write path itself. `telemetry/events.py` is where an event is built from a
   live request, and it is the code that must compute features and drop the text. A schema can
   refuse a field it does not know; it cannot verify that a bounded field was filled from a score
   rather than from a sentence.

2. **Tool-result bodies are never stored.** When a tool reads a file, runs a test, or searches your
   codebase, the router records that a tool call happened and whether it succeeded. The file
   contents, the test output, and the search results are not part of the record.

   *Enforced by the schemas today:* `outcome-event.v1` carries `tool_call_count` and
   `invalid_tool_call_count` as non-negative integers and defines no field for a tool name, a tool
   argument, or a tool result. `error_class` is a short classification such as `rate_limit` or
   `context_overflow`, capped at 64 characters, not a captured error body.
   *Left to Phase 8:* nothing writes these counts yet, and the same write-path caveat applies.

3. **Secrets and credential values are never stored.** Configuration refers to credentials by
   environment-variable name — `env:OPENROUTER_API_KEY` names the variable; the key itself is not
   written to a config file, a model card, a log line, or the store.

   *Enforced by the schemas today:* less than that sentence on its own suggests, and this is the
   honest place to say so. No field is *for* a credential, and no field accepts free-form text of
   credential length. But a credential has no distinguishable shape. A GitHub personal access token
   is forty alphanumeric characters, which satisfies the length bound and identifier character class
   on `event_id`, `lane_id`, and `candidate` exactly as comfortably as a real identifier does. The
   schema cannot tell them apart, because "is this string a secret" is a question about where the
   value came from, not about what it looks like.
   *Left to Phase 8:* effectively all of this prohibition. It is a write-path invariant — those
   fields are populated from router-internal identifiers, never from request content or environment
   values — and it belongs to `telemetry/events.py`.

4. **Session identifiers are hashed with a local salt.** The salt is generated on your machine and
   stays there, so a stored record cannot be linked back to a Hermes session id, and the same
   conversation on two machines produces two unrelated hashes.

   *Enforced by the schemas today:* `root_session_hash` is constrained to `^[0-9a-f]{64}$` in both
   `outcome-event.v1` and `route-decision.v1`. That pattern bounds the field to a fixed-width
   lowercase hex digest, so a raw Hermes session id, a prompt, or a credential cannot be written
   there — none of them fits the shape, and a code path that tried would fail validation.
   *What the pattern does not do:* check that the digest was salted. An unsalted
   `sha256(session_id)` is also sixty-four lowercase hex characters and validates identically. Such
   a digest would be stable across every machine running the same session, and reversible by anyone
   willing to enumerate the session-id space. The pattern enforces that the value *looks like* a
   SHA-256; whether it *is* a salted SHA-256 is a different property, and no schema can see it.
   *Left to Phase 8:* the salting itself. Generating a per-install salt, keeping it on the machine,
   and applying it before the digest is written are all properties of the telemetry write path —
   `telemetry/events.py` builds the record, `telemetry/sqlite.py` stores it — and they arrive with
   Phase 8. **Phase 1 provides no structural backstop for them.** The tests that pin the invariant
   land with that code: the same session id hashed under two different salts must produce two
   different digests, and the raw id must never reach the field.

---

## Where data lives

One **SQLite file**, in WAL mode, under the router's state directory — `~/.hermes/auto-router` by
default. That is the whole store. There is no cloud account, no hosted backend, and no telemetry
endpoint to configure, because there isn't one.

Using an ordinary SQLite file is a deliberate choice: you can open it with any standard tool and read
every row yourself. The claims on this page are checkable rather than something you have to take on
faith.

The router's local gateway binds to the **loopback interface only** and requires a bearer token
generated during setup, stored with owner-only file permissions. It is not reachable from your
network, and it sends no cross-origin headers, so a web page cannot talk to it either.

**Availability:** the gateway does not exist yet. It, the loopback binding, and the generated bearer
token all arrive in **Phase 2**, and there is no setup command today, so nothing generates a token
either. [`SECURITY.md`](../SECURITY.md) publishes loopback-only binding, the generated bearer token
with restrictive file permissions, and the no-CORS rule as **hard requirements the implementation
must meet** rather than behavior that exists now; [`docs/threat-model.md`](threat-model.md) assigns
them to `gateway/auth.py` in Phase 2.

Nothing leaves your machine except the model requests themselves, which go to the provider the router
selected — the same providers Hermes would have called anyway.

---

## Retention and deletion

The default **retention** window is **30 days**. Records older than that are pruned. The window is
yours to change, including shortening it to a day or extending it if you are running an evaluation.

You can **delete** the store at any time, in either of two ways:

- Run the deletion command, which removes stored history without disturbing your configuration.
- Delete the SQLite file yourself. Nothing outside it holds a shadow copy, so removing the file
  removes the history. The router recreates an empty store on next start.

**Availability:** the store, the configurable retention window, and the deletion command all arrive in
**Phase 8**, when the telemetry implementation lands. They do not exist yet. Nothing is being
collected in the meantime, so there is nothing to prune or erase today.

---

## External export

**External export is disabled by default and requires your explicit opt-in.** This is not a
recommended setting; it is the shipped default, and it reflects a requirement from Hermes's
contribution policy that outbound telemetry and attribution be gated behind an explicit, user-facing
opt-in.

If you do turn it on, the supported exporter targets are:

- Langfuse
- OpenTelemetry
- JSONL files
- Parquet files
- Prometheus metrics
- Team-hosted analytics

Even with an exporter enabled, the policy is that only **derived routing features, candidate
identifiers, decisions, usage, and outcomes approved by policy** may leave the machine. An exporter
reads from the local store, so it can only ever emit what the store holds. That is why the storage
prohibitions above are the load-bearing ones: **the export guarantee is exactly as strong as they
are, and no stronger.** Both the exporters and the store arrive in Phase 8, and
[`docs/threat-model.md`](threat-model.md) assigns this row to `telemetry/exporters.py`.

**Availability:** exporters arrive in **Phase 8**. Until then no export mechanism exists in any form.

---

## Optional code outcomes

There is a second, narrower opt-in, separate from everything above. For a repository you explicitly
mark as trusted, the router can record whether its routing actually produced good work:

- Whether a generated diff survived later turns.
- Whether edits were reverted.
- Whether tests passed after a change.
- Whether a commit was created, and whether generated code survived to that commit.
- Cost per successful commit.

This is **off by default** and enabled **per repository** — turning it on for one project does not
turn it on anywhere else. These stay **local metrics**. Enabling code outcomes does not enable
export; if you want them to leave the machine you must separately turn on external export, which is
its own opt-in decision.

Note what is recorded even here: whether a diff survived, not the diff. Whether tests passed, not
their output.

**Availability:** **Phase 8**.

---

## Your controls

| Control | What it does | Available from |
|---|---|---|
| Disable telemetry entirely | Stops the local store from being written at all. Routing still works; the router simply keeps no history, and features that depend on history — statistics, evaluation, outcome learning — go quiet. | Phase 8 |
| Change the retention window | Sets how many days of history are kept before pruning. Defaults to 30. | Phase 8 |
| Delete the local store | Erases stored routing history, either through the deletion command or by removing the SQLite file yourself. | Phase 8 |
| Enable or disable external export | Turns an exporter on or off. **Off by default.** Choosing a target is a deliberate act, and the router describes what will leave the machine before you confirm. | Phase 8 |
| Enable per-repository code outcomes | Lets the router record whether generated code survived, reverted, or passed tests, for one repository you trust. Off by default; stays local. | Phase 8 |
| Contribute to the opt-in training corpus | Collects data specifically for training the learned capability predictor. This is a **separate consent** from telemetry — the default store is deliberately too thin to train on, so the learning track has its own opt-in and its own collection path. Off by default. | Phase 9 |

Every control above defaults to the most private setting. If you install the router and never open
the configuration file, you already have the strongest posture available — there is no "remember to
turn telemetry off" step to forget.

---

## Questions this page should have answered

- *Can the router read my source code?* It processes it in memory to decide where to route, and sends
  it to the provider it selects — the same provider Hermes would have used. It does not keep a copy.
- *Does installing this send anything to the project maintainers?* No. There is no default egress of
  any kind, and no exporter is enabled out of the box.
- *If I route only to local models, does anything leave my machine?* No.
- *Can I verify all this?* Partly today, fully from Phase 8. What you can check right now is the
  contract: both schemas are in this repository, and you can read exactly which fields exist and
  what each one accepts. What you cannot check yet is the code that fills them, because it does not
  exist — the store, and the write path that has to honor the prohibitions above, arrive in Phase 8.
  From then on the store is a plain SQLite file you can open and inspect row by row.

For the decision record behind this posture, see
[`docs/adr/0004-local-telemetry-and-privacy.md`](adr/0004-local-telemetry-and-privacy.md). For the
security invariants, see [`SECURITY.md`](../SECURITY.md).
