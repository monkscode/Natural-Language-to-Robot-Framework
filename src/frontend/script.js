document.addEventListener('DOMContentLoaded', () => {
    const queryInput = document.getElementById('query-input');
    const generateBtn = document.getElementById('generate-btn');
    const robotCodeEl = document.getElementById('robot-code');
    const executionLogsEl = document.getElementById('execution-logs');
    const downloadBtn = document.getElementById('download-btn');
    const statusBadge = document.getElementById('status-badge');
    const statusText = document.getElementById('status-text');

    let robotCodeContent = '';

    function updateStatus(status, text) {
        statusBadge.style.display = 'flex';
        statusBadge.className = `status-badge status-${status}`;
        statusText.textContent = text;
    }

    function hideStatus() {
        setTimeout(() => {
            statusBadge.style.display = 'none';
        }, 5000);
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
    queryInput.addEventListener('input', function() {
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
        robotCodeContent = '';

        try {
            const requestPayload = {
                query: query,
                model: "gemini-1.5-pro-latest"
            };

            const response = await fetch('http://localhost:5000/generate-and-run', {
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
                robotCodeEl.textContent = robotCodeContent;
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
                if (logs.includes('PASSED')) {
                    updateStatus('success', 'Test passed');
                } else {
                    updateStatus('error', 'Test failed');
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
});
