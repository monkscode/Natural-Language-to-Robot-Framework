*** Test Cases ***
Return Status Then Real
    ${ok}=    Run Keyword And Return Status    Fail    Error: caught by return status
    Run Keyword And Expect Error    *    Fail    Error: caught by expect error
    Fail    TimeoutError: locator.fill: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#real2")
