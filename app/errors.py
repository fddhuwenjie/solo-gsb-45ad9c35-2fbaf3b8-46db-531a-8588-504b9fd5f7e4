"""Domain exception types.

``InputError`` maps to HTTP 400.  Every other exception is a server fault.
"""


class InputError(Exception):
    """The request payload is structurally or semantically invalid."""


class NotFound(Exception):
    """The referenced document / revision / finalization does not exist."""


class Conflict(Exception):
    """The operation is incompatible with current state (HTTP 409)."""
