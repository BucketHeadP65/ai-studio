"""The Validate stage: every validation rule over the resolved notes, reported as one `ValidationReport`.

It only reports. Every note still loads, and the caller decides what the errors fail. The rules by area:

* Claims: `missing-loc`, `missing-kind`, `missing-quote` (a quote-required kind, or any claim carrying an edge),
  `comparative-missing-qualifiers` (a claim asserting a comparative relation carries one field of every qualifier
  group that relation declares),
  `duplicate-anchor` (block ids unique per file) and `quote-not-in-source` (every quoted snippet appears in the
  source mirror, whitespace normalized).
* Vocabulary: `unknown-entity-type`, `unknown-relation`, `unknown-facet-value` and `unknown-kind`.
* Pages: `duplicate-id` (an error) and `oversized-summary` (a warning).
* Code paths: `dangling-code-path`, a declared `path_fields` value that is not an existing file or directory under
  `path_root`.
* Mirrors: `mirror-without-provider` (an error, from `mirror_provider_issues`), a `Sources/` mirror whose frontmatter
  names no `provider`. It is reported against its own knowledge base path and left out of the load.
* Sources: `dangling-source-ref` (a warning), a page whose own source key, read under `sources.key_field`, has no
  mirror.
* Drift: `spec-vocabulary-drift` and `facet-choices-drift` (warnings).

A claim whose source has no mirror text is not quote-checked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.domain.models import ValidationIssue, ValidationReport, cited_mirror_key
from engram.indexing.domain.resolver import resolve
from engram.kb.contracts import KnowledgeBaseSchema, facet_choices_drift, quotes_in_mirror, resolve_schema, schema_vocabulary_drift

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from engram.indexing.domain.models import MirrorDoc, ResolvedNote
    from engram.indexing.domain.resolver import ResolveResult
    from engram.kb.contracts import BaseFrontmatter, ClaimBlock, KnowledgeBaseNote, VocabularyIndex

# The longest `summary` that raises no `oversized-summary` warning.
_MAX_SUMMARY_LEN = 400

# The page id the `spec-vocabulary-drift` warnings are reported against, since they belong to no page.
_REGISTRY_PAGE_ID = "_meta/vocabulary"

# The page id the `facet-choices-drift` warnings are reported against, since they belong to no page.
_KNOWLEDGE_BASE_SCHEMA_PAGE_ID = "knowledge-base-schema.json"


def validate(
    notes: Iterable[ResolvedNote],
    vocabulary: VocabularyIndex,
    *,
    mirror_texts: Mapping[str, str] | None = None,
    resolve_result: ResolveResult | None = None,
    spec: KnowledgeBaseSchema | None = None,
    global_ids: frozenset[str] = frozenset(),
    path_root: Path | None = None,
    path_root_source: str | None = None,
    knowledge_base_source_keys: frozenset[str] | None = None,
) -> ValidationReport:
    """Apply every validation rule to `notes` against `vocabulary` and return the report.

    Args:
        notes: The resolved notes to validate.
        vocabulary: The indexed vocabulary index.
        mirror_texts: Raw mirror text by source key. Omit it to skip the quote check.
        resolve_result: The resolver's result, whose dangling-link warnings and ingest queue join the report.
            Omit it to report only this stage's issues.
        spec: The knowledge base schema. It is required.
        global_ids: Every page id already in the collection. Each id found in it yields one `duplicate-id`
            error, even when several notes share it. Empty, the default, skips the check.
        path_root: The directory `path_fields` values must resolve under. `None`, the default, skips the
            `dangling-code-path` check.
        path_root_source: Where `path_root` came from, such as a CLI flag or a settings file, named in each
            `dangling-code-path` message. `None` leaves it out.
        knowledge_base_source_keys: Every source mirror key of the knowledge base. A note whose own source key is missing
            from it gains a `dangling-source-ref` warning. `None`, the default, skips the check.

    Returns:
        The errors, the warnings and the resolver's ingest queue.

    Raises:
        KnowledgeBaseSchemaMissingError: `spec` is `None`.
    """
    resolved_spec = resolve_schema(spec)
    mirrors = mirror_texts or {}
    materialized = list(notes)
    errors: list[ValidationIssue] = list(_check_duplicate_ids(materialized, global_ids))
    warnings: list[ValidationIssue] = list(resolve_result.warnings) if resolve_result is not None else []
    warnings.extend(_check_schema_vocabulary_drift(vocabulary, resolved_spec))
    warnings.extend(_check_facet_choices_drift(resolved_spec))

    for resolved in materialized:
        for issue in _check_note(resolved, vocabulary, mirrors, resolved_spec, path_root, path_root_source, knowledge_base_source_keys):
            (warnings if issue.severity == "warning" else errors).append(issue)

    ingest_queue = resolve_result.ingest_queue if resolve_result is not None else ()
    return ValidationReport(errors=tuple(errors), warnings=tuple(warnings), ingest_queue=ingest_queue)


def validate_vocabulary(note: KnowledgeBaseNote, vocabulary: VocabularyIndex, *, spec: KnowledgeBaseSchema | None = None) -> ValidationReport:
    """Check one note's vocabulary against `vocabulary`: entity type, facet values, claim kinds and relations.

    Only these page-local rules run, the ones a note can meet with no other page's data.

    Args:
        note: The parsed note to check.
        vocabulary: The indexed vocabulary index.
        spec: The knowledge base schema, whose facets are checked. It is required.

    Returns:
        The vocabulary errors found, with no warnings.

    Raises:
        KnowledgeBaseSchemaMissingError: `spec` is `None`.
    """
    resolved_spec = resolve_schema(spec)
    frontmatter = note.frontmatter
    page_id = frontmatter.id
    issues: list[ValidationIssue] = [
        *_check_entity_type(frontmatter, vocabulary, page_id),
        *_check_facets(frontmatter, vocabulary, page_id, resolved_spec),
    ]
    for claim in note.claims:
        issues.extend(_check_claim_type(claim, vocabulary, page_id))
        issues.extend(_check_relations(claim, vocabulary, page_id))
    return ValidationReport(errors=tuple(issues))


def validate_note(
    note: KnowledgeBaseNote,
    vocabulary: VocabularyIndex,
    *,
    mirror_text: str | None = None,
    spec: KnowledgeBaseSchema | None = None,
    path_root: Path | None = None,
    path_root_source: str | None = None,
) -> ValidationReport:
    """Validate one note on its own, with no link index over the rest of the knowledge base.

    The note meets the same note-local rules an index applies, and the quote check when `mirror_text` is given.
    Every cross-note link dangles at this scope, so all `dangling-link` warnings fold into one naming the count.

    Args:
        note: The parsed note to validate.
        vocabulary: The indexed vocabulary index.
        mirror_text: The cited source mirror's raw text. Omit it to skip the quote check.
        spec: The knowledge base schema. It is required.
        path_root: The directory `path_fields` values must resolve under. `None`, the default, skips the
            `dangling-code-path` check.
        path_root_source: Where `path_root` came from, named in each `dangling-code-path` message.

    Returns:
        The note's errors and warnings, and any ingest queue the resolver produced.

    Raises:
        KnowledgeBaseSchemaMissingError: `spec` is `None`.
    """
    resolved = _collapse_dangling_link_warnings(resolve((note,), relations=spec.relations if spec is not None else ()), note.frontmatter.id)
    # A `None` spec reaches `validate` unchanged, which refuses it.
    key_field, link_field = (spec.sources.key_field, spec.sources.digest_link_field) if spec is not None else ("", "")
    cited = cited_mirror_key(note.frontmatter, key_field=key_field, digest_link_field=link_field)
    mirror_texts = {cited: mirror_text} if mirror_text is not None else None
    return validate(
        resolved.notes,
        vocabulary,
        mirror_texts=mirror_texts,
        resolve_result=resolved,
        spec=spec,
        path_root=path_root,
        path_root_source=path_root_source,
    )


def _collapse_dangling_link_warnings(resolved: ResolveResult, page_id: str) -> ResolveResult:
    """Fold every `dangling-link` warning into one naming the count."""
    dangling = [issue for issue in resolved.warnings if issue.rule == "dangling-link"]
    if not dangling:
        return resolved
    other = tuple(issue for issue in resolved.warnings if issue.rule != "dangling-link")
    count = len(dangling)
    plural = "" if count == 1 else "s"
    folded = ValidationIssue(
        severity="warning",
        rule="dangling-link",
        page_id=page_id,
        message=f"{count} cross-note link{plural} not checked here: they resolve at index",
    )
    return resolved.model_copy(update={"warnings": (*other, folded)})


def _check_duplicate_ids(notes: Iterable[ResolvedNote], global_ids: frozenset[str]) -> list[ValidationIssue]:
    """One `duplicate-id` error per validated note id found in `global_ids`, none when it is empty."""
    if not global_ids:
        return []
    issues: list[ValidationIssue] = []
    flagged: set[str] = set()
    for resolved in notes:
        page_id = resolved.note.frontmatter.id
        if page_id in global_ids and page_id not in flagged:
            flagged.add(page_id)
            issues.append(
                _error("duplicate-id", page_id, f"id {page_id!r} is already used elsewhere in the collection. Ids must be unique")
            )
    return issues


def _check_schema_vocabulary_drift(vocabulary: VocabularyIndex, spec: KnowledgeBaseSchema) -> list[ValidationIssue]:
    """One `spec-vocabulary-drift` warning per vocabulary item the schema seeds that the vocabulary lacks."""
    return [
        ValidationIssue(severity="warning", rule="spec-vocabulary-drift", page_id=_REGISTRY_PAGE_ID, message=message)
        for message in schema_vocabulary_drift(vocabulary, spec)
    ]


def _check_facet_choices_drift(spec: KnowledgeBaseSchema) -> list[ValidationIssue]:
    """One `facet-choices-drift` warning per facet seed value and field choices value that disagree."""
    return [
        ValidationIssue(severity="warning", rule="facet-choices-drift", page_id=_KNOWLEDGE_BASE_SCHEMA_PAGE_ID, message=message)
        for message in facet_choices_drift(spec)
    ]


def mirror_provider_issues(mirrors: Iterable[MirrorDoc]) -> list[ValidationIssue]:
    """One `mirror-without-provider` error per mirror whose frontmatter names no `provider`.

    Each error is reported against the mirror's own knowledge base path. Each reported mirror is one
    `loadable_mirrors` drops.

    Args:
        mirrors: The parsed source mirrors.

    Returns:
        One error per mirror without a provider.
    """
    return [
        ValidationIssue(
            severity="error",
            rule="mirror-without-provider",
            page_id=mirror.path,
            message="mirror names no connector, so it cannot be loaded, add a provider field to its frontmatter or re-sync it",
        )
        for mirror in mirrors
        if not mirror.provider
    ]


def _check_note(
    resolved: ResolvedNote,
    vocabulary: VocabularyIndex,
    mirrors: Mapping[str, str],
    spec: KnowledgeBaseSchema,
    path_root: Path | None,
    path_root_source: str | None,
    knowledge_base_source_keys: frozenset[str] | None,
) -> list[ValidationIssue]:
    """Every issue for one note, of either severity: frontmatter, vocabulary, path fields, then claims."""
    note = resolved.note
    frontmatter = note.frontmatter
    page_id = frontmatter.id
    issues: list[ValidationIssue] = []

    # The schema-driven field sets, derived once per note.
    quote_required_types = frozenset(spec.claims.quote_required_types) if spec.claims is not None else frozenset()
    comparative_relations = frozenset(spec.claims.comparative_relations) if spec.claims is not None else frozenset()
    required_qualifiers = {
        relation.slug: relation.qualifiers for relation in spec.relations if relation.slug in comparative_relations and relation.qualifiers
    }

    issues.extend(_check_summary(frontmatter, page_id))
    issues.extend(_check_entity_type(frontmatter, vocabulary, page_id))
    issues.extend(_check_facets(frontmatter, vocabulary, page_id, spec))
    issues.extend(_check_anchor_uniqueness(note.claims, page_id))
    if path_root is not None:
        issues.extend(_check_path_fields(frontmatter, page_id, spec, path_root, path_root_source))
    if knowledge_base_source_keys is not None:
        issues.extend(_check_source_ref(frontmatter, page_id, spec, knowledge_base_source_keys))

    mirror_text = mirrors.get(cited_mirror_key(frontmatter, key_field=spec.sources.key_field, digest_link_field=spec.sources.digest_link_field))
    for claim in note.claims:
        issues.extend(_check_claim(claim, vocabulary, page_id, mirror_text, quote_required_types, required_qualifiers))

    return issues


def _check_summary(frontmatter: BaseFrontmatter, page_id: str) -> list[ValidationIssue]:
    """A `summary` longer than 400 characters is a warning, not a failed page."""
    summary = frontmatter.summary
    if summary is not None and len(summary) > _MAX_SUMMARY_LEN:
        message = f"summary is {len(summary)} chars (> {_MAX_SUMMARY_LEN})"
        return [ValidationIssue(severity="warning", rule="oversized-summary", page_id=page_id, message=message)]
    return []


def _check_entity_type(frontmatter: BaseFrontmatter, vocabulary: VocabularyIndex, page_id: str) -> list[ValidationIssue]:
    """An entity page's `entity_type` must be a registered value."""
    entity_type = getattr(frontmatter, "entity_type", None)
    if entity_type is not None and entity_type not in vocabulary.entity_types:
        return [_error("unknown-entity-type", page_id, f"unregistered entity_type {entity_type!r}")]
    return []


