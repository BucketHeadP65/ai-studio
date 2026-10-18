"""A claim's declared text fields live in one `kb.claims.fields` document, and `api.claims` serves every declared field.

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-12 00:00:00.000000

`kb.claims` gains `fields jsonb NOT NULL DEFAULT '{}'`, filled from the non-null values of `condition`, `baseline`,
`magnitude` and `eval_setting`, each keyed by its column name, and those four columns are dropped. `api.claims` loses
`dataset_name`, `task_name`, `metric_name`, `condition`, `magnitude` and `eval_setting` and gains `fields` (the text
fields) and `links` (each link field mapped to the titles of its target pages, in `ord` order, read from
`kb.claim_qualifiers`). `api.contradictions` serves `fields_a` and `fields_b` in place of `condition_a` and
`condition_b`. A view cannot drop a column in place, so both are dropped and created again, with their comments and
the reader's grant. Downgrade restores `condition`, `baseline`, `magnitude` and `eval_setting` from `fields`, and every
other key in `fields` is lost. It then puts both views, their comments and their grants back as they stood at 0063.
"""

from __future__ import annotations

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_VIEWS = ("api.claims", "api.contradictions")

_BACKFILL = """
UPDATE kb.claims
   SET fields = jsonb_strip_nulls(jsonb_build_object(
         'condition', condition, 'baseline', baseline, 'magnitude', magnitude, 'eval_setting', eval_setting))
 WHERE condition IS NOT NULL OR baseline IS NOT NULL OR magnitude IS NOT NULL OR eval_setting IS NOT NULL
"""

_DROP_TYPED_COLUMNS = "ALTER TABLE kb.claims DROP COLUMN condition, DROP COLUMN baseline, DROP COLUMN magnitude, DROP COLUMN eval_setting"

_ADD_TYPED_COLUMNS = (
    "ALTER TABLE kb.claims ADD COLUMN condition text, ADD COLUMN baseline text, ADD COLUMN magnitude text, ADD COLUMN eval_setting text"
)

_RESTORE = """
UPDATE kb.claims
   SET condition = fields ->> 'condition', baseline = fields ->> 'baseline', magnitude = fields ->> 'magnitude',
       eval_setting = fields ->> 'eval_setting'
 WHERE fields <> '{}'::jsonb
"""

_CLAIMS = """
CREATE VIEW api.claims AS
 SELECT c.id AS claim_id,
    c.collection,
    c.page_id,
    p.title AS page_title,
    subj.title AS subject_name,
    c.text,
    c.quote,
    c.loc,
    c.kind,
    c.fields,
    COALESCE(( SELECT jsonb_object_agg(targets.qualifier, targets.titles) AS jsonb_object_agg
           FROM ( SELECT q.qualifier,
                    jsonb_agg(t.title ORDER BY q.ord) AS titles
                   FROM (kb.claim_qualifiers q
                     JOIN kb.pages t ON (((t.collection = q.target_collection) AND (t.id = q.target_id) AND api.visible_collection(t.collection))))
                  WHERE ((q.collection = c.collection) AND (q.claim_id = c.id))
                  GROUP BY q.qualifier) targets), '{}'::jsonb) AS links,
    c.status
   FROM ((kb.claims c
     JOIN kb.pages p ON (((p.collection = c.collection) AND (p.id = c.page_id))))
     LEFT JOIN kb.pages subj ON (((subj.collection = c.subject_collection) AND (subj.id = c.subject_id) AND api.visible_collection(subj.collection))))
  WHERE api.visible_collection(c.collection)
"""

_CONTRADICTIONS = """
CREATE VIEW api.contradictions AS
 SELECT a.id AS claim_a,
    b.id AS claim_b,
    a.subject_id,
    subj.title AS subject_name,
    a.text AS text_a,
    b.text AS text_b,
    a.quote AS quote_a,
    b.quote AS quote_b,
    a.fields AS fields_a,
    b.fields AS fields_b
   FROM ((kb.claims a
     JOIN kb.claims b ON (((a.subject_id = b.subject_id) AND (a.id < b.id))))
     LEFT JOIN kb.pages subj ON (((subj.id = a.subject_id) AND api.visible_collection(subj.collection))))
  WHERE ((a.status = 'disputed'::text) AND (b.status = 'disputed'::text) AND api.visible_collection(a.collection) AND api.visible_collection(b.collection))
"""

