# SSE fixture corpus

Twelve byte-exact upstream responses. Together with `tests/integration/mock_upstream.py`
they are the oracle for R5 — "streaming SSE and tool-call behavior identical to
talking to the target provider directly" — which plans 02-04, 02-08, and 02-09
all measure against.

## The one rule

**A `.txt` file in this directory contains response payload bytes and nothing
else.** No header block, no comment, no explanatory preamble, no trailing
newline that the upstream would not have sent. Everything a human needs to know
lives in this README; everything a program needs lives in the `FIXTURES` table
in `mock_upstream.py`.

Anything added inside a `.txt` becomes part of the byte sequence the differential
harness compares, so the corpus would then be measuring the commentary.

## Why raw bytes and not Python objects

Frame boundaries *are* the thing under test.

An SSE relay does not break on well-formed frames. It breaks on a frame split
across a TCP read, on a multi-byte UTF-8 codepoint delivered in two pieces, on a
stream that stops without its terminator. Build the corpus as Python objects and
a serializer chooses the boundaries at test time — which means the boundaries are
whatever the serializer felt like, they are not reviewable in a diff, and they
change silently when the serializer does.

As bytes on disk, the boundaries are fixed, reviewable, and version-controlled.

### `.gitattributes` is load-bearing

This repository is configured with `core.autocrlf=true`. Without the `-text`
attribute in this directory's `.gitattributes`, git rewrites every `\n` frame
terminator to `\r\n` on checkout — so the bytes CI reads differ from the bytes
that were committed, while the authoring working tree looks fine.

That would silently destroy the corpus's only reason to exist. Do not remove the
`.gitattributes`. `test_git_does_not_translate_fixture_bytes` asserts it is
still in effect.

## Format

Streaming fixtures are sequences of SSE frames:

```
data: {"id":"chatcmpl-test0001","object":"chat.completion.chunk",...}<terminator>
```

* `data: ` prefix, then one line of compact JSON (no spaces after separators,
  matching what OpenAI-compatible upstreams put on the wire).
* A frame terminator: a blank line. **Both spellings appear in this corpus** —
  `\n\n` in eleven fixtures and `\r\n\r\n` in `empty_deltas_and_keepalives.txt`.
  The SSE grammar permits CR, LF, or CRLF, and a relay that hardcodes one
  separator only breaks on the other. That is worth catching here rather than in
  production.
* A line beginning with `: ` is an SSE comment (a keepalive), not an event.
* Streams end with `data: [DONE]` plus a terminator — except
  `midstream_disconnect.txt`, which is truncated on purpose.

Error fixtures are plain JSON bodies, **not** SSE, with **no trailing newline**
(what a real upstream sends; `Content-Length` is derived from these bytes).

All content is synthetic: model `test-model-a`, ids `chatcmpl-test0001`…`0012`,
tool-call ids `call_test000N`, paths under `/tmp/synthetic*`. No real credential,
no real provider model id, no real prompt, no path containing a username.

## HTTP envelope per fixture

The envelope is not in the `.txt` files (see "The one rule"). `mock_upstream.py`
replays each fixture inside the envelope below; the two must stay in step.

| Fixture | Status | Framing | Notable headers |
|---|---|---|---|
| `text_stream` | 200 | chunked | `Content-Type: text/event-stream; charset=utf-8` |
| `single_tool_call` | 200 | chunked | as above |
| `parallel_tool_calls` | 200 | chunked | as above |
| `fragmented_arguments` | 200 | chunked | as above |
| `fragmented_utf8` | 200 | chunked | as above |
| `empty_deltas_and_keepalives` | 200 | chunked | as above |
| `usage_only_final_chunk` | 200 | chunked | as above |
| `refusal` | 200 | chunked | as above |
| `error_401` | 401 | `Content-Length` | `Content-Type: application/json` |
| `error_429` | 429 | `Content-Length` | `Content-Type: application/json`, **`Retry-After: 20`** |
| `error_context_length` | 400 | `Content-Length` | `Content-Type: application/json` |
| `midstream_disconnect` | 200 | chunked, **no terminating chunk** | as streaming |

Every streaming response also carries `Cache-Control: no-cache`,
`Connection: keep-alive`, and `X-Accel-Buffering: no`.

No response carries a `Date` or `Server` header. A clock-derived header would
make responses byte-unstable, and the whole harness is built on byte comparison.

