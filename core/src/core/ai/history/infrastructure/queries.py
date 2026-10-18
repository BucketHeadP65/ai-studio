"""Every statement the store runs, one constant per statement."""

from typing import Final, LiteralString

INSERT_RUN: Final[LiteralString] = """
    INSERT INTO runs (run_id, conversation_id, parent_run_id, agent_name, started_at, metadata, registration_id)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
    ON CONFLICT (run_id) DO NOTHING
"""

SELECT_RUN: Final[LiteralString] = """
    SELECT run_id, conversation_id, parent_run_id, agent_name, started_at, metadata, registration_id FROM runs WHERE run_id = $1
"""

SELECT_RUNS: Final[LiteralString] = """
    SELECT run_id, conversation_id, parent_run_id, agent_name, started_at, metadata, registration_id FROM runs
    WHERE ($1::text IS NULL OR parent_run_id = $1)
      AND ($2::uuid IS NULL OR conversation_id = $2)
    ORDER BY started_at
"""

LOCK_RUN: Final[LiteralString] = "SELECT pg_advisory_xact_lock(hashtext($1))"

INSERT_EVENT: Final[LiteralString] = """
    INSERT INTO run_events (run_id, seq, kind, step_index, at, tool_call_id, tool_name, error, metadata)
    SELECT $1, coalesce(max(seq) + 1, 0), $2, $3, $4, $5, $6, $7, $8::jsonb
    FROM run_events WHERE run_id = $1
"""

SELECT_EVENTS: Final[LiteralString] = """
    SELECT run_id, seq, kind, step_index, at, tool_call_id, tool_name, error, metadata
    FROM run_events WHERE run_id = $1 ORDER BY seq
"""

SELECT_EVENTS_AFTER: Final[LiteralString] = """
    SELECT e.id, e.run_id, e.kind, e.step_index, e.at, r.agent_name, r.conversation_id
    FROM run_events AS e
    JOIN runs AS r USING (run_id)
    WHERE e.id > $1 AND e.kind = ANY($2::text[])
    ORDER BY e.id
    LIMIT $3
"""

SELECT_LAST_EVENT_POSITION: Final[LiteralString] = "SELECT coalesce(max(id), 0) AS position FROM run_events"
SELECT_RUNS_ENDED_SINCE: Final[LiteralString] = """
    SELECT e.id, e.run_id, e.kind, e.step_index, e.at, r.agent_name, r.conversation_id
    FROM run_events AS e
    JOIN runs AS r USING (run_id)
    WHERE e.kind = ANY($1::text[]) AND e.at >= $2 AND e.id > $4
    ORDER BY e.id
    LIMIT $3
"""

NEXT_STEP_SEQ: Final[LiteralString] = "SELECT coalesce(max(seq) + 1, 0) AS seq FROM steps WHERE run_id = $1"

INSERT_STEP: Final[LiteralString] = """
    INSERT INTO steps (run_id, seq, step_index, at, state, entries)
    VALUES ($1, $2, $3, $4, $5, $6::integer[])
"""

SELECT_LATEST_STEP: Final[LiteralString] = """
    SELECT seq, step_index, at, state, entries FROM steps WHERE run_id = $1 ORDER BY seq DESC LIMIT 1
"""

SELECT_LATEST_COMPLETE_STEP: Final[LiteralString] = """
    SELECT seq, step_index, at, state, entries FROM steps
    WHERE run_id = $1 AND state = 'complete'
    ORDER BY seq DESC LIMIT 1
"""

SELECT_STEPS: Final[LiteralString] = """
    SELECT seq, step_index, at, state, entries FROM steps WHERE run_id = $1 ORDER BY seq
"""

SELECT_COMPLETE_STEPS: Final[LiteralString] = """
    SELECT seq, step_index, at, state, entries FROM steps
    WHERE run_id = $1 AND state = 'complete'
    ORDER BY seq
"""

SELECT_LATEST_STEP_OF_CONVERSATION: Final[LiteralString] = """
    SELECT steps.run_id, steps.seq, steps.entries FROM steps
    JOIN runs ON runs.run_id = steps.run_id
    WHERE runs.conversation_id = $1 AND runs.parent_run_id IS NULL AND steps.state = 'complete'
    ORDER BY runs.started_at DESC, runs.run_id DESC, steps.seq DESC
    LIMIT 1
"""

