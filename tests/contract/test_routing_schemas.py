"""Contract tests for the four routing schemas defined in Phase 1 plan 01-04.

These tests check *shapes*, never behavior. No routing, scoring, eligibility
filtering, or decision generation exists yet, and nothing here asserts on any.

Two of the tests exist to protect invariants that a later phase could plausibly
erode without noticing:

* ``test_outcome_event_with_raw_prompt_fails`` is the privacy invariant. It
  proves ``additionalProperties: false`` actually *rejects* an event carrying
  request content, rather than the prohibition being documentation only.
* ``test_transport_requires_exactly_three_fields`` guards against reconciling
  ADR-0003's seven-element candidate identity with the card shape by promoting
  ``base_url`` or ``credential_ref`` to required. That tuple is the runtime
  composition of identity in Phase 3, not the required field set of a card.

Schemas are loaded through the ``routing`` subdirectory only. A bare
``load_schemas()`` would also pull in the wire schemas owned by plan 01-03,
making these tests fail on another plan's in-progress work.
"""

import copy
import hashlib
import json
import pathlib
import uuid

import jsonschema
import pytest

from hermes_auto.gateway.schemas import (
    EXPECTED_SCHEMA_IDS,
    SCHEMA_ROOT,
    load_schemas,
    validate,
)

pytestmark = pytest.mark.contract

_ID_PREFIX = "https://hermes-auto-router.dev/schema/routing/"

METADATA_ID = _ID_PREFIX + "hermes-auto-metadata.v1.json"
ROUTE_DECISION_ID = _ID_PREFIX + "route-decision.v1.json"
MODEL_CARD_ID = _ID_PREFIX + "model-card.v1.json"
OUTCOME_EVENT_ID = _ID_PREFIX + "outcome-event.v1.json"

EIGHT_CAPABILITIES = {
    "reasoning",
    "code_generation",
    "debugging",
    "tool_orchestration",
    "long_horizon_execution",
    "context_synthesis",
    "structured_precision",
    "multimodal_reasoning",
}

TWELVE_REASON_CODES = {
    "HARD_REQUIREMENT",
    "CAPABILITY_FIT",
    "DOMAIN_AFFINITY",
    "COST_TIEBREAK",
    "LATENCY_TIEBREAK",
    "STICKY_CACHE",
    "SWITCH_HYSTERESIS",
    "USER_PIN",
    "HEALTH_VETO",
    "PROVIDER_FALLBACK",
    "CONTEXT_ANCHOR",
    "EMERGENCY_DEFAULT",
}


@pytest.fixture(scope="module")
def routing_schemas() -> dict[str, dict]:
    """The four routing schemas, keyed by ``$id``.

    Scoped to ``schema/routing`` rather than the whole schema root so this
    module does not depend on the wire schemas owned by a different plan.
    """
    schemas = load_schemas(SCHEMA_ROOT / "routing")
    # Identity, not count: a count passes when a stray schema is added in the
    # same change that renames a required one.
    expected = {METADATA_ID, ROUTE_DECISION_ID, MODEL_CARD_ID, OUTCOME_EVENT_ID}
    assert set(schemas) == expected, sorted(schemas)
    assert expected <= EXPECTED_SCHEMA_IDS
    return schemas


@pytest.fixture(scope="module")
def fixture_dir(repo_root: pathlib.Path) -> pathlib.Path:
    """Directory holding this plan's synthetic routing fixtures."""
    path = repo_root / "tests" / "fixtures" / "routing"
    assert path.is_dir(), path
    return path