def _check_facets(frontmatter: BaseFrontmatter, vocabulary: VocabularyIndex, page_id: str, spec: KnowledgeBaseSchema) -> list[ValidationIssue]:
    """Every value of a facet the vocabulary declares must be registered for that facet."""
    issues: list[ValidationIssue] = []
    for facet in spec.facets:
        allowed = vocabulary.facets.get(facet.key)
        if allowed is None:
            continue
        issues.extend(
            _error("unknown-facet-value", page_id, f"unregistered {facet.key} value {value!r}, registered: {', '.join(sorted(allowed))}")
            for value in frontmatter.facet_values(facet.key)
            if value not in allowed
        )
    return issues


def _check_path_fields(
    frontmatter: BaseFrontmatter, page_id: str, spec: KnowledgeBaseSchema, path_root: Path, path_root_source: str | None
) -> list[ValidationIssue]:
    """Every declared path field must name an existing file or directory inside `path_root`."""
    pt = spec.page_type(frontmatter.type)
    if pt is None:
        return []
    issues: list[ValidationIssue] = []
    resolved_root = path_root.resolve()
    provenance = _path_root_provenance(spec, path_root_source)
    for field_name in pt.path_fields:
        raw = getattr(frontmatter, field_name, None)
        if not isinstance(raw, str) or not raw:
            continue
        resolved = (path_root / raw).resolve()
        if not resolved.is_relative_to(resolved_root):
            issues.append(_error("dangling-code-path", page_id, f"{field_name} {raw!r} escapes {path_root} ({provenance})"))
        elif not resolved.exists():
            issues.append(_error("dangling-code-path", page_id, f"{field_name} {raw!r} does not exist under {path_root} ({provenance})"))
    return issues


