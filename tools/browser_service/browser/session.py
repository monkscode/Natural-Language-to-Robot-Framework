"""
Browser session management for browser service.

This module provides a manager class for handling browser session lifecycle,
including creation, tracking, and cleanup of browser sessions.

The BrowserSessionManager encapsulates browser session configuration and
provides a clean interface for session operations.
"""

from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class BrowserSessionManager:
    """
    Manages browser session lifecycle.

    This class handles the creation, tracking, and cleanup of browser sessions.
    It provides a centralized way to manage browser configuration and state.

    Attributes:
        headless: Whether to run browser in headless mode
        viewport: Browser viewport dimensions (width, height)
        session: Current browser session instance

    Example:
        >>> manager = BrowserSessionManager(headless=False)
        >>> session = await manager.create_session()
        >>> # Use session...
        >>> await manager.close_session()
    """

    def __init__(self, headless: bool = False, viewport: Dict[str, int] = None):
        """
        Initialize the browser session manager.

        Args:
            headless: If True, run browser in headless mode (no UI)
            viewport: Dictionary with 'width' and 'height' keys for viewport size.
                     Defaults to {'width': 1920, 'height': 1080}
        """
        self.headless = headless
        self.viewport = viewport or {'width': 1920, 'height': 1080}
        self.session: Optional[Any] = None
        logger.debug(f"BrowserSessionManager initialized (headless={headless}, viewport={self.viewport})")

    async def create_session(self) -> Any:
        """
        Create and initialize a new browser session.

        This method creates a new browser session with the configured settings.
        If a session already exists, it will be closed before creating a new one.

        Returns:
            Browser session instance

        Raises:
            Exception: If session creation fails

        Example:
            >>> manager = BrowserSessionManager()
            >>> session = await manager.create_session()
        """
        # Close existing session if any
        if self.session:
            logger.warning("Closing existing session before creating new one")
            await self.close_session()

        try:
            # Import here to avoid circular dependencies
            from browser_use.browser.session import BrowserSession

            logger.info(f"Creating browser session (headless={self.headless})")
            self.session = BrowserSession(
                headless=self.headless,
                viewport=self.viewport
            )
            logger.info("✅ Browser session created successfully")
            return self.session

        except Exception as e:
            logger.error(f"Failed to create browser session: {e}")
            raise

    async def close_session(self) -> None:
        """
        Close the current browser session.

        This method gracefully closes the browser session and cleans up resources.
        After calling this method, the session attribute will be set to None.

        Example:
            >>> await manager.close_session()
        """
        if not self.session:
            logger.debug("No active session to close")
            return

        try:
            logger.info("Closing browser session...")
            if hasattr(self.session, 'close'):
                await self.session.close()
            elif hasattr(self.session, 'browser') and self.session.browser:
                await self.session.browser.close()
            logger.info("✅ Browser session closed")
        except Exception as e:
            logger.error(f"Error closing browser session: {e}")
        finally:
            self.session = None

    def get_session(self) -> Optional[Any]:
        """
        Get the current browser session.

        Returns:
            Current browser session instance, or None if no session exists

        Example:
            >>> session = manager.get_session()
            >>> if session:
            ...     # Use session
        """
        return self.session

    def has_session(self) -> bool:
        """
        Check if an active session exists.

        Returns:
            True if an active session exists, False otherwise

        Example:
            >>> if manager.has_session():
            ...     print("Session is active")
        """
        return self.session is not None
