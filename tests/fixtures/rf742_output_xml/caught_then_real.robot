*** Test Cases ***
Caught Then Real
    Run Keyword And Ignore Error    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#caught")
    Fail    TimeoutError: locator.fill: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#real")
