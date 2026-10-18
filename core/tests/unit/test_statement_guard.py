"""The checks on a statement a model wrote: one statement, a size bound, no recursive query and no denied function."""

import pytest

from core.ai.retrieval.sql_tokens import all_tokens, code_tokens
from core.ai.retrieval.statement import (
    BACKSLASH_QUOTE,
    DENIED_FUNCTIONS,
    FUNCTION_NOT_AVAILABLE,
    MAX_BRACKET_DEPTH,
    MAX_STATEMENT_LENGTH,
    NOT_ONE_STATEMENT,
    RECURSIVE_QUERY,
    TOO_DEEP,
    TOO_LONG,
    UNICODE_ESCAPE,
    UNREADABLE_TEXT,
    RefusedStatementError,
    single_statement,
)
from core.ai.retrieval.unreadable_forms import DOLLAR_QUOTE, NON_ASCII, UNREADABLE_FORM

COMMON_QUERIES = [
    "SELECT t.id, t.title AS label, count(*) FROM api.cards AS t GROUP BY t.id, t.title HAVING count(*) > 1 ORDER BY 3 DESC NULLS LAST LIMIT 5",
    "SELECT * FROM api.cards WHERE title ILIKE '%graph%' AND status NOT LIKE 'draft%' AND type = ANY(ARRAY['paper', 'method'])",
    "SELECT a.id FROM a LEFT JOIN b ON b.a_id = a.id AND b.n >= -1 WHERE a.x <> 3 AND a.y != 4 AND a.z IS NOT NULL",
    "SELECT payload->>'name', payload->'items'->0, payload @> '{\"a\": 1}', payload <@ other, tags::text[] FROM t",
    "SELECT arr[1], (ARRAY[1, 2, 3])[2], matrix[1][2], arr[1:2], x::numeric(10, 2), CAST(y AS text) FROM t",
    "SELECT CASE WHEN a > 1 THEN 'big' WHEN a BETWEEN 0 AND 1 THEN 'small' ELSE NULL END, coalesce(b, 0) FROM t",
    "WITH recent AS (SELECT * FROM t WHERE created > now() - INTERVAL '7 days') SELECT count(*) FROM recent",
    "SELECT sum(x) OVER (PARTITION BY g ORDER BY d ROWS BETWEEN 1 PRECEDING AND CURRENT ROW), row_number() OVER w FROM t WINDOW w AS (ORDER BY d)",
    "SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS v(n, label) WHERE EXISTS (SELECT 1 FROM t WHERE t.n = v.n)",
    "SELECT a, b FROM t1 UNION ALL SELECT a, b FROM t2 EXCEPT SELECT a, b FROM t3",
    "SELECT now() AT TIME ZONE 'Europe/Lisbon', date '2026-01-01', extract(year FROM d), 1.5e3, .5, 5., 0.25 FROM t",
    "SELECT name ~* '^a.*b$', name !~ 'x', a % 3, a ^ 2, a || b, a & b, a | b, -a, +a, ~a, a << 1, a >> 1 FROM t",
    "SELECT f(a => 1, b => 'x'), api.search('graph rag', k => 5), count(*) FILTER (WHERE a > 1) FROM t",
    'SELECT "select", "Mixed Case", "with ""quote""", t."x y" FROM "Weird Table" AS t',
    "SELECT a FROM t WHERE b IN (1, 2, 3) AND c NOT IN ('x') AND d IS DISTINCT FROM e AND f SIMILAR TO 'a%'",
    "SELECT unnest(ARRAY[1, 2]) WITH ORDINALITY, array_agg(x ORDER BY y), string_agg(z, ', ') FROM t",
    "SELECT\n\ta,\n\tb\nFROM\tt\r\nWHERE a = 1",
]

ADVISORY_LOCKS = [
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_try_advisory_lock",
    "pg_try_advisory_lock_shared",
    "pg_advisory_xact_lock",
    "pg_advisory_xact_lock_shared",
    "pg_try_advisory_xact_lock",
    "pg_try_advisory_xact_lock_shared",
]


