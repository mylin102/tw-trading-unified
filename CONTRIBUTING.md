# Contributing to tw-trading-unified

Thank you for your interest in contributing to the Taiwan Trading System! This document provides guidelines and instructions for contributing.

## Table of Contents
- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Code Style](#code-style)
- [Testing](#testing)
- [Security Guidelines](#security-guidelines)
- [Pull Request Process](#pull-request-process)
- [Release Process](#release-process)

## Code of Conduct

This project adheres to a Code of Conduct that all contributors are expected to follow. Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating.

## Getting Started

### Prerequisites
- Python 3.9+
- Git
- Pre-commit hooks

### Setup Development Environment

1. **Clone the repository**
   ```bash
   git clone https://github.com/your-org/tw-trading-unified.git
   cd tw-trading-unified
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt  # Development dependencies
   ```

4. **Install pre-commit hooks**
   ```bash
   pre-commit install
   pre-commit install --hook-type commit-msg
   ```

5. **Configure environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

## Development Workflow

### Branch Strategy
- `main`: Production-ready code
- `develop`: Integration branch for features
- `feature/*`: New features
- `bugfix/*`: Bug fixes
- `hotfix/*`: Critical production fixes

### Creating a Feature Branch
```bash
git checkout -b feature/your-feature-name
```

### Commit Messages

Follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Maintenance tasks

**Examples**:
```
feat(strategies): add adaptive momentum strategy
fix(core): resolve position sync issue
docs: update API documentation
```

## Code Style

### Python Style Guide
We follow [PEP 8](https://pep8.org/) with some modifications:

- **Line length**: 100 characters
- **Imports**: Grouped and sorted with isort
- **Type hints**: Required for all public functions
- **Docstrings**: Google style for all modules, classes, and functions

### Automatic Formatting
We use automated tools to maintain code quality:

```bash
# Format code
black .
isort .

# Lint code
ruff check --fix .
ruff format .

# Type checking
mypy .
```

### Pre-commit Hooks
Pre-commit hooks automatically check your code before committing:
```bash
# Run on staged files
pre-commit run

# Run on all files
pre-commit run --all-files
```

## Testing

### Test Structure
```
tests/
├── unit/           # Unit tests
├── integration/    # Integration tests
├── e2e/           # End-to-end tests
├── fixtures/      # Test data
└── conftest.py    # Test configuration
```

### Running Tests
```bash
# Run all tests
pytest

# Run specific test category
pytest tests/unit -v
pytest -m "not slow"  # Skip slow tests

# Run with coverage
pytest --cov=. --cov-report=html
```

### Test Coverage Requirements
- Minimum coverage: 80%
- New features must include tests
- Bug fixes must include regression tests

### Test Data
- Use fixtures for test data
- Never commit real trading data
- Mock external APIs in unit tests

## Security Guidelines

### Sensitive Information
- **Never commit** API keys, passwords, or secrets
- Use environment variables for configuration
- Store secrets in `.env` file (added to `.gitignore`)

### Security Checks
```bash
# Check for vulnerabilities
safety check -r requirements.txt
bandit -r .
```

### Trading System Security
- Validate all inputs
- Implement rate limiting
- Log security events
- Regular security audits

## Pull Request Process

### Before Creating a PR
1. Ensure all tests pass
2. Update documentation if needed
3. Add tests for new functionality
4. Run pre-commit checks

### PR Checklist
- [ ] Tests added/updated
- [ ] Documentation updated
- [ ] Code follows style guidelines
- [ ] Security considerations addressed
- [ ] No breaking changes (or documented)

### PR Description Template
```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] Unit tests added
- [ ] Integration tests added
- [ ] Manual testing performed

## Checklist
- [ ] Code follows style guidelines
- [ ] Self-review completed
- [ ] Comments added for complex code
- [ ] Documentation updated

## Related Issues
Closes #123

## Screenshots (if applicable)
```

### Code Review Process
1. At least one approval required
2. All CI checks must pass
3. Security review for sensitive changes
4. Performance review for critical paths

## Release Process

### Versioning
We follow [Semantic Versioning](https://semver.org/):
- **MAJOR**: Breaking changes
- **MINOR**: New features (backward compatible)
- **PATCH**: Bug fixes (backward compatible)

### Release Checklist
1. Update version in `VERSION` file
2. Update `CHANGELOG.md`
3. Run full test suite
4. Create release tag
5. Deploy to staging environment
6. Verify deployment
7. Deploy to production

### Hotfix Process
For critical production issues:
1. Create `hotfix/*` branch from `main`
2. Fix the issue with minimal changes
3. Test thoroughly
4. Merge to `main` and `develop`
5. Deploy immediately

## Documentation

### Documentation Structure
```
docs/
├── api/           # API documentation
├── architecture/  # System architecture
├── guides/        # How-to guides
├── reference/     # Reference material
└── tutorials/     # Step-by-step tutorials
```

### Writing Documentation
- Use Markdown format
- Include code examples
- Keep documentation up-to-date
- Add diagrams for complex concepts

## Getting Help

- **Issues**: Use GitHub Issues for bug reports and feature requests
- **Discussions**: Use GitHub Discussions for questions and ideas
- **Slack**: Join our Slack channel for real-time discussion

## Acknowledgments
- Thank all contributors
- Reference external libraries
- Acknowledge inspiration sources

---

*Last updated: 2026-04-21*  
*Version: 1.0*