#!/bin/bash
# GCP Ubuntu 22.04 LTS Environment Setup Script
# Purpose: Initialize trading environment without Docker
# Rational: Ensure all C++ dependencies (TA-Lib) and Python libs are ready for production

set -e

echo "🚀 Starting GCP Trading Environment Setup..."

# 1. Update System
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y build-essential wget git python3-pip python3-dev \
    libatlas-base-dev gfortran pkg-config cmake \
    tmux htop vnstat  # Operational tools

# 2. Install TA-Lib (C++ Source)
if [ ! -f "/usr/local/lib/libta_lib.so" ]; then
    echo "📦 Installing TA-Lib C++ dependencies..."
    wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz
    tar -xzf ta-lib-0.4.0-src.tar.gz
    cd ta-lib/
    ./configure --prefix=/usr
    make
    sudo make install
    cd ..
    rm -rf ta-lib ta-lib-0.4.0-src.tar.gz
    echo "✅ TA-Lib installed."
else
    echo "✅ TA-Lib already exists."
fi

# 3. Python Dependencies
echo "🐍 Installing Python packages..."
pip3 install --upgrade pip
pip3 install -r requirements.txt

# 4. Specialized compilations (Numba/Pandas-TA)
pip3 install TA-Lib  # Python wrapper
pip3 install pandas_ta

# 5. Verify Installation
echo "🔍 Verifying environment..."
python3 -c "import shioaji; print(f'Shioaji version: {shioaji.__version__}')"
python3 -c "import talib; print('TA-Lib wrapper: OK')"
python3 -c "import pandas_ta; print('Pandas-TA: OK')"

echo "🎉 Setup Complete! Next steps:"
echo "1. Run 'cp .env.example .env' and fill in your keys."
echo "2. Run 'python3 main.py' or use tmux."
