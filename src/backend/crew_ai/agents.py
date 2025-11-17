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
                 assembler_knowledge_source=None, validator_knowledge_source=None,
                 embedder_config=None):
        """
        Initialize Robot Framework agents.

        Args:
            model_provider: "local" or "online"
            model_name: Model identifier
            library_context: LibraryContext instance (optional, for dynamic keyword knowledge)
            assembler_knowledge_source: StringKnowledgeSource for code assembler agent
            validator_knowledge_source: StringKnowledgeSource for code validator agent
            embedder_config: EmbedderConfig instance for knowledge base embeddings
        """
        self.llm = get_llm(model_provider, model_name)
        self.library_context = library_context
        self.assembler_knowledge_source = assembler_knowledge_source
        self.validator_knowledge_source = validator_knowledge_source
        self.embedder_config = embedder_config

    def step_planner_agent(self) -> Agent:
        """Step Planner agent - no library knowledge needed as per user requirements."""
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
        """Code Assembler agent with RAG-based knowledge retrieval."""
        from crewai.knowledge.knowledge_config import KnowledgeConfig
        
        # Configure knowledge retrieval settings
        knowledge_config = KnowledgeConfig(
            results_limit=10,      # Top 10 most relevant keyword chunks
            score_threshold=0.35   # Minimum relevance score
        )
        
        # Prepare agent configuration
        agent_kwargs = {
            "role": "Robot Framework Code Generator (Output ONLY Code)",
            "goal": f"Generate ONLY raw Robot Framework code using {self.library_context.library_name if self.library_context else 'Robot Framework'}. NO explanations, NO thinking process, ONLY code.",
            "backstory": (
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
            ),
            "llm": self.llm,
            "verbose": True,
            "allow_delegation": True,
        }
        
        # Add knowledge source if available
        if self.assembler_knowledge_source:
            agent_kwargs["knowledge_sources"] = [self.assembler_knowledge_source]
            agent_kwargs["knowledge_config"] = knowledge_config
            
            # Add embedder configuration if available
            if self.embedder_config:
                agent_kwargs["embedder"] = self.embedder_config.to_crewai_config()
        
        return Agent(**agent_kwargs)

    def code_validator_agent(self) -> Agent:
        """Code Validator agent with RAG-based knowledge retrieval."""
        # Import settings to access MAX_AGENT_ITERATIONS
        from ..core.config import settings
        from crewai.knowledge.knowledge_config import KnowledgeConfig
        
        # Configure knowledge retrieval settings
        knowledge_config = KnowledgeConfig(
            results_limit=10,      # Top 10 most relevant validation rules
            score_threshold=0.35   # Minimum relevance score
        )
        
        # Prepare agent configuration
        agent_kwargs = {
            "role": "Robot Framework Linter and Quality Assurance Engineer",
            "goal": f"Validate the generated Robot Framework code for correctness and adherence to {self.library_context.library_name if self.library_context else 'Robot Framework'} rules, and delegate fixes to Code Assembly Agent if errors are found.",
            "backstory": (
                "You are an expert Robot Framework linter. Your sole task is to validate the provided "
                "Robot Framework code for syntax errors, correct keyword usage, and adherence to critical rules. "
                "You must be thorough and provide a clear validation result.\n\n"
                "**DELEGATION WORKFLOW:**\n"
                "When you find errors in the code, you MUST follow this workflow:\n"
                "1. Identify and document all syntax errors, incorrect keyword usage, and rule violations\n"
                "2. Create a detailed fix request with:\n"
                "   - Specific line numbers where errors occur\n"
                "   - Clear description of each error\n"
                "   - Examples of correct syntax for each issue\n"
                "   - Relevant Robot Framework rules being violated\n"
                "3. Delegate the fix request to the Code Assembly Agent with clear, actionable instructions\n"
                "4. The Code Assembly Agent will regenerate the code incorporating your feedback\n"
                "5. You will then validate the regenerated code and repeat if necessary\n\n"
                "**CRITICAL DELEGATION INSTRUCTIONS:**\n"
                "When you find errors, create a detailed fix request and delegate to Code Assembly Agent.\n"
                "Your delegation message should include:\n"
                "- A summary of all errors found\n"
                "- Specific corrections needed for each error\n"
                "- Code examples showing the correct implementation\n"
                "- Priority ranking if multiple errors exist (fix critical syntax errors first)\n\n"
                "**VALIDATION CRITERIA:**\n"
                "- Syntax correctness (indentation, spacing, structure)\n"
                "- Correct keyword usage for the target library\n"
                "- Proper variable assignments for keywords that return values\n"
                "- Valid locator formats\n"
                "- Correct test case structure\n\n"
                "If the code is valid, clearly state 'VALID' and provide a brief summary. "
                "If errors are found, delegate to Code Assembly Agent with detailed correction instructions.\n\n"
                "**ITERATION LIMIT IMPORTANT:**\n"
                f"Maximum iterations are controlled by MAX_AGENT_ITERATIONS={settings.MAX_AGENT_ITERATIONS}. "
                "Make each validation thorough to minimize iterations needed."
            ),
            "llm": self.llm,
            "verbose": True,
            "allow_delegation": True,
        }
        
        # Add knowledge source if available
        if self.validator_knowledge_source:
            agent_kwargs["knowledge_sources"] = [self.validator_knowledge_source]
            agent_kwargs["knowledge_config"] = knowledge_config
            
            # Add embedder configuration if available
            if self.embedder_config:
                agent_kwargs["embedder"] = self.embedder_config.to_crewai_config()
        
        return Agent(**agent_kwargs)
