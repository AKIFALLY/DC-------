class FotekError(Exception):
    """FOTEK NT 系列驅動 base exception."""


class FotekConnectionError(FotekError):
    """Raised when serial port cannot be opened or is unexpectedly closed."""


class FotekTimeoutError(FotekError):
    """Raised when a Modbus read times out."""


class FotekCommandError(FotekError):
    """Raised when a Modbus response is malformed, has bad CRC, or returns an error code."""
