*** Test Cases ***
User Keyword Wrapper
    Do The Click

*** Keywords ***
Do The Click
    Log    inside
    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#inner")
