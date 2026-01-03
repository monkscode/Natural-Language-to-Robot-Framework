from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print("Navigating to app...")
        page.goto("http://localhost:8000")

        # --- Test Bug #1: Text Appears Large ---
        print("\n--- Testing Bug #1 ---")
        robot_code = page.locator("#robot-code")

        # Focus and type
        robot_code.focus()
        page.keyboard.type("a")

        # Check font size of the text
        # We need to check if the text inherits the large font size from the empty state icon
        # or if the empty state is still there.

        # Get computed style
        font_size = page.evaluate("window.getComputedStyle(document.getElementById('robot-code')).fontSize")
        print(f"Computed font size of #robot-code: {font_size}")

        # Check if placeholder is still present
        placeholder_count = page.locator("#code-placeholder").count()
        print(f"Placeholder count: {placeholder_count}")

        if placeholder_count > 0:
            print("FAIL: Placeholder is still present after typing.")
        else:
            print("PASS: Placeholder removed.")

        # --- Test Bug #2: Paste Replaces All ---
        print("\n--- Testing Bug #2 ---")

        # Reset content
        page.reload()
        robot_code = page.locator("#robot-code")

        # Set initial content
        robot_code.focus()
        page.keyboard.type("existing code\n")

        initial_content = robot_code.inner_text()
        print(f"Initial content: {repr(initial_content)}")

        # Paste content
        # We need to simulate a paste event.
        # Playwright's page.evaluate can trigger a paste event or we can use clipboard permissions.
        # Simulating paste via data transfer might be easier.

        paste_content = "pasted line"

        # Grant permissions for clipboard (might be needed for some approaches, but we can try dispatching event)
        context.grant_permissions(["clipboard-read", "clipboard-write"])

        # Focus and paste
        robot_code.focus()

        # Using evaluate to dispatch paste event
        page.evaluate("""
            const event = new ClipboardEvent('paste', {
                clipboardData: new DataTransfer()
            });
            event.clipboardData.setData('text/plain', 'pasted line');
            document.getElementById('robot-code').dispatchEvent(event);
        """)

        # Wait a bit for processing
        page.wait_for_timeout(500)

        final_content = robot_code.inner_text()
        print(f"Final content: {repr(final_content)}")

        if "existing code" in final_content and "pasted line" in final_content:
             print("PASS: Content was appended/inserted.")
        else:
             print("FAIL: Content was replaced or paste failed.")

        browser.close()

if __name__ == "__main__":
    run()
