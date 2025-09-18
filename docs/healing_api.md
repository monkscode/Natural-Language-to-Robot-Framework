# Healing API Documentation

This document describes the REST API endpoints for the Test Self-Healing system.

## Base URL

All healing endpoints are prefixed with `/api/healing`

## Authentication

The API supports Bearer token authentication. For development, unauthenticated requests are allowed.

```
Authorization: Bearer <token>
```

## Rate Limiting

Healing operations are rate-limited to prevent abuse:
- Healing operations: 10 requests per minute per IP
- Configuration changes: 5 requests per 5 minutes per IP

## Endpoints

### GET /healing/status

Get current healing system status and configuration.

**Response:**
```json
{
  "status": "success",
  "healing_enabled": true,
  "active_sessions": 2,
  "configuration": {
    "max_attempts_per_locator": 3,
    "chrome_session_timeout": 30,
    "healing_timeout": 300,
    "max_concurrent_sessions": 3,
    "confidence_threshold": 0.7,
    "max_alternatives": 5
  },
  "containers": {
    "running": 2,
    "stopped": 0
  },
  "statistics": {
    "total_attempts": 45,
    "successful_healings": 32,
    "failed_healings": 13,
    "success_rate": 71.1,
    "average_healing_time": 42.3
  }
}
```

### POST /healing/config

Update healing configuration settings.

**Request Body:**
```json
{
  "enabled": true,
  "max_attempts_per_locator": 5,
  "chrome_session_timeout": 45,
  "healing_timeout": 600,
  "max_concurrent_sessions": 5,
  "confidence_threshold": 0.8,
  "max_alternatives": 7
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Healing configuration updated successfully",
  "configuration": {
    "enabled": true,
    "max_attempts_per_locator": 5,
    "chrome_session_timeout": 45,
    "healing_timeout": 600,
    "max_concurrent_sessions": 5,
    "confidence_threshold": 0.8,
    "max_alternatives": 7
  }
}
```

### GET /healing/sessions

Get list of healing sessions with optional filtering.

**Query Parameters:**
- `status` (optional): Filter by session status (pending, in_progress, success, failed, timeout)
- `limit` (optional): Maximum number of sessions to return (default: 50)

**Response:**
```json
{
  "status": "success",
  "sessions": [
    {
      "session_id": "session-123",
      "status": "IN_PROGRESS",
      "test_file": "test_example.robot",
      "test_case": "Test Login",
      "failing_step": "Click Element    id=login-button",
      "original_locator": "id=login-button",
      "started_at": "2024-01-15T10:30:00Z",
      "completed_at": null,
      "progress": 0.6,
      "current_phase": "validation",
      "attempts_count": 2,
      "error_message": null
    }
  ],
  "total_sessions": 1
}
```

### GET /healing/sessions/{session_id}

Get detailed information about a specific healing session.

**Response:**
```json
{
  "status": "success",
  "session": {
    "session_id": "session-123",
    "status": "SUCCESS",
    "test_file": "test_example.robot",
    "test_case": "Test Login",
    "failing_step": "Click Element    id=login-button",
    "original_locator": "id=login-button",
    "started_at": "2024-01-15T10:30:00Z",
    "completed_at": "2024-01-15T10:32:15Z",
    "progress": 1.0,
    "current_phase": "complete",
    "attempts_count": 3,
    "error_message": null
  },
  "attempts": [
    {
      "locator": "css=#login-button",
      "strategy": "CSS",
      "success": true,
      "confidence_score": 0.85,
      "error_message": null,
      "timestamp": "2024-01-15T10:31:45Z"
    }
  ]
}
```

### POST /healing/sessions/{session_id}/cancel

Cancel an active healing session.

**Response:**
```json
{
  "status": "success",
  "message": "Healing session session-123 cancelled successfully"
}
```

### GET /healing/reports/{run_id}

Get healing report for a specific test run.

**Response:**
```json
{
  "status": "success",
  "report": {
    "run_id": "test-run-123",
    "test_file": "test_example.robot",
    "healing_attempts": [
      {
        "session_id": "session-123",
        "test_case": "Test Login",
        "original_locator": "id=login-button",
        "status": "SUCCESS",
        "attempts": 3,
        "started_at": "2024-01-15T10:30:00Z",
        "completed_at": "2024-01-15T10:32:15Z",
        "error_message": null
      }
    ],
    "total_attempts": 1,
    "successful_healings": 1,
    "failed_healings": 0,
    "total_time": 135.5,
    "generated_at": "2024-01-15T10:32:20Z"
  }
}
```

### GET /healing/reports

Get list of healing reports within a time range.