def _path_root_provenance(spec: KnowledgeBaseSchema, path_root_source: str | None) -> str:
    """Describe how `path_root` was chosen, for a `dangling-code-path` message."""
    detail = f"knowledge-base-schema.json path_root={spec.path_root!r}"
    return detail if path_root_source is None else f"{detail}, root from {path_root_source}"


def _check_source_ref(
    frontmatter: BaseFrontmatter, page_id: str, spec: KnowledgeBaseSchema, knowledge_base_source_keys: frozenset[str]
) -> list[ValidationIssue]:
    """A page's own cited source (its key field, else its digest link field) with no mirror is a warning, not a failed page."""
    key = cited_mirror_key(frontmatter, key_field=spec.sources.key_field, digest_link_field=spec.sources.digest_link_field)
    if not key or key in knowledge_base_source_keys:
        return []
    message = f"cites source {key!r} but the knowledge base has no mirror for it, restore the mirror or remove the reference (or the page)"
    return [ValidationIssue(severity="warning", rule="dangling-source-ref", page_id=page_id, message=message)]


def _check_anchor_uniqueness(claims: Iterable[ClaimBlock], page_id: str) -> list[ValidationIssue]:
    """Block ids (claim anchors) must be unique within a note."""
    seen: set[str] = set()
    issues: list[ValidationIssue] = []
    for claim in claims:
        if claim.anchor in seen:
            issues.append(_error("duplicate-anchor", page_id, f"duplicate block id {claim.anchor!r}", anchor=claim.anchor))
        seen.add(claim.anchor)
    return issues


