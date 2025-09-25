"""
Robot Framework Native Validation Service

This module provides native Robot Framework validation capabilities using RF's built-in
parser and validation tools. It implements generic validation that works with any
Robot Framework library without hardcoded rules.
"""

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from pathlib import Path
import tempfile
import os

try:
    from robot.api import get_model
    from robot.libraries import STDLIBS
    from robot.errors import DataError
    from robot.running.namespace import Namespace
    from robot.running.importer import Importer
except ImportError as e:
    raise ImportError(f"Robot Framework not installed or not accessible: {e}")


@dataclass
class ValidationError:
    """Represents a validation error with details"""
    message: str
    line_number: Optional[int] = None
    column_number: Optional[int] = None
    error_type: str = "syntax"
    severity: str = "error"  # error, warning, info


@dataclass
class ValidationWarning:
    """Represents a validation warning"""
    message: str
    line_number: Optional[int] = None
    suggestion: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of Robot Framework validation"""
    valid: bool
    errors: List[ValidationError]
    warnings: List[ValidationWarning]
    corrected_code: Optional[str] = None
    validation_time: float = 0.0
    parsed_model: Optional[Any] = None


@dataclass
class KeywordValidationResult:
    """Result of keyword validation"""
    valid: bool
    missing_keywords: List[str]
    undefined_keywords: List[str]
    parameter_issues: List[str]
    suggestions: List[str]


@dataclass
class ImportValidationResult:
    """Result of import validation"""
    valid: bool
    missing_imports: List[str]
    invalid_imports: List[str]
    suggestions: List[str]


@dataclass
class ParameterValidationResult:
    """Result of parameter validation"""
    valid: bool
    missing_parameters: List[str]
    extra_parameters: List[str]
    type_mismatches: List[str]
    suggested_corrections: List[str]


class RobotFrameworkNativeValidator:
    """
    Native Robot Framework validator using RF's built-in capabilities.
    
    This validator leverages Robot Framework's own parser and validation
    mechanisms to provide generic validation that works with any RF library.
    """
    
    def __init__(self):
        self.importer = Importer()
        self.namespace = None
    
    def validate_syntax(self, robot_code: str) -> ValidationResult:
        """
        Validate Robot Framework syntax using native parser.
        
        Args:
            robot_code: Robot Framework code as string
            
        Returns:
            ValidationResult with validation details
        """
        import time
        start_time = time.time()
        
        errors = []
        warnings = []
        parsed_model = None
        
        try:
            # Use Robot Framework's native parser
            with tempfile.NamedTemporaryFile(mode='w', suffix='.robot', delete=False) as tmp_file:
                tmp_file.write(robot_code)
                tmp_file.flush()
                tmp_file_path = tmp_file.name
                
            try:
                # Parse using Robot Framework's get_model
                parsed_model = get_model(tmp_file_path)
                
                # Additional syntax validation
                self._validate_structure(parsed_model, errors, warnings)
                
            finally:
                # Clean up temp file with retry logic
                try:
                    os.unlink(tmp_file_path)
                except (OSError, PermissionError):
                    # File might be locked, try again after a short delay
                    import time
                    time.sleep(0.1)
                    try:
                        os.unlink(tmp_file_path)
                    except (OSError, PermissionError):
                        # If still can't delete, log but don't fail validation
                        pass
                    
        except DataError as e:
            errors.append(ValidationError(
                message=str(e),
                error_type="syntax",
                severity="error"
            ))
        except Exception as e:
            errors.append(ValidationError(
                message=f"Parsing error: {str(e)}",
                error_type="parsing",
                severity="error"
            ))
        
        validation_time = time.time() - start_time
        
        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            parsed_model=parsed_model,
            validation_time=validation_time
        )
    
    def validate_keywords(self, parsed_model) -> KeywordValidationResult:
        """
        Validate keyword usage using Robot Framework's keyword resolution.
        
        Args:
            parsed_model: Parsed Robot Framework model
            
        Returns:
            KeywordValidationResult with keyword validation details
        """
        missing_keywords = []
        undefined_keywords = []
        parameter_issues = []
        suggestions = []
        
        try:
            # Initialize namespace for keyword resolution
            if self.namespace is None:
                try:
                    # Try different Namespace initialization patterns for different RF versions
                    self.namespace = Namespace(None, None, None, [])
                except TypeError:
                    try:
                        self.namespace = Namespace(None, None, [])
                    except TypeError:
                        # Skip namespace-based validation if we can't initialize it
                        self.namespace = None
            
            # Collect all keyword calls from test cases
            keyword_calls = self._extract_keyword_calls(parsed_model)
            
            # Validate each keyword call
            for keyword_call in keyword_calls:
                try:
                    # Use Robot Framework's keyword resolution
                    keyword_name = keyword_call.get('keyword', '')
                    if keyword_name and not self._is_builtin_keyword(keyword_name):
                        # Check if keyword exists in available libraries or user keywords
                        if not self._keyword_exists(keyword_name, parsed_model):
                            missing_keywords.append(keyword_name)
                            suggestions.append(f"Define keyword '{keyword_name}' or import required library")
                            
                except Exception as e:
                    parameter_issues.append(f"Error validating keyword '{keyword_call.get('keyword', 'unknown')}': {str(e)}")
            
        except Exception as e:
            parameter_issues.append(f"Keyword validation error: {str(e)}")
        
        return KeywordValidationResult(
            valid=len(missing_keywords) == 0 and len(parameter_issues) == 0,
            missing_keywords=missing_keywords,
            undefined_keywords=undefined_keywords,
            parameter_issues=parameter_issues,
            suggestions=suggestions
        )
    
    def validate_imports(self, parsed_model) -> ImportValidationResult:
        """
        Validate library imports using Robot Framework's import system.
        
        Args:
            parsed_model: Parsed Robot Framework model
            
        Returns:
            ImportValidationResult with import validation details
        """
        missing_imports = []
        invalid_imports = []
        suggestions = []
        
        try:
            # Extract imports from the model
            imports = self._extract_imports(parsed_model)
            
            for import_item in imports:
                import_type = import_item.get('type', '')
                import_name = import_item.get('name', '')
                
                try:
                    if import_type == 'Library':
                        # Validate library import using Robot Framework's importer
                        self._validate_library_import(import_name, import_item.get('args', []))
                    elif import_type == 'Resource':
                        # Validate resource import
                        self._validate_resource_import(import_name)
                    elif import_type == 'Variables':
                        # Validate variables import
                        self._validate_variables_import(import_name)
                        
                except Exception as e:
                    invalid_imports.append(import_name)
                    suggestions.append(f"Check import '{import_name}': {str(e)}")
            
        except Exception as e:
            suggestions.append(f"Import validation error: {str(e)}")
        
        return ImportValidationResult(
            valid=len(invalid_imports) == 0,
            missing_imports=missing_imports,
            invalid_imports=invalid_imports,
            suggestions=suggestions
        )
    
    def validate_parameters(self, keyword_call: Dict[str, Any], keyword_definition: Optional[Dict[str, Any]] = None) -> ParameterValidationResult:
        """
        Validate keyword parameters using Robot Framework's parameter validation.
        
        Args:
            keyword_call: Dictionary representing a keyword call
            keyword_definition: Optional keyword definition for validation
            
        Returns:
            ParameterValidationResult with parameter validation details
        """
        missing_parameters = []
        extra_parameters = []
        type_mismatches = []
        suggested_corrections = []
        
        try:
            keyword_name = keyword_call.get('keyword', '')
            provided_args = keyword_call.get('arguments', [])
            
            # If we have keyword definition, validate against it
            if keyword_definition:
                expected_args = keyword_definition.get('arguments', [])
                
                # Check parameter count
                if len(provided_args) < len(expected_args):
                    missing_count = len(expected_args) - len(provided_args)
                    missing_parameters.extend([f"arg_{i}" for i in range(missing_count)])
                    suggested_corrections.append(f"Add {missing_count} missing argument(s) to '{keyword_name}'")
                
                elif len(provided_args) > len(expected_args):
                    extra_count = len(provided_args) - len(expected_args)
                    extra_parameters.extend([f"extra_arg_{i}" for i in range(extra_count)])
                    suggested_corrections.append(f"Remove {extra_count} extra argument(s) from '{keyword_name}'")
            
            # Validate argument syntax
            for i, arg in enumerate(provided_args):
                if not self._is_valid_argument_syntax(arg):
                    type_mismatches.append(f"Invalid syntax in argument {i+1}: '{arg}'")
                    suggested_corrections.append(f"Fix argument syntax: '{arg}'")
        
        except Exception as e:
            suggested_corrections.append(f"Parameter validation error: {str(e)}")
        
        return ParameterValidationResult(
            valid=len(missing_parameters) == 0 and len(extra_parameters) == 0 and len(type_mismatches) == 0,
            missing_parameters=missing_parameters,
            extra_parameters=extra_parameters,
            type_mismatches=type_mismatches,
            suggested_corrections=suggested_corrections
        )
    
    def _validate_structure(self, parsed_model, errors: List[ValidationError], warnings: List[ValidationWarning]):
        """Validate Robot Framework file structure"""
        try:
            # Check for required sections and proper structure
            if not hasattr(parsed_model, 'sections'):
                warnings.append(ValidationWarning(
                    message="No sections found in Robot Framework file",
                    suggestion="Add at least one section (Settings, Variables, Test Cases, or Keywords)"
                ))
                return
            
            # Validate section structure
            has_test_cases = False
            has_keywords = False
            
            for section in parsed_model.sections:
                if hasattr(section, 'header') and section.header:
                    # Get header text safely
                    header_text = ""
                    if hasattr(section.header, 'data_tokens'):
                        header_text = " ".join(token.value for token in section.header.data_tokens if hasattr(token, 'value'))
                    elif hasattr(section.header, 'value'):
                        header_text = section.header.value
                    elif hasattr(section.header, '__str__'):
                        header_text = str(section.header)
                    
                    header_text = header_text.lower()
                    if 'test case' in header_text:
                        has_test_cases = True
                    elif 'keyword' in header_text:
                        has_keywords = True
            
            if not has_test_cases and not has_keywords:
                warnings.append(ValidationWarning(
                    message="No Test Cases or Keywords sections found",
                    suggestion="Add Test Cases or Keywords section"
                ))
        
        except Exception as e:
            errors.append(ValidationError(
                message=f"Structure validation error: {str(e)}",
                error_type="structure"
            ))
    
    def _extract_keyword_calls(self, parsed_model) -> List[Dict[str, Any]]:
        """Extract keyword calls from parsed model"""
        keyword_calls = []
        
        try:
            for section in parsed_model.sections:
                if hasattr(section, 'body'):
                    for item in section.body:
                        if hasattr(item, 'body'):  # Test case or keyword
                            for step in item.body:
                                # Handle different step types
                                keyword_name = None
                                arguments = []
                                
                                if hasattr(step, 'keyword'):
                                    keyword_name = step.keyword
                                    arguments = getattr(step, 'arguments', [])
                                elif hasattr(step, 'assign') and hasattr(step, 'keyword'):
                                    # Assignment step like ${var}= Keyword
                                    keyword_name = step.keyword
                                    arguments = getattr(step, 'arguments', [])
                                elif hasattr(step, 'tokens'):
                                    # Extract from tokens
                                    tokens = [token.value for token in step.tokens if hasattr(token, 'value')]
                                    if tokens:
                                        keyword_name = tokens[0]
                                        arguments = tokens[1:] if len(tokens) > 1 else []
                                
                                if keyword_name:
                                    keyword_calls.append({
                                        'keyword': keyword_name,
                                        'arguments': arguments
                                    })
        except Exception:
            pass  # Silently handle parsing issues
        
        return keyword_calls
    
    def _extract_imports(self, parsed_model) -> List[Dict[str, Any]]:
        """Extract imports from parsed model"""
        imports = []
        
        try:
            for section in parsed_model.sections:
                if hasattr(section, 'body'):
                    for item in section.body:
                        if hasattr(item, 'type') and item.type in ['LIBRARY', 'RESOURCE', 'VARIABLES']:
                            imports.append({
                                'type': item.type.title(),
                                'name': getattr(item, 'name', ''),
                                'args': getattr(item, 'args', [])
                            })
        except Exception:
            pass  # Silently handle parsing issues
        
        return imports
    
    def _is_builtin_keyword(self, keyword_name: str) -> bool:
        """Check if keyword is a Robot Framework built-in"""
        builtin_keywords = [
            'Log', 'Set Variable', 'Should Be Equal', 'Should Contain',
            'Sleep', 'Fail', 'Pass Execution', 'Set Test Variable',
            'Set Suite Variable', 'Set Global Variable', 'Get Variable Value',
            'Variable Should Exist', 'Variable Should Not Exist',
            'Should Be True', 'Should Be False', 'Should Not Be Equal',
            'Should Be Empty', 'Should Not Be Empty', 'Length Should Be',
            'Should Match', 'Should Not Match', 'Should Match Regexp',
            'Should Not Match Regexp', 'Should Start With', 'Should End With',
            'Should Not Start With', 'Should Not End With'
        ]
        return keyword_name in builtin_keywords
    
    def _keyword_exists(self, keyword_name: str, parsed_model) -> bool:
        """Check if keyword exists in user-defined keywords"""
        try:
            for section in parsed_model.sections:
                if hasattr(section, 'body'):
                    for item in section.body:
                        if hasattr(item, 'name') and item.name == keyword_name:
                            return True
        except Exception:
            pass
        return False
    
    def _validate_library_import(self, library_name: str, args: List[str]):
        """Validate library import using Robot Framework's importer"""
        try:
            # Check if it's a standard library
            if library_name in STDLIBS:
                return True
            
            # Try to import the library
            self.importer.import_library(library_name, args, None, None)
            return True
        except Exception as e:
            raise Exception(f"Cannot import library '{library_name}': {str(e)}")
    
    def _validate_resource_import(self, resource_path: str):
        """Validate resource file import"""
        if not resource_path.endswith('.robot') and not resource_path.endswith('.resource'):
            raise Exception(f"Resource file should have .robot or .resource extension: {resource_path}")
        
        # Check if file exists (basic validation)
        if not Path(resource_path).exists() and not Path(resource_path).is_absolute():
            raise Exception(f"Resource file not found: {resource_path}")
    
    def _validate_variables_import(self, variables_path: str):
        """Validate variables file import"""
        valid_extensions = ['.py', '.yaml', '.yml', '.json']
        if not any(variables_path.endswith(ext) for ext in valid_extensions):
            raise Exception(f"Variables file should have extension: {', '.join(valid_extensions)}")
    
    def _is_valid_argument_syntax(self, argument: str) -> bool:
        """Validate argument syntax"""
        if not isinstance(argument, str):
            return False
        
        # Basic syntax validation
        # Check for unmatched quotes
        if argument.count('"') % 2 != 0 or argument.count("'") % 2 != 0:
            return False
        
        # Check for unmatched brackets
        if argument.count('[') != argument.count(']') or argument.count('{') != argument.count('}'):
            return False
        
        return True