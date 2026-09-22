*** Test Cases ***
Silenced Var Then Teardown
    Set Log Level    NONE
    VAR    ${x}    ${undefined_variable}
    [Teardown]    Fail    Error: teardown message
