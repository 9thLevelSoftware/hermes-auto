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
import json
import pathlib

import jsonschema
import pytest

from hermes_auto.gateway.schemas import SCHEMA_ROOT, load_schemas, validate

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
    assert len(schemas) == 4, sorted(schemas)
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
        "decision_id": "dec_testEmptyRanked",
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
        "decision_id": "dec_testNoReason",
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

    # The top-level description must not claim the record carries "only
    # identifiers, numeric scores, and machine-readable reason codes" while a
    # bounded free-text field exists.
    description = routing_schemas[ROUTE_DECISION_ID]["description"]
    assert "only identifiers, numeric scores, and machine-readable reason codes" not in (
        description
    )
    assert "free-text" in description


def test_unknown_domain_tag_on_card_fails(fixture_dir, routing_schemas):
    """A misspelled affinity fails loudly instead of silently scoring zero."""
    instance = copy.deepcopy(_load(fixture_dir, "valid-model-card.json"))
    instance["affinities"]["backend_aplication"] = 0.9

    with pytest.raises(jsonschema.ValidationError):
        validate(instance, MODEL_CARD_ID, routing_schemas)
