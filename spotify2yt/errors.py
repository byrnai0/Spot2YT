class S2YError(RuntimeError):
    """A user-facing error. The CLI prints the message without a traceback."""


class AuthError(S2YError):
    """Missing or expired credentials."""


class PlaylistError(S2YError):
    """A playlist could not be resolved or validated."""
