#!/usr/bin/env python3
"""
Simple test to check healing API and create test sessions.
"""

import requests
import json

def test_healing_api():
    """Test the healing API endpoints."""
    
    # Try different possible ports
    ports = [8000, 8080, 3000, 5000]
    base_url = None
    
    print("🔍 Finding the server...")
    for port in ports:
        try:
            url = f"http://localhost:{port}"
            response = requests.get(url, timeout=2)
            if response.status_code == 200:
                print(f"✅ Found server at {url}")
                base_url = url
                break
        except:
            continue
    
    if not base_url:
        print("❌ Server not found on any common port")
        print("Make sure the server is running with:")
        print("python -m uvicorn src.backend.main:app --reload")
        return
    
    # Test healing endpoints
    healing_url = f"{base_url}/api/healing"
    
    print(f"\n🧪 Testing healing endpoints at {healing_url}")
    
    # Test status endpoint
    try:
        response = requests.get(f"{healing_url}/status")
        if response.status_code == 200:
            data = response.json()
            print("✅ Status endpoint working:")
            print(f"   Healing enabled: {data.get('healing_enabled')}")
            print(f"   Active sessions: {data.get('active_sessions')}")
        else:
            print(f"❌ Status endpoint failed: {response.status_code}")
    except Exception as e:
        print(f"❌ Status endpoint error: {e}")
    
    # Test sessions endpoint
    try:
        response = requests.get(f"{healing_url}/sessions")
        if response.status_code == 200:
            data = response.json()
            sessions = data.get('sessions', [])
            print(f"✅ Sessions endpoint working: {len(sessions)} sessions found")
            for session in sessions[:3]:
                print(f"   - {session.get('session_id')}: {session.get('status')}")
        else:
            print(f"❌ Sessions endpoint failed: {response.status_code}")
    except Exception as e:
        print(f"❌ Sessions endpoint error: {e}")
    
    # Try to create a test healing session
    try:
        response = requests.post(f"{healing_url}/test-google-healing")
        if response.status_code == 200:
            data = response.json()
            print("✅ Test healing session created:")
            print(f"   Session ID: {data.get('session_id')}")
            print(f"   Test case: {data.get('test_case')}")
            return data.get('session_id')
        else:
            print(f"❌ Test session creation failed: {response.status_code}")
            print(f"   Response: {response.text}")
    except Exception as e:
        print(f"❌ Test session creation error: {e}")
    
    return None

def main():
    print("🔧 Healing API Test")
    print("=" * 40)
    
    session_id = test_healing_api()
    
    if session_id:
        print(f"\n🎉 Success! Test healing session created: {session_id}")
        print("This session should now be visible in the healing dashboard!")
    else:
        print("\n❌ Failed to create test healing session")
        print("Check that the server is running and the healing system is enabled")

if __name__ == "__main__":
    main()