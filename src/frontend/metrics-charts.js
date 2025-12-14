// Enhanced Multi-Chart System for Metrics Dashboard
// This file contains all chart rendering logic with dropdown support

// Chart instances
let chart1, chart2, chart3, chart4;
let currentChartData = {};

// Initialize chart dropdowns
function initializeChartDropdowns() {
    document.getElementById('chart1-select').addEventListener('change', () => renderChart1());
    document.getElementById('chart2-select').addEventListener('change', () => renderChart2());
    document.getElementById('chart3-select').addEventListener('change', () => renderChart3());
    document.getElementById('chart4-select').addEventListener('change', () => renderChart4());
}

// Process and store all chart data
function processChartData(runs) {
    if (!runs || runs.length === 0) return null;
    
    const sortedRuns = [...runs].reverse(); // Oldest first for time series
    
    // Extract URL domains
    const getDomain = (url) => {
        try {
            return new URL(url).hostname.replace('www.', '');
        } catch {
            return 'unknown';
        }
    };
    
    // Group by domain
    const domainStats = {};
    runs.forEach(r => {
        const domain = getDomain(r.url ||'');
        if (!domainStats[domain]) {
            domainStats[domain] = { count: 0, totalCost: 0, totalTime: 0, successCount: 0 };
        }
        domainStats[domain].count++;
        domainStats[domain].totalCost += r.total_cost || 0;
        domainStats[domain].totalTime += r.execution_time || 0;
        if (r.success_rate >= 1.0) domainStats[domain].successCount++;
    });
    
    return {
        sortedRuns,
        labels: sortedRuns.map(r => formatDateForTooltip(r.timestamp)),
        executionTimes: sortedRuns.map(r => r.execution_time),
        llmCalls: sortedRuns.map(r => r.total_llm_calls),
        costs: sortedRuns.map(r => r.total_cost),
        elements: sortedRuns.map(r => r.total_elements || 0),
        successRates: sortedRuns.map(r => (r.success_rate || 0) * 100),
        successElements: sortedRuns.map(r => r.successful_elements || 0),
        failedElements: sortedRuns.map(r => r.failed_elements || 0),
        crewaiCosts: sortedRuns.map(r => r.crewai_cost || 0),
        browserCosts: sortedRuns.map(r => r.browser_use_cost || 0),
        crewaiCalls: sortedRuns.map(r => r.crewai_llm_calls || 0),
        browserCalls: sortedRuns.map(r => r.browser_use_llm_calls || 0),
        promptTokens: sortedRuns.map(r => r.crewai_prompt_tokens || 0),
        completionTokens: sortedRuns.map(r => r.crewai_completion_tokens || 0),
        llmPerElement: sortedRuns.map(r => r.avg_llm_calls_per_element || 0),
        costPerElement: sortedRuns.map(r => r.avg_cost_per_element || 0),
        urls: sortedRuns.map(r => r.url || 'Unknown'),
        // Aggregates
        totalCrewCost: runs.reduce((acc, r) => acc + (r.crewai_cost || 0), 0),
        totalBrowserCost: runs.reduce((acc, r) => acc + (r.browser_use_cost || 0), 0),
        totalPromptTokens: runs.reduce((acc, r) => acc + (r.crewai_prompt_tokens || 0), 0),
        totalCompletionTokens: runs.reduce((acc, r) => acc + (r.crewai_completion_tokens || 0), 0),
        domainStats
    };
}

// Get theme colors
function getThemeColors() {
    const isNeo = document.documentElement.getAttribute('data-theme') === 'neobrutalism';
    return {
        primary: isNeo ? '#ccff00' : '#3b82f6',
        secondary: isNeo ? '#ff00ff' : '#64748b',
        success: isNeo ? '#00ff9d' : '#10b981',
        warning: isNeo ? '#ffff00' : '#f59e0b',
        danger: isNeo ? '#ff0066' : '#ef4444',
        info: isNeo ? '#00ccff' : '#06b6d4',
        purple: isNeo ? '#cc00ff' : '#8b5cf6',
        orange: isNeo ? '#ff9900' : '#f97316',
        pink: isNeo ? '#ff66ff' : '#ec4899',
        teal: isNeo ? '#00ffcc' : '#14b8a6',
        gridColor: isNeo ? '#000000' : '#e2e8f0',
        borderWidth: isNeo ? 2 : 1
    };
}

