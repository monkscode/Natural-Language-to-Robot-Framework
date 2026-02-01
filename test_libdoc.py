#!/usr/bin/env python3
"""Test script to verify libdoc loading."""

from src.backend.crew_ai.library_context.dynamic_context import DynamicLibraryDocumentation

print("Testing pre-generated libdoc loading...")
print("-" * 50)

# Test SeleniumLibrary
try:
    doc = DynamicLibraryDocumentation('SeleniumLibrary')
    data = doc.get_library_documentation()
    print(f"✓ SeleniumLibrary loaded successfully")
    print(f"  - Version: {data.get('version', 'Unknown')}")
    print(f"  - Keywords: {len(data.get('keywords', []))}")
except Exception as e:
    print(f"✗ SeleniumLibrary failed: {e}")

print()

# Test BuiltIn
try:
    doc = DynamicLibraryDocumentation('BuiltIn')
    data = doc.get_library_documentation()
    print(f"✓ BuiltIn loaded successfully")
    print(f"  - Version: {data.get('version', 'Unknown')}")
    print(f"  - Keywords: {len(data.get('keywords', []))}")
except Exception as e:
    print(f"✗ BuiltIn failed: {e}")

print()
print("-" * 50)
print("Libdoc loading test completed!")
