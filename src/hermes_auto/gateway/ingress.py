"""Authentication, limits, and the envelope strip -- everything before the hop.

This is design.md 5.4's Ingress box, minus the parts Phase 2 does not have:
there is no canonicalization, no token estimation, and no routing. What is here
is the four things that must happen before a byte leaves for the upstream.

**Authentication takes one path.** A missing ``Authorization`` header and a wrong
token run through the same ``compare_token`` call and produce the same response.
Branching early on "no header" is the usual shape, and it hands a local attacker
a free oracle: the absent case returns measurably faster than the wrong case, so
"is there a token at all" is answerable by timing before the token itself is
attacked. Loopback binding does not help here -- the threat this addresses is
another process on the same machine. The one place the code *does* branch is on
an absent **expected** token, and it must: ``hmac.compare_digest(b"", b"")`` is
``True``, so comparing an empty credential against an empty expected token
authenticates a caller who sent nothing. That branch denies unconditionally and
still pays a decoy comparison, so it costs the same as the wrong-token path.

**The size cap is enforced while reading, not after.** Checking
``Content-Length`` alone is checking a number the client chose; a chunked request
with no length would sail past it. The body is accumulated with a running total
and abandoned the moment it exceeds the cap, so a 5 GB upload costs the cap and
not the upload. This happens before any upstream connection is opened, which is
what the 413 contract test asserts by checking the mock received nothing.

**The body is an opaque dict.** Parse once, ``pop("_hermes_auto")``, re-dump.
Nothing is modelled, so nothing can be dropped: a field this project has never
heard of survives because there is no schema in the path deciding what to keep.
That is what makes R5 hold for ``reasoning_content``, for
``prediction``, and for whatever OpenAI ships next quarter. This is also why
Pydantic is refused project-wide -- its whole value is modelling the body, and a
modelled body silently discards the unmodelled parts.

**Validators are hoisted, and only the envelope is checked inline.**
``gateway/schemas.py``'s ``validate()`` re-runs ``check_schema`` on every call at
roughly 50 ms a time, which is two orders of magnitude more than the validation
itself. Everything here uses ``build_validator`` once. The ``_hermes_auto``
envelope is ours, bounded to five fields, and costs microseconds, so it is
checked on every request. The full ``openai-chat-request.v1`` check is a
different question -- it walks a 100-200 KB ``messages`` array against a
recursive schema -- and runs only under ``gateway.strict_validation``, whose
default ``02-CONTEXT.md`` ties to the measurement in
``tests/performance/test_validation_cost.py``.
"""

from __future__ import annotations

import json
import secrets
import threading
from typing import Any

import jsonschema
from starlette.requests import ClientDisconnect, Request

from .auth import compare_token
from .errors import GatewayError

__all__ = [
    "ENVELOPE_KEY",
    "ENVELOPE_SCHEMA_ID",
    "MAX_REQUEST_BYTES",
    "PROTOCOL_VERSION",
    "REQUEST_SCHEMA_ID",
    "authenticate",
    "build_validators",
    "encode_body",
    "enforce_limits",
    "envelope_validator",
    "parse_body",
    "request_validator",
    "strip_envelope",
    "validate_envelope",
    "validate_request",
]

#: The private plugin-to-gateway channel. design.md 5.1: the gateway MUST strip
#: this key before the request reaches any upstream, so no provider ever observes
#: it.
ENVELOPE_KEY = "_hermes_auto"

ENVELOPE_SCHEMA_ID = (
    "https://hermes-auto-router.dev/schema/routing/hermes-auto-metadata.v1.json"
)
REQUEST_SCHEMA_ID = (
    "https://hermes-auto-router.dev/schema/wire/openai-chat-request.v1.json"
)

#: The only envelope protocol this gateway speaks. A mismatch is rejected rather
#: than best-effort parsed: a plugin and gateway that disagree here disagree
#: about the meaning of every other field in the envelope.
PROTOCOL_VERSION = 1

