#!/bin/bash

# Sprint2Code - Main Startup Script
# This script starts the Sprint2Code dashboard and backend

set -e

echo "Cleaning up previous deployment files ..."
rm -fr ./backend/deployments/*
echo ""

echo "🚀 Starting Sprint2Code"
echo "=================================="
echo ""

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "📁 Working directory: $(pwd)"
echo ""

# Check if backend and frontend directories exist
if [ ! -d "backend" ] || [ ! -d "frontend" ]; then
    echo "❌ Error: backend or frontend directory not found"
    echo "   Make sure you're running this script from the sprint2code root directory"
    exit 1
fi

echo "🔧 Setting up services..."
echo ""

# Start Backend
echo "📦 Setting up Backend..."
cd backend

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "   Creating Python virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment and install dependencies
echo "   Installing backend dependencies..."
source venv/bin/activate
pip install -q -r requirements.txt 2>/dev/null || pip install -r requirements.txt

# Check for .env file
if [ ! -f ".env" ]; then
    echo "   ⚠️  Warning: backend/.env file not found"
    echo "   Please create one with required environment variables:"
    echo "      - GEMINI_API_KEY"
    echo "      - GITHUB_TOKEN"
    echo "      - JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN"
    echo "      - GOOGLE_CLOUD_PROJECT_ID"
    echo "      - JWT_SECRET_KEY"
    echo ""
    echo "   Backend may not function correctly without these variables."
    echo ""
fi

# Start backend in background
echo "   Starting backend server on http://localhost:8000..."
python main.py > ../backend.log 2>&1 &
BACKEND_PID=$!
echo "   Backend PID: $BACKEND_PID"

cd ..
sleep 3

# Start Frontend
echo ""
echo "📦 Setting up Frontend..."
cd frontend

# Install dependencies if node_modules doesn't exist
if [ ! -d "node_modules" ]; then
    echo "   Installing npm dependencies (this may take a few minutes)..."
    npm install
else
    echo "   Dependencies already installed"
fi

# Check for .env.local file
if [ ! -f ".env.local" ]; then
    echo "   Creating .env.local with default backend URL..."
    echo "NEXT_PUBLIC_BACKEND_URL=http://localhost:8000" > .env.local
fi

# Start frontend in background
echo "   Starting frontend dashboard on http://localhost:3000..."
npm run dev > ../frontend.log 2>&1 &
FRONTEND_PID=$!
echo "   Frontend PID: $FRONTEND_PID"

cd ..
sleep 3

echo ""
echo "✅ Sprint2Code started successfully!"
echo ""
echo "📊 Access the application:"
echo "   Dashboard:  http://localhost:3000"
echo "   Backend:    http://localhost:8000"
echo "   API Docs:   http://localhost:8000/docs"
echo ""
echo "📝 View logs:"
echo "   Backend:    tail -f $SCRIPT_DIR/backend.log"
echo "   Frontend:   tail -f $SCRIPT_DIR/frontend.log"
echo ""
echo "🛑 To stop all services:"
echo "   Press Ctrl+C or run:"
echo "   kill $BACKEND_PID $FRONTEND_PID"
echo ""

# Save PIDs to file for easy cleanup
cat > .sprint2code-pids << EOF
BACKEND_PID=$BACKEND_PID
FRONTEND_PID=$FRONTEND_PID
EOF

echo "💡 Tip: Process IDs saved to .sprint2code-pids"
echo ""
echo "🎯 Next steps:"
echo "   1. Configure your JIRA and GitHub credentials in backend/.env"
echo "   2. Add technical specification YAML configs via the dashboard"
echo "   3. Start creating apps with natural language!"
echo ""

# Wait for user interrupt
echo "Press Ctrl+C to stop all services..."
trap "echo ''; echo '🛑 Stopping services...'; kill $BACKEND_PID 2>/dev/null; kill $FRONTEND_PID 2>/dev/null; echo '✅ All services stopped'; exit 0" INT

# Keep script running
wait
