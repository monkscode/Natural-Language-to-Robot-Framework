#!/usr/bin/env python3
"""
Test the healing system with the actual Google search failure.
This will create healing sessions that should appear in the dashboard.
"""

import asyncio
import sys
import os
from pathlib import Path
from datetime import datetime

# Add the backend to the path
sys.path.append(str(Path(__file__).parent.parent / "src" / "backend"))

async def test_healing_with_real_failure():
    """Test the healing system with the actual Google search failure."""
    print("🧪 Testing Healing System with Real Google Search Failure")
    print("=" * 60)
    
    try:
        from services.failure_detection_service import FailureDetectionService
        from services.healing_orchestrator import HealingOrchestrator
        from core.config_loader import get_healing_config
        from core.models import FailureContext, FailureType
        
        # Find the Google search failure output.xml
        robot_tests_dir = Path("robot_tests")
        output_files = list(robot_tests_dir.glob("*/output.xml"))
        
        if not output_files:
            print("❌ No output.xml files found. Run a test first.")
            return
        
        # Use the most recent output.xml (should be the Google search failure)
        latest_output = max(output_files, key=lambda p: p.stat().st_mtime)
        print(f"📁 Using output file: {latest_output}")
        
        # Step 1: Test failure detection
        print("\n🔍 Step 1: Testing Failure Detection")
        print("-" * 40)
        
        service = FailureDetectionService()
        failures = service.analyze_execution_result(str(latest_output))
        
        print(f"✅ Found {len(failures)} failures:")
        for i, failure in enumerate(failures, 1):
            print(f"   {i}. Locator: {failure.original_locator}")
            print(f"      Type: {failure.failure_type.name}")
            print(f"      Exception: {failure.exception_type}")
            print(f"      Test: {failure.test_case}")
            print(f"      Step: {failure.failing_step}")
        
        if not failures:
            print("❌ No failures detected. The failure detection service may have issues.")
            return
        
        # Step 2: Test healing orchestrator
        print("\n🔧 Step 2: Testing Healing Orchestrator")
        print("-" * 40)
        
        config = get_healing_config()
        print(f"✅ Healing config loaded: enabled={config.enabled}")
        
        orchestrator = HealingOrchestrator(config, "local", "test-model")
        await orchestrator.start()
        print("✅ Healing orchestrator started")
        
        # Step 3: Initiate healing for the first failure
        print("\n🚀 Step 3: Initiating Healing Session")
        print("-" * 40)
        
        failure_to_heal = failures[0]
        print(f"🎯 Healing failure: {failure_to_heal.original_locator}")
        
        session = await orchestrator.initiate_healing(failure_to_heal)
        print(f"✅ Healing session created: {session.session_id}")
        print(f"   Status: {session.status.value}")
        print(f"   Test Case: {session.failure_context.test_case}")
        print(f"   Original Locator: {session.failure_context.original_locator}")
        
        # Step 4: Monitor healing progress
        print("\n⏳ Step 4: Monitoring Healing Progress")
        print("-" * 40)
        
        # Wait a bit for healing to start
        await asyncio.sleep(2)
        
        # Check session status
        updated_session = await orchestrator.get_session_status(session.session_id)
        if updated_session:
            print(f"📊 Session Status: {updated_session.status.value}")
            print(f"   Progress: {updated_session.progress:.1%}")
            print(f"   Current Phase: {updated_session.current_phase}")
            print(f"   Attempts: {len(updated_session.attempts)}")
            
            if updated_session.error_message:
                print(f"   Error: {updated_session.error_message}")
        
        # Step 5: Wait for completion or timeout
        print("\n⏰ Step 5: Waiting for Healing Completion")
        print("-" * 40)
        
        max_wait_time = 30  # seconds
        wait_time = 0
        
        while wait_time < max_wait_time:
            await asyncio.sleep(2)
            wait_time += 2
            
            updated_session = await orchestrator.get_session_status(session.session_id)
            if updated_session:
                print(f"   [{wait_time:2d}s] Status: {updated_session.status.value} | Progress: {updated_session.progress:.1%} | Phase: {updated_session.current_phase}")
                
                if updated_session.status.value in ["SUCCESS", "FAILED", "TIMEOUT"]:
                    print(f"\n🎉 Healing completed with status: {updated_session.status.value}")
                    if updated_session.error_message:
                        print(f"   Error: {updated_session.error_message}")
                    break
            else:
                print(f"   [{wait_time:2d}s] Session not found")
                break
        
        # Step 6: Show final results
        print("\n📊 Step 6: Final Results")
        print("-" * 40)
        
        final_session = await orchestrator.get_session_status(session.session_id)
        if final_session:
            print(f"✅ Final Status: {final_session.status.value}")
            print(f"   Started: {final_session.started_at}")
            print(f"   Completed: {final_session.completed_at}")
            print(f"   Total Attempts: {len(final_session.attempts)}")
            
            if final_session.attempts:
                print("   Healing Attempts:")
                for i, attempt in enumerate(final_session.attempts, 1):
                    print(f"     {i}. {attempt.locator} ({attempt.strategy.value}) - {'✅' if attempt.success else '❌'}")
                    if attempt.error_message:
                        print(f"        Error: {attempt.error_message}")
        
        await orchestrator.stop()
        print("\n✅ Healing orchestrator stopped")
        
        print("\n" + "=" * 60)
        print("🎯 HEALING SYSTEM TEST COMPLETE")
        print("=" * 60)
        print("The healing session should now be visible in the dashboard!")
        print("Check the healing API at: http://localhost:8000/api/healing/sessions")
        
    except ImportError as e:
        print(f"❌ Import error: {e}")
        print("Make sure to run from project root with virtual environment activated")
    except Exception as e:
        print(f"❌ Error testing healing system: {e}")
        import traceback
        traceback.print_exc()

def main():
    """Main test function."""
    print("🔧 Healing System Test")
    print("=" * 60)
    print("This test will create healing sessions using the actual Google search failure")
    print("The sessions should then appear in the healing dashboard")
    print()
    
    asyncio.run(test_healing_with_real_failure())

if __name__ == "__main__":
    main()