// Common chart options
function getCommonOptions(title = '') {
    const colors = getThemeColors();
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { display: true, position: 'bottom' },
            title: { display: !!title, text: title }
        },
        scales: {
            y: { 
                grid: { color: colors.gridColor, drawBorder: false }, 
                ticks: { color: '#888' }
            },
            x: { display: false }
        }
    };
}

// ============= CHART 1: Primary Metrics =============
function renderChart1() {
    if (!currentChartData.labels) return;
    
    const select = document.getElementById('chart1-select');
    const chartType = select.value;
    const canvas = document.getElementById('chart1');
    const ctx = canvas.getContext('2d');
    const colors = getThemeColors();
    
    if (chart1) chart1.destroy();
    
    const data = currentChartData;
    
    switch(chartType) {
        case 'execution-llm':
            chart1 = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: data.labels,
                    datasets: [
                        {
                            label: 'Execution Time (s)',
                            data: data.executionTimes,
                            borderColor: colors.primary,
                            backgroundColor: `${colors.primary}20`,
                            borderWidth: colors.borderWidth + 1,
                            tension: 0.4,
                            fill: true,
                            yAxisID: 'y'
                        },
                        {
                            label: 'LLM Calls',
                            data: data.llmCalls,
                            borderColor: colors.secondary,
                            borderDash: [5, 5],
                            borderWidth: colors.borderWidth,
                            tension: 0.4,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        y: {
                            type: 'linear',
                            display: true,
                            position: 'left',
                            title: { display: true, text: 'Time (s)' },
                            grid: { color: colors.gridColor },
                            ticks: { color: '#888' }
                        },
                        y1: {
                            type: 'linear',
                            display: true,
                            position: 'right',
                            title: { display: true, text: 'LLM Calls' },
                            grid: { drawOnChartArea: false },
                            ticks: { color: '#888' }
                        },
                        x: { display: false }
                    }
                }
            });
            break;
            
        case 'cost-trend':
            chart1 = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: data.labels,
                    datasets: [{
                        label: 'Cost ($)',
                        data: data.costs,
                        borderColor: colors.success,
                        backgroundColor: `${colors.success}20`,
                        borderWidth: colors.borderWidth + 1,
                        tension: 0.4,
                        fill: true
                    }]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        y: {
                            ...getCommonOptions().scales.y,
                            ticks: {
                                color: '#888',
                                callback: (value) => '$' + value.toFixed(4)
                            }
                        },
                        x: { display: false }
                    }
                }
            });
            break;
            
        case 'success-trend':
            chart1 = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: data.labels,
                    datasets: [{
                        label: 'Success Rate (%)',
                        data: data.successRates,
                        borderColor: colors.success,
                        backgroundColor: `${colors.success}20`,
                        borderWidth: colors.borderWidth + 1,
                        tension: 0.4,
                        fill: true
                    }]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        y: {
                            ...getCommonOptions().scales.y,
                            min: 0,
                            max: 100,
                            ticks: {
                                color: '#888',
                                callback: (value) => value + '%'
                            }
                        },
                        x: { display: false }
                    }
                }
            });
            break;
            
        case 'multi-metric':
            // Normalize all metrics to 0-100 scale for comparison
            const normalize = (arr) => {
                const max = Math.max(...arr);
                return max > 0 ? arr.map(v => (v / max) * 100) : arr;
            };
            
            chart1 = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: data.labels,
                    datasets: [
                        {
                            label: 'Cost (normalized)',
                            data: normalize(data.costs),
                            borderColor: colors.primary,
                            borderWidth: colors.borderWidth,
                            tension: 0.4
                        },
                        {
                            label: 'Time (normalized)',
                            data: normalize(data.executionTimes),
                            borderColor: colors.secondary,
                            borderWidth: colors.borderWidth,
                            tension: 0.4
                        },
                        {
                            label: 'LLM Calls (normalized)',
                            data: normalize(data.llmCalls),
                            borderColor: colors.warning,
                            borderWidth: colors.borderWidth,
                            tension: 0.4
                        },
                        {
                            label: 'Success Rate',
                            data: data.successRates,
                            borderColor: colors.success,
                            borderWidth: colors.borderWidth,
                            tension: 0.4
                        }
                    ]
                },
                options: getCommonOptions()
            });
            break;
            
        case 'token-usage':
            chart1 = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.labels,
                    datasets: [
                        {
                            label: 'Prompt Tokens',
                            data: data.promptTokens,
                            backgroundColor: colors.primary,
                            borderColor: colors.borderWidth > 1 ? '#000' : colors.primary,
                            borderWidth: colors.borderWidth
                        },
                        {
                            label: 'Completion Tokens',
                            data: data.completionTokens,
                            backgroundColor: colors.secondary,
                            borderColor: colors.borderWidth > 1 ? '#000' : colors.secondary,
                            borderWidth: colors.borderWidth
                        }
                    ]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        ...getCommonOptions().scales,
                        x: { stacked: true, display: false },
                        y: { stacked: true, ...getCommonOptions().scales.y }
                    }
                }
            });
            break;
            
        case 'cost-efficiency':
            chart1 = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.labels,
                    datasets: [{
                        label: 'Cost per Element ($)',
                        data: data.costPerElement,
                        backgroundColor: colors.warning,
                        borderColor: colors.borderWidth > 1 ? '#000' : colors.warning,
                        borderWidth: colors.borderWidth
                    }]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        y: {
                            ...getCommonOptions().scales.y,
                            ticks: {
                                color: '#888',
                                callback: (value) => '$' + value.toFixed(4)
                            }
                        },
                        x: { display: false }
                    }
                }
            });
            break;
    }
}

