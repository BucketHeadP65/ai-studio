---
id: engram-domain-business-view
aliases: ["engram-domain-business-view"]
type: explanation
app: engram
status: current
tags: [domain, business, overview]
title: "engram, Business view"
---

# engram, Business view

> **One domain, three views.** This is the plain-language one. It tells the
> whole engram story in everyday words, for anyone deciding whether this is
> worth doing. The same story is told for engineers in the
> [Software Engineering view](engineering-view.md) and for researchers in the
> [Research view](research-view.md), and the domain [index](index.md) ties their
> shared vocabulary together. For the full picture of *why* engram exists, see
> [the vision](../vision.md).
> **Diátaxis: explanation.**

## The problem

Most teams already own a lot of knowledge, but it isn't usable as knowledge.
It sits scattered across PDFs, scribbled notes, chat threads, and what one or
two people happen to remember. Three things follow, and all of them cost money:

- **You can't reliably ask it questions.** The answer to "what do we actually
  know about X?" lives in five documents and one person's head. Finding it means
  reading, and reading does not scale.
- **You can't see where an answer came from.** Even when someone gives you an
  answer, tracing it back to the exact paper, page, and sentence that supports
  it is slow, and often impossible.
- **An AI assistant pointed at the pile just guesses.** Hand a chatbot a folder
  of raw documents and it skims a few fragments and improvises. It sounds
  confident, cites nothing you can check, and quietly blends contradictory
  sources into one smooth, wrong answer.

The net effect: a valuable body of knowledge that nobody (human or machine)
can query with confidence.

## The solution, in one paragraph

engram is an **indexer** for knowledge. You keep your notes as an ordinary
folder of Markdown files, a **knowledge base**, the same idea as an Obsidian notebook,
and the single source of truth. engram reads that folder once, carefully, and
turns it into a fast, searchable knowledge base inside a database. The key move:
every statement it stores (a **claim**) keeps a link back to its **source**,
the exact paper or document the statement came from, down to the page and the
quoted sentence. So answers arrive *with citations you can check*, and an AI
assistant can search and reason over real, traceable knowledge instead of
guessing from fragments.

## Before and after

**Before:** a drawer of PDFs and notes. To answer a question you read, you
hunt, you ask around, and you hope. Citations are a manual chore. An AI helper
makes things up.

**After:** an indexed knowledge base. The same content is now searchable,
every fact carries its source, contradictions are flagged instead of hidden,
and both people and AI tools can query it directly.

A few things you can now ask that you simply couldn't before:

- "What do we know about this topic, and which document does each point come
  from?"
- "Where do two of our sources disagree, and on what exactly?"
- "Pull the supporting quote for this claim." You get the verbatim sentence,
  not a paraphrase.

## What it does, at a glance

engram owns the whole journey from raw documents to a knowledge base your tools
can use:

- **Brings the material in.** For a research library it can pull papers from
  Zotero (a popular reference manager) automatically. The step that keeps the
  knowledge base in step with that library is called **sync**. Other types of knowledge base
  (a team's own documents, contracts, recipes) simply start from the folder.
- **Turns documents into clean text.** PDFs and web pages are messy. engram
  converts them into tidy, readable text it can work with. (engram is not tied
  to research. That's just its first and best-worn example. The same machinery
  works for any folder of documents.)
- **Lets you write notes that link claims to sources.** You write notes in a
  light, consistent format. Each important statement points back to the source
  that backs it, with the exact location and quote.
- **Indexes everything into a searchable knowledge graph.** It reads the notes,
  connects the dots between them, checks them for mistakes, and loads the result
  into a database where related ideas are linked together, so you can search by
  meaning, by keyword, and by how facts connect to one another.
- **Serves it to your tools and your AI assistant.** Once indexed, the
  knowledge base answers queries from the command line, from a local dashboard
  in the browser, and directly from an AI assistant, all reading the same
  knowledge, never changing it.
- **Checks how good its own answers are.** engram can grade its own retrieval
  against questions whose correct answers are known in advance, and tune itself
  to do better. This self-checking is called the **eval loop**.
- **Hands the knowledge to other systems.** When you need to share or archive
  the knowledge in a portable, standard form, engram can **export** it as a
  self-contained export directory on disk, a read-only copy, so the original is never put
  at risk.

## Why it's trustworthy

Two properties make engram safe to rely on, and they are the whole point:

- **Provenance: every claim cites a source.** Nothing floats free. Each stored
  statement carries the document it came from, the place within it, and the
  quoted words, and engram checks the quote actually matches the original. When
  an answer comes back, you can follow it all the way to the page. Where two
  sources contradict each other, engram records the disagreement as data rather
  than silently picking one, so you see the conflict instead of inheriting a
  blended guess.
- **It never invents: it indexes what you wrote.** engram does not author new
  facts. It reads your notes and your sources and organizes them. The original
  folder stays the single source of truth, and the database is just a fast,
  rebuildable copy of it. If a fact isn't in your material, it isn't in the
  knowledge base, and you'll never be told otherwise.

In short: engram turns a scattered pile of documents into a knowledge base you
can question, that answers with receipts, and that you (and your AI tools) can
actually trust.

---

*Companion views (siblings in this folder): [Software Engineering](engineering-view.md), [Research](research-view.md), with a shared concept [index](index.md). The full picture lives in [the vision](../vision.md).*
