"""Test code updater service for safely modifying Robot Framework test files."""

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import re
import logging
from dataclasses import dataclass

from robot.api import get_model

from ..core.models import HealingSession, LocatorStrategy


logger = logging.getLogger(__name__)


@dataclass
class UpdateResult:
    """Result of a test file update operation."""
    success: bool
    backup_path: Optional[str] = None
    updated_locators: List[Tuple[str, str]] = None  # (old, new) pairs
    error_message: Optional[str] = None
    syntax_valid: bool = True
    
    def __post_init__(self):
        if self.updated_locators is None:
            self.updated_locators = []


@dataclass
class LocatorReplacement:
    """Represents a locator replacement operation."""
    old_locator: str
    new_locator: str
    strategy: LocatorStrategy
    line_number: Optional[int] = None
    context: Optional[str] = None


class RobotTestCodeUpdater:
    """Service for safely updating Robot Framework test files with healed locators."""
    
    def __init__(self, backup_dir: Optional[str] = None):
        """Initialize the test code updater.
        
        Args:
            backup_dir: Directory to store backup files. If None, uses temp directory.
        """
        self.backup_dir = Path(backup_dir) if backup_dir else Path(tempfile.gettempdir()) / "robot_backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        
    def backup_test_file(self, file_path: str) -> str:
        """Create a timestamped backup of the test file.
        
        Args:
            file_path: Path to the test file to backup
            
        Returns:
            Path to the backup file
            
        Raises:
            FileNotFoundError: If the source file doesn't exist
            IOError: If backup creation fails
        """
        source_path = Path(file_path)
        if not source_path.exists():
            raise FileNotFoundError(f"Test file not found: {file_path}")
            
        # Create backup filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"{source_path.stem}_{timestamp}{source_path.suffix}"
        backup_path = self.backup_dir / backup_filename
        
        try:
            shutil.copy2(source_path, backup_path)
            logger.info(f"Created backup: {backup_path}")
            return str(backup_path)
        except Exception as e:
            logger.error(f"Failed to create backup: {e}")
            raise IOError(f"Failed to create backup: {e}")
    
    def validate_robot_syntax(self, file_path: str) -> Tuple[bool, Optional[str]]:
        """Validate Robot Framework syntax of a file.
        
        Args:
            file_path: Path to the Robot Framework file
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            get_model(file_path)
            return True, None
        except Exception as e:
            error_msg = f"Syntax validation failed: {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def find_locator_in_file(self, file_path: str, locator: str) -> List[Tuple[int, str]]:
        """Find all occurrences of a locator in the file.
        
        Args:
            file_path: Path to the Robot Framework file
            locator: Locator string to find
            
        Returns:
            List of (line_number, line_content) tuples where locator was found
        """
        matches = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            for line_num, line in enumerate(lines, 1):
                if locator in line:
                    matches.append((line_num, line.strip()))
                    
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
            
        return matches
    
    def replace_locator_in_line(self, line: str, old_locator: str, new_locator: str) -> str:
        """Replace a locator in a single line while preserving Robot Framework syntax.
        
        Args:
            line: The line containing the locator
            old_locator: The locator to replace
            new_locator: The replacement locator
            
        Returns:
            Updated line with replaced locator
        """
        # Handle different locator formats in Robot Framework
        patterns = [
            # Direct locator usage: Click Element    id=submit
            (rf'\b{re.escape(old_locator)}\b', new_locator),
            # Variable assignment: ${locator}=    Set Variable    id=submit
            (rf'(\$\{{[^}}]*\}}\s*=\s*Set Variable\s+){re.escape(old_locator)}\b', rf'\1{new_locator}'),
            # Quoted locators: Click Element    "id=submit"
            (rf'"{re.escape(old_locator)}"', f'"{new_locator}"'),
            # Single quoted locators: Click Element    'id=submit'
            (rf"'{re.escape(old_locator)}'", f"'{new_locator}'"),
        ]
        
        updated_line = line
        for pattern, replacement in patterns:
            if re.search(pattern, updated_line):
                updated_line = re.sub(pattern, replacement, updated_line)
                break
                
        return updated_line
    
    def update_locator(self, file_path: str, old_locator: str, new_locator: str, 
                      create_backup: bool = True) -> UpdateResult:
        """Update a single locator in a Robot Framework test file.
        
        Args:
            file_path: Path to the test file
            old_locator: The locator to replace
            new_locator: The replacement locator
            create_backup: Whether to create a backup before updating
            
        Returns:
            UpdateResult with operation details
        """
        result = UpdateResult(success=False)
        
        try:
            # Validate file exists
            if not Path(file_path).exists():
                result.error_message = f"File not found: {file_path}"
                return result
            
            # Create backup if requested
            if create_backup:
                result.backup_path = self.backup_test_file(file_path)
            
            # Find locator occurrences
            matches = self.find_locator_in_file(file_path, old_locator)
            if not matches:
                result.error_message = f"Locator '{old_locator}' not found in file"
                return result
            
            # Read file content
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # Replace locators
            updated_lines = []
            replacements_made = []
            
            for i, line in enumerate(lines):
                if old_locator in line:
                    updated_line = self.replace_locator_in_line(line, old_locator, new_locator)
                    if updated_line != line:
                        updated_lines.append(updated_line)
                        replacements_made.append((old_locator, new_locator))
                        logger.info(f"Replaced locator on line {i+1}: '{old_locator}' -> '{new_locator}'")
                    else:
                        updated_lines.append(line)
                else:
                    updated_lines.append(line)
            
            # Write updated content atomically
            temp_file = f"{file_path}.tmp"
            try:
                with open(temp_file, 'w', encoding='utf-8') as f:
                    f.writelines(updated_lines)
                
                # Validate syntax of updated file
                is_valid, error_msg = self.validate_robot_syntax(temp_file)
                if not is_valid:
                    result.error_message = f"Updated file has syntax errors: {error_msg}"
                    result.syntax_valid = False
                    os.remove(temp_file)
                    return result
                
                # Atomic move
                shutil.move(temp_file, file_path)
                
                result.success = True
                result.updated_locators = replacements_made
                result.syntax_valid = True
                
            except Exception as e:
                # Clean up temp file on error
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                raise e
                
        except Exception as e:
            result.error_message = f"Update failed: {str(e)}"
            logger.error(f"Failed to update locator in {file_path}: {e}")
            
        return result
    
    def update_multiple_locators(self, file_path: str, 
                               replacements: List[LocatorReplacement],
                               create_backup: bool = True) -> UpdateResult:
        """Update multiple locators in a single Robot Framework test file.
        
        Args:
            file_path: Path to the test file
            replacements: List of locator replacements to make
            create_backup: Whether to create a backup before updating
            
        Returns:
            UpdateResult with operation details
        """
        result = UpdateResult(success=False)
        
        try:
            # Validate file exists
            if not Path(file_path).exists():
                result.error_message = f"File not found: {file_path}"
                return result
            
            # Create backup if requested
            if create_backup:
                result.backup_path = self.backup_test_file(file_path)
            
            # Read file content
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # Apply all replacements
            updated_lines = lines.copy()
            replacements_made = []
            
            for replacement in replacements:
                for i, line in enumerate(updated_lines):
                    if replacement.old_locator in line:
                        updated_line = self.replace_locator_in_line(
                            line, replacement.old_locator, replacement.new_locator
                        )
                        if updated_line != line:
                            updated_lines[i] = updated_line
                            replacements_made.append((replacement.old_locator, replacement.new_locator))
                            logger.info(f"Replaced locator on line {i+1}: "
                                      f"'{replacement.old_locator}' -> '{replacement.new_locator}'")
            
            if not replacements_made:
                result.error_message = "No locators were found to replace"
                return result
            
            # Write updated content atomically
            temp_file = f"{file_path}.tmp"
            try:
                with open(temp_file, 'w', encoding='utf-8') as f:
                    f.writelines(updated_lines)
                
                # Validate syntax of updated file
                is_valid, error_msg = self.validate_robot_syntax(temp_file)
                if not is_valid:
                    result.error_message = f"Updated file has syntax errors: {error_msg}"
                    result.syntax_valid = False
                    os.remove(temp_file)
                    return result
                
                # Atomic move
                shutil.move(temp_file, file_path)
                
                result.success = True
                result.updated_locators = replacements_made
                result.syntax_valid = True
                
            except Exception as e:
                # Clean up temp file on error
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                raise e
                
        except Exception as e:
            result.error_message = f"Update failed: {str(e)}"
            logger.error(f"Failed to update locators in {file_path}: {e}")
            
        return result
    
    def restore_from_backup(self, file_path: str, backup_path: str) -> bool:
        """Restore a test file from its backup.
        
        Args:
            file_path: Path to the test file to restore
            backup_path: Path to the backup file
            
        Returns:
            True if restoration was successful, False otherwise
        """
        try:
            if not Path(backup_path).exists():
                logger.error(f"Backup file not found: {backup_path}")
                return False
            
            shutil.copy2(backup_path, file_path)
            logger.info(f"Restored {file_path} from backup {backup_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to restore from backup: {e}")
            return False
    
    def cleanup_old_backups(self, retention_days: int = 7) -> int:
        """Clean up backup files older than specified days.
        
        Args:
            retention_days: Number of days to retain backups
            
        Returns:
            Number of backup files deleted
        """
        deleted_count = 0
        cutoff_time = datetime.now().timestamp() - (retention_days * 24 * 3600)
        
        try:
            for backup_file in self.backup_dir.glob("*.robot"):
                if backup_file.stat().st_mtime < cutoff_time:
                    backup_file.unlink()
                    deleted_count += 1
                    logger.info(f"Deleted old backup: {backup_file}")
                    
        except Exception as e:
            logger.error(f"Error cleaning up backups: {e}")
            
        return deleted_count
    
    def get_backup_info(self, file_path: str) -> List[Dict[str, str]]:
        """Get information about available backups for a test file.
        
        Args:
            file_path: Path to the test file
            
        Returns:
            List of backup information dictionaries
        """
        backups = []
        file_stem = Path(file_path).stem
        
        try:
            for backup_file in self.backup_dir.glob(f"{file_stem}_*.robot"):
                stat = backup_file.stat()
                backups.append({
                    "path": str(backup_file),
                    "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "size": stat.st_size
                })
                
        except Exception as e:
            logger.error(f"Error getting backup info: {e}")
            
        return sorted(backups, key=lambda x: x["created"], reverse=True)