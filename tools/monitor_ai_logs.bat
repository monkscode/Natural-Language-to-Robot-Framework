@echo off
REM Windows batch script to monitor AI logs
REM Usage: monitor_ai_logs.bat [log_name]

set LOG_DIR=logs
set LOG_NAME=%1

if "%LOG_NAME%"=="" (
    echo 🤖 Available AI logs:
    echo ==================
    if exist "%LOG_DIR%\crewai.log" (
        echo ✅ crewai.log
    ) else (
        echo ❌ crewai.log (not found)
    )
    if exist "%LOG_DIR%\litellm.log" (
        echo ✅ litellm.log
    ) else (
        echo ❌ litellm.log (not found)
    )
    if exist "%LOG_DIR%\langchain.log" (
        echo ✅ langchain.log
    ) else (
        echo ❌ langchain.log (not found)
    )
    if exist "%LOG_DIR%\openai.log" (
        echo ✅ openai.log
    ) else (
        echo ❌ openai.log (not found)
    )
    echo.
    echo Usage: monitor_ai_logs.bat [log_name]
    echo Example: monitor_ai_logs.bat crewai
    echo         monitor_ai_logs.bat litellm
    goto :eof
)

set LOG_FILE=%LOG_DIR%\%LOG_NAME%.log

if not exist "%LOG_FILE%" (
    echo ❌ Log file %LOG_FILE% doesn't exist
    echo Make sure to run the application first to create log files
    goto :eof
)

echo 🔍 Monitoring %LOG_FILE%
echo Press Ctrl+C to stop
echo ==================
powershell -Command "Get-Content '%LOG_FILE%' -Wait -Tail 10"