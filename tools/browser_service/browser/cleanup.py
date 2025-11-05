"""
Browser cleanup utilities for browser service.

This module provides utilities for cleaning up browser resources and terminating
browser processes. It implements a multi-strategy approach to ensure reliable
cleanup across different browser automation scenarios.

Cleanup Strategy:
    1. Track browser process ID before closing
    2. Close connected browser gracefully
    3. Stop Playwright instance
    4. Close browser session
    5. Force kill tracked Chrome process if still running (Windows only)

The cleanup is designed to only terminate the Chrome process that was started
by the browser service, not the user's personal Chrome instances.
"""

import sys
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_browser_process_id(session) -> Optional[int]:
    """
    Extract the Chrome process ID from the browser session.

    This function attempts multiple strategies to find the browser process ID:
    1. Extract from CDP URL and use netstat (PRIMARY - works with browser-use)
    2. Check session.browser._browser_process.pid (Playwright direct)
    3. Check session.browser.process.pid (Playwright direct)
    4. Check session.browser._impl._browser_process.pid (Playwright internal)
    5. Check browser contexts for process info
    6. Check session.context.browser.process.pid

    Args:
        session: Browser session object (typically from browser-use)

    Returns:
        Process ID (PID) of the Chrome browser process, or None if not found
    """
    try:
        # PRIMARY METHOD: Try to get from CDP endpoint (works with browser-use)
        # browser-use doesn't expose the Playwright browser object, only CDP URL
        if hasattr(session, 'cdp_url') and session.cdp_url:
            # Extract port from CDP URL and find Chrome process using that port
            import re
            match = re.search(r':(\d+)/', session.cdp_url)
            if match:
                port = match.group(1)
                logger.debug(f"   Found CDP port: {port}, searching for Chrome PID...")

                # On Windows, use netstat to find PID listening on this port
                if sys.platform.startswith('win'):
                    try:
                        import subprocess
                        result = subprocess.run(
                            ['netstat', '-ano'],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )

                        for line in result.stdout.split('\n'):
                            if f':{port}' in line and 'LISTENING' in line:
                                parts = line.split()
                                if parts:
                                    pid = int(parts[-1])
                                    logger.info(f"   📍 Found browser PID via CDP port {port}: {pid}")
                                    return pid
                    except Exception as e:
                        logger.debug(f"   Error using netstat: {e}")
                else:
                    # On Linux/Mac, use lsof
                    try:
                        import subprocess
                        result = subprocess.run(
                            ['lsof', '-ti', f':{port}'],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        if result.stdout.strip():
                            pid = int(result.stdout.strip().split('\n')[0])
                            logger.info(f"   📍 Found browser PID via CDP port {port}: {pid}")
                            return pid
                    except Exception as e:
                        logger.debug(f"   Error using lsof: {e}")

        # FALLBACK METHODS: Try to get PID from session.browser (for direct Playwright usage)
        if hasattr(session, 'browser') and session.browser:
            browser = session.browser

            # Method 1: Check for _browser_process attribute (Playwright)
            if hasattr(browser, '_browser_process') and browser._browser_process:
                pid = browser._browser_process.pid
                if pid:
                    logger.info(f"   📍 Found browser PID via _browser_process: {pid}")
                    return pid

            # Method 2: Check for process attribute (Playwright)
            if hasattr(browser, 'process') and browser.process:
                pid = browser.process.pid
                if pid:
                    logger.info(f"   📍 Found browser PID via process: {pid}")
                    return pid

            # Method 3: Check _impl.process (Playwright internal)
            if hasattr(browser, '_impl') and hasattr(browser._impl, '_browser_process'):
                pid = browser._impl._browser_process.pid
                if pid:
                    logger.info(f"   📍 Found browser PID via _impl._browser_process: {pid}")
                    return pid

            # Method 4: Check contexts for process info
            if hasattr(browser, 'contexts') and browser.contexts:
                for context in browser.contexts:
                    if hasattr(context, '_browser') and hasattr(context._browser, 'process'):
                        pid = context._browser.process.pid
                        if pid:
                            logger.info(f"   📍 Found browser PID via context: {pid}")
                            return pid

        # Try session.context
        if hasattr(session, 'context') and session.context:
            if hasattr(session.context, 'browser') and hasattr(session.context.browser, 'process'):
                pid = session.context.browser.process.pid
                if pid:
                    logger.info(f"   📍 Found browser PID via session.context: {pid}")
                    return pid

        logger.warning("   ⚠️ Could not determine browser PID - graceful close only")
        return None

    except Exception as e:
        logger.warning(f"   ⚠️ Error getting browser PID: {e}")
        return None


async def cleanup_browser_resources(
    session=None,
    connected_browser=None,
    playwright_instance=None
) -> None:
    """
    Simple and robust browser cleanup.

    This function performs a comprehensive cleanup of browser resources:
    1. Tracks the browser process ID before closing
    2. Closes connected browser gracefully
    3. Stops Playwright instance
    4. Closes browser session
    5. Force kills the tracked Chrome process if still running (Windows only)

    The cleanup only kills the Chrome process that was started by this service,
    not the user's personal Chrome instances.

    Args:
        session: Browser session object to clean up
        connected_browser: Connected browser instance to close
        playwright_instance: Playwright instance to stop

    Returns:
        None
    """
    logger.info("🧹 Starting browser cleanup...")

    # Get the PID of OUR Chrome process before closing
    browser_pid = None
    if session:
        browser_pid = get_browser_process_id(session)
        if browser_pid:
            logger.info(f"   📍 Tracked browser PID: {browser_pid}")
        else:
            logger.debug("   ⚠️ Could not track browser PID - will use graceful close only")

    # Step 1: Close connected browser
    if connected_browser:
        try:
            await connected_browser.close()
            logger.info("   ✅ Connected browser closed")
        except Exception:
            pass

    # Step 2: Stop playwright instance
    if playwright_instance:
        try:
            await playwright_instance.stop()
            logger.info("   ✅ Playwright stopped")
        except Exception:
            pass

    # Step 3: Close session gracefully
    if session:
        try:
            if hasattr(session, 'close'):
                await session.close()
            elif hasattr(session, 'browser') and session.browser:
                await session.browser.close()
            logger.info("   ✅ Session closed")
        except Exception:
            pass

    # Step 4: Force kill ONLY our Chrome process if it's still running
    if browser_pid:
        if sys.platform.startswith('win'):
            try:
                import subprocess

                # Check if our specific Chrome process is still running
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {browser_pid}'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if 'chrome.exe' in result.stdout.lower():
                    logger.warning(f"   ⚠️ Chrome process {browser_pid} still running after graceful close")
                    logger.info(f"   🔨 Force killing Chrome PID {browser_pid} and its children...")

                    # Kill only our specific Chrome process and its children
                    # /T flag kills the process tree (parent + all children)
                    subprocess.run(
                        ['taskkill', '/F', '/PID', str(browser_pid), '/T'],
                        capture_output=True,
                        timeout=5
                    )

                    # Wait a moment for processes to terminate
                    import time
                    time.sleep(0.5)

                    # Verify it's gone
                    result = subprocess.run(
                        ['tasklist', '/FI', f'PID eq {browser_pid}'],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )

                    if 'chrome.exe' not in result.stdout.lower():
                        logger.info(f"   ✅ Chrome process {browser_pid} and children terminated")
                    else:
                        logger.warning(f"   ⚠️ Chrome process {browser_pid} may still be running")
                else:
                    logger.info(f"   ✅ Chrome process {browser_pid} already terminated")

            except Exception as e:
                logger.warning(f"   ⚠️ Error during force kill: {e}")
        else:
            # Linux/Mac cleanup
            try:
                import subprocess
                import signal
                
                # Try to kill the process group
                logger.info(f"   🔨 Terminating Chrome PID {browser_pid}...")
                subprocess.run(['kill', '-TERM', str(browser_pid)], timeout=5)
                
                # Wait a moment
                import time
                time.sleep(0.5)
                
                # Check if still running, force kill if needed
                try:
                    subprocess.run(['kill', '-0', str(browser_pid)], timeout=5, check=True)
                    # Still running, force kill
                    logger.warning(f"   ⚠️ Chrome process {browser_pid} still running, force killing...")
                    subprocess.run(['kill', '-KILL', str(browser_pid)], timeout=5)
                    logger.info(f"   ✅ Chrome process {browser_pid} force killed")
                except subprocess.CalledProcessError:
                    # Process doesn't exist anymore
                    logger.info(f"   ✅ Chrome process {browser_pid} terminated")
                    
            except Exception as e:
                logger.warning(f"   ⚠️ Error during force kill: {e}")
    else:
        logger.warning("   ⚠️ No browser PID tracked - cannot force kill (graceful close only)")

    logger.info("🧹 Cleanup complete")
