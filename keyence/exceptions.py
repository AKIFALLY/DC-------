class KVError(Exception):
    """Keyence KV 上位鏈路驅動 base exception."""


class KVConnectionError(KVError):
    """Raised when TCP socket cannot be opened or is unexpectedly closed."""


class KVCommandError(KVError):
    """Raised when the PLC returns an error response (E0/E1/E4...) or malformed reply."""


class KVTimeoutError(KVError):
    """Raised when a command response exceeds the configured timeout."""
