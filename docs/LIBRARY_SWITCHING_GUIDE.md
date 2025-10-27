# Library Switching Guide

Quick guide for switching between Browser Library (Playwright) and SeleniumLibrary, with common commands and best practices.

## Switching Between Browser Library and SeleniumLibrary

**Change library:**
```bash
# Edit src/backend/.env
ROBOT_LIBRARY=browser  # or selenium

# Restart Mark 1
./run.sh
```

**That's it!** All new tests will use the selected library.

## Library Comparison

| Feature | Browser Library | SeleniumLibrary |
|---------|----------------|-----------------|
| **Speed** | 2-3x faster ⚡ | Baseline |
| **Modern Web** | Full support ✅ | Limited ⚠️ |
| **Auto-waiting** | Built-in ✅ | Manual ❌ |
| **AI Compatibility** | Excellent ✅ | Good ⚠️ |
| **Stability** | Production-ready ✅ | Battle-tested ✅ |
| **Learning Curve** | Low | Low |

**Recommendation:** Use Browser Library for new projects ⭐

## Common Keywords

### Browser Library
```robot
New Browser    chromium    headless=False
New Context    viewport=None
New Page    https://example.com
Fill Text    name=q    search term
Click    text=Submit
${text}=    Get Text    css=.result
Close Browser
```

### SeleniumLibrary
```robot
Open Browser    https://example.com    chrome
Input Text    name=q    search term
Click Element    xpath=//button[text()='Submit']
${text}=    Get Text    css=.result
Close Browser
```

## Locator Strategies

### Browser Library (Recommended Order)
1. **Text-based** - `text=Login` (most stable!)
2. **Role-based** - `role=button[name="Submit"]`
3. **Data attributes** - `data-testid=submit-btn`
4. **ID** - `id=login-btn`
5. **CSS** - `css=button.primary`
6. **XPath** - `xpath=//button[@type='submit']`

### SeleniumLibrary
1. **ID** - `id=login-btn`
2. **Name** - `name=username`
3. **Data attributes** - `css=[data-testid="submit-btn"]`
4. **CSS** - `css=button.primary`
5. **XPath** - `xpath=//button[@type='submit']`

## Configuration Quick Reference

```env
# AI Provider
MODEL_PROVIDER=online              # or "local"
GEMINI_API_KEY=your-key-here      # Get from https://aistudio.google.com/app/apikey
ONLINE_MODEL=gemini-2.5-flash     # Fast and accurate

# Application
APP_PORT=5000                      # Change if port in use

# Browser Automation
BROWSER_USE_SERVICE_URL=http://localhost:4999
BROWSER_USE_TIMEOUT=900           # 15 minutes

# Robot Framework Library
ROBOT_LIBRARY=browser             # Recommended: "browser" or "selenium"

# Logging
LOG_LEVEL=INFO                    # DEBUG for troubleshooting
```

## Common Issues

### "Element not found"
**Solution:** Be more specific in your query
```
# Instead of: "click the button"
# Use: "click the submit button in the header"
```

### "Docker is not available"
**Solution:** Start Docker Desktop

### "GEMINI_API_KEY not found"
**Solution:** 
1. Get key from https://aistudio.google.com/app/apikey
2. Add to `src/backend/.env`
3. Restart Mark 1

### Tests are slow
**Solution:**
1. Use `ROBOT_LIBRARY=browser` (2-3x faster)
2. Use `headless=True` in production
3. Check network speed

### Generated code uses wrong library
**Solution:**
1. Check `ROBOT_LIBRARY` in `.env`
2. Restart Mark 1
3. Generate new test

## Best Practices

### Writing Queries
✅ **Good:** "Open Flipkart and search for shoes and get the first product name"
❌ **Bad:** "Test Flipkart"

✅ **Good:** "Click the login button in the header"
❌ **Bad:** "Click button"

### Locator Selection
✅ **Good:** Use text-based locators (`text=Login`)
❌ **Bad:** Use dynamic classes (`css=.btn-active-hover`)

✅ **Good:** Use data-testid attributes
❌ **Bad:** Use complex XPath

### Test Organization
✅ **Good:** One clear objective per test
❌ **Bad:** Multiple unrelated actions

## Performance Tips

1. **Use Browser Library** - 2-3x faster than Selenium
2. **Use headless mode** - Faster in production
3. **Be specific** - Reduces element search time
4. **Batch operations** - Mark 1 finds all elements in one session
5. **Use text locators** - Faster than XPath

## Getting Help

- 📚 **Full Docs:** [Documentation Index](README.md)
- ⚙️ **Configuration:** [Configuration Guide](CONFIGURATION.md)
- 🐛 **Issues:** [Troubleshooting Guide](TROUBLESHOOTING.md)
- 💬 **Questions:** [GitHub Discussions](https://github.com/monkscode/Natural-Language-to-Robot-Framework/discussions)

## Useful Links

- [Google AI Studio](https://aistudio.google.com/app/apikey) - Get Gemini API key
- [Browser Library Docs](https://marketsquare.github.io/robotframework-browser/) - Official docs
- [Robot Framework Docs](https://robotframework.org/) - Robot Framework
- [Playwright Docs](https://playwright.dev/python/) - Playwright Python

---

**Pro Tip:** Use Browser Library (`ROBOT_LIBRARY=browser`) for best performance and modern web support! ⭐