def _load(fixture_dir: pathlib.Path, name: str):
    return json.loads((fixture_dir / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Positive tests
# --------------------------------------------------------------------------


def test_metadata_fixture_validates(fixture_dir, routing_schemas):
    instance = _load(fixture_dir, "valid-hermes-auto-metadata.json")
    validate(instance, METADATA_ID, routing_schemas)


def test_route_decision_fixture_validates(fixture_dir, routing_schemas):
    instance = _load(fixture_dir, "valid-route-decision.json")
    validate(instance, ROUTE_DECISION_ID, routing_schemas)


def test_model_card_fixture_validates(fixture_dir, routing_schemas):
    instance = _load(fixture_dir, "valid-model-card.json")
    validate(instance, MODEL_CARD_ID, routing_schemas)


def test_model_card_unknown_pricing_validates(fixture_dir, routing_schemas):
    """A local candidate with every price null is a legal card.

    Unknown pricing is expressed as null, never as 0. Mode policy — excluded
    from ``economy`` by default, permitted in ``quality`` only on explicit user
    consent — is what handles the unknown, not a zero-valued default.
    """
    instance = _load(fixture_dir, "valid-model-card-unknown-pricing.json")
    validate(instance, MODEL_CARD_ID, routing_schemas)

    economics = instance["economics"]
    assert set(economics.values()) == {None}, economics


def test_all_outcome_events_validate(fixture_dir, routing_schemas):
    events = _load(fixture_dir, "valid-outcome-events.json")
    assert len(events) >= 4, len(events)
    for event in events:
        validate(event, OUTCOME_EVENT_ID, routing_schemas)

    covered = {event["event_type"] for event in events}
    assert {"request_failed", "user_feedback"} <= covered, covered


# --------------------------------------------------------------------------
# Structural tests
# --------------------------------------------------------------------------


def test_route_decision_selected_need_not_be_top_ranked(
    fixture_dir, routing_schemas
):
    """A health veto selects a lower-ranked candidate; that must validate.

    The schema deliberately does not constrain ``selected`` to equal
    ``ranked[0].candidate``. This fixture selects the second entry precisely so
    a future tightening of that rule fails here rather than in Phase 4.
    """
    instance = _load(fixture_dir, "valid-route-decision.json")
    validate(instance, ROUTE_DECISION_ID, routing_schemas)

    assert instance["selected"] != instance["ranked"][0]["candidate"]
    assert instance["selected"] == instance["ranked"][1]["candidate"]


def test_route_decision_allows_empty_ranked(routing_schemas):
    """Every candidate hard-excluded is a representable structured failure."""
    instance = {
        "decision_id": "dec_test_empty_ranked",
        "root_session_hash": "0" * 64,
        "lane_id": "lane_test_main",
        "cache_epoch": 0,
        "turn_id": 0,
        "mode": "economy",
        "requirements": {"reasoning": 0.5},
        "excluded": [
            {
                "candidate": "test-local-fast",
                "reasons": ["context window 65536 < required 84211"],
            },
            {
                "candidate": "test-hosted-general",
                "reasons": ["privacy_class cloud not permitted by policy"],
            },
        ],
        "ranked": [],
        "selected": None,
        "reason_codes": ["HARD_REQUIREMENT"],
    }
    validate(instance, ROUTE_DECISION_ID, routing_schemas)


def test_reason_code_enum_has_twelve_codes(routing_schemas):
    """The design.md §12 reason-code vocabulary is frozen at twelve."""
    schema = routing_schemas[ROUTE_DECISION_ID]
    enum = schema["properties"]["reason_codes"]["items"]["enum"]

    assert len(enum) == 12, enum
    assert set(enum) == TWELVE_REASON_CODES, set(enum) ^ TWELVE_REASON_CODES
    assert "DEBUGGING_AFFINITY" not in enum


def test_all_eight_capabilities_required_on_card(routing_schemas):
    """A card must score every dimension; a missing one is not a surplus."""
    schema = routing_schemas[MODEL_CARD_ID]
    capabilities = schema["properties"]["capabilities"]

    assert set(capabilities["required"]) == EIGHT_CAPABILITIES
    assert set(capabilities["properties"]) == EIGHT_CAPABILITIES
    assert capabilities["additionalProperties"] is False

    for name, subschema in capabilities["properties"].items():
        assert subschema["minimum"] == 0, name
        assert subschema["maximum"] == 1, name


def test_transport_requires_exactly_three_fields(routing_schemas):
    """``transport.required`` is exactly adapter, provider, model.

    ADR-0003's seven-element candidate identity is composed at runtime in
    Phase 3 from the card plus resolved configuration. Promoting ``base_url``
    or ``credential_ref`` to required to "follow the ADR" would make every
    local unauthenticated candidate unrepresentable.
    """
    schema = routing_schemas[MODEL_CARD_ID]
    transport = schema["properties"]["transport"]

    assert set(transport["required"]) == {"adapter", "provider", "model"}


def test_outcome_event_defines_no_content_carrying_property(routing_schemas):
    """The allowlist contains no property that could hold content, at any depth."""
    forbidden = {
        "prompt",
        "prompt_text",
        "raw_prompt",
        "messages",
        "content",
        "tool_result",
        "tool_output",
        "response_text",
        "api_key",
        "secret",
        "credential",
    }

    def walk(node, path="$"):
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            overlap = forbidden & set(properties)
            assert not overlap, f"{path}: {sorted(overlap)}"
            # Every object level is closed, so no future field can smuggle
            # content in without a schema version bump.
            assert node.get("additionalProperties") is False, path
            for name, child in properties.items():
                walk(child, f"{path}.{name}")
        if "items" in node:
            walk(node["items"], path + "[]")

    walk(routing_schemas[OUTCOME_EVENT_ID])


def test_every_routing_schema_declares_id_and_dialect(routing_schemas):
    """All four schemas are self-describing and use draft 2020-12."""
    assert set(routing_schemas) == {
        METADATA_ID,
        ROUTE_DECISION_ID,
        MODEL_CARD_ID,
        OUTCOME_EVENT_ID,
    }

    for schema_id, schema in routing_schemas.items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["title"], schema_id
        assert schema["description"], schema_id
        jsonschema.Draft202012Validator.check_schema(schema)


# --------------------------------------------------------------------------
# Negative tests
# --------------------------------------------------------------------------


def test_capability_above_one_fails(fixture_dir, routing_schemas):
    instance = _load(
        fixture_dir, "invalid-model-card-capability-out-of-range.json"
    )
    assert instance["capabilities"]["debugging"] == 1.4

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, MODEL_CARD_ID, routing_schemas)


def test_outcome_event_with_raw_prompt_fails(fixture_dir, routing_schemas):
    """The privacy invariant: an event carrying request content is rejected.

    This is the test that makes ADR-0004's "no raw prompt text" default
    structural. If it ever passes, the outcome-event schema has been opened up
    and prompts can be persisted while every other gate stays green.
    """
    instance = _load(fixture_dir, "invalid-outcome-event-raw-prompt.json")
    assert "prompt_text" in instance

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, OUTCOME_EVENT_ID, routing_schemas)


