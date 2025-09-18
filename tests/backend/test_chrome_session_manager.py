"""Unit tests for Chrome session manager."""

import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from concurrent.futures import ThreadPoolExecutor

from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementNotInteractableException,
    StaleElementReferenceException
)

from src.backend.services.chrome_session_manager import (
    ChromeSession,
    SessionPool,
    ChromeSessionManager
)
from src.backend.core.models.healing_models import (
    HealingConfiguration,
    LocatorStrategy,
    ValidationResult
)


class TestChromeSession:
    """Test ChromeSession class."""
    
    def test_chrome_session_creation(self):
        """Test ChromeSession creation and properties."""
        mock_driver = Mock()
        session = ChromeSession(
            session_id="test-session",
            driver=mock_driver,
            created_at=datetime.now(),
            last_used=datetime.now()
        )
        
        assert session.session_id == "test-session"
        assert session.driver == mock_driver
        assert session.is_active is True
        assert session.usage_count == 0
        assert session.current_url is None
    
    def test_update_last_used(self):
        """Test updating last used timestamp."""
        mock_driver = Mock()
        session = ChromeSession(
            session_id="test-session",
            driver=mock_driver,
            created_at=datetime.now(),
            last_used=datetime.now() - timedelta(minutes=5)
        )
        
        old_last_used = session.last_used
        old_usage_count = session.usage_count
        
        session.update_last_used()
        
        assert session.last_used > old_last_used
        assert session.usage_count == old_usage_count + 1
    
    def test_is_expired(self):
        """Test session expiration check."""
        mock_driver = Mock()
        session = ChromeSession(
            session_id="test-session",
            driver=mock_driver,
            created_at=datetime.now(),
            last_used=datetime.now() - timedelta(seconds=60)
        )
        
        assert session.is_expired(30) is True
        assert session.is_expired(120) is False
    
    def test_close_session(self):
        """Test closing a session."""
        mock_driver = Mock()
        session = ChromeSession(
            session_id="test-session",
            driver=mock_driver,
            created_at=datetime.now(),
            last_used=datetime.now()
        )
        
        session.close()
        
        mock_driver.quit.assert_called_once()
        assert session.is_active is False
    
    def test_close_session_with_exception(self):
        """Test closing a session when driver.quit() raises exception."""
        mock_driver = Mock()
        mock_driver.quit.side_effect = Exception("Driver error")
        
        session = ChromeSession(
            session_id="test-session",
            driver=mock_driver,
            created_at=datetime.now(),
            last_used=datetime.now()
        )
        
        # Should not raise exception
        session.close()
        
        assert session.is_active is False


