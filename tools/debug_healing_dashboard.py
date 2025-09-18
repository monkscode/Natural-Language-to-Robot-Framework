#!/usr/bin/env python3
"""
Debug script for healing dashboard issues.
This script helps identify why the healing dashboard is not showing results.
"""

import asyncio
import sys
import os
import requests
import json
from pathlib import Path
from datetime import datetime

# Add the backend to the path
sys.path.append(str(Path(__file__).parent.parent / "src" / "backend"))

def test_healing_api_endpoints():
    """Test the healing API endpoints to see if they're working."""
    base_url = "http://localhost:8000/api/healing"
    
    print("🔍 Testing Healing API Endpoints")
    print("=" * 50)
    
    endpoints = [
        "/status",
        "/sessions",
        "/reports",
        "/statistics"
    ]
    
    for endpoint in endpoints:
        try:
            url = f"{base_url}{endpoint}"
            print(f"Testing {url}...")
            
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ {endpoint}: SUCCESS")
                
                if endpoint == "/sessions":
                    sessions = data.get("sessions", [])
                    print(f"   Found {len(sessions)} sessions")
                    for session in sessions[:3]:  # Show first 3
                        print(f"   - {session.get('session_id', 'unknown')}: {session.get('status', 'unknown')}")
                
                elif endpoint == "/status":
                    print(f"   Healing enabled: {data.get('healing_enabled', 'unknown')}")
                    print(f"   Active sessions: {data.get('active_sessions', 'unknown')}")
                
                elif endpoint == "/statistics":
                    stats = data.get("statistics", {})
                    print(f"   Total attempts: {stats.get('total_attempts', 0)}")
                    print(f"   Success rate: {stats.get('success_rate', 0):.1f}%")
                
            else:
                print(f"❌ {endpoint}: HTTP {response.status_code}")
                print(f"   Response: {response.text[:200]}")
                
        except requests.exceptions.ConnectionError:
            print(f"❌ {endpoint}: Connection failed - is the server running?")
        except Exception as e:
            print(f"❌ {endpoint}: Error - {e}")
        
        print()

def test_failure_detection():
    """Test the failure detection service with the Google search failure."""
    print("🔍 Testing Failure Detection Service")
    print("=" * 50)
    
    try:
        from services.failure_detection_service import FailureDetectionService
        
        # Find the Google search failure output.xml
        robot_tests_dir = Path("robot_tests")
        output_files = list(robot_tests_dir.glob("*/output.xml"))
        
        if not output_files:
            print("❌ No output.xml files found in robot_tests directory")
            return
        
        # Use the most recent output.xml
        latest_output = max(output_files, key=lambda p: p.stat().st_mtime)
        print(f"📁 Using output file: {latest_output}")
        
        # Analyze the failure
        service = FailureDetectionService()
        failures = service.analyze_execution_result(str(latest_output))
        
        print(f"✅ Found {len(failures)} failures")
        for i, failure in enumerate(failures, 1):
            print(f"   {i}. {failure.original_locator} - {failure.failure_type.name}")
            print(f"      Exception: {failure.exception_type}")
            print(f"      Test: {failure.test_case}")
        
        return failures
        
    except ImportError as e:
        print(f"❌ Import error: {e}")
        print("Make sure to run from project root with virtual environment activated")
        return []
    except Exception as e:
        print(f"❌ Error analyzing failures: {e}")
        return []

def test_healing_orchestrator():
    """Test the healing orchestrator service."""
    print("🔍 Testing Healing Orchestrator")
    print("=" * 50)
    
    try:
        from services.healing_orchestrator import HealingOrchestrator
        from core.config_loader import get_healing_config
        from core.models import FailureContext, FailureType
        
        # Get healing config
        config = get_healing_config()
        print(f"✅ Healing config loaded: enabled={config.enabled}")
        
        # Create orchestrator
        orchestrator = HealingOrchestrator(config, "local", "test-model")
        print(f"✅ Healing orchestrator created")
        
        # Check active sessions
        print(f"📊 Active sessions: {len(orchestrator.active_sessions)}")
        
        return orchestrator
        
    except Exception as e:
        print(f"❌ Error testing orchestrator: {e}")
        return None