#: Default request-body ceiling. Vision requests carry base64 images and are
#: genuinely large, so this is generous; it exists to bound memory, not to
#: express a policy about request size.
#:
#: Not currently settable from config.yaml. ``config.GatewayConfig`` freezes its
#: key set and rejects unknown keys under ``gateway:``, and plan 02-04 does not
#: own ``config.py``. Injected through ``create_app(max_body_bytes=...)`` until
#: a plan that owns the config module adds the key.
MAX_REQUEST_BYTES = 32 * 1024 * 1024

_JSON_CONTENT_TYPES = ("application/json",)

_validators: dict[str, Any] = {}
_validator_lock = threading.Lock()

#: Compared against when no token is configured, so that path costs the same as
#: a wrong-token path. Generated per process and never written down: a constant
#: here would be a credential an attacker could read out of the source and send.
_DECOY_TOKEN = secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Hoisted validators
# ---------------------------------------------------------------------------


def _safe_echo(value: str, *, limit: int = 64) -> str:
    """Bound and sanitise a caller-supplied string before it enters an error body.

    An error ``message`` is rendered to a terminal by the OpenAI SDK, so an
    unbounded echo of a request header hands the caller the terminal. The
    ``!r`` at the call site already reprs control bytes and h11 rejects them in
    a header value, so this is defence in depth rather than a closed hole --
    it pins the property instead of relying on either mechanism.
    """
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "..."


def _validator_for(schema_id: str) -> Any:
    """Return a validator for *schema_id*, building it at most once per process.

    ``load_schemas()`` is called bare. Passing a subset mapping -- for instance
    only the ``wire/`` directory -- makes ``$ref`` resolution raise
    ``referencing.exceptions.Unresolvable``, which is **not** a
    ``ValidationError`` and would escape the ``except`` clauses below. The
    request schema's ``_hermes_auto`` property is a ``$ref`` into ``routing/``,
    so a wire-only mapping fails on precisely the requests that carry an
    envelope -- which in production is all of them.
    """
    cached = _validators.get(schema_id)
    if cached is not None:
        return cached
    with _validator_lock:
        cached = _validators.get(schema_id)
        if cached is None:
            from .schemas import build_validator, load_schemas

            cached = build_validator(schema_id, load_schemas())
            _validators[schema_id] = cached
        return cached


def envelope_validator() -> Any:
    """The hoisted ``hermes-auto-metadata.v1`` validator."""
    return _validator_for(ENVELOPE_SCHEMA_ID)


def request_validator() -> Any:
    """The hoisted ``openai-chat-request.v1`` validator."""
    return _validator_for(REQUEST_SCHEMA_ID)


def build_validators() -> None:
    """Build every validator this module uses. Call from lifespan startup.

    Both are built unconditionally, including the request validator when
    ``strict_validation`` is off, so that flipping the flag at runtime cannot put
    a 50 ms ``check_schema`` pass on a user's first request.
    """
    envelope_validator()
    request_validator()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _bearer(header: str | None) -> str:
    """Extract the bearer credential, returning ``""`` for anything unusable.

    Returning a sentinel rather than raising is what keeps absent and malformed
    on the same code path as wrong. The empty string is then compared against the
    real token like any other candidate and loses.
    """
    if not header:
        return ""
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return credential.strip()


def authenticate(request: Request, expected_token: str | None = None) -> None:
    """Reject the request unless it carries the gateway's bearer token.

    Args:
        request: The inbound request.
        expected_token: The minted token. Read from ``request.app.state`` when
            omitted, so a handler can call ``authenticate(request)``.

    Raises:
        GatewayError: 401 in the ``openai-error.v1`` shape. The message is
            identical for a missing and a wrong token, and both reach it through
            the same ``compare_token`` call.
    """
    if expected_token is None:
        expected_token = getattr(request.app.state, "token", None)

    supplied = _bearer(request.headers.get("authorization"))

    if not expected_token:
        # `hmac.compare_digest(b"", b"")` is True, so comparing against an empty
        # expected token authenticates a caller who sent no credential at all --
        # the exact opposite of the paragraph above. Deny unconditionally, but
        # still pay a comparison against a decoy so that "this gateway has no
        # token" and "you sent the wrong token" do not separate under timing for
        # a local process. Same shape as `gateway/admin.py::require_admin`.
        compare_token(supplied, _DECOY_TOKEN)
        raise _invalid_api_key()

    if not compare_token(supplied, expected_token):
        raise _invalid_api_key()


