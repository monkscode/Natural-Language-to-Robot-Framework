*** Settings ***
Suite Teardown    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#suite-teardown")

*** Test Cases ***
Body Passes
    Log    ok
