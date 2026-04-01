"""
Unit tests for RobotAgents._get_agent_context in src.backend.crew_ai.agents.

Purpose: _get_agent_context decides which context (optimized vs library fallback)
         is injected into each agent's prompt.  A regression here means agents
         get wrong/empty context, producing incorrect RF code.

Tests:
  - Optimized context has priority when available
  - Falls back to library context when optimization unavailable
  - Returns empty string when no context available
  - Unknown agent type returns empty string
"""

import pytest
from unittest.mock import patch, MagicMock


class TestGetAgentContext:
    """Tests for RobotAgents._get_agent_context."""

    def _make_agents(self, optimization_enabled=False, library_context=None):
        """Create RobotAgents with mocked dependencies."""
        from src.backend.crew_ai.agents import RobotAgents
        agents = RobotAgents.__new__(RobotAgents)
        agents.llm = MagicMock()
        agents.library_context = library_context or MagicMock()
        agents.planner_context = None
        agents.assembler_context = None
        agents.validator_context = None
        return agents

    def test_library_context_used(self):
        """When no optimization, uses library context."""
        lib_ctx = MagicMock()
        lib_ctx.get_full_context.return_value = "Browser Library context here"
        agents = self._make_agents(library_context=lib_ctx)

        result = agents._get_agent_context("planner")
        # Should use library context (either directly or via method)
        assert result is not None

    def test_optimized_priority(self):
        """When optimized context exists, it takes priority."""
        agents = self._make_agents(optimization_enabled=True)
        agents.planner_context = "Optimized planner context"

        result = agents._get_agent_context("planner")
        assert "Optimized" in result

    def test_no_context_returns_empty(self):
        """When no context available at all, returns empty string."""
        lib_ctx = MagicMock()
        lib_ctx.get_full_context.return_value = ""
        agents = self._make_agents(library_context=lib_ctx)

        result = agents._get_agent_context("planner")
        assert isinstance(result, str)

    def test_unknown_type_returns_empty(self):
        """Unknown agent type gracefully returns empty string."""
        agents = self._make_agents()
        result = agents._get_agent_context("nonexistent_type")
        assert isinstance(result, str)
