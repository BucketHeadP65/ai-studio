"""Unit specs for the policy-delivery core (`engram.evals.harness.domain.delivery` and `engram.evals.harness.infrastructure.policy_files`).

`evals apply-policy` writes a winning tuned `RetrievalPolicy` into its three channels, the
in-code `DEFAULT_INSTRUCTIONS` default, the shipped `searching-the-knowledge-base` skill's managed section,
and a per-knowledge-base active-policy YAML, all in the working tree and never through git. These specs pin
the two pure rewriters (idempotent, sentinel-guarded, round-tripping to a valid module) and the
`apply_policy` orchestration over `tmp_path` copies, including its idempotency and its no-git stance.
"""

from __future__ import annotations

import importlib.util
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from polyfactory.factories.pydantic_factory import ModelFactory

from engram.evals.harness.contracts import ApplyReport, apply_policy
from engram.evals.harness.domain import delivery as domain_delivery
from engram.evals.harness.domain.delivery import _BEGIN, _END, replace_managed_section, rewrite_default_instructions
from engram.evals.tuning import policy as policy_module
from engram.evals.tuning.policy import RetrievalPolicy, baseline_policy, load_policy, save_policy
from engram.evals.tuning.strategy import DEFAULT_STRATEGY


class _BaselineView(Protocol):
    """The two `str` fields the round-trip reads off a loaded module's `baseline_policy()` result.

    A freshly-imported `policy.py` defines its own `RetrievalPolicy` class (a distinct class object
    from the test's import), so the loaded baseline is matched structurally on these fields rather
    than with a nominal `isinstance` against the test's class.
    """

    @property
    def instructions(self) -> str: ...

    @property
    def strategy(self) -> str: ...


class _PolicyModuleSurface(Protocol):
    """The slice of a freshly-imported `policy.py` the rewrite round-trip asserts on."""

    @property
    def default_instructions(self) -> str: ...

    def baseline_policy(self) -> _BaselineView: ...


class RetrievalPolicyFactory(ModelFactory[RetrievalPolicy]):
    """A `RetrievalPolicy` with an in-set strategy (polyfactory's random string would fail the validator)."""

    strategy = DEFAULT_STRATEGY.value


def _skill_fixture(inner: str) -> str:
    """A SKILL.md-shaped document carrying the sentinel block around `inner`, with surrounding text."""
    return f"# Searching the Memory\n\nIntro paragraph.\n\n{_BEGIN}\n{inner}\n{_END}\n\n## Retrieval surface\n\nTrailing.\n"


class TestReplaceManagedSection:
    def test_replaces_the_inner_content_between_the_sentinels(self):
        text = _skill_fixture("OLD instructions")

        result = replace_managed_section(text, "NEW instructions")

        assert "NEW instructions" in result
        assert "OLD instructions" not in result

    def test_keeps_the_sentinel_lines(self):
        text = _skill_fixture("OLD")

        result = replace_managed_section(text, "NEW")

        assert _BEGIN in result
        assert _END in result

    def test_preserves_the_surrounding_text_exactly(self):
        text = _skill_fixture("OLD")

        result = replace_managed_section(text, "NEW")

        assert result.startswith("# Searching the Memory\n\nIntro paragraph.\n\n")
        assert result.endswith("\n\n## Retrieval surface\n\nTrailing.\n")

    def test_is_idempotent_applying_twice_equals_once(self):
        text = _skill_fixture("OLD")

        once = replace_managed_section(text, "NEW")
        twice = replace_managed_section(once, "NEW")

        assert twice == once

    def test_content_with_a_backslash_is_emitted_verbatim(self):
        # A re.sub template replacement would interpret a backslash escape; the content must survive.
        text = _skill_fixture("OLD")

        result = replace_managed_section(text, r"Use C:\path and a literal \n in the prose.")

        assert r"Use C:\path and a literal \n in the prose." in result

    def test_raises_a_clear_value_error_when_the_sentinels_are_absent(self):
        with pytest.raises(ValueError, match="sentinel"):
            replace_managed_section("# Heading\n\nNo managed block here.\n", "NEW")


