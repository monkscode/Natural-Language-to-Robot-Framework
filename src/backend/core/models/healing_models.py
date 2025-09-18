"""Data models for the test self-healing system."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
from enum import Enum


class FailureType(Enum):
    """Types of test failures that can be healed."""
    ELEMENT_NOT_FOUND = "element_not_found"
    ELEMENT_NOT_INTERACTABLE = "element_not_interactable"
    TIMEOUT = "timeout"
    STALE_ELEMENT = "stale_element"
    OTHER = "other"


class LocatorStrategy(Enum):
    """Supported locator strategies in priority order."""
    ID = "id"
    NAME = "name"
    CSS = "css"
    XPATH = "xpath"
    LINK_TEXT = "link_text"
    PARTIAL_LINK_TEXT = "partial_link_text"
    TAG_NAME = "tag_name"
    CLASS_NAME = "class_name"


class HealingStatus(Enum):
    """Status of a healing session."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DISABLED = "disabled"


@dataclass
class FailureContext:
    """Context information about a test failure that can be healed."""
    test_file: str
    test_case: str
    failing_step: str
    original_locator: str
    target_url: str
    exception_type: str
    exception_message: str
    timestamp: datetime
    run_id: str
    failure_type: FailureType = FailureType.OTHER
    additional_context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ElementFingerprint:
    """Unique signature of a web element for better matching."""
    tag_name: str
    attributes: Dict[str, str]
    text_content: str
    parent_context: List[str]
    sibling_context: List[str]
    dom_path: str
    visual_hash: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert fingerprint to dictionary for storage."""
        return {
            "tag_name": self.tag_name,
            "attributes": self.attributes,
            "text_content": self.text_content,
            "parent_context": self.parent_context,
            "sibling_context": self.sibling_context,
            "dom_path": self.dom_path,
            "visual_hash": self.visual_hash,
            "created_at": self.created_at.isoformat()
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ElementFingerprint':
        """Create fingerprint from dictionary."""
        data = data.copy()
        if "created_at" in data:
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**data)


@dataclass
class LocatorAttempt:
    """Record of a locator healing attempt."""
    locator: str
    strategy: LocatorStrategy
    success: bool
    error_message: Optional[str] = None
    confidence_score: float = 0.0
    execution_time: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ValidationResult:
    """Result of validating a locator against a live session."""
    locator: str
    strategy: LocatorStrategy
    is_valid: bool
    element_found: bool
    is_interactable: bool
    matches_expected_type: bool
    confidence_score: float
    error_message: Optional[str] = None
    element_properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MatchResult:
    """Result of matching an element fingerprint."""
    matched: bool
    confidence_score: float
    matching_elements: List[str] = field(default_factory=list)
    best_match_locator: Optional[str] = None
    match_details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HealingSession:
    """Represents a complete healing session for a failed test."""
    session_id: str
    failure_context: FailureContext
    status: HealingStatus
    started_at: datetime
    completed_at: Optional[datetime] = None
    attempts: List[LocatorAttempt] = field(default_factory=list)
    successful_locator: Optional[str] = None
    backup_file_path: Optional[str] = None
    chrome_session_id: Optional[str] = None
    error_message: Optional[str] = None
    
    @property
    def duration(self) -> Optional[float]:
        """Calculate session duration in seconds."""
        if self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate of attempts."""
        if not self.attempts:
            return 0.0
        successful = sum(1 for attempt in self.attempts if attempt.success)
        return successful / len(self.attempts)


@dataclass
class HealingReport:
    """Comprehensive report of healing activities."""
    session: HealingSession
    original_failure: FailureContext
    healing_summary: Dict[str, Any]
    performance_metrics: Dict[str, float]
    recommendations: List[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert report to dictionary for API responses."""
        return {
            "session_id": self.session.session_id,
            "status": self.session.status.value,
            "original_locator": self.original_failure.original_locator,
            "healed_locator": self.session.successful_locator,
            "duration": self.session.duration,
            "attempts_count": len(self.session.attempts),
            "success_rate": self.session.success_rate,
            "healing_summary": self.healing_summary,
            "performance_metrics": self.performance_metrics,
            "recommendations": self.recommendations,
            "generated_at": self.generated_at.isoformat()
        }


@dataclass
class HealingConfiguration:
    """Configuration settings for the self-healing system."""
    enabled: bool = True
    max_attempts_per_locator: int = 3
    chrome_session_timeout: int = 30  # seconds
    healing_timeout: int = 300  # seconds (5 minutes)
    max_concurrent_sessions: int = 3
    backup_retention_days: int = 7
    
    # Failure detection settings
    enable_fingerprinting: bool = True
    confidence_threshold: float = 0.7
    
    # Locator generation settings
    strategies: List[LocatorStrategy] = field(default_factory=lambda: [
        LocatorStrategy.ID,
        LocatorStrategy.NAME,
        LocatorStrategy.CSS,
        LocatorStrategy.XPATH,
        LocatorStrategy.LINK_TEXT
    ])
    max_alternatives: int = 5
    
    # Validation settings
    element_wait_timeout: int = 10  # seconds
    interaction_test: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "enabled": self.enabled,
            "max_attempts_per_locator": self.max_attempts_per_locator,
            "chrome_session_timeout": self.chrome_session_timeout,
            "healing_timeout": self.healing_timeout,
            "max_concurrent_sessions": self.max_concurrent_sessions,
            "backup_retention_days": self.backup_retention_days,
            "enable_fingerprinting": self.enable_fingerprinting,
            "confidence_threshold": self.confidence_threshold,
            "strategies": [s.value for s in self.strategies],
            "max_alternatives": self.max_alternatives,
            "element_wait_timeout": self.element_wait_timeout,
            "interaction_test": self.interaction_test
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'HealingConfiguration':
        """Create configuration from dictionary."""
        data = data.copy()
        if "strategies" in data:
            data["strategies"] = [LocatorStrategy(s) for s in data["strategies"]]
        return cls(**data)