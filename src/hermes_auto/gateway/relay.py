"""The response path. It does not understand SSE, and that is the design.

R5 says streaming and tool-call behavior must be identical to talking to the
target provider directly. There are two ways to get there. One is to parse the
upstream event stream, model it, and re-emit it carefully -- and then defend that
care against every future edit. The other is to never look at the bytes. This
module is the second one: **code that does not parse cannot mis-fragment.**

That makes R5 structural rather than test-enforced. The tests in
``tests/contract/test_passthrough_identity.py`` confirm the property; they are
not what creates it.

**What must never appear in this file.** Anything that reassembles frames: a
line-oriented iterator, a UTF-8 decoder, a buffer that waits for a frame
terminator, a re-serializer. Each of them silently normalizes exactly the cases
the fixture corpus was built to catch:

* ``fragmented_utf8`` carries raw multi-byte sequences, and plan 02-03's
  ``set_rechunk`` cuts them mid-codepoint. Anything that turns bytes into text
  here either raises or substitutes a replacement character. Neither is what the
  upstream sent.
* ``empty_deltas_and_keepalives`` terminates its frames with CRLF while the other
  eleven fixtures use LF. A relay that splits on one terminator emits different
  bytes for the other.
* ``usage_only_final_chunk`` ends with ``choices: []``. A relay that models
  "chunks have choices" drops it, and the caller silently loses token accounting.

The upstream iterator this function consumes comes from ``httpx``'s
``aiter_raw`` (see :mod:`hermes_auto.gateway.upstream`), which yields undecoded
transport bytes. Any future change that parses in this module must justify itself
against R5 in writing, not in a commit message.

**Cancellation.** When the client hangs up mid-stream, the ASGI server delivers
``http.disconnect``, Starlette cancels the task iterating this generator, and the
``CancelledError`` propagates into the upstream iterator's ``finally``, which
closes the upstream response. Nothing here catches it, and nothing here should:
swallowing the cancellation is what leaves an abandoned generation billing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

__all__ = ["relay_stream"]


async def relay_stream(upstream_iter: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Yield every chunk from *upstream_iter* unchanged.

    No buffering, no re-framing, no text conversion, no inspection. The only
    thing this function adds to the byte stream is nothing.

    Args:
        upstream_iter: Raw transport bytes, as produced by
            :meth:`hermes_auto.gateway.upstream.UpstreamClient.stream`.

    Yields:
        Exactly the chunks received, in order, with the same contents. Chunk
        *boundaries* may be re-drawn by the transport below this function --
        that is a property of TCP and of the ASGI server, not of this code --
        which is why the contract suite asserts on the concatenated bytes rather
        than on the chunk sequence.
    """
    async for chunk in upstream_iter:
        # ---------------------------------------------------------------
        # COMMIT BARRIER EXTENSION POINT (design.md 8.2) -- Phase 6 attaches here.
        #
        # 8.2 says the route is committed once the first valid SSE event has
        # been sent to Hermes, and that before that point the gateway may
        # transparently fail over. The observation "a first chunk is about to
        # reach the client" is available at exactly this line and nowhere else.
        #
        # Deliberately a marked place and not an abstraction: a hook, a
        # callback parameter, or a Protocol added now would be shaped by
        # guesswork about a failover path that does not exist yet, and the
        # empty version of it would still cost this function a branch per
        # chunk. Phase 6 has the failover policy in hand and can shape it then.
        #
        # Whatever attaches here must not consume, buffer, or inspect `chunk`
        # beyond "it is non-empty" -- see this module's docstring.
        # ---------------------------------------------------------------
        yield chunk
