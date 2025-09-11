from crewai import Task

class RobotTasks:
    def plan_steps_task(self, agent, query) -> Task:
        return Task(
            description=f"""
            Analyze the user's query and break it down into a structured series of high-level test steps.
            The user query is: "{query}"

            --- RULES ---
            1.  Respond with a JSON array of objects. Each object represents a single test step.
            2.  Each object must have keys: "step_description", "element_description", "value", and "keyword".
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
                "The context will be the output of the 'plan_steps_task'."
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