class TestTheStatementThatPasses:
    @pytest.mark.parametrize(
        ("sql", "statement"),
        [
            ("SELECT 1", "SELECT 1"),
            ("SELECT 1;", "SELECT 1"),
            ("  SELECT 1 ;  ", "SELECT 1"),
            ("SELECT 1 -- the one\n", "SELECT 1"),
            ("/* first */ SELECT 1 /* last */ ;", "SELECT 1"),
            ("SELECT ';' AS semicolon", "SELECT ';' AS semicolon"),
            ('SELECT 1 AS "a;b"', 'SELECT 1 AS "a;b"'),
            ("SELECT 'WITH RECURSIVE' AS text", "SELECT 'WITH RECURSIVE' AS text"),
            ("SELECT 1 -- WITH RECURSIVE", "SELECT 1"),
            ("WITH t AS (SELECT 1 AS n) SELECT n FROM t", "WITH t AS (SELECT 1 AS n) SELECT n FROM t"),
        ],
    )
    def test_it_is_returned_without_comments_or_a_closing_semicolon(self, sql: str, statement: str):
        assert single_statement(sql) == statement

    @pytest.mark.parametrize("sql", ["SELECT 'it''s' AS text", "SELECT 'a\\b' AS text", "SELECT 1 WHERE x LIKE 'a\\_b'", "SELECT E'a\\nb'"])
    def test_a_text_with_a_backslash_that_does_not_touch_a_quote(self, sql: str):
        assert single_statement(sql) == sql

    @pytest.mark.parametrize(
        ("sql", "statement"),
        [
            ("SELECT 'café crème' AS text", "SELECT 'café crème' AS text"),
            ('SELECT 1 AS "Größe"', 'SELECT 1 AS "Größe"'),
            ("SELECT 1 -- コメント\n", "SELECT 1"),
            ("SELECT 1 /* commentaire à côté */", "SELECT 1"),
            ('SELECT "ÉTAT" FROM "Tábla" WHERE name = \'Zürich\'', 'SELECT "ÉTAT" FROM "Tábla" WHERE name = \'Zürich\''),
        ],
    )
    def test_other_letters_inside_a_text_a_quoted_name_or_a_comment(self, sql: str, statement: str):
        assert single_statement(sql) == statement

    @pytest.mark.parametrize("sql", COMMON_QUERIES)
    def test_the_queries_a_person_writes(self, sql: str):
        assert single_statement(sql) == sql

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT a ? 'k' FROM t",
            "SELECT a ?| ARRAY['k'] FROM t",
            "SELECT a ?& ARRAY['k', 'l'] FROM t",
            "SELECT a ?  'k' AND b ?| c FROM t",
        ],
    )
    def test_the_json_key_operators(self, sql: str):
        assert single_statement(sql) == sql

    @pytest.mark.parametrize("sql", COMMON_QUERIES)
    def test_what_it_returns_holds_the_tokens_it_checked(self, sql: str):
        assert [token.value for token in code_tokens(single_statement(sql))] == [token.value for token in code_tokens(sql)]

    def test_a_comment_of_any_kind_becomes_whitespace(self):
        sql = "SELECT 1 /* a */ + /*+ hint */ 2 --+ hint\n"

        assert single_statement(sql) == "SELECT 1   +   2"

    def test_a_comment_between_two_signs_keeps_them_apart(self):
        assert single_statement("SELECT 1 - /**/- 2") == "SELECT 1 -  - 2"

    def test_a_statement_of_many_plain_tokens(self):
        sql = "SELECT " + "1+" * 9_000 + "1"

        assert single_statement(sql) == sql

    def test_a_statement_at_the_size_bound(self):
        sql = "SELECT '" + "x" * (MAX_STATEMENT_LENGTH - len("SELECT ''")) + "'"

        assert len(single_statement(sql)) == MAX_STATEMENT_LENGTH


