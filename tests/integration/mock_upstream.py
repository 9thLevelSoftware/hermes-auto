"""A byte-scripted OpenAI-compatible upstream for the differential test harness.

This module is the *oracle* every later plan in Phase 2 measures against, so it
is production code that happens to live under ``tests/``, not scaffolding.

**Why stdlib ``http.server`` and not a framework.** The corpus in
``tests/fixtures/sse/`` deliberately contains byte sequences that no HTTP client
or server library would let you put on the wire: a chunked response that stops
without its terminating chunk, a JSON string carrying a lone UTF-16 surrogate, a
frame truncated mid-token. A framework normalizes exactly those away -- which is
to say it normalizes away the only cases where an SSE relay actually breaks.
Hand-writing the chunked framing is the point, not an accident.

**What it does not do.** It never validates what it receives. A gateway bug that
sends a malformed request must surface in the gateway's own tests, not as a 400
from the oracle; an oracle that gatekeeps can mask the divergence it exists to
measure. It records requests and replays bytes.

Three primitives exist for consumers rather than for this module's own tests:

``set_rechunk(sizes)``
    Re-splits the identical response bytes at arbitrary offsets. This is the
    primitive plan 02-08's ``hypothesis`` fuzzer drives. SSE relays break on
    frames split across TCP reads, not on well-formed frames, and a split can be
    placed inside a multi-byte UTF-8 codepoint -- which is what distinguishes a
    byte-transparent relay (``aiter_raw``) from a line-oriented one
    (``aiter_lines``).

``set_first_byte_delay(seconds)``
    Delays the response. Against a localhost upstream that answers in
    microseconds, gateway overhead is the whole measurement and a TTFT budget is
    meaningless; a realistic first-byte delay puts the overhead in proportion.

``requests``
    Every received request, body parsed when it is JSON, so a test can assert
    that ``_hermes_auto`` was stripped before forwarding.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = [
    "FIXTURES",
    "FIXTURE_DIR",
    "FixtureSpec",
    "MockUpstream",
    "MockUpstreamError",
    "UnknownFixtureError",
]

#: Directory holding the byte-exact fixture corpus. Documented in its README.
FIXTURE_DIR: pathlib.Path = (
    pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "sse"
)

#: SSE frame terminators, longest first. The corpus contains both: the SSE
#: grammar permits CR, LF, or CRLF, and a relay that hardcodes one only breaks
#: on the other.
_FRAME_TERMINATORS = (b"\r\n\r\n", b"\n\n")

#: How often the serving loop checks for a shutdown request. Bounds close()'s
#: latency; the idle cost of a 20 Hz poll is immaterial next to a half-second
#: teardown paid once per instance.
_POLL_INTERVAL_SECONDS = 0.02


class MockUpstreamError(Exception):
    """Base class for this module's errors, so callers catch one type."""


class UnknownFixtureError(MockUpstreamError):
    """``script()`` was given a name with no fixture behind it.

    Raised eagerly from ``script()`` rather than lazily inside a server thread,
    where the traceback would surface as an opaque 500 in an unrelated test.
    """


@dataclass(frozen=True)
class FixtureSpec:
    """The HTTP envelope a fixture's payload bytes are delivered inside.

    The envelope lives here rather than in the ``.txt`` file because those files
    are byte-exact payloads: a header block or comment inside one would become
    part of the bytes under test. ``tests/fixtures/sse/README.md`` documents the
    same table for a human reader.
    """

    #: HTTP status line code.
    status: int
    #: Reason phrase. Fixed rather than derived, so the status line is byte-stable.
    reason: str
    #: Response headers, in emission order. No ``Date`` or ``Server`` header is
    #: ever sent: a clock-derived header would make responses non-reproducible.
    headers: tuple[tuple[str, str], ...]
    #: ``True`` to frame with ``Transfer-Encoding: chunked`` (what a real SSE
    #: upstream does), ``False`` to send a ``Content-Length``.
    chunked: bool
    #: ``True`` to close the connection without writing the terminating chunk,
    #: so the client observes a transport error rather than a clean short body.
    truncate: bool = False


