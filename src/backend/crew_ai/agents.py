import os
import sys
import logging
from crewai import Agent
from crewai.llm import LLM
from crewai_tools import SeleniumScrapingTool, ScrapeElementFromWebsiteTool
from langchain_ollama import OllamaLLM

# Import browser_use_tool from tools package
# Note: Path setup is handled by tools/__init__.py automatically
from tools.browser_use_tool import BatchBrowserUseTool

# Import cleaned LLM wrapper for robust output parsing
from .cleaned_llm_wrapper import get_cleaned_llm

logger = logging.getLogger(__name__)

# Initialize the LLMs


def get_llm(model_provider, model_name):
    """
    Get LLM instance for the specified provider with automatic output cleaning.

    This function now returns cleaned LLM wrappers that automatically fix
    common formatting issues (like extra text on Action lines) before CrewAI
    parses the output. This prevents workflow failures due to LLM formatting quirks.

    The cleaning is transparent - the LLM behaves exactly the same but with
    robust parsing that handles real-world LLM behavior.

    Args:
        model_provider: "local" for Ollama, "online" for Gemini
        model_name: Model identifier (e.g., "llama3.1", "gemini-2.5-flash")

    Returns:
        Cleaned LLM wrapper instance that automatically fixes formatting issues
    """
    logger.info(
        f"🧹 Initializing cleaned LLM wrapper for {model_provider}/{model_name}")
    return get_cleaned_llm(model_provider, model_name)


# Initialize the tools
# Note: These are tool instances, not classes. CrewAI requires instantiated tools.
selenium_tool = SeleniumScrapingTool()
scrape_tool = ScrapeElementFromWebsiteTool()
# Primary tool: Batch processing for multiple elements with full context
batch_browser_use_tool = BatchBrowserUseTool()


