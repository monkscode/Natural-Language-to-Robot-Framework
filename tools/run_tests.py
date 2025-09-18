#!/usr/bin/env python3
"""
Comprehensive test runner for the Natural Language to Robot Framework project.
"""

import subprocess
import sys
import os
from pathlib import Path

def run_command(command, description):
    """Run a command and handle the result."""
    print(f"\n{'='*60}")
    print(f"🚀 {description}")
    print(f"{'='*60}")
    
    try:
        result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Command failed with exit code {e.returncode}")
        print("STDOUT:", e.stdout)
        print("STDERR:", e.stderr)
        return False

def main():
    """Main test runner function."""
    print("🧪 Natural Language to Robot Framework - Test Suite Runner")
    print("=" * 70)
    
    # Change to project root directory
    project_root = Path(__file__).parent.parent
    os.chdir(project_root)
    
    # Test categories to run
    test_categories = [
        ("pytest tests/backend/test_docker_logging_integration.py -v", "Docker Logging Integration Tests"),
        ("pytest tests/backend/ -k 'not test_docker_logging_integration' --tb=short", "Backend Unit Tests"),
        ("pytest tests/integration/ --tb=short", "Integration Tests"),
        ("pytest tests/ --tb=short -x", "Full Test Suite (Stop on First Failure)"),
    ]
    
    results = {}
    
    for command, description in test_categories:
        success = run_command(command, description)
        results[description] = success
    
    # Summary
    print(f"\n{'='*70}")
    print("📊 TEST RESULTS SUMMARY")
    print(f"{'='*70}")
    
    all_passed = True
    for test_name, passed in results.items():
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{status} - {test_name}")
        if not passed:
            all_passed = False
    
    print(f"\n{'='*70}")
    if all_passed:
        print("🎉 ALL TESTS PASSED!")
        print("The project is ready for deployment.")
    else:
        print("⚠️  SOME TESTS FAILED!")
        print("Please review the failed tests and fix any issues.")
    print(f"{'='*70}")
    
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())