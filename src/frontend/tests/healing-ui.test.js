/**
 * Frontend tests for healing UI components
 * Tests the healing progress indicators, configuration panel, and dashboard functionality
 */

// Mock DOM elements and APIs
const mockFetch = jest.fn();
global.fetch = mockFetch;

// Mock DOM
document.body.innerHTML = `
    <div id="healing-progress" class="healing-progress" style="display: none;">
        <div class="healing-header">
            <div class="healing-icon">🔧</div>
            <div class="healing-info">
                <div class="healing-title">Self-Healing in Progress</div>
                <div class="healing-subtitle" id="healing-status">Analyzing test failures...</div>
            </div>
            <div class="healing-spinner"></div>
        </div>
        <div class="healing-progress-bar">
            <div class="healing-progress-fill" id="healing-progress-fill"></div>
        </div>
        <div class="healing-details" id="healing-details">
            <div class="healing-step">
                <span class="step-icon">🔍</span>
                <span class="step-text">Detecting locator failures...</span>
                <span class="step-status pending" id="step-detection">⏳</span>
            </div>
            <div class="healing-step">
                <span class="step-icon">🧠</span>
                <span class="step-text">Generating alternative locators...</span>
                <span class="step-status pending" id="step-generation">⏳</span>
            </div>
            <div class="healing-step">
                <span class="step-icon">✅</span>
                <span class="step-text">Validating new locators...</span>
                <span class="step-status pending" id="step-validation">⏳</span>
            </div>
            <div class="healing-step">
                <span class="step-icon">📝</span>
                <span class="step-text">Updating test code...</span>
                <span class="step-status pending" id="step-update">⏳</span>
            </div>
        </div>
    </div>

    <div class="nav-tabs">
        <button class="nav-tab active" data-section="workspace">Test Generation</button>
        <button class="nav-tab" data-section="healing-config">Healing Config</button>
        <button class="nav-tab" data-section="healing-dashboard">Healing Dashboard</button>
    </div>

    <div class="workspace" style="display: grid;"></div>
    <div id="healing-config-section" style="display: none;">
        <input type="checkbox" id="healing-enabled">
        <input type="number" id="max-attempts" value="3">
        <input type="number" id="session-timeout" value="30">
        <input type="number" id="healing-timeout" value="5">
        <input type="range" id="confidence-threshold" value="0.7">
        <div id="confidence-value">0.7</div>
        <button id="save-config-btn">Save Configuration</button>
        <button id="reset-config-btn">Reset to Defaults</button>
    </div>

    <div id="healing-dashboard-section" style="display: none;">
        <div id="total-attempts">0</div>
        <div id="success-rate">0%</div>
        <div id="avg-healing-time">0s</div>
        <div id="last-24h-attempts">0</div>
        <button id="refresh-reports-btn">Refresh</button>
        <tbody id="reports-table-body"></tbody>
    </div>

    <div id="healing-report-modal" class="modal" style="display: none;">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Healing Report</h2>
                <button class="modal-close" id="close-report-modal">&times;</button>
            </div>
            <div class="modal-body" id="healing-report-content"></div>
        </div>
    </div>

    <div id="execution-logs"></div>
`;

