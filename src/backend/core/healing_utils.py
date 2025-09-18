"""Utility functions for working with self-healing data models."""

import uuid
from datetime import datetime
from typing import Dict, Any, Optional

from .models.healing_models import (
    FailureContext,
    FailureType,
    HealingSession,
    HealingStatus,
    LocatorAttempt,
    LocatorStrategy,
    ValidationResult
)


def create_failure_context(
    test_file: str,
    test_case: str,
    failing_step: str,
    original_locator: str,
    target_url: str,
    exception_type: str,
    exception_message: str,
    run_id: str,
    additional_context: Optional[Dict[str, Any]] = None
) -> FailureContext:
    """Create a FailureContext with automatic failure type classification.
    
    Args:
        test_file: Path to the test file
        test_case: Name of the test case
        failing_step: Description of the failing step
        original_locator: The locator that failed
        target_url: URL where the failure occurred
        exception_type: Type of exception raised
        exception_message: Exception message
        run_id: Unique identifier for the test run
        additional_context: Optional additional context data
        
    Returns:
        FailureContext: Populated failure context
    """
    failure_type = classify_failure_type(exception_type, exception_message)
    
    return FailureContext(
        test_file=test_file,
        test_case=test_case,
        failing_step=failing_step,
        original_locator=original_locator,
        target_url=target_url,
        exception_type=exception_type,
        exception_message=exception_message,
        timestamp=datetime.now(),
        run_id=run_id,
        failure_type=failure_type,
        additional_context=additional_context or {}
    )


def classify_failure_type(exception_type: str, exception_message: str) -> FailureType:
    """Classify the type of failure based on exception details.
    
    Args:
        exception_type: Type of exception
        exception_message: Exception message
        
    Returns:
        FailureType: Classified failure type
    """
    exception_type_lower = exception_type.lower()
    message_lower = exception_message.lower()
    
    # Check for element not found errors
    if ("nosuchelementexception" in exception_type_lower or
        "element not found" in message_lower or
        "unable to locate element" in message_lower or
        "no such element" in message_lower):
        return FailureType.ELEMENT_NOT_FOUND
    
    # Check for interactability errors
    if ("elementnotinteractableexception" in exception_type_lower or
        "element not interactable" in message_lower or
        "element is not clickable" in message_lower):
        return FailureType.ELEMENT_NOT_INTERACTABLE
    
    # Check for timeout errors
    if ("timeoutexception" in exception_type_lower or
        "timeout" in message_lower or
        "timed out" in message_lower):
        return FailureType.TIMEOUT
    
    # Check for stale element errors
    if ("staleelementreferenceexception" in exception_type_lower or
        "stale element" in message_lower or
        "element is no longer attached" in message_lower):
        return FailureType.STALE_ELEMENT
    
    return FailureType.OTHER


def create_healing_session(failure_context: FailureContext) -> HealingSession:
    """Create a new healing session for a failure.
    
    Args:
        failure_context: The failure to heal
        
    Returns:
        HealingSession: New healing session
    """
    session_id = str(uuid.uuid4())
    
    return HealingSession(
        session_id=session_id,
        failure_context=failure_context,
        status=HealingStatus.PENDING,
        started_at=datetime.now()
    )


def create_locator_attempt(
    locator: str,
    strategy: LocatorStrategy,
    success: bool,
    error_message: Optional[str] = None,
    confidence_score: float = 0.0,
    execution_time: float = 0.0
) -> LocatorAttempt:
    """Create a locator attempt record.
    
    Args:
        locator: The locator that was attempted
        strategy: Strategy used for the locator
        success: Whether the attempt was successful
        error_message: Error message if failed
        confidence_score: Confidence in the locator (0.0-1.0)
        execution_time: Time taken for the attempt
        
    Returns:
        LocatorAttempt: Attempt record
    """
    return LocatorAttempt(
        locator=locator,
        strategy=strategy,
        success=success,
        error_message=error_message,
        confidence_score=confidence_score,
        execution_time=execution_time,
        timestamp=datetime.now()
    )


def create_validation_result(
    locator: str,
    strategy: LocatorStrategy,
    is_valid: bool,
    element_found: bool = False,
    is_interactable: bool = False,
    matches_expected_type: bool = False,
    confidence_score: float = 0.0,
    error_message: Optional[str] = None,
    element_properties: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    """Create a validation result.
    
    Args:
        locator: The locator that was validated
        strategy: Strategy used for the locator
        is_valid: Overall validation result
        element_found: Whether element was found
        is_interactable: Whether element is interactable
        matches_expected_type: Whether element matches expected type
        confidence_score: Confidence in the validation
        error_message: Error message if validation failed
        element_properties: Properties of the found element
        
    Returns:
        ValidationResult: Validation result
    """
    return ValidationResult(
        locator=locator,
        strategy=strategy,
        is_valid=is_valid,
        element_found=element_found,
        is_interactable=is_interactable,
        matches_expected_type=matches_expected_type,
        confidence_score=confidence_score,
        error_message=error_message,
        element_properties=element_properties or {}
    )


def is_healable_failure(failure_context: FailureContext) -> bool:
    """Determine if a failure can be healed.
    
    Args:
        failure_context: The failure context to check
        
    Returns:
        bool: True if the failure can potentially be healed
    """
    # Only certain failure types are healable
    healable_types = {
        FailureType.ELEMENT_NOT_FOUND,
        FailureType.ELEMENT_NOT_INTERACTABLE,
        FailureType.TIMEOUT,
        FailureType.STALE_ELEMENT
    }
    
    return failure_context.failure_type in healable_types


def calculate_confidence_score(
    element_found: bool,
    is_interactable: bool,
    matches_expected_type: bool,
    has_stable_attributes: bool = False
) -> float:
    """Calculate confidence score for a locator validation.
    
    Args:
        element_found: Whether element was found
        is_interactable: Whether element is interactable
        matches_expected_type: Whether element matches expected type
        has_stable_attributes: Whether element has stable identifying attributes
        
    Returns:
        float: Confidence score between 0.0 and 1.0
    """
    if not element_found:
        return 0.0
    
    score = 0.4  # Base score for finding element
    
    if is_interactable:
        score += 0.3
    
    if matches_expected_type:
        score += 0.2
    
    if has_stable_attributes:
        score += 0.1
    
    return min(score, 1.0)