class TestTheStatementThatIsRefused:
    @pytest.mark.parametrize(
        "sql", ["SELECT 1; SELECT 2", "SELECT 1;SELECT 2", "SELECT 1; DROP TABLE kb.pages", "SELECT 1; -- x\nSELECT 2"]
    )
    def test_a_second_statement(self, sql: str):
        with pytest.raises(RefusedStatementError, match=NOT_ONE_STATEMENT):
            single_statement(sql)

    @pytest.mark.parametrize("sql", ["", "   ", "\n", ";", " ; ; ", "-- only a comment", "/* only a comment */"])
    def test_no_statement_at_all(self, sql: str):
        with pytest.raises(RefusedStatementError, match=NOT_ONE_STATEMENT):
            single_statement(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH RECURSIVE t AS (SELECT 1) SELECT * FROM t",
            "with recursive t as (select 1) select * from t",
            "WITH\n  RECURSIVE t AS (SELECT 1) SELECT * FROM t",
            "WITH /* gap */ RECURSIVE t AS (SELECT 1) SELECT * FROM t",
            "SELECT * FROM (WITH RECURSIVE t AS (SELECT 1) SELECT * FROM t) AS q",
        ],
    )
    def test_a_recursive_query_however_it_is_spelled(self, sql: str):
        with pytest.raises(RefusedStatementError, match="WITH RECURSIVE is not allowed"):
            single_statement(sql)

    def test_the_recursive_refusal_names_what_to_use_instead(self):
        assert "api functions that walk the graph" in RECURSIVE_QUERY

    def test_a_statement_over_the_size_bound(self):
        sql = "SELECT '" + "x" * (MAX_STATEMENT_LENGTH - len("SELECT ''") + 1) + "'"

        with pytest.raises(RefusedStatementError, match=TOO_LONG):
            single_statement(sql)

    def test_the_size_refusal_comes_before_any_other_check(self):
        with pytest.raises(RefusedStatementError, match=TOO_LONG):
            single_statement("SELECT 1; " * MAX_STATEMENT_LENGTH)


class TestTheBracketDepth:
    def test_the_bound_is_one_hundred(self):
        assert MAX_BRACKET_DEPTH == 100

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT " + "(" * 100 + "1" + ")" * 100,
            "SELECT " + "search(" * 100 + "1" + ")" * 100,
            "SELECT " + "ARRAY[" * 100 + "1" + "]" * 100,
            "SELECT " + "(ARRAY[" * 50 + "1" + "])" * 50,
            "SELECT '" + "(" * 500 + "' AS text",
            "SELECT 1 /* " + "(" * 500 + " */",
            "SELECT " + "(1)" * 5_000,
        ],
        ids=["parentheses", "calls", "square brackets", "both kinds", "inside a text", "inside a comment", "many but shallow"],
    )
    def test_a_statement_that_nests_to_the_bound_or_not_at_all_passes(self, sql: str):
        assert single_statement(sql) == sql.replace(" /* " + "(" * 500 + " */", "")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT " + "(" * 101 + "1" + ")" * 101,
            "SELECT " + "ARRAY[" * 101 + "1" + "]" * 101,
            "SELECT " + "(ARRAY[" * 51 + "1" + "])" * 51,
            "SELECT " + "search(" * 2_498 + "1" + ")" * 2_498,
            "SELECT " + "(" * 19_000,
        ],
        ids=["parentheses", "square brackets", "both kinds", "nested calls", "never closed"],
    )
    def test_a_statement_that_nests_deeper_is_refused(self, sql: str):
        with pytest.raises(RefusedStatementError, match=TOO_DEEP):
            single_statement(sql)

    def test_the_refusal_names_the_bound(self):
        assert TOO_DEEP == "The statement nests brackets more than 100 levels deep. Send a simpler one."

    def test_a_closing_bracket_with_nothing_open_does_not_offset_a_later_depth(self):
        within = "SELECT " + ")" * 200 + "(" * 100 + ")" * 100
        past = "SELECT " + ")" * 200 + "(" * 101

        assert single_statement(within) == within
        with pytest.raises(RefusedStatementError, match=TOO_DEEP):
            single_statement(past)


