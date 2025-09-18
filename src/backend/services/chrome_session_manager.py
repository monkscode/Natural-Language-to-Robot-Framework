"""Chrome session manager for test self-healing validation."""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, AsyncGenerator
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    WebDriverException,
    NoSuchElementException,
    TimeoutException,
    ElementNotInteractableException,
    StaleElementReferenceException
)
try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    # Fallback if webdriver_manager is not available
    ChromeDriverManager = None

from ..core.models.healing_models import ValidationResult, LocatorStrategy, HealingConfiguration


logger = logging.getLogger(__name__)


@dataclass
class ChromeSession:
    """Represents a Chrome browser session for locator validation."""
    session_id: str
    driver: webdriver.Chrome
    created_at: datetime
    last_used: datetime
    current_url: Optional[str] = None
    is_active: bool = True
    usage_count: int = 0
    
    def update_last_used(self):
        """Update the last used timestamp."""
        self.last_used = datetime.now()
        self.usage_count += 1
    
    def is_expired(self, timeout_seconds: int) -> bool:
        """Check if session has expired based on timeout."""
        return (datetime.now() - self.last_used).total_seconds() > timeout_seconds
    
    def close(self):
        """Close the Chrome session."""
        try:
            if self.driver:
                self.driver.quit()
        except Exception as e:
            logger.warning(f"Error closing Chrome session {self.session_id}: {e}")
        finally:
            self.is_active = False


@dataclass
class SessionPool:
    """Pool of Chrome sessions with resource management."""
    sessions: Dict[str, ChromeSession] = field(default_factory=dict)
    max_sessions: int = 3
    session_timeout: int = 30
    _lock: Lock = field(default_factory=Lock)
    
    def add_session(self, session: ChromeSession) -> bool:
        """Add a session to the pool if there's capacity."""
        with self._lock:
            if len(self.sessions) >= self.max_sessions:
                return False
            self.sessions[session.session_id] = session
            return True
    
    def get_session(self, session_id: str) -> Optional[ChromeSession]:
        """Get a session by ID."""
        with self._lock:
            return self.sessions.get(session_id)
    
    def remove_session(self, session_id: str) -> Optional[ChromeSession]:
        """Remove and return a session from the pool."""
        with self._lock:
            return self.sessions.pop(session_id, None)
    
    def get_available_session(self, url: str) -> Optional[ChromeSession]:
        """Get an available session, preferring one already on the target URL."""
        with self._lock:
            # First, try to find a session already on the target URL
            for session in self.sessions.values():
                if (session.is_active and 
                    session.current_url == url and 
                    not session.is_expired(self.session_timeout)):
                    return session
            
            # Then, try to find any available session
            for session in self.sessions.values():
                if (session.is_active and 
                    not session.is_expired(self.session_timeout)):
                    return session
            
            return None
    
    def cleanup_expired_sessions(self):
        """Remove and close expired sessions."""
        expired_sessions = []
        with self._lock:
            for session_id, session in list(self.sessions.items()):
                if not session.is_active or session.is_expired(self.session_timeout):
                    expired_sessions.append(self.sessions.pop(session_id))
        
        for session in expired_sessions:
            session.close()
            logger.info(f"Cleaned up expired Chrome session {session.session_id}")
    
    def close_all_sessions(self):
        """Close all sessions in the pool."""
        with self._lock:
            sessions_to_close = list(self.sessions.values())
            self.sessions.clear()
        
        for session in sessions_to_close:
            session.close()
        
        logger.info(f"Closed {len(sessions_to_close)} Chrome sessions")
    
    def get_pool_stats(self) -> Dict[str, Any]:
        """Get statistics about the session pool."""
        with self._lock:
            active_sessions = sum(1 for s in self.sessions.values() if s.is_active)
            expired_sessions = sum(1 for s in self.sessions.values() 
                                 if s.is_expired(self.session_timeout))
            total_usage = sum(s.usage_count for s in self.sessions.values())
            
            return {
                "total_sessions": len(self.sessions),
                "active_sessions": active_sessions,
                "expired_sessions": expired_sessions,
                "max_sessions": self.max_sessions,
                "total_usage": total_usage,
                "session_timeout": self.session_timeout
            }


