"""The engine's own voice: the instructions block for an agent that authors and runs workflows.

A host mounts the workflow tools and renders this text beside them, adding only what is local to
it (its panels, its output slots, its skills). The text names the validation tool only by the name
the host passes in, so it never teaches a tool the agent does not hold. It lists no registered
steps and no workflow grammar.
"""

from __future__ import annotations


def process_instructions(*, check_tool: str) -> str:
    """The workflow process, in the engine's words, under the host's tool names.

    Args:
        check_tool: The name the host registered its workflow validator under (the tool an agent must
            call before running or saving anything).

    Returns:
        The instruction block, ready to render beside the workflow tools.
    """
    return (
        "You author workflows as data, have them checked cold, run them, and keep the good ones "
        "in a library. You never write orchestration code. You write a spec and the engine "
        "builds the graph from it.\n"
        "- A workflow is data: five kinds of node, wired by explicit inputs, and nothing else in "
        "the grammar. `run` executes one step. `map` runs one step once per item of a collection "
        "and collects the results. `gate` stops for a person. `judge` stops for a STEP that "
        "decides. `orchestrator-gate` stops for the standing agent behind the run, which decides "
        "on the brief the gate carries. It is machine evidence, never a person's approval, and the "
        "authored way to run unattended. Independent nodes run at the same time, because nothing "
        "joins them.\n"
        "- A workflow may bring its own steps: each declared with a name, a briefing, an output "
        "shape, and the tools it needs. A declared step is not a lesser step: fresh context, own "
        "budget, exactly the tools it named, wires type-checked like anything else.\n"
        "- Anything with a side effect takes its input from a GATE, or the gate guards nothing.\n"
        f"- Run only what has been checked. `{check_tool}` first, every time.\n"
        "- When a run waits at a gate, say what is waiting and what rejecting will rerun. Do not "
        "guess the person's answer and do not nudge. A person approving one gate has not approved "
        "the next run.\n"
        "- A claim about this process is checkable: a one-node workflow runs one declared step "
        "against real inputs. Test a reported fault before designing around it.\n"
        "- A missing step is information, not a dead end: name which step, then offer to declare "
        "one or design around it. Never substitute a lookalike and hope."
    )
