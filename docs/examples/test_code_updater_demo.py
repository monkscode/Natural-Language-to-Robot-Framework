#!/usr/bin/env python3
"""
Demo script showing how to use the RobotTestCodeUpdater service.

This script demonstrates the key functionality of the test code updater:
- Creating backups
- Updating single and multiple locators
- Validating syntax
- Restoring from backups
"""

import sys
import tempfile
from pathlib import Path

# Add the backend to the path
sys.path.append(str(Path(__file__).parent.parent.parent / "src" / "backend"))

try:
    from services.test_code_updater import (
        RobotTestCodeUpdater, 
        LocatorReplacement
    )
    from core.models import LocatorStrategy
except ImportError:
    print("⚠️  Could not import backend services. This demo requires the full application environment.")
    print("Run this demo from the project root with the virtual environment activated.")
    sys.exit(1)


def create_sample_robot_file(file_path: str) -> None:
    """Create a sample Robot Framework test file for demonstration."""
    content = """*** Settings ***
Library    SeleniumLibrary

*** Variables ***
${LOGIN_URL}    https://example.com/login

*** Test Cases ***
User Login Test
    [Documentation]    Test user login functionality
    Open Browser    ${LOGIN_URL}    chrome
    Input Text    id=username    testuser
    Input Password    id=password    testpass
    Click Element    id=submit
    Wait Until Page Contains    Welcome
    Element Should Be Visible    id=success_message
    Close Browser

Search Functionality Test
    [Documentation]    Test search feature
    Open Browser    https://example.com/search    chrome
    Input Text    name=query    robot framework
    Click Button    xpath=//button[@type='submit']
    Wait Until Page Contains    Results
    Element Should Contain    css=.result-count    results found
    Close Browser
"""
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"Created sample Robot Framework file: {file_path}")


def demonstrate_single_locator_update():
    """Demonstrate updating a single locator."""
    print("\n=== Single Locator Update Demo ===")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create sample file and updater
        robot_file = Path(temp_dir) / "test_single.robot"
        create_sample_robot_file(str(robot_file))
        
        updater = RobotTestCodeUpdater(backup_dir=str(Path(temp_dir) / "backups"))
        
        # Update a single locator
        print("Updating 'id=username' to 'name=user'...")
        result = updater.update_locator(
            str(robot_file),
            "id=username",
            "name=user",
            create_backup=True
        )
        
        if result.success:
            print(f"✅ Update successful!")
            print(f"   Backup created: {result.backup_path}")
            print(f"   Locators updated: {result.updated_locators}")
            print(f"   Syntax valid: {result.syntax_valid}")
            
            # Show the updated content
            with open(robot_file, 'r') as f:
                content = f.read()
            print("\nUpdated file content (excerpt):")
            for i, line in enumerate(content.split('\n'), 1):
                if 'name=user' in line:
                    print(f"   Line {i}: {line.strip()}")
        else:
            print(f"❌ Update failed: {result.error_message}")


def demonstrate_multiple_locator_update():
    """Demonstrate updating multiple locators at once."""
    print("\n=== Multiple Locator Update Demo ===")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create sample file and updater
        robot_file = Path(temp_dir) / "test_multiple.robot"
        create_sample_robot_file(str(robot_file))
        
        updater = RobotTestCodeUpdater(backup_dir=str(Path(temp_dir) / "backups"))
        
        # Define multiple locator replacements
        replacements = [
            LocatorReplacement("id=username", "name=user", LocatorStrategy.NAME),
            LocatorReplacement("id=password", "name=pass", LocatorStrategy.NAME),
            LocatorReplacement("id=submit", "css=.login-btn", LocatorStrategy.CSS),
            LocatorReplacement("id=success_message", "css=.alert-success", LocatorStrategy.CSS),
            LocatorReplacement("name=query", "id=search-input", LocatorStrategy.ID),
        ]
        
        print(f"Updating {len(replacements)} locators...")
        result = updater.update_multiple_locators(
            str(robot_file),
            replacements,
            create_backup=True
        )
        
        if result.success:
            print(f"✅ Multiple updates successful!")
            print(f"   Backup created: {result.backup_path}")
            print(f"   Total replacements made: {len(result.updated_locators)}")
            print(f"   Syntax valid: {result.syntax_valid}")
            
            print("\nReplacements made:")
            for old, new in result.updated_locators:
                print(f"   '{old}' → '{new}'")
        else:
            print(f"❌ Update failed: {result.error_message}")


def demonstrate_backup_and_restore():
    """Demonstrate backup creation and restoration."""
    print("\n=== Backup and Restore Demo ===")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create sample file and updater
        robot_file = Path(temp_dir) / "test_backup.robot"
        create_sample_robot_file(str(robot_file))
        
        updater = RobotTestCodeUpdater(backup_dir=str(Path(temp_dir) / "backups"))
        
        # Create a backup manually
        print("Creating backup...")
        backup_path = updater.backup_test_file(str(robot_file))
        print(f"✅ Backup created: {backup_path}")
        
        # Modify the original file
        print("Modifying original file...")
        with open(robot_file, 'w') as f:
            f.write("# This file has been corrupted!\n")
        
        # Restore from backup
        print("Restoring from backup...")
        success = updater.restore_from_backup(str(robot_file), backup_path)
        
        if success:
            print("✅ Restoration successful!")
            
            # Verify restoration
            with open(robot_file, 'r') as f:
                content = f.read()
            if "*** Settings ***" in content:
                print("   Original content restored correctly")
            else:
                print("   ❌ Restoration may have failed")
        else:
            print("❌ Restoration failed")
        
        # Show backup information
        backup_info = updater.get_backup_info(str(robot_file))
        print(f"\nBackup information:")
        for info in backup_info:
            print(f"   Path: {info['path']}")
            print(f"   Created: {info['created']}")
            print(f"   Size: {info['size']} bytes")


def demonstrate_error_handling():
    """Demonstrate error handling scenarios."""
    print("\n=== Error Handling Demo ===")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        updater = RobotTestCodeUpdater(backup_dir=str(Path(temp_dir) / "backups"))
        
        # Try to update a non-existent file
        print("Attempting to update non-existent file...")
        result = updater.update_locator(
            "/nonexistent/file.robot",
            "id=test",
            "name=test"
        )
        
        if not result.success:
            print(f"✅ Error handled correctly: {result.error_message}")
        
        # Try to update a locator that doesn't exist
        robot_file = Path(temp_dir) / "test_error.robot"
        create_sample_robot_file(str(robot_file))
        
        print("Attempting to update non-existent locator...")
        result = updater.update_locator(
            str(robot_file),
            "id=nonexistent",
            "name=new"
        )
        
        if not result.success:
            print(f"✅ Error handled correctly: {result.error_message}")


def main():
    """Run all demonstrations."""
    print("🤖 Robot Framework Test Code Updater Demo")
    print("=" * 50)
    
    try:
        demonstrate_single_locator_update()
        demonstrate_multiple_locator_update()
        demonstrate_backup_and_restore()
        demonstrate_error_handling()
        
        print("\n" + "=" * 50)
        print("✅ All demonstrations completed successfully!")
        print("\nKey features demonstrated:")
        print("  • Safe locator replacement with backup creation")
        print("  • Multiple locator updates in a single operation")
        print("  • Atomic file operations to prevent corruption")
        print("  • Robot Framework syntax validation")
        print("  • Backup and restore functionality")
        print("  • Comprehensive error handling")
        
    except Exception as e:
        print(f"\n❌ Demo failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()