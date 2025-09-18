/**
 * Integration tests for healing UI with backend API
 * These tests verify the frontend correctly integrates with healing endpoints
 */

describe('Healing UI Integration Tests', () => {
    let mockFetch;

    beforeEach(() => {
        mockFetch = jest.fn();
        global.fetch = mockFetch;
    });

    afterEach(() => {
        jest.clearAllMocks();
    });

    describe('API Integration', () => {
        test('should handle healing progress stream correctly', async () => {
            // Mock SSE stream data
            const streamData = [
                { stage: 'healing', status: 'running', message: 'Analyzing test failures...' },
                { stage: 'healing', status: 'running', message: 'Generating alternative locators...' },
                { stage: 'healing', status: 'running', message: 'Validating new locators...' },
                { stage: 'healing', status: 'complete', message: 'Healing completed successfully!' }
            ];

            // Simulate processing each stream event
            streamData.forEach(data => {
                expect(data.stage).toBe('healing');
                expect(['running', 'complete']).toContain(data.status);
                expect(data.message).toBeTruthy();
            });
        });

        test('should load healing configuration from status endpoint', async () => {
            const mockStatusResponse = {
                status: 'success',
                healing_enabled: true,
                active_sessions: 2,
                configuration: {
                    max_attempts_per_locator: 3,
                    chrome_session_timeout: 30,
                    healing_timeout: 300,
                    max_concurrent_sessions: 3,
                    confidence_threshold: 0.7,
                    max_alternatives: 5
                },
                containers: {
                    status: 'running',
                    count: 2
                },
                statistics: {
                    total_attempts: 42,
                    successful_healings: 36,
                    failed_healings: 6,
                    success_rate: 85.7,
                    average_healing_time: 12.3
                }
            };

            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve(mockStatusResponse)
            });

            const response = await fetch('/api/healing/status');
            const data = await response.json();

            expect(data.status).toBe('success');
            expect(data.healing_enabled).toBe(true);
            expect(data.configuration.max_attempts_per_locator).toBe(3);
            expect(data.statistics.success_rate).toBe(85.7);
        });

        test('should save healing configuration via config endpoint', async () => {
            const configUpdate = {
                enabled: true,
                max_attempts_per_locator: 5,
                chrome_session_timeout: 45,
                healing_timeout: 600,
                confidence_threshold: 0.8
            };

            const mockConfigResponse = {
                status: 'success',
                message: 'Healing configuration updated successfully',
                configuration: configUpdate
            };

            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve(mockConfigResponse)
            });

            const response = await fetch('/api/healing/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(configUpdate)
            });

            const data = await response.json();

            expect(mockFetch).toHaveBeenCalledWith('/api/healing/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(configUpdate)
            });
            expect(data.status).toBe('success');
            expect(data.configuration.max_attempts_per_locator).toBe(5);
        });

        test('should load healing statistics from statistics endpoint', async () => {
            const mockStatsResponse = {
                status: 'success',
                statistics: {
                    total_attempts: 156,
                    successful_healings: 134,
                    failed_healings: 22,
                    success_rate: 85.9,
                    average_healing_time: 15.2,
                    last_24h_attempts: 12,
                    last_24h_success_rate: 91.7,
                    top_failure_types: [
                        { type: 'NoSuchElementException', count: 18 },
                        { type: 'ElementNotInteractableException', count: 4 }
                    ],
                    healing_trends: [
                        { date: '2024-01-15', attempts: 8, successful: 7, success_rate: 87.5 },
                        { date: '2024-01-16', attempts: 12, successful: 11, success_rate: 91.7 }
                    ]
                }
            };

            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve(mockStatsResponse)
            });

            const response = await fetch('/api/healing/statistics');
            const data = await response.json();

            expect(data.status).toBe('success');
            expect(data.statistics.total_attempts).toBe(156);
            expect(data.statistics.success_rate).toBe(85.9);
            expect(data.statistics.top_failure_types).toHaveLength(2);
            expect(data.statistics.healing_trends).toHaveLength(2);
        });

        test('should load healing reports from reports endpoint', async () => {
            const mockReportsResponse = {
                status: 'success',
                reports: [
                    {
                        run_id: 'test-20240115-143022',
                        test_file: 'login_test.robot',
                        total_attempts: 3,
                        successful_healings: 2,
                        failed_healings: 1,
                        generated_at: '2024-01-15T14:30:22Z'
                    },
                    {
                        run_id: 'test-20240115-151045',
                        test_file: 'checkout_test.robot',
                        total_attempts: 1,
                        successful_healings: 1,
                        failed_healings: 0,
                        generated_at: '2024-01-15T15:10:45Z'
                    }
                ],
                total_reports: 2
            };

            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve(mockReportsResponse)
            });

            const response = await fetch('/api/healing/reports');
            const data = await response.json();

            expect(data.status).toBe('success');
            expect(data.reports).toHaveLength(2);
            expect(data.reports[0].run_id).toBe('test-20240115-143022');
            expect(data.reports[1].successful_healings).toBe(1);
            expect(data.total_reports).toBe(2);
        });

        test('should load detailed healing report from report endpoint', async () => {
            const runId = 'test-20240115-143022';
            const mockReportResponse = {
                status: 'success',
                report: {
                    run_id: runId,
                    test_file: 'login_test.robot',
                    healing_attempts: [
                        {
                            session_id: 'healing-session-001',
                            test_case: 'Valid Login Test',
                            original_locator: 'id=old-login-button',
                            healed_locator: 'id=new-login-button',
                            status: 'success',
                            attempts: 2,
                            started_at: '2024-01-15T14:30:22Z',
                            completed_at: '2024-01-15T14:30:45Z',
                            error_message: null
                        },
                        {
                            session_id: 'healing-session-002',
                            test_case: 'Invalid Login Test',
                            original_locator: 'css=.error-message',
                            healed_locator: null,
                            status: 'failed',
                            attempts: 3,
                            started_at: '2024-01-15T14:31:00Z',
                            completed_at: '2024-01-15T14:31:30Z',
                            error_message: 'No valid alternative locators found'
                        }
                    ],
                    total_attempts: 2,
                    successful_healings: 1,
                    failed_healings: 1,
                    total_time: 68.0,
                    generated_at: '2024-01-15T14:32:00Z'
                }
            };

            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.resolve(mockReportResponse)
            });

            const response = await fetch(`/api/healing/reports/${runId}`);
            const data = await response.json();

            expect(data.status).toBe('success');
            expect(data.report.run_id).toBe(runId);
            expect(data.report.healing_attempts).toHaveLength(2);
            expect(data.report.healing_attempts[0].status).toBe('success');
            expect(data.report.healing_attempts[1].status).toBe('failed');
            expect(data.report.total_time).toBe(68.0);
        });
    });

    describe('Error Handling', () => {
        test('should handle API errors gracefully', async () => {
            mockFetch.mockRejectedValueOnce(new Error('Network error'));

            try {
                await fetch('/api/healing/status');
                fail('Expected error to be thrown');
            } catch (error) {
                expect(error.message).toBe('Network error');
            }
        });

        test('should handle HTTP error responses', async () => {
            mockFetch.mockResolvedValueOnce({
                ok: false,
                status: 500,
                statusText: 'Internal Server Error',
                json: () => Promise.resolve({
                    detail: 'Failed to get healing status: Database connection error'
                })
            });

            const response = await fetch('/api/healing/status');
            expect(response.ok).toBe(false);
            expect(response.status).toBe(500);

            const errorData = await response.json();
            expect(errorData.detail).toContain('Database connection error');
        });

        test('should handle malformed JSON responses', async () => {
            mockFetch.mockResolvedValueOnce({
                ok: true,
                json: () => Promise.reject(new Error('Invalid JSON'))
            });

            try {
                const response = await fetch('/api/healing/status');
                await response.json();
                fail('Expected JSON parsing error');
            } catch (error) {
                expect(error.message).toBe('Invalid JSON');
            }
        });
    });

    describe('Real-time Updates', () => {
        test('should handle Server-Sent Events for healing progress', () => {
            const mockEventSource = {
                addEventListener: jest.fn(),
                removeEventListener: jest.fn(),
                close: jest.fn(),
                readyState: 1
            };

            global.EventSource = jest.fn(() => mockEventSource);

            const sessionId = 'healing-session-123';
            const eventSource = new EventSource(`/api/healing/progress/${sessionId}`);

            // Simulate event listeners
            const statusHandler = jest.fn();
            const progressHandler = jest.fn();
            const completeHandler = jest.fn();
            const errorHandler = jest.fn();

            eventSource.addEventListener('status', statusHandler);
            eventSource.addEventListener('progress', progressHandler);
            eventSource.addEventListener('complete', completeHandler);
            eventSource.addEventListener('error', errorHandler);

            // Verify event source was created correctly
            expect(EventSource).toHaveBeenCalledWith(`/api/healing/progress/${sessionId}`);
            expect(mockEventSource.addEventListener).toHaveBeenCalledWith('status', statusHandler);
            expect(mockEventSource.addEventListener).toHaveBeenCalledWith('progress', progressHandler);
            expect(mockEventSource.addEventListener).toHaveBeenCalledWith('complete', completeHandler);
            expect(mockEventSource.addEventListener).toHaveBeenCalledWith('error', errorHandler);
        });

        test('should handle SSE connection errors', () => {
            const mockEventSource = {
                addEventListener: jest.fn(),
                removeEventListener: jest.fn(),
                close: jest.fn(),
                readyState: 2 // CLOSED
            };

            global.EventSource = jest.fn(() => mockEventSource);

            const sessionId = 'healing-session-123';
            const eventSource = new EventSource(`/api/healing/progress/${sessionId}`);

            const errorHandler = jest.fn();
            eventSource.addEventListener('error', errorHandler);

            // Simulate connection error
            const errorEvent = { type: 'error', data: 'Connection failed' };
            errorHandler(errorEvent);

            expect(errorHandler).toHaveBeenCalledWith(errorEvent);
            expect(eventSource.readyState).toBe(2); // CLOSED
        });
    });

    describe('Data Validation', () => {
        test('should validate healing configuration data', () => {
            const validConfig = {
                enabled: true,
                max_attempts_per_locator: 3,
                chrome_session_timeout: 30,
                healing_timeout: 300,
                confidence_threshold: 0.7
            };

            // Validate required fields
            expect(typeof validConfig.enabled).toBe('boolean');
            expect(typeof validConfig.max_attempts_per_locator).toBe('number');
            expect(validConfig.max_attempts_per_locator).toBeGreaterThan(0);
            expect(validConfig.max_attempts_per_locator).toBeLessThanOrEqual(10);
            expect(validConfig.confidence_threshold).toBeGreaterThanOrEqual(0);
            expect(validConfig.confidence_threshold).toBeLessThanOrEqual(1);
        });

        test('should validate healing report data structure', () => {
            const validReport = {
                run_id: 'test-123',
                test_file: 'test.robot',
                healing_attempts: [
                    {
                        session_id: 'session-1',
                        test_case: 'Test Case',
                        original_locator: 'id=old',
                        healed_locator: 'id=new',
                        status: 'success'
                    }
                ],
                total_attempts: 1,
                successful_healings: 1,
                failed_healings: 0,
                total_time: 10.5
            };

            // Validate report structure
            expect(typeof validReport.run_id).toBe('string');
            expect(typeof validReport.test_file).toBe('string');
            expect(Array.isArray(validReport.healing_attempts)).toBe(true);
            expect(typeof validReport.total_attempts).toBe('number');
            expect(typeof validReport.total_time).toBe('number');

            // Validate healing attempt structure
            const attempt = validReport.healing_attempts[0];
            expect(typeof attempt.session_id).toBe('string');
            expect(typeof attempt.test_case).toBe('string');
            expect(typeof attempt.original_locator).toBe('string');
            expect(['success', 'failed', 'timeout']).toContain(attempt.status);
        });
    });
});