#!/usr/bin/env python3
"""
Start the server and test the healing dashboard.
"""

import subprocess
import time
import requests
import json
import sys
from pathlib import Path

def start_server():
    """Start the FastAPI server."""
    print("🚀 Starting FastAPI server...")
    
    # Change to project root
    project_root = Path(__file__).parent.parent
    
    # Start server in background
    process = subprocess.Popen([
        sys.executable, "-m", "uvicorn", 
        "src.backend.main:app", 
        "--reload", 
        "--host", "0.0.0.0", 
        "--port", "8000"
    ], cwd=project_root)
    
    # Wait for server to start
    print("⏳ Waiting for server to start...")
    for i in range(30):  # Wait up to 30 seconds
        try:
            response = requests.get("http://localhost:8000/", timeout=2)
            if response.status_code == 200:
                print("✅ Server started successfully!")
                return process
        except:
            pass
        time.sleep(1)
        print(f"   Waiting... ({i+1}/30)")
    
    print("❌ Server failed to start")
    process.terminate()
    return None

def test_healing_api():
    """Test the healing API endpoints."""
    print("\n🔍 Testing Healing API...")
    
    base_url = "http://localhost:8000/api/healing"
    
    # Test status endpoint
    try:
        response = requests.get(f"{base_url}/status")
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Status: Healing enabled = {data.get('healing_enabled')}")
            print(f"   Active sessions: {data.get('active_sessions', 0)}")
        else:
            print(f"❌ Status endpoint failed: {response.status_code}")
    except Exception as e:
        print(f"❌ Status endpoint error: {e}")
    
    # Test sessions endpoint
    try:
        response = requests.get(f"{base_url}/sessions")
        if response.status_code == 200:
            data = response.json()
            sessions = data.get("sessions", [])
            print(f"✅ Sessions: Found {len(sessions)} sessions")
            for session in sessions[:3]:
                print(f"   - {session.get('session_id', 'unknown')}: {session.get('status', 'unknown')}")
        else:
            print(f"❌ Sessions endpoint failed: {response.status_code}")
    except Exception as e:
        print(f"❌ Sessions endpoint error: {e}")

def create_test_healing_session():
    """Create a test healing session."""
    print("\n🧪 Creating test healing session...")
    
    try:
        response = requests.post("http://localhost:8000/api/healing/test-google-healing")
        if response.status_code == 200:
            data = response.json()
            session_id = data.get("session_id")
            print(f"✅ Test session created: {session_id}")
            print(f"   Test case: {data.get('test_case')}")
            print(f"   Original locator: {data.get('original_locator')}")
            return session_id
        else:
            print(f"❌ Failed to create test session: {response.status_code}")
            print(f"   Response: {response.text}")
    except Exception as e:
        print(f"❌ Error creating test session: {e}")
    
    return None

def main():
    """Main function."""
    print("🔧 Healing Dashboard Test Setup")
    print("=" * 50)
    
    # Start server
    server_process = start_server()
    if not server_process:
        return
    
    try:
        # Test API
        test_healing_api()
        
        # Create test session
        session_id = create_test_healing_session()
        
        # Test API again to see the new session
        print("\n🔄 Testing API after creating session...")
        test_healing_api()
        
        print("\n" + "=" * 50)
        print("🎯 DASHBOARD ACCESS:")
        print("=" * 50)
        print("1. 🌐 Open your browser and go to:")
        print("   http://localhost:8000/")
        print()
        print("2. 📊 Or access the API directly:")
        print("   http://localhost:8000/api/healing/sessions")
        print("   http://localhost:8000/api/healing/status")
        print()
        print("3. 📋 View healing session details:")
        if session_id:
            print(f"   http://localhost:8000/api/healing/sessions/{session_id}")
        print()
        print("Press Ctrl+C to stop the server when done testing...")
        
        # Keep server running
        server_process.wait()
        
    except KeyboardInterrupt:
        print("\n🛑 Stopping server...")
        server_process.terminate()
        server_process.wait()
        print("✅ Server stopped")

if __name__ == "__main__":
    main()