def test_credential_ref_rejects_literal_key(fixture_dir, routing_schemas):
    """A card can reference a credential but can never contain one."""
    instance = copy.deepcopy(_load(fixture_dir, "valid-model-card.json"))
    instance["transport"]["credential_ref"] = "sk-test-not-a-real-key"

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, MODEL_CARD_ID, routing_schemas)


def test_metadata_rejects_unknown_key(fixture_dir, routing_schemas):
    """The envelope is plugin-authored, so an unknown key is a version skew."""
    instance = copy.deepcopy(
        _load(fixture_dir, "valid-hermes-auto-metadata.json")
    )
    instance["unexpected_field"] = "value from a newer plugin"

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, METADATA_ID, routing_schemas)


def test_metadata_rejects_wrong_protocol_version(fixture_dir, routing_schemas):
    """protocol_version != 1 must fail rather than be best-effort parsed."""
    instance = copy.deepcopy(
        _load(fixture_dir, "valid-hermes-auto-metadata.json")
    )
    instance["protocol_version"] = 2

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, METADATA_ID, routing_schemas)


def test_excluded_candidate_requires_a_reason(routing_schemas):
    """An exclusion with an empty reason list is unrepresentable."""
    instance = {
        "decision_id": "dec_test_no_reason",
        "root_session_hash": "0" * 64,
        "lane_id": "lane_test_main",
        "cache_epoch": 0,
        "turn_id": 0,
        "mode": "balanced",
        "requirements": {},
        "excluded": [{"candidate": "test-local-fast", "reasons": []}],
        "ranked": [],
        "selected": None,
        # A real code, not []: with an empty list this instance also violates
        # reason_codes' minItems and would keep passing for the wrong reason.
        "reason_codes": ["HARD_REQUIREMENT"],
    }

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, ROUTE_DECISION_ID, routing_schemas)


def test_identifier_fields_reject_oversized_values(fixture_dir, routing_schemas):
    """A 4 KB payload in any identifier field is rejected.

    ``test_outcome_event_defines_no_content_carrying_property`` walks property
    NAMES and cannot see this class of defect: ``additionalProperties: false``
    constrains which keys exist, never what their values hold. Before the value
    constraints landed, a full multi-line prompt -- API key included -- passed
    validation in ``root_session_hash``, ``lane_id``, ``event_id``, and
    ``candidate`` alike, which made the schema description's own claim false.
    """
    payload = "A" * 4096

    for field in ("event_id", "root_session_hash", "lane_id", "candidate"):
        instance = copy.deepcopy(
            _load(fixture_dir, "valid-outcome-events.json")[0]
        )
        instance[field] = payload

        with pytest.raises(jsonschema.ValidationError):
            validate(instance, OUTCOME_EVENT_ID, routing_schemas)


def test_root_session_hash_rejects_raw_session_id(fixture_dir, routing_schemas):
    """The raw Hermes session id must not fit where its salted digest goes.

    ADR-0004 and docs/privacy.md tell users cross-machine correlation is
    unavailable *by construction*. ``^[0-9a-f]{64}$`` is what backs that claim:
    a fixed-width hex digest has no room for a session id, a prompt, or a
    credential, so no future code path can quietly write one here.
    """
    raw = "hermes-session-9f2c-user-alice"

    event = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[0])
    event["root_session_hash"] = raw
    with pytest.raises(jsonschema.ValidationError):
        validate(event, OUTCOME_EVENT_ID, routing_schemas)

    decision = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))
    decision["root_session_hash"] = raw
    with pytest.raises(jsonschema.ValidationError):
        validate(decision, ROUTE_DECISION_ID, routing_schemas)

    # Both schemas agree on the constraint, so neither can drift alone.
    for schema_id in (OUTCOME_EVENT_ID, ROUTE_DECISION_ID):
        pattern = routing_schemas[schema_id]["properties"]["root_session_hash"][
            "pattern"
        ]
        assert pattern == "^[0-9a-f]{64}$", schema_id


def test_decision_id_pattern_is_anchored_at_both_ends(fixture_dir, routing_schemas):
    """``^dec_`` alone accepted ``dec_`` followed by an arbitrary payload."""
    smuggled = "dec_" + "sk-not-a-real-key\nline two of a raw prompt"

    event = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[0])
    event["decision_id"] = smuggled
    with pytest.raises(jsonschema.ValidationError):
        validate(event, OUTCOME_EVENT_ID, routing_schemas)

    decision = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))
    decision["decision_id"] = smuggled
    with pytest.raises(jsonschema.ValidationError):
        validate(decision, ROUTE_DECISION_ID, routing_schemas)