class TestRewriteDefaultInstructions:
    def test_round_trips_to_a_module_with_the_new_constant(self, tmp_path: Path):
        new = "Rewritten instructions: cite the page ids you used."
        source = Path(policy_module.__file__).read_text(encoding="utf-8")

        rewritten = rewrite_default_instructions(source, new)
        loaded = _load_module(tmp_path / "rewritten_policy.py", rewritten)

        assert new == loaded.default_instructions

    def test_the_rewritten_module_still_builds_a_baseline_policy(self, tmp_path: Path):
        # A mix of single and double quotes plus a newline, so repr() must escape to a valid literal.
        new = "Rewritten: cite the \"page ids\" you 'relied on'.\nAnswer concisely."
        source = Path(policy_module.__file__).read_text(encoding="utf-8")

        rewritten = rewrite_default_instructions(source, new)
        loaded = _load_module(tmp_path / "escaped_policy.py", rewritten)

        assert loaded.baseline_policy().instructions == new
        assert loaded.baseline_policy().strategy == DEFAULT_STRATEGY.value

    def test_rewrites_a_parenthesized_literal_that_contains_an_inner_paren(self):
        # A non-greedy match to the FIRST ) would truncate this assignment mid-literal (at the inner
        # `(verbatim)`), leaving an orphaned tail and a syntactically broken module. The terminator
        # must anchor on the assignment's real closing paren (a line-start `)`), not an inner one.
        source = (
            "OTHER = 1\n"
            "DEFAULT_INSTRUCTIONS = (\n"
            '    "Answer from the retrieved context only. "\n'
            '    "Cite the page ids (verbatim) you relied on."\n'
            ")\n"
            "TRAILING = 2\n"
        )

        result = rewrite_default_instructions(source, "NEW")

        assert "DEFAULT_INSTRUCTIONS = 'NEW'" in result
        assert "verbatim" not in result
        assert "Answer from the retrieved context" not in result
        assert "OTHER = 1\n" in result
        assert "TRAILING = 2\n" in result
        compile(result, "policy.py", "exec")

    def test_raises_a_clear_value_error_when_the_assignment_is_absent(self):
        with pytest.raises(ValueError, match="DEFAULT_INSTRUCTIONS"):
            rewrite_default_instructions("X = 1\n", "anything")


class TestActivePolicyRoundTrip:
    def test_save_then_load_is_identity(self, tmp_path: Path):
        policy = RetrievalPolicyFactory.build()
        path = tmp_path / "active-policy.yaml"

        save_policy(policy, path)

        assert load_policy(path) == policy


