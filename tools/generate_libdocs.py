#!/usr/bin/env python3
"""
Generate Robot Framework library documentation as JSON files.

This script generates libdoc JSON files for Robot Framework libraries
during the Docker build stage. The JSON files are then copied to the
runtime image, allowing the application to use library documentation
without needing the actual libraries installed.

Usage:
    python tools/generate_libdocs.py [output_directory]
    
The default output directory is ./data/libdocs/
"""

import json
import os
import sys
import tempfile
from pathlib import Path


def generate_libdoc(library_name: str, output_dir: Path) -> bool:
    """
    Generate libdoc JSON for a library.
    
    Args:
        library_name: Name of the Robot Framework library
        output_dir: Directory to write the JSON file
        
    Returns:
        True if successful, False otherwise
    """
    try:
        from robot.libdoc import libdoc
        
        output_file = output_dir / f"{library_name.lower()}.json"
        
        print(f"Generating libdoc for {library_name}...")
        
        # Generate libdoc JSON
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as temp_file:
            temp_path = temp_file.name

        try:
            libdoc(library_name, temp_path, format='JSON')

            # Read and rewrite to output directory
            with open(temp_path, 'r', encoding='utf-8') as f:
                doc_data = json.load(f)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        
        # Write to output directory
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(doc_data, f, indent=2)
        
        keyword_count = len(doc_data.get('keywords', []))
        version = doc_data.get('version', 'Unknown')
        print(f"  ✓ {library_name} v{version}: {keyword_count} keywords")
        
        return True
        
    except ImportError as e:
        print(f"  ✗ {library_name}: Library not installed - {e}")
        return False
    except Exception as e:
        print(f"  ✗ {library_name}: Error generating libdoc - {e}")
        return False


def main():
    # Default output directory
    output_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "./data/libdocs")
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Generating libdocs to: {output_dir.absolute()}")
    print("-" * 50)
    
    # Libraries to generate docs for
    libraries = [
        "SeleniumLibrary",
        "Browser",
        "BuiltIn"
    ]

    # Browser is optional - it may not be installed in all environments
    optional_libs = {"Browser"}

    success_count = 0
    failed_required = []
    failed_optional = []
    for library in libraries:
        if generate_libdoc(library, output_dir):
            success_count += 1
        elif library in optional_libs:
            failed_optional.append(library)
        else:
            failed_required.append(library)
    
    print("-" * 50)
    print(f"Generated {success_count}/{len(libraries)} library documentation files")

    if failed_optional:
        print(f"Optional libraries skipped (non-fatal): {', '.join(failed_optional)}")

    if failed_required:
        print(f"Missing required libdocs: {', '.join(failed_required)}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