@pytest.mark.parametrize(
    "occurred_at",
    ["", "not-a-timestamp", "2026-07-26", "2026-07-26 10:00:00", "2026-07-26T10:00:00"],
)
def test_occurred_at_rejects_non_rfc3339_values(
    occurred_at, fixture_dir, routing_schemas
):
    """``format: date-time`` is annotation-only, so a ``pattern`` does the work.

    ``jsonschema`` applies no format checker unless one is passed explicitly,
    and the ``date-time`` checker additionally needs ``rfc3339-validator``,
    which this project does not depend on. Both ``""`` and outright garbage
    validated before the pattern was added.
    """
    instance = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[0])
    instance["occurred_at"] = occurred_at

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, OUTCOME_EVENT_ID, routing_schemas)


def test_reason_codes_reject_empty_and_duplicated(fixture_dir, routing_schemas):
    """The selection path must be as explainable as the exclusion path.

    ``excluded[].reasons`` carries ``minItems: 1`` so no exclusion is
    unexplained; ``reason_codes`` had neither ``minItems`` nor ``uniqueItems``,
    so a decision could select a candidate and explain nothing (design.md §12).
    """
    instance = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))

    instance["reason_codes"] = []
    with pytest.raises(jsonschema.ValidationError):
        validate(instance, ROUTE_DECISION_ID, routing_schemas)

    instance["reason_codes"] = ["COST_TIEBREAK"] * 3
    with pytest.raises(jsonschema.ValidationError):
        validate(instance, ROUTE_DECISION_ID, routing_schemas)


def test_exclusion_reason_is_bounded_free_text(fixture_dir, routing_schemas):
    """The one free-text channel in the record is capped, and the description says so.

    ``excluded[].reasons[]`` is stored durably, so an unbounded string is a
    place a stack trace or a tool-result body could land. 200 characters is
    comfortably above the design.md §12 example and far below any body.
    """
    instance = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))
    instance["excluded"][0]["reasons"] = ["x" * 201]

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, ROUTE_DECISION_ID, routing_schemas)

    instance["excluded"][0]["reasons"] = ["x" * 200]
    validate(instance, ROUTE_DECISION_ID, routing_schemas)

    instance["excluded"][0]["reasons"] = ["ok"] * 9
    with pytest.raises(jsonschema.ValidationError):
        validate(instance, ROUTE_DECISION_ID, routing_schemas)


def test_unknown_domain_tag_on_card_fails(fixture_dir, routing_schemas):
    """A misspelled affinity fails loudly instead of silently scoring zero."""
    instance = copy.deepcopy(_load(fixture_dir, "valid-model-card.json"))
    instance["affinities"]["backend_aplication"] = 0.9

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, MODEL_CARD_ID, routing_schemas)


# --------------------------------------------------------------------------
# Cross-schema drift
# --------------------------------------------------------------------------

#: Every place a model-card ``id`` is named. The card is the definition; the
#: other four are consumers, and a consumer stricter than the definition makes a
#: loadable card unwritable.
_CANDIDATE_ID_SITES = (
    (MODEL_CARD_ID, ("properties", "id")),
    (ROUTE_DECISION_ID, ("properties", "excluded", "items", "properties", "candidate")),
    (ROUTE_DECISION_ID, ("properties", "ranked", "items", "properties", "candidate")),
    (ROUTE_DECISION_ID, ("properties", "selected")),
    (OUTCOME_EVENT_ID, ("properties", "candidate")),
)

_DECISION_ID_SITES = (
    (ROUTE_DECISION_ID, ("properties", "decision_id")),
    (OUTCOME_EVENT_ID, ("properties", "decision_id")),
)


def _at(schema, path):
    for key in path:
        schema = schema[key]
    return schema


def test_candidate_id_constraint_is_identical_at_every_site(routing_schemas):
    """The five places a candidate id appears must agree byte for byte.

    This is the drift guard, in the shape of the ``root_session_hash`` check.
    Model-card ``id`` was left at ``minLength: 1`` while all four of its
    consumers were tightened to ``maxLength: 128`` plus a pattern, so a card
    with ``id: "openai/gpt-4o"`` loaded cleanly in Phase 3 and produced a
    RouteDecision that could not be written in Phase 4. Nothing caught it,
    because every schema was internally consistent. Asserting equality across
    sites is what makes the next such divergence fail here.
    """
    reference = _at(routing_schemas[MODEL_CARD_ID], ("properties", "id"))

    for schema_id, path in _CANDIDATE_ID_SITES:
        node = _at(routing_schemas[schema_id], path)
        where = f"{schema_id}#/{'/'.join(path)}"
        assert node["pattern"] == reference["pattern"], where
        assert node["maxLength"] == reference["maxLength"], where

    assert reference["maxLength"] == 128
    assert reference["pattern"] == "^[A-Za-z0-9_.:-]+(/[A-Za-z0-9_.:-]+)*$"