KEEP_INSTRUCTIONS: Final[LiteralString] = """
    INSERT INTO instructions (digest, text)
    SELECT digest, text FROM unnest($1::text[], $2::text[]) AS given (digest, text)
    ON CONFLICT (digest) DO NOTHING
"""

INSERT_MESSAGES: Final[LiteralString] = """
    INSERT INTO messages (conversation_id, ordinal, key, kind, first_run_id, first_seq, created_at)
    SELECT $1, ordinal, key, kind, $2, $3, $4
    FROM unnest($5::integer[], $6::text[], $7::text[]) AS given (ordinal, key, kind)
"""

INSERT_MESSAGE_FORMS: Final[LiteralString] = """
    INSERT INTO message_forms (conversation_id, ordinal, form, content, content_digest)
    SELECT $1, ordinal, form, content, content_digest
    FROM unnest($2::integer[], $3::integer[], $4::jsonb[], $5::text[]) AS given (ordinal, form, content, content_digest)
"""

INSERT_MESSAGE_STAMPS: Final[LiteralString] = """
    INSERT INTO message_stamps (conversation_id, ordinal, version, stamps, stamps_digest)
    SELECT $1, ordinal, version, stamps, stamps_digest
    FROM unnest($2::integer[], $3::integer[], $4::jsonb[], $5::text[]) AS given (ordinal, version, stamps, stamps_digest)
"""

UPSERT_TOOL_EFFECT: Final[LiteralString] = """
    INSERT INTO tool_effects (run_id, tool_call_id, tool_name, status, started_at, ended_at, idempotency_key, effect_summary)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    ON CONFLICT (run_id, tool_call_id) DO UPDATE SET
        tool_name = excluded.tool_name,
        status = excluded.status,
        started_at = excluded.started_at,
        ended_at = excluded.ended_at,
        idempotency_key = excluded.idempotency_key,
        effect_summary = excluded.effect_summary
"""

SELECT_TOOL_EFFECT: Final[LiteralString] = """
    SELECT run_id, tool_call_id, tool_name, status, started_at, ended_at, idempotency_key, effect_summary
    FROM tool_effects WHERE run_id = $1 AND tool_call_id = $2
"""

SELECT_STARTED_TOOL_EFFECTS: Final[LiteralString] = """
    SELECT run_id, tool_call_id, tool_name, status, started_at, ended_at, idempotency_key, effect_summary
    FROM tool_effects WHERE run_id = $1 AND status = 'started' ORDER BY started_at
"""

KEEP_REQUEST_PARAMETERS: Final[LiteralString] = """
    INSERT INTO request_parameters (digest, parameters) VALUES ($1, $2::jsonb) ON CONFLICT (digest) DO NOTHING
"""

POINT_STEP_AT_PARAMETERS: Final[LiteralString] = """
    INSERT INTO step_parameters (run_id, step_index, digest)
    VALUES ($1, $2, $3)
    ON CONFLICT (run_id, step_index) DO UPDATE SET digest = excluded.digest
"""

SELECT_STEP_PARAMETERS: Final[LiteralString] = """
    SELECT step_parameters.step_index, request_parameters.parameters
    FROM step_parameters JOIN request_parameters ON request_parameters.digest = step_parameters.digest
    WHERE step_parameters.run_id = $1 ORDER BY step_parameters.step_index
"""

SELECT_CONVERSATION_DIGESTS: Final[LiteralString] = """
    SELECT key,
        coalesce((SELECT array_agg(content_digest ORDER BY form) FROM message_forms
                  WHERE message_forms.conversation_id = messages.conversation_id
                    AND message_forms.ordinal = messages.ordinal), '{}') AS form_digests,
        coalesce((SELECT array_agg(stamps_digest ORDER BY version) FROM message_stamps
                  WHERE message_stamps.conversation_id = messages.conversation_id
                    AND message_stamps.ordinal = messages.ordinal), '{}') AS stamp_digests
    FROM messages WHERE conversation_id = $1 ORDER BY ordinal
"""

