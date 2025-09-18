#!/bin/bash
# Setup script for Test Self-Healing System Virtual Environment
# This script creates and configures the virtual environment with all required dependencies

set -e

echo "🚀 Setting up Test Self-Healing System Virtual Environment"
echo "=========================================================="

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.8 or higher."
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
    echo "✅ Virtual environment created"
else
    echo "📦 Virtual environment already exists"
fi

# Activate virtual environment
echo "🔧 Activating virtual environment..."
source venv/Scripts/activate

# Upgrade pip
echo "⬆️  Upgrading pip..."
python -m pip install --upgrade pip

# Install requirements
echo "📚 Installing dependencies..."
if [ -f "requirements-dev.txt" ]; then
    pip install -r requirements-dev.txt
fi

if [ -f "src/backend/requirements.txt" ]; then
    pip install -r src/backend/requirements.txt
fi

# Install additional test dependencies
echo "🧪 Installing test dependencies..."
pip install pytest pytest-asyncio pytest-mock pytest-cov

echo ""
echo "✅ Virtual environment setup complete!"
echo ""
echo "To activate the virtual environment:"
echo "  source venv/Scripts/activate"
echo ""
echo "To run tests:"
echo "  python tests/integration/test_e2e_simple.py"
echo "  python -m pytest tests/integration/test_e2e_pytest.py -v"
echo ""
echo "To deactivate:"
echo "  deactivate"