def test_decision_id_constraint_is_identical_at_both_sites(routing_schemas):
    """A decision must be joinable to its own outcome events."""
    patterns = {
        _at(routing_schemas[sid], path)["pattern"] for sid, path in _DECISION_ID_SITES
    }
    assert patterns == {"^dec_[A-Za-z0-9_-]{1,64}$"}, patterns


def test_slash_delimited_candidate_id_round_trips(routing_schemas):
    """``openai/gpt-4o`` must survive card -> decision -> outcome event.

    Slash-delimited ids are the aggregator convention the project targets, and
    the repo's own fixture already uses one for ``transport.model``. Forcing
    operators to rename their candidates to satisfy the router is the wrong
    direction, so ``/`` is in the shared class.
    """
    candidate = "openai/gpt-4o"

    card = {
        "id": candidate,
        "enabled": True,
        "transport": {
            "adapter": "openai_compatible",
            "provider": "test-aggregator",
            "model": "openai/gpt-4o",
        },
        "capabilities": dict.fromkeys(EIGHT_CAPABILITIES, 0.5),
        "limits": {
            "context_tokens": 128000,
            "max_output_tokens": 16384,
            "tools": True,
            "vision": True,
            "structured_output": True,
        },
    }
    validate(card, MODEL_CARD_ID, routing_schemas)

    decision = {
        "decision_id": "dec_test_slash_id",
        "root_session_hash": "0" * 64,
        "lane_id": "lane_test_main",
        "cache_epoch": 0,
        "turn_id": 0,
        "mode": "balanced",
        "requirements": {},
        "excluded": [{"candidate": candidate, "reasons": ["health veto"]}],
        "ranked": [{"candidate": candidate, "shortfall": 0.0, "final_loss": 0.1}],
        "selected": candidate,
        "reason_codes": ["CAPABILITY_FIT"],
    }
    validate(decision, ROUTE_DECISION_ID, routing_schemas)

    event = {
        "event_id": "evt_test_slash",
        "event_type": "request_completed",
        "occurred_at": "2026-07-26T10:00:00Z",
        "root_session_hash": "0" * 64,
        "lane_id": "lane_test_main",
        "candidate": candidate,
    }
    validate(event, OUTCOME_EVENT_ID, routing_schemas)


@pytest.mark.parametrize(
    "candidate",
    ["/leading", "trailing/", "double//slash", "", "has space", "new\nline"],
)
def test_candidate_id_rejects_malformed_separators(candidate, routing_schemas):
    """Permitting ``/`` does not permit empty or unbounded segments."""
    card = {
        "id": candidate,
        "enabled": True,
        "transport": {"adapter": "a", "provider": "p", "model": "m"},
        "capabilities": dict.fromkeys(EIGHT_CAPABILITIES, 0.5),
        "limits": {
            "context_tokens": 1000,
            "max_output_tokens": 100,
            "tools": False,
            "vision": False,
            "structured_output": False,
        },
    }
    with pytest.raises(jsonschema.ValidationError):
        validate(card, MODEL_CARD_ID, routing_schemas)


# --------------------------------------------------------------------------
# Value bounds added in review cycle 3
# --------------------------------------------------------------------------


def test_version_fields_are_bounded(fixture_dir, routing_schemas):
    """``model_card_version`` and ``router_version`` were fully unconstrained.

    Both were ``{"type": "string", "minLength": 1}`` -- a one-million-character
    value validated -- while the schema description one screen above claimed
    every identifier was value-constrained.
    """
    for field in ("model_card_version", "router_version"):
        for bad in ("A" * 65, "has space", "line\nbreak", "a" * 64 + "b"):
            instance = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))
            instance[field] = bad
            with pytest.raises(jsonschema.ValidationError):
                validate(instance, ROUTE_DECISION_ID, routing_schemas)

        # PEP 440 local versions and dates must still validate.
        for good in ("0.1.0", "2026-07-26", "1.2.3+local.1", "v1.2.3-rc.1"):
            instance = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))
            instance[field] = good
            validate(instance, ROUTE_DECISION_ID, routing_schemas)


def test_version_fields_still_accept_a_version_shaped_credential(
    fixture_dir, routing_schemas
):
    """Documented as OPEN BY DESIGN, asserted so nobody claims otherwise.

    ``sk-live-<40 chars>`` is 48 characters of ``[A-Za-z0-9_.:+-]`` and is
    therefore indistinguishable from a version token to any pattern that must
    also accept ``1.2.3+local.1`` and ``v1.2.3-rc.1``. The cap bounds how much
    fits; it does not decide what the value means. This test exists so a future
    reader finds the limitation asserted rather than discovering it in a
    security review, and so a description that starts claiming otherwise is
    contradicted by a green test.
    """
    instance = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))
    instance["router_version"] = "sk-live-" + "x" * 40

    validate(instance, ROUTE_DECISION_ID, routing_schemas)