class TestTheDeniedFunctions:
    def test_the_list_changes_no_session_takes_no_lock_runs_no_string_and_reads_no_file(self):
        designed = {
            "set_config",
            *ADVISORY_LOCKS,
            "pg_sleep",
            "pg_sleep_for",
            "pg_sleep_until",
            "query_to_xml",
            "query_to_xmlschema",
            "query_to_xml_and_xmlschema",
            "cursor_to_xml",
            "cursor_to_xmlschema",
            "table_to_xml",
            "table_to_xmlschema",
            "table_to_xml_and_xmlschema",
            "schema_to_xml",
            "schema_to_xmlschema",
            "schema_to_xml_and_xmlschema",
            "database_to_xml",
            "database_to_xmlschema",
            "database_to_xml_and_xmlschema",
            "pg_notify",
            "pg_cancel_backend",
            "pg_terminate_backend",
            "pg_reload_conf",
            "pg_rotate_logfile",
            "pg_read_file",
            "pg_read_binary_file",
            "pg_ls_dir",
            "pg_stat_file",
        }

        assert designed == DENIED_FUNCTIONS

    @pytest.mark.parametrize(
        "name",
        [
            "set_config",
            "pg_catalog.set_config",
            '"set_config"',
            '"SET_CONFIG"',
            "Set_Config",
            'pg_catalog."Set_Config"',
            '"pg_catalog"."set_config"',
            "public . set_config",
        ],
    )
    def test_set_config_however_its_name_is_written(self, name: str):
        with pytest.raises(RefusedStatementError, match=FUNCTION_NOT_AVAILABLE):
            single_statement(f"SELECT {name}('app.collection', 'other', false)")

    @pytest.mark.parametrize(
        "call", ["set_config ('a', 'b', false)", "set_config/* gap */('a', 'b', false)", "set_config\n(\n'a', 'b', false)"]
    )
    def test_a_call_with_anything_between_the_name_and_the_bracket(self, call: str):
        with pytest.raises(RefusedStatementError, match=FUNCTION_NOT_AVAILABLE):
            single_statement(f"SELECT {call}")

    @pytest.mark.parametrize("name", sorted(DENIED_FUNCTIONS))
    def test_every_denied_name(self, name: str):
        with pytest.raises(RefusedStatementError, match=FUNCTION_NOT_AVAILABLE):
            single_statement(f"SELECT {name}(1)")

    @pytest.mark.parametrize("name", ["lo_import", "lo_export", "lo_open", "lo_unlink", "pg_catalog.lo_get", '"LO_FROM_BYTEA"'])
    def test_every_large_object_function(self, name: str):
        with pytest.raises(RefusedStatementError, match=FUNCTION_NOT_AVAILABLE):
            single_statement(f"SELECT {name}(1)")

    def test_a_denied_function_in_a_from_clause_a_subquery_and_a_case(self):
        for sql in [
            "SELECT * FROM pg_sleep(1)",
            "SELECT * FROM (SELECT pg_advisory_lock(1)) AS q",
            "SELECT CASE WHEN true THEN set_config('a', 'b', false) END",
        ]:
            with pytest.raises(RefusedStatementError, match=FUNCTION_NOT_AVAILABLE):
                single_statement(sql)

    def test_query_to_xml_whose_text_holds_a_recursive_query(self):
        sql = "SELECT query_to_xml('WITH RECURSIVE t AS (SELECT 1) SELECT * FROM t', true, false, '')"

        with pytest.raises(RefusedStatementError, match=FUNCTION_NOT_AVAILABLE):
            single_statement(sql)

    def test_the_refusal_names_the_surface_to_use_instead(self):
        assert FUNCTION_NOT_AVAILABLE == "That function is not available. Query the api views and functions."

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 'set_config' AS text",
            "SELECT 'pg_sleep(1)' AS text",
            "SELECT 1 -- set_config('a', 'b', false)",
            "/* pg_sleep(1) */ SELECT 1",
            "SELECT 'a' IN ('set_config', 'pg_sleep')",
            "SELECT 1 AS set_config",
            "SELECT set_config FROM t",
            "SELECT lower('A'), log(2), loop_count(1)",
        ],
    )
    def test_the_same_names_that_are_not_calls_pass(self, sql: str):
        assert single_statement(sql)


class TestTheTextsThatHideACall:
    @pytest.mark.parametrize(
        "sql", ["SELECT 'a\\' || set_config('x', 'y', false) || 'b'", "SELECT E'it\\'s'", "SELECT 'a\\\\' || pg_sleep(1) || 'b'"]
    )
    def test_a_backslash_before_a_quote_is_refused(self, sql: str):
        with pytest.raises(RefusedStatementError, match=BACKSLASH_QUOTE):
            single_statement(sql)

    def test_the_refusal_names_the_way_to_write_a_quote(self):
        assert "two quotes" in BACKSLASH_QUOTE

    @pytest.mark.parametrize("sql", ['SELECT U&"\\0073et_config"(1)', "SELECT U&\"d!0061t!+000061\" UESCAPE '!' FROM t", 'SELECT u&"x"'])
    def test_a_unicode_escaped_name_is_refused(self, sql: str):
        with pytest.raises(RefusedStatementError, match=UNICODE_ESCAPE):
            single_statement(sql)

    def test_a_unicode_escaped_text_passes(self):
        assert single_statement("SELECT U&'d\\0061t\\+000061'")


