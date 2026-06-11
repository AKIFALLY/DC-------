class PELError(Exception):
    """PEL-5000C driver base exception."""


class PELConnectionError(PELError):
    """Raised when TCP socket cannot be opened or is unexpectedly closed."""


class PELCommandError(PELError):
    """Raised when an SCPI command is rejected or returns an error response."""


class PELTimeoutError(PELError):
    """Raised when a query exceeds the configured timeout."""


class PELSafetyError(PELError):
    """Raised when a measured value exceeds a configured safety limit."""