def test_occurred_at_bounds_fractional_seconds(fixture_dir, routing_schemas):
    """``(\\.\\d+)?`` was an unbounded decimal channel.

    A 100,000-digit fractional second validated. Nanoseconds is the ceiling any
    real clock offers, so nine digits is the cap.
    """
    instance = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[0])

    instance["occurred_at"] = "2026-07-26T10:00:00." + "9" * 9 + "Z"
    validate(instance, OUTCOME_EVENT_ID, routing_schemas)

    for digits in (10, 1000, 100000):
        instance["occurred_at"] = "2026-07-26T10:00:00." + "9" * digits + "Z"
        with pytest.raises(jsonschema.ValidationError):
            validate(instance, OUTCOME_EVENT_ID, routing_schemas)


@pytest.mark.parametrize(
    "occurred_at",
    [
        "2026-13-01T10:00:00Z",  # month 13
        "2026-00-01T10:00:00Z",  # month 0
        "2026-07-45T10:00:00Z",  # day 45
        "2026-07-00T10:00:00Z",  # day 0
        "2026-07-26T99:00:00Z",  # hour 99
        "2026-07-26T24:00:00Z",  # hour 24
        "2026-07-26T10:99:00Z",  # minute 99
        "2026-07-26T10:00:99Z",  # second 99
        "2026-07-26T10:00:00+99:00",  # offset hour 99
    ],
)
def test_occurred_at_range_checks_components(
    occurred_at, fixture_dir, routing_schemas
):
    """The old shape-only pattern accepted ``2026-13-45T99:99:99Z``."""
    instance = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[0])
    instance["occurred_at"] = occurred_at

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, OUTCOME_EVENT_ID, routing_schemas)


@pytest.mark.parametrize(
    "occurred_at",
    [
        "2026-07-26T10:00:00Z",
        "2026-07-26t10:00:00z",  # RFC 3339 permits lowercase
        "2026-07-26T10:00:00.5Z",
        "2026-07-26T10:00:00.123456789Z",
        "2026-07-26T10:00:00+05:30",
        "2026-07-26T10:00:00-08:00",
        "2026-12-31T23:59:60Z",  # leap second
    ],
)
def test_occurred_at_accepts_legal_rfc3339(occurred_at, fixture_dir, routing_schemas):
    """Range-checking must not reject values RFC 3339 permits.

    Lowercase ``t``/``z`` are legal and the previous pattern rejected them.
    """
    instance = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[0])
    instance["occurred_at"] = occurred_at

    validate(instance, OUTCOME_EVENT_ID, routing_schemas)


def test_occurred_at_is_not_calendar_validated(routing_schemas):
    """Documented limitation, asserted so the description stays honest.

    A regex cannot check a day against its month. ``2026-02-31`` validates, and
    the field description says so. If someone later adds real calendar checking,
    this test fails and the description must be updated with it.
    """
    schema = routing_schemas[OUTCOME_EVENT_ID]["properties"]["occurred_at"]
    assert "not calendar-validated" in schema["description"].lower()

    instance = {
        "event_id": "evt_test_cal",
        "event_type": "request_completed",
        "occurred_at": "2026-02-31T10:00:00Z",
        "root_session_hash": "0" * 64,
        "lane_id": "lane_test_main",
    }
    validate(instance, OUTCOME_EVENT_ID, routing_schemas)


def test_arrays_are_bounded(fixture_dir, routing_schemas):
    """Per-entry caps aggregate without ``maxItems``.

    5,000 exclusions x 8 reasons x 200 characters was 8 MB in one record, every
    individual value inside its documented cap.
    """
    base = _load(fixture_dir, "valid-route-decision.json")

    instance = copy.deepcopy(base)
    instance["excluded"] = [
        {"candidate": f"cand-{i}", "reasons": ["r"]} for i in range(65)
    ]
    with pytest.raises(jsonschema.ValidationError):
        validate(instance, ROUTE_DECISION_ID, routing_schemas)

    instance["excluded"] = instance["excluded"][:64]
    validate(instance, ROUTE_DECISION_ID, routing_schemas)

    instance = copy.deepcopy(base)
    instance["ranked"] = [
        {"candidate": f"cand-{i}", "shortfall": 0.0, "final_loss": 0.0}
        for i in range(65)
    ]
    instance["selected"] = "cand-0"
    with pytest.raises(jsonschema.ValidationError):
        validate(instance, ROUTE_DECISION_ID, routing_schemas)

    instance["ranked"] = instance["ranked"][:64]
    validate(instance, ROUTE_DECISION_ID, routing_schemas)


def test_integer_fields_have_finite_ceilings(fixture_dir, routing_schemas):
    """JSON integers are arbitrary-precision, so ``minimum: 0`` alone is unbounded.

    A 400-digit ``tool_call_count`` validated. The ceilings are generous enough
    that no real measurement approaches them, so they cost nothing.
    """
    huge = 10**400

    for field in ("cache_epoch", "turn_id"):
        instance = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))
        instance[field] = huge
        with pytest.raises(jsonschema.ValidationError):
            validate(instance, ROUTE_DECISION_ID, routing_schemas)

    for field in (
        "ttft_ms",
        "total_latency_ms",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "tool_call_count",
        "invalid_tool_call_count",
        "retry_count",
    ):
        instance = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[0])
        instance[field] = huge
        with pytest.raises(jsonschema.ValidationError):
            validate(instance, OUTCOME_EVENT_ID, routing_schemas)