def _invalid_api_key() -> GatewayError:
    """The single 401. One body for a missing, malformed, wrong, or absent token."""
    return GatewayError(
        401,
        "Incorrect API key provided. The hermes-auto-router gateway requires "
        "the bearer token from its state directory.",
        "invalid_request_error",
        code="invalid_api_key",
    )


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


async def enforce_limits(request: Request, max_bytes: int | None = None) -> bytes:
    """Check the content type, read the body under a cap, and return it.

    Runs before any upstream connection is opened, so an oversized body costs one
    local read and zero provider quota.

    Raises:
        GatewayError: 415 for a non-JSON content type, 413 for a body over the
            cap, 400 for a client that disconnects mid-upload.
    """
    if max_bytes is None:
        max_bytes = getattr(request.app.state, "max_body_bytes", MAX_REQUEST_BYTES)

    content_type = request.headers.get("content-type", "")
    # Split off charset and boundary parameters before comparing.
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in _JSON_CONTENT_TYPES:
        raise GatewayError(
            415,
            f"Unsupported content type {_safe_echo(media_type) or '<missing>'!r}; "
            "expected application/json.",
            "invalid_request_error",
            param="content-type",
            code="unsupported_media_type",
        )

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise _too_large(max_bytes)
        except ValueError:
            # A malformed Content-Length is not trusted and not fatal; the
            # running total below is the real enforcement.
            pass

    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                # Abandon immediately. Reading the rest to be polite is how a
                # size cap becomes a memory-exhaustion vector with extra steps.
                raise _too_large(max_bytes)
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise GatewayError(
            400,
            "Client disconnected before the request body was complete.",
            "invalid_request_error",
            code="incomplete_request",
        ) from exc

    return b"".join(chunks)


def _too_large(max_bytes: int) -> GatewayError:
    return GatewayError(
        413,
        f"Request body exceeds the {max_bytes} byte limit.",
        "invalid_request_error",
        code="request_too_large",
    )


# ---------------------------------------------------------------------------
# The opaque body
# ---------------------------------------------------------------------------


def parse_body(raw: bytes) -> dict[str, Any]:
    """Parse the request body into an opaque dict.

    The only structural claim made here is "the top level is a JSON object".
    Nothing below it is inspected, named, or typed.
    """
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayError(
            400,
            f"Request body is not valid JSON ({type(exc).__name__}).",
            "invalid_request_error",
            code="invalid_json",
        ) from exc
    if not isinstance(body, dict):
        raise GatewayError(
            400,
            f"Request body must be a JSON object, got {type(body).__name__}.",
            "invalid_request_error",
            code="invalid_request_body",
        )
    return body


