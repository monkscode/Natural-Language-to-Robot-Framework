#!/usr/bin/env python3
"""
Demo script showing the element fingerprinting system in action.

This script demonstrates how the fingerprinting system can:
1. Create fingerprints from element information
2. Store and retrieve fingerprints
3. Match fingerprints against modified DOM structures
4. Handle various scenarios like UI changes and similar elements
"""

import sys
import os
import tempfile
from pathlib import Path

# Add the backend to the path
sys.path.append(str(Path(__file__).parent.parent.parent / "src" / "backend"))

try:
    from services.fingerprinting_service import FingerprintingService
except ImportError:
    print("⚠️  Could not import backend services. This demo requires the full application environment.")
    print("Run this demo from the project root with the virtual environment activated.")
    sys.exit(1)


def demo_basic_fingerprinting():
    """Demonstrate basic fingerprinting functionality."""
    print("=== Basic Fingerprinting Demo ===")
    
    # Create a temporary directory for storage
    temp_dir = tempfile.mkdtemp()
    service = FingerprintingService(storage_path=temp_dir)
    
    # Original DOM structure
    original_dom = """
    <html>
        <body>
            <div class="container">
                <form id="login-form">
                    <input id="username" name="username" type="text" placeholder="Username" />
                    <input id="password" name="password" type="password" placeholder="Password" />
                    <button id="login-btn" class="btn btn-primary" type="submit">Login</button>
                </form>
            </div>
        </body>
    </html>
    """
    
    # Element information for the login button
    element_info = {
        "tag_name": "button",
        "attributes": {
            "id": "login-btn",
            "class": "btn btn-primary",
            "type": "submit"
        },
        "text_content": "Login",
        "dom_context": original_dom,
        "locator": "id=login-btn"
    }
    
    # Step 1: Create fingerprint
    print("1. Creating fingerprint...")
    fingerprint = service.create_fingerprint(element_info)
    print(f"   Tag: {fingerprint.tag_name}")
    print(f"   Attributes: {fingerprint.attributes}")
    print(f"   Text: '{fingerprint.text_content}'")
    print(f"   Parent context: {fingerprint.parent_context}")
    print(f"   DOM path: {fingerprint.dom_path}")
    
    # Step 2: Store fingerprint
    print("\n2. Storing fingerprint...")
    test_id = "login_test"
    step_id = "click_login"
    service.store_fingerprint(test_id, step_id, fingerprint)
    print(f"   Stored as: {test_id}_{step_id}")
    
    # Step 3: Retrieve fingerprint
    print("\n3. Retrieving fingerprint...")
    retrieved = service.retrieve_fingerprint(test_id, step_id)
    print(f"   Retrieved successfully: {retrieved is not None}")
    print(f"   Matches original: {retrieved.tag_name == fingerprint.tag_name}")
    
    print(f"\nStorage location: {temp_dir}")
    return service, fingerprint


def demo_dom_matching():
    """Demonstrate DOM matching with UI changes."""
    print("\n=== DOM Matching Demo ===")
    
    service, original_fingerprint = demo_basic_fingerprinting()
    
    # Modified DOM (simulating UI changes)
    modified_dom = """
    <html>
        <body>
            <div class="main-container">
                <form id="signin-form" class="auth-form">
                    <h2>Sign In</h2>
                    <input id="user-email" name="email" type="email" placeholder="Email" />
                    <input id="user-pass" name="password" type="password" placeholder="Password" />
                    <button id="signin-btn" class="button primary" type="submit">Sign In</button>
                </form>
            </div>
        </body>
    </html>
    """
    
    print("\n4. Matching against modified DOM...")
    match_result = service.match_fingerprint(modified_dom, original_fingerprint)
    
    print(f"   Match found: {match_result.confidence_score > 0}")
    print(f"   Confidence score: {match_result.confidence_score:.3f}")
    print(f"   Matching elements: {len(match_result.matching_elements)}")
    print(f"   Best match locator: {match_result.best_match_locator}")
    
    if match_result.match_details:
        print("   Score breakdown:")
        breakdown = match_result.match_details.get('score_breakdown', {})
        for metric, score in breakdown.items():
            print(f"     {metric}: {score:.3f}")


