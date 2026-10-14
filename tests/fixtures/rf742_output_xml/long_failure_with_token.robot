*** Test Cases ***
Long Failure With Token
    ${tail}=    Evaluate    "x" * 3000
    Fail    Error: page.goto: net::ERR_ABORTED at https://example.com/app?token=SECRETVALUE123\nCall log:\n${SPACE}${SPACE}-${SPACE}navigating to "https://example.com/app?token=SECRETVALUE123", waiting until "load"\n${SPACE}${SPACE}-${SPACE}${tail}
