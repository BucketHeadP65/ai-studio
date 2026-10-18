"""The errors the projects slice raises when it refuses a request, each carrying the reason as its message."""


class ProjectError(Exception):
    """A request on a project's files, owner notes or skills that the slice refuses, with the reason."""


class ProjectRequestRefusedError(ProjectError):
    """The request itself is refused: a path outside the project, a directory named as a file, or an agent reply to edit."""


class ProjectTargetMissingError(ProjectError):
    """Nothing is at the path, the note id or the reply position the request names."""


class ProjectTargetConflictError(ProjectError):
    """Something is already where the request would create, or the file changed since the caller read it."""
