*** Test Cases ***
Silenced If Then Teardown
    Set Log Level    NONE
    IF    True
        Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#silenced")
    END
    [Teardown]    Fail    Error: teardown message
