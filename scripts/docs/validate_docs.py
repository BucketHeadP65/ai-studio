#!/usr/bin/env python3
"""Validate the governed docs knowledge base against `docs/knowledge-base-schema.json` (see docs/README.md for the standard).

Checks:
  1. Every governed doc has frontmatter with a valid `type`, and the per-kind required fields
     (base `id`/`type`/`title`/`status` plus any field the schema marks `required` for that
     kind, e.g. `app` on Diataxis kinds, `members` on hubs, `path` on modules).
  2. `status` is one of the kind's declared `status_values`; any frontmatter field with declared
     `choices` (e.g. `app`) is one of them.
  3. `id` is unique across the whole knowledge base.
  4. `id` is reachable the way Obsidian resolves `[[id]]` links: equal to the file's own filename
     stem, or listed in that file's `aliases`.
  5. Every relative markdown link in CONTEXT-MAP.md resolves to a real file.
  6. The vocabulary: no retired word (docs/initiatives/2026-09-02-vocabulary/design.md section 4.7) and no
     banned symbol (em dash, en dash, semicolon, middle dot, arrows, times, section sign) in the prose of a
     live doc, a CLAUDE.md, a README, the plugin's pages, a skill file, a template's guidance page, a workflow
     or step file, a proof page, the repo-docs knowledge base's own _meta pages, or a user-visible string in
     studio-client/src. Not prose, so never a finding: code spans, link targets, URLs, file paths, HTML comments,
     double-quoted literal text, frontmatter identifiers, a step's program block and schema lines, and the names
     of outside things. ADRs, frozen history, executed initiatives, the memory pages and the vocabulary
     initiative itself are not linted for words, they record the past or name the retired words on purpose.

Governed set: docs/** and <app>/docs/**, plus CONTEXT-MAP.md and every CONTEXT.md, including
the lifecycle dirs `docs/brainstorming/`, `docs/specs/`, `docs/plans/`, and `docs/memory/Units/`
(all governed page types now; see docs/knowledge-base-schema.json). Excluded: `docs/implementation/`
(workflow state, never documentation), `docs/_meta/` (the vocabulary pages, their own
grammar, claimed by no page type), `docs/superpowers/` (legacy workflow state), generated
split-doc artifacts (`sections/` chunks and index.md files opening with a GENERATED banner), and
`node_modules`.

Stdlib-only on purpose — no venv needed. Exit 0 clean, exit 1 with findings.
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# Apps are the top-level dirs that may own a docs/ tree + CONTEXT.md.
APPS = ["core", "engram", "studio", "studio-client", "client-core", "harness"]

SPEC = json.loads((REPO / "docs" / "knowledge-base-schema.json").read_text(encoding="utf-8"))
PAGE_TYPES = {pt["page_type"]: pt for pt in SPEC["page_types"]}
TYPES = set(PAGE_TYPES)

EXCLUDED_PARTS = {"implementation", "superpowers", "sections", "node_modules"}
# The repo-docs registry pages live ONLY at the repo-root `docs/_meta/` — a path-PREFIX test,
# not a bare part-name exclusion. A bare `"_meta"` in EXCLUDED_PARTS would silently exempt any
# directory anywhere in the governed set with that name, leaving it out of governance with no
# signal (the class of drift this replaces).
_CORPUS_PREFIXES = (("docs", "_meta"),)


def allowed_statuses(kind):
    """Return the kind's declared status vocabulary, or the eight grammar defaults."""
    values = PAGE_TYPES.get(kind, {}).get("status_values", [])
    return set(values) if values else {"stub", "draft", "synthesized", "verified", "active", "superseded", "outdated", "pending"}


def required_fields(kind):
    """Return the kind's required frontmatter keys mapped to their schema field type.

    The base four (`id`, `type`, `title`, `status`) are always `scalar`. Declared required
    fields carry whatever `type` the schema gives them (`scalar` or `link-list`) — a
    link-list (e.g. a hub's `members`) is authored as a YAML block with an empty inline value,
    so presence alone satisfies it; a required scalar must carry a non-empty value, same as the
    base four.
    """
    base = {name: "scalar" for name in ("id", "type", "title", "status")}
    declared = {f["name"]: f.get("type", "scalar") for f in PAGE_TYPES.get(kind, {}).get("frontmatter_fields", []) if f.get("required")}
    return base | declared


