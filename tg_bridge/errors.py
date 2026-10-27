"""Bridge-level errors. Messages are shown to the user/model, so they never contain secrets or PII."""


class TgBridgeError(Exception):
    """Base class for anticipated failures."""


class ConfigError(TgBridgeError):
    """Settings are missing or invalid."""


class NotLoggedInError(TgBridgeError):
    """The session file is missing or not authorized."""


class RateLimitedError(TgBridgeError):
    """Telegram asked us to wait (FloodWait)."""

    def __init__(self, seconds: int):
        super().__init__(f"Telegram rate limit: retry in {seconds} s")
        self.seconds = seconds
