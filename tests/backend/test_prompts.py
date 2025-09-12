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

# For loop test
LOOP_PLAN_STEPS_MOCKED_OUTPUT = """
[
    {
        "step_description": "Get Webelements from the main menu and store them in a variable named 'links'",
        "element_description": "the main menu",
        "value": "",
        "keyword": "Get Webelements"
    },
    {
        "step_description": "For each 'link' in 'links', click the 'link'",
        "element_description": "the link",
        "value": "",
        "keyword": "Click Element",
        "loop_type": "FOR",
        "loop_source": "@{links}"
    }
]
"""

# For conditional logic test
CONDITIONAL_PLAN_STEPS_MOCKED_OUTPUT = """
[
    {
        "step_description": "Go to the cart page",
        "element_description": "the cart page link",
        "value": "https://example.com/cart",
        "keyword": "Go To"
    },
    {
        "step_description": "Get Text from the total amount element and store it in a variable named 'total'",
        "element_description": "the total amount element",
        "value": "",
        "keyword": "Get Text"
    },
    {
        "step_description": "Input Text into the discount code field with value 'SAVE10'",
        "element_description": "the discount code field",
        "value": "SAVE10",
        "keyword": "Input Text",
        "condition_type": "IF",
        "condition_value": "${{total}} > 100"
    }
]
"""
