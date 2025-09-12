# This file contains the mocked prompts and return values for the tests.

# For test_prompt_engineering.py
IDENTIFY_ELEMENTS_TASK_MOCKED_OUTPUT = "{\"locator\": \"xpath=//button[contains(text(), 'Submit')]\"}"

# For test_planner_prompt.py
PLAN_STEPS_TASK_MOCKED_OUTPUT = """
[
    {
        "step_description": "Open Browser to the application's login page",
        "element_description": "the application's login page",
        "value": "https://example.com/login",
        "keyword": "Open Browser"
    },
    {
        "step_description": "Input Text into the username or email field with value 'myuser'",
        "element_description": "the username or email field",
        "value": "myuser",
        "keyword": "Input Text"
    },
    {
        "step_description": "Input Text into the password field with value 'mypassword'",
        "element_description": "the password field",
        "value": "mypassword",
        "keyword": "Input Text"
    },
    {
        "step_description": "Click Element on the 'Login' or 'Sign In' button",
        "element_description": "the 'Login' or 'Sign In' button",
        "value": "",
        "keyword": "Click Element"
    }
]
"""