class TestWhatSqlparseAndTheDatabaseReadDifferently:
    @pytest.mark.parametrize(
        ("sql", "message"),
        [
            ("SELECT @set_config('x.y','1',false)::int", UNREADABLE_FORM),
            ("SELECT 1 #set_config('x.y','1',false)::int", UNREADABLE_FORM),
            ("SELECT 1 # 2", UNREADABLE_FORM),
            ("SELECT (ARRAY[1,2,3])[1 :set_config('x.y','2',false)::int]", UNREADABLE_FORM),
            ("SELECT (ARRAY[1,2]) [pg_try_advisory_lock(7)::int]", UNREADABLE_FORM),
            ("SELECT (ARRAY[1,2]) [pg_sleep(5)::text::int]", UNREADABLE_FORM),
            ("SELECT (ARRAY[1,2]) [query_to_xml('select 1',true,false,'')::text::int]", UNREADABLE_FORM),
            ("SELECT 1 AS \u00b4a, set_config('x.y','b',false) AS b\u00b4", NON_ASCII),
            ("SELECT 1 AS \u20ac$$, pg_try_advisory_lock(7) AS \u20acx$$", NON_ASCII),
            ("SELECT 1 AS a$$, set_config('x.y','1',false) AS b$$", FUNCTION_NOT_AVAILABLE),
            ("SELECT /* /* */ ' */ set_config('x.y','1',false) --'", UNREADABLE_FORM),
            ("SELECT 1 +/* (( */ 1) AS q; DROP TABLE t; SELECT * FROM (SELECT 2", NOT_ONE_STATEMENT),
            ("SELECT 1 AS \u00b4a) AS q; DROP TABLE x; SELECT * FROM (SELECT 2 AS b\u00b4", NON_ASCII),
            ("SELECT (1; SELECT 2", NOT_ONE_STATEMENT),
            ("SELECT 'a\x00'", UNREADABLE_TEXT),
            ("SELECT '\ud800'", UNREADABLE_TEXT),
        ],
        ids=[
            "an at sign before a call",
            "a hash before a call",
            "a hash comment",
            "a slice with a colon before a call",
            "a spaced subscript that holds a lock",
            "a spaced subscript that sleeps",
            "a spaced subscript that runs a query",
            "acute accent names",
            "names holding a euro sign and dollars",
            "names ending in dollars",
            "a comment inside a comment",
            "an operator that opens a comment, then a second statement",
            "acute accent names around a second statement",
            "a bracket that never closes",
            "a NUL character",
            "a lone surrogate",
        ],
    )
    def test_each_is_refused_with_its_own_message(self, sql: str, message: str):
        with pytest.raises(RefusedStatementError) as raised:
            single_statement(sql)

        assert str(raised.value) == message

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 +-- '\n 2, set_config('x.y','8',false) --'",
            "SELECT 1 +/* ' */ 2, set_config('x.y','9',false) /* ' */",
            "SELECT 1 +-- x\n 2",
            "SELECT 1 +/* x */ 2",
            "SELECT a |/* x */ b FROM t",
        ],
    )
    def test_an_operator_that_runs_into_a_comment_is_refused(self, sql: str):
        with pytest.raises(RefusedStatementError, match=UNREADABLE_FORM):
            single_statement(sql)

    @pytest.mark.parametrize(
        "sql",
        ["SELECT 1 - -2", "SELECT 1 - /* x */ 2", "SELECT 1 -- x", "SELECT a * b / c FROM t", "SELECT a /* x */ * b", "SELECT 2 *-- x\n 2"],
    )
    def test_an_operator_that_is_apart_from_its_comment_passes(self, sql: str):
        assert single_statement(sql)


class TestNonAsciiOutsideATextAndAComment:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 AS \u00e9t\u00e9",
            "SELECT \u00c5\u00c6 FROM t",
            "SELECT 1\u00a0+ 2",
            "SELECT 1\u2003+ 2",
            "SELECT 1 AS \u00b4a\u00b4",
            "SELECT t.\u00f8 FROM t",
            "SELECT 1 AS \ud7ff",
        ],
    )
    def test_it_is_refused(self, sql: str):
        with pytest.raises(RefusedStatementError, match=NON_ASCII):
            single_statement(sql)

    def test_the_refusal_says_where_other_letters_are_allowed(self):
        assert "inside texts and double-quoted names" in NON_ASCII


