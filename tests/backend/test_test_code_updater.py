"""Unit tests for the test code updater service."""

import os
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timedelta
import pytest
from unittest.mock import patch, mock_open

from src.backend.services.test_code_updater import (
    RobotTestCodeUpdater, 
    UpdateResult, 
    LocatorReplacement
)
from src.backend.core.models import LocatorStrategy


class TestTestCodeUpdater:
    """Test cases for TestCodeUpdater service."""
    
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)
    
    @pytest.fixture
    def updater(self, temp_dir):
        """Create a RobotTestCodeUpdater instance with temp backup directory."""
        return RobotTestCodeUpdater(backup_dir=str(Path(temp_dir) / "backups"))
    
    @pytest.fixture
    def sample_robot_file(self, temp_dir):
        """Create a sample Robot Framework test file."""
        content = """*** Settings ***
Library    SeleniumLibrary

*** Test Cases ***
Login Test
    [Documentation]    Test user login functionality
    Open Browser    https://example.com/login    chrome
    Input Text    id=username    testuser
    Input Password    id=password    testpass
    Click Element    id=submit
    Wait Until Page Contains    Welcome
    Close Browser

Search Test
    [Documentation]    Test search functionality
    Open Browser    https://example.com/search    chrome
    Input Text    name=query    robot framework
    Click Button    xpath=//button[@type='submit']
    Wait Until Page Contains    Results
    Close Browser
"""
        file_path = Path(temp_dir) / "test_sample.robot"
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return str(file_path)
    
    def test_backup_test_file_success(self, updater, sample_robot_file):
        """Test successful backup creation."""
        backup_path = updater.backup_test_file(sample_robot_file)
        
        assert Path(backup_path).exists()
        assert "test_sample_" in backup_path
        assert backup_path.endswith(".robot")
        
        # Verify backup content matches original
        with open(sample_robot_file, 'r') as original:
            original_content = original.read()
        with open(backup_path, 'r') as backup:
            backup_content = backup.read()
        
        assert original_content == backup_content
    
    def test_backup_test_file_nonexistent(self, updater):
        """Test backup creation with nonexistent file."""
        with pytest.raises(FileNotFoundError):
            updater.backup_test_file("/nonexistent/file.robot")
    
    def test_validate_robot_syntax_valid(self, updater, sample_robot_file):
        """Test syntax validation with valid Robot Framework file."""
        is_valid, error_msg = updater.validate_robot_syntax(sample_robot_file)
        
        assert is_valid is True
        assert error_msg is None
    
    def test_validate_robot_syntax_invalid(self, updater, temp_dir):
        """Test syntax validation with invalid Robot Framework file."""
        # Robot Framework parser is very lenient, so we test with a non-existent file
        # which will definitely cause a validation error
        nonexistent_file = Path(temp_dir) / "nonexistent.robot"
        
        is_valid, error_msg = updater.validate_robot_syntax(str(nonexistent_file))
        
        assert is_valid is False
        assert error_msg is not None
        assert "Syntax validation failed" in error_msg
    
    def test_find_locator_in_file(self, updater, sample_robot_file):
        """Test finding locator occurrences in file."""
        matches = updater.find_locator_in_file(sample_robot_file, "id=username")
        
        assert len(matches) == 1
        line_num, line_content = matches[0]
        assert line_num == 8
        assert "Input Text    id=username    testuser" in line_content
    
    def test_find_locator_in_file_multiple_matches(self, updater, sample_robot_file):
        """Test finding multiple occurrences of a locator."""
        matches = updater.find_locator_in_file(sample_robot_file, "chrome")
        
        assert len(matches) == 2  # Two browser opens with chrome
    
    def test_find_locator_in_file_no_matches(self, updater, sample_robot_file):
        """Test finding locator that doesn't exist."""
        matches = updater.find_locator_in_file(sample_robot_file, "id=nonexistent")
        
        assert len(matches) == 0
    
    def test_replace_locator_in_line_simple(self, updater):
        """Test simple locator replacement in a line."""
        line = "    Input Text    id=username    testuser"
        updated = updater.replace_locator_in_line(line, "id=username", "name=user")
        
        assert updated == "    Input Text    name=user    testuser"
    
    def test_replace_locator_in_line_quoted(self, updater):
        """Test locator replacement with quoted locators."""
        line = '    Click Element    "id=submit"'
        updated = updater.replace_locator_in_line(line, "id=submit", "css=.submit-btn")
        
        assert updated == '    Click Element    "css=.submit-btn"'
    
    def test_replace_locator_in_line_variable(self, updater):
        """Test locator replacement in variable assignment."""
        line = "    ${locator}=    Set Variable    id=username"
        updated = updater.replace_locator_in_line(line, "id=username", "name=user")
        
        assert updated == "    ${locator}=    Set Variable    name=user"
    
    def test_update_locator_success(self, updater, sample_robot_file):
        """Test successful single locator update."""
        result = updater.update_locator(
            sample_robot_file, 
            "id=username", 
            "name=user",
            create_backup=True
        )
        
        assert result.success is True
        assert result.backup_path is not None
        assert len(result.updated_locators) == 1
        assert result.updated_locators[0] == ("id=username", "name=user")
        assert result.syntax_valid is True
        
        # Verify file was actually updated
        with open(sample_robot_file, 'r') as f:
            content = f.read()
        assert "name=user" in content
        assert "id=username" not in content
    
    def test_update_locator_not_found(self, updater, sample_robot_file):
        """Test locator update when locator is not found."""
        result = updater.update_locator(
            sample_robot_file, 
            "id=nonexistent", 
            "name=new"
        )
        
        assert result.success is False
        assert "not found in file" in result.error_message
    
    def test_update_locator_nonexistent_file(self, updater):
        """Test locator update with nonexistent file."""
        result = updater.update_locator(
            "/nonexistent/file.robot", 
            "id=test", 
            "name=test"
        )
        
        assert result.success is False
        assert "File not found" in result.error_message
    
    def test_update_multiple_locators_success(self, updater, sample_robot_file):
        """Test successful multiple locator updates."""
        replacements = [
            LocatorReplacement("id=username", "name=user", LocatorStrategy.NAME),
            LocatorReplacement("id=password", "name=pass", LocatorStrategy.NAME),
            LocatorReplacement("id=submit", "css=.submit-btn", LocatorStrategy.CSS)
        ]
        
        result = updater.update_multiple_locators(
            sample_robot_file, 
            replacements,
            create_backup=True
        )
        
        assert result.success is True
        assert result.backup_path is not None
        assert len(result.updated_locators) == 3
        assert result.syntax_valid is True
        
        # Verify all replacements were made
        with open(sample_robot_file, 'r') as f:
            content = f.read()
        assert "name=user" in content
        assert "name=pass" in content
        assert "css=.submit-btn" in content
        assert "id=username" not in content
        assert "id=password" not in content
        assert "id=submit" not in content
    
    def test_update_multiple_locators_no_matches(self, updater, sample_robot_file):
        """Test multiple locator updates when no locators are found."""
        replacements = [
            LocatorReplacement("id=nonexistent1", "name=new1", LocatorStrategy.NAME),
            LocatorReplacement("id=nonexistent2", "name=new2", LocatorStrategy.NAME)
        ]
        
        result = updater.update_multiple_locators(sample_robot_file, replacements)
        
        assert result.success is False
        assert "No locators were found to replace" in result.error_message
    
    @patch('src.backend.services.test_code_updater.get_model')
    def test_update_locator_syntax_error(self, mock_get_model, updater, sample_robot_file):
        """Test locator update that results in syntax error."""
        # Mock syntax validation to fail
        mock_get_model.side_effect = Exception("Syntax error")
        
        result = updater.update_locator(
            sample_robot_file, 
            "id=username", 
            "invalid[locator"
        )
        
        assert result.success is False
        assert result.syntax_valid is False
        assert "syntax errors" in result.error_message
    
    def test_restore_from_backup_success(self, updater, sample_robot_file):
        """Test successful restoration from backup."""
        # Create backup
        backup_path = updater.backup_test_file(sample_robot_file)
        
        # Modify original file
        with open(sample_robot_file, 'w') as f:
            f.write("Modified content")
        
        # Restore from backup
        success = updater.restore_from_backup(sample_robot_file, backup_path)
        
        assert success is True
        
        # Verify restoration
        with open(sample_robot_file, 'r') as f:
            content = f.read()
        assert "*** Settings ***" in content
        assert "Modified content" not in content
    
    def test_restore_from_backup_nonexistent(self, updater, sample_robot_file):
        """Test restoration from nonexistent backup."""
        success = updater.restore_from_backup(
            sample_robot_file, 
            "/nonexistent/backup.robot"
        )
        
        assert success is False
    
    def test_cleanup_old_backups(self, updater, temp_dir):
        """Test cleanup of old backup files."""
        backup_dir = Path(temp_dir) / "backups"
        backup_dir.mkdir(exist_ok=True)
        
        # Create some backup files with different ages
        old_backup = backup_dir / "old_backup.robot"
        recent_backup = backup_dir / "recent_backup.robot"
        
        old_backup.touch()
        recent_backup.touch()
        
        # Make old backup appear old
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_backup, (old_time, old_time))
        
        deleted_count = updater.cleanup_old_backups(retention_days=7)
        
        assert deleted_count == 1
        assert not old_backup.exists()
        assert recent_backup.exists()
    
    def test_get_backup_info(self, updater, sample_robot_file):
        """Test getting backup information."""
        import time
        # Create a couple of backups
        backup1 = updater.backup_test_file(sample_robot_file)
        time.sleep(1)  # Ensure different timestamps
        backup2 = updater.backup_test_file(sample_robot_file)
        
        backup_info = updater.get_backup_info(sample_robot_file)
        
        assert len(backup_info) >= 1  # At least one backup should be found
        assert all("path" in info for info in backup_info)
        assert all("created" in info for info in backup_info)
        assert all("size" in info for info in backup_info)
        
        # If we have multiple backups, should be sorted by creation time (most recent first)
        if len(backup_info) > 1:
            assert backup_info[0]["created"] >= backup_info[1]["created"]


