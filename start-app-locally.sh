#!/bin/bash

# Orion Dev Orchestrator - Local App Startup Script
# This script helps you start AI-generated apps locally for testing

set -e

echo "🚀 Orion Dev Orchestrator - Local App Startup"
echo "=============================================="
echo ""

# Check if repository path is provided
if [ -z "$1" ]; then
    echo "Usage: ./start-app-locally.sh <path-to-generated-repo>"
    echo ""
    echo "Example:"
    echo "  ./start-app-locally.sh ~/projects/my-generated-app"
    echo ""
    exit 1
fi

REPO_PATH="$1"

# Check if directory exists
if [ ! -d "$REPO_PATH" ]; then
    echo "❌ Error: Directory '$REPO_PATH' does not exist"
    exit 1
fi

cd "$REPO_PATH"
echo "📁 Working directory: $(pwd)"
echo ""

# Detect project structure
HAS_BACKEND=false
HAS_FRONTEND=false

if [ -d "backend" ] && [ -f "backend/requirements.txt" ]; then
    HAS_BACKEND=true
    echo "✅ Backend detected (Python/FastAPI)"
fi

if [ -d "frontend" ] && [ -f "frontend/package.json" ]; then
    HAS_FRONTEND=true
    echo "✅ Frontend detected (Next.js/React)"
fi

if [ "$HAS_BACKEND" = false ] && [ "$HAS_FRONTEND" = false ]; then
    echo "❌ No backend or frontend found in this directory"
    echo "   Make sure you're pointing to the generated app repository"
    exit 1
fi

echo ""
echo "🔧 Setting up and starting services..."
echo ""

# Start Backend
if [ "$HAS_BACKEND" = true ]; then
    echo "📦 Setting up Backend..."
    cd backend
    
    # Create virtual environment if it doesn't exist
    if [ ! -d "venv" ]; then
        echo "   Creating Python virtual environment..."
        python3 -m venv venv
    fi
    
    # Activate virtual environment and install dependencies
    echo "   Installing dependencies..."
    source venv/bin/activate
    pip install -q -r requirements.txt
    
    # Check for .env file
    if [ ! -f ".env" ]; then
        echo "   ⚠️  Warning: backend/.env file not found"
        echo "   Create one based on your configuration"
    fi
    
    # Start backend in background
    echo "   Starting backend on http://localhost:8000..."
    python main.py > ../backend.log 2>&1 &
    BACKEND_PID=$!
    echo "   Backend PID: $BACKEND_PID"
    
    cd ..
    sleep 3
fi

# Start Frontend
if [ "$HAS_FRONTEND" = true ]; then
    echo ""
    echo "📦 Setting up Frontend..."
    cd frontend
    
    # Install dependencies if node_modules doesn't exist
    if [ ! -d "node_modules" ]; then
        echo "   Installing npm dependencies..."
        npm install
    fi
    
    # Check for .env.local file
    if [ ! -f ".env.local" ]; then
        echo "   ⚠️  Warning: frontend/.env.local file not found"
        echo "   Create one with NEXT_PUBLIC_BACKEND_URL=http://localhost:8000"
    fi
    
    # Start frontend in background
    echo "   Starting frontend on http://localhost:3000..."
    npm run dev > ../frontend.log 2>&1 &
    FRONTEND_PID=$!
    echo "   Frontend PID: $FRONTEND_PID"
    
    cd ..
    sleep 3
fi

echo ""
echo "✅ Application started successfully!"
echo ""
echo "📊 Access your application:"
if [ "$HAS_FRONTEND" = true ]; then
    echo "   Frontend: http://localhost:3000"
fi
if [ "$HAS_BACKEND" = true ]; then
    echo "   Backend:  http://localhost:8000"
    echo "   API Docs: http://localhost:8000/docs"
fi
echo ""
echo "📝 Logs:"
if [ "$HAS_BACKEND" = true ]; then
    echo "   Backend:  tail -f $REPO_PATH/backend.log"
fi
if [ "$HAS_FRONTEND" = true ]; then
    echo "   Frontend: tail -f $REPO_PATH/frontend.log"
fi
echo ""
echo "🛑 To stop the application:"
echo "   Press Ctrl+C or run:"
if [ "$HAS_BACKEND" = true ]; then
    echo "   kill $BACKEND_PID  # Stop backend"
fi
if [ "$HAS_FRONTEND" = true ]; then
    echo "   kill $FRONTEND_PID  # Stop frontend"
fi
echo ""

# Save PIDs to file for easy cleanup
cat > .orion-pids << EOF
BACKEND_PID=$BACKEND_PID
FRONTEND_PID=$FRONTEND_PID
EOF

echo "💡 Tip: PIDs saved to .orion-pids for easy cleanup"
echo ""

# Wait for user interrupt
echo "Press Ctrl+C to stop all services..."
trap "echo ''; echo '🛑 Stopping services...'; [ -n '$BACKEND_PID' ] && kill $BACKEND_PID 2>/dev/null; [ -n '$FRONTEND_PID' ] && kill $FRONTEND_PID 2>/dev/null; echo '✅ All services stopped'; exit 0" INT

# Keep script running
wait