# One entry per comment the two views carry: the COMMENT ON target and its text.
_COMMENTS: tuple[tuple[str, str], ...] = (
    (
        "VIEW api.claims",
        (
            "Joined, human-readable claims: owning page title, subject title, each link field's target page TITLES (never "
            "bare ids) and each text field, plus quote+loc for L4 verification. Example: SELECT page_title, text, quote, "
            "loc FROM api.claims WHERE claim_id = '<page-id>#c1';"
        ),
    ),
    ("COLUMN api.claims.claim_id", "Claim id '<page_id>#cN', unique only paired with collection. Example value: '<page-id>#c1'."),
    ("COLUMN api.claims.collection", "This claim's own collection (its owning page's collection). Example value: '<collection>'."),
    ("COLUMN api.claims.page_id", "Owning page id."),
    ("COLUMN api.claims.page_title", "Owning page title."),
    ("COLUMN api.claims.subject_name", "Title of the page the claim is about."),
    ("COLUMN api.claims.text", "The atomic claim sentence."),
    ("COLUMN api.claims.quote", "Verbatim source snippet supporting the claim (may be NULL)."),
    ("COLUMN api.claims.loc", "Raw locator into the source. Example value: '\u00a75.1, Table 2'."),
    ("COLUMN api.claims.kind", "Claim type (vocabulary)."),
    (
        "COLUMN api.claims.fields",
        (
            "jsonb: the claim's text fields, each declared by the knowledge base's claim grammar, keyed by field name with "
            "the value as written. '{}' when the claim carries none. Example: SELECT claim_id, text FROM api.claims WHERE "
            "fields->>'<field>' = '<value>';"
        ),
    ),
    (
        "COLUMN api.claims.links",
        (
            "jsonb: the claim's link fields, each declared by the knowledge base's claim grammar, keyed by field name, each "
            "an array of its target pages' titles in written order. A target that resolved to no page is left out. '{}' "
            "when the claim carries none. Example: SELECT claim_id, text FROM api.claims WHERE links->'<field>' ? "
            "'<page-title>';"
        ),
    ),
    ("COLUMN api.claims.status", "Claim status: active|disputed|superseded|retracted-source."),
    (
        "VIEW api.contradictions",
        (
            "Disputed claim pairs about the same subject, with both quotes and both claims' text fields. Example: SELECT "
            "subject_name, text_a, text_b FROM api.contradictions LIMIT 20;"
        ),
    ),
    ("COLUMN api.contradictions.claim_a", "First claim id of the disputed pair."),
    ("COLUMN api.contradictions.claim_b", "Second claim id of the disputed pair."),
    ("COLUMN api.contradictions.subject_id", "Shared subject page id."),
    ("COLUMN api.contradictions.subject_name", "Shared subject name."),
    ("COLUMN api.contradictions.text_a", "Claim A sentence."),
    ("COLUMN api.contradictions.text_b", "Claim B sentence."),
    ("COLUMN api.contradictions.quote_a", "Claim A source quote."),
    ("COLUMN api.contradictions.quote_b", "Claim B source quote."),
    ("COLUMN api.contradictions.fields_a", "jsonb: claim A's text fields keyed by field name, as api.claims.fields holds them."),
    ("COLUMN api.contradictions.fields_b", "jsonb: claim B's text fields keyed by field name, as api.claims.fields holds them."),
)

# The two views as they stood at 0063, in the order the downgrade creates them.

_PREVIOUS_VIEWS = (
    """
CREATE VIEW api.claims AS
 SELECT c.id AS claim_id,
    c.collection,
    c.page_id,
    p.title AS page_title,
    subj.title AS subject_name,
    ds.title AS dataset_name,
    tk.title AS task_name,
    mt.title AS metric_name,
    c.text,
    c.quote,
    c.loc,
    c.kind,
    c.condition,
    c.magnitude,
    c.eval_setting,
    c.status
   FROM ((((((((kb.claims c
     JOIN kb.pages p ON (((p.collection = c.collection) AND (p.id = c.page_id))))
     LEFT JOIN kb.pages subj ON (((subj.collection = c.subject_collection) AND (subj.id = c.subject_id) AND api.visible_collection(subj.collection))))
     LEFT JOIN kb.claim_qualifiers qds ON (((qds.collection = c.collection) AND (qds.claim_id = c.id) AND (qds.qualifier = 'on_dataset'::text) AND (qds.ord = 0))))
     LEFT JOIN kb.pages ds ON (((ds.collection = qds.target_collection) AND (ds.id = qds.target_id) AND api.visible_collection(ds.collection))))
     LEFT JOIN kb.claim_qualifiers qtk ON (((qtk.collection = c.collection) AND (qtk.claim_id = c.id) AND (qtk.qualifier = 'on_task'::text) AND (qtk.ord = 0))))
     LEFT JOIN kb.pages tk ON (((tk.collection = qtk.target_collection) AND (tk.id = qtk.target_id) AND api.visible_collection(tk.collection))))
     LEFT JOIN kb.claim_qualifiers qmt ON (((qmt.collection = c.collection) AND (qmt.claim_id = c.id) AND (qmt.qualifier = 'metric'::text) AND (qmt.ord = 0))))
     LEFT JOIN kb.pages mt ON (((mt.collection = qmt.target_collection) AND (mt.id = qmt.target_id) AND api.visible_collection(mt.collection))))
  WHERE api.visible_collection(c.collection)
""",
    """
CREATE VIEW api.contradictions AS
 SELECT a.id AS claim_a,
    b.id AS claim_b,
    a.subject_id,
    subj.title AS subject_name,
    a.text AS text_a,
    b.text AS text_b,
    a.quote AS quote_a,
    b.quote AS quote_b,
    a.condition AS condition_a,
    b.condition AS condition_b
   FROM ((kb.claims a
     JOIN kb.claims b ON (((a.subject_id = b.subject_id) AND (a.id < b.id))))
     LEFT JOIN kb.pages subj ON (((subj.id = a.subject_id) AND api.visible_collection(subj.collection))))
  WHERE ((a.status = 'disputed'::text) AND (b.status = 'disputed'::text) AND api.visible_collection(a.collection) AND api.visible_collection(b.collection))
""",
)