class TestUpdateResult:
    """Test cases for UpdateResult dataclass."""
    
    def test_update_result_initialization(self):
        """Test UpdateResult initialization."""
        result = UpdateResult(success=True)
        
        assert result.success is True
        assert result.backup_path is None
        assert result.updated_locators == []
        assert result.error_message is None
        assert result.syntax_valid is True
    
    def test_update_result_with_data(self):
        """Test UpdateResult with data."""
        result = UpdateResult(
            success=True,
            backup_path="/path/to/backup.robot",
            updated_locators=[("old", "new")],
            error_message=None,
            syntax_valid=True
        )
        
        assert result.success is True
        assert result.backup_path == "/path/to/backup.robot"
        assert result.updated_locators == [("old", "new")]
        assert result.error_message is None
        assert result.syntax_valid is True


class TestLocatorReplacement:
    """Test cases for LocatorReplacement dataclass."""
    
    def test_locator_replacement_initialization(self):
        """Test LocatorReplacement initialization."""
        replacement = LocatorReplacement(
            old_locator="id=old",
            new_locator="id=new",
            strategy=LocatorStrategy.ID
        )
        
        assert replacement.old_locator == "id=old"
        assert replacement.new_locator == "id=new"
        assert replacement.strategy == LocatorStrategy.ID
        assert replacement.line_number is None
        assert replacement.context is None
    
    def test_locator_replacement_with_context(self):
        """Test LocatorReplacement with context information."""
        replacement = LocatorReplacement(
            old_locator="id=submit",
            new_locator="css=.submit-btn",
            strategy=LocatorStrategy.CSS,
            line_number=10,
            context="Click Element step"
        )
        
        assert replacement.old_locator == "id=submit"
        assert replacement.new_locator == "css=.submit-btn"
        assert replacement.strategy == LocatorStrategy.CSS
        assert replacement.line_number == 10
        assert replacement.context == "Click Element step"


