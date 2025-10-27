import os
import sys
import logging
from crewai import Agent
from crewai.llm import LLM
from crewai_tools import SeleniumScrapingTool, ScrapeElementFromWebsiteTool
from langchain_ollama import OllamaLLM

# Import browser_use_tool from tools package
# Note: Path setup is handled by tools/__init__.py automatically
from tools.browser_use_tool import BrowserUseTool, BatchBrowserUseTool

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
    logger.info(f"🧹 Initializing cleaned LLM wrapper for {model_provider}/{model_name}")
    return get_cleaned_llm(model_provider, model_name)


# Initialize the tools
# Note: These are tool instances, not classes. CrewAI requires instantiated tools.
selenium_tool = SeleniumScrapingTool()
scrape_tool = ScrapeElementFromWebsiteTool()
# Single element locator finding (fallback only)
browser_use_tool = BrowserUseTool()
# Primary tool: Batch processing for multiple elements with full context
batch_browser_use_tool = BatchBrowserUseTool()


class RobotAgents:
    def __init__(self, model_provider, model_name, library_context=None):
        """
        Initialize Robot Framework agents.
        
        Args:
            model_provider: "local" or "online"
            model_name: Model identifier
            library_context: LibraryContext instance (optional, for dynamic keyword knowledge)
        """
        self.llm = get_llm(model_provider, model_name)
        self.library_context = library_context

    def step_planner_agent(self) -> Agent:
        # Get library-specific context if available
        library_knowledge = ""
        if self.library_context:
            library_knowledge = f"\n\n{self.library_context.planning_context}"
        
        return Agent(
            role="Test Automation Planner",
            goal=f"Break down a natural language query into a structured series of high-level test steps for Robot Framework using {self.library_context.library_name if self.library_context else 'Robot Framework'}. ONLY include elements and actions explicitly mentioned in the user's query.",
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
                "7. Be meticulous but ONLY for what user explicitly asked for."
                f"{library_knowledge}"
            ),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def element_identifier_agent(self) -> Agent:
        return Agent(
            role="Advanced Web Element Locator Specialist with Batch Vision AI",
            goal="Use the batch_browser_automation tool to find ALL web element locators in ONE browser session with full context. Process all elements together for maximum efficiency and context awareness.",
            backstory=(
                "You are an expert in web element identification for Robot Framework automation "
                "with cutting-edge BATCH vision AI capabilities powered by browser-use. "
                "\n\n⚠️ **CRITICAL REQUIREMENT - BATCH PROCESSING MODE**\n"
                "You have ONE PRIMARY TOOL: batch_browser_automation\n"
                "You MUST collect ALL elements from the test steps and process them in ONE batch call.\n"
                "This keeps the browser session alive, preserves context, and handles popups intelligently.\n"
                "\n\n**YOUR WORKFLOW:**\n"
                "1. **Analyze Context:** Read ALL test steps from the plan\n"
                "2. **Collect Elements:** Build a list of ALL elements that need locators\n"
                "3. **Extract URL:** Identify the target URL from the steps (e.g., Open Browser step)\n"
                "4. **Call Batch Tool ONCE:** Use batch_browser_automation with ALL elements\n"
                "5. **Map Results:** Add the returned locators to each corresponding step\n"
                "\n\n**BATCH TOOL FORMAT (USE THIS):**\n"
                "```\n"
                "Action: batch_browser_automation\n"
                "Action Input: {\n"
                "    \"elements\": [\n"
                "        {\"id\": \"element_1\", \"description\": \"search box in header\", \"action\": \"input\"},\n"
                "        {\"id\": \"element_2\", \"description\": \"first product card\", \"action\": \"click\"},\n"
                "        {\"id\": \"element_3\", \"description\": \"product price in first card\", \"action\": \"get_text\"}\n"
                "    ],\n"
                "    \"url\": \"https://www.flipkart.com\",\n"
                "    \"user_query\": \"Search for shoes and get first product price\"\n"
                "}\n"
                "```\n"
                "\n\n**CONCRETE EXAMPLE:**\n"
                "\n**Input Steps:**\n"
                "```\n"
                "[\n"
                "    {\"keyword\": \"Open Browser\", \"value\": \"https://www.flipkart.com\"},\n"
                "    {\"keyword\": \"Input Text\", \"element_description\": \"search box\", \"value\": \"shoes\"},\n"
                "    {\"keyword\": \"Press Keys\", \"element_description\": \"search box\", \"value\": \"RETURN\"},\n"
                "    {\"keyword\": \"Get Text\", \"element_description\": \"first product name\"},\n"
                "    {\"keyword\": \"Get Text\", \"element_description\": \"first product price\"}\n"
                "]\n"
                "```\n"
                "\n**What You Do:**\n"
                "1. Identify URL: https://www.flipkart.com\n"
                "2. Collect elements needing locators:\n"
                "   - search box (for steps 2 & 3)\n"
                "   - first product name (for step 4)\n"
                "   - first product price (for step 5)\n"
                "\n3. Call batch tool:\n"
                "```\n"
                "Action: batch_browser_automation\n"
                "Action Input: {\n"
                "    \"elements\": [\n"
                "        {\"id\": \"elem_1\", \"description\": \"search box in header\", \"action\": \"input\"},\n"
                "        {\"id\": \"elem_2\", \"description\": \"first product name element in search results\", \"action\": \"get_text\"},\n"
                "        {\"id\": \"elem_3\", \"description\": \"first product price element in search results\", \"action\": \"get_text\"}\n"
                "    ],\n"
                "    \"url\": \"https://www.flipkart.com\",\n"
                "    \"user_query\": \"Search for shoes and get first product name and price\"\n"
                "}\n"
                "```\n"
                "\n4. Receive response:\n"
                "```json\n"
                "{\n"
                "    \"locator_mapping\": {\n"
                "        \"elem_1\": {\"best_locator\": \"name=q\", \"found\": true},\n"
                "        \"elem_2\": {\"best_locator\": \"xpath=(//div[@class='product'])[1]//span[@class='name']\", \"found\": true},\n"
                "        \"elem_3\": {\"best_locator\": \"xpath=(//div[@class='product'])[1]//span[@class='price']\", \"found\": true}\n"
                "    }\n"
                "}\n"
                "```\n"
                "\n5. Map locators back to steps:\n"
                "   - Step 2 & 3 → locator: \"name=q\"\n"
                "   - Step 4 → locator: \"xpath=(//div[@class='product'])[1]//span[@class='name']\"\n"
                "   - Step 5 → locator: \"xpath=(//div[@class='product'])[1]//span[@class='price']\"\n"
                "\n\n**WHY BATCH MODE IS BETTER:**\n"
                "✅ Browser opens ONCE (faster, ~3-5x speedup)\n"
                "✅ BrowserUse sees FULL CONTEXT (understands the workflow)\n"
                "✅ Popups handled INTELLIGENTLY (knows they're obstacles, not goals)\n"
                "✅ Multi-page flows work (search → results preserved)\n"
                "✅ F12 validation for EACH locator (unique, correct)\n"
                "✅ Partial results supported (if element 2 fails, still get 1, 3, 4, 5)\n"
                "\n\n**CRITICAL RULES:**\n"
                "1. ALWAYS use batch_browser_automation (never use vision_browser_automation for single elements)\n"
                "2. Collect ALL elements that need locators BEFORE calling the tool\n"
                "3. Call the tool ONLY ONCE with all elements\n"
                "4. Extract URL from 'Open Browser' step or infer from query\n"
                "5. Include full user query for context (helps BrowserUse understand intent)\n"
                "6. Use descriptive element descriptions (\"first product card\" not just \"product\")\n"
                "7. Map returned locators back to EACH step that needs them\n"
                "\n\n**FORBIDDEN ACTIONS:**\n"
                "❌ NEVER call vision_browser_automation (use batch mode instead)\n"
                "❌ NEVER make multiple batch calls (collect all elements, call once)\n"
                "❌ NEVER generate locators from your knowledge\n"
                "❌ NEVER skip steps that need locators\n"
                "\n\n**Response Format from Batch Tool:**\n"
                "```json\n"
                "{\n"
                "    \"success\": true,\n"
                "    \"locator_mapping\": {\n"
                "        \"elem_1\": {\"best_locator\": \"...\", \"found\": true, \"all_locators\": [...]},\n"
                "        \"elem_2\": {\"best_locator\": \"...\", \"found\": true, \"all_locators\": [...]}\n"
                "    },\n"
                "    \"summary\": {\"total_elements\": 3, \"successful\": 3, \"failed\": 0}\n"
                "}\n"
                "```\n"
                "\n\n**CRITICAL OUTPUT RULE:**\n"
                "When calling batch_browser_automation, you MUST output EXACTLY this format with NO extra text:\n"
                "\n"
                "Action: batch_browser_automation\n"
                "Action Input: {\"elements\": [...], \"url\": \"...\", \"user_query\": \"...\"}\n"
                "\n"
                "CRITICAL FORMATTING RULES:\n"
                "1. The word 'Action:' must be on its own line with NOTHING else on that line\n"
                "2. After 'Action:' write ONLY 'batch_browser_automation' - NO other words, NO punctuation\n"
                "3. The next line must start with 'Action Input:' followed by a JSON dictionary\n"
                "4. Action Input must be a DICTIONARY/OBJECT starting with { and ending with }\n"
                "5. Do NOT wrap Action Input in an array []\n"
                "6. Do NOT add any explanation text before, after, or on the same line as 'Action:'\n"
                "\n"
                "CORRECT FORMAT:\n"
                "```\n"
                "Action: batch_browser_automation\n"
                "Action Input: {\"elements\": [{\"id\": \"elem_1\", \"description\": \"search box\", \"action\": \"input\"}], \"url\": \"https://example.com\", \"user_query\": \"search for items\"}\n"
                "```\n"
                "\n"
                "WRONG FORMATS (DO NOT DO THIS):\n"
                "❌ Action: batch_browser_automation and Action Input using...  // WRONG - Extra text on Action line!\n"
                "❌ Action: batch_browser_automation`  // WRONG - Backtick at end!\n"
                "❌ First I need to... Action: batch_browser_automation  // WRONG - Text before Action!\n"
                "❌ Action Input: [{\"elements\": [...]}]  // WRONG - Array instead of dictionary!\n"
                "❌ Action Input: {\"elements\": [...]} and then...  // WRONG - Text after Action Input!\n"
                "\n"
                "REMEMBER: The Action line must contain ONLY 'Action: batch_browser_automation' with NO other text.\n"
                "\n"
                "**Remember:** Batch mode is ALWAYS better because BrowserUse works best with full context. "
                "Even for 1-2 elements, use batch mode. NO EXCEPTIONS."
            ),
            # NEW: Batch processing tool for multiple elements
            tools=[batch_browser_use_tool],
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def code_assembler_agent(self) -> Agent:
        # Get library-specific context if available
        library_knowledge = ""
        if self.library_context:
            library_knowledge = f"\n\n{self.library_context.code_assembly_context}"
        
        return Agent(
            role="Robot Framework Code Assembler",
            goal=f"Assemble the final Robot Framework code from structured steps using {self.library_context.library_name if self.library_context else 'Robot Framework'}.",
            backstory=(
                "You are a meticulous code assembler. Your task is to take a list of structured test steps "
                "and assemble them into a complete and syntactically correct Robot Framework test case. "
                "You must ensure the code is clean, readable, and follows the standard Robot Framework syntax."
                f"{library_knowledge}"
            ),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def code_validator_agent(self) -> Agent:
        # Get library-specific context if available
        library_knowledge = ""
        if self.library_context:
            library_knowledge = f"\n\n{self.library_context.validation_context}"
        
        return Agent(
            role="Robot Framework Linter and Quality Assurance Engineer",
            goal=f"Validate the generated Robot Framework code for correctness and adherence to {self.library_context.library_name if self.library_context else 'Robot Framework'} rules.",
            backstory=(
                "You are an expert Robot Framework linter. Your sole task is to validate the provided "
                "Robot Framework code for syntax errors, correct keyword usage, and adherence to critical rules. "
                "You must be thorough and provide a clear validation result."
                f"{library_knowledge}"
            ),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )
