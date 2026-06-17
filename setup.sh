#!/bin/bash

# Setup script for LFG Mint Bot
# This script installs system dependencies (like ffmpeg) that cannot be installed via pip

set -e  # Exit on error

echo "🚀 Setting up LFG Mint Bot..."

# Check if running on Linux
if [[ "$OSTYPE" != "linux-gnu"* ]]; then
    echo "⚠️  Warning: This script is designed for Linux. Please install ffmpeg manually for your OS."
    echo "   macOS: brew install ffmpeg"
    echo "   Windows: Download from https://ffmpeg.org/download.html"
    exit 1
fi

# Check if ffmpeg is already installed
if command -v ffmpeg &> /dev/null; then
    echo "✅ ffmpeg is already installed"
    ffmpeg -version | head -n 1
else
    echo "📦 Installing ffmpeg..."
    
    # Detect package manager and install ffmpeg
    if command -v apt-get &> /dev/null; then
        # Debian/Ubuntu
        sudo apt-get update
        sudo apt-get install -y ffmpeg
    elif command -v yum &> /dev/null; then
        # CentOS/RHEL
        sudo yum install -y ffmpeg
    elif command -v dnf &> /dev/null; then
        # Fedora
        sudo dnf install -y ffmpeg
    elif command -v pacman &> /dev/null; then
        # Arch Linux
        sudo pacman -S --noconfirm ffmpeg
    else
        echo "❌ Could not detect package manager. Please install ffmpeg manually:"
        echo "   https://ffmpeg.org/download.html"
        exit 1
    fi
    
    echo "✅ ffmpeg installed successfully"
    ffmpeg -version | head -n 1
fi

echo ""
echo "📦 Creating virtualenv (.venv) and installing dependencies..."
# The pre-push hooks (mypy, pytest) run from .venv/bin/python, so create and
# populate that venv explicitly here — otherwise the installed hook fails every
# push with FileNotFoundError for .venv/bin/python.
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

echo ""
echo "📦 Installing the blocking pre-push gate (ruff, mypy, gitleaks, pytest)..."
# Bypass in genuine emergencies with: git push --no-verify
.venv/bin/pre-commit install --hook-type pre-push

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "1. Create a .env file with your configuration"
echo "2. Set up your trait_layers directory"
echo "3. Run: python main.py"