def demo_similar_elements():
    """Demonstrate handling of similar elements."""
    print("\n=== Similar Elements Demo ===")
    
    temp_dir = tempfile.mkdtemp()
    service = FingerprintingService(storage_path=temp_dir)
    
    # DOM with multiple similar buttons
    multi_button_dom = """
    <html>
        <body>
            <div class="toolbar">
                <button id="save-btn" class="btn btn-primary">Save</button>
                <button id="cancel-btn" class="btn btn-secondary">Cancel</button>
                <button id="delete-btn" class="btn btn-danger">Delete</button>
            </div>
            <div class="content">
                <button id="submit-btn" class="btn btn-primary">Submit</button>
                <button id="reset-btn" class="btn btn-secondary">Reset</button>
            </div>
        </body>
    </html>
    """
    
    # Create fingerprint for the submit button
    submit_element_info = {
        "tag_name": "button",
        "attributes": {
            "id": "submit-btn",
            "class": "btn btn-primary"
        },
        "text_content": "Submit",
        "dom_context": multi_button_dom,
        "locator": "id=submit-btn"
    }
    
    print("5. Creating fingerprint for submit button...")
    fingerprint = service.create_fingerprint(submit_element_info)
    
    print("6. Matching against DOM with multiple similar buttons...")
    match_result = service.match_fingerprint(multi_button_dom, fingerprint)
    
    print(f"   Found {len(match_result.matching_elements)} button candidates")
    print(f"   Best match: {match_result.best_match_locator}")
    print(f"   Confidence: {match_result.confidence_score:.3f}")
    
    # Show all matching elements
    print("   All candidates:")
    for i, locator in enumerate(match_result.matching_elements, 1):
        print(f"     {i}. {locator}")


def demo_error_handling():
    """Demonstrate error handling capabilities."""
    print("\n=== Error Handling Demo ===")
    
    temp_dir = tempfile.mkdtemp()
    service = FingerprintingService(storage_path=temp_dir)
    
    # Test with malformed DOM
    print("7. Testing with malformed DOM...")
    malformed_element_info = {
        "tag_name": "button",
        "attributes": {"id": "test-btn"},
        "text_content": "Test",
        "dom_context": "<<malformed>>dom<<content>>",
        "locator": "id=test-btn"
    }
    
    try:
        fingerprint = service.create_fingerprint(malformed_element_info)
        print(f"   Fingerprint created successfully: {fingerprint.tag_name}")
        print("   System handled malformed DOM gracefully")
    except Exception as e:
        print(f"   Error: {e}")
    
    # Test matching against invalid DOM
    print("\n8. Testing matching against invalid DOM...")
    if 'fingerprint' in locals():
        match_result = service.match_fingerprint("invalid<dom>", fingerprint)
        print(f"   Match result: {match_result.matched}")
        print(f"   Confidence: {match_result.confidence_score}")
        print("   System handled invalid DOM gracefully")


def demo_cleanup():
    """Demonstrate fingerprint cleanup functionality."""
    print("\n=== Cleanup Demo ===")
    
    temp_dir = tempfile.mkdtemp()
    service = FingerprintingService(storage_path=temp_dir)
    
    # Create multiple fingerprints
    print("9. Creating multiple fingerprints...")
    for i in range(5):
        element_info = {
            "tag_name": "button",
            "attributes": {"id": f"btn-{i}"},
            "text_content": f"Button {i}",
            "dom_context": f'<button id="btn-{i}">Button {i}</button>',
            "locator": f"id=btn-{i}"
        }
        
        fingerprint = service.create_fingerprint(element_info)
        service.store_fingerprint(f"test_{i}", "step_1", fingerprint)
    
    # Count files
    storage_files = list(Path(temp_dir).glob("*.json"))
    print(f"   Created {len(storage_files)} fingerprint files")
    
    # Cleanup with 0 day retention
    print("\n10. Cleaning up old fingerprints...")
    cleaned_count = service.cleanup_old_fingerprints(retention_days=0)
    
    remaining_files = list(Path(temp_dir).glob("*.json"))
    print(f"   Cleaned up {cleaned_count} files")
    print(f"   Remaining files: {len(remaining_files)}")


def main():
    """Run all demos."""
    print("Element Fingerprinting System Demo")
    print("=" * 50)
    
    try:
        demo_basic_fingerprinting()
        demo_dom_matching()
        demo_similar_elements()
        demo_error_handling()
        demo_cleanup()
        
        print("\n" + "=" * 50)
        print("Demo completed successfully!")
        print("\nThe fingerprinting system demonstrates:")
        print("✓ Element fingerprint creation and storage")
        print("✓ DOM matching with confidence scoring")
        print("✓ Handling of similar elements")
        print("✓ Robust error handling")
        print("✓ Automatic cleanup capabilities")
        
    except Exception as e:
        print(f"\nDemo failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()