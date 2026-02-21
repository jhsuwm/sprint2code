"""
Main FastAPI application for the Orion Dev Orchestrator backend.
This serves as the entry point for the API and handles routing.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Import centralized logging configuration FIRST to apply timestamps to all loggers
import log_config
from log_config import error

# Import authentication routes - DISABLED (Firestore dependency from rooster)
# from auth.routes import router as auth_router

# Import agents routes
from agents.routes import router as agents_router

# Import Autonomous Dev routes
from routes.autonomous_dev_routes import router as autonomous_dev_router

# Create FastAPI application
app = FastAPI(
    title="Orion Dev Orchestrator API",
    description="Backend API for autonomous AI-powered development agent",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Configure CORS for Next.js frontend (Local development only)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # Next.js development server
        "http://localhost:3001",  # Alternative Next.js port
        "http://127.0.0.1:3000",  # Localhost alternative
        "http://127.0.0.1:3001",  # Localhost alternative
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Include authentication routes - DISABLED (Firestore dependency from rooster)
# app.include_router(auth_router, prefix="/auth", tags=["authentication"])

# Include agents routes
app.include_router(agents_router, prefix="/agents", tags=["agents"])

# Include Autonomous Dev routes
app.include_router(autonomous_dev_router, prefix="/autonomous-dev", tags=["autonomous-dev"])

@app.get("/")
async def root():
    """
    Root endpoint for health check.
    """
    return {
        "message": "Orion Dev Orchestrator API is running",
        "version": "1.0.0",
        "status": "healthy"
    }

@app.get("/health")
async def health_check():
    """
    Health check endpoint for monitoring.
    Returns healthy status for startup probe - simplified for reliability.
    """
    # Ultra-simple health check that should always work
    return {"status": "healthy", "service": "orion-dev-orchestrator"}

@app.get("/health/detailed")
async def detailed_health_check():
    """
    Detailed health check for monitoring.
    """
    from datetime import datetime
    import os
    
    # Basic health info
    response = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "service": "orion-dev-orchestrator",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "development"),
        "port": os.getenv("PORT", "8000")
    }
    
    return response

# For local development
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
