# Frontend Testing Instructions

## Prerequisites

1. Install Node.js (version 16 or higher)
2. Navigate to the frontend directory: `cd src/frontend`

## Running Tests

### Install Dependencies
```bash
npm install
```

### Run Tests
```bash
# Run all tests once
npm test

# Run tests in watch mode (re-runs on file changes)
npm run test:watch

# Run tests with coverage report
npm run test:coverage
```

## Test Coverage

The tests cover the following healing UI components:

### 1. Healing Progress Indicators
- ✅ Shows progress when healing starts
- ✅ Updates progress bar based on healing phase
- ✅ Updates step status correctly (pending → active → complete)
- ✅ Hides progress indicator when healing completes
- ✅ Handles healing errors and shows error states

### 2. Navigation Tabs
- ✅ Switches between Test Generation, Healing Config, and Dashboard sections
- ✅ Updates active tab styling
- ✅ Shows/hides appropriate sections

### 3. Configuration Panel
- ✅ Loads healing configuration from API
- ✅ Saves configuration changes to API
- ✅ Resets configuration to default values
- ✅ Updates confidence threshold slider value display
- ✅ Handles API errors gracefully

### 4. Healing Dashboard
- ✅ Loads and displays healing statistics
- ✅ Loads and displays healing reports table
- ✅ Shows empty state when no reports available
- ✅ Refreshes data when refresh button clicked
- ✅ Handles API errors gracefully

### 5. Healing Report Modal
- ✅ Displays detailed healing report in modal
- ✅ Shows before/after locator comparison
- ✅ Closes modal when close button clicked
- ✅ Closes modal when clicking outside modal area

### 6. Error Handling
- ✅ Handles network errors gracefully
- ✅ Shows error notifications for failed operations
- ✅ Displays appropriate error messages

## Test Files

- `tests/healing-ui.test.js` - Main test file for healing UI components
- `tests/setup.js` - Jest setup and mocks
- `package.json` - Test configuration and dependencies

## Manual Testing

After implementing the frontend changes, you can manually test the healing UI by:

1. Starting the backend server
2. Opening the frontend in a browser
3. Clicking the "Healing Config" tab to test configuration panel
4. Clicking the "Healing Dashboard" tab to test statistics and reports
5. Running a test that triggers healing to see progress indicators

## Integration Testing

The healing UI integrates with the following backend endpoints:

- `GET /api/healing/status` - Get healing system status and configuration
- `POST /api/healing/config` - Update healing configuration
- `GET /api/healing/statistics` - Get healing statistics
- `GET /api/healing/reports` - Get list of healing reports
- `GET /api/healing/reports/{run_id}` - Get detailed healing report
- `GET /api/healing/progress/{session_id}` - Stream healing progress (SSE)

Make sure these endpoints are working before testing the frontend integration.