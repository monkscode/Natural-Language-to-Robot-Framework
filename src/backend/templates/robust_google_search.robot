*** Settings ***
Library    SeleniumLibrary
Library    Collections

*** Variables ***
${SEARCH_TIMEOUT}    15s
${GOOGLE_URL}        https://www.google.com

*** Test Cases ***
Robust Google Search
    [Documentation]    Robust Google search with multiple fallback strategies
    [Tags]    google    search    robust
    Open Browser With Retry    ${GOOGLE_URL}
    Handle Cookie Consent
    Perform Search With Fallbacks    dhruvil vyas
    Verify Search Results
    Close Browser

*** Keywords ***
Open Browser With Retry
    [Arguments]    ${url}
    [Documentation]    Open browser with retry logic and proper Chrome options
    FOR    ${i}    IN RANGE    3
        TRY
            Open Browser    ${url}    chrome    
            ...    options=add_argument("--headless");add_argument("--no-sandbox");add_argument("--disable-dev-shm-usage");add_argument("--disable-gpu");add_argument("--window-size=1920,1080");add_argument("--disable-blink-features=AutomationControlled");add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            Wait Until Page Contains Element    css=input[name="q"]    timeout=${SEARCH_TIMEOUT}
            Log    Successfully opened browser on attempt ${i+1}
            BREAK
        EXCEPT    AS    ${error}
            Log    Browser open attempt ${i+1} failed: ${error}
            Run Keyword If    ${i} < 2    Sleep    2s
            Run Keyword If    ${i} == 2    Fail    Failed to open Google after 3 attempts: ${error}
        END
    END

Handle Cookie Consent
    [Documentation]    Handle Google's cookie consent dialog if present
    ${consent_buttons}=    Create List    
    ...    css=button[id*="accept"]
    ...    css=button[id*="Accept"]  
    ...    css=button:contains("Accept")
    ...    css=button:contains("I agree")
    ...    xpath=//button[contains(text(), "Accept")]
    ...    xpath=//button[contains(text(), "I agree")]
    
    FOR    ${selector}    IN    @{consent_buttons}
        ${consent_present}=    Run Keyword And Return Status    
        ...    Wait Until Element Is Visible    ${selector}    timeout=3s
        IF    ${consent_present}
            Log    Found consent button with selector: ${selector}
            Click Element    ${selector}
            Sleep    1s
            BREAK
        END
    END

