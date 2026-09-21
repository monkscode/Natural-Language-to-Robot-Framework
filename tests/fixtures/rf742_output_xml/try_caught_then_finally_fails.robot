*** Test Cases ***
Caught Then Finally Fails
    TRY
        Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#in-try")
    EXCEPT    *    type=GLOB
        Log    handled
    FINALLY
        Fail    Error: cleanup in finally failed
    END