class ChromeSessionManager:
    """Manages Chrome browser sessions for locator validation."""
    
    def __init__(self, config: HealingConfiguration):
        """Initialize the Chrome session manager."""
        self.config = config
        self.pool = SessionPool(
            max_sessions=config.max_concurrent_sessions,
            session_timeout=config.chrome_session_timeout
        )
        self.executor = ThreadPoolExecutor(max_workers=config.max_concurrent_sessions)
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Chrome options for headless operation
        self.chrome_options = self._create_chrome_options()
        
        logger.info(f"Chrome session manager initialized with max {config.max_concurrent_sessions} sessions")
    
    def _create_chrome_options(self) -> Options:
        """Create Chrome options for headless operation."""
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-plugins")
        options.add_argument("--disable-images")
        options.add_argument("--disable-javascript")  # We don't need JS for locator validation
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        # Performance optimizations
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        
        return options
    
    async def start(self):
        """Start the session manager and cleanup task."""
        if self._running:
            return
        
        self._running = True
        # Start periodic cleanup task
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info("Chrome session manager started")
    
    async def stop(self):
        """Stop the session manager and cleanup resources."""
        if not self._running:
            return
        
        self._running = False
        
        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # Close all sessions
        self.pool.close_all_sessions()
        
        # Shutdown executor
        self.executor.shutdown(wait=True)
        
        logger.info("Chrome session manager stopped")
    
    async def _periodic_cleanup(self):
        """Periodically clean up expired sessions."""
        while self._running:
            try:
                await asyncio.sleep(30)  # Cleanup every 30 seconds
                self.pool.cleanup_expired_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error during session cleanup: {e}")
    
    def _create_chrome_session(self) -> ChromeSession:
        """Create a new Chrome session."""
        try:
            # Use webdriver-manager to handle ChromeDriver installation if available
            if ChromeDriverManager is not None:
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=self.chrome_options)
            else:
                # Fallback to system ChromeDriver
                driver = webdriver.Chrome(options=self.chrome_options)
            
            session = ChromeSession(
                session_id=str(uuid.uuid4()),
                driver=driver,
                created_at=datetime.now(),
                last_used=datetime.now()
            )
            
            logger.info(f"Created new Chrome session {session.session_id}")
            return session
            
        except Exception as e:
            logger.error(f"Failed to create Chrome session: {e}")
            raise
    
    async def get_session(self, url: str) -> ChromeSession:
        """Get or create a Chrome session for the given URL."""
        # Try to get an existing session
        session = self.pool.get_available_session(url)
        
        if session:
            session.update_last_used()
            
            # Navigate to URL if different
            if session.current_url != url:
                await self._navigate_to_url(session, url)
            
            return session
        
        # Create new session if pool has capacity
        if len(self.pool.sessions) < self.pool.max_sessions:
            session = await asyncio.get_event_loop().run_in_executor(
                self.executor, self._create_chrome_session
            )
            
            if self.pool.add_session(session):
                await self._navigate_to_url(session, url)
                return session
            else:
                # Pool became full while creating session
                session.close()
        
        # Pool is full, wait for a session to become available
        max_wait_time = 60  # seconds
        start_time = time.time()
        
        while time.time() - start_time < max_wait_time:
            self.pool.cleanup_expired_sessions()
            session = self.pool.get_available_session(url)
            
            if session:
                session.update_last_used()
                if session.current_url != url:
                    await self._navigate_to_url(session, url)
                return session
            
            await asyncio.sleep(1)
        
        raise RuntimeError("No Chrome sessions available and timeout reached")
    
    async def _navigate_to_url(self, session: ChromeSession, url: str):
        """Navigate a session to the specified URL."""
        try:
            await asyncio.get_event_loop().run_in_executor(
                self.executor, session.driver.get, url
            )
            session.current_url = url
            logger.debug(f"Session {session.session_id} navigated to {url}")
        except Exception as e:
            logger.error(f"Failed to navigate session {session.session_id} to {url}: {e}")
            raise
    
    async def validate_locator(
        self, 
        session: ChromeSession, 
        locator: str, 
        strategy: LocatorStrategy,
        expected_properties: Optional[Dict[str, Any]] = None
    ) -> ValidationResult:
        """Validate a locator against the live Chrome session."""
        start_time = time.time()
        
        try:
            # Map strategy to Selenium By
            by_mapping = {
                LocatorStrategy.ID: By.ID,
                LocatorStrategy.NAME: By.NAME,
                LocatorStrategy.CSS: By.CSS_SELECTOR,
                LocatorStrategy.XPATH: By.XPATH,
                LocatorStrategy.LINK_TEXT: By.LINK_TEXT,
                LocatorStrategy.PARTIAL_LINK_TEXT: By.PARTIAL_LINK_TEXT,
                LocatorStrategy.TAG_NAME: By.TAG_NAME,
                LocatorStrategy.CLASS_NAME: By.CLASS_NAME
            }
            
            by_method = by_mapping.get(strategy)
            if not by_method:
                return ValidationResult(
                    locator=locator,
                    strategy=strategy,
                    is_valid=False,
                    element_found=False,
                    is_interactable=False,
                    matches_expected_type=False,
                    confidence_score=0.0,
                    error_message=f"Unsupported locator strategy: {strategy}"
                )
            
            # Validate locator in executor to avoid blocking
            result = await asyncio.get_event_loop().run_in_executor(
                self.executor, 
                self._validate_locator_sync, 
                session, 
                locator, 
                strategy, 
                by_method, 
                expected_properties
            )
            
            execution_time = time.time() - start_time
            logger.debug(f"Locator validation completed in {execution_time:.2f}s: {locator}")
            
            return result
            
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"Locator validation failed after {execution_time:.2f}s: {e}")
            
            return ValidationResult(
                locator=locator,
                strategy=strategy,
                is_valid=False,
                element_found=False,
                is_interactable=False,
                matches_expected_type=False,
                confidence_score=0.0,
                error_message=str(e)
            )
    
    def _validate_locator_sync(
        self, 
        session: ChromeSession, 
        locator: str, 
        strategy: LocatorStrategy,
        by_method: By,
        expected_properties: Optional[Dict[str, Any]]
    ) -> ValidationResult:
        """Synchronous locator validation (runs in executor)."""
        element = None
        element_found = False
        
        try:
            session.update_last_used()
            
            # Wait for element to be present
            wait = WebDriverWait(session.driver, self.config.element_wait_timeout)
            element = wait.until(EC.presence_of_element_located((by_method, locator)))
            element_found = True
            
        except TimeoutException:
            return ValidationResult(
                locator=locator,
                strategy=strategy,
                is_valid=False,
                element_found=False,
                is_interactable=False,
                matches_expected_type=False,
                confidence_score=0.0,
                error_message="Element not found within timeout"
            )
            
        except NoSuchElementException:
            return ValidationResult(
                locator=locator,
                strategy=strategy,
                is_valid=False,
                element_found=False,
                is_interactable=False,
                matches_expected_type=False,
                confidence_score=0.0,
                error_message="Element not found"
            )
            
        except ElementNotInteractableException:
            # For this exception, we know the element exists but is not interactable
            return ValidationResult(
                locator=locator,
                strategy=strategy,
                is_valid=False,
                element_found=True,
                is_interactable=False,
                matches_expected_type=False,
                confidence_score=0.2,
                error_message="Element found but not interactable"
            )
        
        # If we get here, element was found successfully
        element_properties = {}
        
        # Gather element properties
        try:
            element_properties = {
                "tag_name": element.tag_name,
                "text": element.text,
                "is_displayed": element.is_displayed(),
                "is_enabled": element.is_enabled(),
                "location": element.location,
                "size": element.size
            }
            
            # Get common attributes
            for attr in ["id", "class", "name", "type", "value", "href"]:
                value = element.get_attribute(attr)
                if value:
                    element_properties[attr] = value
                    
        except StaleElementReferenceException:
            # Element became stale, try to find it again
            try:
                element = session.driver.find_element(by_method, locator)
                element_properties = {"tag_name": element.tag_name}
            except Exception:
                element_properties = {"tag_name": "unknown"}
        except Exception:
            # If we can't get properties, use basic ones
            element_properties = {"tag_name": "unknown"}
        
        # Test interactability if configured
        is_interactable = True
        if self.config.interaction_test:
            try:
                location = element.location
                is_interactable = (
                    element.is_displayed() and 
                    element.is_enabled() and
                    location.get('x', 0) >= 0 and 
                    location.get('y', 0) >= 0
                )
            except Exception:
                is_interactable = False
        
        # Check if element matches expected properties
        matches_expected_type = True
        if expected_properties:
            for key, expected_value in expected_properties.items():
                actual_value = element_properties.get(key)
                if actual_value != expected_value:
                    matches_expected_type = False
                    break
        
        # Calculate confidence score
        confidence_score = self._calculate_confidence_score(
            element_found, is_interactable, matches_expected_type, strategy
        )
        
        is_valid = element_found and is_interactable and matches_expected_type
        
        try:
            return ValidationResult(
                locator=locator,
                strategy=strategy,
                is_valid=is_valid,
                element_found=element_found,
                is_interactable=is_interactable,
                matches_expected_type=matches_expected_type,
                confidence_score=confidence_score,
                element_properties=element_properties
            )
        except Exception as e:
            return ValidationResult(
                locator=locator,
                strategy=strategy,
                is_valid=False,
                element_found=False,
                is_interactable=False,
                matches_expected_type=False,
                confidence_score=0.0,
                error_message=f"Validation error: {str(e)}"
            )
    
    def _calculate_confidence_score(
        self, 
        element_found: bool, 
        is_interactable: bool, 
        matches_expected_type: bool,
        strategy: LocatorStrategy
    ) -> float:
        """Calculate confidence score for a validation result."""
        score = 0.0
        
        if element_found:
            score += 0.4
        
        if is_interactable:
            score += 0.3
        
        if matches_expected_type:
            score += 0.2
        
        # Strategy-based bonus (more stable strategies get higher scores)
        strategy_bonus = {
            LocatorStrategy.ID: 0.1,
            LocatorStrategy.NAME: 0.08,
            LocatorStrategy.CSS: 0.06,
            LocatorStrategy.XPATH: 0.04,
            LocatorStrategy.LINK_TEXT: 0.05,
            LocatorStrategy.PARTIAL_LINK_TEXT: 0.03,
            LocatorStrategy.TAG_NAME: 0.02,
            LocatorStrategy.CLASS_NAME: 0.04
        }
        
        score += strategy_bonus.get(strategy, 0.0)
        
        # Ensure perfect score is exactly 1.0
        if score >= 1.0:
            return 1.0
        
        return round(score, 2)
    
    @asynccontextmanager
    async def session_context(self, url: str) -> AsyncGenerator[ChromeSession, None]:
        """Context manager for Chrome sessions."""
        session = await self.get_session(url)
        try:
            yield session
        finally:
            # Session is returned to pool automatically
            pass
    
    def get_session_stats(self) -> Dict[str, Any]:
        """Get statistics about session usage."""
        pool_stats = self.pool.get_pool_stats()
        
        return {
            **pool_stats,
            "executor_threads": self.executor._threads if hasattr(self.executor, '_threads') else 0,
            "running": self._running
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Perform a health check of the session manager."""
        try:
            # Try to create a test session
            test_url = "data:text/html,<html><body><div id='test'>Test</div></body></html>"
            
            async with self.session_context(test_url) as session:
                # Try to validate a simple locator
                result = await self.validate_locator(
                    session, 
                    "test", 
                    LocatorStrategy.ID
                )
                
                health_status = {
                    "status": "healthy" if result.is_valid else "degraded",
                    "session_manager_running": self._running,
                    "test_validation_success": result.is_valid,
                    "pool_stats": self.get_session_stats(),
                    "timestamp": datetime.now().isoformat()
                }
                
                return health_status
                
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "session_manager_running": self._running,
                "pool_stats": self.get_session_stats(),
                "timestamp": datetime.now().isoformat()
            }