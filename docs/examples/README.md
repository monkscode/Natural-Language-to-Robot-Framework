# Examples

This directory contains demonstration scripts that show how to use various components of the Natural Language to Robot Framework project with self-healing capabilities.

## Available Examples

### 🔍 `failure_detection_demo.py`
Demonstrates the failure detection service capabilities:
- Analyzing Robot Framework output.xml files
- Classifying different types of failures
- Identifying healable vs non-healable failures
- Providing healing recommendations

**Usage:**
```bash
cd docs/examples
python failure_detection_demo.py
```

### 🤖 `healing_agents_demo.py`
Shows how the AI healing agents work together:
- Failure analysis agent workflow
- Locator generation agent capabilities
- Locator validation agent process
- Complete healing workflow simulation

**Usage:**
```bash
cd docs/examples
python healing_agents_demo.py
```

### 🔎 `fingerprinting_demo.py`
Demonstrates the element fingerprinting system:
- Creating element fingerprints
- Storing and retrieving fingerprints
- Matching fingerprints against modified DOM
- Handling similar elements and error scenarios

**Usage:**
```bash
cd docs/examples
python fingerprinting_demo.py
```

### 📝 `test_code_updater_demo.py`
Shows the test code updater functionality:
- Single and multiple locator updates
- Backup creation and restoration
- Syntax validation
- Error handling scenarios

**Usage:**
```bash
cd docs/examples
python test_code_updater_demo.py
```

## Prerequisites

To run these examples, you need:

1. **Virtual Environment**: Activate the project's virtual environment
   ```bash
   source venv/bin/activate  # Linux/Mac
   # or
   venv\Scripts\activate     # Windows
   ```

2. **Dependencies**: Install project dependencies
   ```bash
   pip install -r requirements-dev.txt
   ```

3. **Project Structure**: Run from the project root directory or ensure proper Python path setup

## Example Output

Each demo provides detailed output showing:
- ✅ Successful operations
- ⚠️  Warnings and handled errors
- 📊 Results and statistics
- 🎉 Completion summaries

## Integration with Main Application

These examples demonstrate the same services and components used by the main application:

- **Failure Detection Service** - Used in the healing workflow
- **Healing Agents** - Core AI components for self-healing
- **Fingerprinting Service** - Element identification and matching
- **Test Code Updater** - Automated test maintenance

## Troubleshooting

### Import Errors
If you see import errors:
```
⚠️  Could not import backend services. This demo requires the full application environment.
```

**Solution:**
1. Ensure you're running from the project root
2. Activate the virtual environment
3. Install all dependencies

### Missing Dependencies
If specific libraries are missing:
```bash
pip install -r requirements-dev.txt
```

### Path Issues
If Python can't find the modules:
```bash
# Run from project root
cd /path/to/Natural-Language-to-Robot-Framework
python docs/examples/failure_detection_demo.py
```

## Contributing

When adding new examples:

1. **Follow the pattern**: Use the same structure as existing examples
2. **Add error handling**: Include proper import error handling
3. **Document usage**: Update this README with the new example
4. **Test thoroughly**: Ensure examples work in different environments

## Related Documentation

- [Project Structure](../PROJECT_STRUCTURE.md) - Overall project organization
- [Healing API](../healing_api.md) - API documentation
- [Configuration](../../config/self_healing.yaml) - System configuration