_PREVIOUS_COMMENTS: tuple[tuple[str, str], ...] = (
    (
        "VIEW api.claims",
        (
            "Joined, human-readable claims: owning page title and subject/dataset/task/metric NAMES (never bare ids), plus "
            "quote+loc for L4 verification. Example: SELECT page_title, text, quote, loc FROM api.claims WHERE claim_id = "
            "'<page-id>#c1';"
        ),
    ),
    ("COLUMN api.claims.claim_id", "Claim id '<page_id>#cN' \u2014 unique only paired with collection. Example value: '<page-id>#c1'."),
    ("COLUMN api.claims.collection", "This claim's own collection (its owning page's collection). Example value: '<collection>'."),
    ("COLUMN api.claims.page_id", "Owning page id."),
    ("COLUMN api.claims.page_title", "Owning page title."),
    ("COLUMN api.claims.subject_name", "Title of the page the claim is about."),
    ("COLUMN api.claims.dataset_name", "Dataset name the result is on."),
    ("COLUMN api.claims.task_name", "Task name."),
    ("COLUMN api.claims.metric_name", "Metric name."),
    ("COLUMN api.claims.text", "The atomic claim sentence."),
    ("COLUMN api.claims.quote", "Verbatim source snippet supporting the claim (may be NULL)."),
    ("COLUMN api.claims.loc", "Raw locator into the source. Example value: '\u00a75.1, Table 2'."),
    ("COLUMN api.claims.kind", "Claim type (vocabulary)."),
    ("COLUMN api.claims.condition", "Qualifying condition."),
    ("COLUMN api.claims.magnitude", "Effect size text."),
    ("COLUMN api.claims.eval_setting", "Evaluation setting."),
    ("COLUMN api.claims.status", "Claim status: active|disputed|superseded|retracted-source."),
    (
        "VIEW api.contradictions",
        (
            "Disputed claim pairs about the same subject, with both quotes + conditions. Example: SELECT subject_name, "
            "text_a, text_b FROM api.contradictions LIMIT 20;"
        ),
    ),
    ("COLUMN api.contradictions.claim_a", "First claim id of the disputed pair."),
    ("COLUMN api.contradictions.claim_b", "Second claim id of the disputed pair."),
    ("COLUMN api.contradictions.subject_id", "Shared subject page id."),
    ("COLUMN api.contradictions.subject_name", "Shared subject name."),
    ("COLUMN api.contradictions.text_a", "Claim A sentence."),
    ("COLUMN api.contradictions.text_b", "Claim B sentence."),
    ("COLUMN api.contradictions.quote_a", "Claim A source quote."),
    ("COLUMN api.contradictions.quote_b", "Claim B source quote."),
    ("COLUMN api.contradictions.condition_a", "Claim A qualifying condition."),
    ("COLUMN api.contradictions.condition_b", "Claim B qualifying condition."),
)


def upgrade() -> None:
    """Fold the four typed claim columns into `fields`, then publish claims and contradictions over it."""
    for view in _VIEWS:
        op.execute(f"DROP VIEW {view}")
    op.execute("ALTER TABLE kb.claims ADD COLUMN fields jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute(_BACKFILL)
    op.execute(_DROP_TYPED_COLUMNS)
    op.execute(_CLAIMS)
    op.execute(_CONTRADICTIONS)
    _write_comments(_COMMENTS)
    _grant_reader()


def downgrade() -> None:
    """Restore `condition`, `baseline`, `magnitude` and `eval_setting` from `fields`, and every other key in `fields` is lost.

    Both views, their comments and their grants then stand as at 0063.
    """
    for view in _VIEWS:
        op.execute(f"DROP VIEW {view}")
    op.execute(_ADD_TYPED_COLUMNS)
    op.execute(_RESTORE)
    op.execute("ALTER TABLE kb.claims DROP COLUMN fields")
    for view in _PREVIOUS_VIEWS:
        op.execute(view)
    _write_comments(_PREVIOUS_COMMENTS)
    _grant_reader()


def _write_comments(comments: tuple[tuple[str, str], ...]) -> None:
    """Write each comment on its target."""
    for target, text in comments:
        op.execute(f"COMMENT ON {target} IS {_literal(text)}")


def _grant_reader() -> None:
    """Let the read-only role select from both views."""
    for view in _VIEWS:
        op.execute(f"GRANT SELECT ON TABLE {view} TO {_READER_ROLE}")


def _literal(text: str) -> str:
    """`text` as a single-quoted SQL string literal."""
    return "'" + text.replace("'", "''") + "'"