// ============= CHART 2: Cost Analysis =============
function renderChart2() {
    if (!currentChartData.labels) return;
    
    const select = document.getElementById('chart2-select');
    const chartType = select.value;
    const canvas = document.getElementById('chart2');
    const ctx = canvas.getContext('2d');
    const colors = getThemeColors();
    
    if (chart2) chart2.destroy();
    
    const data = currentChartData;
    
    switch(chartType) {
        case 'cost-breakdown':
            chart2 = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    labels: ['Browser Actions', 'CrewAI Logic'],
                    datasets: [{
                        data: [data.totalBrowserCost, data.totalCrewCost],
                        backgroundColor: [colors.primary, colors.secondary],
                        borderColor: colors.borderWidth > 1 ? '#000' : '#fff',
                        borderWidth: colors.borderWidth
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: true, position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label: (context) => `${context.label}: $${context.parsed.toFixed(4)}`
                            }
                        }
                    }
                }
            });
            break;
            
        case 'cost-stacked':
            chart2 = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.labels,
                    datasets: [
                        {
                            label: 'Browser Cost',
                            data: data.browserCosts,
                            backgroundColor: colors.primary,
                            borderWidth: colors.borderWidth
                        },
                        {
                            label: 'CrewAI Cost',
                            data: data.crewaiCosts,
                            backgroundColor: colors.secondary,
                            borderWidth: colors.borderWidth
                        }
                    ]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        ...getCommonOptions().scales,
                        x: { stacked: true, display: false },
                        y: { 
                            stacked: true, 
                            ...getCommonOptions().scales.y,
                            ticks: {
                                color: '#888',
                                callback: (value) => '$' + value.toFixed(4)
                            }
                        }
                    }
                }
            });
            break;
            
        case 'cost-comparison':
            chart2 = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: data.labels,
                    datasets: [
                        {
                            label: 'Browser Cost',
                            data: data.browserCosts,
                            borderColor: colors.primary,
                            backgroundColor: `${colors.primary}20`,
                            borderWidth: colors.borderWidth + 1,
                            tension: 0.4,
                            fill: true
                        },
                        {
                            label: 'CrewAI Cost',
                            data: data.crewaiCosts,
                            borderColor: colors.secondary,
                            backgroundColor: `${colors.secondary}20`,
                            borderWidth: colors.borderWidth + 1,
                            tension: 0.4,
                            fill: true
                        }
                    ]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        y: {
                            ...getCommonOptions().scales.y,
                            ticks: {
                                color: '#888',
                                callback: (value) => '$' + value.toFixed(4)
                            }
                        },
                        x: { display: false }
                    }
                }
            });
            break;
            
        case 'domain-cost':
            const domains = Object.keys(data.domainStats);
            const domainCosts = domains.map(d => data.domainStats[d].totalCost);
            
            chart2 = new Chart(ctx, {
                type: 'polarArea',
                data: {
                    labels: domains,
                    datasets: [{
                        data: domainCosts,
                        backgroundColor: [
                            `${colors.primary}80`,
                            `${colors.secondary}80`,
                            `${colors.success}80`,
                            `${colors.warning}80`,
                            `${colors.danger}80`,
                            `${colors.info}80`
                        ],
                        borderColor: colors.borderWidth > 1 ? '#000' : '#fff',
                        borderWidth: colors.borderWidth
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: true, position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label: (context) => `${context.label}: $${context.parsed.r.toFixed(4)}`
                            }
                        }
                    }
                }
            });
            break;
    }
}

