*** Test Cases ***
Test Setup Fails
    [Setup]    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#test-setup")
    Log    never
