"""Domain exceptions used across the application."""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected, user-facing application errors."""


class ConfigError(AppError):
    """The analysis configuration is missing or invalid."""


class DataError(AppError):
    """Input data cannot be imported or interpreted safely."""


class NotFoundError(AppError):
    """A requested entity does not exist."""


class PermissionDenied(AppError):
    """A role attempted an action outside its remit."""


class WorkflowError(AppError):
    """A state transition or decision constraint was violated."""
