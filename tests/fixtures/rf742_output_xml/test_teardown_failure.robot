*** Test Cases ***
Test Teardown Fails
    Log    body ok
    [Teardown]    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#test-teardown")
