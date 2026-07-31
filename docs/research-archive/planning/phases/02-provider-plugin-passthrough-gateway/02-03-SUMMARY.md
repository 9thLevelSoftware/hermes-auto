# Plan 02-03 Summary — Mock Upstream & Byte-Exact SSE Fixture Corpus

**Status**: Complete with Warnings
**Wave**: 1 · **Agent**: testing-api-tester (+ senior-developer concerns)
**Requirements**: R5
**Verification**: 19 run / 18 passed, plus 5 stronger replacements

## Files created (16, plus 1 unplanned — see Decisions)

`tests/integration/mock_upstream.py`, `tests/integration/test_mock_upstream.py` (80 tests),
`tests/fixtures/sse/README.md`, `tests/fixtures/sse/.gitattributes`, and 12 `.txt` fixtures:
`text_stream`, `single_tool_call`, `parallel_tool_calls`, `fragmented_arguments`, `fragmented_utf8`,
`empty_deltas_and_keepalives`, `usage_only_final_chunk`, `refusal`, `error_401`, `error_429`,
`error_context_length`, `midstream_disconnect`.

## Contract frozen here (driven by 02-04 and 02-08)

`MockUpstream` — context manager; `.url`, `.port`; `script(fixture_name: str)`;
`set_first_byte_delay(seconds)`; `set_rechunk(sizes | None)`;
`.requests -> list[{method, path, headers, body: bytes, json: Any|None}]` (snapshot copy);
`clear_requests()`; `close()`. Module also exports `FIXTURES`, `FIXTURE_DIR`, `FixtureSpec`,
`fixture_bytes`, `fixture_path`, `split_frames`, `iter_fixture_names`, `UnknownFixtureError`.
Import as `from tests.integration.mock_upstream import …` under pytest.

Measured: `set_first_byte_delay(0.3)` → 0.3027 s observed (+2.7 ms).

## The unplanned 17th file prevented silent CI corruption

The repo has `core.autocrlf=true` and no `.gitattributes`. The agent proved empirically that git
rewrites `\n` → `\r\n` on checkout of a `.txt` probe. **Unmitigated, every frame terminator in the
corpus differs between commit and checkout** — invisible in the authoring tree, breaking 02-04,
02-08, and 02-09 in CI. A `*.txt -text -diff` scoped to this one directory fixes it
(`git check-attr` → `text: unset`; `src/` and `tests/fixtures/wire/` confirmed unaffected).

Proved by deleting all 12 fixtures and restoring from git: byte-identical, 80 tests still pass.
Pinned by `test_git_does_not_translate_fixture_bytes`.

## Decisions a reviewer should know

1. **`fragmented_utf8.txt` uses a surrogate-pair split for the across-frame case, not a raw-byte
   split.** These were in tension in the plan: Task 1 wanted a codepoint split across a frame
   boundary, Task 3 wanted every frame to validate against the schema. A raw UTF-8 split leaves
   frames that are not valid UTF-8 and cannot be parsed at all. So U+1F600 ships as lone
   `\ud83d`/`\ude00` halves in adjacent frames — what real OpenAI upstreams actually emit, each frame
   valid JSON, neither half encodable alone. The byte-level hazard is covered separately: raw 2-, 3-,
   and 4-byte sequences sit in the payload and `set_rechunk` cuts at offsets 1097–1099, each *proved*
   mid-codepoint by asserting the prefix raises `UnicodeDecodeError` before the byte-identity
   assertion. Offsets are computed from the file, so they cannot silently drift onto a boundary.
2. **`empty_deltas_and_keepalives.txt` uses CRLF terminators; the other eleven use LF.** SSE permits
   both; a relay hardcoding one breaks only on the other.
3. **`text_stream.txt` carries a `reasoning_content` delta** — covers §20.3 "provider reasoning
   fields" without a 13th file, doubling as an unmodelled-field passthrough probe.
4. **`script()` is sticky, not one-shot** — 02-08's fuzzer issues many requests per fixture.
5. **`serve_forever(poll_interval=0.02)`.** The 0.5 s default made `close()` cost half a second; the
   suite went 22 s → 2.0 s. 02-08 stands up an instance per fuzz case.
6. **Responses send no `Date`/`Server` header** (`send_response_only`) — a clock-derived header is
   byte-unstable and the harness compares bytes.

## Vacuous verifications found and strengthened

- **`V10` could not fail.** `print([...] or 'interface complete')` exits 0 whether or not methods are
  missing — demonstrated by running it against a bare `class Fake: pass`, which printed the missing
  list and still exited 0. Replaced with an asserting version.
- `test $(ls …*.txt|wc -l) -eq 12` passes on 12 empty files → set equality disk↔registry plus a
  non-triviality check.
- `p.count('data: ')>=4` on `fragmented_arguments` passes on 4 identical frames → now asserts 6
  fragments, none parsing alone, concatenation valid, the `"argu`/`ments"` split present.
- `b.count(b'data: ')>=2` on `fragmented_utf8` passes on a file with no UTF-8 at all → now asserts the
  raw sequences and the proven mid-codepoint offsets.
- **In the empty-delta check, `'"delta": {}' in t.replace(' ','')` is an unsatisfiable disjunct** —
  the needle contains a space, the haystack has none. Only the second disjunct ever did work. It also
  never checked `: ping` despite the fixture's name.
- `grep -q 'http.server'` is satisfied by the mandatory import line alone. Real proof is the AST
  stdlib scan plus 80 replay tests.
- The credential grep covered 3 patterns → widened to real provider model ids (`gpt-N`, `claude-N`,
  `gemini-N`, `llama-N`, `mistral-`, `command-r`) and developer paths (`C:\Users`, `/home/`, `/Users/`).

## Issues raised (real, out of scope here)

- The cross-plan guard in this plan's frontmatter **cannot pass during a parallel wave** — it asserts
  a whole-tree property from inside one plan, the defect class `02-CONTEXT.md` § Phase-Close Gate
  names. It belongs at phase close. Same idiom confirmed in 02-01 and 02-02.
- `httpx` imports in `.venv` but was not declared in `pyproject.toml` at the time of writing (02-02
  declared it in the same wave). This plan's code uses stdlib `http.client` only.
- Cases **not** expressible as a byte fixture, with reasons, are tabulated in the corpus README:
  non-streaming text, images, and structured outputs belong to the `openai-chat-request/response.v1`
  fixtures; cancellation and client disconnect are behaviors of the *client* half, where the test
  closes its own connection and `midstream_disconnect` supplies the upstream half.
