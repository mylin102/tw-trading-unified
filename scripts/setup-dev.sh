#!/bin/bash
# setup-dev.sh - Development environment setup script for tw-trading-unified

set -e  # Exit on error

echo "🚀 Setting up tw-trading-unified development environment..."
echo "=========================================================="

# Check Python version
echo "🔍 Checking Python version..."
python_version=$(python3 --version | cut -d' ' -f2)
required_version="3.9.0"

if [ "$(printf '%s\n' "$required_version" "$python_version" | sort -V | head -n1)" = "$required_version" ]; then
    echo "✅ Python $python_version detected (>= $required_version)"
else
    echo "❌ Python $python_version detected, but $required_version or higher is required"
    exit 1
fi

# Create virtual environment
echo "🔧 Creating virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "✅ Virtual environment created"
else
    echo "⚠️  Virtual environment already exists"
fi

# Activate virtual environment
echo "🔧 Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "📦 Upgrading pip..."
pip install --upgrade pip

# Install requirements
echo "📦 Installing requirements..."
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
    echo "✅ Production requirements installed"
else
    echo "❌ requirements.txt not found"
    exit 1
fi

# Install development requirements
echo "📦 Installing development requirements..."
dev_req_file="requirements-dev.txt"
if [ -f "$dev_req_file" ]; then
    pip install -r "$dev_req_file"
    echo "✅ Development requirements installed"
else
    echo "⚠️  $dev_req_file not found, installing common dev packages..."
    pip install pytest pytest-cov black isort ruff mypy bandit safety pre-commit
    echo "✅ Common development packages installed"
fi

# Install pre-commit hooks
echo "🔧 Installing pre-commit hooks..."
if command -v pre-commit &> /dev/null; then
    pre-commit install
    pre-commit install --hook-type commit-msg
    echo "✅ Pre-commit hooks installed"
else
    echo "❌ pre-commit not found, please install it manually"
fi

# Create .env file if it doesn't exist
echo "🔧 Setting up environment variables..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "✅ .env file created from example"
        echo "⚠️  Please edit .env file with your configuration"
    else
        echo "⚠️  .env.example not found, creating basic .env file"
        cat > .env << EOF
# Trading System Configuration
# ============================

# Shioaji API Configuration
SHIOAJI_API_KEY=your_api_key_here
SHIOAJI_SECRET_KEY=your_secret_key_here

# Trading Mode
TRADING_MODE=PAPER  # PAPER or LIVE
PAPER_CAPITAL=40000  # TWD

# Logging
LOG_LEVEL=INFO
LOG_FILE=logs/trading_system.log

# Dashboard
DASHBOARD_PORT=8500
DASHBOARD_PASSWORD=5888

# Data Storage
DATA_DIR=./data
EXPORTS_DIR=./exports
EOF
        echo "✅ Basic .env file created"
        echo "⚠️  Please edit .env file with your actual configuration"
    fi
else
    echo "✅ .env file already exists"
fi

# Create necessary directories
echo "🔧 Creating necessary directories..."
directories=("logs" "exports" "data" "tests/fixtures")
for dir in "${directories[@]}"; do
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
        echo "  Created: $dir"
    fi
done

# Set up test data
echo "🔧 Setting up test data..."
if [ ! -f "tests/fixtures/README.md" ]; then
    cat > tests/fixtures/README.md << EOF
# Test Fixtures

This directory contains test data for the trading system.

## Structure
- \`futures/\`: Futures trading test data
- \`options/\`: Options trading test data  
- \`stocks/\`: Stocks trading test data

## Guidelines
1. Never commit real trading data
2. Use synthetic/mocked data for tests
3. Keep test data small and focused
4. Document the purpose of each fixture
EOF
    echo "✅ Test fixtures directory initialized"
fi

# Run initial checks
echo "🔍 Running initial checks..."
echo "1. Checking Python syntax..."
if python -m py_compile $(find . -name "*.py" -not -path "./venv/*" -not -path "./tests/fixtures/*" | head -10); then
    echo "✅ Python syntax check passed"
else
    echo "❌ Python syntax check failed"
fi

echo "2. Running smoke tests..."
if python -m pytest tests/test_ci_smoke.py -v; then
    echo "✅ Smoke tests passed"
else
    echo "❌ Smoke tests failed"
fi

echo "3. Checking code formatting..."
if black --check . --exclude="venv|tests/fixtures"; then
    echo "✅ Code formatting check passed"
else
    echo "⚠️  Code formatting issues found, run 'black .' to fix"
fi

# Final instructions
echo ""
echo "🎉 Development environment setup complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Edit the .env file with your configuration:"
echo "   - SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY"
echo "   - Trading mode and capital settings"
echo ""
echo "2. Activate the virtual environment:"
echo "   source venv/bin/activate"
echo ""
echo "3. Run pre-commit checks before committing:"
echo "   pre-commit run --all-files"
echo ""
echo "4. Run tests:"
echo "   pytest -v"
echo ""
echo "5. Start the trading system:"
echo "   python main.py"
echo ""
echo "For more information, see CONTRIBUTING.md"
echo ""

# Deactivate virtual environment
deactivate

echo "✅ Setup script completed successfully!"