class TestFormsThatAreNotPlainNamesAndTexts:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 'unterminated",
            'SELECT "unterminated',
            "SELECT `a`",
            "SELECT [a]",
            "SELECT 1 AS {a}",
            "SELECT $1",
            "SELECT :a",
            "SELECT %s",
            "SELECT %(a)s",
            "SELECT \\set x",
            "SELECT now() AT TIME ZONE 'a\\b'",
            "SELECT 1 AS \x01a",
            "SELECT 1\x0b+ 2",
            "SELECT 1\x1c+ 2",
            "SELECT 1\x85+ 2",
            "SELECT 1 2x",
            "SELECT $a$ x",
            "SELECT 1 GO 2",
            'SELECT 1 AS ""',
        ],
    )
    def test_it_is_refused(self, sql: str):
        with pytest.raises(RefusedStatementError) as raised:
            single_statement(sql)

        assert str(raised.value) in {UNREADABLE_FORM, NON_ASCII}

    @pytest.mark.parametrize(
        "sql", ["SELECT 1 # 2", "SELECT a #> b FROM t", "SELECT a #>> '{x}' FROM t", "SELECT a #- '{x}' FROM t", "SELECT #a"]
    )
    def test_a_hash_outside_a_text_and_a_comment(self, sql: str):
        with pytest.raises(RefusedStatementError, match=UNREADABLE_FORM):
            single_statement(sql)

    @pytest.mark.parametrize("sql", ["SELECT '#' AS hash", "SELECT 1 -- # 2", "SELECT 1 /* # */", 'SELECT 1 AS "#"'])
    def test_a_hash_inside_a_text_a_comment_or_a_quoted_name_passes(self, sql: str):
        assert single_statement(sql)

    @pytest.mark.parametrize("sql", ["SELECT 1 /* a /* b */ c */", "SELECT 1 /* /* */", "SELECT 1 /*+ a /* b */", "SELECT 1 /* a /*/ b */"])
    def test_a_block_comment_that_holds_another_opening(self, sql: str):
        with pytest.raises(RefusedStatementError, match=UNREADABLE_FORM):
            single_statement(sql)

    @pytest.mark.parametrize(
        "sql", ["SELECT 1 /* a * b / c */", "SELECT 1 /**/", "SELECT 1 /*+ a */", "SELECT 1 /* a */ /* b */", "SELECT 1 --+ a"]
    )
    def test_a_block_comment_that_holds_no_opening_passes(self, sql: str):
        assert single_statement(sql) == "SELECT 1"

    @pytest.mark.parametrize(
        "sql", ["SELECT $$a$$", "SELECT $tag$a$tag$", "SELECT $$a;b$$ AS text", "SELECT $$pg_advisory_lock(1)$$ AS text"]
    )
    def test_a_dollar_quoted_text(self, sql: str):
        with pytest.raises(RefusedStatementError, match=DOLLAR_QUOTE):
            single_statement(sql)

    def test_the_dollar_refusal_names_the_way_to_write_a_text(self):
        assert "single quotes" in DOLLAR_QUOTE

    @pytest.mark.parametrize(
        "sql", ["SELECT 1; SELECT 2", "SELECT (1; SELECT 2", "SELECT 1;;", "SELECT ';'; SELECT 2", "SELECT (SELECT 1; SELECT 2)"]
    )
    def test_a_semicolon_that_is_not_the_last_code_token(self, sql: str):
        with pytest.raises(RefusedStatementError, match=NOT_ONE_STATEMENT):
            single_statement(sql)

    @pytest.mark.parametrize("sql", ["SELECT 1 ;", "SELECT 1; -- done", "SELECT 1;\n/* done */", "SELECT ';' ;"])
    def test_a_closing_semicolon_that_is_the_last_code_token_passes(self, sql: str):
        assert single_statement(sql) in {"SELECT 1", "SELECT ';'"}


class TestTheTextTheDatabaseCannotTake:
    @pytest.mark.parametrize(
        "sql", ["SELECT 'a\x00b'", "\x00", "SELECT 1 -- \x00", "SELECT '\ud800'", "SELECT 1 /* \udfff */", 'SELECT 1 AS "\ud83d"']
    )
    def test_a_nul_or_a_character_that_is_not_utf_8(self, sql: str):
        with pytest.raises(RefusedStatementError, match=UNREADABLE_TEXT):
            single_statement(sql)

    def test_one_message_for_both(self):
        assert UNREADABLE_TEXT == "The statement holds a character the database cannot take. Remove it."


class TestWhenTheLexerLosesText:
    def test_the_tokens_are_not_trusted(self):
        with pytest.raises(ValueError, match="dropped part"):
            all_tokens("SELECT 1", tokenize=lambda statement: iter(()))