@pytest.mark.parametrize(
    "error_class",
    [
        "sk-proj-AAAABBBBCCCC",  # a credential shape
        "Timed out talking to the provider.",  # prose with spaces
        "rate‮limit",  # RTL override
        "rate\x00limit",  # embedded NUL
        "RATE_LIMIT",  # not snake_case
        "rate-limit",  # hyphen, not the documented shape
    ],
)
def test_error_class_enforces_snake_case(error_class, fixture_dir, routing_schemas):
    """The 64-character cap alone accepted 64 characters of anything."""
    instance = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[1])
    instance["error_class"] = error_class

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, OUTCOME_EVENT_ID, routing_schemas)


def test_error_class_accepts_the_documented_vocabulary(fixture_dir, routing_schemas):
    instance = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[1])

    for good in (
        "rate_limit",
        "auth_failure",
        "context_overflow",
        "timeout",
        "upstream_5xx",
        "tool_schema_violation",
    ):
        instance["error_class"] = good
        validate(instance, OUTCOME_EVENT_ID, routing_schemas)


def test_decision_id_accepts_the_obvious_generators(fixture_dir, routing_schemas):
    """``"dec_" + str(uuid4())`` and snake_case ids must validate.

    ``^dec_[A-Za-z0-9]{1,64}$`` excluded ``_`` and ``-``, which rejected the one
    generator anybody reaches for and forced a test id to be renamed to
    camelCase to pass. Widening the suffix class does not weaken the field: the
    anchoring and the length cap are what bound it.
    """
    for good in (
        "dec_" + str(uuid.uuid4()),
        "dec_test_empty_ranked",
        "dec_" + hashlib.sha256(b"decision").hexdigest()[:32],
        "dec_a",
    ):
        decision = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))
        decision["decision_id"] = good
        validate(decision, ROUTE_DECISION_ID, routing_schemas)

        event = copy.deepcopy(_load(fixture_dir, "valid-outcome-events.json")[0])
        event["decision_id"] = good
        validate(event, OUTCOME_EVENT_ID, routing_schemas)

    # Still anchored and still capped.
    for bad in ("dec_", "dec_" + "a" * 65, "xdec_abc", "dec_abc def", "dec_abc\ndef"):
        decision = copy.deepcopy(_load(fixture_dir, "valid-route-decision.json"))
        decision["decision_id"] = bad
        with pytest.raises(jsonschema.ValidationError):
            validate(decision, ROUTE_DECISION_ID, routing_schemas)


def test_legitimate_real_world_values_all_validate(routing_schemas):
    """One pass over every tightened field with a value a real system produces.

    Tightening patterns is how you accidentally reject production traffic. This
    asserts the tightened schema still accepts a real UUID event id, a real
    sha256 digest, a dotted-and-coloned lane id, a hyphenated candidate, a
    ``dec_``-prefixed uuid4, and ``selected: null``.
    """
    digest = hashlib.sha256(b"root-session-and-local-salt").hexdigest()
    decision_id = "dec_" + str(uuid.uuid4())

    decision = {
        "decision_id": decision_id,
        "root_session_hash": digest,
        "lane_id": "lane:main.sub-1_v2",
        "cache_epoch": 0,
        "turn_id": 0,
        "mode": "quality",
        "requirements": {"reasoning": 1.0},
        "excluded": [
            {
                "candidate": "anthropic/claude-sonnet-4",
                "reasons": ["context window 65536 < required 84211"],
            }
        ],
        "ranked": [],
        "selected": None,
        "reason_codes": ["HARD_REQUIREMENT"],
        "model_card_version": "1.2.3+local.1",
        "router_version": "0.1.0",
    }
    validate(decision, ROUTE_DECISION_ID, routing_schemas)

    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "request_failed",
        "occurred_at": "2026-07-26T10:00:00.123456789+05:30",
        "root_session_hash": digest,
        "lane_id": "lane:main.sub-1_v2",
        "decision_id": decision_id,
        "candidate": "openai/gpt-4o-mini",
        "error_class": "upstream_5xx",
        "actual_cost_usd": None,
        "retry_count": 2,
    }
    validate(event, OUTCOME_EVENT_ID, routing_schemas)


