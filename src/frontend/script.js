/* --- THEME & MODE LOGIC --- */
function setTheme(themeName) {
    document.documentElement.setAttribute('data-theme', themeName);
    localStorage.setItem('theme', themeName);

    // Update active state using data-theme attribute
    document.querySelectorAll('.theme-toggle-group .control-btn').forEach(btn => {
        btn.classList.remove('active');
        if (btn.dataset.theme === themeName) {
            btn.classList.add('active');
        }
    });

    // Dispatch custom event for other pages to listen to (Comment #2 - decoupling)
    window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: themeName } }));
}

function toggleDarkMode() {
    const currentMode = document.documentElement.getAttribute('data-mode');
    const newMode = currentMode === 'dark' ? 'light' : 'dark';

    document.documentElement.setAttribute('data-mode', newMode);
    localStorage.setItem('mode', newMode);
    // Icon visibility is now controlled by CSS based on data-mode attribute

    // Dispatch custom event for other pages to listen to (Comment #2 - decoupling)
    window.dispatchEvent(new CustomEvent('modechange', { detail: { mode: newMode } }));
}

// Initialize theme on page load (theme/mode can be set before DOM ready)
const savedTheme = localStorage.getItem('theme') || 'professional';
const savedMode = localStorage.getItem('mode') || 'light';

// Set theme and mode attributes immediately (works before DOM ready)
setTheme(savedTheme);
if (savedMode === 'dark') {
    document.documentElement.setAttribute('data-mode', 'dark');
}

// Update icons and set up event delegation once DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    // Icon visibility is controlled by CSS based on data-mode attribute
    // No manual icon manipulation needed

    // Event delegation for theme buttons (using specific selector to avoid matching <html> element)
    document.querySelectorAll('.theme-toggle-group [data-theme]').forEach(btn => {
        btn.addEventListener('click', () => setTheme(btn.dataset.theme));
    });

    // Event delegation for dark mode toggle
    document.querySelectorAll('[data-action="toggle-dark-mode"]').forEach(btn => {
        btn.addEventListener('click', toggleDarkMode);
    });
});

