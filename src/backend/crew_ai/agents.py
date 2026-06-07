import logging
from crewai import Agent
# from crewai_tools import ScrapeElementFromWebsiteTool  # DEPRECATED - use batch tool instead

# Import browser_use_tool from tools package
# Note: Path setup is handled by tools/__init__.py automatically
from tools.browser_use_tool import BatchBrowserUseTool

# Import LLM factory function
from .cleaned_llm_wrapper import get_llm

logger = logging.getLogger(__name__)


# Initialize the tools
# Note: These are tool instances, not classes. CrewAI requires instantiated tools.
# scrape_tool = ScrapeElementFromWebsiteTool()  # DEPRECATED - use batch tool instead 
# Primary tool: Batch processing for multiple elements with full context
batch_browser_use_tool = BatchBrowserUseTool()


class RobotAgents:
    def __init__(self, model_provider, model_name, library_context=None,
                 keyword_search_tool=None,
                 planner_context=None,
                 assembler_context=None):
        """
        Initialize Robot Framework agents.

        Args:
            model_provider: "local", "gemini", or "vertex"
            model_name: Model identifier
            library_context: LibraryContext instance (optional, for dynamic keyword knowledge)
            keyword_search_tool: KeywordSearchTool instance (optional, added to code assembler tools)
            planner_context: Optimized context for Test Automation Planner (optional)
            assembler_context: Optimized context for Code Assembler (optional)
        """
        self.llm = get_llm(model_provider, model_name)
        self.library_context = library_context
        self.keyword_search_tool = keyword_search_tool

        # Role-specific optimized contexts
        self.planner_context = planner_context
        self.assembler_context = assembler_context

    def _get_agent_context(self, agent_type: str) -> str:
        """
        Unified context retrieval with consistent priority chain.
        
        Priority:
        1. Optimized context (from SmartKeywordProvider - pattern learning/zero-context+tool)
        2. Library context (static context from library_context)
        3. Empty string (graceful degradation)
        
        Args:
            agent_type: "planner" or "assembler"

        Returns:
            Context string with appropriate formatting
        """
        # Map agent type to optimized context attribute
        optimized_context_map = {
            "planner": self.planner_context,
            "assembler": self.assembler_context,
        }

        # Map agent type to library context property
        library_context_map = {
            "planner": "planning_context",
            "assembler": "code_assembly_context",
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
                
                "🔍 **KEYWORD SYNTAX LOOKUP (CRITICAL)** 🔍\n"
                "You have access to 'keyword_search' tool. USE IT BEFORE generating code when:\n"
                "- You see ANY keyword not in common list (New Browser, Click, Fill Text, Get Text)\n"
                "- Step value contains '=' pattern (e.g., 'something=value') - may need splitting\n"
                "- You're not 100% sure about argument count or order\n"
                "Pattern: If value is 'x=y', search the keyword first - tool will show if it needs\n"
                "separate args <x> <y> or combined 'x=y'. Follow the tool's argument structure exactly.\n\n"
                
                "When you receive input, immediately output the code starting with *** Settings ***.\n"
                "Do NOT explain what you're doing. Do NOT think out loud. Just output the code."
                f"{library_knowledge}"
            ),
            tools=[self.keyword_search_tool] if self.keyword_search_tool else [],
            llm=self.llm,
            verbose=True,
            # No in-crew delegation: the LLM validator agent was removed and replaced
            # by the deterministic robot --dryrun gate (dryrun_service.py). The repair
            # loop builds a fresh single-agent crew, so this agent never delegates.
            allow_delegation=False,
        )
