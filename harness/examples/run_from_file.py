"""Run a workflow straight from a file -- no store, no database, no model key needed.

Loads `verified_answer.md`, runs the electrician, indexes the spec to a pydantic_graph,
and executes it behind a console human gate.

The evidence leaf reads an exported knowledge base bundle by default (real knowledge base, no network),
or queries the live indexed knowledge base over ranked search on a read-only role (`--live`).
The drafting leaf is a pydantic-ai agent on an offline FunctionModel, so this demo runs
anywhere without a key.

    uv run python examples/run_from_file.py --yes
    uv run python examples/run_from_file.py --live --question "..." --yes
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import yaml

from core.ai.workflows.contracts import StepRegistry, load_spec_document, run_workflow, validate
from core.settings import read_infrastructure
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

# The engram checkout is a sibling of this package inside the monorepo, so both
# paths are structural rather than absolute guesses at where a home directory is.
ENGRAM_REPO = Path(__file__).resolve().parents[2] / "engram"
BUNDLE = ENGRAM_REPO / "bundles" / "research"
ENGRAM_SETTINGS = ENGRAM_REPO / "config" / "application_settings.json"
ARTIFACT = Path(__file__).parent / "verified_answer.md"
# The approved answer lands under the gitignored state home, so a run never rewrites a tracked file.
OUT_DIR = Path(__file__).resolve().parents[1] / ".state" / "examples"


class Counting:
    """A `RunObserver` that tallies how many times each node ran.

    The count lives out here rather than inside the leaves, and that is load-bearing rather
    than tidy: a leaf that keeps a counter mutates process state, which makes it something a
    durable engine must never re-execute, and `draft_answer` below is marked as exactly the
    opposite. Observation is a port the indexer already offers -- so the bookkeeping goes
    where bookkeeping goes, and the leaves stay honest about what they do to the world.
    """

    def __init__(self) -> None:
        self.runs: dict[str, int] = {}

    def started(self, node_id: str) -> None:
        """One more run of this node. Counted on the way in, so a node that failed still counts."""
        self.runs[node_id] = self.runs.get(node_id, 0) + 1

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        """Nothing to do: the tally is the whole of what this demo reports."""
        del node_id, failed


# --- the typed contracts the leaves speak ------------------------------------------


@dataclass(frozen=True)
class Snippet:
    """One ranked piece of evidence, with the source id a citation needs."""

    source_id: str
    title: str
    text: str


@dataclass(frozen=True)
class Evidence:
    """What the gathering leaf hands the drafter: ranked snippets, nothing else."""

    snippets: tuple[Snippet, ...]


@dataclass(frozen=True)
class Answer:
    """The drafted answer plus the source ids it claims to rest on."""

    text: str
    citations: list[str]


@dataclass(frozen=True)
class Published:
    """Where the approved answer landed and how many citations it carried."""

    path: str
    cited: int


# --- evidence leaves: engram's knowledge base, two transports -------------------------------


_MIN_TOKEN_CHARS = 3


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= _MIN_TOKEN_CHARS}


def _read_concept(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---\n", 4)
    front = yaml.safe_load(text[4:end]) if text.startswith("---\n") and end != -1 else {}
    return (front if isinstance(front, dict) else {}), text[end + 5 :]


async def gather_evidence_offline(question: str) -> Evidence:
    """Rank the exported knowledge base bundle lexically; snippets = description + first evidence bullet."""
    want = _tokens(question)
    scored: list[tuple[int, str, str, str]] = []
    for path in sorted(BUNDLE.glob("*/*.md")):
        front, body = _read_concept(path)
        title = str(front.get("title", path.stem))
        description = str(front.get("description", ""))
        aliases = front.get("aliases")
        alias_text = " ".join(str(a) for a in aliases) if isinstance(aliases, list) else ""
        score = len(want & _tokens(f"{title} {description} {alias_text}"))
        if score:
            bullet = next((line.strip("- ").strip() for line in body.splitlines() if line.startswith("- [")), "")
            engram_meta = front.get("okf_x_engram")
            source_id = str(engram_meta.get("id", path.stem)) if isinstance(engram_meta, dict) else path.stem
            scored.append((score, source_id, title, f"{description} {bullet}".strip()))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return Evidence(snippets=tuple(Snippet(source_id=s, title=t, text=x[:400]) for _, s, t, x in scored[:6]))


def _reader_dsn() -> str:
    url = read_infrastructure(ENGRAM_SETTINGS).database.reader_url
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


async def gather_evidence_live(question: str) -> Evidence:
    """Ranked search over the live indexed knowledge base, read-only role."""
    conn = await asyncpg.connect(_reader_dsn(), ssl="require", timeout=15)
    try:
        rows = await conn.fetch("select id, page_title, snippet from api.search($1, 6)", question)
    finally:
        await conn.close()
    return Evidence(snippets=tuple(Snippet(source_id=r["id"], title=r["page_title"], text=r["snippet"]) for r in rows))


# --- the agent leaf: fenced improvisation ------------------------------------------


def _offline_drafter() -> FunctionModel:
    drafts = 0

    def drafter(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        nonlocal drafts
        drafts += 1
        prompt = " ".join(str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart))
        cited = re.findall(r"\[src:([^\]]+)\] ([^:]+) ::", prompt)[:3]
        question = prompt.split("Question:", 1)[-1].split("Evidence:", 1)[0].strip()
        prefix = "Revised draft" if drafts > 1 else "Draft"
        lines = [f"{prefix}: {question}"] + [f"- {title.strip()} [{source_id}]" for source_id, title in cited]
        args = {"text": "\n".join(lines), "citations": [source_id for source_id, _ in cited]}
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=args)])

    return FunctionModel(drafter)


drafting_agent: Agent[None, Answer] = Agent(
    _offline_drafter(),
    name="drafter",
    output_type=Answer,
    instructions="Answer the question strictly from the evidence; cite source ids for every claim.",
)
"""The drafting agent, and the other half of `draft_answer`'s `replay_safe=True`.

