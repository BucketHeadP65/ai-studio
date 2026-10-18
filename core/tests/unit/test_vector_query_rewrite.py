"""The rewrite that gives each meaning text of a model's statement its vector, so the model never handles a vector."""

import pytest

from core.ai.retrieval.statement import RefusedStatementError
from core.ai.retrieval.vector_query_rewrite import (
    MAX_TEXT_LENGTH,
    MAX_TEXTS,
    NO_QUERY_VEC,
    TEXT_TOO_LONG,
    TOO_MANY_TEXTS,
    VECTOR_COUNT,
    VECTOR_QUERY_FORM,
    VectorQueryRewrite,
)


def search(*arguments: str) -> str:
    """A call of the search with the arguments as the model wrote them."""
    return f"SELECT * FROM api.search({', '.join(arguments)})"


class TestFindingTheTexts:
    def test_a_text_in_a_named_argument(self):
        rewrite = VectorQueryRewrite.of(search("'graph rag'", "vector_query => 'how retrieval walks a graph'"))

        assert rewrite.texts == ("how retrieval walks a graph",)

    def test_a_doubled_quote_inside_the_text(self):
        rewrite = VectorQueryRewrite.of(search("'rag'", "vector_query => 'the author''s method'"))

        assert rewrite.texts == ("the author's method",)

    def test_a_text_in_a_common_table_expression(self):
        sql = "WITH hits AS (SELECT * FROM api.search('a', vector_query => 'meaning')) SELECT count(*) FROM hits"

        assert VectorQueryRewrite.of(sql).texts == ("meaning",)

    def test_a_text_in_each_of_two_calls(self):
        sql = (
            "SELECT * FROM api.search('a', vector_query => 'first') AS one "
            "JOIN api.search('b', vector_query => 'second') AS two ON one.id = two.id"
        )

        assert VectorQueryRewrite.of(sql).texts == ("first", "second")

    def test_the_same_text_twice_is_held_once(self):
        sql = "SELECT * FROM api.search('a', vector_query => 'same') UNION ALL SELECT * FROM api.search('b', vector_query => 'same')"

        assert VectorQueryRewrite.of(sql).texts == ("same",)

    def test_the_name_in_any_case_or_quoted(self):
        sql = """SELECT * FROM api.search('a', VECTOR_QUERY => 'upper'), api.search('b', "vector_query" => 'quoted')"""

        assert VectorQueryRewrite.of(sql).texts == ("upper", "quoted")

    def test_a_text_after_other_named_arguments(self):
        sql = search("query => 'a'", "k => 5", "vector_query => 'meaning'", "facets => '{}'")

        assert VectorQueryRewrite.of(sql).texts == ("meaning",)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 'vector_query => ''hidden''' AS text",
            "SELECT 1 -- vector_query => 'hidden'",
            "/* vector_query => 'hidden' */ SELECT 1",
            "SELECT vector_query FROM t",
            "SELECT 1 AS vector_query",
            "SELECT 'query_vec => x' AS text",
        ],
    )
    def test_the_same_words_inside_a_string_or_a_comment_or_without_a_value(self, sql: str):
        assert VectorQueryRewrite.of(sql).texts == ()

    @pytest.mark.parametrize("statement", ["", " ", "SELECT 1", "-- only a comment"])
    def test_a_statement_with_no_text(self, statement: str):
        assert VectorQueryRewrite.of(statement).texts == ()

    def test_up_to_the_most_texts(self):
        sql = "SELECT " + ", ".join(f"(SELECT 1 FROM api.search('q', vector_query => 'text {n}'))" for n in range(MAX_TEXTS))

        assert len(VectorQueryRewrite.of(sql).texts) == MAX_TEXTS

    def test_a_text_of_the_most_characters(self):
        text = "x" * MAX_TEXT_LENGTH

        assert VectorQueryRewrite.of(search("'q'", f"vector_query => '{text}'")).texts == (text,)

    def test_a_quote_counts_once_toward_the_length(self):
        text = "'" * MAX_TEXT_LENGTH

        assert VectorQueryRewrite.of(search("'q'", "vector_query => '" + text.replace("'", "''") + "'")).texts == (text,)


