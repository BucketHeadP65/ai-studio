-- The inspection's own tables. Unqualified: the application decides the schema through the connection's search path.

CREATE TABLE IF NOT EXISTS cursors (
    consumer        text PRIMARY KEY,
    position        bigint NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id         bigserial PRIMARY KEY,
    source          text NOT NULL,
    watch           text NOT NULL,
    symptom         text NOT NULL,
    spans           jsonb NOT NULL,
    spans_digest    text NOT NULL,
    found_by        text NOT NULL,
    seen_at         timestamptz NOT NULL DEFAULT now(),
    attempted_at    timestamptz,
    refusal         text,
    resolution      text NOT NULL DEFAULT 'open' CHECK (resolution IN ('open', 'undecided', 'invalid', 'dismissed')),
    UNIQUE (source, symptom, spans_digest)
);

CREATE INDEX IF NOT EXISTS leads_by_source ON leads (source);

CREATE TABLE IF NOT EXISTS questions (
    question_id     bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL UNIQUE,
    kind            text NOT NULL,
    source          text NOT NULL,
    watch           text NOT NULL,
    lead_id         bigint REFERENCES leads (lead_id) ON DELETE SET NULL,
    tier            text NOT NULL,
    model           text NOT NULL,
    asked_at        timestamptz NOT NULL DEFAULT now(),
    answer          text
);

CREATE INDEX IF NOT EXISTS questions_by_source ON questions (source);

CREATE TABLE IF NOT EXISTS findings (
    finding_id      bigserial PRIMARY KEY,
    lead_id         bigint UNIQUE REFERENCES leads (lead_id) ON DELETE CASCADE,
    source          text NOT NULL,
    symptom         text NOT NULL,
    spans           jsonb NOT NULL,
    blames          jsonb NOT NULL,
    found_by        text NOT NULL,
    model           text,
    decided_at      timestamptz NOT NULL DEFAULT now(),
    question_id     bigint REFERENCES questions (question_id) ON DELETE SET NULL,
    explanation     text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS findings_by_source ON findings (source);
