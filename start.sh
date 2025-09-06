#!/bin/bash

# LoopTrader Web Interface Startup Script

echo "🚀 Starting LoopTrader Web Interface..."

# Check if Poetry is installed
if ! command -v poetry &> /dev/null; then
    echo "❌ Poetry is not installed. Please install Poetry first."
    exit 1
fi

# Install dependencies if not already installed
echo "📦 Installing dependencies..."
poetry install --no-root

# Set environment variables
export FLASK_DEBUG=True
export PORT=${PORT:-3000}

# Start the application
echo "🌟 Starting web server on port $PORT..."
echo "📱 Access the application at: http://localhost:$PORT"
echo "🔑 Demo login - Username: admin, Password: admin"
echo ""

poetry run python src/looptrader_web/app.py
