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


class InvalidTransitionError(DelegateError):
    code = "invalid_transition"


class CycleError(DelegateError):
    """A dependency or parent link would create a cycle."""

    code = "cycle"
