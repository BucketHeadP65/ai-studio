---
title: Render a decision brief
summary: Joins a reviewed brief into Markdown for the person and into a proposed question note for the knowledge base, with no model call and no writing.
version: 1
step:
  name: render_decision_brief
  source: |
    labels = {
        "recommend_change": "Recommend a change",
        "keep_current": "Keep the current course",
        "insufficient_evidence": "Not enough evidence yet",
    }
    label = labels[brief["decision"]]
    support = [e for e in brief["evidence"] if e["role"] == "support"]
    against = [e for e in brief["evidence"] if e["role"] == "counterevidence"]
    evidence_lines = []
    for group_title, group in (("Supporting", support), ("Arguing the other way", against)):
        if not group:
            continue
        evidence_lines.append("**" + group_title + "**")
        evidence_lines.append("")
        for e in group:
            where = e["ref"]
            if e.get("locator"):
                where = where + ", " + e["locator"]
            line = "- " + e["statement"] + " (" + where + ")"
            if e.get("quote"):
                line = line + "\n  > " + e["quote"]
            line = line + "\n  Holds when: " + e["conditions"]
            evidence_lines.append(line)
        evidence_lines.append("")
    if not brief["evidence"]:
        evidence_lines = ["Nothing in the knowledge bases read settles this yet.", ""]
    evidence_body = "\n".join(evidence_lines).rstrip()
    alternative_lines = ["- " + a for a in brief["alternatives"]]
    alternatives_body = "\n".join(alternative_lines) if alternative_lines else "None recorded."
    answer_body = brief["recommendation"] + "\n\n**Alternatives considered**\n\n" + alternatives_body
    matters_body = brief["question"] + "\n\n" + brief["limitations"]
    tldr_body = label + ". " + brief["summary"] + "\n\nNext: " + brief["next_action"]
    document = "\n".join(
        [
            "# " + brief["title"],
            "",
            "**Question.** " + brief["question"],
            "",
            "**Decision.** " + label,
            "",
            brief["summary"],
            "",
            "## Recommendation",
            "",
            brief["recommendation"],
            "",
            "## Evidence",
            "",
            evidence_body,
            "",
            "## Alternatives considered",
            "",
            alternatives_body,
            "",
            "## Limitations",
            "",
            brief["limitations"],
            "",
            "## Next action",
            "",
            brief["next_action"],
            "",
        ]
    )
    note_draft = {
        "page_type": "question",
        "title": brief["title"],
        "summary": brief["summary"],
        "frontmatter": {
            "status": "synthesized",
            "question_status": "open" if brief["decision"] == "insufficient_evidence" else "answered",
        },
        "sections": [
            {"heading": "TL;DR", "body": tldr_body},
            {"heading": "Why It Matters", "body": matters_body},
            {"heading": "Current Evidence", "body": evidence_body},
            {"heading": "Candidate Answers", "body": answer_body},
        ],
    }
    {"document": document, "note_draft": note_draft}
  inputs:
    brief: {like: draft_decision_brief}
  output:
    json_schema:
      type: object
      title: RenderedBrief
      required: [document, note_draft]
      properties:
        document:
          type: string
          description: The whole brief as Markdown, for the person to read.
        note_draft:
          type: object
          title: NoteDraft
          description: A proposed note for a knowledge base. Data only, and writing it is a separate act the person takes.
          required: [page_type, title, summary, frontmatter, sections]
          properties:
            page_type:
              type: string
              description: The destination page type, this knowledge base's question type.
            title: {type: string}
            summary: {type: string, maxLength: 280}
            frontmatter:
              type: object
              title: NoteFrontmatter
              description: Fields the destination schema declares. It never carries id, type, title or summary.
              required: [status, question_status]
              properties:
                status:
                  type: string
                  description: The page's own status in this knowledge base's lifecycle.
                question_status:
                  enum: [open, answered]
                  type: string
                  description: Whether this brief answered the question or left it open.
            sections:
              type: array
              description: The question type's canonical sections, in the order the schema declares them.
              items:
                type: object
                title: NoteSection
                required: [heading, body]
                properties:
                  heading: {type: string}
                  body: {type: string}
---

Use as the last stage of a decision brief, over the brief the judge approved. It calls no model and writes nothing:
a reviewed conclusion is not rewritten by another model, and the note it proposes is data the person chooses to
keep or not. Do not use it over an unreviewed draft, and do not use it against a knowledge base whose schema
declares no question page type, since the note it proposes names that page type and its canonical sections.
