"""
FastAPI routes for the Multi-Agent Vacation Planning System
"""
from fastapi import APIRouter, HTTPException, Depends, status, Header, Query
from fastapi.security import HTTPBearer
from typing import Optional, List, Dict, Any

# Auth disabled - Firestore dependency from rooster
# from auth.jwt_utils import verify_token as verify_jwt_token
# from auth.routes import verify_middleware_token
from .orchestrator import OrchestratorAgent
from .models import ChatRequest, ChatResponse
from .constants import MAX_CHAT_HISTORY_CONTEXT
# Vacation-specific routes removed - stubbed out for autonomous dev orchestrator
# from .booking_routes import router as booking_router
# from .itinerary_routes import router as itinerary_router
# from .communication_routes import router as communication_router
# from .new_session_routes import router as new_session_router  # Auth dependency
# from .flight_search_routes import router as flight_search_router
# Import enhanced logging functions
# Firestore not used in orion-dev-orchestrator
try:
    from ..log_config import info, error
    from ..utils.enhanced_logging import set_user_context
except ImportError:
    from log_config import info, error
    from utils.enhanced_logging import set_user_context

# Create router
router = APIRouter()

# Security
security = HTTPBearer()


# Auth disabled - no authentication for orion-dev-orchestrator
async def get_current_user_id() -> str:
    """
    Return default user ID (no authentication)
    """
    return "default_user"


@router.post("/chat", response_model=ChatResponse)
async def process_chat_message(
    chat_request: ChatRequest,
    user_id: str = Depends(get_current_user_id)
):
    """
    Process a chat message through the multi-agent vacation planning system
    """
    try:
        info(f"Processing chat message for user {user_id}: {chat_request.message[:100]}...", "AgentRoutes")
        info(f"Chat request session_id: {chat_request.session_id}", "AgentRoutes")
        
        # Initialize orchestrator (now uses shared session storage)
        orchestrator = OrchestratorAgent()
        
        # Process through orchestrator
        response = await orchestrator.process_chat_message(user_id, chat_request)
        
        info(f"Chat processing completed for user {user_id}, session {response.session_id}", "AgentRoutes")
        
        # Log response structure for debugging
        if response.itinerary_data:
            info(f"Response includes itinerary data with status: {response.itinerary_data.status}", "AgentRoutes")
            info(f"Itinerary content length: {len(response.itinerary_data.markdown_content) if response.itinerary_data.markdown_content else 0}", "AgentRoutes")
        
        return response
        
    except HTTPException:
        # Re-raise HTTP exceptions (like auth failures) without modification
        raise
    except Exception as e:
        error(f"Error processing chat message for user {user_id}: {type(e).__name__}: {e}", "AgentRoutes")
        import traceback
        error(f"Full traceback: {traceback.format_exc()}", "AgentRoutes")
        
        # Provide more specific error messages based on error type
        if "JSON" in str(e) or "parse" in str(e).lower():
            error_detail = "Failed to process AI response format"
        elif "timeout" in str(e).lower() or "TimeoutError" in str(type(e).__name__):
            error_detail = "Request timeout - AI processing took too long"
        elif "database" in str(e).lower() or "firestore" in str(e).lower():
            error_detail = "Database connection error"
        elif "authentication" in str(e).lower() or "auth" in str(e).lower():
            error_detail = "Authentication error"
        else:
            error_detail = "Failed to process chat message"
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_detail
        )


@router.get("/session/{session_id}")
async def get_session_info(
    session_id: str,
    user_id: str = Depends(get_current_user_id)
):
    """
    Get information about a planning session
    """
    try:
        # Initialize orchestrator (now uses shared session storage)
        orchestrator = OrchestratorAgent()
        
        session_info = await orchestrator.get_session_info(session_id)
        
        if not session_info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )
        
        # Verify session belongs to user
        if session_info["user_id"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this session"
            )
        
        return session_info
        
    except HTTPException:
        raise
    except Exception as e:
        error(f"Error getting session info for {session_id}: {e}", "AgentRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get session information"
        )


@router.delete("/session/{session_id}")
async def clear_session(
    session_id: str,
    user_id: str = Depends(get_current_user_id)
):
    """
    Clear a planning session
    """
    try:
        # Initialize orchestrator (now uses shared session storage)
        orchestrator = OrchestratorAgent()
        
        # First verify session belongs to user
        session_info = await orchestrator.get_session_info(session_id)
        
        if not session_info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )
        
        if session_info["user_id"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this session"
            )
        
        # Clear the session
        success = await orchestrator.clear_session(session_id)
        
        return {
            "success": success,
            "message": "Session cleared successfully" if success else "Session not found"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        error(f"Error clearing session {session_id}: {e}", "AgentRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to clear session"
        )


@router.get("/health")
async def health_check():
    """
    Health check endpoint for the multi-agent system
    """
    try:
        # Initialize orchestrator (now uses shared session storage)
        orchestrator = OrchestratorAgent()
        
        active_sessions = orchestrator.get_active_sessions_count()
        
        # Test agent initialization
        agent_status = {
            "user_intent_agent": orchestrator.user_intent_agent.name
            # Vacation agents removed for autonomous dev orchestrator
        }
        
        return {
            "status": "healthy",
            "active_sessions": active_sessions,
            "agents": agent_status,
            "orchestrator": orchestrator.name
        }
        
    except Exception as e:
        error(f"Health check failed: {e}", "AgentRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Multi-agent system health check failed"
        )


@router.get("/agents/info")
async def get_agents_info(user_id: str = Depends(get_current_user_id)):
    """
    Get information about all available agents
    """
    try:
        # Initialize orchestrator (now uses shared session storage)
        orchestrator = OrchestratorAgent()
        
        agents_info = {
            "orchestrator": orchestrator.get_agent_info(),
            "user_intent_agent": orchestrator.user_intent_agent.get_agent_info()
            # Vacation agents removed for autonomous dev orchestrator
        }
        
        return {
            "agents": agents_info,
            "total_agents": len(agents_info),
            "system_status": "operational"
        }
        
    except Exception as e:
        error(f"Error getting agents info: {e}", "AgentRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get agents information"
        )


# chat-history endpoint removed - Firestore dependency from rooster


# Vacation-specific routes removed - stubbed out for autonomous dev orchestrator
# router.include_router(booking_router)
# router.include_router(itinerary_router)
# router.include_router(communication_router)
# router.include_router(new_session_router)  # Auth dependency
# router.include_router(flight_search_router)
