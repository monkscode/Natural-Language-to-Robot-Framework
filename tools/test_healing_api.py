#!/usr/bin/env python3
"""
Test the healing API endpoints and create test sessions for the dashboard.
"""

import requests
import json
import time

def test_healing_api():
    """Test the healing API endpoints."""
    base_url = "http://localhost:5000/api/healing"
    
    print("🔍 Testing Healing API Endpoints")
    print("=" * 50)
    
    # Test 1: Check healing status
    print("1. Testing /status endpoint...")
    try:
        response = requests.get(f"{base_url}/status", timeout=10)
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Status: SUCCESS")
            print(f"   Healing enabled: {data.get('healing_enabled', 'unknown')}")
            print(f"   Active sessions: {data.get('active_sessions', 'unknown')}")
        else:
            print(f"❌ Status: HTTP {response.status_code}")
            print(f"   Response: {response.text[:200]}")
    except Exception as e:
        print(f"❌ Status: Error - {e}")
    
    print()
    
    # Test 2: Check existing sessions
    print("2. Testing /sessions endpoint...")
    try:
        response = requests.get(f"{base_url}/sessions", timeout=10)
        if response.status_code == 200:
            data = response.json()
            sessions = data.get("sessions", [])
            print(f"✅ Sessions: SUCCESS")
            print(f"   Found {len(sessions)} sessions")
            for session in sessions[:3]:  # Show first 3
                print(f"   - {session.get('session_id', 'unknown')}: {session.get('status', 'unknown')}")
        else:
            print(f"❌ Sessions: HTTP {response.status_code}")
            print(f"   Response: {response.text[:200]}")
    except Exception as e:
        print(f"❌ Sessions: Error - {e}")
    
    print()
    
    # Test 3: Create a test Google search healing session
    print("3. Creating test Google search healing session...")
    try:
        response = requests.post(f"{base_url}/test-google-healing", timeout=30)
        if response.status_code == 200:
            data = response.json()
            session_id = data.get("session_id")
            print(f"✅ Test session created: {session_id}")
            print(f"   Test case: {data.get('test_case')}")
            print(f"   Original locator: {data.get('original_locator')}")
            
            # Wait a moment for the session to be processed
            print("   Waiting for session to be processed...")
            time.sleep(3)
            
            # Check the session status
            session_response = requests.get(f"{base_url}/sessions/{session_id}", timeout=10)
            if session_response.status_code == 200:
                session_data = session_response.json()
                session_info = session_data.get("session", {})
                print(f"   Session status: {session_info.get('status', 'unknown')}")
                print(f"   Progress: {session_info.get('progress', 0):.1%}")
                print(f"   Current phase: {session_info.get('current_phase', 'unknown')}")
            
            return session_id
        else:
            print(f"❌ Test session: HTTP {response.status_code}")
            print(f"   Response: {response.text[:500]}")
            return None
    except Exception as e:
        print(f"❌ Test session: Error - {e}")
        return None
    
    print()

def test_dashboard_data():
    """Test that the dashboard has data to display."""
    base_url = "http://localhost:5000/api/healing"
    
    print("🔍 Testing Dashboard Data")
    print("=" * 50)
    
    # Check sessions
    try:
        response = requests.get(f"{base_url}/sessions", timeout=10)
        if response.status_code == 200:
            data = response.json()
            sessions = data.get("sessions", [])
            print(f"📊 Dashboard Sessions: {len(sessions)} total")
            
            if sessions:
                print("   Recent sessions:")
                for session in sessions[:5]:  # Show first 5
                    print(f"   - {session.get('session_id', 'unknown')[:8]}... | {session.get('status', 'unknown')} | {session.get('test_case', 'unknown')}")
            else:
                print("   ⚠️  No sessions found - dashboard will be empty")
        else:
            print(f"❌ Failed to get sessions: HTTP {response.status_code}")
    except Exception as e:
        print(f"❌ Error getting sessions: {e}")
    
    print()
    
    # Check statistics
    try:
        response = requests.get(f"{base_url}/statistics", timeout=10)
        if response.status_code == 200:
            data = response.json()
            stats = data.get("statistics", {})
            print(f"📈 Dashboard Statistics:")
            print(f"   Total attempts: {stats.get('total_attempts', 0)}")
            print(f"   Successful healings: {stats.get('successful_healings', 0)}")
            print(f"   Success rate: {stats.get('success_rate', 0):.1f}%")
            print(f"   Last 24h attempts: {stats.get('last_24h_attempts', 0)}")
        else:
            print(f"❌ Failed to get statistics: HTTP {response.status_code}")
    except Exception as e:
        print(f"❌ Error getting statistics: {e}")

def main():
    """Main test function."""
    print("🔧 Healing API & Dashboard Test")
    print("=" * 60)
    print("Testing the healing API and creating data for the dashboard")
    print()
    
    # Test API endpoints
    session_id = test_healing_api()
    
    print()
    
    # Test dashboard data
    test_dashboard_data()
    
    print()
    print("=" * 60)
    print("🎯 DASHBOARD ACCESS:")
    print("=" * 60)
    print("You can now access the healing dashboard at:")
    print("• Main app: http://localhost:5000/")
    print("• API status: http://localhost:5000/api/healing/status")
    print("• Sessions: http://localhost:5000/api/healing/sessions")
    print("• Statistics: http://localhost:5000/api/healing/statistics")
    
    if session_id:
        print(f"• Test session: http://localhost:5000/api/healing/sessions/{session_id}")
    
    print("\n📊 The dashboard should now show healing session data!")

if __name__ == "__main__":
    main()