// ============= CHART 3: Element & Performance =============
function renderChart3() {
    if (!currentChartData.labels) return;
    
    const select = document.getElementById('chart3-select');
    const chartType = select.value;
    const canvas = document.getElementById('chart3');
    const ctx = canvas.getContext('2d');
    const colors = getThemeColors();
    
    if (chart3) chart3.destroy();
    
    const data = currentChartData;
    
    switch(chartType) {
        case 'elements-bar':
            chart3 = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.labels,
                    datasets: [{
                        label: 'Elements Found',
                        data: data.elements,
                        backgroundColor: colors.warning,
                        borderColor: colors.borderWidth > 1 ? '#000' : colors.warning,
                        borderWidth: colors.borderWidth
                    }]
                },
                options: getCommonOptions()
            });
            break;
            
        case 'success-elements':
            chart3 = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.labels,
                    datasets: [
                        {
                            label: 'Successful',
                            data: data.successElements,
                            backgroundColor: colors.success,
                            borderWidth: colors.borderWidth
                        },
                        {
                            label: 'Failed',
                            data: data.failedElements,
                            backgroundColor: colors.danger,
                            borderWidth: colors.borderWidth
                        }
                    ]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        ...getCommonOptions().scales,
                        x: { stacked: true, display: false },
                        y: { stacked: true, ...getCommonOptions().scales.y }
                    }
                }
            });
            break;
            
        case 'element-efficiency':
            chart3 = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: data.labels,
                    datasets: [{
                        label: 'LLM Calls per Element',
                        data: data.llmPerElement,
                        borderColor: colors.info,
                        backgroundColor: `${colors.info}20`,
                        borderWidth: colors.borderWidth + 1,
                        tension: 0.4,
                        fill: true
                    }]
                },
                options: getCommonOptions()
            });
            break;
            
        case 'performance-scatter':
            chart3 = new Chart(ctx, {
                type: 'scatter',
                data: {
                    datasets: [{
                        label: 'Workflows',
                        data: data.sortedRuns.map(r => ({
                            x: r.execution_time,
                            y: r.total_cost * 1000 // Convert to cents for better visibility
                        })),
                        backgroundColor: colors.primary,
                        borderColor: colors.borderWidth > 1 ? '#000' : colors.primary,
                        borderWidth: colors.borderWidth,
                        pointRadius: 6
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: {
                            title: { display: true, text: 'Execution Time (s)' },
                            grid: { color: colors.gridColor },
                            ticks: { color: '#888' }
                        },
                        y: {
                            title: { display: true, text: 'Cost (cents)' },
                            grid: { color: colors.gridColor },
                            ticks: { 
                                color: '#888',
                                callback: (value) => '¢' + value.toFixed(1)
                            }
                        }
                    }
                }
            });
            break;
    }
}

