"""The continuation ledger against a fake Postgres: which attempt may run, what it resumes from, what it commits.

Every test here drives `PostgresContinuationStore` over `FakeDatabase`, so what is asserted is the
statements it ran, the order it ran them in, the transaction each one sat inside, and the answer it
gave for a scripted set of rows.
"""

from __future__ import annotations

import json

from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from pydantic_ai.messages import ModelMessagesTypeAdapter

from core.ai.artifacts import artifact_digest
from core.ai.history.contracts import (
    ContinuationAttempt,
    ContinuationConflictError,
    ContinuationInFlightError,
    ContinuationPayloadTooLargeError,
    ContinuationRef,
    ContinuationUnsafeReplayError,
    PostgresContinuationStore,
)
from core.ai.history.domain import CONTINUATION_OUTPUT_BYTES, CONTINUATION_REF_BYTES
from core.ai.history.infrastructure.checkpoints import checkpoint_digest
from core.ai.history.infrastructure.queries import (
    ADVANCE_CONTINUATION_HEAD,
    COMPLETE_CONTINUATION_ATTEMPT,
    LOCK_CONTINUATION,
    OPEN_CONTINUATION_HEAD,
    SELECT_CONTINUATION_ATTEMPT,
    SELECT_CONTINUATION_HEAD,
    SELECT_STARTED_CONTINUATION_ATTEMPTS,
    SELECT_STEP_AT_SEQ,
    START_CONTINUATION_ATTEMPT,
)
from tests.support.fakes.database import FakeDatabase
from tests.unit.history.conftest import turn_messages

if TYPE_CHECKING:
    from pydantic import JsonValue

AT = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
ITEM_ONE = UUID("11111111-1111-4111-8111-111111111111")
ITEM_TWO = UUID("22222222-2222-4222-8222-222222222222")
ATTEMPT_ONE = "a" * 64
ATTEMPT_TWO = "b" * 64
RUN_ONE = "run-one"
RUN_TWO = "run-two"
NO_ROWS: list[dict[str, object]] = []

MESSAGES = turn_messages()
MESSAGE_FORMS = ModelMessagesTypeAdapter.dump_python(MESSAGES, mode="json")
CHECKPOINT = checkpoint_digest(MESSAGES)
EMPTY_CHECKPOINT = checkpoint_digest([])


def an_attempt(
    *, conversation_id: UUID = ITEM_ONE, attempt_id: str = ATTEMPT_ONE, expected_revision: int = 0, run_id: str = RUN_ONE
) -> ContinuationAttempt:
    return ContinuationAttempt(
        conversation_id=conversation_id, attempt_id=attempt_id, expected_revision=expected_revision, run_id=run_id
    )


def started_row(
    *, conversation_id: UUID = ITEM_ONE, attempt_id: str = ATTEMPT_ONE, expected_revision: int = 0, run_id: str = RUN_ONE
) -> dict[str, object]:
    """One `continuation_attempts` row in the shape the check constraint allows a started attempt."""
    return {
        "conversation_id": conversation_id,
        "attempt_id": attempt_id,
        "expected_revision": expected_revision,
        "status": "started",
        "run_id": run_id,
        "snapshot_seq": None,
        "checkpoint_digest": None,
        "output": None,
        "output_digest": None,
        "started_at": AT,
        "completed_at": None,
    }


def completed_row(
    *,
    conversation_id: UUID = ITEM_ONE,
    attempt_id: str = ATTEMPT_ONE,
    expected_revision: int = 0,
    run_id: str = RUN_ONE,
    snapshot_seq: int = 3,
    output: object = None,
    output_digest: str | None = None,
) -> dict[str, object]:
    """One `continuation_attempts` row in the shape the check constraint allows a completed attempt."""
    answer = {"note": "worth a look"} if output is None else output
    document = json.dumps(answer, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "conversation_id": conversation_id,
        "attempt_id": attempt_id,
        "expected_revision": expected_revision,
        "status": "completed",
        "run_id": run_id,
        "snapshot_seq": snapshot_seq,
        "checkpoint_digest": CHECKPOINT,
        "output": answer,
        "output_digest": output_digest if output_digest is not None else _digest(document),
        "started_at": AT,
        "completed_at": AT,
    }


def _digest(document: str) -> str:
    return sha256(document.encode()).hexdigest()


def head_row(*, conversation_id: UUID = ITEM_ONE, revision: int = 1, attempt_id: str = ATTEMPT_ONE) -> dict[str, object]:
    return {"conversation_id": conversation_id, "revision": revision, "attempt_id": attempt_id}


def run_row(*, run_id: str = RUN_ONE, conversation_id: UUID = ITEM_ONE) -> dict[str, object]:
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "parent_run_id": None,
        "agent_name": None,
        "started_at": AT,
        "metadata": {},
        "registration_id": None,
    }


