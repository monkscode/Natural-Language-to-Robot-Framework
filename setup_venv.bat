@echo off
REM Setup script for Test Self-Healing System Virtual Environment (Windows)
REM This script creates and configures the virtual environment with all required dependencies

echo 🚀 Setting up Test Self-Healing System Virtual Environment
echo ==========================================================

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python is not installed or not in PATH. Please install Python 3.8 or higher.
    exit /b 1
)

REM Create virtual environment if it doesn't exist
if not exist "venv" (
    echo 📦 Creating virtual environment...
    python -m venv venv
    echo ✅ Virtual environment created
) else (
    echo 📦 Virtual environment already exists
)

REM Activate virtual environment
echo 🔧 Activating virtual environment...
call venv\Scripts\activate.bat

REM Upgrade pip
echo ⬆️  Upgrading pip...
python -m pip install --upgrade pip

REM Install requirements
echo 📚 Installing dependencies...
if exist "requirements-dev.txt" (
    pip install -r requirements-dev.txt
)

if exist "src\backend\requirements.txt" (
    pip install -r src\backend\requirements.txt
)

REM Install additional test dependencies
echo 🧪 Installing test dependencies...
pip install pytest pytest-asyncio pytest-mock pytest-cov

echo.
echo ✅ Virtual environment setup complete!
echo.
echo To activate the virtual environment:
echo   venv\Scripts\activate.bat
echo.
echo To run tests:
echo   python tests\integration\test_e2e_simple.py
echo   python -m pytest tests\integration\test_e2e_pytest.py -v
echo.
echo To deactivate:
echo   deactivate