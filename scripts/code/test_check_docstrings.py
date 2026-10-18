"""The docstring check reports what breaks the code standard, and nothing else."""

import contextlib
import io
import sys
import tempfile
import textwrap
import unittest

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_docstrings  # noqa: E402


def _findings(source: str, *, name: str = "module.py", vocabulary: bool = False) -> list[str]:
    """The messages the check prints for one module written to a temporary folder."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return [finding.message for finding in check_docstrings.check_file(path, Path(folder), vocabulary=vocabulary)]


class PrivateDocstrings(unittest.TestCase):
    """A private definition has one line or no docstring."""

    def test_a_private_function_with_a_long_docstring_is_reported(self) -> None:
        """Two lines on a private function is one too many."""
        source = '''
            def _helper(value: int) -> int:
                """Doubles the value.

                Args:
                    value: The value.
                """
                return value * 2
        '''
        self.assertEqual(_findings(source), ["private `_helper` needs a one-line docstring or none"])

    def test_a_private_function_with_one_line_passes(self) -> None:
        """One line is what a private function may have."""
        source = '''
            def _helper(value: int) -> int:
                """Doubles the value."""
                return value * 2
        '''
        self.assertEqual(_findings(source), [])

    def test_a_function_inside_a_function_is_private(self) -> None:
        """A nested function counts as private whatever its name."""
        source = '''
            def outer() -> None:
                """Runs the inner step."""

                def inner() -> None:
                    """Does the work.

                    It does it twice.
                    """
        '''
        self.assertEqual(_findings(source), ["private `inner` needs a one-line docstring or none"])

    def test_a_method_of_a_private_class_is_private(self) -> None:
        """A method inherits the privacy of its class."""
        source = '''
            class _Row:
                """One row."""

                def width(self) -> int:
                    """How wide the row is.

                    Counted in characters.
                    """
                    return 1
        '''
        self.assertEqual(_findings(source), ["private `width` needs a one-line docstring or none"])

    def test_a_module_with_a_private_name_makes_its_functions_private(self) -> None:
        """Everything in a module whose own name is private is private."""
        source = '''
            def helper(value: int) -> int:
                """Doubles the value.

                It never fails.
                """
                return value * 2
        '''
        self.assertEqual(_findings(source, name="_internal.py"), ["private `helper` needs a one-line docstring or none"])


class PublicDocstrings(unittest.TestCase):
    """A public function documents its parameters and what it returns."""

    def test_a_public_function_without_a_docstring_is_reported(self) -> None:
        """Every public function has a docstring."""
        self.assertEqual(_findings("def run() -> None:\n    pass\n"), ["public `run` has no docstring"])

    def test_missing_args_and_returns_are_reported(self) -> None:
        """A parameter needs an Args section and a value needs a Returns section."""
        source = '''
            def double(value: int) -> int:
                """Doubles the value."""
                return value * 2
        '''
        self.assertEqual(
            _findings(source),
            ["`double` takes value and has no Args section", "`double` returns a value and has no Returns section"],
        )

    def test_a_documented_function_passes(self) -> None:
        """Args and Returns together meet the standard."""
        source = '''
            def double(value: int) -> int:
                """Doubles the value.

                Args:
                    value: The value.

                Returns:
                    Twice the value.
                """
                return value * 2
        '''
        self.assertEqual(_findings(source), [])

    def test_a_method_does_not_count_self(self) -> None:
        """A method with only `self` needs no Args section."""
        source = '''
            class Counter:
                """Counts."""

                def reset(self) -> None:
                    """Starts again from zero."""
        '''
        self.assertEqual(_findings(source), [])

    def test_a_generator_needs_yields(self) -> None:
        """A function that yields documents it under Yields, not Returns."""
        source = '''
            from collections.abc import Iterator


            def numbers() -> Iterator[int]:
                """The first two numbers.

                Returns:
                    The numbers.
                """
                yield 1
                yield 2
        '''
        self.assertEqual(_findings(source), ["`numbers` is a generator and has no Yields section"])

    def test_a_property_needs_no_returns(self) -> None:
        """A property describes what it holds."""
        source = '''
            class Counter:
                """Counts."""

                @property
                def total(self) -> int:
                    """How many have been counted."""
                    return 0
        '''
        self.assertEqual(_findings(source), [])

    def test_a_route_and_a_command_need_no_sections(self) -> None:
        """A route's or command's docstring is its help text."""
        source = '''
            class Controller:
                """The routes."""

                @router.get("/items")
                async def list_items(self, limit: int) -> dict[str, int]:
                    """Every item, up to the limit."""
                    return {}


            @app.command()
            def index(path: str) -> None:
                """Index the knowledge base."""
        '''
        self.assertEqual(_findings(source), [])

    def test_an_overload_needs_no_docstring(self) -> None:
        """An overload carries no docstring. The implementation beneath it carries the contract."""
        source = '''
            from typing import Literal, overload


            @overload
            def pick(kind: Literal["a"]) -> int: ...
            @overload
            def pick(kind: Literal["b"]) -> str: ...
            def pick(kind: str) -> int | str:
                """One value for `kind`.

                Args:
                    kind: Which value.

                Returns:
                    The value.
                """
                return 0
        '''
        self.assertEqual(_findings(source), [])

    def test_a_double_underscore_method_other_than_the_constructor_and_call_is_exempt(self) -> None:
        """A method such as `__repr__` needs no docstring."""
        source = '''
            class Counter:
                """Counts."""

                def __eq__(self, other: object) -> bool:
                    return True
        '''
        self.assertEqual(_findings(source), [])

    def test_a_public_call_method_is_read_like_any_method(self) -> None:
        """A `__call__` that takes parameters and returns a value names both."""
        source = '''
            class Doubler:
                """Doubles."""

                def __call__(self, value: int) -> int:
                    """Double the value."""
                    return value * 2
        '''
        self.assertEqual(
            _findings(source), ["`__call__` takes value and has no Args section", "`__call__` returns a value and has no Returns section"]
        )

    def test_a_public_constructor_with_parameters_needs_args(self) -> None:
        """A constructor that takes parameters names each one in its own Args section."""
        source = '''
            class Counter:
                """Counts."""

                def __init__(self, start: int) -> None:
                    """Starts counting from the given number."""
        '''
        self.assertEqual(_findings(source), ["`__init__` takes start and has no Args section"])

    def test_a_public_constructor_without_a_docstring_is_reported(self) -> None:
        """A constructor that takes parameters and says nothing is reported."""
        source = '''
            class Counter:
                """Counts."""

                def __init__(self, start: int) -> None:
                    self.start = start
        '''
        self.assertEqual(_findings(source), ["public `__init__` takes start and has no docstring"])

    def test_a_constructor_without_parameters_or_in_a_private_class_is_exempt(self) -> None:
        """No parameters to name, or a private class, needs no constructor docstring."""
        source = '''
            class Counter:
                """Counts."""

                def __init__(self) -> None:
                    self.start = 0


            class _Tally:
                def __init__(self, start: int) -> None:
                    self.start = start
        '''
        self.assertEqual(_findings(source), [])


class AgentTools(unittest.TestCase):
    """A function registered as an agent tool keeps the docstring the model reads, whatever its name."""

    def test_a_private_method_passed_to_add_function_keeps_its_sections(self) -> None:
        """The model reads this docstring, so the one-line rule for private methods does not apply."""
        source = '''
            class Port:
                """Holds the tools."""

                def toolset(self) -> None:
                    """Register the tools."""
                    contract.add_function(self._search, name="search_workflows")

                async def _search(self, task: str) -> str:
                    """Find workflows for a task.

                    Args:
                        task: What the workflow should do.
                    """
                    return task
        '''
        self.assertEqual(_findings(source), [])

    def test_a_decorated_tool_keeps_its_sections(self) -> None:
        """A `@agent.tool` function is a tool too."""
        source = '''
            @agent.tool
            async def _lookup(ctx: object, name: str) -> str:
                """Look a name up.

                Args:
                    name: The name.
                """
                return name
        '''
        self.assertEqual(_findings(source), [])

    def test_a_closure_its_factory_returns_keeps_its_sections(self) -> None:
        """A tool or step factory hands back the closure, whose docstring the model reads."""
        source = '''
            def search_tool(limit: int) -> object:
                """Build the search tool.

                Args:
                    limit: How many hits to return.

                Returns:
                    The tool.
                """

                async def search(task: str) -> str:
                    """Find workflows for a task.

                    Args:
                        task: What the workflow should do.
                    """
                    return task

                return search
        '''
        self.assertEqual(_findings(source), [])

    def test_a_function_passed_to_add_api_route_is_help_text(self) -> None:
        """A route registered by a call follows the route rule, like a decorated one."""
        source = '''
            async def change_events(request: object, topics: str = "") -> object:
                """Every change notification, as an event stream."""
                return request


            application.add_api_route("/api/events", change_events, methods=["GET"])
        '''
        self.assertEqual(_findings(source), [])

    def test_an_unregistered_private_function_still_gets_one_line(self) -> None:
        """Only a registered tool is left to the tool rule."""
        source = '''
            def _helper(value: int) -> int:
                """Doubles the value.

                Args:
                    value: The value.
                """
                return value * 2
        '''
        self.assertEqual(_findings(source), ["private `_helper` needs a one-line docstring or none"])


class ToolsAcrossModules(unittest.TestCase):
    """A tool registered in one module and defined in another keeps the docstring the model reads."""

    def test_a_tool_imported_and_registered_elsewhere_needs_no_returns(self) -> None:
        """The registering module and the defining module are checked in one run."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tools.py").write_text(textwrap.dedent('''
                async def message_worker(node_id: str) -> str:
                    """Send a message to the worker at `node_id`.

                    Args:
                        node_id: The graph node.
                    """
                    return node_id
            '''), encoding="utf-8")
            (root / "process.py").write_text(textwrap.dedent('''
                """Hosts the tools."""

                from tools import message_worker

                own.add_function(message_worker, name="message_worker")
            '''), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                status = check_docstrings.main([str(root)])
        self.assertEqual((status, printed.getvalue()), (0, ""))


class Prose(unittest.TestCase):
    """Docstrings and comments use plain punctuation."""

    def test_an_em_dash_and_a_semicolon_are_reported(self) -> None:
        """Both symbols are banned in prose."""
        source = '''
            """Holds the rules \u2014 all of them; nothing else."""
        '''
        self.assertEqual(_findings(source), ["banned symbol ';' '\u2014'"])

    def test_a_symbol_inside_a_code_span_passes(self) -> None:
        """A code span is not prose."""
        self.assertEqual(_findings('"""Runs `a; b` once."""\n'), [])

    def test_a_dash_clause_break_in_a_comment_is_reported(self) -> None:
        """A double hyphen between words is a clause break."""
        source = "value = 1  # the default -- never changed\n"
        self.assertEqual(_findings(source), ["dash used as a clause break"])

    def test_a_list_item_is_not_a_clause_break(self) -> None:
        """A hyphen at the start of a line opens a list item."""
        source = '''
            """The rules.

            - One idea per sentence.
            """
        '''
        self.assertEqual(_findings(source), [])

    def test_retired_words_need_the_vocabulary_flag(self) -> None:
        """A retired word is reported only when asked for."""
        source = '"""Writes into the vault."""\n'
        self.assertEqual(_findings(source), [])
        self.assertEqual(_findings(source, vocabulary=True), ["retired word `vault`, say knowledge base"])

    def test_a_parameter_name_in_args_is_not_prose(self) -> None:
        """An Args entry's name is an identifier, while its description is still prose."""
        source = '''
            def run(vault: str) -> None:
                """Run once.

                Args:
                    vault: The name to use.
                """
        '''
        self.assertEqual(_findings(source, vocabulary=True), [])
        described = source.replace("The name to use", "The vault to use")
        self.assertEqual(_findings(described, vocabulary=True), ["retired word `vault`, say knowledge base"])


class CommandLine(unittest.TestCase):
    """The exit status says whether anything was found."""

    def test_the_exit_status_is_one_with_findings_and_zero_without(self) -> None:
        """1 means findings, 0 means none."""
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / "bad.py"
            bad.write_text("def run() -> None:\n    pass\n", encoding="utf-8")
            good = Path(folder) / "good.py"
            good.write_text('"""Nothing to see."""\n', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(check_docstrings.main([str(bad)]), 1)
            self.assertIn("public `run` has no docstring", printed.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_docstrings.main([str(good)]), 0)


class DescriptionArguments(unittest.TestCase):
    """A `description=` or `help=` string is prose, read like a docstring."""

    def test_a_field_description_with_a_semicolon_is_reported(self) -> None:
        """A request field's description follows the punctuation rule."""
        source = '''
            from pydantic import BaseModel, Field


            class Body(BaseModel):
                """A request body."""

                name: str = Field(description="The name; never empty.")
        '''
        self.assertTrue(any("';'" in message for message in _findings(source)))

    def test_the_fixed_part_of_a_help_f_string_is_read(self) -> None:
        """The literal pieces of an f-string help text are prose too."""
        source = '''
            import typer

            LIMIT = 3


            def build(count: int = typer.Option(LIMIT, help=f"At most {LIMIT}; never more.")) -> None:
                """Build."""
        '''
        self.assertTrue(any("';'" in message for message in _findings(source)))

    def test_a_description_built_at_run_time_is_left_alone(self) -> None:
        """A description that is not written as a literal is not read."""
        source = '''
            from pydantic import Field

            TEXT = "The name; never empty."
            name: str = Field(description=TEXT)
        '''
        self.assertFalse(any("';'" in message for message in _findings(source)))


if __name__ == "__main__":
    unittest.main()
