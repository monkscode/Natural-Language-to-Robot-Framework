"""
Base interface for library-specific context.

This defines the contract that all library contexts must implement,
ensuring consistent behavior across different Robot Framework libraries.
"""

from abc import ABC, abstractmethod


class LibraryContext(ABC):
    """
    Abstract base class for library-specific context.

    Each library context provides syntax examples, keyword documentation,
    and best practices that AI agents use to generate correct code dynamically.
    """

    @property
    @abstractmethod
    def library_name(self) -> str:
        """Return the library name (e.g., 'SeleniumLibrary', 'Browser')."""
        pass

    @property
    @abstractmethod
    def library_import(self) -> str:
        """Return the Robot Framework import statement."""
        pass

    @property
    @abstractmethod
    def planning_context(self) -> str:
        """
        Context for the Step Planner Agent.

        Provides:
        - Available keywords
        - When to use each keyword
        - Best practices for planning
        """
        pass

    @property
    @abstractmethod
    def code_assembly_context(self) -> str:
        """
        Context for the Code Assembler Agent.

        Provides:
        - Code structure templates
        - Syntax rules
        - Variable declaration patterns
        - Complete examples
        """
        pass

    @property
    @abstractmethod
    def validation_context(self) -> str:
        """
        Context for the Code Validator Agent.

        Provides:
        - Common syntax errors
        - Validation rules
        - Correct vs incorrect examples
        """
        pass

    def get_full_context(self, agent_role: str) -> str:
        """
        Get complete context for a specific agent role.

        Args:
            agent_role: One of "planner", "assembler", "validator"

        Returns:
            Complete context string for that agent
        """
        if agent_role == "planner":
            return self.planning_context
        elif agent_role == "assembler":
            return self.code_assembly_context
        elif agent_role == "validator":
            return self.validation_context
        else:
            raise ValueError(f"Unknown agent role: {agent_role}")
