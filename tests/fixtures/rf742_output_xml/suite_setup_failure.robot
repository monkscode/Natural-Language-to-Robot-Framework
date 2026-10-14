*** Settings ***
Suite Setup    Fail    TimeoutError: locator.click: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#suite-setup")

*** Test Cases ***
Under Failed Suite Setup
    Log    never