_SSE_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Type", "text/event-stream; charset=utf-8"),
    ("Cache-Control", "no-cache"),
    ("Connection", "keep-alive"),
    ("X-Accel-Buffering", "no"),
)

_JSON_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Type", "application/json"),
)


def _sse(name: str, *, truncate: bool = False) -> tuple[str, FixtureSpec]:
    return name, FixtureSpec(
        status=200, reason="OK", headers=_SSE_HEADERS, chunked=True, truncate=truncate
    )


#: Every fixture, with the envelope it is replayed inside. Insertion order is
#: the corpus order documented in the README; iteration is deterministic.
FIXTURES: Mapping[str, FixtureSpec] = dict(
    (
        _sse("text_stream"),
        _sse("single_tool_call"),
        _sse("parallel_tool_calls"),
        _sse("fragmented_arguments"),
        _sse("fragmented_utf8"),
        _sse("empty_deltas_and_keepalives"),
        _sse("usage_only_final_chunk"),
        _sse("refusal"),
        (
            "error_401",
            FixtureSpec(
                status=401,
                reason="Unauthorized",
                headers=_JSON_HEADERS,
                chunked=False,
            ),
        ),
        (
            "error_429",
            FixtureSpec(
                status=429,
                reason="Too Many Requests",
                # `retry-after` is load-bearing: a gateway that drops it turns a
                # recoverable throttle into an opaque failure for the caller.
                headers=_JSON_HEADERS + (("Retry-After", "20"),),
                chunked=False,
            ),
        ),
        (
            "error_context_length",
            FixtureSpec(
                status=400,
                reason="Bad Request",
                headers=_JSON_HEADERS,
                chunked=False,
            ),
        ),
        _sse("midstream_disconnect", truncate=True),
    )
)


def fixture_path(name: str) -> pathlib.Path:
    """Return the on-disk path of fixture *name*.

    Raises:
        UnknownFixtureError: *name* is not in :data:`FIXTURES`.
    """
    if name not in FIXTURES:
        raise UnknownFixtureError(
            f"unknown fixture {name!r}; available: {sorted(FIXTURES)}"
        )
    return FIXTURE_DIR / f"{name}.txt"


def fixture_bytes(name: str) -> bytes:
    """Return fixture *name*'s payload bytes, read fresh from disk.

    Deliberately uncached. A cached corpus silently serves stale bytes to a test
    that rewrote a fixture, and these files exist precisely so their bytes can be
    compared against what came off the wire.

    Raises:
        UnknownFixtureError: *name* is not in :data:`FIXTURES`, or its file is
            missing from the corpus directory.
    """
    path = fixture_path(name)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise UnknownFixtureError(f"fixture {name!r} unreadable at {path}: {exc}") from exc


def split_frames(body: bytes) -> list[bytes]:
    """Split *body* into SSE frames, each keeping its own terminator.

    This is the mock's default wire chunking -- one HTTP chunk per SSE frame,
    which is what a real upstream emits. A trailing run of bytes with no
    terminator (``midstream_disconnect``'s truncated frame) is returned as a
    final piece rather than discarded; dropping it would quietly turn a
    truncation fixture into a well-formed one.
    """
    pieces: list[bytes] = []
    start = 0
    while start < len(body):
        cut = -1
        width = 0
        for terminator in _FRAME_TERMINATORS:
            found = body.find(terminator, start)
            if found != -1 and (cut == -1 or found < cut):
                cut, width = found, len(terminator)
        if cut == -1:
            pieces.append(body[start:])
            break
        pieces.append(body[start : cut + width])
        start = cut + width
    return pieces


def _apply_rechunk(body: bytes, sizes: Sequence[int] | None) -> list[bytes]:
    """Re-split *body* into pieces of the given *sizes*.

    ``None`` means default SSE-frame chunking. Any bytes left once *sizes* is
    exhausted become one final piece, so a caller does not have to know the
    body's exact length to drive a fuzzer at it.
    """
    if sizes is None:
        return split_frames(body)

    pieces: list[bytes] = []
    offset = 0
    for size in sizes:
        if offset >= len(body):
            break
        pieces.append(body[offset : offset + size])
        offset += size
    if offset < len(body):
        pieces.append(body[offset:])
    return pieces


