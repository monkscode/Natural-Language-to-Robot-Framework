# Frequently Asked Questions

## General Questions

### Do I need to know programming to use Mark 1?
No! That's the whole point. Just describe your test in plain English and Mark 1 generates the code for you.

### What websites does Mark 1 work with?
Mark 1 works with 95%+ of modern websites. It's particularly good with:
- E-commerce sites (Amazon, Flipkart, eBay)
- Social media platforms
- Standard web applications
- SaaS products

### Is Mark 1 free?
Yes! Mark 1 is open source (Apache 2.0 License). You only pay for AI model API calls:
- **Google Gemini**: Free tier includes 1,500 requests/day
- **Ollama**: Completely free (runs locally)

### Can I use Mark 1 offline?
Yes, if you use Ollama with local models. However, online models (Gemini) generally provide better results and faster performance.

## Technical Questions

### How accurate is the element detection?
Mark 1 achieves 95%+ success rate on first try using advanced AI vision technology. The system:
- Understands context and intent
- Adapts to dynamic websites
- Generates stable, maintainable locators

### What if a test fails?
Check the detailed HTML logs in `robot_tests/{run-id}/log.html`. They show:
- Exact step where failure occurred
- Screenshots at failure point
- Complete error messages
- Element locators used

### Can I edit the generated tests?
Absolutely! The generated `.robot` files are standard Robot Framework code. You can:
- Edit them manually
- Add custom keywords
- Integrate with existing test suites
- Version control them

### Does Mark 1 handle dynamic websites?
Yes. Mark 1 adapts to dynamic content and generates stable locators using best practices:
- Prioritizes stable attributes (id, name, data-*)
- Avoids brittle selectors (dynamic classes)
- Adds appropriate waits for AJAX content

### Can I integrate Mark 1 with CI/CD?
Yes! Two options:
1. **API Integration**: Call the `/generate-and-run` endpoint from your CI pipeline
2. **Direct Execution**: Run the generated `.robot` files using Robot Framework CLI

### How much does it cost to run?
With Gemini's free tier (1,500 requests/day), you can generate 100-200 tests per day at no cost.

**Paid tier**: ~$0.001 per request (very affordable for most use cases)

## Comparison Questions

### How is Mark 1 different from Selenium IDE?
| Feature | Mark 1 | Selenium IDE |
|---------|--------|--------------|
| Input | Natural language | Record actions |
| Output | Robot Framework | Selenium code |
| Element Detection | AI-powered | Record only |
| Maintenance | Regenerate | Re-record |

### How is Mark 1 different from Playwright Codegen?
Playwright Codegen requires recording actions and generates code in Python/JS/Java. Mark 1 understands natural language and creates Robot Framework tests without any recording.

### Can Mark 1 replace manual testing?
Mark 1 excels at automating repetitive test scenarios. It complements (not replaces) manual exploratory testing and edge case validation.

## Troubleshooting Questions

### Why is my test failing with "Element not found"?
Common causes:
1. **Website structure changed** - Dynamic sites update frequently
2. **Element requires scrolling** - Element not in viewport
3. **Popup blocking element** - Modal or overlay in the way
4. **Timing issue** - Element loads after page load

**Solution**: Try being more specific in your query or regenerate the test.

### The browser automation service isn't starting
Check if port 4999 is available:

```bash
# Linux/Mac
lsof -i :4999

# Windows
netstat -ano | findstr :4999
```

If port is in use, kill the process or change the port in configuration.

### Can I run multiple tests in parallel?
Currently, Mark 1 processes one test at a time. Parallel execution is on the roadmap for future releases.

### Tests work locally but fail in CI/CD
Common issues:
- Docker not available in CI environment
- Missing environment variables
- Network restrictions
- Insufficient resources

Ensure your CI environment has Docker and all required dependencies.

### How do I debug generated tests?
1. Open `robot_tests/{run-id}/log.html` in browser
2. Check application logs in `logs/` directory
3. Run the `.robot` file manually with verbose output:
   ```bash
   robot --loglevel DEBUG robot_tests/{run-id}/test.robot
   ```

## Performance Questions

### How long does test generation take?
- **Simple tests**: 15-20 seconds
- **Complex tests**: 25-35 seconds
- **Execution time**: Varies by website (typically 10-60 seconds)

### Can I speed up test generation?
Yes:
- Use faster AI models (gemini-2.5-flash)
- Simplify your test queries
- Ensure good network connectivity
- Use local models (Ollama) for faster response

### What are the rate limits?
**Gemini Free Tier**:
- 1,500 requests per day
- 15 requests per minute

**Ollama**:
- No rate limits (runs locally)
- Limited by your hardware

## Security & Privacy Questions

### Is my data secure?
Yes:
- API keys stored locally in `.env` file
- Test data processed locally or in your chosen cloud
- Browser sessions isolated in Docker containers
- Logs stored locally only

### Can I use Mark 1 with sensitive data?
For maximum privacy, use Ollama with local models. This ensures:
- No data leaves your machine
- Complete control over AI processing
- No third-party API calls

### Are API keys committed to git?
No. The `.env` file is in `.gitignore` by default. Never commit API keys to version control.

## Need More Help?

- 📚 Check other documentation in the [docs](.) folder
- 🐛 Report bugs on [GitHub Issues](https://github.com/monkscode/Natural-Language-to-Robot-Framework/issues)
- 💬 Ask questions in [GitHub Discussions](https://github.com/monkscode/Natural-Language-to-Robot-Framework/discussions)