def _check_claim(
    claim: ClaimBlock,
    vocabulary: VocabularyIndex,
    page_id: str,
    mirror_text: str | None,
    quote_required_types: frozenset[str],
    required_qualifiers: Mapping[str, tuple[tuple[str, ...], ...]],
) -> list[ValidationIssue]:
    """Every error-severity issue for one claim block."""
    issues: list[ValidationIssue] = []
    anchor = claim.anchor

    if not claim.loc.strip():
        issues.append(_error("missing-loc", page_id, "claim is missing the required loc", anchor=anchor))
    if not claim.kind.strip():
        page_types = ", ".join(sorted(vocabulary.claim_types))
        issues.append(_error("missing-kind", page_id, f"claim is missing the required kind, one of: {page_types}", anchor=anchor))
    issues.extend(_check_claim_type(claim, vocabulary, page_id))

    issues.extend(_check_relations(claim, vocabulary, page_id))
    issues.extend(_check_quote_required(claim, page_id, quote_required_types))
    issues.extend(_check_comparative_qualifiers(claim, page_id, required_qualifiers))
    issues.extend(_check_quotes(claim, page_id, mirror_text))
    return issues


def _check_claim_type(claim: ClaimBlock, vocabulary: VocabularyIndex, page_id: str) -> list[ValidationIssue]:
    """A present claim `kind` must be a registered claim type."""
    if claim.kind.strip() and claim.kind not in vocabulary.claim_types:
        claim_types = ", ".join(sorted(vocabulary.claim_types))
        return [_error("unknown-kind", page_id, f"unregistered claim type {claim.kind!r}, registered: {claim_types}", anchor=claim.anchor)]
    return []