// ============= CHART 4: Advanced Analysis =============
function renderChart4() {
    if (!currentChartData.labels) return;
    
    const select = document.getElementById('chart4-select');
    const chartType = select.value;
    const canvas = document.getElementById('chart4');
    const ctx = canvas.getContext('2d');
    const colors = getThemeColors();
    
    if (chart4) chart4.destroy();
    
    const data = currentChartData;
    
    switch(chartType) {
        case 'llm-distribution':
            // Create histogram bins
            const bins = [0, 10, 20, 30, 40, 50, 100];
            const binCounts = new Array(bins.length - 1).fill(0);
            
            data.llmCalls.forEach(calls => {
                for (let i = 0; i < bins.length - 1; i++) {
                    if (calls >= bins[i] && calls < bins[i + 1]) {
                        binCounts[i]++;
                        break;
                    }
                }
            });
            
            chart4 = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: bins.slice(0, -1).map((b, i) => `${b}-${bins[i+1]}`),
                    datasets: [{
                        label: 'Workflow Count',
                        data: binCounts,
                        backgroundColor: colors.purple,
                        borderColor: colors.borderWidth > 1 ? '#000' : colors.purple,
                        borderWidth: colors.borderWidth
                    }]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        x: { 
                            title: { display: true, text: 'LLM Call Range' },
                            grid: { color: colors.gridColor },
                            ticks: { color: '#888' }
                        },
                        y: { 
                            title: { display: true, text: 'Count' },
                            ...getCommonOptions().scales.y
                        }
                    }
                }
            });
            break;
            
        case 'workflow-radar':
            // Take average of last 5 workflows
            const recent = data.sortedRuns.slice(-5);
            const avgMetrics = {
                cost: recent.reduce((a, r) => a + r.total_cost, 0) / recent.length * 100,
                time: recent.reduce((a, r) => a + r.execution_time, 0) / recent.length / 10,
                llm: recent.reduce((a, r) => a + r.total_llm_calls, 0) / recent.length,
                elements: recent.reduce((a, r) => a + (r.total_elements || 0), 0) / recent.length * 10,
                success: recent.reduce((a, r) => a + (r.success_rate || 0), 0) / recent.length * 100
            };
            
            chart4 = new Chart(ctx, {
                type: 'radar',
                data: {
                    labels: ['Cost ($x100)', 'Time (/10s)', 'LLM Calls', 'Elements (x10)', 'Success Rate (%)'],
                    datasets: [{
                        label: 'Recent Performance',
                        data: [
                            avgMetrics.cost,
                            avgMetrics.time,
                            avgMetrics.llm,
                            avgMetrics.elements,
                            avgMetrics.success
                        ],
                        backgroundColor: `${colors.primary}40`,
                        borderColor: colors.primary,
                        borderWidth: colors.borderWidth + 1,
                        pointBackgroundColor: colors.primary,
                        pointBorderColor: colors.borderWidth > 1 ? '#000' : '#fff',
                        pointHoverBackgroundColor: '#fff',
                        pointHoverBorderColor: colors.primary
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: true, position: 'bottom' } },
                    scales: {
                        r: {
                            angleLines: { color: colors.gridColor },
                            grid: { color: colors.gridColor },
                            ticks: { color: '#888', backdropColor: 'transparent' }
                        }
                    }
                }
            });
            break;
            
        case 'domain-performance':
            const domains = Object.keys(data.domainStats);
            const domainSuccessRates = domains.map(d => {
                const stats = data.domainStats[d];
                return (stats.successCount / stats.count) * 100;
            });
            
            chart4 = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: domains,
                    datasets: [{
                        label: 'Success Rate (%)',
                        data: domainSuccessRates,
                        backgroundColor: domainSuccessRates.map(rate => 
                            rate >= 80 ? colors.success : rate >= 50 ? colors.warning : colors.danger
                        ),
                        borderColor: colors.borderWidth > 1 ? '#000' : 'transparent',
                        borderWidth: colors.borderWidth
                    }]
                },
                options: {
                    ...getCommonOptions(),
                    indexAxis: 'y',
                    scales: {
                        x: {
                            min: 0,
                            max: 100,
                            title: { display: true, text: 'Success Rate (%)' },
                            grid: { color: colors.gridColor },
                            ticks: { 
                                color: '#888',
                                callback: (value) => value + '%'
                            }
                        },
                        y: {
                            grid: { display: false },
                            ticks: { color: '#888' }
                        }
                    }
                }
            });
            break;
            
        case 'time-distribution':
            // Create time bins
            const maxTime = Math.max(...data.executionTimes);
            const timeBins = [0, 20, 40, 60, 80, 100, maxTime + 1];
            const timeBinCounts = new Array(timeBins.length - 1).fill(0);
            
            data.executionTimes.forEach(time => {
                for (let i = 0; i < timeBins.length - 1; i++) {
                    if (time >= timeBins[i] && time < timeBins[i + 1]) {
                        timeBinCounts[i]++;
                        break;
                    }
                }
            });
            
            chart4 = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: timeBins.slice(0, -1).map((b, i) => `${b}-${timeBins[i+1]}s`),
                    datasets: [{
                        label: 'Workflow Count',
                        data: timeBinCounts,
                        backgroundColor: colors.info,
                        borderColor: colors.borderWidth > 1 ? '#000' : colors.info,
                        borderWidth: colors.borderWidth
                    }]
                },
                options: {
                    ...getCommonOptions(),
                    scales: {
                        x: {
                            title: { display: true, text: 'Execution Time Range' },
                            grid: { color: colors.gridColor },
                            ticks: { color: '#888' }
                        },
                        y: {
                            title: { display: true, text: 'Count' },
                            ...getCommonOptions().scales.y
                        }
                    }
                }
            });
            break;
    }
}