def create_test_healing_session():
    """Create a test healing session to populate the dashboard."""
    print("🧪 Creating Test Healing Session")
    print("=" * 50)
    
    try:
        base_url = "http://localhost:8000/api/healing"
        
        # Try to create a test Google search healing session
        response = requests.post(f"{base_url}/test-google-healing", timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            session_id = data.get("session_id")
            print(f"✅ Test healing session created: {session_id}")
            print(f"   Test case: {data.get('test_case')}")
            print(f"   Original locator: {data.get('original_locator')}")
            return session_id
        else:
            print(f"❌ Failed to create test session: HTTP {response.status_code}")
            print(f"   Response: {response.text}")
            return None
            
    except Exception as e:
        print(f"❌ Error creating test session: {e}")
        return None

def check_frontend_files():
    """Check if frontend files exist for the healing dashboard."""
    print("🔍 Checking Frontend Files")
    print("=" * 50)
    
    frontend_dir = Path("src/frontend")
    
    if not frontend_dir.exists():
        print("❌ Frontend directory not found")
        return
    
    # Look for healing-related frontend files
    healing_files = []
    for pattern in ["*healing*", "*dashboard*"]:
        healing_files.extend(list(frontend_dir.rglob(pattern)))
    
    if healing_files:
        print(f"✅ Found {len(healing_files)} healing-related frontend files:")
        for file in healing_files:
            print(f"   - {file.relative_to(frontend_dir)}")
    else:
        print("❌ No healing-related frontend files found")
        print("   The dashboard UI may not be implemented yet")

def check_server_status():
    """Check if the server is running and accessible."""
    print("🔍 Checking Server Status")
    print("=" * 50)
    
    try:
        response = requests.get("http://localhost:8000/", timeout=5)
        if response.status_code == 200:
            print("✅ Server is running and accessible")
        else:
            print(f"⚠️  Server responded with status {response.status_code}")
    except requests.exceptions.ConnectionError:
        print("❌ Server is not running or not accessible")
        print("   Start the server with: python -m uvicorn src.backend.main:app --reload")
    except Exception as e:
        print(f"❌ Error checking server: {e}")

def check_healing_logs():
    """Check healing-related log files."""
    print("🔍 Checking Healing Logs")
    print("=" * 50)
    
    logs_dir = Path("logs")
    if not logs_dir.exists():
        print("❌ Logs directory not found")
        return
    
    healing_logs = [
        "healing_all.log",
        "healing_operations.log", 
        "healing_errors.log",
        "healing_audit.log"
    ]
    
    for log_file in healing_logs:
        log_path = logs_dir / log_file
        if log_path.exists():
            size = log_path.stat().st_size
            mtime = datetime.fromtimestamp(log_path.stat().st_mtime)
            print(f"✅ {log_file}: {size} bytes, modified {mtime}")
            
            # Show last few lines
            try:
                with open(log_path, 'r') as f:
                    lines = f.readlines()
                    if lines:
                        print(f"   Last entry: {lines[-1].strip()}")
            except Exception as e:
                print(f"   Error reading log: {e}")
        else:
            print(f"❌ {log_file}: Not found")

def main():
    """Main debugging function."""
    print("🔧 Healing Dashboard Debug Tool")
    print("=" * 60)
    print("This tool helps diagnose why the healing dashboard is not showing results")
    print()
    
    # Check server status first
    check_server_status()
    print()
    
    # Test API endpoints
    test_healing_api_endpoints()
    
    # Check healing logs
    check_healing_logs()
    print()
    
    # Test failure detection
    failures = test_failure_detection()
    print()
    
    # Test healing orchestrator
    orchestrator = test_healing_orchestrator()
    print()
    
    # Check frontend files
    check_frontend_files()
    print()
    
    # Create test session if no failures found
    if not failures:
        print("⚠️  No recent failures found. Creating test healing session...")
        session_id = create_test_healing_session()
        if session_id:
            print(f"✅ Test session created. Check dashboard for session: {session_id}")
    
    print("\n" + "=" * 60)
    print("🎯 RECOMMENDATIONS:")
    print()
    
    if not failures:
        print("1. ⚠️  No recent test failures found")
        print("   - Run a test that fails to generate healing data")
        print("   - Or use the test healing session created above")
    
    print("2. 🔍 Check the healing dashboard at:")
    print("   - http://localhost:8000/ (if frontend exists)")
    print("   - Or use API directly: http://localhost:8000/api/healing/sessions")
    
    print("3. 📊 Monitor healing logs in real-time:")
    print("   - tail -f logs/healing_operations.log")
    print("   - tail -f logs/healing_all.log")
    
    print("4. 🧪 Create test failures:")
    print("   - Run: python -c \"import requests; requests.post('http://localhost:8000/api/healing/test-google-healing')\"")

if __name__ == "__main__":
    main()