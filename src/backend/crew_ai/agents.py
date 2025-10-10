import os
from crewai import Agent
from crewai.llm import LLM
# from langchain_community.llms import Ollama
from crewai_tools import SeleniumScrapingTool, ScrapeElementFromWebsiteTool
# Import browser_use_tool to ensure it's included in the package
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from tools.browser_use_tool import BrowserUseTool, BatchBrowserUseTool
from langchain_ollama import OllamaLLM

# Initialize the LLMs
def get_llm(model_provider, model_name):
    """
    Get LLM instance for the specified provider.
    
    Uses direct LLM calls without rate limiting wrappers. Rate limiting was removed
    during codebase cleanup as it added unnecessary complexity. Google Gemini API
    has generous rate limits (1500 RPM for gemini-2.5-flash) that are sufficient
    for our use case.
    
    Args:
        model_provider: "local" for Ollama, "online" for Gemini
        model_name: Model identifier (e.g., "llama3.1", "gemini-2.5-flash")
    
    Returns:
        LLM instance (OllamaLLM for local, LLM for online)
    """
    if model_provider == "local":
        return OllamaLLM(model=model_name)
    else:
        # Use direct LLM for online models (no rate limiting wrapper needed)
        return LLM(
            api_key=os.getenv("GEMINI_API_KEY"),
            model=f"{model_name}",
        )

# Initialize the tools
# Note: These are tool instances, not classes. CrewAI requires instantiated tools.
selenium_tool = SeleniumScrapingTool()
scrape_tool = ScrapeElementFromWebsiteTool()
browser_use_tool = BrowserUseTool()  # Single element locator finding (fallback only)
batch_browser_use_tool = BatchBrowserUseTool()  # Primary tool: Batch processing for multiple elements with full context

class RobotAgents:
    def __init__(self, model_provider, model_name):
        self.llm = get_llm(model_provider, model_name)

    def step_planner_agent(self) -> Agent:
        return Agent(
            role="Test Automation Planner",
            goal="Break down a natural language query into a structured series of high-level test steps for Robot Framework. ONLY include elements and actions explicitly mentioned in the user's query.",
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
                "When calling batch_browser_automation, output ONLY the Action and Action Input. NO explanations, NO thinking, NO text before or after.\n"
                "CORRECT FORMAT:\n"
                "```\n"
                "Action: batch_browser_automation\n"
                "Action Input: {your_json_here}\n"
                "```\n"
                "\n"
                "WRONG (DO NOT DO THIS):\n"
                "First I need to analyze... [explanation]\n"
                "Action: batch_browser_automation\n"
                "\n"
                "**Remember:** Batch mode is ALWAYS better because BrowserUse works best with full context. "
                "Even for 1-2 elements, use batch mode. NO EXCEPTIONS."
            ),
            tools=[batch_browser_use_tool],  # NEW: Batch processing tool for multiple elements
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def popup_strategy_agent(self) -> Agent:
        return Agent(
            role="Popup Interference Strategy Analyzer",
            goal="Analyze the user's query and website context to determine which popups/modals need dismissal and which can be ignored",
            backstory=(
                "You are an expert at understanding web automation task requirements and "
                "distinguishing between essential UI interactions and distractions. "
                "\n\nYour responsibility is to analyze the user's task and determine:\n"
                "1. Does this task require user login? (e.g., 'add to cart', 'checkout', 'view orders')\n"
                "2. Does this task require cookie consent? (e.g., form submission, account actions)\n"
                "3. Can this task be completed without dismissing certain popups?\n"
                "\n**EXAMPLES OF YOUR ANALYSIS:**\n"
                "\nExample 1: Search Task (NO LOGIN NEEDED)\n"
                "Query: 'Search for shoes on Flipkart and get the first product name'\n"
                "Analysis:\n"
                "- Task: Public search and data extraction\n"
                "- Login required: NO (search is public)\n"
                "- Cookie consent: NO (reading data only)\n"
                "- Strategy: Dismiss login popup if blocking, ignore newsletter\n"
                "\nExample 2: Cart Task (LOGIN NEEDED)\n"
                "Query: 'Add product to cart and proceed to checkout'\n"
                "Analysis:\n"
                "- Task: Requires user session\n"
                "- Login required: YES (cart requires account)\n"
                "- Cookie consent: YES (session management)\n"
                "- Strategy: DO NOT dismiss login popup, accept cookies\n"
                "\nExample 3: Price Comparison (NO INTERACTION NEEDED)\n"
                "Query: 'Compare prices of iPhone on Amazon and Flipkart'\n"
                "Analysis:\n"
                "- Task: Read-only data extraction\n"
                "- Login required: NO\n"
                "- Cookie consent: NO\n"
                "- Strategy: Dismiss all popups blocking content\n"
                "\n**YOUR OUTPUT FORMAT:**\n"
                "You must return ONLY a valid JSON object with these keys:\n"
                "```json\n"
                "{\n"
                "    \"task_requires_login\": false,\n"
                "    \"task_requires_cookies\": false,\n"
                "    \"dismiss_login_popup\": true,\n"
                "    \"dismiss_cookie_consent\": false,\n"
                "    \"dismiss_promotional_popups\": true,\n"
                "    \"reasoning\": \"User wants to search for shoes which is a public action. Login popup will block the search box, so it must be dismissed. Cookie consent is not needed for reading search results.\"\n"
                "}\n"
                "```\n"
                "\n**RULES:**\n"
                "- Be conservative: If unsure, set dismiss flags to true (better to dismiss than block user's task)\n"
                "- Focus on user's goal: 'Search' = public, 'Checkout' = requires login\n"
                "- Promotional popups: Always safe to dismiss (newsletters, offers, app installs)\n"
                "- Return ONLY valid JSON, no markdown, no explanation outside JSON\n"
            ),
            tools=[],  # This is an analysis agent, no tools needed
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def code_assembler_agent(self) -> Agent:
        return Agent(
            role="Robot Framework Code Assembler",
            goal="Assemble the final Robot Framework code from structured steps.",
            backstory="You are a meticulous code assembler. Your task is to take a list of structured test steps and assemble them into a complete and syntactically correct Robot Framework test case. You must ensure the code is clean, readable, and follows the standard Robot Framework syntax.",
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def code_validator_agent(self) -> Agent:
        return Agent(
            role="Robot Framework Linter and Quality Assurance Engineer",
            goal="Validate the generated Robot Framework code for correctness and adherence to critical rules.",
            backstory="You are an expert Robot Framework linter. Your sole task is to validate the provided Robot Framework code for syntax errors, correct keyword usage, and adherence to critical rules. You must be thorough and provide a clear validation result.",
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )
