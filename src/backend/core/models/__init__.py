"""Core data models for the test self-healing system."""

from .healing_models import (
    FailureContext,
    ElementFingerprint,
    HealingReport,
    HealingSession,
    HealingConfiguration,
    LocatorAttempt,
    ValidationResult,
    MatchResult,
    FailureType,
    LocatorStrategy,
    HealingStatus
)

# Note: Service classes are imported separately from their respective modules

__all__ = [
    "FailureContext",
    "ElementFingerprint", 
    "HealingReport",
    "HealingSession",
    "HealingConfiguration",
    "LocatorAttempt",
    "ValidationResult",
    "MatchResult",
    "FailureType",
    "LocatorStrategy",
    "HealingStatus"
]