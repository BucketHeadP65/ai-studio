#!/usr/bin/env python3
"""Split an architecture master into per-H2 chunks + a retrieval index.

The master (<dir>/architecture.md) is the single source of truth. This script
regenerates <dir>/sections/NN-slug.md and <dir>/index.md from it. Workflow for
edits: edit the master, re-run `python3 scripts/docs/split_doc.py --dir <dir>`,
never edit generated files.

Ported from the Knowledge vault's _meta/design/architecture/split_doc.py,
parametrized with --dir so the root master and graduated app masters share one
script (see docs/adr/0005-docs-as-code-standard.md).

Conventions the script relies on (per section):
  - sections are H2 headings (`## N. Title`) outside code fences
  - first blockquote line `> **TL;DR:** ...` -> lifted into the index
  - an HTML comment `<!-- load-when: ... -->` -> lifted into the index
Missing TL;DR or load-when on any section -> exit 1.
"""

import argparse
import re
import sys
from pathlib import Path


def generated_note(rel_dir):
    return (
        "<!-- GENERATED from architecture.md by scripts/docs/split_doc.py "
        f"--dir {rel_dir} — do not edit here; edit the master and re-run the "
        "script. -->"
    )


def strip_frontmatter(lines):
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                return lines[j + 1 :]
    return lines


def split_sections(lines):
    """Return (preamble_lines, [(heading_text, section_lines)]), fence-aware."""
    in_fence = False
    preamble, sections, current = [], [], None
    for ln in lines:
        if re.match(r"^(```|~~~)", ln.strip()):
            in_fence = not in_fence
        if not in_fence and ln.startswith("## ") and not ln.startswith("###"):
            current = (ln[3:].strip(), [ln])
            sections.append(current)
        elif current is not None:
            current[1].append(ln)
        else:
            preamble.append(ln)
    return preamble, sections


def slugify(heading, max_len=56):
    h = re.sub(r"^\d+\.\s*", "", heading)
    h = h.replace("—", " ").replace("–", " ").replace("`", "")
    h = re.sub(r"[^a-z0-9 -]", "", h.lower())
    h = re.sub(r"\s+", "-", h.strip())
    h = re.sub(r"-{2,}", "-", h)
    if len(h) > max_len:  # cut at a word boundary, not mid-word
        h = h[:max_len].rsplit("-", 1)[0]
    return h.rstrip("-")


def tldr_of(body):
    for ln in body:
        m = re.match(r">\s*\*\*TL;DR:?\*\*:?\s*(.+)", ln)
        if m:
            return m.group(1).strip()
    return ""


def load_when_of(body):
    for ln in body:
        m = re.search(r"<!--\s*load-when:\s*(.+?)\s*-->", ln)
        if m:
            return m.group(1).strip()
    return ""


def approx_tokens(text):
    return int(len(text.split()) * 1.33)


# Matches relative markdown link targets — not absolute URLs, anchors,
# protocol links, or root-absolute paths.
_REL_LINK = re.compile(r"\]\((?!(?:[a-z]+:|#|/))([^)]+)\)")


def reparent_links(body_lines):
    """Rewrite master-relative links for a chunk one directory deeper.

    Chunks live in <dir>/sections/ while the master's links resolve from
    <dir>/, so every relative target needs a ../ prefix. Fence-aware: lines
    inside code fences are left untouched.
    """
    out, in_fence = [], False
    for ln in body_lines:
        if re.match(r"^(```|~~~)", ln.strip()):
            in_fence = not in_fence
            out.append(ln)
            continue
        out.append(ln if in_fence else _REL_LINK.sub(r"](../\1)", ln))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Split an architecture master into per-H2 chunks + a retrieval index."
    )
    ap.add_argument(
        "--dir",
        required=True,
        type=Path,
        help="split-doc scope: master = DIR/architecture.md, "
        "out = DIR/sections/, index = DIR/index.md",
    )
    args = ap.parse_args()

    root = args.dir.resolve()
    master = root / "architecture.md"
    out = root / "sections"
    index = root / "index.md"
    if not master.is_file():
        sys.exit(f"FATAL: master not found: {master}")
    repo_root = Path(__file__).resolve().parent.parent.parent
    try:
        rel_dir = root.relative_to(repo_root).as_posix()
    except ValueError:  # scope outside the repo — show the absolute path
        rel_dir = str(root)
    banner = generated_note(rel_dir)

    md = master.read_text(encoding="utf-8")
    lines = strip_frontmatter(md.splitlines())
    expected = 0
    in_fence = False
    for ln in lines:
        if re.match(r"^(```|~~~)", ln.strip()):
            in_fence = not in_fence
        if not in_fence and ln.startswith("## ") and not ln.startswith("###"):
            expected += 1

    preamble, sections = split_sections(lines)
    if len(sections) != expected:
        sys.exit(f"FATAL: heading scan ({expected}) != sections split ({len(sections)})")

    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.md"):
        stale.unlink()

    entries, problems = [], []
    for n, (heading, body) in enumerate(sections, start=1):
        slug = slugify(heading)
        fname = f"{n:02d}-{slug}.md"
        text = "\n".join(reparent_links(body)).strip() + "\n"
        (out / fname).write_text(banner + "\n\n" + text, encoding="utf-8")
        tldr, lw = tldr_of(body), load_when_of(body)
        if not tldr:
            problems.append(f"{fname}: missing TL;DR line")
        if not lw:
            problems.append(f"{fname}: missing load-when comment")
        entries.append((n, heading, fname, approx_tokens(text), tldr, lw))

    # --- index.md ---
    o = [banner, "", "# Architecture doc — section index", ""]
    o += [
        "**Master:** [architecture.md](architecture.md) — the single source of",
        "truth. Edit it, then run `python3 scripts/docs/split_doc.py --dir "
        f"{rel_dir}` to regenerate `sections/` and this index.",
        "",
        "**Purpose of this index:** when a question touches specific points of",
        "the design, load ONLY the matching `sections/` chunk(s) below instead",
        "of the whole document. Match against the *Load when* keywords; token",
        "counts say what each load costs.",
        "",
    ]
    # carry the master's preamble (orientation / how-to-read) into the index
    pre = [ln for ln in preamble if not ln.startswith("# ") and ln.strip() != "---"]
    pre_text = "\n".join(pre).strip()
    if pre_text:
        o += ["## Orientation (from the master)", "", pre_text, ""]
    total = sum(e[3] for e in entries)
    o.append(f"Sections: {len(entries)} · whole document ≈ {total:,} tokens.")
    o.append("")
    for n, heading, fname, tok, tldr, lw in entries:
        title = re.sub(r"^\d+\.\s*", "", heading)
        o.append(f"### {n:02d} — {title}")
        o.append(f"[sections/{fname}](sections/{fname}) · ~{tok:,} tokens")
        if tldr:
            o.append(f"- **TL;DR:** {tldr}")
        if lw:
            o.append(f"- **Load when:** {lw}")
        o.append("")
    index.write_text("\n".join(o).rstrip() + "\n", encoding="utf-8")

    print(f"master   : {master} ({approx_tokens(md):,} tokens approx)")
    print(f"chunks   : {len(entries)} -> {out}/")
    print(f"index    : {index} (total ≈ {total:,} tokens across chunks)")
    for p in problems:
        print(f"WARN     : {p}")
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