class TestApplyPolicy:
    def test_writes_all_three_targets_with_the_policy_instructions(self, tmp_path: Path):
        policy = baseline_policy().model_copy(update={"instructions": "Tuned: cite your page ids."})
        targets = _stage_targets(tmp_path)

        report = apply_policy(
            policy,
            policy_module_path=targets.policy_module_path,
            skill_md_path=targets.skill_md_path,
            active_policy_path=targets.active_policy_path,
        )

        assert isinstance(report, ApplyReport)
        loaded = _load_module(tmp_path / "applied_policy.py", targets.policy_module_path.read_text(encoding="utf-8"))
        assert loaded.default_instructions == "Tuned: cite your page ids."
        assert "Tuned: cite your page ids." in targets.skill_md_path.read_text(encoding="utf-8")
        assert load_policy(targets.active_policy_path) == policy

    def test_report_names_the_three_targets(self, tmp_path: Path):
        policy = baseline_policy()
        targets = _stage_targets(tmp_path)

        report = apply_policy(
            policy,
            policy_module_path=targets.policy_module_path,
            skill_md_path=targets.skill_md_path,
            active_policy_path=targets.active_policy_path,
        )

        assert report.instructions_target == targets.policy_module_path
        assert report.skill_target == targets.skill_md_path
        assert report.active_policy_target == targets.active_policy_path

    def test_keeps_the_skill_surrounding_text_intact(self, tmp_path: Path):
        policy = baseline_policy().model_copy(update={"instructions": "Tuned."})
        targets = _stage_targets(tmp_path)

        apply_policy(
            policy,
            policy_module_path=targets.policy_module_path,
            skill_md_path=targets.skill_md_path,
            active_policy_path=targets.active_policy_path,
        )

        skill = targets.skill_md_path.read_text(encoding="utf-8")
        assert skill.startswith("# Searching the Memory\n\nIntro paragraph.\n\n")
        assert skill.endswith("\n\n## Retrieval surface\n\nTrailing.\n")

    def test_is_idempotent_a_second_apply_reproduces_the_same_files(self, tmp_path: Path):
        policy = baseline_policy().model_copy(update={"instructions": "Tuned twice over."})
        targets = _stage_targets(tmp_path)

        apply_policy(
            policy,
            policy_module_path=targets.policy_module_path,
            skill_md_path=targets.skill_md_path,
            active_policy_path=targets.active_policy_path,
        )
        first_module = targets.policy_module_path.read_text(encoding="utf-8")
        first_skill = targets.skill_md_path.read_text(encoding="utf-8")
        first_active = targets.active_policy_path.read_text(encoding="utf-8")

        apply_policy(
            policy,
            policy_module_path=targets.policy_module_path,
            skill_md_path=targets.skill_md_path,
            active_policy_path=targets.active_policy_path,
        )

        assert targets.policy_module_path.read_text(encoding="utf-8") == first_module
        assert targets.skill_md_path.read_text(encoding="utf-8") == first_skill
        assert targets.active_policy_path.read_text(encoding="utf-8") == first_active

    def test_a_skill_without_the_managed_block_refuses_before_any_file_is_written(self, tmp_path: Path):
        policy = baseline_policy().model_copy(update={"instructions": "Tuned but refused."})
        targets = _stage_targets(tmp_path)
        targets.skill_md_path.write_text("# Searching the Memory\n\nNo managed block here.\n", encoding="utf-8")
        module_before = targets.policy_module_path.read_text(encoding="utf-8")

        with pytest.raises(ValueError, match="managed-section sentinels not found"):
            apply_policy(
                policy,
                policy_module_path=targets.policy_module_path,
                skill_md_path=targets.skill_md_path,
                active_policy_path=targets.active_policy_path,
            )

        assert targets.policy_module_path.read_text(encoding="utf-8") == module_before
        assert not targets.active_policy_path.exists()


class _StagedTargets:
    """The three working-tree paths an `apply_policy` over `tmp_path` writes (copies of the real files)."""

    def __init__(self, policy_module_path: Path, skill_md_path: Path, active_policy_path: Path) -> None:
        self.policy_module_path = policy_module_path
        self.skill_md_path = skill_md_path
        self.active_policy_path = active_policy_path