class _Handler(BaseHTTPRequestHandler):
    """Records the request, then replays the scripted bytes.

    ``HTTP/1.1`` is required: ``Transfer-Encoding: chunked`` is not a legal
    framing under ``HTTP/1.0``, and chunked framing is what lets the truncation
    fixture end a response mid-body in a way a client detects as an error.
    """

    protocol_version = "HTTP/1.1"
    # Suppresses the reason phrase BaseHTTPRequestHandler would otherwise derive;
    # every response here sets its own from the FixtureSpec.
    server_version = "MockUpstream"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence per-request logging to stderr; a test run emits hundreds."""

    @property
    def _mock(self) -> MockUpstream:
        return self.server._mock  # type: ignore[attr-defined]

    def _read_body(self) -> bytes:
        """Drain the request body.

        Always drained, even when the response ignores it: leaving bytes unread
        on a keep-alive connection desynchronizes the next request on it.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        return self.rfile.read(length) if length > 0 else b""

    # -- request handling -------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler dispatch name
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler dispatch name
        self._handle("POST")

    def _handle(self, method: str) -> None:
        body = self._read_body()
        try:
            parsed: Any = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = None

        self._mock._record(
            {
                "method": method,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
                "json": parsed,
            }
        )

        name, delay, sizes = self._mock._current_script()
        if name is None:
            self._send_unscripted()
            return

        spec = FIXTURES[name]
        payload = fixture_bytes(name)
        pieces = _apply_rechunk(payload, sizes)

        if delay > 0:
            # Before the status line, so the delay covers the true first byte.
            time.sleep(delay)

        self._send_headers(spec, payload)
        if spec.chunked:
            self._write_chunked(pieces, truncate=spec.truncate)
        else:
            self._write_plain(pieces)

    def _send_headers(self, spec: FixtureSpec, payload: bytes) -> None:
        # send_response_only rather than send_response: the latter appends
        # `Server` and `Date`, and a clock-derived header would make every
        # response byte-unstable for a harness built on byte comparison.
        self.send_response_only(spec.status, spec.reason)
        for key, value in spec.headers:
            self.send_header(key, value)
        if spec.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.flush()

    def _write_chunked(self, pieces: Sequence[bytes], *, truncate: bool) -> None:
        for piece in pieces:
            if not piece:
                # A zero-length chunk IS the terminator in HTTP/1.1 chunked
                # framing; emitting one mid-body would silently end the response.
                continue
            self.wfile.write(b"%x\r\n" % len(piece))
            self.wfile.write(piece)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        if truncate:
            # No terminating chunk. The client hits EOF inside the body and
            # raises rather than returning a clean short read.
            self.close_connection = True
            return

        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _write_plain(self, pieces: Sequence[bytes]) -> None:
        for piece in pieces:
            if piece:
                self.wfile.write(piece)
                self.wfile.flush()

    def _send_unscripted(self) -> None:
        """Answer a request that arrived before ``script()`` was called.

        A 500 with a named error beats hanging or replaying whatever ran last:
        the failure points at the test that forgot to script, not at the code
        under test.
        """
        body = json.dumps(
            {"error": {"message": "no fixture scripted", "type": "mock_upstream_error"}}
        ).encode("utf-8")
        self.send_response_only(500, "Internal Server Error")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


@dataclass
class _State:
    """Mutable script state, guarded by :attr:`MockUpstream._lock`."""

    fixture: str | None = None
    first_byte_delay: float = 0.0
    rechunk: tuple[int, ...] | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)


