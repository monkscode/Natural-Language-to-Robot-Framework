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
            *   `Should Be True`: For validation assertions with conditions.

            --- HANDLING CONDITIONAL LOGIC ---
            For validation steps that require comparison (like price checks), structure the step as:
            *   Use `Get Text` to retrieve the value
            *   Use a separate validation step with `Should Be True` keyword
            *   Include the `condition_expression` key with the actual comparison logic
            
            Example for price validation:
            1. Get Text from price element -> store in variable
            2. Validate with Should Be True and condition_expression like "${{float(product_price.replace('₹', '').replace(',', '')) < 9999}}"

            --- HANDLING LOOPS ---
            If the user's query implies a loop (e.g., "for every link", "for each item"), you must structure the output JSON for that step with two additional keys: `loop_type` and `loop_source`.
            *   `loop_type`: Should be "FOR".
            *   `loop_source`: Should be the element that contains the items to loop over (e.g., "the main menu").

            --- EXAMPLE SCENARIOS ---

            **Example 1: Price Validation**
            *Query:* "Check if product price is less than 100"
            *Output Steps:*
            1. Get Text from the price element and store it in a variable named 'product_price'
            2. Validate that the price is less than 100 using Should Be True with condition_expression

            **Example 2: Conditional Action**
            *Query:* "Go to the cart, and if the total is over $100, apply the 'SAVE10' discount code."
            *Output Steps:*
            1. Go to the cart page
            2. Get Text from the total amount element and store it in a variable named 'total'
            3. Input Text into the discount code field with value 'SAVE10', with condition_type 'IF' and condition_value '${{float(total.replace("$", "")) > 100}}'

            --- FINAL OUTPUT RULES ---
            1.  You MUST respond with ONLY a valid JSON array of objects.
            2.  Each object in the array represents a single test step and MUST have the following keys: "step_description", "element_description", "value", and "keyword".
            3.  For validation steps, use "Should Be True" keyword with a "condition_expression" key.
            4.  The keys `condition_type`, `condition_value`, `loop_type`, and `loop_source` are OPTIONAL and should only be included for steps with conditional logic or loops.
            5.  If the query involves a web search (e.g., "search for X") but does not specify a URL, you MUST generate a first step to open a search engine. Use 'https://www.google.com' as the value for the URL.
            6.  When generating an "Open Browser" step, you MUST also include the `browser=chrome` argument and options to ensure a clean session. Use `options=add_argument("--headless");add_argument("--no-sandbox");add_argument("--incognito")`.
            7.  If the query says to input text in google search bar, then after entering the text, you MUST press Enter and not the search button.

            """,
            expected_output="A JSON array of objects, where each object represents a single test step with the keys: 'step_description', 'element_description', 'value', and 'keyword', and optional keys for conditions and loops.",
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
                "4.  Choose the most specific and robust locator possible based on the priority.\n"
                "5.  For validation steps using 'Should Be True', no locator is needed."
            ),
            expected_output="A JSON array of objects, where each object represents a single test step with the added 'locator' key.",
            agent=agent,
        )

    def assemble_code_task(self, agent) -> Task:
        return Task(
            description=(
                "Assemble the final Robot Framework code from the structured steps provided in the context. "
                "The context will be the output of the 'identify_elements_task'.\n\n"
                "--- CRITICAL RULES FOR VALIDATION ---\n"
                "When you encounter a step with keyword 'Should Be True' and a 'condition_expression' key:\n"
                "1. Generate a proper Should Be True statement with the expression\n"
                "2. The expression should be a valid Python expression that Robot Framework can evaluate\n"
                "3. Use proper Python string methods for text manipulation\n\n"
                "**Example for price validation:**\n"
                "*Input Step:*\n"
                "`{\"keyword\": \"Should Be True\", \"condition_expression\": \"${float(product_price.replace('₹', '').replace(',', '')) < 9999}\"}`\n"
                "*Output Code:*\n"
                "`    ${price_numeric}=    Evaluate    float('${product_price}'.replace('₹', '').replace(',', ''))`\n"
                "`    Should Be True    ${price_numeric} < 9999`\n\n"
                "--- HANDLING CONDITIONAL LOGIC ---\n"
                "If a step in the context contains the keys `condition_type` and `condition_value`, you MUST use the `Run Keyword If` keyword from Robot Framework's BuiltIn library. "
                "The format should be: `Run Keyword If    ${condition_value}    Keyword    argument1    argument2`\n\n"
                "**Example:**\n"
                "*Input Step:*\n"
                "`{\"keyword\": \"Input Text\", \"locator\": \"id=discount-code\", \"value\": \"SAVE10\", \"condition_type\": \"IF\", \"condition_value\": \"${total} > 100\"}`\n"
                "*Output Code:*\n"
                "`    Run Keyword If    ${total} > 100    Input Text    id=discount-code    SAVE10`\n\n"
                "--- HANDLING LOOPS ---\n"
                "If a step in the context contains the keys `loop_type` and `loop_source`, you MUST use a `FOR` loop. The `loop_source` will be a list of elements. You should iterate over this list and perform the specified action on each item.\n\n"
                "**Example:**\n"
                "*Input Step:*\n"
                "`{\"keyword\": \"Click Element\", \"loop_type\": \"FOR\", \"loop_source\": \"@{links}\"}`\n"
                "*Output Code:*\n"
                "`    FOR    ${link}    IN    @{links}`\n"
                "`        Click Element    ${link}`\n"
                "`    END`\n\n"
                "--- LIBRARIES TO INCLUDE ---\n"
                "Always include these libraries in the Settings section:\n"
                "- SeleniumLibrary (for web automation)\n"
                "- BuiltIn (for basic Robot Framework keywords like Should Be True, Evaluate)\n"
                "- String (if string manipulation is needed)\n\n"
                "--- CRITICAL RULES ---\n"
                "1. You MUST respond with ONLY the raw Robot Framework code.\n"
                "2. The code should start with '*** Settings ***' or '*** Test Cases ***'.\n"
                "3. Do NOT include any introductory text, natural language explanations, or markdown formatting like ``` or ```robotframework.\n"
                "4. For price or numeric validations, always use Evaluate to convert strings to numbers properly."
            ),
            expected_output="A raw string containing only the complete and syntactically correct Robot Framework code. The output MUST NOT contain any markdown fences or other explanatory text.",
            agent=agent,
        )

    def validate_code_task(self, agent) -> Task:
        return Task(
            description=(
                "Validate the generated Robot Framework code for correctness and adherence to critical rules. "
                "The context will be the output of the 'assemble_code_task'.\n\n"
                "--- VALIDATION CHECKLIST ---\n"
                "1. All required libraries are imported (SeleniumLibrary, BuiltIn, String if needed)\n"
                "2. All keywords have the correct number of arguments\n"
                "3. Variables are properly declared before use\n"
                "4. Should Be True statements have valid expressions\n"
                "5. Run Keyword If statements have proper syntax\n"
                "6. Price/numeric comparisons use proper conversion (Evaluate)\n\n"
                "--- COMMON ERRORS TO CHECK ---\n"
                "1. Get Text without locator argument\n"
                "2. Invalid expressions in Should Be True\n"
                "3. Missing variable assignments (${var}=)\n"
                "4. Incorrect conditional syntax\n\n"
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