def strip_envelope(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split *body* into what goes upstream and the ``_hermes_auto`` envelope.

    A shallow copy, not a deep one: only the top-level key set changes, and deep
    copying a 200 KB ``messages`` array on every request would be a real cost for
    no benefit. The input is not mutated.

    An absent envelope yields an empty dict, not an error. Absent is not corrupt:
    the gateway is an OpenAI-compatible endpoint, and a caller that speaks plain
    OpenAI without the plugin is served normally. A *present* envelope is then
    held to the schema in full.

    Returns:
        ``(forwarded_body, envelope)``. Every key of *body* except
        ``_hermes_auto`` appears in ``forwarded_body`` with an identical value,
        including keys this project has never heard of.
    """
    forwarded = dict(body)
    envelope = forwarded.pop(ENVELOPE_KEY, None)
    if envelope is None:
        return forwarded, {}
    if not isinstance(envelope, dict):
        raise GatewayError(
            400,
            f"{ENVELOPE_KEY} must be an object, got {type(envelope).__name__}.",
            "invalid_request_error",
            param=ENVELOPE_KEY,
            code="invalid_routing_metadata",
        )
    return forwarded, envelope


def validate_envelope(envelope: dict[str, Any]) -> None:
    """Validate a non-empty ``_hermes_auto`` envelope against its frozen schema.

    The ``protocol_version`` check is made explicitly *before* the schema run so
    that a version mismatch produces a message naming both versions. The schema's
    ``const: 1`` would reject it either way, but with an error text that reads as
    a generic constraint failure -- and this is the one failure a user is most
    likely to hit, by upgrading one half of the pair.

    Raises:
        GatewayError: 400 in the error envelope. Never a bare
            ``ValidationError``: this is on the request path, and a leaked
            ``jsonschema`` exception becomes a 500 with a stack trace in it.
    """
    if not envelope:
        return

    version = envelope.get("protocol_version")
    if version != PROTOCOL_VERSION:
        raise GatewayError(
            400,
            f"{ENVELOPE_KEY}.protocol_version is {version!r}; this gateway speaks "
            f"version {PROTOCOL_VERSION}. Upgrade the hermes-auto provider plugin "
            f"and the gateway together.",
            "invalid_request_error",
            param=f"{ENVELOPE_KEY}.protocol_version",
            code="protocol_version_mismatch",
        )

    try:
        envelope_validator().validate(envelope)
    except jsonschema.ValidationError as exc:
        # Report the failing keyword and the path, never exc.message. Avoiding
        # exc.instance is not enough: for maxLength, minLength, type and
        # additionalProperties, jsonschema embeds the offending instance *inside*
        # exc.message, so formatting it echoed root_session_id -- and, on the type
        # branch, an arbitrary caller-supplied object -- straight back to the
        # client. This is the shape validate_request below already used.
        path = ".".join(str(part) for part in exc.absolute_path)
        raise GatewayError(
            400,
            f"{ENVELOPE_KEY} failed validation at "
            f"{path or '<root>'} (constraint: {exc.validator}).",
            "invalid_request_error",
            param=f"{ENVELOPE_KEY}.{path}" if path else ENVELOPE_KEY,
            code="invalid_routing_metadata",
        ) from exc


def validate_request(body: dict[str, Any], strict_validation: bool) -> None:
    """Run the full request-schema check, but only under ``strict_validation``.

    Default off per ``02-CONTEXT.md`` § Request Validation Policy. The validator
    is hoisted either way, so turning the flag on costs validation time and not a
    ``check_schema`` pass.

    The trade being made: with the flag off, a malformed request is rejected by
    the *upstream* rather than by the gateway, which is both cheaper and more
    faithful -- the provider's own error is what the caller would have seen
    talking to it directly, which is exactly R5.
    """
    if not strict_validation:
        return
    try:
        request_validator().validate(body)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path)
        # exc.message can quote the offending instance, which at this level may
        # be message content. Only the keyword and the path are reported.
        raise GatewayError(
            400,
            f"Request failed validation at {path or '<root>'} "
            f"(constraint: {exc.validator}).",
            "invalid_request_error",
            param=path or None,
            code="invalid_request_body",
        ) from exc


def encode_body(body: dict[str, Any]) -> bytes:
    """Serialize the forwarded body.

    ``separators`` drops the whitespace ``json.dumps`` inserts by default, so the
    re-dump is not larger than what arrived. ``ensure_ascii=False`` keeps
    non-ASCII characters as themselves rather than expanding each to a ``\\uXXXX``
    escape -- which would triple the size of a CJK prompt and change the byte
    count the upstream sees for no reason.
    """
    # ``surrogatepass`` rather than a bare ``encode``: a lone UTF-16 surrogate is
    # legal RFC 8259 JSON and is what every ``ensure_ascii`` encoder emits, so a
    # strict encode turns a request the provider would have accepted into a 500.
    # Windows subprocess output decoded with ``errors="surrogateescape"`` reaches
    # a tool result this way.
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8", "surrogatepass"
    )