document.addEventListener('DOMContentLoaded', () => {
    const queryInput = document.getElementById('query-input');
    const actionBtn = document.getElementById('action-btn');
    const actionBtnText = document.getElementById('action-btn-text');
    const actionBtnIcon = document.getElementById('action-btn-icon');
    const newTestBtn = document.getElementById('new-test-btn');
    const robotCodeEl = document.getElementById('robot-code');
    const codePlaceholder = document.getElementById('code-placeholder');
    const generationLogsEl = document.getElementById('generation-logs');
    const executionLogsEl = document.getElementById('execution-logs');
    const downloadBtn = document.getElementById('download-btn');
    const copyCodeBtn = document.getElementById('copy-code-btn');
    const codeLanguageLabel = document.getElementById('code-language');
    const statusBadge = document.getElementById('status-badge');
    const statusText = document.getElementById('status-text');
    const editHint = document.getElementById('edit-hint');

    let robotCodeContent = '';
    let executionStartTime = null;
    let currentState = 'idle'; // idle, ready_generate, ready_execute, generating, executing

    // Track generation and execution history
    let hasGeneratedCode = false;
    let hasExecutedCode = false;

    // Store the original user query for pattern learning
    let currentUserQuery = null;

    // Store workflow ID from generation for unified ID tracking
    let currentWorkflowId = null;

    // Track manual collapse/expand state
    let generationLogsManualState = null; // null = auto, true = expanded, false = collapsed
    let executionLogsManualState = null; // null = auto, true = expanded, false = collapsed

    // Get log section containers
    const generationLogsSection = document.getElementById('generation-logs-section');
    const executionLogsSection = document.getElementById('execution-logs-section');

    // UI State Management
    const UIState = {
        IDLE: 'idle',
        READY_TO_GENERATE: 'ready_generate',
        READY_TO_EXECUTE: 'ready_execute',
        GENERATING: 'generating',
        EXECUTING: 'executing'
    };

    const buttonConfig = {
        [UIState.IDLE]: {
            text: 'Enter a query or paste code',
            disabled: true,
            icon: 'M13 10V3L4 14h7v7l9-11h-7z', // Lightning bolt
            action: null
        },
        [UIState.READY_TO_GENERATE]: {
            text: 'Generate Test',
            disabled: false,
            icon: 'M13 10V3L4 14h7v7l9-11h-7z', // Lightning bolt
            action: 'generate'
        },
        [UIState.READY_TO_EXECUTE]: {
            text: 'Execute Test',
            disabled: false,
            icon: 'M5 3l14 9-14 9V3z', // Play icon
            action: 'execute'
        },
        [UIState.GENERATING]: {
            text: 'Generating...',
            disabled: true,
            icon: null,
            action: null
        },
        [UIState.EXECUTING]: {
            text: 'Executing...',
            disabled: true,
            icon: null,
            action: null
        }
    };

    function updateButtonState(state) {
        currentState = state;
        const config = buttonConfig[state];

        actionBtnText.textContent = config.text;
        actionBtn.disabled = config.disabled;

        if (config.icon) {
            actionBtnIcon.innerHTML = `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="${config.icon}"></path>`;
            actionBtnIcon.style.display = 'block';
        } else {
            actionBtnIcon.style.display = 'none';
        }

        // Show/hide spinner
        const btnSpinner = actionBtn.querySelector('.btn-spinner');
        if (state === UIState.GENERATING || state === UIState.EXECUTING) {
            actionBtn.classList.add('btn-loading');
            btnSpinner.style.display = 'block';
        } else {
            actionBtn.classList.remove('btn-loading');
            btnSpinner.style.display = 'none';
        }
    }

    function determineState() {
        const hasQuery = queryInput.value.trim().length > 0;
        const hasCode = getCodeContent().trim().length > 0;

        if (currentState === UIState.GENERATING || currentState === UIState.EXECUTING) {
            return currentState; // Don't change state during operations
        }

        if (hasCode) {
            return UIState.READY_TO_EXECUTE;
        } else if (hasQuery) {
            return UIState.READY_TO_GENERATE;
        } else {
            return UIState.IDLE;
        }
    }

    function updateUI() {
        const newState = determineState();
        updateButtonState(newState);
        updatePlaceholder();
        updateNewTestButton();
    }

    function updatePlaceholder() {
        const hasQuery = queryInput.value.trim().length > 0;
        const hasCode = getCodeContent().trim().length > 0;

        if (!hasCode && codePlaceholder) {
            if (hasQuery) {
                codePlaceholder.querySelector('p').textContent = "⚡ Click 'Generate Test' to create code from your query";
            } else {
                codePlaceholder.querySelector('p').textContent = "📝 Paste your Robot Framework code here, or enter a query above to generate";
            }
        }
    }

    function updateNewTestButton() {
        const hasQuery = queryInput.value.trim().length > 0;
        const hasCode = getCodeContent().trim().length > 0;

        // Show "New Test" button if there's any content
        if (hasQuery || hasCode) {
            newTestBtn.style.display = 'inline-flex';
        } else {
            newTestBtn.style.display = 'none';
        }
    }

    function getCodeContent() {
        // Get text content, excluding the placeholder
        if (codePlaceholder && codePlaceholder.parentElement === robotCodeEl) {
            return '';
        }
        return robotCodeEl.textContent || '';
    }

    function setCodeContent(code, highlighted = false) {
        robotCodeContent = code;

        // Remove placeholder if exists
        if (codePlaceholder && codePlaceholder.parentElement === robotCodeEl) {
            robotCodeEl.removeChild(codePlaceholder);
        }

        if (highlighted) {
            robotCodeEl.innerHTML = code;
        } else {
            robotCodeEl.textContent = code;
        }

        if (code.trim()) {
            copyCodeBtn.style.display = 'flex';
            codeLanguageLabel.style.display = 'block';
            downloadBtn.style.display = 'inline-flex';
            editHint.style.display = 'block';
        }

        updateUI();
    }

    function clearCode() {
        robotCodeEl.innerHTML = '';
        if (codePlaceholder) {
            robotCodeEl.appendChild(codePlaceholder);
        }
        robotCodeContent = '';
        copyCodeBtn.style.display = 'none';
        codeLanguageLabel.style.display = 'none';
        downloadBtn.style.display = 'none';
        editHint.style.display = 'none';
        updateUI();
    }

    function updateStatus(status, text, persistent = false) {
        statusBadge.style.display = 'flex';
        statusBadge.className = `status-badge status-${status}`;
        statusText.textContent = text;

        if (persistent) {
            statusBadge.classList.add('status-persistent');
        } else {
            statusBadge.classList.remove('status-persistent');
        }
    }

    function hideStatus() {
        if (!statusBadge.classList.contains('status-persistent')) {
            setTimeout(() => {
                statusBadge.style.display = 'none';
            }, 5000);
        }
    }

    function createLogEntry(logEvent, stage) {
        const logEntry = document.createElement('div');
        logEntry.className = 'log-entry';

        // Determine log level styling based on message content or status
        let logLevel = 'info';
        if (logEvent.status === 'error' || logEvent.message.includes('⚠️') || logEvent.message.includes('ERROR')) {
            logLevel = 'error';
        } else if (logEvent.message.includes('🎉') || logEvent.message.includes('Success')) {
            logLevel = 'success';
            // Check if this is a celebration message
            if (logEvent.message.includes('🎉') && (logEvent.message.includes('Generated') || logEvent.message.includes('Success!'))) {
                logEntry.classList.add('log-celebration');
            }
        } else if (logEvent.message.includes('💡')) {
            logLevel = 'insight';
        }
        logEntry.classList.add(`log-${logLevel}`);

        // Build the log message HTML
        let html = `<div class="log-timestamp">[${new Date().toLocaleTimeString()}]</div>`;
        html += `<div class="log-message">${escapeHtml(logEvent.message)}`;

        // Add step info if present (no individual progress bars)
        if (logEvent.step) {
            html += ` <span class="log-step-info">(Step ${logEvent.step})</span>`;
        }

        html += `</div>`;

        logEntry.innerHTML = html;

        // Update global progress bar if progress is present
        if (logEvent.progress !== undefined) {
            updateGlobalProgress(stage, logEvent.progress);
        }

        return logEntry;
    }

    function updateGlobalProgress(stage, progress) {
        const progressContainer = stage === 'generation'
            ? document.getElementById('generation-progress-container')
            : document.getElementById('execution-progress-container');
        const progressFill = stage === 'generation'
            ? document.getElementById('generation-progress-fill')
            : document.getElementById('execution-progress-fill');
        const progressText = stage === 'generation'
            ? document.getElementById('generation-progress-text')
            : document.getElementById('execution-progress-text');

        // Show progress container
        progressContainer.style.display = 'block';

        // Update progress bar and text
        progressFill.style.width = `${progress}%`;
        progressText.textContent = `${progress}%`;

        // Hide progress bar when complete (100%)
        if (progress >= 100) {
            setTimeout(() => {
                progressContainer.style.display = 'none';
            }, 1000);
        }
    }

    function routeLogToContainer(logEntry, stage) {
        // Route log to the correct container based on stage
        const targetContainer = stage === 'generation' ? generationLogsEl : executionLogsEl;
        const contentEl = stage === 'generation'
            ? document.getElementById('generation-logs-content')
            : document.getElementById('execution-logs-content');

        // Remove empty state if present
        const emptyState = targetContainer.querySelector('.empty-state');
        if (emptyState) {
            targetContainer.innerHTML = '';
        }

        // Append log entry
        targetContainer.appendChild(logEntry);

        // Only auto-scroll if section is expanded
        if (!contentEl.classList.contains('collapsed')) {
            targetContainer.scrollTop = targetContainer.scrollHeight;
        }
    }

    // Event Listeners
    queryInput.addEventListener('input', function () {
        this.style.height = 'auto';
        this.style.height = Math.max(150, this.scrollHeight) + 'px';
        updateUI();
    });

    // Handle first character input to prevent it from being swallowed
    robotCodeEl.addEventListener('keydown', (e) => {
        // If placeholder is present and user types a key that produces output
        if (codePlaceholder && codePlaceholder.parentElement === robotCodeEl) {
            // Check for printable characters (length 1, no special modifiers)
            if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
                e.preventDefault(); // Prevent default insertion to handle it manually

                // Remove placeholder
                robotCodeEl.removeChild(codePlaceholder);

                // Reset any inherited styles (fixes large font bug)
                document.execCommand('fontSize', false, '3'); // Reset to normal

                // Manually insert the character
                document.execCommand('insertText', false, e.key);

                updateUI();
            }
        }
    });

    robotCodeEl.addEventListener('input', () => {
        // When user types or edits, update UI state
        // Remove placeholder if user starts typing (fallback for other input methods)
        if (codePlaceholder && codePlaceholder.parentElement === robotCodeEl) {
            robotCodeEl.removeChild(codePlaceholder);
            // Reset any inherited styles (fixes large font bug)
            document.execCommand('fontSize', false, '3'); // Reset to normal
        }
        updateUI();
    });

    robotCodeEl.addEventListener('paste', (e) => {
        // Handle paste to preserve user's formatting and apply syntax highlighting
        e.preventDefault();
        const text = e.clipboardData.getData('text/plain');
        if (!text.trim()) return;

        // Remove placeholder if present
        if (codePlaceholder && codePlaceholder.parentElement === robotCodeEl) {
            robotCodeEl.removeChild(codePlaceholder);
            applySyntaxHighlighting(text);
            return;
        }

        // Get existing code and append
        const existingCode = getCodeContent();
        const combinedCode = existingCode.trim() ? existingCode + '\n' + text : text;
        applySyntaxHighlighting(combinedCode);

        // User pasted code directly - don't show generation logs
        // Execution logs will show when user clicks execute
    });

    // Also listen for blur event to apply formatting when user finishes typing
    robotCodeEl.addEventListener('blur', () => {
        const code = getCodeContent().trim();
        if (code && !robotCodeEl.querySelector('.rf-section')) {
            // Code exists but no syntax highlighting - apply it
            applySyntaxHighlighting(code);
        }
    });

    // Keyup event as backup for detecting content changes
    robotCodeEl.addEventListener('keyup', () => {
        updateUI();
    });

    // New Test Button Handler
    newTestBtn.addEventListener('click', () => {
        const hasCode = getCodeContent().trim().length > 0;

        if (hasCode) {
            // Show confirmation dialog
            if (confirm('⚠️ This will clear your current test. Are you sure you want to start a new test?')) {
                clearAll();
            }
        } else {
            clearAll();
        }
    });

    function clearAll() {
        queryInput.value = '';
        queryInput.style.height = 'auto';
        clearCode();
        generationLogsEl.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🎬</div><p>Test generation logs will appear here</p></div>';
        executionLogsEl.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>Test execution logs will appear here</p></div>';
        statusBadge.classList.remove('status-persistent');
        statusBadge.style.display = 'none';

        // Reset tracking flags
        hasGeneratedCode = false;
        hasExecutedCode = false;
        generationLogsManualState = null;
        executionLogsManualState = null;

        // Clear stored user query and workflow ID
        currentUserQuery = null;
        currentWorkflowId = null;  // Reset unified workflow ID

        // Hide both log sections
        generationLogsSection.style.display = 'none';
        executionLogsSection.style.display = 'none';

        updateUI();
    }

    // Manage log section visibility
    function updateLogSectionsVisibility() {
        // Generation logs visibility
        if (hasGeneratedCode) {
            generationLogsSection.style.display = 'block';

            // Auto-collapse during execution unless manually overridden
            if (generationLogsManualState === null) {
                if (currentState === UIState.EXECUTING || hasExecutedCode) {
                    collapseSection('generation', false);
                } else {
                    expandSection('generation', false);
                }
            } else {
                // Respect manual state
                if (generationLogsManualState) {
                    expandSection('generation', false);
                } else {
                    collapseSection('generation', false);
                }
            }
        } else {
            generationLogsSection.style.display = 'none';
        }

        // Execution logs visibility
        if (hasExecutedCode || currentState === UIState.EXECUTING) {
            executionLogsSection.style.display = 'block';

            // Auto-expand during execution unless manually overridden
            if (executionLogsManualState === null) {
                expandSection('execution', false);
            } else {
                // Respect manual state
                if (executionLogsManualState) {
                    expandSection('execution', false);
                } else {
                    collapseSection('execution', false);
                }
            }
        } else if (!hasGeneratedCode) {
            // If user pastes code directly (no generation), don't show execution logs until execution starts
            executionLogsSection.style.display = 'none';
        }
    }

    function collapseSection(section, isManual = true) {
        const content = section === 'generation'
            ? document.getElementById('generation-logs-content')
            : document.getElementById('execution-logs-content');
        const button = section === 'generation'
            ? document.getElementById('toggle-generation-logs')
            : document.getElementById('toggle-execution-logs');
        const sectionEl = section === 'generation' ? generationLogsSection : executionLogsSection;

        content.classList.add('collapsed');
        sectionEl.classList.add('collapsed-section');
        button.setAttribute('aria-expanded', 'false');
        button.querySelector('span').textContent = 'Expand';
        button.querySelector('svg path').setAttribute('d', 'M5 15l7-7 7 7');

        if (isManual) {
            if (section === 'generation') {
                generationLogsManualState = false;
            } else {
                executionLogsManualState = false;
            }
        }
    }

    function expandSection(section, isManual = true) {
        const content = section === 'generation'
            ? document.getElementById('generation-logs-content')
            : document.getElementById('execution-logs-content');
        const button = section === 'generation'
            ? document.getElementById('toggle-generation-logs')
            : document.getElementById('toggle-execution-logs');
        const sectionEl = section === 'generation' ? generationLogsSection : executionLogsSection;
        const logsEl = section === 'generation' ? generationLogsEl : executionLogsEl;

        content.classList.remove('collapsed');
        sectionEl.classList.remove('collapsed-section');
        button.setAttribute('aria-expanded', 'true');
        button.querySelector('span').textContent = 'Collapse';
        button.querySelector('svg path').setAttribute('d', 'M19 9l-7 7-7-7');

        // Auto-scroll to latest log when expanding
        if (isManual) {
            logsEl.scrollTop = logsEl.scrollHeight;

            if (section === 'generation') {
                generationLogsManualState = true;
            } else {
                executionLogsManualState = true;
            }
        }
    }

    // Toggle button handlers
    document.getElementById('toggle-generation-logs').addEventListener('click', () => {
        const content = document.getElementById('generation-logs-content');
        if (content.classList.contains('collapsed')) {
            expandSection('generation', true);
        } else {
            collapseSection('generation', true);
        }
    });

    document.getElementById('toggle-execution-logs').addEventListener('click', () => {
        const content = document.getElementById('execution-logs-content');
        if (content.classList.contains('collapsed')) {
            expandSection('execution', true);
        } else {
            collapseSection('execution', true);
        }
    });

    // Main Action Button Handler
    actionBtn.addEventListener('click', async () => {
        const config = buttonConfig[currentState];

        if (!config.action) return;

        if (config.action === 'generate') {
            await handleGenerate();
        } else if (config.action === 'execute') {
            await handleExecute();
        }
    });

    async function handleGenerate() {
        const query = queryInput.value.trim();
        const hasExistingCode = getCodeContent().trim().length > 0;

        // Confirmation for regeneration
        if (hasExistingCode) {
            if (!confirm('⚠️ This will replace your current code. Continue?')) {
                return;
            }
        }

        // Store the user query for pattern learning when executing
        currentUserQuery = query;

        updateButtonState(UIState.GENERATING);
        clearCode();
        // Show loading placeholder while generation is in progress
        generationLogsEl.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🧠</div><p>AI is generating your test code... Please wait.</p></div>';
        downloadBtn.style.display = 'none';
        statusBadge.classList.remove('status-persistent');
        statusBadge.style.display = 'none';

        // Reset progress bar
        const generationProgressContainer = document.getElementById('generation-progress-container');
        const generationProgressFill = document.getElementById('generation-progress-fill');
        const generationProgressText = document.getElementById('generation-progress-text');
        generationProgressContainer.style.display = 'none';
        generationProgressFill.style.width = '0%';
        generationProgressText.textContent = '0%';

        // Mark that generation has started
        hasGeneratedCode = true;

        // Reset manual state for generation logs (allow auto-management)
        generationLogsManualState = null;

        // Update log sections visibility
        updateLogSectionsVisibility();

        // Auto-scroll to generation logs so user can see progress
        setTimeout(() => {
            generationLogsSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 100);

        try {
            const requestPayload = {
                query: query,
                model: "gemini-1.5-pro-latest"
            };

            const response = await fetch('/generate-test', {
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
                                handleGenerationData(data);
                            } catch (e) {
                                console.error('Failed to parse JSON from stream:', jsonData);
                            }
                        }
                    }
                }
            }
        } catch (error) {
            updateStatus('error', 'Generation failed');
            executionLogsEl.textContent = `Error: ${error.message}`;
            hideStatus();
        } finally {
            updateUI();
        }
    }

    async function handleExecute() {
        const code = getCodeContent().trim();

        if (!code) {
            alert('No code to execute');
            return;
        }

        updateButtonState(UIState.EXECUTING);
        // Show loading placeholder while execution is in progress
        executionLogsEl.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⏳</div><p>Execution in progress... Please wait.</p></div>';
        statusBadge.classList.remove('status-persistent');
        statusBadge.style.display = 'none';
        executionStartTime = Date.now();

        // Reset progress bar
        const executionProgressContainer = document.getElementById('execution-progress-container');
        const executionProgressFill = document.getElementById('execution-progress-fill');
        const executionProgressText = document.getElementById('execution-progress-text');
        executionProgressContainer.style.display = 'none';
        executionProgressFill.style.width = '0%';
        executionProgressText.textContent = '0%';

        // Mark that execution has started
        hasExecutedCode = true;

        // Reset manual state for execution logs (allow auto-management)
        executionLogsManualState = null;

        // Update log sections visibility (will auto-collapse generation, show execution)
        updateLogSectionsVisibility();

        // Auto-scroll to execution logs so user can see progress
        setTimeout(() => {
            executionLogsSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 100);

        try {
            const requestPayload = {
                robot_code: code,
                user_query: currentUserQuery,  // Pass the original query for pattern learning
                workflow_id: currentWorkflowId  // Pass unified workflow ID from generation
            };

            const response = await fetch('/execute-test', {
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
                                handleExecutionData(data);
                            } catch (e) {
                                console.error('Failed to parse JSON from stream:', jsonData);
                            }
                        }
                    }
                }
            }
        } catch (error) {
            updateStatus('error', 'Execution failed');
            executionLogsEl.textContent = `Error: ${error.message}`;
            hideStatus();
        } finally {
            updateUI();
        }
    }

    function handleGenerationData(data) {
        updateStatus('processing', data.message);

        if (data.status === 'running') {
            const stage = data.stage || 'generation';
            const logEntry = createLogEntry(data, stage);
            routeLogToContainer(logEntry, stage);
        } else if (data.status === 'complete' && data.robot_code) {
            applySyntaxHighlighting(data.robot_code);
            // Capture workflow_id for unified ID tracking during execution
            if (data.workflow_id) {
                currentWorkflowId = data.workflow_id;
            }
            updateStatus('success', 'Generation complete', false);
            hideStatus();
            // Reset state to allow button update, then update UI
            currentState = UIState.IDLE;
            updateUI();
            updateLogSectionsVisibility();
            // Auto-scroll back to code area so user can see the generated code
            setTimeout(() => {
                document.querySelector('.section:has(#robot-code)')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }, 200);
        } else if (data.status === 'error') {
            updateStatus('error', 'Generation failed');
            const stage = data.stage || 'generation';
            const logEntry = createLogEntry(data, stage);
            routeLogToContainer(logEntry, stage);
            hideStatus();
            // Reset state to allow button update, then update UI
            currentState = UIState.IDLE;
            updateUI();
            updateLogSectionsVisibility();
        }
    }

    // Format execution logs into structured, readable HTML
    function formatExecutionLog(rawLog) {
        // Pattern: Robot Framework Test Execution (Exit Code: X)
        // ==== Suite: Test Test: Generated Test - PASS Results: X passed, Y failed
        // Detailed logs available in: <path>

        const container = document.createElement('div');
        container.className = 'execution-summary';

        // Extract exit code
        const exitCodeMatch = rawLog.match(/Robot Framework Test Execution \(Exit Code: (\d+)\)/i);
        const exitCode = exitCodeMatch ? exitCodeMatch[1] : null;

        // Extract suite name - simplified regex to avoid ReDoS
        // Changed from: /Suite:\s*([^\s]+(?:\s+[^\s]+)*?)(?=\s+Test:|$)/i
        const suiteMatch = rawLog.match(/Suite:\s*([^\n]+?)\s+Test:/i);
        const suiteName = suiteMatch ? suiteMatch[1].trim() : 'Unknown';

        // Extract test name and status - simplified regex to avoid ReDoS
        // Changed from: /Test:\s*(.+?)\s*-\s*(PASS|FAIL)/i
        const testMatch = rawLog.match(/Test:\s*([^-]+)\s*-\s*(PASS|FAIL)/i);
        const testName = testMatch ? testMatch[1].trim() : 'Unknown';
        // BUG FIX: Derive testStatus from failed count (below) instead of testMatch[2]
        // because the Test line can incorrectly show PASS when results show failures

        // Extract results - simplified regex to avoid ReDoS
        // Changed from: /Results?:\s*(\d+)\s*passed,\s*(\d+)\s*failed/i
        const resultsMatch = rawLog.match(/Results?:\s*(\d+) passed, (\d+) failed/i);
        const passed = resultsMatch ? resultsMatch[1] : '0';
        const failed = resultsMatch ? resultsMatch[2] : '0';
        const testStatus = parseInt(failed) > 0 ? 'FAIL' : (testMatch ? testMatch[2].toUpperCase() : 'UNKNOWN');
        // Extract log path
        const logPathMatch = rawLog.match(/Detailed logs available in:\s*(.+?)(?:\s*$|$)/i);
        let logPath = logPathMatch ? logPathMatch[1].trim() : null;

        // Clean up the log path - remove any trailing whitespace or characters
        if (logPath) {
            logPath = logPath.replace(/\s+$/, '');
        }

        // Build formatted HTML
        let html = '';

        // Header with exit code
        html += '<div class="summary-header">';
        html += `🤖 Robot Framework Test Execution`;
        if (exitCode !== null) {
            html += ` <span class="${exitCode === '0' ? 'summary-pass' : 'summary-fail'}">(Exit Code: ${exitCode})</span>`;
        }
        html += '</div>';

        // Divider
        html += '<div class="summary-divider">══════════════════════════════════════════════════</div>';

        // Suite info
        html += `<div class="summary-line"><span class="summary-label">Suite:</span> ${escapeHtml(suiteName)}</div>`;

        // Test info
        html += `<div class="summary-line"><span class="summary-label">Test:</span> ${escapeHtml(testName)} - `;
        html += `<span class="${testStatus === 'PASS' ? 'summary-pass' : 'summary-fail'}">${testStatus}</span></div>`;

        // Results
        html += `<div class="summary-line"><span class="summary-label">Results:</span> `;
        html += `<span class="summary-pass">${passed} passed</span>, `;
        html += `<span class="${parseInt(failed) > 0 ? 'summary-fail' : ''}">${failed} failed</span></div>`;

        // Log link
        if (logPath) {
            // Extract run_id from path (e.g., robot_tests/UUID/log.html)
            const pathParts = logPath.replace(/\\/g, '/').split('/');
            const robotTestsIndex = pathParts.findIndex(part => part === 'robot_tests');

            let logUrl = logPath; // fallback to original path
            if (robotTestsIndex !== -1 && pathParts.length > robotTestsIndex + 2) {
                const runId = pathParts[robotTestsIndex + 1];
                const fileName = pathParts[pathParts.length - 1];
                // Create HTTP URL using the /reports endpoint
                logUrl = `/reports/${runId}/${fileName}`;
            }

            // XSS Protection: Validate URL scheme to prevent javascript: injection
            const isValidUrl = logUrl.startsWith('/') || logUrl.startsWith('http://') || logUrl.startsWith('https://');
            if (!isValidUrl) {
                logUrl = '#'; // Fallback to safe value
            }

            html += `<div class="summary-line" style="margin-top: 0.5rem;">`;
            html += `<span class="summary-label">📄 Detailed logs:</span> `;
            html += `<a href="${logUrl}" target="_blank" class="log-link" title="Open log file">View Log</a>`;
            html += `</div>`;
        }

        container.innerHTML = html;
        return container;
    }

    function handleExecutionData(data) {
        updateStatus('processing', data.message);

        if (data.status === 'running') {
            const stage = data.stage || 'execution';
            const logEntry = createLogEntry(data, stage);
            routeLogToContainer(logEntry, stage);
        } else if (data.status === 'complete' && data.result) {
            const logs = data.result.logs || 'No execution logs available';

            // Format the execution logs into structured HTML
            const formattedLog = formatExecutionLog(logs);
            executionLogsEl.innerHTML = '';
            executionLogsEl.appendChild(formattedLog);

            const executionTime = executionStartTime ? ((Date.now() - executionStartTime) / 1000).toFixed(1) : null;
            const timeText = executionTime ? ` (${executionTime}s)` : '';

            if (data.test_status === 'passed') {
                updateStatus('success', `Test passed${timeText}`, true);
            } else if (data.test_status === 'failed') {
                updateStatus('error', 'Test failed', true);
            } else {
                const logsUpper = logs.toUpperCase();
                const resultsMatch = logsUpper.match(/(\d+)\s+PASSED,\s+(\d+)\s+FAILED/);

                if (resultsMatch) {
                    const failedCount = parseInt(resultsMatch[2]);
                    if (failedCount === 0) {
                        updateStatus('success', `Test passed${timeText}`, true);
                    } else {
                        updateStatus('error', 'Test failed', true);
                    }
                } else if (logsUpper.includes('ALL TESTS PASSED')) {
                    updateStatus('success', `Test passed${timeText}`, true);
                } else if (logsUpper.includes('ERROR') || logsUpper.includes('EXCEPTION')) {
                    updateStatus('error', 'Test completed with errors', true);
                } else {
                    updateStatus('success', `Test completed${timeText}`, true);
                }
            }
            hideStatus();
            // Reset state to allow button update, then update UI
            currentState = UIState.IDLE;
            updateUI();
            updateLogSectionsVisibility();
            // Auto-scroll to execution logs to show the results
            setTimeout(() => {
                executionLogsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }, 200);
        } else if (data.status === 'error') {
            updateStatus('error', 'Execution failed');
            const errorEntry = document.createElement('div');
            errorEntry.style.color = 'var(--error)';
            errorEntry.textContent = `[${new Date().toLocaleTimeString()}] ERROR: ${data.message}`;
            executionLogsEl.appendChild(errorEntry);
            hideStatus();
            // Reset state to allow button update, then update UI
            currentState = UIState.IDLE;
            updateUI();
            updateLogSectionsVisibility();
        }
    }

    // Copy code button handler
    copyCodeBtn.addEventListener('click', async () => {
        const code = getCodeContent();
        try {
            await navigator.clipboard.writeText(code);

            const originalHTML = copyCodeBtn.innerHTML;
            const originalAriaLabel = copyCodeBtn.getAttribute('aria-label');

            copyCodeBtn.classList.add('copied');
            copyCodeBtn.setAttribute('aria-label', 'Copied successfully');
            copyCodeBtn.innerHTML = `
                <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"></path>
                </svg>
            `;

            setTimeout(() => {
                copyCodeBtn.classList.remove('copied');
                copyCodeBtn.setAttribute('aria-label', originalAriaLabel);
                copyCodeBtn.innerHTML = originalHTML;
            }, 2000);
        } catch (err) {
            console.error('Failed to copy:', err);
            alert('Failed to copy code to clipboard');
        }
    });

    // Download button handler
    downloadBtn.addEventListener('click', () => {
        const code = getCodeContent();
        const blob = new Blob([code], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'generated_test.robot';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

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

    // Keyboard shortcuts
    queryInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            if (currentState === UIState.READY_TO_GENERATE) {
                actionBtn.click();
            }
        }
    });

    robotCodeEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            if (currentState === UIState.READY_TO_EXECUTE) {
                actionBtn.click();
            }
        }
    });

    // Syntax highlighting function
    function applySyntaxHighlighting(code) {
        const lines = code.split('\n');
        let highlightedHTML = '';

        lines.forEach((line, index) => {
            let highlightedLine = '';

            if (line.trim().startsWith('***') && line.trim().endsWith('***')) {
                highlightedLine = `<span class="rf-section">${escapeHtml(line)}</span>`;
            } else if (line.trim().startsWith('#')) {
                highlightedLine = `<span class="rf-comment">${escapeHtml(line)}</span>`;
            } else if (line.match(/^(Library|Resource|Variables|Suite Setup|Suite Teardown|Test Setup|Test Teardown|Test Template|Test Timeout|Force Tags|Default Tags|Documentation)\s/)) {
                const match = line.match(/^(Library|Resource|Variables|Suite Setup|Suite Teardown|Test Setup|Test Teardown|Test Template|Test Timeout|Force Tags|Default Tags|Documentation)(\s+.*)$/);
                if (match) {
                    const keyword = match[1];
                    const rest = match[2];
                    const restHighlighted = escapeHtml(rest).replace(/(\$\{[^}]+\})/g, '<span class="rf-variable">$1</span>');
                    highlightedLine = `<span class="rf-keyword">${keyword}</span>${restHighlighted}`;
                } else {
                    highlightedLine = escapeHtml(line);
                }
            } else if (line.startsWith('${')) {
                const varMatch = line.match(/^(\$\{[^}]+\})(\s+)(.+)$/);
                if (varMatch) {
                    const varName = varMatch[1];
                    const separator = varMatch[2];
                    const varValue = varMatch[3];
                    highlightedLine = `<span class="rf-variable">${escapeHtml(varName)}</span>${separator}${escapeHtml(varValue)}`;
                } else {
                    highlightedLine = `<span class="rf-variable">${escapeHtml(line)}</span>`;
                }
            } else if (line.length > 0 && !line.startsWith(' ') && !line.startsWith('\t') && !line.startsWith('***') && !line.startsWith('#')) {
                highlightedLine = `<span class="rf-test-name">${escapeHtml(line)}</span>`;
            } else if (line.match(/^\s+\S/)) {
                const leadingSpaces = line.match(/^(\s+)/)[1];
                const trimmed = line.trimStart();

                const escapedLeadingSpaces = escapeHtml(leadingSpaces);
                if (trimmed.startsWith('${')) {
                    const varMatch = trimmed.match(/^(\$\{[^}]+\})(\s+)(.+)$/);
                    if (varMatch) {
                        const varName = varMatch[1];
                        const separator = varMatch[2];
                        const varValue = varMatch[3];
                        highlightedLine = `${escapedLeadingSpaces}<span class="rf-variable">${escapeHtml(varName)}</span>${escapeHtml(separator)}${escapeHtml(varValue)}`;
                    } else {
                        highlightedLine = `${escapedLeadingSpaces}<span class="rf-variable">${escapeHtml(trimmed)}</span>`;
                    }
                } else {
                    const keywordMatch = trimmed.match(/^([^\s]+(?:\s+[^\s]+)*?)(\s{2,}|\t)/);

                    if (keywordMatch) {
                        const keyword = keywordMatch[1];
                        const separator = keywordMatch[2];
                        const rest = trimmed.substring(keyword.length + separator.length);
                        const restHighlighted = escapeHtml(rest).replace(/(\$\{[^}]+\})/g, '<span class="rf-variable">$1</span>');
                        highlightedLine = `${escapedLeadingSpaces}<span class="rf-builtin">${escapeHtml(keyword)}</span>${escapeHtml(separator)}${restHighlighted}`;
                    } else {
                        highlightedLine = `${escapedLeadingSpaces}<span class="rf-builtin">${escapeHtml(trimmed)}</span>`;
                    }
                }
            } else if (line.includes('${') && !line.match(/^\s+\$/)) {
                highlightedLine = escapeHtml(line).replace(/(\$\{[^}]+\})/g, '<span class="rf-variable">$1</span>');
            } else {
                highlightedLine = escapeHtml(line);
            }

            // Add newline only if not the last line
            if (index < lines.length - 1) {
                highlightedHTML += highlightedLine + '\n';
            } else {
                highlightedHTML += highlightedLine;
            }
        });

        setCodeContent(highlightedHTML, true);
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Initialize UI
    updateUI();
});