def governed_files():
    roots = [REPO / "docs"] + [REPO / a / "docs" for a in APPS]
    files = []
    for root in roots:
        if root.is_dir():
            files += sorted(root.rglob("*.md"))
    for extra in [REPO / "CONTEXT-MAP.md"] + [REPO / a / "CONTEXT.md" for a in APPS]:
        if extra.is_file():
            files.append(extra)
    out = []
    for f in files:
        rel = f.relative_to(REPO)
        if EXCLUDED_PARTS & set(rel.parts):
            continue
        if any(rel.parts[: len(prefix)] == prefix for prefix in _CORPUS_PREFIXES):
            continue
        if f.read_text(encoding="utf-8").lstrip()[:14] == "<!-- GENERATED":
            continue  # split-doc artifact — regenerated, carries no frontmatter
        out.append(f)
    return out


def parse_frontmatter(text):
    """Flat key: value frontmatter parser (no pyyaml dependency).

    Each matched `key: value` line lands in the dict, including a key declared as a YAML block
    (e.g. `members:` with items on following `- ` lines, which this parser doesn't descend into)
    — its value is `""`, but the key's *presence* is preserved. Block items themselves
    (`  - ...`) don't match the `key:` pattern and are skipped, except under `aliases`.
    `aliases` is special-cased into a list of strings from either form it is written in: the
    inline JSON-ish array (`["x", "y"]`) a hand-authored doc carries, or the block (`aliases:`
    followed by `- x` lines) engram's renderer writes. Every other key stays a scalar string, so
    `fm[key] == ""` is ambiguous on its own — it means either "declared as a block, value lives in
    the following lines" (fine for a link-list field) or "declared inline and left blank" (never
    fine for a scalar field). Callers must resolve that ambiguity using the field's own declared
    type (`required_fields` carries it), not the parsed value alone.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fm = {}
    alias_block = False
    for ln in lines[1:]:
        if ln.strip() == "---":
            return fm
        if alias_block:
            item = re.match(r"^\s*-\s+(.*?)\s*$", ln)
            if item:
                fm["aliases"].append(item.group(1).strip("\"'"))
                continue
            alias_block = False
        m = re.match(r"^([A-Za-z_-]+):\s*(.*?)\s*(#.*)?$", ln)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip("\"'")
        if key == "aliases":
            alias_block = m.group(2).strip() == ""
            value = [item.strip().strip("\"'") for item in value.strip("[]").split(",") if item.strip()]
        fm[key] = value
    return None  # unterminated frontmatter block


def check_frontmatter(files, problems):
    ids = {}
    for f in files:
        rel = f.relative_to(REPO)
        fm = parse_frontmatter(f.read_text(encoding="utf-8"))
        if fm is None:
            problems.append(f"{rel}: missing or unterminated frontmatter")
            continue
        kind = fm.get("type", "")
        if kind and kind not in TYPES:
            problems.append(f"{rel}: unknown type `{kind}` (allowed: {sorted(TYPES)})")
        for key, field_type in required_fields(kind).items():
            if key not in fm:
                problems.append(f"{rel}: frontmatter missing `{key}`")
            elif field_type != "link-list" and not fm[key]:
                problems.append(f"{rel}: frontmatter missing `{key}`")
        status = fm.get("status", "")
        if status and status.lower() not in allowed_statuses(kind):
            problems.append(f"{rel}: unknown status `{status}` for type `{kind}` (allowed: {sorted(allowed_statuses(kind))})")
        for field in PAGE_TYPES.get(kind, {}).get("frontmatter_fields", []):
            choices = field.get("choices")
            value = fm.get(field["name"], "")
            if choices and value and value not in choices:
                problems.append(f"{rel}: unknown {field['name']} `{value}` for type `{kind}` (allowed: {sorted(choices)})")
        doc_id = fm.get("id", "")
        if doc_id:
            aliases = fm.get("aliases", [])
            if doc_id != f.stem and doc_id not in aliases:
                problems.append(f"{rel}: id `{doc_id}` unreachable — must equal the filename stem (`{f.stem}`) or appear in `aliases`")
            if doc_id in ids:
                problems.append(f"{rel}: duplicate id `{doc_id}` (also in {ids[doc_id]})")
            else:
                ids[doc_id] = rel


def check_context_map(problems):
    cmap = REPO / "CONTEXT-MAP.md"
    if not cmap.is_file():
        problems.append("CONTEXT-MAP.md: missing at repo root")
        return
    text = cmap.read_text(encoding="utf-8")
    # strip code fences so example links aren't checked
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    for m in re.finditer(r"\]\(([^)#\s]+)(#[^)\s]*)?\)", text):
        target = m.group(1)
        if re.match(r"^[a-z]+://", target) or target.startswith("mailto:"):
            continue
        if not (REPO / target).exists():
            problems.append(f"CONTEXT-MAP.md: dangling entry `{target}`")



VOCABULARY_DESIGN = REPO / "docs" / "initiatives" / "2026-09-02-vocabulary" / "design.md"

# Retired words that are unambiguous in English, so a match is a finding. The pattern names the word as it
# appears in prose, the value names what says it now. Words the design retires but English also uses in other
# senses (kind, spec, block, leaf, tier, run, artifact, world as a plain noun) are left to review.
RETIRED = {
    r"\bleaf\b(?! sections?)": "step (a leaf section of a page stays)",
    r"\bpack(s|ed|ing)?\b": "save (a draft into the library), import (a folder of files); pack only distils a session",
    r"\bshel(f|ves)\b": "library, reusable step, or Workflows",
    r"\bparked\b": "waiting (at a gate) or abandoned (attempts used up)",
    r"\bloadouts?\b": "setup",
    r"\battestations?\b": "evidence",
    r"\bcorpusspec\b": "schema (knowledge-base-schema.json)",
    r"\bcorp(us|ora)\b": "knowledge base(s)",
    r"\bdoors?\b": "plugin(s)",
    r"\bjournals?\b": "session log or run log",
    r"\b(brought|carried) steps?\b": "inline step",
    r"\bstep pages?\b|\bstored steps?\b": "reusable step",
    r"\bcode lea(f|ves)\b|\bwork registry\b|\bwork lea(f|ves)\b": "program, or registry of steps",
    r"\bintake\b": "source queue",
    r"\bcalibrat(e|es|ed|ing|ion|ions)\b": "tuning run",
    r"\badvisor(y|ies)\b": "recommendation",
    r"\bwiki\b|\bwiki_kg\b": "knowledge base, or kb_graph",
    r"\bworld stamps?\b": "environment",
    r"\bledgers?\b": "source queue, evidence, activations, or refresh history",
    r"\bseats?\b": "model tier",
    r"\blibrary visibility\b": "private or shared",
    r"\bmedium\b": "source type",
    r"\bbundles?\b": "template (a starting folder) or export directory",
    r"\blint (reports?|errors?|warnings?|store|issues?|findings?|stage)\b|\bcompile lint\b|\blint-clean\b": "validation",
    r"\b(source|intake|zotero|youtube|x) providers?\b|\bengram_providers\b|\bsources\.providers\b": "connector",
    r"\bvaults?\b": "knowledge base",
    r"\bauthored (artifacts?|items?|capabilit(y|ies)|lea(f|ves)|agents?|profiles?)\b": "custom item",
    r"\b(re)?compil(e|es|ed|er|ers|ing|ation)\b": "index (verb), indexing, indexed",
    r"\bRegistryIndex\b|\bschema spec\b": "vocabulary index, or schema",
    r"\beval runs?\b|\bgolden datasets?\b": "evaluation run, dataset",
    r"\b(faithfulness|machine staleness|ingest) gates?\b|\bfreshness sweeps?\b|\bstaleness refusal\b": "quote check, freshness check, validation",
    r"\bdrafting mirrors?\b": "drafting text",
    r"\btrust tiers?\b": "trust level",
    r"\bstep logs?\b|\bmodel records?\b": "transcript",
    r"\bmenu keys?\b": "delegation tier",
    r"\bregistry (entry|entries|rows?)\b": "registered knowledge base",
    r"\bfeeds (file|yaml)\b": "watch list",
    r"\bingest (jobs?|runs?)\b": "refresh, refresh run",
    r"\bchange events?\b": "change notification",
    r"\bspine\b": "always on",
    r"\bworkflow stores?\b": "library",
    r"\bthe dashboard\b": "AI Factory Studio",
}
# The retired headwords of design.md section 4.7 that the map above must cover, so the design stays the source.
_DESIGN_HEADWORDS_COVERED = {
    "shelf", "block", "door", "parked", "ledger", "journal", "artifact", "leaf", "work", "task", "brought", "carried",
    "seat", "world", "loadout", "authored", "attestation", "kind", "spec", "compile", "lint", "intake", "medium",
    "bundle", "library visibility", "calibration", "advisory", "corpus", "vault", "collection", "provider",
    "dashboard", "studio", "wiki", "memory",
}
_REVIEW_ONLY = {"block", "artifact", "leaf", "work", "task", "kind", "spec", "collection", "studio", "memory", "world"}
BANNED_SYMBOLS = "—–;·→⇒←×§"
_CODE_SPAN = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*(?:\n[^`\n]*)?`", re.S)
_LINK_TARGET = re.compile(r"\]\([^)\s]*\)")
_BARE_URL = re.compile(r"https?://\S+")
_WIKILINK_TARGET = re.compile(r"\[\[[^\]|]*")
_TS_COMMENT = re.compile(r"^[ \t]*//[^\n]*|/\*.*?\*/", re.S | re.M)
_TS_EXPRESSION = re.compile(r"\$\{[^}]*\}")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BARE_PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+\.\w+")
# Names of things outside this repository that happen to carry a retired word: never our vocabulary.
_EXTERNAL_NAMES = re.compile(
    r"LLM[- ]Wiki|Knowledge Bundle|Direct Corpus Interaction|\([^()\n]*, Medium\)|npm packs?|packed\s+copy|llama-pack|leaf chunks|leaf nodes"
    r"|leaf toolsets?|right-leaf",
    re.IGNORECASE,
)
# A double-quoted span is literal text (a title, a UI string, a quotation from an outside document), never our prose.
_QUOTED = re.compile(r'"[^"\n]{1,300}"')
# The split-doc summary marker, matched by scripts/docs/split_doc.py, and the note section of the same name.
_MARKERS = re.compile(r"TL;DR")
_FRONTMATTER_IDENTIFIER = re.compile(r"^(?:name|id|aliases|path|app|type|status|owner|date|tags|kinds|folders):[^\n]*$", re.M)
_TS_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|`((?:[^`\\]|\\.)*)`', re.S)
_JSX_TEXT = re.compile(r">([^<>{}=;()]+)<")
_VOCAB_SKIP_PARTS = {"adr", "brainstorming", "specs", "plans", "memory"}
# Words the UI scan cannot tell from CSS class names or enum values (Tailwind `font-medium`, a thinking level).
_UI_SKIP = {r"\bmedium\b"}
_VOCAB_SKIP_PREFIXES = (("docs", "initiatives", "2026-09-02-vocabulary"),)
# A skill copied byte for byte from an outside repository keeps that repository's words. A test pins the copy to
# its release, so it is never edited here.
_UPSTREAM_COPIES = (("engram", "src", "engram", "skills", "agent-threads"),)


def _design_headwords():
    """The first-column words of design.md section 4.7, split on commas, parentheticals dropped."""
    text = VOCABULARY_DESIGN.read_text(encoding="utf-8")
    section = re.search(r"### 4\.7 Words retired everywhere(.*?)### 4\.8", text, re.S)
    words = set()
    for line in (section.group(1) if section else "").splitlines():
        if not line.startswith("| ") or line.startswith("| Word ") or line.startswith("|---"):
            continue
        cell = re.sub(r"\(.*?\)", "", line.split("|")[1])
        words |= {w.strip() for w in cell.split(",") if w.strip()}
    return words


def _blank(match):
    return re.sub(r"[^\n]", " ", match.group(0))


def _blank_data_lines(frontmatter):
    """A workflow or step file's frontmatter minus its data: a `source: |` program block and JSON-schema lines."""
    out, in_block, indent = [], False, 0
    for line in frontmatter.splitlines(keepends=True):
        own_indent = len(line) - len(line.lstrip(" "))
        if in_block and (line.strip() == "" or own_indent > indent):
            out.append(re.sub(r"[^\n]", " ", line))
            continue
        in_block = False
        block = re.match(r"^(\s*)source:\s*[|>]", line)
        if block:
            in_block, indent = True, len(block.group(1))
        elif "{type:" in line or re.match(r"^\s*(enum|required):", line):
            out.append(re.sub(r"[^\n]", " ", line))
            continue
        out.append(line)
    return "".join(out)