`error_429`'s `Retry-After` is deliberate: a gateway that drops it converts a
recoverable throttle into an opaque failure for the caller.

## What each fixture covers

| Fixture | `design.md` §20.3 case | What it catches |
|---|---|---|
| `text_stream` | Streaming text; provider reasoning fields | Baseline relay. Carries a `reasoning_content` delta — an unmodelled field that must survive passthrough verbatim. |
| `single_tool_call` | Single tool calls | Arguments arriving whole. |
| `parallel_tool_calls` | Parallel tool calls | Two calls with the **same function name**, interleaved, so `tool_calls[].index` is the only possible assembly key. A relay keying on id or name reassembles wrong. |
| `fragmented_arguments` | Fragmented tool-call arguments | Six fragments, none valid JSON alone, valid only concatenated in arrival order. Includes the key `"arguments"` cut as `"argu` / `ments"`. The phase's highest-risk failure mode. |
| `fragmented_utf8` | Fragmented tool-call arguments (UTF-8) | Two hazards at once — see below. |
| `empty_deltas_and_keepalives` | Empty deltas | `"delta":{}` frames, `: ping` comment keepalives, a `"choices":[]` frame, and CRLF terminators. |
| `usage_only_final_chunk` | Usage in final stream chunks | Final chunk with `choices: []` and populated `usage`. Catches a relay that drops the usage chunk. |
| `refusal` | Refusals | Non-null `refusal` with `finish_reason: content_filter`. |
| `error_401` | (auth failure) | Non-streaming error envelope. |
| `error_429` | (throttle) | `Retry-After` propagation. |
| `error_context_length` | Context errors | `code: context_length_exceeded` at HTTP 400. |
| `midstream_disconnect` | Malformed SSE; client disconnect | Two valid frames, then a frame truncated mid-token, no `[DONE]`, connection closed without the terminating chunk. Client must see a transport error, not a clean short body. `design.md` §8.2: the route is committed once a frame is forwarded, so this must fail rather than splice. |

### `fragmented_utf8.txt` in detail

Two independent hazards, deliberately in one fixture:

1. **A codepoint split across an SSE frame boundary.** U+1F600 arrives as a lone
   high surrogate (`\ud83d`) in one frame and a lone low surrogate (`\ude00`) in
   the next. This is what real OpenAI-compatible upstreams emit. Neither half is
   encodable to UTF-8 alone, so a relay that encodes each delta independently
   raises `UnicodeEncodeError`. Each frame is still valid JSON, so every frame
   here still validates against `sse-stream-contract.v1`.

2. **Raw multi-byte UTF-8 in the payload** — 2-byte (`café crème`), 3-byte
   (`日本`), and 4-byte (`𝄞`, `F0 9D 84 9E`) sequences. These exist so a *byte*
   offset inside a codepoint exists for `MockUpstream.set_rechunk()` to cut at.
   That split is what distinguishes a byte-transparent relay (`aiter_raw`) from
   a line-oriented one (`aiter_lines`).

Only (1) can live in the file's frame structure. A raw byte split across an SSE
frame boundary would leave individual frames that are not valid UTF-8 and
therefore not parseable or schema-validatable at all — so (2) is expressed as the
*material* for a mid-codepoint split, and `set_rechunk` places the actual cut.
`test_rechunk_split_inside_a_utf8_codepoint_preserves_bytes` computes the offset
from the file and proves the boundary is mid-codepoint before asserting on it.

## Encoding notes for anyone writing a verification command

* `fragmented_arguments.txt` and `empty_deltas_and_keepalives.txt` are
  **ASCII-only** on purpose, because `pathlib.Path.read_text()` with no explicit
  encoding decodes with the locale codec — `cp1252` on Windows — and would
  mangle or raise on anything else.
* For every other fixture use `read_bytes()`, or `read_text(encoding="utf-8")`.
* `read_text()` also applies universal-newline translation, which silently
  rewrites `\r\n` to `\n`. Any assertion about frame terminators must use
  `read_bytes()`.

## §20.3 cases not represented here

* **Non-streaming text**, **images**, **structured outputs** — request-shaped or
  whole-response cases, covered by `openai-chat-request.v1` /
  `openai-chat-response.v1` fixtures, not by an SSE frame sequence.
* **Cancellation**, **client disconnect (client side)** — behaviors of the
  *client* half of the connection. No upstream byte sequence expresses them;
  they are driven by the test closing its own connection, for which
  `midstream_disconnect` provides the upstream half.