def step_row(*, seq: int = 3, state: str = "complete", entries: list[int] | None = None) -> dict[str, object]:
    """One `steps` row. `entries` names one message per triple, and the default names all four of the worked example."""
    named = entries if entries is not None else [ordinal for index in range(len(MESSAGES)) for ordinal in (index, 0, 0)]
    return {"seq": seq, "step_index": 1, "at": AT, "state": state, "entries": named}


def message_rows() -> list[dict[str, object]]:
    """The four pieces `SELECT_STEP_MESSAGES` answers with for the worked example's step."""
    return [
        {"ordinal": index, "form": 0, "version": 0, "content": form, "stamps": {}} for index, form in enumerate(MESSAGE_FORMS)
    ]


def a_ref(*, conversation_id: UUID = ITEM_ONE, revision: int = 1, run_id: str = RUN_ONE, seq: int = 3, digest: str = CHECKPOINT) -> ContinuationRef:
    return ContinuationRef(
        conversation_id=conversation_id, revision=revision, run_id=run_id, snapshot_seq=seq, checkpoint_digest=digest
    )


def statements_of(database: FakeDatabase) -> list[str]:
    return database.opened.statements()


async def test_beginning_with_no_head_at_all_inserts_one_started_attempt() -> None:
    database = FakeDatabase([NO_ROWS, NO_ROWS, NO_ROWS])
    store = PostgresContinuationStore(database)

    assert await store.begin_attempt(an_attempt()) is None

    ran = statements_of(database)
    assert ran == [
        LOCK_CONTINUATION,
        SELECT_CONTINUATION_ATTEMPT,
        SELECT_STARTED_CONTINUATION_ATTEMPTS,
        SELECT_CONTINUATION_HEAD,
        START_CONTINUATION_ATTEMPT,
    ]
    assert database.opened.executed[0][1] == (str(ITEM_ONE),), "the lock names the conversation, nothing wider"
    started = database.opened.executed[-1][1]
    assert started[:4] == (ITEM_ONE, ATTEMPT_ONE, 0, RUN_ONE), "the deterministic run id is kept before the run is registered"


async def test_beginning_from_the_matching_revision_is_admitted() -> None:
    database = FakeDatabase([NO_ROWS, NO_ROWS, [head_row(revision=4)]])
    store = PostgresContinuationStore(database)

    assert await store.begin_attempt(an_attempt(attempt_id=ATTEMPT_TWO, expected_revision=4, run_id=RUN_TWO)) is None

    assert START_CONTINUATION_ATTEMPT in statements_of(database)


async def test_beginning_from_a_stale_predecessor_refuses() -> None:
    database = FakeDatabase([NO_ROWS, NO_ROWS, [head_row(revision=7)]])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationConflictError) as refusal:
        await store.begin_attempt(an_attempt(attempt_id=ATTEMPT_TWO, expected_revision=4))

    assert "7" in str(refusal.value)
    assert "4" in str(refusal.value)
    assert START_CONTINUATION_ATTEMPT not in statements_of(database)


async def test_a_completed_duplicate_hands_back_the_stored_output_and_ref() -> None:
    database = FakeDatabase([[completed_row()]])
    store = PostgresContinuationStore(database)

    commit = await store.begin_attempt(an_attempt())

    assert commit is not None
    assert commit.output == {"note": "worth a look"}
    assert commit.ref == a_ref(revision=1)
    assert SELECT_STARTED_CONTINUATION_ATTEMPTS not in statements_of(database), "a settled attempt needs nothing else read"


async def test_a_completed_duplicate_under_another_run_id_refuses() -> None:
    database = FakeDatabase([[completed_row(run_id="run-somebody-else")]])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationConflictError) as refusal:
        await store.begin_attempt(an_attempt())

    assert "run-somebody-else" in str(refusal.value)


async def test_a_started_duplicate_refuses_automatic_replay() -> None:
    database = FakeDatabase([[started_row()]])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationUnsafeReplayError) as refusal:
        await store.begin_attempt(an_attempt())

    assert ATTEMPT_ONE in str(refusal.value)
    assert START_CONTINUATION_ATTEMPT not in statements_of(database)


async def test_another_unfinished_attempt_on_the_same_conversation_refuses_before_the_head_is_read() -> None:
    database = FakeDatabase([NO_ROWS, [started_row(attempt_id=ATTEMPT_TWO, run_id=RUN_TWO)]])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationInFlightError) as refusal:
        await store.begin_attempt(an_attempt())

    assert ATTEMPT_TWO in str(refusal.value), "the refusal names the attempt still in flight"
    assert RUN_TWO in str(refusal.value), "and the run it belongs to"
    ran = statements_of(database)
    assert SELECT_CONTINUATION_HEAD not in ran, "nothing is read or advanced past an unfinished attempt"
    assert START_CONTINUATION_ATTEMPT not in ran


