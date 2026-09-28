"""Domain exception types.

``InputError`` maps to HTTP 400.  Every other exception is a server fault.
"""


class InputError(Exception):
    """The request payload is structurally or semantically invalid."""


class NotFound(Exception):
    """The referenced document / revision / finalization does not exist."""


class Conflict(Exception):
    """The operation is incompatible with current state (HTTP 409)."""


class InfeasibleCompensation(Conflict):
    """Faulty-pin compensation cannot cover the finalization (HTTP 409).

    Carries a structured detail payload (first unreachable cell, missing
    dot numbers, verified page prefix) returned alongside the error.
    """

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}