def _stage_targets(tmp_path: Path) -> _StagedTargets:
    """Copy the real `policy.py` and seed a SKILL.md fixture under `tmp_path`, plus an active-policy path."""
    policy_copy = tmp_path / "policy.py"
    policy_copy.write_text(Path(policy_module.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    skill_copy = tmp_path / "SKILL.md"
    skill_copy.write_text(_skill_fixture("Baseline instructions seed."), encoding="utf-8")
    return _StagedTargets(policy_copy, skill_copy, tmp_path / "_meta" / "active-policy.yaml")


@dataclass(frozen=True)
class _LoadedPolicyModule:
    """A typed `_PolicyModuleSurface` extracted from a dynamically-imported `policy.py` copy.

    Crossing the untyped `ModuleType` boundary once, here, narrowing the two values the round-trip
    asserts on (the default-instructions string and the constructed baseline) so the rest of the test
    stays statically typed (no `object`, no `cast`).
    """

    default_instructions: str
    _baseline_instructions: str
    _baseline_strategy: str

    def baseline_policy(self) -> _BaselineView:
        """The loaded module's baseline policy view (its `instructions` and `strategy`, narrowed at load)."""
        return _LoadedBaseline(instructions=self._baseline_instructions, strategy=self._baseline_strategy)


@dataclass(frozen=True)
class _LoadedBaseline:
    """The structural `_BaselineView` narrowed from a loaded module's `baseline_policy()` result."""

    instructions: str
    strategy: str


def _load_module(path: Path, source: str) -> _PolicyModuleSurface:
    """Write `source` to `path`, import it, and return its typed `policy.py` surface (the round-trip).

    Registers the module in `sys.modules` under its spec name before executing it, so the rewritten
    `policy.py`'s deferred pydantic annotations (`RetrievalPolicy`'s `Mapping[...]`) resolve against
    the module's own globals and `baseline_policy()` constructs cleanly. The two values the round-trip
    asserts on are narrowed off the module into a typed `_LoadedPolicyModule`.
    """
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:  # pragma: no cover (a real .py path always yields a loader spec)
        msg = f"could not build an import spec for {path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    default_instructions = module.DEFAULT_INSTRUCTIONS
    baseline = module.baseline_policy()
    instructions = getattr(baseline, "instructions", None)
    strategy = getattr(baseline, "strategy", None)
    if not isinstance(default_instructions, str) or not isinstance(instructions, str) or not isinstance(strategy, str):  # pragma: no cover
        msg = f"loaded module at {path} is not a policy module surface"
        raise TypeError(msg)
    return _LoadedPolicyModule(default_instructions=default_instructions, _baseline_instructions=instructions, _baseline_strategy=strategy)


class TestApplyPolicyHistorySnapshot:
    """The human apply shares the runtime rung's history discipline."""

    def test_apply_snapshots_the_outgoing_active_policy(self, tmp_path: Path):
        staged = _stage_targets(tmp_path)
        staged.active_policy_path.parent.mkdir(parents=True, exist_ok=True)
        save_policy(baseline_policy().model_copy(update={"instructions": "OLD"}), staged.active_policy_path)
        incoming = baseline_policy().model_copy(update={"instructions": "NEW"})

        apply_policy(
            incoming,
            policy_module_path=staged.policy_module_path,
            skill_md_path=staged.skill_md_path,
            active_policy_path=staged.active_policy_path,
        )

        snapshot = tmp_path / "_meta" / "policy-history" / "active-policy.v1.yaml"
        assert load_policy(snapshot).instructions == "OLD"  # the outgoing policy is preserved
        assert load_policy(staged.active_policy_path).instructions == "NEW"

    def test_first_apply_creates_no_history(self, tmp_path: Path):
        staged = _stage_targets(tmp_path)

        apply_policy(
            baseline_policy().model_copy(update={"instructions": "NEW"}),
            policy_module_path=staged.policy_module_path,
            skill_md_path=staged.skill_md_path,
            active_policy_path=staged.active_policy_path,
        )

        assert not (tmp_path / "_meta" / "policy-history").exists()


def test_the_policy_file_writer_lives_in_infrastructure_and_the_domain_writes_no_files() -> None:
    """`apply_policy` and `ApplyReport` come from the infrastructure module, and the domain rewriters touch no file."""
    assert apply_policy.__module__ == "engram.evals.harness.infrastructure.policy_files"
    assert ApplyReport.__module__ == "engram.evals.harness.infrastructure.policy_files"
    domain_source = Path(domain_delivery.__file__).read_text(encoding="utf-8")
    assert "write_text" not in domain_source
    assert "read_text" not in domain_source
    assert "engram.kb" not in domain_source