The marker is half a claim on its own: a leaf whose body is safe to re-run still repays its whole
agent loop unless the agent journals its own model requests. Which capability does that journaling
is the engine's business, so it is attached where the engine is chosen -- the studio's composition
root, for every agent it builds -- and no engine is named in this file. Here the run happens in
this process, where there is no replay to survive and nothing to attach, which is why the demo
still needs no server, no queue and no key."""


async def draft_answer(question: str, evidence: Evidence) -> Answer:
    """Draft an answer from the evidence, citing source ids for every claim."""
    prompt = f"Question: {question}\n\nEvidence:\n" + "\n".join(f"[src:{s.source_id}] {s.title} :: {s.text}" for s in evidence.snippets)
    result = await drafting_agent.run(prompt)
    return result.output


# --- the gated side effect -----------------------------------------------------------


async def publish_answer(answer: Answer) -> Published:
    """Write the approved answer to disk -- the side effect the gate guards."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "answer.md"
    front = yaml.safe_dump(
        {"generated": {"by": "process:verified-answer/1", "at": "2026-07-30"}, "citations": answer.citations}, sort_keys=False
    )
    path.write_text(f"---\n{front}---\n\n{answer.text}\n", encoding="utf-8")
    return Published(path=str(path), cited=len(answer.citations))


# --- the human gate -------------------------------------------------------------------


class ConsoleApproval:
    """The human in the loop.

    `reject_first` scripts one rejection so the rewind is visible: the draft node
    re-runs, evidence gathering does not, and nothing publishes until a human says yes.
    """

    def __init__(self, *, auto: bool, reject_first: bool = False) -> None:
        self.auto = auto
        self.pending_rejections = 1 if reject_first else 0
        self.reviews = 0

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        """Show the gated value and collect the verdict (scripted, auto, or typed)."""
        del note
        self.reviews += 1
        print(f"\n--- gate {gate_id!r} asks for review (#{self.reviews}) ---\n{getattr(value, 'text', value)}\n")
        if self.pending_rejections:
            self.pending_rejections -= 1
            print("(scripted rejection -- rewinding to the drafting node)")
            return False
        if self.auto or not sys.stdin.isatty():
            print("(auto-approved)")
            return True
        reply = await asyncio.to_thread(input, "approve? [y/N] ")
        return reply.strip().lower() == "y"


# --- main ------------------------------------------------------------------------------


async def main() -> int:
    """Load, validate, index, and run the artifact behind a console gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default="Which wins on multi-hop QA benchmarks, GraphRAG or LightRAG, and on what evidence?")
    parser.add_argument("--live", action="store_true", help="query the live knowledge base database instead of the exported OKF bundle")
    parser.add_argument("--yes", action="store_true", help="auto-approve the human gate")
    parser.add_argument("--reject-first", action="store_true", help="reject once, to show the gate's rewind loop")
    args = parser.parse_args()

    loaded = load_spec_document(ARTIFACT.read_text(encoding="utf-8"))
    print(f"artifact : {loaded.frontmatter.get('title', '(untitled)')}")

    registry = StepRegistry()
    # Reads a file (or a database) and returns what it found. Re-reading is not free of
    # consequence -- the knowledge base may have moved on -- so it stays an activity, recorded once.
    registry.register(gather_evidence_live if args.live else gather_evidence_offline, name="gather_evidence")
    # A prompt and one agent run, and the agent journals every model request of its own.
    registry.register(draft_answer, replay_safe=True)
    # Writes a file. Running it twice writes it twice, which no history would show.
    registry.register(publish_answer)

    spec = loaded.workflow
    report = validate(spec, registry)
    if not report.ok:
        for issue in report.issues:
            print(f"WIRING   : [{issue.kind}] {issue.where}: {issue.detail}")
        return 1
    print(f"wiring   : ok ({len(spec.nodes)} nodes, electrician found nothing)")
    print(f"evidence : {'indexed knowledge base, live ranked search' if args.live else f'exported bundle, offline ({BUNDLE.name})'}")

    approval = ConsoleApproval(auto=args.yes, reject_first=args.reject_first)
    counting = Counting()
    result = await run_workflow(spec, registry, params={"question": args.question}, approval=approval, observer=counting)
    if not isinstance(result, Published):
        msg = f"workflow returned {type(result).__name__}, expected Published"
        raise TypeError(msg)
    print(f"\ngate      : {approval.reviews} review(s) before publication")
    print(f"node runs : {', '.join(f'{node}={count}' for node, count in counting.runs.items())}")
    print(f"published: {result.path} ({result.cited} citations)")
    print(await asyncio.to_thread(Path(result.path).read_text, encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
