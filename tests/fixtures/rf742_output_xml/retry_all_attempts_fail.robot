*** Test Cases ***
Retry All Attempts Fail
    Wait Until Keyword Succeeds    2x    10ms    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#retry")
