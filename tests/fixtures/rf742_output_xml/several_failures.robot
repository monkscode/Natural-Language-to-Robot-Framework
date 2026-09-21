*** Test Cases ***
Several Failures
    Run Keyword And Continue On Failure    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#first")
    Fail    Error: second failure
