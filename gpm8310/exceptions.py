class GPMError(Exception):
    """GPM-8310 driver base exception."""


class GPMConnectionError(GPMError):
    """Raised when TCP socket cannot be opened or is unexpectedly closed."""


class GPMCommandError(GPMError):
    """Raised when an SCPI command is rejected or returns an error response."""


class GPMTimeoutError(GPMError):
    """Raised when a query exceeds the configured timeout."""
