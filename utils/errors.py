"""Momathi Protocol — Custom exceptions."""


class ParadexAPIError(Exception):
    """Raised when Paradex exchange API calls fail."""
    pass


class TradeExecutionError(Exception):
    """Raised when trade execution logic fails."""
    pass


class ConfigError(Exception):
    """Raised when environment configuration validation fails."""
    pass
