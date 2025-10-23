document.addEventListener('DOMContentLoaded', () => {
    const queryInput = document.getElementById('query-input');
    const generateBtn = document.getElementById('generate-btn');
    const robotCodeEl = document.getElementById('robot-code');
    const executionLogsEl = document.getElementById('execution-logs');
    const downloadBtn = document.getElementById('download-btn');
    const copyCodeBtn = document.getElementById('copy-code-btn');
    const codeLanguageLabel = document.getElementById('code-language');
    const statusBadge = document.getElementById('status-badge');
    const statusText = document.getElementById('status-text');

    let robotCodeContent = '';
    let executionStartTime = null;

    function updateStatus(status, text, persistent = false) {
        statusBadge.style.display = 'flex';
        statusBadge.className = `status-badge status-${status}`;
        statusText.textContent = text;

        // Add persistent class for final results
        if (persistent) {
            statusBadge.classList.add('status-persistent');
        } else {
            statusBadge.classList.remove('status-persistent');
        }
    }

    function hideStatus() {
        // Only hide if not persistent
        if (!statusBadge.classList.contains('status-persistent')) {
            setTimeout(() => {
                statusBadge.style.display = 'none';
            }, 5000);
        }
    }

    function setButtonLoading(loading) {
        const btnText = generateBtn.querySelector('.btn-text');
        const btnSpinner = generateBtn.querySelector('.btn-spinner');

        if (loading) {
            generateBtn.classList.add('btn-loading');
            btnSpinner.style.display = 'block';
            generateBtn.disabled = true;
        } else {
            generateBtn.classList.remove('btn-loading');
            btnSpinner.style.display = 'none';
            generateBtn.disabled = false;
        }
    }

    // Auto-resize textarea
    queryInput.addEventListener('input', function () {
        this.style.height = 'auto';
        this.style.height = Math.max(150, this.scrollHeight) + 'px';
    });

    generateBtn.addEventListener('click', async () => {
        const query = queryInput.value.trim();
        if (!query) {
            queryInput.focus();
            queryInput.style.borderColor = 'var(--error)';
            setTimeout(() => {
                queryInput.style.borderColor = '';
            }, 2000);
            return;
        }

        setButtonLoading(true);
        robotCodeEl.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⚡</div><p>Generated Robot Framework code will appear here</p></div>';
        executionLogsEl.innerHTML = ''; // Clear previous logs
        downloadBtn.style.display = 'none';
        copyCodeBtn.style.display = 'none';
        codeLanguageLabel.style.display = 'none';
        robotCodeContent = '';

        // Clear previous persistent status when starting new test
        statusBadge.classList.remove('status-persistent');
        statusBadge.style.display = 'none';

        // Track execution start time
        executionStartTime = Date.now();

        try {
            const requestPayload = {
                query: query,
                model: "gemini-1.5-pro-latest"
            };

            const response = await fetch('/generate-and-run', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream'
                },
                body: JSON.stringify(requestPayload),
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                const chunk = decoder.decode(value);
                const lines = chunk.split('\n\n');

                for (const line of lines) {
                    if (line.startsWith('data:')) {
                        const jsonData = line.substring(5);
                        if (jsonData.trim()) {
                            try {
                                const data = JSON.parse(jsonData);
                                handleStreamedData(data);
                            } catch (e) {
                                console.error('Failed to parse JSON from stream:', jsonData);
                            }
                        }
                    }
                }
            }
        } catch (error) {
            updateStatus('error', 'Request failed');
            executionLogsEl.textContent = `Error: ${error.message}`;
            hideStatus();
        } finally {
            setButtonLoading(false);
        }
    });

    function handleStreamedData(data) {
        updateStatus('processing', data.message);

        if (data.stage === 'generation') {
            // Display running logs for generation stage
            if (data.status === 'running') {
                const logEntry = document.createElement('div');
                logEntry.textContent = `[${new Date().toLocaleTimeString()}] ${data.message}`;
                executionLogsEl.appendChild(logEntry);
                executionLogsEl.scrollTop = executionLogsEl.scrollHeight; // Auto-scroll
            } else if (data.status === 'complete' && data.robot_code) {
                robotCodeContent = data.robot_code;

                // Apply syntax highlighting
                applySyntaxHighlighting(robotCodeContent);

                // Show copy button and language label
                copyCodeBtn.style.display = 'flex';
                codeLanguageLabel.style.display = 'block';
                downloadBtn.style.display = 'inline-flex';

                // Clear generation logs and prepare for execution logs
                executionLogsEl.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>Test execution logs will appear here</p></div>';
            } else if (data.status === 'error') {
                updateStatus('error', 'Generation failed');
                robotCodeEl.innerHTML = `<div class="empty-state"><div class="empty-state-icon">⚠️</div><p>${data.message}</p></div>`;
                const errorEntry = document.createElement('div');
                errorEntry.style.color = 'var(--error)';
                errorEntry.textContent = `[${new Date().toLocaleTimeString()}] ERROR: ${data.message}`;
                executionLogsEl.appendChild(errorEntry);
                hideStatus();
            }
        } else if (data.stage === 'execution') {
            // When execution starts, clear the placeholder and show the first real log
            if (executionLogsEl.querySelector('.empty-state')) {
                executionLogsEl.innerHTML = '';
            }
            if (data.status === 'running') {
                const logEntry = document.createElement('div');
                logEntry.textContent = `[${new Date().toLocaleTimeString()}] ${data.message}`;
                executionLogsEl.appendChild(logEntry);
                executionLogsEl.scrollTop = executionLogsEl.scrollHeight;
            } else if (data.status === 'complete' && data.result) {
                const logs = data.result.logs || 'No execution logs available';
                executionLogsEl.textContent = logs; // Replace with final, full logs

                // Calculate execution time
                const executionTime = executionStartTime ? ((Date.now() - executionStartTime) / 1000).toFixed(1) : null;
                const timeText = executionTime ? ` (${executionTime}s)` : '';

                // PRIORITY 1: Use explicit test_status from backend (most reliable)
                if (data.test_status === 'passed') {
                    updateStatus('success', `Test passed${timeText}`, true); // persistent = true
                } else if (data.test_status === 'failed') {
                    updateStatus('error', 'Test failed', true); // No time for failures
                }
                // PRIORITY 2: Fallback to improved log parsing (for backward compatibility)
                else {
                    const logsUpper = logs.toUpperCase();

                    // Pattern 1: Check for "X passed, 0 failed" (success)
                    const resultsMatch = logsUpper.match(/(\d+)\s+PASSED,\s+(\d+)\s+FAILED/);
                    // Calculate execution time
                    const executionTime = executionStartTime ? ((Date.now() - executionStartTime) / 1000).toFixed(1) : null;
                    const timeText = executionTime ? ` (${executionTime}s)` : '';

                    if (resultsMatch) {
                        const failedCount = parseInt(resultsMatch[2]);
                        if (failedCount === 0) {
                            updateStatus('success', `Test passed${timeText}`, true); // persistent
                        } else {
                            updateStatus('error', 'Test failed', true); // No time for failures
                        }
                    }
                    // Pattern 2: Check for "All tests passed" message
                    else if (logsUpper.includes('ALL TESTS PASSED')) {
                        updateStatus('success', `Test passed${timeText}`, true); // persistent
                    }
                    // Pattern 3: Check for individual test status lines
                    else if (logsUpper.includes('TEST:') && logsUpper.includes(' - PASS')) {
                        updateStatus('success', `Test passed${timeText}`, true); // persistent
                    }
                    else if (logsUpper.includes('TEST:') && logsUpper.includes(' - FAIL')) {
                        updateStatus('error', 'Test failed', true); // No time for failures
                    }
                    // Pattern 4: Check for error indicators
                    else if (logsUpper.includes('ERROR') || logsUpper.includes('EXCEPTION')) {
                        updateStatus('error', 'Test completed with errors', true); // No time for errors
                    }
                    // Default: If no clear failure indicators, assume success
                    else {
                        updateStatus('success', `Test completed${timeText}`, true); // persistent
                    }
                }

                hideStatus();
            } else if (data.status === 'error') {
                updateStatus('error', 'Execution failed');
                const errorEntry = document.createElement('div');
                errorEntry.style.color = 'var(--error)';
                errorEntry.textContent = `[${new Date().toLocaleTimeString()}] ERROR: ${data.message}`;
                executionLogsEl.appendChild(errorEntry);
                hideStatus();
            }
        }
    }

    // Copy code button handler
    copyCodeBtn.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(robotCodeContent);

            // Success feedback - change icon to checkmark
            const originalHTML = copyCodeBtn.innerHTML;
            copyCodeBtn.classList.add('copied');
            copyCodeBtn.innerHTML = `
                <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"></path>
                </svg>
            `;

            setTimeout(() => {
                copyCodeBtn.classList.remove('copied');
                copyCodeBtn.innerHTML = originalHTML;
            }, 2000);
        } catch (err) {
            console.error('Failed to copy:', err);
            alert('Failed to copy code to clipboard');
        }
    });

    // Download button handler
    downloadBtn.addEventListener('click', () => {
        const blob = new Blob([robotCodeContent], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'generated_test.robot';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        // Success feedback
        downloadBtn.classList.add('download-success');
        const originalHTML = downloadBtn.innerHTML;
        downloadBtn.innerHTML = `
            <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path>
            </svg>
            Downloaded
        `;

        setTimeout(() => {
            downloadBtn.classList.remove('download-success');
            downloadBtn.innerHTML = originalHTML;
        }, 2000);
    });

    // Keyboard shortcut
    queryInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            generateBtn.click();
        }
    });

    // Notification helper function
    function showNotification(message, type = 'info') {
        // Create notification element
        const notification = document.createElement('div');
        notification.className = `notification notification-${type}`;
        notification.textContent = message;

        // Add styles
        notification.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 1rem 1.5rem;
            border-radius: 8px;
            color: white;
            font-weight: 500;
            z-index: 1001;
            animation: slideIn 0.3s ease;
        `;

        if (type === 'success') {
            notification.style.background = 'var(--success)';
        } else if (type === 'error') {
            notification.style.background = 'var(--error)';
        } else {
            notification.style.background = 'var(--primary)';
        }

        document.body.appendChild(notification);

        // Remove after 3 seconds
        setTimeout(() => {
            notification.style.animation = 'slideOut 0.3s ease';
            setTimeout(() => {
                document.body.removeChild(notification);
            }, 300);
        }, 3000);
    }

    // Add CSS animations for notifications
    const style = document.createElement('style');
    style.textContent = `
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        @keyframes slideOut {
            from { transform: translateX(0); opacity: 1; }
            to { transform: translateX(100%); opacity: 0; }
        }
    `;
    document.head.appendChild(style);

    // Syntax highlighting function for Robot Framework
    function applySyntaxHighlighting(code) {
        const lines = code.split('\n');
        let highlightedHTML = '';

        lines.forEach(line => {
            let highlightedLine = '';

            // Sections (*** Settings ***, *** Variables ***, etc.)
            if (line.trim().startsWith('***') && line.trim().endsWith('***')) {
                highlightedLine = `<span class="rf-section">${escapeHtml(line)}</span>`;
            }
            // Comments
            else if (line.trim().startsWith('#')) {
                highlightedLine = `<span class="rf-comment">${escapeHtml(line)}</span>`;
            }
            // Settings keywords (Library, Resource, etc.) - detect by position
            else if (line.match(/^(Library|Resource|Variables|Suite Setup|Suite Teardown|Test Setup|Test Teardown|Test Template|Test Timeout|Force Tags|Default Tags|Documentation)\s/)) {
                const match = line.match(/^(Library|Resource|Variables|Suite Setup|Suite Teardown|Test Setup|Test Teardown|Test Template|Test Timeout|Force Tags|Default Tags|Documentation)(\s+.*)$/);
                if (match) {
                    const keyword = match[1];
                    const rest = match[2];
                    const restHighlighted = escapeHtml(rest).replace(/(\$\{[^}]+\})/g, '<span class="rf-variable">$1</span>');
                    highlightedLine = `<span class="rf-keyword">${keyword}</span>${restHighlighted}`;
                } else {
                    highlightedLine = escapeHtml(line);
                }
            }
            // Variable definitions at column 0 (starts with ${)
            else if (line.startsWith('${')) {
                // Variable definition: ${name}    value
                const varMatch = line.match(/^(\$\{[^}]+\})(\s+)(.+)$/);
                if (varMatch) {
                    const varName = varMatch[1];
                    const separator = varMatch[2];
                    const varValue = varMatch[3];
                    // Variable name in blue, value in default text color
                    highlightedLine = `<span class="rf-variable">${escapeHtml(varName)}</span>${separator}${escapeHtml(varValue)}`;
                } else {
                    // Just a variable without value
                    highlightedLine = `<span class="rf-variable">${escapeHtml(line)}</span>`;
                }
            }
            // Test case names (lines that don't start with whitespace and aren't sections/comments)
            else if (line.length > 0 && !line.startsWith(' ') && !line.startsWith('\t') && !line.startsWith('***') && !line.startsWith('#')) {
                highlightedLine = `<span class="rf-test-name">${escapeHtml(line)}</span>`;
            }
            // Indented lines (test steps or variable definitions) - dynamically detect keywords
            else if (line.match(/^\s+\S/)) {
                const leadingSpaces = line.match(/^(\s+)/)[1];
                const trimmed = line.trimStart();
                
                // Check if this is a variable definition (starts with ${)
                if (trimmed.startsWith('${')) {
                    // Variable definition: ${name}    value
                    // Match variable name, then any whitespace (1+), then the value
                    const varMatch = trimmed.match(/^(\$\{[^}]+\})(\s+)(.+)$/);
                    if (varMatch) {
                        const varName = varMatch[1];
                        const separator = varMatch[2];
                        const varValue = varMatch[3];
                        // Variable name in blue, value in default text color (no span = default)
                        highlightedLine = `${leadingSpaces}<span class="rf-variable">${escapeHtml(varName)}</span>${separator}${escapeHtml(varValue)}`;
                    } else {
                        // Just a variable without value - highlight only the variable
                        highlightedLine = `${leadingSpaces}<span class="rf-variable">${escapeHtml(trimmed)}</span>`;
                    }
                } else {
                    // Robot Framework uses 2+ spaces or tabs to separate keyword from arguments
                    // Match everything before the first occurrence of 2+ spaces or tab
                    const keywordMatch = trimmed.match(/^([^\s]+(?:\s+[^\s]+)*?)(\s{2,}|\t)/);
                    
                    if (keywordMatch) {
                        // Found a keyword (text before 2+ spaces or tab)
                        const keyword = keywordMatch[1];
                        const separator = keywordMatch[2];
                        const rest = trimmed.substring(keyword.length + separator.length);
                        
                        // Highlight variables in the rest
                        const restHighlighted = escapeHtml(rest).replace(/(\$\{[^}]+\})/g, '<span class="rf-variable">$1</span>');
                        highlightedLine = `${leadingSpaces}<span class="rf-builtin">${escapeHtml(keyword)}</span>${separator}${restHighlighted}`;
                    } else {
                        // No separator found - could be a keyword without arguments (like "Close Browser")
                        // Highlight the entire trimmed line as a keyword
                        highlightedLine = `${leadingSpaces}<span class="rf-builtin">${escapeHtml(trimmed)}</span>`;
                    }
                }
            }
            // Variables (${...}) in other lines (but NOT indented lines starting with ${)
            else if (line.includes('${') && !line.match(/^\s+\$/)) {
                highlightedLine = escapeHtml(line).replace(/(\$\{[^}]+\})/g, '<span class="rf-variable">$1</span>');
            }
            // Default: just escape HTML
            else {
                highlightedLine = escapeHtml(line);
            }

            highlightedHTML += highlightedLine + '\n';
        });

        robotCodeEl.innerHTML = highlightedHTML;
    }

    // Helper function to escape HTML
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
});
