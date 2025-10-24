# Best Practices Guide

Get the most out of Mark 1 with these proven best practices.

## Writing Effective Test Queries

### Be Specific

**✅ Good Examples:**
```
"Open Flipkart and search for shoes and then get the first product name"
"Navigate to GitHub using url https://github.com/monkscode, and then get the name of the Pinned project"
"Go to amazon.com, search for laptop, and click the first result"
```

**❌ Avoid:**
```
"Test the search"  (too vague)
"Check if everything works"  (not specific)
"Search for products"  (missing details)
"Test the website"  (no clear goal)
```

### Structure Your Queries

Use clear, sequential steps:

1. **Navigation**: Where to go
2. **Action**: What to do
3. **Verification**: What to check

**Example:**
```
"Open flipkart.com, search for 'running shoes', and verify the first product has a price"
```

### Mention Exact Elements

Be explicit about which elements to interact with:

**✅ Good:**
- "first product name"
- "login button in header"
- "search input at top"
- "submit button in form"

**❌ Avoid:**
- "the product"
- "a button"
- "some text"

### Use Full URLs

**✅ Good:**
```
"Navigate to https://github.com/monkscode"
```

**❌ Avoid:**
```
"Go to GitHub"  (ambiguous)
```

### One Goal Per Test

Keep tests focused on a single workflow:

**✅ Good:**
```
"Search for shoes on Flipkart and get the first product name"
```

**❌ Avoid:**
```
"Search for shoes, add to cart, checkout, verify order, check email"
```
(Too many steps - split into multiple tests)

## Test Organization

### Naming Conventions

Use descriptive names for test runs:

```
search_flipkart_shoes_2024-01-15
github_profile_navigation
amazon_product_search_laptop
```

### Directory Structure

Organize generated tests:

```
robot_tests/
├── e-commerce/
│   ├── flipkart_search/
│   └── amazon_checkout/
├── social-media/
│   ├── github_profile/
│   └── twitter_post/
└── internal-apps/
    └── admin_dashboard/
```

### Version Control

**Do commit:**
- Generated `.robot` files (after review)
- Test documentation
- Configuration templates

**Don't commit:**
- `.env` files with API keys
- Temporary test runs
- Log files

## Performance Optimization

### Query Optimization

**Fast queries:**
- Simple navigation + one action
- Clear element identifiers
- Direct URLs

**Slow queries:**
- Multiple complex steps
- Ambiguous element descriptions
- Conditional logic

### Model Selection

**For speed:**
```env
ONLINE_MODEL=gemini-2.5-flash
```

**For accuracy:**
```env
ONLINE_MODEL=gemini-1.5-pro-latest
```

**For privacy:**
```env
MODEL_PROVIDER=local
LOCAL_MODEL=llama3.1
```

### Timeout Configuration

Adjust based on website complexity:

```env
# Fast websites
BROWSER_USE_TIMEOUT=300

# Standard websites
BROWSER_USE_TIMEOUT=600

# Complex/slow websites
BROWSER_USE_TIMEOUT=900
```

## Reliability Best Practices

### Handle Dynamic Content

For websites with dynamic content:

1. **Be specific about timing:**
   ```
   "Wait for search results to load, then get the first product"
   ```

2. **Use stable identifiers:**
   - Prefer: id, name, data-* attributes
   - Avoid: dynamic classes, nth-child selectors

### Deal with Popups

If popups are common:

```
"Close any popups, then search for shoes on Flipkart"
```

Mark 1 will handle popup detection intelligently.

### Test on Stable Environments

- Use production URLs (not staging with frequent changes)
- Test during off-peak hours
- Avoid websites under active development

## Maintenance Strategies

### Regular Regeneration

Websites change frequently. Regenerate tests:
- Monthly for stable sites
- Weekly for frequently updated sites
- After major website updates

### Review Generated Code

Always review generated `.robot` files:
- Check locators make sense
- Verify test logic is correct
- Add custom assertions if needed

### Keep Tests Simple

