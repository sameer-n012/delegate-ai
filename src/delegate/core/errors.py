"""Domain errors. Adapters map these to CLI exit codes, MCP tool errors, or HTTP statuses."""


class DelegateError(Exception):
    """Base class for all expected, user-facing errors."""

    code: str = "error"


class NotFoundError(DelegateError):
    code = "not_found"


class ValidationError(DelegateError):
    code = "invalid"


class ConflictError(DelegateError):
    """Optimistic-concurrency failure, or the task is no longer in the expected state."""

    code = "conflict"


class NotHolderError(DelegateError):
    """The actor tried to change a task that another agent holds."""

    code = "not_holder"


class IdentityRequiredError(DelegateError):
    """A write was attempted without an actor id (DELEGATE_AGENT or --as)."""

    code = "no_identity"


class ForbiddenError(DelegateError):
    """The actor may not perform this action (e.g. an agent changing project state)."""

    code = "forbidden"


class ProjectClosedError(DelegateError):
    """An agent tried to change tasks in a project that is not in_progress."""

    code = "project_not_in_progress"


class InvalidTransitionError(DelegateError):
    code = "invalid_transition"


class CycleError(DelegateError):
    """A dependency or parent link would create a cycle."""

    code = "cycle"
