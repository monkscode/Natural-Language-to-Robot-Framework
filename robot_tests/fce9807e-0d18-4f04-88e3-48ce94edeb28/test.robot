*** Settings ***
Library    SeleniumLibrary

*** Test Cases ***
Test Case From Query: go to aqa.science and click the login button
    Open Browser    https://aqa.science  browser=chrome  options=add_argument("--headless");add_argument("--no-sandbox")
    Maximize Browser Window
    Click Element    css=button[type='submit']
    [Teardown]    Close Browser