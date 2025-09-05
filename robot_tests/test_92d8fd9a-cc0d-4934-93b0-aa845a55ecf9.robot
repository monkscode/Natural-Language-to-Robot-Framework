*** Settings ***
Library    SeleniumLibrary

*** Test Cases ***
Test Case From Query: go to youtube.com, search for "funny cat videos", and click the first video
    Open Browser    https://www.youtube.com    chrome
    Input Text    name=q    funny cat videos
    Click Element    css=button[aria-label='Search']
    Click Element
    [Teardown]    Close Browser