class TestSessionPool:
    """Test SessionPool class."""
    
    def test_session_pool_creation(self):
        """Test SessionPool creation."""
        pool = SessionPool(max_sessions=5, session_timeout=60)
        
        assert pool.max_sessions == 5
        assert pool.session_timeout == 60
        assert len(pool.sessions) == 0
    
    def test_add_session(self):
        """Test adding sessions to pool."""
        pool = SessionPool(max_sessions=2)
        
        session1 = self._create_mock_session("session1")
        session2 = self._create_mock_session("session2")
        session3 = self._create_mock_session("session3")
        
        assert pool.add_session(session1) is True
        assert pool.add_session(session2) is True
        assert pool.add_session(session3) is False  # Pool is full
        
        assert len(pool.sessions) == 2
    
    def test_get_session(self):
        """Test getting session by ID."""
        pool = SessionPool()
        session = self._create_mock_session("test-session")
        
        pool.add_session(session)
        
        retrieved = pool.get_session("test-session")
        assert retrieved == session
        
        not_found = pool.get_session("non-existent")
        assert not_found is None
    
    def test_remove_session(self):
        """Test removing session from pool."""
        pool = SessionPool()
        session = self._create_mock_session("test-session")
        
        pool.add_session(session)
        assert len(pool.sessions) == 1
        
        removed = pool.remove_session("test-session")
        assert removed == session
        assert len(pool.sessions) == 0
        
        not_found = pool.remove_session("non-existent")
        assert not_found is None
    
    def test_get_available_session_same_url(self):
        """Test getting available session with same URL preference."""
        pool = SessionPool()
        
        session1 = self._create_mock_session("session1", url="http://example.com")
        session2 = self._create_mock_session("session2", url="http://other.com")
        
        pool.add_session(session1)
        pool.add_session(session2)
        
        # Should prefer session with matching URL
        available = pool.get_available_session("http://example.com")
        assert available == session1
    
    def test_get_available_session_any_url(self):
        """Test getting any available session when no URL match."""
        pool = SessionPool()
        
        session1 = self._create_mock_session("session1", url="http://other.com")
        
        pool.add_session(session1)
        
        # Should return any available session
        available = pool.get_available_session("http://example.com")
        assert available == session1
    
    def test_get_available_session_expired(self):
        """Test that expired sessions are not returned."""
        pool = SessionPool(session_timeout=30)
        
        # Create expired session
        session = self._create_mock_session("session1")
        session.last_used = datetime.now() - timedelta(seconds=60)
        
        pool.add_session(session)
        
        available = pool.get_available_session("http://example.com")
        assert available is None
    
    def test_cleanup_expired_sessions(self):
        """Test cleanup of expired sessions."""
        pool = SessionPool(session_timeout=30)
        
        active_session = self._create_mock_session("active")
        expired_session = self._create_mock_session("expired")
        expired_session.last_used = datetime.now() - timedelta(seconds=60)
        
        pool.add_session(active_session)
        pool.add_session(expired_session)
        
        assert len(pool.sessions) == 2
        
        pool.cleanup_expired_sessions()
        
        assert len(pool.sessions) == 1
        assert "active" in pool.sessions
        expired_session.close.assert_called_once()
    
    def test_close_all_sessions(self):
        """Test closing all sessions in pool."""
        pool = SessionPool()
        
        session1 = self._create_mock_session("session1")
        session2 = self._create_mock_session("session2")
        
        pool.add_session(session1)
        pool.add_session(session2)
        
        pool.close_all_sessions()
        
        assert len(pool.sessions) == 0
        session1.close.assert_called_once()
        session2.close.assert_called_once()
    
    def test_get_pool_stats(self):
        """Test getting pool statistics."""
        pool = SessionPool(max_sessions=5, session_timeout=30)
        
        active_session = self._create_mock_session("active")
        active_session.usage_count = 10
        
        expired_session = self._create_mock_session("expired")
        expired_session.last_used = datetime.now() - timedelta(seconds=60)
        expired_session.usage_count = 5
        
        pool.add_session(active_session)
        pool.add_session(expired_session)
        
        stats = pool.get_pool_stats()
        
        assert stats["total_sessions"] == 2
        assert stats["active_sessions"] == 2  # Both are marked as active
        assert stats["expired_sessions"] == 1
        assert stats["max_sessions"] == 5
        assert stats["total_usage"] == 15
        assert stats["session_timeout"] == 30
    
    def _create_mock_session(self, session_id: str, url: str = None) -> ChromeSession:
        """Create a mock ChromeSession for testing."""
        mock_driver = Mock()
        session = ChromeSession(
            session_id=session_id,
            driver=mock_driver,
            created_at=datetime.now(),
            last_used=datetime.now(),
            current_url=url
        )
        session.close = Mock()
        return session