async def test_begin_runs_in_one_short_transaction_holding_the_lock_and_the_records() -> None:
    database = FakeDatabase([NO_ROWS, NO_ROWS, NO_ROWS])
    store = PostgresContinuationStore(database)

    await store.begin_attempt(an_attempt())

    assert database.opened.transactions == [(0, 5)], "one transaction, opened at the lock and closed past the insert"


async def test_reading_the_checkpoint_addresses_the_exact_run_and_sequence() -> None:
    database = FakeDatabase([[run_row()], [step_row(seq=3)], message_rows(), NO_ROWS])
    store = PostgresContinuationStore(database)

    snapshot = await store.read_checkpoint(a_ref(seq=3))

    assert snapshot.messages == MESSAGES
    step_read = next(args for statement, args in database.opened.executed if statement == SELECT_STEP_AT_SEQ)
    assert step_read == (RUN_ONE, 3), "the step is addressed by run and sequence, never by step index or latest"


async def test_a_checkpoint_of_another_conversation_refuses() -> None:
    database = FakeDatabase([[run_row(conversation_id=ITEM_TWO)]])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationUnsafeReplayError) as refusal:
        await store.read_checkpoint(a_ref())

    assert str(ITEM_TWO) in str(refusal.value)


async def test_a_sequence_the_run_never_wrote_refuses() -> None:
    database = FakeDatabase([[run_row()], NO_ROWS])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationUnsafeReplayError) as refusal:
        await store.read_checkpoint(a_ref(seq=9))

    assert "9" in str(refusal.value)


async def test_an_interrupted_checkpoint_refuses() -> None:
    database = FakeDatabase([[run_row()], [step_row(state="interrupted")]])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationUnsafeReplayError) as refusal:
        await store.read_checkpoint(a_ref())

    assert "interrupted" in str(refusal.value)


async def test_a_checkpoint_digest_that_disagrees_refuses() -> None:
    database = FakeDatabase([[run_row()], [step_row()], message_rows()])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationUnsafeReplayError) as refusal:
        await store.read_checkpoint(a_ref(digest="c" * 64))

    assert CHECKPOINT in str(refusal.value)


async def test_an_unresolved_tool_effect_refuses_the_read() -> None:
    effect = {
        "run_id": RUN_ONE,
        "tool_call_id": "call_1",
        "tool_name": "lookup_city",
        "status": "started",
        "started_at": AT,
        "ended_at": None,
        "idempotency_key": None,
        "effect_summary": None,
    }
    database = FakeDatabase([[run_row()], [step_row()], message_rows(), [effect]])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationUnsafeReplayError) as refusal:
        await store.read_checkpoint(a_ref())

    assert "call_1" in str(refusal.value)
    assert "exactly once" not in str(refusal.value)


async def test_committing_completes_the_attempt_and_advances_the_head_by_one() -> None:
    database = FakeDatabase(
        [
            [started_row(expected_revision=1)],
            [head_row(revision=1)],
            [run_row()],
            [step_row()],
            message_rows(),
            NO_ROWS,
            [{"attempt_id": ATTEMPT_ONE}],
            [{"revision": 2}],
        ]
    )
    store = PostgresContinuationStore(database)

    commit = await store.commit_attempt(
        an_attempt(expected_revision=1), snapshot_seq=3, checkpoint_digest=CHECKPOINT, output={"note": "worth a look"}
    )

    assert commit.ref == a_ref(revision=2)
    assert commit.output == {"note": "worth a look"}
    ran = statements_of(database)
    assert ran[0] == LOCK_CONTINUATION
    assert COMPLETE_CONTINUATION_ATTEMPT in ran
    assert ADVANCE_CONTINUATION_HEAD in ran
    advanced = next(args for statement, args in database.opened.executed if statement == ADVANCE_CONTINUATION_HEAD)
    assert advanced == (ITEM_ONE, 1, 2, ATTEMPT_ONE), "the head moves from its predecessor revision to exactly one past it"
    assert database.opened.transactions == [(0, len(ran))], "one transaction around the whole commit"


async def test_the_first_commit_of_a_conversation_opens_its_head() -> None:
    database = FakeDatabase(
        [
            [started_row()],
            NO_ROWS,
            [run_row()],
            [step_row()],
            message_rows(),
            NO_ROWS,
            [{"attempt_id": ATTEMPT_ONE}],
            [{"revision": 1}],
        ]
    )
    store = PostgresContinuationStore(database)

    commit = await store.commit_attempt(an_attempt(), snapshot_seq=3, checkpoint_digest=CHECKPOINT, output={"note": "a"})

    assert commit.ref.revision == 1
    assert OPEN_CONTINUATION_HEAD in statements_of(database)


