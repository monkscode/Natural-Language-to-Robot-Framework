*** Test Cases ***
Var Then Teardown
    VAR    ${x}    ${undefined_variable}
    [Teardown]    Fail    Error: teardown message