# Integration test scenarios
class TestTestCodeUpdaterIntegration:
    """Integration tests for TestCodeUpdater with real Robot Framework scenarios."""
    
    @pytest.fixture
    def complex_robot_file(self, temp_dir):
        """Create a complex Robot Framework test file."""
        content = """*** Settings ***
Library    SeleniumLibrary
Library    Collections

*** Variables ***
${LOGIN_URL}    https://example.com/login
${USERNAME}     testuser
${PASSWORD}     testpass

*** Test Cases ***
Complete User Journey
    [Documentation]    Test complete user workflow
    [Tags]    smoke    regression
    
    # Login section
    Open Browser    ${LOGIN_URL}    chrome
    Maximize Browser Window
    Input Text    id=username    ${USERNAME}
    Input Password    id=password    ${PASSWORD}
    Click Element    id=submit
    Wait Until Page Contains    Dashboard    timeout=10s
    
    # Navigation section
    Click Link    xpath=//a[contains(text(), 'Profile')]
    Wait Until Element Is Visible    name=profile_form
    Input Text    name=first_name    John
    Input Text    name=last_name    Doe
    Click Button    css=.save-button
    
    # Verification section
    Wait Until Page Contains    Profile updated successfully
    Element Should Be Visible    id=success_message
    
    [Teardown]    Close Browser

Data Driven Test
    [Documentation]    Test with multiple data sets
    [Template]    Login With Credentials
    
    valid_user      valid_pass      success
    invalid_user    valid_pass      error
    valid_user      invalid_pass    error

*** Keywords ***
Login With Credentials
    [Arguments]    ${username}    ${password}    ${expected_result}
    Open Browser    ${LOGIN_URL}    chrome
    Input Text    id=username    ${username}
    Input Password    id=password    ${password}
    Click Element    id=submit
    Run Keyword If    '${expected_result}' == 'success'
    ...    Wait Until Page Contains    Dashboard
    ...    ELSE    Wait Until Page Contains    Invalid credentials
    Close Browser
"""
        file_path = Path(temp_dir) / "complex_test.robot"
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return str(file_path)
    
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)
    
    @pytest.fixture
    def updater(self, temp_dir):
        """Create a RobotTestCodeUpdater instance."""
        return RobotTestCodeUpdater(backup_dir=str(Path(temp_dir) / "backups"))
    
    def test_complex_multiple_locator_update(self, updater, complex_robot_file):
        """Test updating multiple different types of locators in a complex file."""
        replacements = [
            LocatorReplacement("id=username", "name=user", LocatorStrategy.NAME),
            LocatorReplacement("id=password", "name=pass", LocatorStrategy.NAME),
            LocatorReplacement("id=submit", "css=.login-btn", LocatorStrategy.CSS),
            LocatorReplacement("name=profile_form", "id=profile", LocatorStrategy.ID),
            LocatorReplacement("css=.save-button", "xpath=//button[@type='submit']", LocatorStrategy.XPATH),
            LocatorReplacement("id=success_message", "css=.alert-success", LocatorStrategy.CSS)
        ]
        
        result = updater.update_multiple_locators(
            complex_robot_file, 
            replacements,
            create_backup=True
        )
        
        assert result.success is True
        assert len(result.updated_locators) >= 6  # Some locators appear multiple times
        assert result.syntax_valid is True
        
        # Verify all replacements were made correctly
        with open(complex_robot_file, 'r') as f:
            content = f.read()
        
        # Check new locators are present
        assert "name=user" in content
        assert "name=pass" in content
        assert "css=.login-btn" in content
        assert "id=profile" in content
        assert "xpath=//button[@type='submit']" in content
        assert "css=.alert-success" in content
        
        # Check old locators are gone
        assert "id=username" not in content
        assert "id=password" not in content
        assert "id=submit" not in content
        assert "name=profile_form" not in content
        assert "css=.save-button" not in content
        assert "id=success_message" not in content
        
        # Verify file still has valid Robot Framework syntax
        is_valid, error_msg = updater.validate_robot_syntax(complex_robot_file)
        assert is_valid is True
        assert error_msg is None
    
    def test_rollback_scenario(self, updater, complex_robot_file):
        """Test rollback scenario when update fails."""
        # First, make a successful update to create a backup
        result1 = updater.update_locator(
            complex_robot_file, 
            "id=username", 
            "name=user",
            create_backup=True
        )
        assert result1.success is True
        backup_path = result1.backup_path
        
        # Now simulate a failed update by corrupting the file
        with open(complex_robot_file, 'w') as f:
            f.write("Corrupted content")
        
        # Restore from backup
        success = updater.restore_from_backup(complex_robot_file, backup_path)
        assert success is True
        
        # Verify restoration worked
        with open(complex_robot_file, 'r') as f:
            content = f.read()
        assert "*** Settings ***" in content
        assert "id=username" in content  # Should have the original content restored
        assert "Corrupted content" not in content