// Main render function called from metrics.html
function renderAllCharts(runs) {
    const chart1Canvas = document.getElementById('chart1');
    const chart2Canvas = document.getElementById('chart2');
    const chart3Canvas = document.getElementById('chart3');
    const chart4Canvas = document.getElementById('chart4');
    
    if (!runs || runs.length === 0) {
        showChartPlaceholder(chart1Canvas, 'Not enough data for chart');
        showChartPlaceholder(chart2Canvas, 'Not enough data for chart');
        showChartPlaceholder(chart3Canvas, 'Not enough data for chart');
        showChartPlaceholder(chart4Canvas, 'Not enough data for chart');
        return;
    }
    
    clearChartPlaceholder(chart1Canvas);
    clearChartPlaceholder(chart2Canvas);
    clearChartPlaceholder(chart3Canvas);
    clearChartPlaceholder(chart4Canvas);
    
    // Process all data
    currentChartData = processChartData(runs);
    
    // Render all charts with current selections
    renderChart1();
    renderChart2();
    renderChart3();
    renderChart4();
}

// Export functions for use in metrics.html
window.metricsCharts = {
    initialize: initializeChartDropdowns,
    render: renderAllCharts,
    updateColors: () => {
        if (currentChartData.labels) {
            renderChart1();
            renderChart2();
            renderChart3();
            renderChart4();
        }
    }
};