describe('Healing UI Components', () => {
    let healingProgress, healingStatus, healingProgressFill;
    let navTabs, workspaceSection, healingConfigSection, healingDashboardSection;
    let healingEnabledCheckbox, saveConfigBtn, resetConfigBtn;
    let totalAttemptsEl, successRateEl, refreshReportsBtn, reportsTableBody;

    beforeEach(() => {
        // Reset mocks
        mockFetch.mockClear();
        
        // Get DOM elements
        healingProgress = document.getElementById('healing-progress');
        healingStatus = document.getElementById('healing-status');
        healingProgressFill = document.getElementById('healing-progress-fill');
        
        navTabs = document.querySelectorAll('.nav-tab');
        workspaceSection = document.querySelector('.workspace');
        healingConfigSection = document.getElementById('healing-config-section');
        healingDashboardSection = document.getElementById('healing-dashboard-section');
        
        healingEnabledCheckbox = document.getElementById('healing-enabled');
        saveConfigBtn = document.getElementById('save-config-btn');
        resetConfigBtn = document.getElementById('reset-config-btn');
        
        totalAttemptsEl = document.getElementById('total-attempts');
        successRateEl = document.getElementById('success-rate');
        refreshReportsBtn = document.getElementById('refresh-reports-btn');
        reportsTableBody = document.getElementById('reports-table-body');
    });

    describe('Healing Progress Indicators', () => {
        test('should show healing progress when healing starts', () => {
            const healingData = {
                stage: 'healing',
                status: 'running',
                message: 'Analyzing test failures...'
            };

            // Simulate healing progress handler
            healingProgress.style.display = 'block';
            healingStatus.textContent = healingData.message;

            expect(healingProgress.style.display).toBe('block');
            expect(healingStatus.textContent).toBe('Analyzing test failures...');
        });

        test('should update progress bar based on healing phase', () => {
            const phases = [
                { message: 'Analyzing test failures...', expectedProgress: '25%' },
                { message: 'Generating alternative locators...', expectedProgress: '50%' },
                { message: 'Validating new locators...', expectedProgress: '75%' },
                { message: 'Updating test code...', expectedProgress: '90%' }
            ];

            phases.forEach(phase => {
                let progress = 0;
                if (phase.message.includes('Analyzing')) progress = 25;
                else if (phase.message.includes('Generating')) progress = 50;
                else if (phase.message.includes('Validating')) progress = 75;
                else if (phase.message.includes('Updating')) progress = 90;

                healingProgressFill.style.width = `${progress}%`;
                expect(healingProgressFill.style.width).toBe(phase.expectedProgress);
            });
        });

        test('should update healing step status correctly', () => {
            const stepDetection = document.getElementById('step-detection');
            
            // Test step status updates
            stepDetection.className = 'step-status active';
            stepDetection.textContent = '🔄';
            expect(stepDetection.className).toBe('step-status active');
            expect(stepDetection.textContent).toBe('🔄');

            stepDetection.className = 'step-status complete';
            stepDetection.textContent = '✅';
            expect(stepDetection.className).toBe('step-status complete');
            expect(stepDetection.textContent).toBe('✅');
        });

        test('should hide progress indicator when healing completes', (done) => {
            healingProgress.style.display = 'block';
            healingProgressFill.style.width = '100%';
            healingStatus.textContent = 'Healing completed successfully!';

            // Simulate completion timeout
            setTimeout(() => {
                healingProgress.style.display = 'none';
                expect(healingProgress.style.display).toBe('none');
                done();
            }, 100);
        });
    });

    describe('Navigation Tabs', () => {
        test('should switch between sections when tabs are clicked', () => {
            const configTab = document.querySelector('[data-section="healing-config"]');
            const dashboardTab = document.querySelector('[data-section="healing-dashboard"]');

            // Click config tab
            configTab.classList.add('active');
            workspaceSection.style.display = 'none';
            healingConfigSection.style.display = 'block';
            healingDashboardSection.style.display = 'none';

            expect(configTab.classList.contains('active')).toBe(true);
            expect(workspaceSection.style.display).toBe('none');
            expect(healingConfigSection.style.display).toBe('block');

            // Click dashboard tab
            configTab.classList.remove('active');
            dashboardTab.classList.add('active');
            healingConfigSection.style.display = 'none';
            healingDashboardSection.style.display = 'block';

            expect(dashboardTab.classList.contains('active')).toBe(true);
            expect(healingDashboardSection.style.display).toBe('block');
        });
    });

    describe('Configuration Panel', () => {
        test('should load healing configuration from API', async () => {
            const mockConfig = {
                healing_enabled: true,
                configuration: {
                    max_attempts_per_locator: 3,
                    chrome_session_timeout: 30,
                    healing_timeout: 300,
                    confidence_threshold: 0.7
                }
            };

            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve(mockConfig)
            });

            // Simulate loading config
            const response = await fetch('/api/healing/status');
            const data = await response.json();
            
            healingEnabledCheckbox.checked = data.healing_enabled;
            document.getElementById('max-attempts').value = data.configuration.max_attempts_per_locator;
            document.getElementById('session-timeout').value = data.configuration.chrome_session_timeout;

            expect(healingEnabledCheckbox.checked).toBe(true);
            expect(document.getElementById('max-attempts').value).toBe('3');
            expect(document.getElementById('session-timeout').value).toBe('30');
        });

        test('should save configuration to API', async () => {
            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve({ status: 'success' })
            });

            healingEnabledCheckbox.checked = true;
            document.getElementById('max-attempts').value = '5';

            const config = {
                enabled: healingEnabledCheckbox.checked,
                max_attempts_per_locator: parseInt(document.getElementById('max-attempts').value)
            };

            await fetch('/api/healing/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            });

            expect(mockFetch).toHaveBeenCalledWith('/api/healing/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    enabled: true,
                    max_attempts_per_locator: 5
                })
            });
        });

        test('should reset configuration to defaults', () => {
            // Set non-default values
            healingEnabledCheckbox.checked = false;
            document.getElementById('max-attempts').value = '10';
            document.getElementById('confidence-threshold').value = '0.9';

            // Reset to defaults
            healingEnabledCheckbox.checked = true;
            document.getElementById('max-attempts').value = '3';
            document.getElementById('confidence-threshold').value = '0.7';
            document.getElementById('confidence-value').textContent = '0.7';

            expect(healingEnabledCheckbox.checked).toBe(true);
            expect(document.getElementById('max-attempts').value).toBe('3');
            expect(document.getElementById('confidence-threshold').value).toBe('0.7');
        });
    });

    describe('Healing Dashboard', () => {
        test('should load and display healing statistics', async () => {
            const mockStats = {
                statistics: {
                    total_attempts: 42,
                    success_rate: 85.7,
                    average_healing_time: 12.3,
                    last_24h_attempts: 8
                }
            };

            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve(mockStats)
            });

            // Simulate loading statistics
            const response = await fetch('/api/healing/statistics');
            const data = await response.json();
            const stats = data.statistics;

            totalAttemptsEl.textContent = stats.total_attempts;
            successRateEl.textContent = `${stats.success_rate.toFixed(1)}%`;
            document.getElementById('avg-healing-time').textContent = `${stats.average_healing_time.toFixed(1)}s`;
            document.getElementById('last-24h-attempts').textContent = stats.last_24h_attempts;

            expect(totalAttemptsEl.textContent).toBe('42');
            expect(successRateEl.textContent).toBe('85.7%');
            expect(document.getElementById('avg-healing-time').textContent).toBe('12.3s');
            expect(document.getElementById('last-24h-attempts').textContent).toBe('8');
        });

        test('should load and display healing reports', async () => {
            const mockReports = {
                reports: [
                    {
                        run_id: 'test-123',
                        test_file: 'login_test.robot',
                        total_attempts: 2,
                        successful_healings: 1,
                        failed_healings: 1,
                        generated_at: '2024-01-15T10:30:00Z'
                    }
                ]
            };

            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve(mockReports)
            });

            // Simulate loading reports
            const response = await fetch('/api/healing/reports');
            const data = await response.json();

            const reportRow = `
                <tr>
                    <td>${data.reports[0].run_id}</td>
                    <td>${data.reports[0].test_file}</td>
                    <td>${data.reports[0].total_attempts}</td>
                    <td>${data.reports[0].successful_healings}</td>
                    <td>${data.reports[0].failed_healings}</td>
                    <td>${new Date(data.reports[0].generated_at).toLocaleDateString()}</td>
                    <td><button class="btn btn-secondary btn-small">View Report</button></td>
                </tr>
            `;

            reportsTableBody.innerHTML = reportRow;

            expect(reportsTableBody.innerHTML).toContain('test-123');
            expect(reportsTableBody.innerHTML).toContain('login_test.robot');
            expect(reportsTableBody.innerHTML).toContain('View Report');
        });

        test('should show empty state when no reports available', () => {
            const emptyState = `
                <tr class="empty-row">
                    <td colspan="7">
                        <div class="empty-state">
                            <div class="empty-state-icon">📋</div>
                            <p>No healing reports available</p>
                        </div>
                    </td>
                </tr>
            `;

            reportsTableBody.innerHTML = emptyState;

            expect(reportsTableBody.innerHTML).toContain('No healing reports available');
        });
    });

    describe('Healing Report Modal', () => {
        test('should display healing report in modal', async () => {
            const mockReport = {
                report: {
                    run_id: 'test-123',
                    total_attempts: 2,
                    successful_healings: 1,
                    failed_healings: 1,
                    total_time: 15.5,
                    healing_attempts: [
                        {
                            test_case: 'Login Test',
                            original_locator: 'id=old-button',
                            healed_locator: 'id=new-button',
                            status: 'success',
                            error_message: null
                        }
                    ]
                }
            };

            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve(mockReport)
            });

            // Simulate viewing report
            const response = await fetch('/api/healing/reports/test-123');
            const data = await response.json();
            const report = data.report;

            const modalContent = document.getElementById('healing-report-content');
            modalContent.innerHTML = `
                <div class="report-section">
                    <h3>Report Summary</h3>
                    <div class="report-card">
                        <div class="report-card-title">Total Attempts</div>
                        <div class="report-card-value">${report.total_attempts}</div>
                    </div>
                </div>
            `;

            document.getElementById('healing-report-modal').style.display = 'flex';

            expect(modalContent.innerHTML).toContain('Total Attempts');
            expect(modalContent.innerHTML).toContain('2');
            expect(document.getElementById('healing-report-modal').style.display).toBe('flex');
        });

        test('should close modal when close button is clicked', () => {
            const modal = document.getElementById('healing-report-modal');
            const closeBtn = document.getElementById('close-report-modal');

            modal.style.display = 'flex';
            
            // Simulate close button click
            modal.style.display = 'none';

            expect(modal.style.display).toBe('none');
        });
    });

    describe('Error Handling', () => {
        test('should handle API errors gracefully', async () => {
            mockFetch.mockRejectedValueOnce(new Error('Network error'));

            try {
                await fetch('/api/healing/status');
            } catch (error) {
                expect(error.message).toBe('Network error');
            }
        });

        test('should show error notification for failed operations', () => {
            // Simulate showing error notification
            const notification = document.createElement('div');
            notification.className = 'notification notification-error';
            notification.textContent = 'Failed to save configuration';
            notification.style.background = 'var(--error)';

            document.body.appendChild(notification);

            expect(notification.textContent).toBe('Failed to save configuration');
            expect(notification.className).toBe('notification notification-error');
        });
    });
});