def _prose(text):
    """`text` with everything that is not prose blanked to spaces, so line numbers hold.

    Blanked: code spans, link targets, bare URLs, wikilink targets, HTML comments (sentinels code matches verbatim),
    bare file paths, the names of outside things that carry a retired word, double-quoted literal text, and the
    frontmatter's identifier lines (name, id, aliases, path and the like are never words).
    """
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = _FRONTMATTER_IDENTIFIER.sub(_blank, _blank_data_lines(text[:end])) + text[end:]
    for pattern in (_CODE_SPAN, _LINK_TARGET, _BARE_URL, _WIKILINK_TARGET, _HTML_COMMENT, _BARE_PATH, _EXTERNAL_NAMES, _MARKERS, _QUOTED):
        text = pattern.sub(_blank, text)
    return text


_DASH_BREAK = re.compile(r"(?<=\S) -{1,2} (?=\S)")


def _line_findings(line, words=True):
    """The retired words and banned symbols in one line of prose, as messages."""
    found = []
    if words:
        for pattern, replacement in RETIRED.items():
            hit = re.search(pattern, line, re.IGNORECASE)
            if hit:
                found.append(f"retired word `{hit.group(0)}`, say {replacement}")
    bad = [ch for ch in line if ch in BANNED_SYMBOLS]
    if bad:
        found.append(f"banned symbol {' '.join(repr(ch) for ch in sorted(set(bad)))}")
    return found


