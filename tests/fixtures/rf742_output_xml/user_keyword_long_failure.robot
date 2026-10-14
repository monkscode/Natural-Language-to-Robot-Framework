*** Test Cases ***
User Keyword Long Failure
    Click Through Wrapper

*** Keywords ***
Click Through Wrapper
    ${tail}=    Evaluate    "x" * 4500
    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#wrapped")\n${SPACE}${SPACE}-${SPACE}${tail}
