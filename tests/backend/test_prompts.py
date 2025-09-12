# This file contains the mocked prompts and return values for the tests.

# For test_prompt_engineering.py
IDENTIFY_ELEMENTS_TASK_MOCKED_OUTPUT = "{\"locator\": \"xpath=//button[contains(text(), 'Submit')]\"}"

# For test_planner_prompt.py
PLAN_STEPS_TASK_MOCKED_OUTPUT = """
[
    {
        "step_description": "Open Browser to https://github.com/login",
        "element_description": "",
        "value": "https://github.com/login",
        "keyword": "Open Browser"
    },
    {
        "step_description": "Input Text into the username field with value 'testuser'",
        "element_description": "the username field",
        "value": "testuser",
        "keyword": "Input Text"
    },
    {
        "step_description": "Input Text into the password field with value 'password123'",
        "element_description": "the password field",
        "value": "password123",
        "keyword": "Input Text"
    },
    {
        "step_description": "Click Element on the 'Sign in' button",
        "element_description": "the 'Sign in' button",
        "value": "",
        "keyword": "Click Element"
    }
]
"""
