"""The refusals of the registry of knowledge bases, and the error that carries one."""

from __future__ import annotations

from enum import StrEnum


class KnowledgeBaseRefusal(StrEnum):
    """Why the registry of knowledge bases refused a request. Each value is the `error` code the HTTP answer carries."""

    SCHEMA_AND_TEMPLATE = "schema_and_template"
    UNKNOWN_TEMPLATE = "unknown_template"
    SCHEMA_OR_TEMPLATE_REQUIRED = "schema_or_template_required"
    HIDDEN_DIRECTORY = "hidden_directory"
    NAME_COLLECTION_MISMATCH = "name_collection_mismatch"
    INVALID_PATH = "invalid_path"
    SOURCES_WITHOUT_QUEUE = "sources_without_queue"
    KNOWLEDGE_BASE_REQUIRED = "knowledge_base_required"
    UNKNOWN_SCANNER = "unknown_scanner"
    UNKNOWN_KNOWLEDGE_BASE = "unknown_knowledge_base"
    SCHEMA_NOT_FOUND = "spec_not_found"
    UNKNOWN_JOB = "unknown_job"
    UNKNOWN_PAGE = "unknown_page"
    NAME_COLLISION = "name_collision"
    PATH_COLLISION = "path_collision"
    DIRECTORY_NOT_EMPTY = "directory_not_empty"
    BROKEN_KNOWLEDGE_BASE = "broken_knowledge_base"
    SOURCE_QUEUE_UNREADABLE = "source_queue_unreadable"
    WATCH_LIST_UNREADABLE = "watch_list_unreadable"
    NOTHING_TO_RETRY = "nothing_to_retry"
    INVALID_DECISION = "invalid_decision"
    TEMPLATE_SCHEMA_UNREADABLE = "template_schema_unreadable"
    REGISTRY_WRITE_REFUSED = "registry_write_refused"


class KnowledgeBaseRefusedError(Exception):
    """One request the registry of knowledge bases will not carry out, with the refusal and the sentence a person reads."""

    def __init__(self, refusal: KnowledgeBaseRefusal, message: str) -> None:
        """Hold the refusal beside the message.

        Args:
            refusal: Why the request was refused.
            message: The sentence a person reads.
        """
        super().__init__(message)
        self.refusal = refusal


def unknown_knowledge_base_error(name: str) -> KnowledgeBaseRefusedError:
    """The refusal for a name that no registered knowledge base holds.

    Args:
        name: The name the request gave.

    Returns:
        The error to raise.
    """
    return KnowledgeBaseRefusedError(KnowledgeBaseRefusal.UNKNOWN_KNOWLEDGE_BASE, f"no knowledge base named {name!r} is registered")