Perform Search With Fallbacks
    [Arguments]    ${search_term}
    [Documentation]    Perform search with multiple fallback strategies
    
    # Strategy 1: Standard search input and button click with explicit waits
    TRY
        Log    Trying Strategy 1: Standard button click with waits
        Wait Until Element Is Visible    name=q    timeout=${SEARCH_TIMEOUT}
        Clear Element Text    name=q
        Input Text    name=q    ${search_term}
        
        # Wait for search suggestions to appear (indicates input is working)
        Sleep    1s
        
        # Try to wait for button to be enabled
        Wait Until Element Is Enabled    name=btnK    timeout=5s
        Click Element    name=btnK
        
        # Wait for results
        Wait Until Page Contains Element    css=h3, css=[data-header-feature="0"]    timeout=${SEARCH_TIMEOUT}
        Log    Strategy 1 succeeded: Button click worked
        RETURN
    EXCEPT    AS    ${error}
        Log    Strategy 1 failed: ${error}
    END
    
    # Strategy 2: Press Enter instead of clicking button
    TRY
        Log    Trying Strategy 2: Press Enter key
        Wait Until Element Is Visible    name=q    timeout=${SEARCH_TIMEOUT}
        Clear Element Text    name=q
        Input Text    name=q    ${search_term}
        Press Keys    name=q    RETURN
        
        Wait Until Page Contains Element    css=h3, css=[data-header-feature="0"]    timeout=${SEARCH_TIMEOUT}
        Log    Strategy 2 succeeded: Enter key worked
        RETURN
    EXCEPT    AS    ${error}
        Log    Strategy 2 failed: ${error}
    END
    
    # Strategy 3: Use CSS selector for search button
    TRY
        Log    Trying Strategy 3: CSS selector for button
        Wait Until Element Is Visible    name=q    timeout=${SEARCH_TIMEOUT}
        Clear Element Text    name=q
        Input Text    name=q    ${search_term}
        
        ${button_selectors}=    Create List
        ...    css=input[name="btnK"]
        ...    css=button[name="btnK"]
        ...    css=input[value="Google Search"]
        ...    css=button[aria-label*="Search"]
        
        FOR    ${selector}    IN    @{button_selectors}
            ${button_found}=    Run Keyword And Return Status
            ...    Wait Until Element Is Enabled    ${selector}    timeout=3s
            IF    ${button_found}
                Click Element    ${selector}
                Wait Until Page Contains Element    css=h3, css=[data-header-feature="0"]    timeout=${SEARCH_TIMEOUT}
                Log    Strategy 3 succeeded with selector: ${selector}
                RETURN
            END
        END
        
        Fail    No search button found with any CSS selector
    EXCEPT    AS    ${error}
        Log    Strategy 3 failed: ${error}
    END
    
    # Strategy 4: JavaScript form submission
    TRY
        Log    Trying Strategy 4: JavaScript form submission
        Wait Until Element Is Visible    name=q    timeout=${SEARCH_TIMEOUT}
        Clear Element Text    name=q
        Input Text    name=q    ${search_term}
        
        # Try different JavaScript approaches
        ${js_commands}=    Create List
        ...    document.querySelector('form[role="search"]').submit();
        ...    document.querySelector('form').submit();
        ...    document.querySelector('input[name="q"]').form.submit();
        
        FOR    ${js_cmd}    IN    @{js_commands}
            TRY
                Execute JavaScript    ${js_cmd}
                Wait Until Page Contains Element    css=h3, css=[data-header-feature="0"]    timeout=${SEARCH_TIMEOUT}
                Log    Strategy 4 succeeded with JS: ${js_cmd}
                RETURN
            EXCEPT
                Log    JavaScript command failed: ${js_cmd}
                Continue For Loop
            END
        END
        
        Fail    All JavaScript submission methods failed
    EXCEPT    AS    ${error}
        Log    Strategy 4 failed: ${error}
    END
    
    # Strategy 5: Alternative search input methods
    TRY
        Log    Trying Strategy 5: Alternative input methods
        
        ${input_selectors}=    Create List
        ...    css=textarea[name="q"]
        ...    css=input[title*="Search"]
        ...    css=input[aria-label*="Search"]
        ...    xpath=//input[@name='q']
        
        FOR    ${selector}    IN    @{input_selectors}
            ${input_found}=    Run Keyword And Return Status
            ...    Wait Until Element Is Visible    ${selector}    timeout=3s
            IF    ${input_found}
                Clear Element Text    ${selector}
                Input Text    ${selector}    ${search_term}
                Press Keys    ${selector}    RETURN
                Wait Until Page Contains Element    css=h3, css=[data-header-feature="0"]    timeout=${SEARCH_TIMEOUT}
                Log    Strategy 5 succeeded with selector: ${selector}
                RETURN
            END
        END
        
        Fail    No alternative search input found
    EXCEPT    AS    ${error}
        Log    Strategy 5 failed: ${error}
    END
    
    # If all strategies fail, provide detailed error
    Fail    All search strategies failed. Google's interface may have changed significantly.

Verify Search Results
    [Documentation]    Verify that search results are displayed properly
    
    # Check for multiple possible result indicators
    ${result_selectors}=    Create List
    ...    css=h3
    ...    css=[data-header-feature="0"]
    ...    css=.g h3
    ...    css=[role="heading"]
    ...    xpath=//h3
    
    ${results_found}=    Set Variable    ${False}
    FOR    ${selector}    IN    @{result_selectors}
        ${elements_found}=    Run Keyword And Return Status
        ...    Wait Until Element Is Visible    ${selector}    timeout=5s
        IF    ${elements_found}
            ${result_count}=    Get Element Count    ${selector}
            IF    ${result_count} > 0
                Log    Found ${result_count} search results using selector: ${selector}
                ${results_found}=    Set Variable    ${True}
                BREAK
            END
        END
    END
    
    Should Be True    ${results_found}    No search results found with any selector
    
    # Additional verification - check page title contains search term
    ${title}=    Get Title
    Should Contain    ${title}    dhruvil vyas    Search term not found in page title
    
    Log    Search verification completed successfully