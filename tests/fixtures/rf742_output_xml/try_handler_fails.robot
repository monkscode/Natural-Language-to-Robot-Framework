*** Test Cases ***
Handler Fails
    TRY
        Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#in-try")
    EXCEPT    *    type=GLOB    AS    ${err}
        Fail    Could not click the button
    END