class TestTheTextsThatAreRefused:
    @pytest.mark.parametrize("value", ["E'a meaning'", "e'a meaning'", "U&'a meaning'", "N'a meaning'"])
    def test_a_text_that_is_not_one_plain_quoted_literal(self, value: str):
        with pytest.raises(RefusedStatementError, match=VECTOR_QUERY_FORM):
            VectorQueryRewrite.of(search("'q'", f"vector_query => {value}"))

    @pytest.mark.parametrize("value", ["NULL", "'a' || 'b'", "lower('A')", "'a'::text", "('a')", "some_column", "'m'::halfvec(2)"])
    def test_a_text_that_is_an_expression(self, value: str):
        with pytest.raises(RefusedStatementError, match=VECTOR_QUERY_FORM):
            VectorQueryRewrite.of(search("'q'", f"vector_query => {value}"))

    @pytest.mark.parametrize("argument", ["query_vec => NULL", "query_vec => '[0.5,0.25]'::halfvec", "QUERY_MODEL => 'other'"])
    def test_a_vector_or_a_model_the_model_passes_itself(self, argument: str):
        with pytest.raises(RefusedStatementError, match="Never pass query_vec or query_model"):
            VectorQueryRewrite.of(search("'q'", argument))

    def test_the_refusals_say_how_to_pass_the_text(self):
        assert VECTOR_QUERY_FORM == "Pass vector_query by name, as one quoted text."
        assert "vector_query => '<text>'" in NO_QUERY_VEC

    def test_a_text_past_the_most_texts(self):
        sql = "SELECT " + ", ".join(f"(SELECT 1 FROM api.search('q', vector_query => 'text {n}'))" for n in range(MAX_TEXTS + 1))

        with pytest.raises(RefusedStatementError, match=TOO_MANY_TEXTS):
            VectorQueryRewrite.of(sql)

    def test_the_same_text_past_the_most_texts_still_counts(self):
        sql = "SELECT " + ", ".join("(SELECT 1 FROM api.search('q', vector_query => 'same'))" for _ in range(MAX_TEXTS + 1))

        with pytest.raises(RefusedStatementError, match=TOO_MANY_TEXTS):
            VectorQueryRewrite.of(sql)

    def test_a_text_over_the_most_characters(self):
        with pytest.raises(RefusedStatementError, match=TEXT_TOO_LONG):
            VectorQueryRewrite.of(search("'q'", "vector_query => '" + "x" * (MAX_TEXT_LENGTH + 1) + "'"))


class TestRenderingTheVectors:
    def test_a_text_becomes_its_vector(self):
        rewrite = VectorQueryRewrite.of(search("'graph rag'", "vector_query => 'how retrieval walks a graph'", "k => 5"))

        rendered = rewrite.render([[0.5, 0.25]])

        assert rendered == search("'graph rag'", "query_vec => '[0.5,0.25]'::halfvec", "k => 5")

    def test_the_type_is_the_one_it_is_given(self):
        rewrite = VectorQueryRewrite.of(search("'q'", "vector_query => 'm'"))

        assert rewrite.render([[1.0]], "halfvec(1024)") == search("'q'", "query_vec => '[1.0]'::halfvec(1024)")

    def test_the_model_rides_beside_each_vector(self):
        rewrite = VectorQueryRewrite.of(search("'q'", "vector_query => 'm'", "k => 5"))

        rendered = rewrite.render([[1.0]], model="embed-large")

        assert rendered == search("'q'", "query_vec => '[1.0]'::halfvec, query_model => 'embed-large'", "k => 5")

    def test_no_model_rides_beside_a_missing_vector(self):
        rewrite = VectorQueryRewrite.of(search("'q'", "vector_query => 'm'"))

        assert rewrite.render(None, model="embed-large") == search("'q'", "query_vec => NULL")

    def test_identical_texts_share_one_vector(self):
        sql = "SELECT * FROM api.search('a', vector_query => 'same') UNION ALL SELECT * FROM api.search('b', vector_query => 'same')"

        rendered = VectorQueryRewrite.of(sql).render([[0.5]])

        assert rendered.count("query_vec => '[0.5]'::halfvec") == 2

    def test_each_text_gets_its_own_vector_in_order(self):
        sql = "SELECT * FROM api.search('a', vector_query => 'first') UNION ALL SELECT * FROM api.search('b', vector_query => 'second')"

        rendered = VectorQueryRewrite.of(sql).render([[1.0], [2.0]])

        assert rendered.index("'[1.0]'") < rendered.index("'[2.0]'")

    def test_without_vectors_each_text_becomes_null(self):
        rewrite = VectorQueryRewrite.of(search("'q'", "vector_query => 'm'", "k => 5"))

        assert rewrite.render(None) == search("'q'", "query_vec => NULL", "k => 5")

    def test_the_words_inside_strings_and_comments_stay(self):
        sql = "SELECT 'vector_query => ''x''' AS note, * FROM api.search('q', vector_query => 'm') -- vector_query => 'y'"

        rendered = VectorQueryRewrite.of(sql).render([[0.5]])

        assert "'vector_query => ''x'''" in rendered
        assert "-- vector_query => 'y'" in rendered

    def test_a_wrong_number_of_vectors_is_an_error(self):
        rewrite = VectorQueryRewrite.of(search("'q'", "vector_query => 'm'"))

        with pytest.raises(ValueError, match=VECTOR_COUNT):
            rewrite.render([[0.5], [0.25]])