def _check_relations(claim: ClaimBlock, vocabulary: VocabularyIndex, page_id: str) -> list[ValidationIssue]:
    """Each claim edge's relation key must be registered."""
    issues: list[ValidationIssue] = []
    for relation, _target in claim.edges:
        if relation not in vocabulary.relations:
            issues.append(_error("unknown-relation", page_id, f"unregistered relation {relation!r}", anchor=claim.anchor))
    return issues


def _check_quote_required(claim: ClaimBlock, page_id: str, quote_required_types: frozenset[str]) -> list[ValidationIssue]:
    """`quote::` is required for a quote-required kind or any claim carrying an edge."""
    needs_quote = claim.kind in quote_required_types or bool(claim.edges)
    if needs_quote and not (claim.quote and claim.quote.strip()):
        reason = "carrying an evidential edge" if claim.edges and claim.kind not in quote_required_types else f"of kind {claim.kind!r}"
        return [_error("missing-quote", page_id, f"claim {reason} is missing the required quote", anchor=claim.anchor)]
    return []


def _check_comparative_qualifiers(
    claim: ClaimBlock, page_id: str, required_qualifiers: Mapping[str, tuple[tuple[str, ...], ...]]
) -> list[ValidationIssue]:
    """Each comparative relation the claim asserts needs one carried link field from every qualifier group it declares."""
    issues: list[ValidationIssue] = []
    for relation in dict.fromkeys(relation for relation, _ in claim.edges):
        missing = [group for group in required_qualifiers.get(relation, ()) if not any(claim.links.get(name) for name in group)]
        if missing:
            requirement = ", and ".join(" or ".join(group) for group in missing)
            message = f"claim {claim.anchor!r} asserting {relation!r} must carry {requirement}"
            issues.append(_error("comparative-missing-qualifiers", page_id, message, anchor=claim.anchor))
    return issues


def _check_quotes(claim: ClaimBlock, page_id: str, mirror_text: str | None) -> list[ValidationIssue]:
    """Every quoted snippet in a present `quote::` must appear in the cited mirror, whitespace normalized."""
    if mirror_text is None or not (claim.quote and claim.quote.strip()):
        return []
    if not quotes_in_mirror(claim.quote, mirror_text):
        return [_error("quote-not-in-source", page_id, "claim quote not found in the source mirror", anchor=claim.anchor)]
    return []


def _error(rule: str, page_id: str, message: str, *, anchor: str | None = None) -> ValidationIssue:
    """Build an error-severity `ValidationIssue`."""
    return ValidationIssue(severity="error", rule=rule, page_id=page_id, message=message, anchor=anchor)
