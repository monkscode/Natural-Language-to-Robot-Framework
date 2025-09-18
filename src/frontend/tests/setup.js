/**
 * Jest setup file for frontend tests
 */

// Mock console methods to reduce noise in tests
global.console = {
    ...console,
    log: jest.fn(),
    error: jest.fn(),
    warn: jest.fn(),
    info: jest.fn(),
};

// Mock window.location
delete window.location;
window.location = {
    href: 'http://localhost:3000',
    origin: 'http://localhost:3000',
    protocol: 'http:',
    host: 'localhost:3000',
    hostname: 'localhost',
    port: '3000',
    pathname: '/',
    search: '',
    hash: ''
};

// Mock localStorage
const localStorageMock = {
    getItem: jest.fn(),
    setItem: jest.fn(),
    removeItem: jest.fn(),
    clear: jest.fn(),
};
global.localStorage = localStorageMock;

// Mock sessionStorage
const sessionStorageMock = {
    getItem: jest.fn(),
    setItem: jest.fn(),
    removeItem: jest.fn(),
    clear: jest.fn(),
};
global.sessionStorage = sessionStorageMock;

// Mock CSS custom properties
Object.defineProperty(document.documentElement.style, 'setProperty', {
    value: jest.fn(),
});

// Mock CSS getComputedStyle
global.getComputedStyle = jest.fn(() => ({
    getPropertyValue: jest.fn(() => ''),
}));

// Mock IntersectionObserver
global.IntersectionObserver = jest.fn(() => ({
    observe: jest.fn(),
    disconnect: jest.fn(),
    unobserve: jest.fn(),
}));

// Mock ResizeObserver
global.ResizeObserver = jest.fn(() => ({
    observe: jest.fn(),
    disconnect: jest.fn(),
    unobserve: jest.fn(),
}));

// Mock EventSource for SSE
global.EventSource = jest.fn(() => ({
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    close: jest.fn(),
    readyState: 1,
    CONNECTING: 0,
    OPEN: 1,
    CLOSED: 2,
}));

// Mock setTimeout and setInterval for tests
jest.useFakeTimers();

// Clean up after each test
afterEach(() => {
    jest.clearAllMocks();
    jest.clearAllTimers();
    document.body.innerHTML = '';
});