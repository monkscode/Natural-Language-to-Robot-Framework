# Contributing to Mark 1 - Natural Language to Robot Framework

We welcome contributions to Mark 1! We are grateful for your interest in making this project better.

## How to Contribute

We appreciate all contributions, whether it's bug fixes, new features, documentation improvements, or suggestions.

### Development Setup

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/your-username/mark-1.git
   cd mark-1
   ```
3. **Set up the development environment**:
   ```bash
   # Create and activate virtual environment
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   
   # Install dependencies
   pip install -r src/backend/requirements.txt
   ```
4. **Configure your environment**:
   ```bash
   cp src/backend/.env.example src/backend/.env
   # Edit .env and add your API keys
   ```

### Making Changes

1. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feature/amazing-feature
   ```
   Use descriptive branch names:
   - `feature/add-api-testing` for new features
   - `fix/element-detection-bug` for bug fixes
   - `docs/update-readme` for documentation

2. **Make your changes**:
   - Write clean, readable code
   - Follow existing code style and conventions
   - Add comments for complex logic
   - Update documentation if needed

3. **Test your changes**:
   ```bash
   # Run the test script
   ./test.sh
   
   # Or test manually
   python tools/browser_use_service.py  # Terminal 1
   ./run.sh                              # Terminal 2
   ```

4. **Commit your changes**:
   ```bash
   git add .
   git commit -m "Add amazing feature"
   ```
   Write clear commit messages:
   - Use present tense ("Add feature" not "Added feature")
   - Be descriptive but concise
   - Reference issues if applicable (#123)

5. **Push to your fork**:
   ```bash
   git push origin feature/amazing-feature
   ```

6. **Open a Pull Request**:
   - Go to the original repository on GitHub
   - Click "New Pull Request"
   - Select your branch
   - Describe your changes clearly
   - Link any related issues

### What to Contribute

**Bug Fixes**
- Found a bug? Open an issue first, then submit a fix
- Include steps to reproduce the bug
- Add tests if possible

**New Features**
- Discuss major features in an issue before starting
- Keep features focused and well-documented
- Update README if user-facing

**Documentation**
- Fix typos, improve clarity
- Add examples and use cases
- Update outdated information

**Code Quality**
- Refactor complex code
- Improve error messages
- Add type hints and comments

### Code Guidelines

- **Python Style**: Follow PEP 8 conventions
- **Naming**: Use descriptive variable and function names
- **Comments**: Explain why, not what
- **Error Handling**: Use appropriate exceptions with clear messages
- **Dependencies**: Minimize new dependencies; discuss if needed

### Testing

Before submitting a PR:
- Test your changes locally
- Ensure existing tests still pass
- Add new tests for new features
- Verify the application starts without errors

### Pull Request Process

1. Ensure your PR has a clear title and description
2. Link related issues using keywords (Fixes #123, Closes #456)
3. Wait for review from maintainers
4. Address any requested changes
5. Once approved, your PR will be merged

### Getting Help

- **Questions?** Open a discussion on GitHub
- **Stuck?** Comment on your PR or issue
- **Ideas?** Share them in GitHub Discussions

## Contributor License Agreement (CLA)

Before we can accept your contribution, we need you to agree to our Contributor License Agreement (CLA). This is a common practice in open source projects. The CLA protects you and us, and ensures that we have the necessary rights to use your contribution.

You can find the full text of the CLA in the [CLA.md](CLA.md) file in this repository. **By submitting a pull request, you are agreeing to the terms of the CLA.**

## Code of Conduct

- Be respectful and inclusive
- Welcome newcomers and help them learn
- Focus on constructive feedback
- Assume good intentions

## License

This project is licensed under the Apache 2.0 License. You can find the full text of the license in the [LICENSE](LICENSE) file in this repository.

By contributing, you agree that your contributions will be licensed under the Apache 2.0 License.

---

**Thank you for contributing to Mark 1!** 🎉

Your contributions help make test automation accessible to everyone.
