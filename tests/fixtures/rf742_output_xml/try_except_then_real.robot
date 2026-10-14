*** Test Cases ***
Try Except Then Real
    TRY
        Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#in-try")
    EXCEPT    *    type=GLOB
        Log    handled
    END
    Fail    TimeoutError: locator.fill: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#after-try")
