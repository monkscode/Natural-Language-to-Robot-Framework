"""Logging configuration for live updates - just simple dictionaries"""

# Emojis for different operations
EMOJI = {
    'start': '🎬',
    'ai': '🧠',
    'search': '🔍',
    'code': '⚡',
    'validate': '🔬',
    'docker': '🐳',
    'run': '🚀',
    'success': '🎉',
    'error': '⚠️',
    'info': '💡',
    'thinking': '🤔',
    'tool': '🔧',
    'browser': '🌐'
}

# Educational insights
INSIGHTS = {
    'planning': '� AI breaaks complex tasks into atomic steps for better accuracy',
    'elements': '🎯 Using vision-based detection with 95%+ accuracy',
    'code': '⚡ Browser Library is 2-3x faster than Selenium',
    'validation': '🔬 Validating syntax, structure, and best practices',
    'execution': '🐳 Running in isolated Docker container',
    'batch_processing': '🚀 Processing all elements in one browser session for better context'
}

# Agent workflow stages with progress weights
WORKFLOW_STAGES = {
    'planning': {
        'name': 'Planning test steps',
        'emoji': '🧠',
        'progress_start': 0,
        'progress_end': 25,
        'insight': INSIGHTS['planning']
    },
    'identifying': {
        'name': 'Identifying page elements',
        'emoji': '🔍',
        'progress_start': 25,
        'progress_end': 60,
        'insight': INSIGHTS['elements']
    },
    'generating': {
        'name': 'Generating test code',
        'emoji': '⚡',
        'progress_start': 60,
        'progress_end': 80,
        'insight': INSIGHTS['code']
    },
    'validating': {
        'name': 'Validating code',
        'emoji': '🔬',
        'progress_start': 80,
        'progress_end': 100,
        'insight': INSIGHTS['validation']
    }
}

# Error suggestions
ERROR_TIPS = {
    'element_not_found': [
        "Try describing the element differently",
        "Check if the element is visible on the page",
        "Verify the website URL is correct"
    ],
    'docker': [
        "Ensure Docker is running on your system",
        "Check Docker container logs for details",
        "Verify Docker has sufficient resources"
    ],
    'api': [
        "Check your internet connection",
        "Verify your API key is valid",
        "Check API service status"
    ],
    'generation': [
        "Check your internet connection",
        "Verify your API key is valid",
        "Try with a simpler query"
    ],
    'execution': [
        "Check if the website is accessible",
        "Review the generated code",
        "Try running again"
    ]
}