SELECT_FIRST_MESSAGES: Final[LiteralString] = """
    SELECT message_forms.content, message_stamps.stamps FROM message_forms
    JOIN message_stamps ON message_stamps.conversation_id = message_forms.conversation_id
      AND message_stamps.ordinal = message_forms.ordinal AND message_stamps.version = 0
    WHERE message_forms.conversation_id = $1 AND message_forms.form = 0
    ORDER BY message_forms.ordinal
"""

SELECT_STEP_MESSAGES: Final[LiteralString] = """
    SELECT wanted.ordinal, wanted.form, wanted.version, message_forms.content, message_stamps.stamps
    FROM unnest($2::integer[], $3::integer[], $4::integer[]) WITH ORDINALITY AS wanted (ordinal, form, version, place)
    JOIN message_forms ON message_forms.conversation_id = $1
      AND message_forms.ordinal = wanted.ordinal AND message_forms.form = wanted.form
    JOIN message_stamps ON message_stamps.conversation_id = $1
      AND message_stamps.ordinal = wanted.ordinal AND message_stamps.version = wanted.version
    ORDER BY wanted.place
"""

SELECT_INSTRUCTIONS: Final[LiteralString] = "SELECT digest, text FROM instructions WHERE digest = ANY($1::text[])"

DELETE_CONVERSATION_RUNS: Final[LiteralString] = "DELETE FROM runs WHERE conversation_id = $1"

DELETE_CONVERSATION_MESSAGES: Final[LiteralString] = "DELETE FROM messages WHERE conversation_id = $1"

SELECT_STEP_AT_SEQ: Final[LiteralString] = """
    SELECT seq, step_index, at, state, entries FROM steps WHERE run_id = $1 AND seq = $2
"""

LOCK_CONTINUATION: Final[LiteralString] = "SELECT pg_advisory_xact_lock(hashtext($1))"

SELECT_CONTINUATION_ATTEMPT: Final[LiteralString] = """
    SELECT conversation_id, attempt_id, expected_revision, status, run_id, snapshot_seq, checkpoint_digest,
           output, output_digest, started_at, completed_at
    FROM continuation_attempts WHERE conversation_id = $1 AND attempt_id = $2
"""

SELECT_STARTED_CONTINUATION_ATTEMPTS: Final[LiteralString] = """
    SELECT conversation_id, attempt_id, expected_revision, status, run_id, snapshot_seq, checkpoint_digest,
           output, output_digest, started_at, completed_at
    FROM continuation_attempts WHERE conversation_id = $1 AND status = 'started' ORDER BY started_at, attempt_id
"""

SELECT_CONTINUATION_HEAD: Final[LiteralString] = """
    SELECT conversation_id, revision, attempt_id FROM continuation_heads WHERE conversation_id = $1
"""

START_CONTINUATION_ATTEMPT: Final[LiteralString] = """
    INSERT INTO continuation_attempts (conversation_id, attempt_id, expected_revision, status, run_id, started_at)
    VALUES ($1, $2, $3, 'started', $4, $5)
"""

COMPLETE_CONTINUATION_ATTEMPT: Final[LiteralString] = """
    UPDATE continuation_attempts
    SET status = 'completed', snapshot_seq = $3, checkpoint_digest = $4,
        output = $5::jsonb, output_digest = $6, completed_at = $7
    WHERE conversation_id = $1 AND attempt_id = $2 AND status = 'started'
    RETURNING attempt_id
"""

OPEN_CONTINUATION_HEAD: Final[LiteralString] = """
    INSERT INTO continuation_heads (conversation_id, revision, attempt_id) VALUES ($1, 1, $2)
    ON CONFLICT (conversation_id) DO NOTHING
    RETURNING revision
"""

ADVANCE_CONTINUATION_HEAD: Final[LiteralString] = """
    UPDATE continuation_heads SET revision = $3, attempt_id = $4
    WHERE conversation_id = $1 AND revision = $2
    RETURNING revision
"""

DELETE_CONVERSATION_CONTINUATION_HEADS: Final[LiteralString] = "DELETE FROM continuation_heads WHERE conversation_id = $1"

DELETE_CONVERSATION_CONTINUATION_ATTEMPTS: Final[LiteralString] = "DELETE FROM continuation_attempts WHERE conversation_id = $1"