Simple tests are easier to maintain:
- One workflow per test
- Clear, linear steps
- Minimal conditional logic

## Security & Privacy

### Handling Sensitive Data

**For sensitive workflows:**
1. Use Ollama (local models)
2. Don't include credentials in queries
3. Use environment variables for secrets

**Example:**
```robot
*** Variables ***
${USERNAME}    %{TEST_USERNAME}
${PASSWORD}    %{TEST_PASSWORD}
```

### API Key Management

- Store keys in `.env` (never commit)
- Rotate keys regularly
- Use separate keys for dev/prod
- Monitor usage in Google AI Studio

### Data Privacy

**What Mark 1 sees:**
- Your test queries
- Website URLs
- Element descriptions

**What Mark 1 doesn't see:**
- Actual credentials (unless you include them)
- Personal data on websites
- Browser history

## CI/CD Integration

### API Integration

```bash
curl -X POST http://localhost:5000/generate-and-run \
  -H "Content-Type: application/json" \
  -d '{"query": "your test query"}'
```

### Direct Execution

```bash
# Generate test first
# Then run in CI
robot robot_tests/{run-id}/test.robot
```

### Environment Setup

Ensure CI environment has:
- Docker installed and running
- Environment variables configured
- Sufficient resources (4GB RAM minimum)

## Debugging Strategies

### Use HTML Logs

Always check `log.html` first:
- Shows exact failure point
- Includes screenshots
- Complete error messages

### Enable Verbose Logging

For troubleshooting:

```env
LOG_LEVEL=DEBUG
CREWAI_VERBOSE=true
```

### Test Incrementally

Break complex queries into smaller parts:

1. Test navigation first
2. Add search action
3. Add verification

### Manual Verification

If test fails:
1. Visit website manually
2. Check element exists
3. Verify no popups/modals
4. Test with different browser

## Common Patterns

### E-commerce Testing

```
"Open {site}, search for {product}, get first result price"
"Navigate to {site}, add {product} to cart, verify cart count"
"Go to {site}, filter by {category}, sort by price"
```

### Form Testing

```
"Fill login form with username and password, click submit"
"Complete registration form with test data, verify success message"
"Submit contact form and check confirmation"
```

### Navigation Testing

```
"Navigate to {url}, click {menu item}, verify page title"
"Go to {site}, follow {link}, check URL changed"
"Open {page}, scroll to {section}, verify content"
```

### Data Extraction

```
"Get all product names from search results"
"Extract price from product page"
"Collect all links from navigation menu"
```

## Limitations to Consider

### What Works Well

✅ Standard web interactions (click, type, navigate)
✅ Element detection and verification
✅ Form filling and submission
✅ Data extraction from pages
✅ Multi-step workflows

### Current Limitations

❌ File uploads/downloads
❌ Complex authentication (OAuth, 2FA)
❌ Mobile app testing
❌ API testing
❌ Database validation
❌ Email verification
❌ Parallel execution

### Workarounds

For unsupported features:
1. Generate partial test with Mark 1
2. Manually add missing functionality
3. Use Robot Framework's full capabilities

## Getting Better Results

### Iterate on Queries

If first attempt doesn't work:
1. Make query more specific
2. Break into smaller steps
3. Try different wording
4. Check website manually

### Learn from Logs

Review successful tests to understand:
- What query patterns work best
- How Mark 1 interprets instructions
- Which locator strategies are used

### Provide Feedback

Help improve Mark 1:
- Report issues on GitHub
- Share successful patterns
- Suggest improvements
- Contribute to documentation

## Resources

- [FAQ](FAQ.md) - Common questions
- [Troubleshooting](TROUBLESHOOTING.md) - Fix issues
- [Configuration](CONFIGURATION.md) - Customize settings
- [Contributing](../CONTRIBUTING.md) - Help improve Mark 1

---

**Remember:** Mark 1 is a tool to accelerate test creation, not replace testing expertise. Use it to handle repetitive tasks while you focus on test strategy and edge cases.
