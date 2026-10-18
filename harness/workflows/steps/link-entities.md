---
title: Link a digest's entities
summary: Every name a written digest refers to resolved to one entity page and the digest, read back from its file, rewritten to link them.
version: 2
step:
  name: link_entities
  instructions: |
    The digest's result, its id and its status, arrives in this message with the collection. If the digest's
    status is skipped, return it unchanged with entities_linked set to 0 and do nothing else.

    Read the digest inside your program: call read_page with the collection as `knowledge_base` and the
    digest id as `page_id`, and keep the object it answers. Its `payload` is the page exactly as write_note
    takes it (frontmatter, sections, claims) and its `path` is where the page lives. Never print the whole
    payload and never retype any of it: print only what you need, a part at a time, such as each claim's
    index and text. When read_page refuses the page, write nothing and return the digest id, its status and
    entities_linked set to 0.

    Call knowledge_base_profile with the collection as `knowledge_base`. Its `entity_page_type` is the page
    type an entity page is written as. If it is empty, the base keeps no entity pages: return the digest
    unchanged with entities_linked set to 0. Under vocabulary, `entity_types` lists the types an entity may
    take, and `claim_link_fields` lists the claim fields that hold a link, each by `field` with whether it
    `holds` one link or a list. Under page_types, the digest's own page type lists its `edge_fields`: each
    field whose `range` is the entity page type is an entity link field. When there are several, use every
    one whose relation `description` fits the entities the source names, and the first when unsure. With
    none, link only the claims.

    With the payload in hand, gather every name it needs linked: for each claim, what each claim link field
    refers to (read it from the claim's text and quote), and every other named thing of one of the
    entity_types that the source itself names and that bears on the base's purpose. When it needs no name
    linked, write nothing and return the digest id, its status and entities_linked set to 0. Otherwise give
    each name once, as the source writes it, with its entity type from entity_types, the other names the
    source uses for it as aliases, and a one-sentence summary of what it is. Send them all to
    resolve_entities in one call, with the collection as `knowledge_base`. It answers one id per name, in
    the order you sent them. A refusal names every offender and writes nothing: fix them all and call it
    again.

    Then set the link fields on that payload in code and pass the same object to write_note as `payload`,
    with its `path` as `path`. Set each claim link field to the id of the entity it refers to, as the string
    '[[page.id]]' with the brackets, or a list of those when the field holds a list. Leave a claim link
    field empty when the claim refers to no named entity. Set each entity link field to the ids
    resolve_entities answered for the entities it takes, each as '[[page.id]]', or to the one id the digest
    is most about when the field holds one link. A bare id, a 'page:' prefix, or markdown link syntax is
    refused as not a link. When write_note refuses the page naming a link field, fix exactly that link field
    on the payload and write again. When the refusal names anything else, write nothing and return the
    digest id, its status and entities_linked set to 0. An answer that says indexing is deferred to the end
    of the run is a success.

    Return the digest id, its status, and how many distinct entity ids the digest now links.
  inputs:
    digest: {like: digest_source}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: LinkResult
      required: [digest_id, status, entities_linked]
      properties:
        status:
          enum: [written, skipped]
          type: string
        digest_id: {type: string}
        entities_linked: {type: integer}
  tools: [knowledge_base_profile, read_page, resolve_entities, write_note]
---

Use once per written digest, as a turn of its own, after the digest is indexed, in a base whose schema names an entity
page type. It reads the digest with read_page and every field name it fills from the base's profile. Do not use to write
an entity page by hand: only resolve_entities writes those. Do not use to link a digest to other digests: it links
entities only.
