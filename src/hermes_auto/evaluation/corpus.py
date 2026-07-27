"""Load the fixed-model baseline task corpus (design.md §15.3).

The corpus is the list of units of work every baseline is measured over. It is
data, not behavior: this module parses and validates a YAML file and returns
frozen records. It performs no routing, no scoring, no candidate selection, and
no model invocation.

Two properties matter downstream and are enforced here rather than trusted:

* **Sorted output.** :func:`load_corpus` returns tasks ordered by ``task_id``,
  so a later reordering of the YAML file cannot change the rendered baseline
  report. Report determinism starts at this function.
* **Unique, stable ids.** ``task_id`` is the join key between the corpus and
  every recorded run. A duplicate is rejected loudly, because a silent duplicate
  would merge two different units of work into one row of every baseline.

Task descriptions are content-free abstractions by contract (see the header
comment of ``tests/fixtures/baseline/corpus.yaml`` and ``docs/privacy.md``); no
prompt text, code, real path, or credential belongs in a corpus file.
"""

from __future__ import annotations

import dataclasses
import pathlib

import yaml

__all__ = [
    "SUPPORTED_CORPUS_VERSION",
    "VALID_CATEGORIES",
    "BaselineTask",
    "CorpusError",
    "load_corpus",
    "require_mapping",
]

#: The only corpus schema version this loader understands. A corpus declaring a
#: different version is rejected rather than best-effort parsed, so a future
#: format change cannot be silently misread as the current one.
SUPPORTED_CORPUS_VERSION = 1

#: The nine workload categories of the design.md §15.3 dynamic execution track.
#: Closed on purpose: an unrecognized category would produce a baseline row that
#: no later phase knows how to compare against.
VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "bug_fixing",
        "feature_implementation",
        "test_repair",
        "refactoring",
        "multi_step_shell",
        "web_research",
        "data_extraction",
        "vision_assisted",
        "context_compression",
    }
)

_REQUIRED_KEYS = (
    "task_id",
    "category",
    "description",
    "expected_tool_calls",
    "verifiable",
)


class CorpusError(Exception):
    """A corpus file, or a recorded run referring to one, is not usable.

    Raised for an unsupported version, a malformed structure, a missing or
    unknown key, a duplicate ``task_id``, a category outside
    :data:`VALID_CATEGORIES`, and (from :mod:`hermes_auto.evaluation.baselines`)
    a recorded run naming a task the corpus does not define.
    """


@dataclasses.dataclass(frozen=True)
class BaselineTask:
    """One unit of work in the baseline corpus.

    Attributes:
        task_id: Stable kebab-case identifier and join key to recorded runs.
        category: One of :data:`VALID_CATEGORIES`.
        description: A content-free abstraction of the work to be done.
        expected_tool_calls: Approximate tool-call count indicating the work was
            actually performed rather than answered from memory; ``None`` when
            no meaningful expectation exists.
        verifiable: ``True`` when success is mechanically checkable (tests, exit
            code, file state) with no judge in the loop.
    """

    task_id: str
    category: str
    description: str
    expected_tool_calls: int | None
    verifiable: bool


def require_mapping(value: object, what: str) -> dict:
    """Return *value* as a mapping, or raise :class:`CorpusError` naming its type.

    Public because :mod:`hermes_auto.evaluation.baselines` guards recorded-run
    structure with the same rule. One helper, one message format: an operator
    reading ``... must be a mapping, found list`` should not have to know whether
    the offending file was a corpus or a recorded run to recognize the shape.
    """
    if not isinstance(value, dict):
        raise CorpusError(f"{what} must be a mapping, found {type(value).__name__}")
    return value


def load_corpus(path: str | pathlib.Path) -> list[BaselineTask]:
    """Parse *path* and return its tasks sorted by ``task_id``.

    Args:
        path: Path to a corpus YAML file.

    Returns:
        The corpus tasks, ordered by ``task_id``. Sorting here is what makes the
        rendered baseline report independent of the order tasks appear in the
        file.

    Raises:
        CorpusError: The file is unreadable or unparseable, declares a version
            other than :data:`SUPPORTED_CORPUS_VERSION`, is structurally wrong,
            omits a required key, carries an unknown key, repeats a ``task_id``,
            or names a category outside :data:`VALID_CATEGORIES`. The message
            always names the offending task id where one is known.
    """
    corpus_path = pathlib.Path(path)

    try:
        text = corpus_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusError(f"{corpus_path}: could not be read: {exc}") from exc

    try:
        # safe_load, never load: a corpus file is untrusted input and full-tag
        # YAML loading can construct arbitrary Python objects.
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CorpusError(f"{corpus_path}: not parseable as YAML: {exc}") from exc

    document = require_mapping(document, f"{corpus_path}: top-level document")

    version = document.get("version")
    if version != SUPPORTED_CORPUS_VERSION:
        raise CorpusError(
            f"{corpus_path}: unsupported corpus version {version!r}; "
            f"expected {SUPPORTED_CORPUS_VERSION}"
        )

    raw_tasks = document.get("tasks")
    if not isinstance(raw_tasks, list):
        raise CorpusError(
            f"{corpus_path}: 'tasks' must be a list, "
            f"found {type(raw_tasks).__name__}"
        )

    tasks: list[BaselineTask] = []
    seen: set[str] = set()

    for position, raw in enumerate(raw_tasks):
        entry = require_mapping(raw, f"{corpus_path}: task at position {position}")

        task_id = entry.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise CorpusError(
                f"{corpus_path}: task at position {position} has a missing or "
                "non-string 'task_id'"
            )

        for key in _REQUIRED_KEYS:
            if key not in entry:
                raise CorpusError(
                    f"{corpus_path}: task {task_id!r} is missing required key {key!r}"
                )

        unknown = sorted(set(entry) - set(_REQUIRED_KEYS))
        if unknown:
            raise CorpusError(
                f"{corpus_path}: task {task_id!r} carries unknown key(s) {unknown}"
            )

        if task_id in seen:
            raise CorpusError(
                f"{corpus_path}: duplicate task_id {task_id!r}; task ids are the "
                "join key to recorded runs and must be unique"
            )
        seen.add(task_id)

        category = entry["category"]
        if category not in VALID_CATEGORIES:
            raise CorpusError(
                f"{corpus_path}: task {task_id!r} has unknown category "
                f"{category!r}; expected one of {sorted(VALID_CATEGORIES)}"
            )

        description = entry["description"]
        if not isinstance(description, str) or not description.strip():
            raise CorpusError(
                f"{corpus_path}: task {task_id!r} has a missing or empty 'description'"
            )

        expected_tool_calls = entry["expected_tool_calls"]
        if expected_tool_calls is not None and (
            isinstance(expected_tool_calls, bool)
            or not isinstance(expected_tool_calls, int)
            or expected_tool_calls < 0
        ):
            raise CorpusError(
                f"{corpus_path}: task {task_id!r} has 'expected_tool_calls' "
                f"{expected_tool_calls!r}; expected a non-negative integer or null"
            )

        verifiable = entry["verifiable"]
        if not isinstance(verifiable, bool):
            raise CorpusError(
                f"{corpus_path}: task {task_id!r} has 'verifiable' "
                f"{verifiable!r}; expected a boolean"
            )

        tasks.append(
            BaselineTask(
                task_id=task_id,
                category=category,
                description=description,
                expected_tool_calls=expected_tool_calls,
                verifiable=verifiable,
            )
        )

    return sorted(tasks, key=lambda task: task.task_id)