def _vocabulary_findings(rel, prose, problems):
    for lineno, line in enumerate(prose.splitlines(), 1):
        problems.extend(f"{rel}:{lineno}: {message}" for message in _line_findings(line))


def prose_findings(text, *, words=True, dashes=False):
    """Every finding in the prose of `text`, as (line number, message) pairs counted from 1.

    Code spans, link targets, URLs, file paths and double-quoted text are not prose and are never
    read. `words` adds the retired words to the banned symbols. `dashes` adds a hyphen or a double
    hyphen used as a clause break, which a list item at the start of a line is not.
    """
    found = []
    for lineno, line in enumerate(_prose(text).splitlines(), 1):
        found.extend((lineno, message) for message in _line_findings(line, words))
        if dashes and _DASH_BREAK.search(line):
            found.append((lineno, "dash used as a clause break"))
    return found


def _vocabulary_docs(files):
    """The governed docs the word lint reads: live docs only, never the record of the past."""
    out = []
    for f in files:
        rel = f.relative_to(REPO)
        if _VOCAB_SKIP_PARTS & set(rel.parts):
            continue
        if any(rel.parts[: len(prefix)] == prefix for prefix in _VOCAB_SKIP_PREFIXES):
            continue
        if "initiatives" in rel.parts and not _initiative_is_live(f.parent):
            continue
        out.append(f)
    return out