class MockUpstream:
    """A scripted OpenAI-compatible upstream bound to an ephemeral loopback port.

    Binds ``127.0.0.1`` on port ``0`` and reports the assigned port, so tests
    running in parallel never collide on a fixed port.

    Use as a context manager::

        with MockUpstream() as up:
            up.script("text_stream")
            ...  # POST to up.url
            assert up.requests[0]["json"]["model"] == "auto:balanced"
    """

    def __init__(self) -> None:
        self._state = _State()
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        # Without daemon threads a client that abandons a stream mid-flight can
        # keep a handler thread alive past the test and hang interpreter exit.
        self._server.daemon_threads = True
        self._server._mock = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            # serve_forever's default 0.5 s poll interval is also the worst-case
            # latency of shutdown(), which makes every close() cost half a second.
            # Plan 02-08's fuzzer stands an instance up per case, so that default
            # would dominate its runtime while measuring nothing.
            kwargs={"poll_interval": _POLL_INTERVAL_SECONDS},
            name=f"mock-upstream-{self.port}",
            daemon=True,
        )
        self._thread.start()

    # -- addressing -------------------------------------------------------

    @property
    def port(self) -> int:
        """The OS-assigned loopback port."""
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """Base URL, no trailing slash."""
        return f"http://127.0.0.1:{self.port}"

    # -- scripting --------------------------------------------------------

    def script(self, fixture_name: str) -> None:
        """Replay *fixture_name* for subsequent requests, with its own envelope.

        Sticky rather than one-shot: the chunk-boundary fuzzer issues many
        requests against one fixture, and re-scripting per request would make
        every such test a loop around a mutation that never varies.

        Raises:
            UnknownFixtureError: no fixture is registered under that name.
        """
        fixture_path(fixture_name)  # eager existence check
        with self._lock:
            self._state.fixture = fixture_name

    def set_first_byte_delay(self, seconds: float) -> None:
        """Sleep *seconds* before emitting the response's first byte.

        Raises:
            ValueError: *seconds* is negative.
        """
        if seconds < 0:
            raise ValueError(f"first-byte delay must be >= 0, got {seconds!r}")
        with self._lock:
            self._state.first_byte_delay = float(seconds)

    def set_rechunk(self, sizes: Sequence[int] | None) -> None:
        """Re-split the response body into pieces of *sizes* bytes.

        ``None`` restores the default of one wire chunk per SSE frame. Re-chunking
        must change framing and never content, which is the invariant plan
        02-08's fuzzer asserts.

        Raises:
            ValueError: any size is not a positive integer. A zero-length chunk
                is the HTTP terminator, so permitting one would truncate the
                body instead of re-framing it -- the fuzzer would then be
                measuring a bug in this mock.
        """
        if sizes is None:
            with self._lock:
                self._state.rechunk = None
            return
        chunk_sizes = tuple(int(size) for size in sizes)
        if any(size <= 0 for size in chunk_sizes):
            raise ValueError(f"rechunk sizes must all be > 0, got {chunk_sizes!r}")
        with self._lock:
            self._state.rechunk = chunk_sizes

    # -- observation ------------------------------------------------------

    @property
    def requests(self) -> list[dict[str, Any]]:
        """Requests received so far, oldest first.

        Each entry is ``{method, path, headers, body, json}``: ``body`` is the
        raw bytes and ``json`` the parsed value, or ``None`` when the body was
        not JSON. Both are kept because a test asserting ``_hermes_auto`` was
        stripped wants the parsed form, while one asserting byte-level
        passthrough wants the bytes.

        Returns a snapshot copy; the list is appended from handler threads.
        """
        with self._lock:
            return list(self._state.requests)

    def clear_requests(self) -> None:
        """Drop the recorded request log."""
        with self._lock:
            self._state.requests.clear()

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Stop serving and release the port. Idempotent."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> MockUpstream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals used by the handler ------------------------------------

    def _record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._state.requests.append(entry)

    def _current_script(self) -> tuple[str | None, float, tuple[int, ...] | None]:
        with self._lock:
            return (
                self._state.fixture,
                self._state.first_byte_delay,
                self._state.rechunk,
            )


def iter_fixture_names() -> Iterator[str]:
    """Yield every fixture name in sorted order.

    Sorted rather than declaration order so a parametrized test's ids -- and any
    failure report built from them -- are stable across runs.
    """
    yield from sorted(FIXTURES)