def test_outcome_event_maximum_serialized_size_matches_its_description(
    routing_schemas,
):
    """The size ceiling the description quotes must be the real one.

    An outcome event with every declared property simultaneously at its cap is
    the schema's worst case. The description names a byte count; a number in a
    description is a claim like any other, and this asserts it. Adding a
    property or raising a cap changes the number and fails here, which is the
    prompt to update the description in the same commit.
    """
    maximal = {
        "event_id": "a" * 64,
        "event_type": "request_completed",
        "occurred_at": "2026-07-26T10:00:00.123456789+05:30",
        "root_session_hash": "f" * 64,
        "lane_id": "a" * 128,
        "decision_id": "dec_" + "a" * 64,
        "candidate": "a" * 128,
        "ttft_ms": 86400000,
        "total_latency_ms": 86400000,
        "input_tokens": 100000000,
        "output_tokens": 100000000,
        "cached_tokens": 100000000,
        "reasoning_tokens": 100000000,
        "actual_cost_usd": 1e308,
        "tool_call_count": 100000,
        "invalid_tool_call_count": 100000,
        "retry_count": 10000,
        "empty_response": True,
        "context_compressed": True,
        "user_interrupted": True,
        "turn_succeeded": True,
        "feedback": "good",
        "error_class": "a" * 64,
    }
    # Every declared property, so this really is the worst case.
    declared = set(routing_schemas[OUTCOME_EVENT_ID]["properties"])
    assert set(maximal) == declared, declared ^ set(maximal)

    validate(maximal, OUTCOME_EVENT_ID, routing_schemas)

    size = len(json.dumps(maximal))
    assert size == 1114, size
    assert "1,114 bytes" in routing_schemas[OUTCOME_EVENT_ID]["description"]


def test_route_decision_free_text_ceiling_matches_its_description(routing_schemas):
    """64 exclusions x 8 reasons x 200 characters = 102,400, as described."""
    worst = {
        "decision_id": "dec_worst_case",
        "root_session_hash": "0" * 64,
        "lane_id": "lane_test_main",
        "cache_epoch": 0,
        "turn_id": 0,
        "mode": "balanced",
        "requirements": {},
        "excluded": [
            {"candidate": f"cand-{i}", "reasons": ["x" * 200] * 8} for i in range(64)
        ],
        "ranked": [],
        "selected": None,
        "reason_codes": ["HARD_REQUIREMENT"],
    }
    validate(worst, ROUTE_DECISION_ID, routing_schemas)

    free_text = sum(len(r) for e in worst["excluded"] for r in e["reasons"])
    assert free_text == 102400, free_text
    assert "102,400 characters" in (
        routing_schemas[ROUTE_DECISION_ID]["properties"]["excluded"]["items"][
            "properties"
        ]["reasons"]["description"]
    )


# --------------------------------------------------------------------------
# Description honesty
# --------------------------------------------------------------------------

#: Phrasings that claim a value constraint establishes something only the
#: write path can. Each was present in a shipped description and each was
#: false: a bounded field still holds whatever a writer puts in it.
_OVERCLAIMS = (
    "cannot round-trip",
    "cannot hold",
    "cannot be written here",
    "can never be written",
    "has room to carry",
    "structural guarantee",
    "impossible by design",
    "becomes structural rather than advisory",
    "never carries prompt text",
    "never contains a credential",
    "under any name, spelling, or value",
)


@pytest.mark.parametrize("schema_key", ["ROUTE_DECISION", "OUTCOME_EVENT", "METADATA"])
def test_descriptions_make_no_absolute_containment_claim(schema_key, routing_schemas):
    """Descriptions must not promise more than the schema delivers.

    Two review cycles running, the recurring defect was a description asserting
    that a value constraint made content-carrying impossible. It does not:
    ``additionalProperties: false`` plus value bounds constrain the SHAPE of
    what can be written, never the INTENT. A 128-character identifier holds a
    40-character credential; a 200-character diagnostic holds 200 characters of
    prompt. Content exclusion is a write-path invariant landing in Phase 8, and
    these descriptions must say so rather than claim the schema settles it.
    """
    schema_id = {
        "ROUTE_DECISION": ROUTE_DECISION_ID,
        "OUTCOME_EVENT": OUTCOME_EVENT_ID,
        "METADATA": METADATA_ID,
    }[schema_key]

    def walk(node, path="$"):
        if isinstance(node, dict):
            description = node.get("description")
            if isinstance(description, str):
                lowered = description.lower()
                for claim in _OVERCLAIMS:
                    assert claim not in lowered, f"{path}: {claim!r}"
            for name, child in node.items():
                walk(child, f"{path}.{name}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(routing_schemas[schema_id])


@pytest.mark.parametrize(
    ("schema_key", "required_phrases"),
    [
        ("ROUTE_DECISION", ("write-path invariant", "phase 8", "shape")),
        ("OUTCOME_EVENT", ("write-path invariant", "phase 8", "shape")),
    ],
)
def test_descriptions_name_the_write_path_owner(
    schema_key, required_phrases, routing_schemas
):
    """Removing the false claim is only half the fix.

    A description that simply goes quiet about content exclusion leaves the next
    reader to assume the schema handles it. Each must state that value bounds
    constrain shape rather than intent, and name Phase 8's write path as where
    the exclusion actually lives.
    """
    schema_id = {
        "ROUTE_DECISION": ROUTE_DECISION_ID,
        "OUTCOME_EVENT": OUTCOME_EVENT_ID,
    }[schema_key]
    description = routing_schemas[schema_id]["description"].lower()

    for phrase in required_phrases:
        assert phrase in description, phrase