def _initiative_is_live(folder):
    """Whether an initiative folder is current work: its design (else its plan) is approved or active.

    A draft is another session's work in progress and an executed or superseded one is the record of the past, so
    neither is linted for words.
    """
    for name in ("design.md", "plan.md"):
        path = folder / name
        if path.is_file():
            fm = parse_frontmatter(path.read_text(encoding="utf-8")) or {}
            return fm.get("status", "").lower() in {"approved", "active"}
    return True


def _guidance_files():
    """CLAUDE.md files, README files, the plugin's pages and every skill file: read first, so they carry the vocabulary."""
    packages = APPS + ["connectors", "client-core"]
    files = [REPO / "CLAUDE.md", REPO / "README.md", REPO / "scripts" / "stack" / "README.md"]
    files += [REPO / a / "CLAUDE.md" for a in packages] + [REPO / a / "README.md" for a in packages]
    files += sorted(REPO.glob("templates/*/README.md")) + sorted(REPO.glob("harness/proof/*.md"))
    files += [f for f in sorted(REPO.glob("docs/_meta/**/*.md")) if f.name != "dashboard.md"]
    trees = [REPO / "harness" / "skills", REPO / "engram" / "src" / "engram" / "skills", REPO / "engram" / "plugin"]
    trees += [REPO / "harness" / "workflows"]
    trees += sorted(REPO.glob("templates/*/_meta")) + sorted(REPO.glob("templates/*/workflows"))
    for tree in trees:
        if tree.is_dir():
            files += sorted(tree.rglob("*.md"))
    gone = [name for name in packages if not (REPO / name).is_dir()]
    empty = [pattern for pattern in ("templates/*/README.md", "templates/*/_meta", "templates/*/workflows") if not any(REPO.glob(pattern))]
    if gone or empty:
        trouble = ", ".join(gone + empty)
        msg = f"the guidance list names {len(gone) + len(empty)} folder(s) or pattern(s) that match nothing, so their pages are scanned by nobody: {trouble}"
        raise SystemExit(f"FAIL : {msg}")
    files = [f for f in files if not any(f.relative_to(REPO).parts[: len(prefix)] == prefix for prefix in _UPSTREAM_COPIES)]
    return [f for f in files if f.is_file()]


