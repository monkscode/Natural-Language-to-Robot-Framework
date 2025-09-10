document.addEventListener('DOMContentLoaded', () => {
    const queryInput = document.getElementById('query-input');
    const generateBtn = document.getElementById('generate-btn');
    const robotCodeEl = document.getElementById('robot-code');
    const executionLogsEl = document.getElementById('execution-logs');
    const downloadBtn = document.getElementById('download-btn');
    let robotCodeContent = '';

    generateBtn.addEventListener('click', async () => {
        const query = queryInput.value;
        if (!query) {
            alert('Please enter a query.');
            return;
        }

        // Reset previous results
        robotCodeEl.textContent = 'Generating...';
        executionLogsEl.textContent = '';
        downloadBtn.style.display = 'none';

        try {
            const response = await fetch('/generate-and-run', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ query }),
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'An unknown error occurred.');
            }

            const data = await response.json();
            robotCodeContent = data.robot_code;
            robotCodeEl.textContent = robotCodeContent;
            executionLogsEl.textContent = data.logs;
            downloadBtn.style.display = 'block';

        } catch (error) {
            robotCodeEl.textContent = 'Error generating code.';
            executionLogsEl.textContent = error.message;
        }
    });

    downloadBtn.addEventListener('click', () => {
        const blob = new Blob([robotCodeContent], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'test.robot';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    });
});
