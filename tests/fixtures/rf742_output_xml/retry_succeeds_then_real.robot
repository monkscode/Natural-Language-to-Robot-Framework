*** Variables ***
${N}    0

*** Test Cases ***
Retry Succeeds Then Real
    Wait Until Keyword Succeeds    3x    10ms    Flaky
    Fail    TimeoutError: locator.fill: Timeout 10000ms exceeded.\nCall log:\n${SPACE}${SPACE}-${SPACE}waiting for locator("#real3")

*** Keywords ***
Flaky
    ${n}=    Evaluate    ${N} + 1
    Set Suite Variable    ${N}    ${n}
    IF    ${n} < 2    Fail    Error: flaky attempt ${n}
