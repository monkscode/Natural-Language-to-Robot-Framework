from crewai import Task

class RobotTasks:
    def plan_steps_task(self, agent, query) -> Task:
        return Task(
            description=f"""
            Your mission is to act as an expert Test Automation Planner. You must analyze a user's natural language query and decompose it into a comprehensive, step-by-step test plan that a junior test engineer could follow.

            The user query is: "{query}"

            --- CORE PRINCIPLES ---
            1.  **Explicitness:** Your plan must be explicit. Do not assume any prior context. If a user says "log in", you must include steps for navigating to the login page, entering the username, entering the password, and clicking the submit button.
            2.  **Decomposition:** Break down complex actions into smaller, single-action steps. For example, "search for a product and add it to the cart" should be multiple steps: "Input text into search bar", "Click search button", "Click product link", "Click add to cart button".
            3.  **Keyword Precision:** Use the most appropriate Robot Framework keyword for each action.

            --- KEYWORD GUIDELINES ---
            *   `Open Browser`: For starting a new browser session.
            *   `Input Text`: For typing text into input fields.
            *   `Click Element`: For clicking buttons, links, etc.
            *   `Get Text`: For retrieving text from an element to be stored or validated.
            *   `Select From List By Value`: For selecting an option from a dropdown menu.
            *   `Wait Until Element Is Visible`: For waiting for dynamic content to appear.
            *   `Close Browser`: For ending the test session.

            --- EXAMPLE SCENARIOS ---

            **Example 1: Generic Login**
            *Query:* "Log in to the application with username 'myuser' and password 'mypassword'"
            *Output Steps:*
            1. Open Browser to the application's login page
            2. Input Text into the username or email field with value 'myuser'
            3. Input Text into the password field with value 'mypassword'
            4. Click Element on the 'Login' or 'Sign In' button

            **Example 2: Generic E-commerce Search**
            *Query:* "Go to the e-commerce site, search for 'product name', apply the 'high-rating' filter, and add the first item to the basket."
            *Output Steps:*
            1. Open Browser to the e-commerce site
            2. Input Text into the main search bar with value 'product name'
            3. Click Element on the search button
            4. Click Element on the filter for 'high-rating'
            5. Click Element on the first item in the search results
            6. Click Element on the 'Add to Basket' or 'Add to Cart' button

            --- FINAL OUTPUT RULES ---
            1.  You MUST respond with ONLY a valid JSON array of objects.
            2.  Each object in the array represents a single test step and MUST have the following keys: "step_description", "element_description", "value", and "keyword".
            3.  If the query involves a web search (e.g., "search for X") but does not specify a URL, you MUST generate a first step to open a search engine. Use 'https://www.google.com' as the value for the URL.
            4.  When generating an "Open Browser" step, you MUST also include the `browser=chrome` argument and options to ensure a clean session. Use `options=add_argument("--headless");add_argument("--no-sandbox");add_argument("--incognito")`.
            """,
            expected_output="A JSON array of objects, where each object represents a single test step with the keys: 'step_description', 'element_description', 'value', and 'keyword'.",
            agent=agent,
        )

    def identify_elements_task(self, agent) -> Task:
        return Task(
            description=(
                "For each step provided in the context, identify the best locator for the described element. "
                "The context will be the output of the 'plan_steps_task'.\n\n"
                "--- LOCATOR STRATEGY PRIORITY ---\n"
                "1.  **ID**: `id=element_id`\n"
                "2.  **Name**: `name=element_name`\n"
                "3.  **CSS Selector**: `css=css_selector` (e.g., `css=input[name='q']`, `css=button#submit`)\n"
                "4.  **XPath**: `xpath=//tag[@attribute='value']` (e.g., `xpath=//button[text()='Search']`)\n"
                "5.  **Link Text**: `link=Link Text`\n\n"
                "--- RULES ---\n"
                "1.  You MUST add a 'locator' key to each JSON object in the array.\n"
                "2.  The value of the 'locator' key MUST be a valid Robot Framework locator string.\n"
                "3.  If no specific element is described, the locator can be an empty string `''`.\n"
                "4.  Choose the most specific and robust locator possible based on the priority."
            ),
            expected_output="A JSON array of objects, where each object represents a single test step with the added 'locator' key.",
            agent=agent,
        )

    def assemble_code_task(self, agent) -> Task:
        return Task(
            description=(
                "Assemble the final Robot Framework code from the structured steps provided in the context. "
                "The context will be the output of the 'identify_elements_task'.\n\n"
                "--- CRITICAL RULES ---\n"
                "1. You MUST respond with ONLY the raw Robot Framework code.\n"
                "2. The code should start with '*** Settings ***' or '*** Test Cases ***'.\n"
                "3. Do NOT include any introductory text, natural language explanations, or markdown formatting like ``` or ```robotframework."
            ),
            expected_output="A raw string containing only the complete and syntactically correct Robot Framework code. The output MUST NOT contain any markdown fences or other explanatory text.",
            agent=agent,
        )

    def validate_code_task(self, agent) -> Task:
        return Task(
            description=(
                "Validate the generated Robot Framework code for correctness and adherence to critical rules. "
                "The context will be the output of the 'assemble_code_task'.\n\n"
                "--- CRITICAL RULES ---\n"
                "1. You MUST respond with ONLY a single, valid JSON object.\n"
                "2. Do NOT include any introductory text, natural language explanations, or markdown formatting like ```json.\n"
                "3. The JSON object must have exactly two keys: 'valid' (a boolean) and 'reason' (a string).\n"
                "4. If the code is valid, set 'valid' to true and 'reason' to 'The code is valid.'.\n"
                "5. If the code is invalid, set 'valid' to false and provide a brief, clear explanation in the 'reason' key."
            ),
            expected_output="A single, raw JSON object with two keys: 'valid' (boolean) and 'reason' (string). For example: {\"valid\": true, \"reason\": \"The code is valid.\"}",
            agent=agent,
        )
