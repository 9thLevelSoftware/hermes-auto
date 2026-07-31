# Privacy

Hermes Auto Router sees request content in memory because it must forward the
request to the selected provider. It does not create a prompt database,
telemetry store, replay corpus, training dataset, or outcome history.

## What remains in memory

The gateway keeps a bounded set of routing facts:

- a salted hash of the root session identifier;
- a hash used to recognize the current turn;
- selected candidate ID and tier;
- detected complexity tier and score;
- capability/context filter reason codes;
- sanitized fallback reason classes; and
- whether a route is pinned for a tool loop or session.

It does not retain prompts, message text, tool-result bodies, images, raw
session identifiers, credentials, provider response bodies, user feedback, or
outcome scores. A gateway restart clears all routing decisions.

The admin decision endpoint and explanation commands expose only this bounded,
prompt-free representation.

## Credentials

Configuration stores `none` or an environment-variable reference such as
`env:PROVIDER_API_KEY`. Discovery may report the variable's name and whether it
is populated, but never reads the value into discovery output or writes it to
configuration.

The gateway resolves the value only when constructing the selected candidate's
outbound authentication header. Redaction prevents secrets and credentialed
URLs from entering logs or aggregate errors.

## Provider disclosure

Request content is sent to the candidate selected from the shortlist you
approved. Each candidate configuration names its provider, model, and endpoint.
Fallback can send the same request to another eligible approved candidate after
a network, authentication, rate-limit, or server failure and before a response
is committed.

Hermes Auto Router cannot control an upstream provider's retention policy.
Choose candidates whose provider policies are appropriate for your data.

## Local access

Inference and admin listeners bind to loopback and require different generated
bearer tokens stored with restrictive permissions. No CORS headers are enabled.
Any process already running as the same operating-system user may still be able
to inspect process memory or environment variables; that operating-system
account boundary is outside this application's control.
