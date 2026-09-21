*** Test Cases ***
Expect Error Mismatch
    Run Keyword And Expect Error    Nothing like this*    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#mismatch")
