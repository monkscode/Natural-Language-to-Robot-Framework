*** Settings ***
Library    SeleniumLibrary

*** Test Cases ***
User Defined Test
    [Documentation]    Test case generated from user query
    Open Browser    https://www.google.com/search?q=Robot+Framework    browser=chrome
    Close Browser