class TestChromeSessionManager:
    """Test ChromeSessionManager class."""
    
    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return HealingConfiguration(
            max_concurrent_sessions=2,
            chrome_session_timeout=30,
            element_wait_timeout=10,
            interaction_test=True
        )
    
    @pytest.fixture
    def manager(self, config):
        """Create ChromeSessionManager for testing."""
        return ChromeSessionManager(config)
    
    def test_manager_initialization(self, manager, config):
        """Test manager initialization."""
        assert manager.config == config
        assert manager.pool.max_sessions == config.max_concurrent_sessions
        assert manager.pool.session_timeout == config.chrome_session_timeout
        assert isinstance(manager.executor, ThreadPoolExecutor)
        assert manager._running is False
    
    def test_create_chrome_options(self, manager):
        """Test Chrome options creation."""
        options = manager._create_chrome_options()
        
        # Check that headless and other important options are set
        arguments = options.arguments
        assert "--headless" in arguments
        assert "--no-sandbox" in arguments
        assert "--disable-dev-shm-usage" in arguments
        assert "--disable-gpu" in arguments
    
    @pytest.mark.asyncio
    async def test_start_stop_manager(self, manager):
        """Test starting and stopping the manager."""
        assert manager._running is False
        
        await manager.start()
        assert manager._running is True
        assert manager._cleanup_task is not None
        
        await manager.stop()
        assert manager._running is False
    
    @patch('src.backend.services.chrome_session_manager.ChromeDriverManager')
    @patch('src.backend.services.chrome_session_manager.webdriver.Chrome')
    def test_create_chrome_session(self, mock_chrome, mock_driver_manager, manager):
        """Test creating a Chrome session."""
        mock_driver_manager.return_value.install.return_value = "/path/to/chromedriver"
        mock_driver = Mock()
        mock_chrome.return_value = mock_driver
        
        session = manager._create_chrome_session()
        
        assert isinstance(session, ChromeSession)
        assert session.driver == mock_driver
        assert session.is_active is True
        mock_chrome.assert_called_once()
    
    @pytest.mark.asyncio
    @patch.object(ChromeSessionManager, '_create_chrome_session')
    @patch.object(ChromeSessionManager, '_navigate_to_url')
    async def test_get_session_new(self, mock_navigate, mock_create, manager):
        """Test getting a new session when pool is empty."""
        mock_session = Mock(spec=ChromeSession)
        mock_session.session_id = "test-session"
        mock_create.return_value = mock_session
        mock_navigate.return_value = None
        
        # Mock pool methods
        manager.pool.get_available_session = Mock(return_value=None)
        manager.pool.add_session = Mock(return_value=True)
        
        session = await manager.get_session("http://example.com")
        
        assert session == mock_session
        mock_create.assert_called_once()
        mock_navigate.assert_called_once_with(mock_session, "http://example.com")
    
    @pytest.mark.asyncio
    @patch.object(ChromeSessionManager, '_navigate_to_url')
    async def test_get_session_existing(self, mock_navigate, manager):
        """Test getting an existing session."""
        mock_session = Mock(spec=ChromeSession)
        mock_session.current_url = "http://other.com"
        mock_session.update_last_used = Mock()
        
        manager.pool.get_available_session = Mock(return_value=mock_session)
        
        session = await manager.get_session("http://example.com")
        
        assert session == mock_session
        mock_session.update_last_used.assert_called_once()
        mock_navigate.assert_called_once_with(mock_session, "http://example.com")
    
    @pytest.mark.asyncio
    @patch.object(ChromeSessionManager, '_navigate_to_url')
    async def test_get_session_same_url(self, mock_navigate, manager):
        """Test getting session already on the same URL."""
        mock_session = Mock(spec=ChromeSession)
        mock_session.current_url = "http://example.com"
        mock_session.update_last_used = Mock()
        
        manager.pool.get_available_session = Mock(return_value=mock_session)
        
        session = await manager.get_session("http://example.com")
        
        assert session == mock_session
        mock_session.update_last_used.assert_called_once()
        mock_navigate.assert_not_called()  # No navigation needed
    
    @pytest.mark.asyncio
    async def test_navigate_to_url(self, manager):
        """Test navigating session to URL."""
        mock_driver = Mock()
        mock_session = Mock(spec=ChromeSession)
        mock_session.driver = mock_driver
        mock_session.session_id = "test-session"
        
        await manager._navigate_to_url(mock_session, "http://example.com")
        
        mock_driver.get.assert_called_once_with("http://example.com")
        assert mock_session.current_url == "http://example.com"
    
    @pytest.mark.asyncio
    async def test_validate_locator_success(self, manager):
        """Test successful locator validation."""
        mock_session = Mock(spec=ChromeSession)
        mock_element = Mock()
        mock_element.tag_name = "div"
        mock_element.text = "Test"
        mock_element.is_displayed.return_value = True
        mock_element.is_enabled.return_value = True
        mock_element.location = {"x": 100, "y": 200}
        mock_element.size = {"width": 50, "height": 30}
        mock_element.get_attribute.return_value = None
        
        with patch.object(manager, '_validate_locator_sync') as mock_validate:
            mock_validate.return_value = ValidationResult(
                locator="test-id",
                strategy=LocatorStrategy.ID,
                is_valid=True,
                element_found=True,
                is_interactable=True,
                matches_expected_type=True,
                confidence_score=0.9,
                element_properties={"tag_name": "div", "text": "Test"}
            )
            
            result = await manager.validate_locator(
                mock_session, 
                "test-id", 
                LocatorStrategy.ID
            )
            
            assert result.is_valid is True
            assert result.element_found is True
            assert result.confidence_score == 0.9
    
    def test_validate_locator_sync_success(self, manager):
        """Test synchronous locator validation success."""
        mock_session = Mock(spec=ChromeSession)
        mock_driver = Mock()
        mock_session.driver = mock_driver
        mock_session.update_last_used = Mock()
        
        mock_element = Mock()
        mock_element.tag_name = "div"
        mock_element.text = "Test"
        mock_element.is_displayed.return_value = True
        mock_element.is_enabled.return_value = True
        mock_element.location = {"x": 100, "y": 200}
        mock_element.size = {"width": 50, "height": 30}
        mock_element.get_attribute.return_value = None
        
        # Make sure driver.find_element returns the same mock element
        mock_driver.find_element.return_value = mock_element
        
        with patch('src.backend.services.chrome_session_manager.WebDriverWait') as mock_wait:
            mock_wait.return_value.until.return_value = mock_element
            
            result = manager._validate_locator_sync(
                mock_session,
                "test-id",
                LocatorStrategy.ID,
                By.ID,
                None
            )
            
            assert result.is_valid is True
            assert result.element_found is True
            assert result.is_interactable is True
            assert result.confidence_score == 1.0
            mock_session.update_last_used.assert_called_once()
    
    def test_validate_locator_sync_timeout(self, manager):
        """Test synchronous locator validation timeout."""
        mock_session = Mock(spec=ChromeSession)
        mock_driver = Mock()
        mock_session.driver = mock_driver
        mock_session.update_last_used = Mock()
        
        with patch('src.backend.services.chrome_session_manager.WebDriverWait') as mock_wait:
            mock_wait.return_value.until.side_effect = TimeoutException()
            
            result = manager._validate_locator_sync(
                mock_session,
                "test-id",
                LocatorStrategy.ID,
                By.ID,
                None
            )
            
            assert result.is_valid is False
            assert result.element_found is False
            assert result.error_message == "Element not found within timeout"
    
    def test_validate_locator_sync_no_such_element(self, manager):
        """Test synchronous locator validation with NoSuchElementException."""
        mock_session = Mock(spec=ChromeSession)
        mock_driver = Mock()
        mock_session.driver = mock_driver
        mock_session.update_last_used = Mock()
        
        with patch('src.backend.services.chrome_session_manager.WebDriverWait') as mock_wait:
            mock_wait.return_value.until.side_effect = NoSuchElementException()
            
            result = manager._validate_locator_sync(
                mock_session,
                "test-id",
                LocatorStrategy.ID,
                By.ID,
                None
            )
            
            assert result.is_valid is False
            assert result.element_found is False
            assert result.error_message == "Element not found"
    
    def test_validate_locator_sync_not_interactable(self, manager):
        """Test synchronous locator validation with ElementNotInteractableException."""
        mock_session = Mock(spec=ChromeSession)
        mock_driver = Mock()
        mock_session.driver = mock_driver
        mock_session.update_last_used = Mock()
        
        with patch('src.backend.services.chrome_session_manager.WebDriverWait') as mock_wait:
            mock_wait.return_value.until.side_effect = ElementNotInteractableException()
            
            result = manager._validate_locator_sync(
                mock_session,
                "test-id",
                LocatorStrategy.ID,
                By.ID,
                None
            )
            
            assert result.is_valid is False
            assert result.element_found is True
            assert result.is_interactable is False
            assert result.confidence_score == 0.2
    
    def test_calculate_confidence_score(self, manager):
        """Test confidence score calculation."""
        # Perfect score
        score = manager._calculate_confidence_score(
            element_found=True,
            is_interactable=True,
            matches_expected_type=True,
            strategy=LocatorStrategy.ID
        )
        assert score == 1.0
        
        # Partial score
        score = manager._calculate_confidence_score(
            element_found=True,
            is_interactable=False,
            matches_expected_type=True,
            strategy=LocatorStrategy.XPATH
        )
        assert score == 0.64  # 0.4 + 0.2 + 0.04
        
        # Zero score
        score = manager._calculate_confidence_score(
            element_found=False,
            is_interactable=False,
            matches_expected_type=False,
            strategy=LocatorStrategy.TAG_NAME
        )
        assert score == 0.02  # Only strategy bonus
    
    @pytest.mark.asyncio
    async def test_session_context_manager(self, manager):
        """Test session context manager."""
        mock_session = Mock(spec=ChromeSession)
        
        with patch.object(manager, 'get_session', return_value=mock_session) as mock_get:
            async with manager.session_context("http://example.com") as session:
                assert session == mock_session
            
            mock_get.assert_called_once_with("http://example.com")
    
    def test_get_session_stats(self, manager):
        """Test getting session statistics."""
        with patch.object(manager.pool, 'get_pool_stats') as mock_stats:
            mock_stats.return_value = {
                "total_sessions": 2,
                "active_sessions": 1,
                "max_sessions": 3
            }
            
            stats = manager.get_session_stats()
            
            assert "total_sessions" in stats
            assert "running" in stats
            assert stats["running"] == manager._running
    
    @pytest.mark.asyncio
    async def test_health_check_healthy(self, manager):
        """Test health check when system is healthy."""
        mock_result = ValidationResult(
            locator="test",
            strategy=LocatorStrategy.ID,
            is_valid=True,
            element_found=True,
            is_interactable=True,
            matches_expected_type=True,
            confidence_score=1.0
        )
        
        with patch.object(manager, 'session_context') as mock_context:
            mock_session = Mock()
            mock_context.return_value.__aenter__.return_value = mock_session
            mock_context.return_value.__aexit__.return_value = None
            
            with patch.object(manager, 'validate_locator', return_value=mock_result):
                health = await manager.health_check()
                
                assert health["status"] == "healthy"
                assert health["test_validation_success"] is True
    
    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self, manager):
        """Test health check when system is unhealthy."""
        with patch.object(manager, 'session_context') as mock_context:
            mock_context.side_effect = Exception("Test error")
            
            health = await manager.health_check()
            
            assert health["status"] == "unhealthy"
            assert "error" in health
            assert health["error"] == "Test error"


if __name__ == "__main__":
    pytest.main([__file__])