def _ui_strings(path):
    """The user-visible text of one studio-client source file: string literals with a space, and JSX text.

    Comments are code, not text, and are blanked first. Each piece says whether the symbol check applies: a template
    literal may hold a script or a style sheet, so only its words are checked.
    """
    text = _TS_EXPRESSION.sub(lambda m: "_" * len(m.group(0)), _TS_COMMENT.sub(_blank, path.read_text(encoding="utf-8")))
    pieces = []
    for m in _TS_STRING.finditer(text):
        literal = next(g for g in m.groups() if g is not None)
        if " " in literal and not literal.startswith("/api/"):
            pieces.append((text.count("\n", 0, m.start()) + 1, literal, m.group(3) is None))
    if path.suffix == ".tsx":
        for m in _JSX_TEXT.finditer(text):
            if m.group(1).strip():
                pieces.append((text.count("\n", 0, m.start()) + 1, m.group(1), True))
    return pieces


def check_vocabulary(files, problems):
    missing = _design_headwords() - _DESIGN_HEADWORDS_COVERED
    if missing:
        problems.append(f"validate_docs.py: design.md 4.7 retires {sorted(missing)} and the lint does not know them")
    for f in _vocabulary_docs(files) + _guidance_files():
        _vocabulary_findings(f.relative_to(REPO), _prose(f.read_text(encoding="utf-8")), problems)
    ui = REPO / "studio-client" / "src"
    if ui.is_dir():
        for path in sorted(list(ui.rglob("*.ts")) + list(ui.rglob("*.tsx"))):
            if ".test." in path.name or "node_modules" in path.parts:
                continue
            rel = path.relative_to(REPO)
            for lineno, piece, symbols_apply in _ui_strings(path):
                for pattern, replacement in RETIRED.items():
                    if pattern in _UI_SKIP:
                        continue
                    hit = re.search(pattern, piece, re.IGNORECASE)
                    if hit:
                        problems.append(f"{rel}:{lineno}: retired word `{hit.group(0)}` in UI text, say {replacement}")
                if symbols_apply and any(ch in BANNED_SYMBOLS for ch in piece):
                    problems.append(f"{rel}:{lineno}: banned symbol in UI text")


def main():
    """Run every check, the vocabulary included (`--vocabulary` is accepted and changes nothing)."""
    problems = []
    files = governed_files()
    check_frontmatter(files, problems)
    check_context_map(problems)
    check_vocabulary(files, problems)
    if problems:
        for p in problems:
            print(f"FAIL : {p}")
        print(f"\n{len(problems)} problem(s) across {len(files)} governed docs.")
        sys.exit(1)
    print(f"OK   : {len(files)} governed docs, frontmatter valid, ids unique and reachable, map resolves, vocabulary clean.")


if __name__ == "__main__":
    main()