class RobotAgents:
    def __init__(self, model_provider, model_name, library_context=None, 
                 optimized_context=None, keyword_search_tool=None,
                 planner_context=None, identifier_context=None,
                 assembler_context=None, validator_context=None):
        """
        Initialize Robot Framework agents.

        Args:
            model_provider: "local" or "online"
            model_name: Model identifier
            library_context: LibraryContext instance (optional, for dynamic keyword knowledge)
            optimized_context: DEPRECATED - Use assembler_context instead (kept for backward compatibility)
            keyword_search_tool: KeywordSearchTool instance (optional, added to code assembler tools)
            planner_context: Optimized context for Test Automation Planner (optional)
            identifier_context: Optimized context for Element Identifier (optional, currently unused)
            assembler_context: Optimized context for Code Assembler (optional)
            validator_context: Optimized context for Code Validator (optional)
        """
        self.llm = get_llm(model_provider, model_name)
        self.library_context = library_context
        self.keyword_search_tool = keyword_search_tool
        
        # Handle backward compatibility for optimized_context (deprecated)
        if optimized_context is not None and assembler_context is None:
            logger.warning(
                "⚠️ DEPRECATION WARNING: 'optimized_context' parameter is deprecated. "
                "Use 'assembler_context' instead for clarity and consistency."
            )
            assembler_context = optimized_context
        
        # Role-specific optimized contexts
        self.planner_context = planner_context
        self.identifier_context = identifier_context  # Currently unused by element_identifier_agent
        self.assembler_context = assembler_context
        self.validator_context = validator_context

    def _get_agent_context(self, agent_type: str) -> str:
        """
        Unified context retrieval with consistent priority chain.
        
        Priority:
        1. Optimized context (from SmartKeywordProvider - pattern learning/zero-context+tool)
        2. Library context (static context from library_context)
        3. Empty string (graceful degradation)
        
        Args:
            agent_type: "planner", "assembler", or "validator"
        
        Returns:
            Context string with appropriate formatting
        """
        # Map agent type to optimized context attribute
        optimized_context_map = {
            "planner": self.planner_context,
            "assembler": self.assembler_context,
            "validator": self.validator_context
        }
        
        # Map agent type to library context property
        library_context_map = {
            "planner": "planning_context",
            "assembler": "code_assembly_context",
            "validator": "validation_context"
        }
        
        optimized_context = optimized_context_map.get(agent_type)
        
        # Priority 1: Use optimized context if available
        if optimized_context:
            logger.info(f"🎯 {agent_type.capitalize()} using optimized context")
            return f"\n\n{optimized_context}"
        
        # Priority 2: Fall back to library context
        if self.library_context:
            library_property = library_context_map.get(agent_type)
            if library_property:
                static_context = getattr(self.library_context, library_property)
                logger.info(f"📚 {agent_type.capitalize()} using static library context (optimization not available)")
                return f"\n\n{static_context}"
        
        # Priority 3: Graceful degradation
        logger.warning(f"⚠️ {agent_type.capitalize()} has no context available - using minimal validation")
        return ""

    def step_planner_agent(self) -> Agent:
        # Step Planner needs MINIMAL context - just library name and planning rules
        # It doesn't need keyword details or implementation specifics - that's for the Code Assembler
        library_name = self.library_context.library_name if self.library_context else 'Robot Framework'
        
        # Get library-specific planning rules (timing behavior, capabilities)
        library_guidance = ""
        if self.library_context:
            library_guidance = f"""

**{library_name} PLANNING CONSIDERATIONS:**
{self.library_context.planning_rules}

**Remember:** Create HIGH-LEVEL steps. The Code Assembler handles implementation details.
"""

        return Agent(
            role="Test Automation Planner",
            goal=f"Break down a natural language query into a structured series of high-level test steps for Robot Framework using {library_name}. ONLY include elements and actions explicitly mentioned in the user's query.",
            backstory=(
                "You are an expert test automation planner with a strict focus on user requirements. "
                "Your task is to analyze the user's query and convert ONLY the explicitly mentioned actions into structured test steps. "
                "CRITICAL RULES:\n"
                "1. ONLY create steps for elements and actions explicitly mentioned by the user\n"
                "2. DO NOT add popup dismissal, cookie consent, or any 'smart' helper steps\n"
                "3. DO NOT anticipate or add steps for common website patterns (login, popups, etc.)\n"
                "4. The browser automation will handle popups contextually - you don't need to\n"
                "5. If user says 'search for shoes', create steps for: search input + enter. Nothing else.\n"
                "6. If user says 'get product name', create step for: get product name. Nothing else.\n"
                "7. Be meticulous but ONLY for what user explicitly asked for.\n"
                "8. Create HIGH-LEVEL steps - the Code Assembler will handle keyword details."
                f"{library_guidance}"
            ),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def element_identifier_agent(self) -> Agent:
        return Agent(
            role="Advanced Web Element Locator Specialist with Batch Vision AI",
            goal="Use batch_browser_automation to find ALL element locators in ONE call.",
            backstory=(
                "Expert web element locator using batch vision AI. "
                "Workflow: (1) Collect ALL elements from test steps, (2) Extract URL, (3) Call batch_browser_automation ONCE with all elements, (4) Map locators to steps. "
                "Tool format: Action: batch_browser_automation | Action Input: {\"elements\": [{\"id\": \"elem_1\", \"description\": \"...\", \"action\": \"input/click/get_text\"}], \"url\": \"...\", \"user_query\": \"...\"}. "
                "CRITICAL: Action line must have ONLY 'batch_browser_automation' with NO extra text. Action Input must be a dict {}, NOT array []. "
                "Benefits: Browser opens once (3-5x faster), full context awareness, intelligent popup handling, validated locators. "
                "⚠️ CRITICAL: Always use the 'best_locator' value from locator_mapping - it has been AI-validated and scored. "
                "DO NOT analyze or override with your own preference. See task description for detailed rules."
            ),
            # NEW: Batch processing tool for multiple elements
            tools=[batch_browser_use_tool],
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def code_assembler_agent(self) -> Agent:
        # Get context via unified method with consistent priority chain
        library_knowledge = self._get_agent_context("assembler")

        return Agent(
            role="Robot Framework Code Generator (Output ONLY Code)",
            goal=f"Generate ONLY raw Robot Framework code using {self.library_context.library_name if self.library_context else 'Robot Framework'}. NO explanations, NO thinking process, ONLY code.",
            backstory=(
                "You are a CODE PRINTER, not a code explainer. Your ONLY job is to output raw Robot Framework code.\n\n"
                
                "🚫 **ABSOLUTELY FORBIDDEN IN YOUR OUTPUT** 🚫\n"
                "You must NEVER include:\n"
                "❌ Thinking process ('Thought:', 'I will', 'Let me', 'First', 'Now')\n"
                "❌ Explanations ('From the first step:', 'Also add', 'This is because')\n"
                "❌ Markdown formatting ('**Variables:**', '```robot', '```')\n"
                "❌ Numbered lists ('1. New Browser', '2. New Context')\n"
                "❌ Commentary ('# This does X', except actual Robot Framework comments)\n"
                "❌ Any text before *** Settings ***\n"
                "❌ Any text after the last keyword (Close Browser, etc.)\n\n"
                
                "✅ **YOUR OUTPUT MUST BE** ✅\n"
                "ONLY raw Robot Framework code that:\n"
                "1. Starts IMMEDIATELY with *** Settings *** (first line, first character)\n"
                "2. Contains ONLY valid Robot Framework syntax\n"
                "3. Has NO explanatory text anywhere\n"
                "4. Can be directly saved as a .robot file and executed\n\n"
                
                "📋 **EXAMPLE OF CORRECT OUTPUT** 📋\n"
                "*** Settings ***\n"
                "Library    Browser\n"
                "Library    BuiltIn\n\n"
                "*** Variables ***\n"
                "${browser}    chromium\n\n"
                "*** Test Cases ***\n"
                "Generated Test\n"
                "    New Browser    ${browser}\n"
                "    Close Browser\n\n"
                
                "❌ **EXAMPLE OF WRONG OUTPUT** ❌\n"
                "Now, I will assemble the code.*** Settings ***  ← WRONG! No text before ***\n"
                "**Variables:**  ← WRONG! No markdown headers\n"
                "From the first step: ...  ← WRONG! No explanations\n\n"
                
                "🎯 **REMEMBER** 🎯\n"
                "You are a CODE PRINTER. Your output is directly saved as a .robot file.\n"
                "If you include ANY text that is not valid Robot Framework syntax, the file will be broken.\n"
                "Think of yourself as a printer that can ONLY print code, nothing else.\n\n"
                
                "When you receive input, immediately output the code starting with *** Settings ***.\n"
                "Do NOT explain what you're doing. Do NOT think out loud. Just output the code.\n\n"
                "**DELEGATION HANDLING:**\n"
                "You may receive delegation requests from Code Validator with error details. "
                "When you receive a delegation request:\n"
                "1. Carefully review the error details and validation feedback provided\n"
                "2. Identify the specific issues in the previously generated code\n"
                "3. Regenerate the code with all corrections applied\n"
                "4. Focus on fixing the exact errors mentioned (syntax, keyword usage, variable assignments, etc.)\n"
                "5. Preserve all correct parts of the code while fixing only the problematic sections\n"
                "6. Ensure the regenerated code addresses every error point raised by the validator\n\n"
                "When processing delegation requests, prioritize:\n"
                "- Critical syntax errors that prevent code execution\n"
                "- Incorrect keyword usage for the target library\n"
                "- Missing variable assignments for keywords that return values\n"
                "- Proper indentation and formatting\n\n"
                "Your goal is to learn from validation feedback and produce corrected code that passes validation."
                f"{library_knowledge}"
            ),
            tools=[self.keyword_search_tool] if self.keyword_search_tool else [],
            llm=self.llm,
            verbose=True,
            allow_delegation=True,
        )

    def code_validator_agent(self) -> Agent:
        # Import settings to access MAX_AGENT_ITERATIONS
        from ..core.config import settings

        # Get context via unified method with consistent priority chain
        library_knowledge = self._get_agent_context("validator")

        # Build tools list - add keyword_search_tool if available
        # This allows validator to look up keyword details for validation
        tools = []
        if self.keyword_search_tool:
            tools.append(self.keyword_search_tool)
            logger.info("🔧 Validator has keyword_search_tool access for keyword verification")

        return Agent(
            role="Robot Framework Linter and Quality Assurance Engineer",
            goal=f"Validate Robot Framework code for {self.library_context.library_name if self.library_context else 'Robot Framework'} correctness. Delegate fixes if errors found.",
            backstory=(
                "Expert Robot Framework validator. Check: syntax, keyword usage, variable assignments, locator formats, test structure. "
                "Use keyword_search tool to verify keyword details (arguments, return values, syntax) when needed. "
                "If VALID: Return JSON {\"valid\": true, \"reason\": \"...\"}. "
                "If INVALID: Document errors with line numbers, then delegate to Code Assembly Agent with fix instructions."
                f"{library_knowledge}"
            ),
            tools=tools,
            llm=self.llm,
            verbose=True,
            allow_delegation=True,
            max_iter=settings.MAX_AGENT_ITERATIONS,
        )
