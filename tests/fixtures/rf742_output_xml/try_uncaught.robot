*** Test Cases ***
Uncaught Try
    TRY
        Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#uncaught")
    EXCEPT    Something else entirely
        Log    never
    END
