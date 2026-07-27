"""The gateway's own error envelope, in the shape OpenAI clients already parse.

Every error this gateway *originates* -- 401, 413, 415, 400, 502 -- comes out of
this module in the ``openai-error.v1`` shape frozen by plan 02-02. Errors the
*upstream* originates are not built here at all: those are relayed with their
status, headers, and bytes untouched, because a gateway that reinterprets a
provider's 429 has already broken R5. The distinction is the whole reason this
module is small.

**The validator is hoisted, and it does not run in production.** Building a
``Draft202012Validator`` costs a ``check_schema`` pass of roughly 50 ms
(``gateway/schemas.py``), so it is built at most once per process, lazily, and
only when something asks for it. Nothing on the request path asks: an error
response is emitted from a fixed dict literal whose shape cannot drift at
runtime, and paying schema validation on the error branch would put the most
expensive code in the least tested place. :func:`assert_error_body` exists for
tests and for the contract suite, which is where a shape regression must be
caught.

**Nothing here logs.** ``message`` is caller-supplied and a caller could put a
detail from an upstream error into it, so this module never routes anything to a
logger; the app decides what, if anything, is worth recording, and does that
through the redaction sink.
"""

from __future__ import annotations

import threading
from typing import Any

from starlette.responses import JSONResponse

__all__ = [
    "ERROR_SCHEMA_ID",
    "GatewayError",
    "assert_error_body",
    "error_body",
    "error_response",
    "gateway_error_handler",
    "http_exception_handler",
    "unhandled_exception_handler",
]

#: ``$id`` of the frozen error envelope (plan 02-02).
ERROR_SCHEMA_ID = "https://hermes-auto-router.dev/schema/wire/openai-error.v1.json"

#: Built at most once, on first use, never on the request path. Guarded because
#: two concurrent requests reaching a test helper simultaneously would otherwise
#: each pay the ``check_schema`` cost.
_validator: Any = None
_validator_lock = threading.Lock()


class GatewayError(Exception):
    """An error the gateway itself originates, carrying its wire representation.

    Raised from ingress and from the upstream client, and converted to a response
    by :func:`gateway_error_handler`. It exists so that a failure deep in the
    request path cannot reach the client as a Starlette default HTML 500 that an
    OpenAI SDK will fail to parse -- the SDK looks for ``error.message``, and a
    body without one surfaces to the user as an unhelpful transport error.
    """

    def __init__(
        self,
        status: int,
        message: str,
        type_: str,
        *,
        param: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type_ = type_
        self.param = param
        self.code = code

    def to_response(self) -> JSONResponse:
        return error_response(
            self.status, self.message, self.type_, param=self.param, code=self.code
        )


def error_body(
    message: str,
    type_: str,
    *,
    param: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    """Return the error envelope as a plain dict.

    ``param`` and ``code`` are always present, explicitly null when unset, which
    is what real OpenAI responses do and what the schema types as
    ``["string", "null"]``. Omitting them would also validate, but clients that
    read ``error.code`` without a guard are common enough that emitting the key
    is the friendlier of two conforming choices.
    """
    return {
        "error": {
            "message": message,
            "type": type_,
            "param": param,
            "code": code,
        }
    }


def error_response(
    status: int,
    message: str,
    type_: str,
    param: str | None = None,
    code: str | None = None,
) -> JSONResponse:
    """Return a ``JSONResponse`` conforming to ``openai-error.v1``.

    Args:
        status: HTTP status code to send.
        message: Human-readable description. Never include a credential, a token,
            a session identifier, or message content here -- this string goes to
            the client verbatim and is not redacted.
        type_: OpenAI error category, e.g. ``invalid_request_error``.
        param: Request parameter the error refers to, or None.
        code: Machine-readable code, e.g. ``context_length_exceeded``, or None.
    """
    return JSONResponse(
        error_body(message, type_, param=param, code=code), status_code=status
    )


def _get_validator() -> Any:
    """Return the hoisted error-envelope validator, building it on first use.

    ``load_schemas()`` is called bare, with no subset mapping. A subset raises
    ``referencing.exceptions.Unresolvable`` from inside ``build_validator``,
    which is not a ``ValidationError`` and would escape any ``except
    ValidationError`` a caller wrapped this in.
    """
    global _validator
    if _validator is None:
        with _validator_lock:
            if _validator is None:
                from .schemas import build_validator, load_schemas

                _validator = build_validator(ERROR_SCHEMA_ID, load_schemas())
    return _validator


def assert_error_body(body: Any) -> None:
    """Raise ``jsonschema.ValidationError`` unless *body* conforms. Test-only.

    Deliberately not called from any request path. See the module docstring.
    """
    _get_validator().validate(body)


# ---------------------------------------------------------------------------
# Starlette exception handlers
# ---------------------------------------------------------------------------


async def gateway_error_handler(request: Any, exc: Exception) -> JSONResponse:
    """Convert a :class:`GatewayError` into its wire envelope."""
    assert isinstance(exc, GatewayError)
    return exc.to_response()


async def http_exception_handler(request: Any, exc: Exception) -> JSONResponse:
    """Convert Starlette's own ``HTTPException`` into the envelope.

    Without this, an unrouted path returns ``text/plain`` "Not Found" and a
    wrong method returns a bare 405, neither of which an OpenAI SDK can parse.
    A client that typo'd the URL should learn that from the error body rather
    than from a JSON decode failure.
    """
    status = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", None) or "request could not be handled"
    return error_response(status, str(detail), "invalid_request_error")


async def unhandled_exception_handler(request: Any, exc: Exception) -> JSONResponse:
    """Last resort for a bug in the gateway itself.

    Emits the exception *type* and never its ``str()``: an exception message can
    carry a URL with an embedded credential, a fragment of a request body, or a
    path from the developer's machine, and this response goes to the client.
    """
    return error_response(
        500,
        f"internal gateway error ({type(exc).__name__})",
        "internal_error",
        code="internal_error",
    )