**Query Parameters:**
- `limit` (optional): Maximum number of reports to return (default: 20)
- `days` (optional): Number of days to look back (default: 7)

**Response:**
```json
{
  "status": "success",
  "reports": [
    {
      "run_id": "test-run-123",
      "test_file": "test_example.robot",
      "total_attempts": 1,
      "successful_healings": 1,
      "failed_healings": 0,
      "generated_at": "2024-01-15T10:32:20Z"
    }
  ],
  "total_reports": 1
}
```

### GET /healing/statistics

Get healing statistics and trends.

**Query Parameters:**
- `days` (optional): Number of days for trend analysis (default: 30)

**Response:**
```json
{
  "status": "success",
  "statistics": {
    "total_attempts": 125,
    "successful_healings": 89,
    "failed_healings": 36,
    "success_rate": 71.2,
    "average_healing_time": 45.3,
    "last_24h_attempts": 12,
    "last_24h_success_rate": 83.3,
    "top_failure_types": [
      {
        "type": "locator_not_found",
        "count": 45
      },
      {
        "type": "element_not_interactable",
        "count": 23
      }
    ],
    "healing_trends": [
      {
        "date": "2024-01-14",
        "attempts": 8,
        "successful": 6,
        "success_rate": 75.0
      },
      {
        "date": "2024-01-15",
        "attempts": 12,
        "successful": 10,
        "success_rate": 83.3
      }
    ]
  }
}
```

### GET /healing/progress/{session_id}

Stream real-time healing progress updates via Server-Sent Events.

**Response:** Server-Sent Events stream

```
event: status
data: {"session_id": "session-123", "status": "IN_PROGRESS", "progress": 0.0, "current_phase": "starting", "attempts_count": 0}

event: progress
data: {"phase": "analysis", "message": "Analyzing failure context", "progress": 0.2}

event: progress
data: {"phase": "generation", "message": "Generated 5 alternative locators", "progress": 0.5, "candidates": [...]}

event: progress
data: {"phase": "validation", "message": "Validating locators against live session", "progress": 0.8}

event: complete
data: {"session_id": "session-123", "status": "SUCCESS", "completed_at": "2024-01-15T10:32:15Z"}
```

### DELETE /healing/containers/cleanup

Clean up all healing-related Docker containers.

**Response:**
```json
{
  "status": "success",
  "message": "Cleaned up 3 healing containers",
  "containers_cleaned": 3,
  "errors": null
}
```

### POST /healing/test

Trigger a test healing session for development/testing purposes.

**Request Body:**
```json
{
  "test_file": "test_example.robot",
  "test_case": "Test Login",
  "failing_locator": "id=login-button",
  "target_url": "https://example.com/login"
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Test healing session initiated",
  "session_id": "test-session-123",
  "progress_url": "/api/healing/progress/test-session-123"
}
```

## Error Responses

All endpoints return error responses in the following format:

```json
{
  "detail": "Error message describing what went wrong"
}
```

Common HTTP status codes:
- `400`: Bad Request - Invalid request parameters
- `401`: Unauthorized - Authentication required
- `403`: Forbidden - Insufficient permissions
- `404`: Not Found - Resource not found
- `422`: Unprocessable Entity - Validation error
- `429`: Too Many Requests - Rate limit exceeded
- `500`: Internal Server Error - Server error

## WebSocket Alternative

For real-time updates, you can also use the Server-Sent Events endpoint at `/healing/progress/{session_id}` which provides a persistent connection for streaming progress updates.

## Usage Examples

### JavaScript/Fetch

```javascript
// Get healing status
const response = await fetch('/api/healing/status');
const status = await response.json();

// Update configuration
const configUpdate = {
  enabled: true,
  max_attempts_per_locator: 5
};
await fetch('/api/healing/config', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer your-token'
  },
  body: JSON.stringify(configUpdate)
});

// Stream progress updates
const eventSource = new EventSource('/api/healing/progress/session-123');
eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Progress update:', data);
};
```

### Python/Requests

```python
import requests

# Get healing status
response = requests.get('/api/healing/status')
status = response.json()

# Update configuration
config_update = {
    'enabled': True,
    'max_attempts_per_locator': 5
}
response = requests.post('/api/healing/config', 
                        json=config_update,
                        headers={'Authorization': 'Bearer your-token'})

# Get session details
response = requests.get('/api/healing/sessions/session-123')
session = response.json()
```

### cURL

```bash
# Get healing status
curl -X GET /api/healing/status

# Update configuration
curl -X POST /api/healing/config \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{"enabled": true, "max_attempts_per_locator": 5}'

# Stream progress updates
curl -N /api/healing/progress/session-123
```