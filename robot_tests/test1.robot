*** Settings ***
Library    SeleniumLibrary

*** Test Cases ***
User Defined Test
    [Documentation]    Test case generated from user query
    Open Browser    https://www.google.com/search?q=Robot+Framework    browser=chrome    options=add_argument('--no-sandbox');add_argument('--disable-dev-shm-usage')
    Close Browser
