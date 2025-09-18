document.addEventListener('DOMContentLoaded', () => {
    const queryInput = document.getElementById('query-input');
    const generateBtn = document.getElementById('generate-btn');
    const robotCodeEl = document.getElementById('robot-code');
    const executionLogsEl = document.getElementById('execution-logs');
    const downloadBtn = document.getElementById('download-btn');
    const statusBadge = document.getElementById('status-badge');
    const statusText = document.getElementById('status-text');

    // Healing UI elements
    const healingProgress = document.getElementById('healing-progress');
    const healingStatus = document.getElementById('healing-status');
    const healingProgressFill = document.getElementById('healing-progress-fill');
    const healingDetails = document.getElementById('healing-details');

    // Navigation and sections
    const navTabs = document.querySelectorAll('.nav-tab');
    const workspaceSection = document.querySelector('.workspace');
    const executionSection = workspaceSection.nextElementSibling;
    const healingConfigSection = document.getElementById('healing-config-section');
    const healingDashboardSection = document.getElementById('healing-dashboard-section');

    // Configuration elements
    const healingEnabledCheckbox = document.getElementById('healing-enabled');
    const maxAttemptsInput = document.getElementById('max-attempts');
    const sessionTimeoutInput = document.getElementById('session-timeout');
    const healingTimeoutInput = document.getElementById('healing-timeout');
    const confidenceThresholdSlider = document.getElementById('confidence-threshold');
    const confidenceValue = document.getElementById('confidence-value');
    const saveConfigBtn = document.getElementById('save-config-btn');
    const resetConfigBtn = document.getElementById('reset-config-btn');

    // Dashboard elements
    const totalAttemptsEl = document.getElementById('total-attempts');
    const successRateEl = document.getElementById('success-rate');
    const avgHealingTimeEl = document.getElementById('avg-healing-time');
    const last24hAttemptsEl = document.getElementById('last-24h-attempts');
    const refreshReportsBtn = document.getElementById('refresh-reports-btn');
    const reportsTableBody = document.getElementById('reports-table-body');

    // Modal elements
    const healingReportModal = document.getElementById('healing-report-modal');
    const closeReportModal = document.getElementById('close-report-modal');
    const healingReportContent = document.getElementById('healing-report-content');

    let robotCodeContent = '';
    let currentHealingSession = null;

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
        } else if (data.stage === 'healing') {
            // Handle healing progress updates
            handleHealingProgress(data);
        }
    }

    function handleHealingProgress(data) {
        if (data.status === 'running') {
            // Show healing progress indicator
            healingProgress.style.display = 'block';
            healingStatus.textContent = data.message;

            // Update progress based on message content
            let progress = 0;
            if (data.message.includes('Analyzing')) {
                progress = 25;
                updateHealingStep('step-detection', 'active');
            } else if (data.message.includes('Generating')) {
                progress = 50;
                updateHealingStep('step-detection', 'complete');
                updateHealingStep('step-generation', 'active');
            } else if (data.message.includes('Validating')) {
                progress = 75;
                updateHealingStep('step-generation', 'complete');
                updateHealingStep('step-validation', 'active');
            } else if (data.message.includes('Updating')) {
                progress = 90;
                updateHealingStep('step-validation', 'complete');
                updateHealingStep('step-update', 'active');
            }

            healingProgressFill.style.width = `${progress}%`;

            // Add healing log entry
            const logEntry = document.createElement('div');
            logEntry.style.color = 'var(--primary)';
            logEntry.textContent = `[${new Date().toLocaleTimeString()}] HEALING: ${data.message}`;
            executionLogsEl.appendChild(logEntry);
            executionLogsEl.scrollTop = executionLogsEl.scrollHeight;

        } else if (data.status === 'complete') {
            // Healing completed successfully
            healingProgressFill.style.width = '100%';
            updateHealingStep('step-update', 'complete');
            healingStatus.textContent = 'Healing completed successfully!';

            setTimeout(() => {
                healingProgress.style.display = 'none';
                resetHealingSteps();
            }, 3000);

            const logEntry = document.createElement('div');
            logEntry.style.color = 'var(--success)';
            logEntry.textContent = `[${new Date().toLocaleTimeString()}] HEALING: Successfully healed locators`;
            executionLogsEl.appendChild(logEntry);
            executionLogsEl.scrollTop = executionLogsEl.scrollHeight;

        } else if (data.status === 'error') {
            // Healing failed
            healingStatus.textContent = 'Healing failed: ' + data.message;
            updateAllHealingSteps('error');

            setTimeout(() => {
                healingProgress.style.display = 'none';
                resetHealingSteps();
            }, 5000);

            const logEntry = document.createElement('div');
            logEntry.style.color = 'var(--error)';
            logEntry.textContent = `[${new Date().toLocaleTimeString()}] HEALING ERROR: ${data.message}`;
            executionLogsEl.appendChild(logEntry);
            executionLogsEl.scrollTop = executionLogsEl.scrollHeight;
        }
    }

    function updateHealingStep(stepId, status) {
        const stepElement = document.getElementById(stepId);
        if (stepElement) {
            stepElement.className = `step-status ${status}`;
            switch (status) {
                case 'active':
                    stepElement.textContent = '🔄';
                    break;
                case 'complete':
                    stepElement.textContent = '✅';
                    break;
                case 'error':
                    stepElement.textContent = '❌';
                    break;
                default:
                    stepElement.textContent = '⏳';
            }
        }
    }

    function updateAllHealingSteps(status) {
        ['step-detection', 'step-generation', 'step-validation', 'step-update'].forEach(stepId => {
            updateHealingStep(stepId, status);
        });
    }

    function resetHealingSteps() {
        ['step-detection', 'step-generation', 'step-validation', 'step-update'].forEach(stepId => {
            updateHealingStep(stepId, 'pending');
        });
        healingProgressFill.style.width = '0%';
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

    // Navigation tabs functionality
    navTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const targetSection = tab.dataset.section;
            
            // Update active tab
            navTabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            
            // Show/hide sections
            if (targetSection === 'workspace') {
                workspaceSection.style.display = 'grid';
                executionSection.style.display = 'block';
                healingConfigSection.style.display = 'none';
                healingDashboardSection.style.display = 'none';
            } else if (targetSection === 'healing-config') {
                workspaceSection.style.display = 'none';
                executionSection.style.display = 'none';
                healingConfigSection.style.display = 'block';
                healingDashboardSection.style.display = 'none';
                loadHealingConfig();
            } else if (targetSection === 'healing-dashboard') {
                workspaceSection.style.display = 'none';
                executionSection.style.display = 'none';
                healingConfigSection.style.display = 'none';
                healingDashboardSection.style.display = 'block';
                loadHealingDashboard();
            }
        });
    });

    // Configuration panel functionality
    confidenceThresholdSlider.addEventListener('input', (e) => {
        confidenceValue.textContent = e.target.value;
    });

    saveConfigBtn.addEventListener('click', async () => {
        try {
            const config = {
                enabled: healingEnabledCheckbox.checked,
                max_attempts_per_locator: parseInt(maxAttemptsInput.value),
                chrome_session_timeout: parseInt(sessionTimeoutInput.value),
                healing_timeout: parseInt(healingTimeoutInput.value) * 60, // Convert to seconds
                confidence_threshold: parseFloat(confidenceThresholdSlider.value)
            };

            const response = await fetch('/api/healing/config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(config)
            });

            if (response.ok) {
                showNotification('Configuration saved successfully!', 'success');
            } else {
                throw new Error('Failed to save configuration');
            }
        } catch (error) {
            showNotification('Failed to save configuration: ' + error.message, 'error');
        }
    });

    resetConfigBtn.addEventListener('click', () => {
        // Reset to default values
        healingEnabledCheckbox.checked = true;
        maxAttemptsInput.value = '3';
        sessionTimeoutInput.value = '30';
        healingTimeoutInput.value = '5';
        confidenceThresholdSlider.value = '0.7';
        confidenceValue.textContent = '0.7';
    });

    // Dashboard functionality
    refreshReportsBtn.addEventListener('click', () => {
        loadHealingReports();
    });

    // Modal functionality
    closeReportModal.addEventListener('click', () => {
        healingReportModal.style.display = 'none';
    });

    healingReportModal.addEventListener('click', (e) => {
        if (e.target === healingReportModal) {
            healingReportModal.style.display = 'none';
        }
    });

    // Load healing configuration
    async function loadHealingConfig() {
        try {
            const response = await fetch('/api/healing/status');
            if (response.ok) {
                const data = await response.json();
                const config = data.configuration;
                
                healingEnabledCheckbox.checked = data.healing_enabled;
                maxAttemptsInput.value = config.max_attempts_per_locator;
                sessionTimeoutInput.value = config.chrome_session_timeout;
                healingTimeoutInput.value = Math.round(config.healing_timeout / 60); // Convert to minutes
                confidenceThresholdSlider.value = config.confidence_threshold;
                confidenceValue.textContent = config.confidence_threshold;
            }
        } catch (error) {
            console.error('Failed to load healing config:', error);
        }
    }

    // Load healing dashboard data
    async function loadHealingDashboard() {
        await Promise.all([
            loadHealingStatistics(),
            loadHealingReports()
        ]);
    }

    async function loadHealingStatistics() {
        try {
            const response = await fetch('/api/healing/statistics');
            if (response.ok) {
                const data = await response.json();
                const stats = data.statistics;
                
                totalAttemptsEl.textContent = stats.total_attempts;
                successRateEl.textContent = `${stats.success_rate.toFixed(1)}%`;
                avgHealingTimeEl.textContent = `${stats.average_healing_time.toFixed(1)}s`;
                last24hAttemptsEl.textContent = stats.last_24h_attempts;
            }
        } catch (error) {
            console.error('Failed to load healing statistics:', error);
        }
    }

    async function loadHealingReports() {
        try {
            const response = await fetch('/api/healing/reports');
            if (response.ok) {
                const data = await response.json();
                displayHealingReports(data.reports);
            }
        } catch (error) {
            console.error('Failed to load healing reports:', error);
        }
    }

    function displayHealingReports(reports) {
        if (reports.length === 0) {
            reportsTableBody.innerHTML = `
                <tr class="empty-row">
                    <td colspan="7">
                        <div class="empty-state">
                            <div class="empty-state-icon">📋</div>
                            <p>No healing reports available</p>
                        </div>
                    </td>
                </tr>
            `;
            return;
        }

        reportsTableBody.innerHTML = reports.map(report => `
            <tr>
                <td>${report.run_id}</td>
                <td>${report.test_file}</td>
                <td>${report.total_attempts}</td>
                <td>${report.successful_healings}</td>
                <td>${report.failed_healings}</td>
                <td>${new Date(report.generated_at).toLocaleDateString()}</td>
                <td>
                    <button class="btn btn-secondary btn-small" onclick="viewHealingReport('${report.run_id}')">
                        View Report
                    </button>
                </td>
            </tr>
        `).join('');
    }

    // Global function for viewing healing reports
    window.viewHealingReport = async function(runId) {
        try {
            const response = await fetch(`/api/healing/reports/${runId}`);
            if (response.ok) {
                const data = await response.json();
                displayHealingReportModal(data.report);
            } else {
                throw new Error('Failed to load report');
            }
        } catch (error) {
            showNotification('Failed to load healing report: ' + error.message, 'error');
        }
    };

    function displayHealingReportModal(report) {
        const content = `
            <div class="report-section">
                <h3>Report Summary</h3>
                <div class="report-grid">
                    <div class="report-card">
                        <div class="report-card-title">Total Attempts</div>
                        <div class="report-card-value">${report.total_attempts}</div>
                    </div>
                    <div class="report-card">
                        <div class="report-card-title">Successful</div>
                        <div class="report-card-value">${report.successful_healings}</div>
                    </div>
                    <div class="report-card">
                        <div class="report-card-title">Failed</div>
                        <div class="report-card-value">${report.failed_healings}</div>
                    </div>
                    <div class="report-card">
                        <div class="report-card-title">Total Time</div>
                        <div class="report-card-value">${report.total_time.toFixed(1)}s</div>
                    </div>
                </div>
            </div>
            
            <div class="report-section">
                <h3>Healing Attempts</h3>
                ${report.healing_attempts.map(attempt => `
                    <div class="healing-attempt">
                        <div class="attempt-header">
                            <div class="attempt-title">${attempt.test_case}</div>
                            <div class="attempt-status ${attempt.status}">${attempt.status}</div>
                        </div>
                        <div class="locator-comparison">
                            <div class="locator-box">
                                <div class="locator-label">Original Locator</div>
                                <div class="locator-value">${attempt.original_locator}</div>
                            </div>
                            <div class="locator-box">
                                <div class="locator-label">Healed Locator</div>
                                <div class="locator-value">${attempt.healed_locator || 'N/A'}</div>
                            </div>
                        </div>
                        ${attempt.error_message ? `<div style="margin-top: 0.75rem; color: var(--error); font-size: 0.875rem;">${attempt.error_message}</div>` : ''}
                    </div>
                `).join('')}
            </div>
        `;
        
        healingReportContent.innerHTML = content;
        healingReportModal.style.display = 'flex';
    }

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
});