async def test_a_head_that_moved_under_the_commit_refuses() -> None:
    database = FakeDatabase(
        [
            [started_row(expected_revision=1)],
            [head_row(revision=1)],
            [run_row()],
            [step_row()],
            message_rows(),
            NO_ROWS,
            [{"attempt_id": ATTEMPT_ONE}],
            NO_ROWS,
        ]
    )
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationConflictError) as refusal:
        await store.commit_attempt(
            an_attempt(expected_revision=1), snapshot_seq=3, checkpoint_digest=CHECKPOINT, output={"note": "a"}
        )

    assert "head" in str(refusal.value)


async def test_committing_an_already_completed_attempt_hands_the_stored_output_back() -> None:
    database = FakeDatabase([[completed_row()]])
    store = PostgresContinuationStore(database)

    commit = await store.commit_attempt(an_attempt(), snapshot_seq=3, checkpoint_digest=CHECKPOINT, output={"note": "worth a look"})

    assert commit.output == {"note": "worth a look"}
    assert commit.ref == a_ref(revision=1)
    assert COMPLETE_CONTINUATION_ATTEMPT not in statements_of(database), "a settled attempt is never written twice"


async def test_committing_a_different_output_over_a_completed_attempt_refuses() -> None:
    database = FakeDatabase([[completed_row()]])
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationConflictError) as refusal:
        await store.commit_attempt(an_attempt(), snapshot_seq=3, checkpoint_digest=CHECKPOINT, output={"note": "something else"})

    assert "digest" in str(refusal.value)


async def test_an_output_past_the_ceiling_refuses_before_any_statement_runs() -> None:
    database = FakeDatabase([])
    store = PostgresContinuationStore(database)
    oversize: JsonValue = {"text": "x" * (CONTINUATION_OUTPUT_BYTES + 1)}

    with pytest.raises(ContinuationPayloadTooLargeError) as refusal:
        await store.commit_attempt(an_attempt(), snapshot_seq=3, checkpoint_digest=CHECKPOINT, output=oversize)

    assert str(CONTINUATION_OUTPUT_BYTES) in str(refusal.value)
    assert statements_of(database) == [], "the ceiling is checked before the database is touched at all"


async def test_a_ref_past_the_ceiling_refuses() -> None:
    long_run = "run-" + "z" * CONTINUATION_REF_BYTES
    database = FakeDatabase(
        [
            [started_row(run_id=long_run)],
            NO_ROWS,
            [run_row(run_id=long_run)],
            [step_row()],
            message_rows(),
            NO_ROWS,
            [{"attempt_id": ATTEMPT_ONE}],
            [{"revision": 1}],
        ]
    )
    store = PostgresContinuationStore(database)

    with pytest.raises(ContinuationPayloadTooLargeError) as refusal:
        await store.commit_attempt(
            an_attempt(run_id=long_run), snapshot_seq=3, checkpoint_digest=CHECKPOINT, output={"note": "a"}
        )

    assert str(CONTINUATION_REF_BYTES) in str(refusal.value)


async def test_two_map_items_advance_their_own_conversations_independently() -> None:
    first = FakeDatabase([NO_ROWS, NO_ROWS, [head_row(conversation_id=ITEM_ONE, revision=2)]])
    second = FakeDatabase([NO_ROWS, NO_ROWS, NO_ROWS])

    assert await PostgresContinuationStore(first).begin_attempt(an_attempt(expected_revision=2)) is None
    assert await PostgresContinuationStore(second).begin_attempt(an_attempt(conversation_id=ITEM_TWO, attempt_id=ATTEMPT_TWO)) is None

    assert first.opened.executed[0][1] == (str(ITEM_ONE),)
    assert second.opened.executed[0][1] == (str(ITEM_TWO),), "each item's line takes its own lock"
    assert first.opened.executed[-1][1][2] == 2
    assert second.opened.executed[-1][1][2] == 0, "one item's revision says nothing about another's"


async def test_the_empty_checkpoint_digest_is_stable() -> None:
    assert checkpoint_digest([]) == EMPTY_CHECKPOINT
    assert checkpoint_digest(MESSAGES) != EMPTY_CHECKPOINT


async def test_the_checkpoint_digest_is_the_artifact_digest_of_the_json_mode_messages() -> None:
    assert checkpoint_digest(MESSAGES) == artifact_digest(ModelMessagesTypeAdapter.dump_python(list(MESSAGES), mode="json"))
