"""
New Session Routes - Handle clearing user session and context for new vacation planning
"""
import logging
import uuid
from fastapi import APIRouter, HTTPException, Depends, Header, status
from typing import Dict, Any, Optional
from .orchestrator import OrchestratorAgent
# Removed auth imports for OSS mode

# Import centralized logging functions
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import info, debug, warning, error, critical

logger = logging.getLogger(__name__)

router = APIRouter()

async def get_current_user_id(
    x_user_token: Optional[str] = Header(None)
) -> str:
    """
    For OSS mode: Return demo user ID
    """
    info("OSS Mode: Returning demo user ID", agent_module="NewSessionRoutes")
    return "demo-user"
        )

@router.post("/new-session")
async def create_new_session(
    user_id: str = Depends(get_current_user_id)
) -> Dict[str, Any]:
    """
    Create a new vacation planning session by generating a new session_id.
    This ensures the AI assistant starts fresh without access to previous conversations.
    
    IMPORTANT: Preserves origin location information (origin_location, origin_airport_code,
    origin_airport_name) from the previous session, as these typically don't change
    within the same login session.
    """
    try:
        if not user_id:
            raise HTTPException(status_code=400, detail="User ID not found")
        
        # Generate a new session ID
        new_session_id = str(uuid.uuid4())
        
        info(f"Creating new vacation session {new_session_id} for user {user_id}", agent_module="NewSessionRoutes")
        
        # Initialize orchestrator
        orchestrator = OrchestratorAgent()
        
        # Preserve origin location information from previous session
        preserved_origin_location = None
        preserved_origin_airport_code = None
        preserved_origin_airport_name = None
        
        # Find the most recent active session for this user to extract origin info
        for session_id, session in list(orchestrator.active_sessions.items()):
            if session.user_id == user_id:
                # Extract origin information if available
                if session.user_preferences:
                    if session.user_preferences.origin_location:
                        preserved_origin_location = session.user_preferences.origin_location
                        info(f"Preserving origin_location: {preserved_origin_location}", agent_module="NewSessionRoutes")
                    if session.user_preferences.origin_airport_code:
                        preserved_origin_airport_code = session.user_preferences.origin_airport_code
                        info(f"Preserving origin_airport_code: {preserved_origin_airport_code}", agent_module="NewSessionRoutes")
                    if session.user_preferences.origin_airport_name:
                        preserved_origin_airport_name = session.user_preferences.origin_airport_name
                        info(f"Preserving origin_airport_name: {preserved_origin_airport_name}", agent_module="NewSessionRoutes")
                    # Break after finding the first session with origin info
                    if preserved_origin_location:
                        break
        
        # Clear any active sessions for this user
        cleared_sessions = 0
        for session_id, session in list(orchestrator.active_sessions.items()):
            if session.user_id == user_id:
                del orchestrator.active_sessions[session_id]
                cleared_sessions += 1
                info(f"Cleared active session {session_id} for user {user_id}", agent_module="NewSessionRoutes")
        
        # Create and store the new session in active_sessions to prevent duplicate session creation
        from .models import PlanningSession, UserPreferences
        
        # Create new session with preserved origin information
        new_session = PlanningSession(
            session_id=new_session_id,
            user_id=user_id,
            status="active"
        )
        
        # If we have preserved origin information, create UserPreferences with only origin fields
        if preserved_origin_location or preserved_origin_airport_code or preserved_origin_airport_name:
            new_session.user_preferences = UserPreferences(
                origin_location=preserved_origin_location,
                origin_airport_code=preserved_origin_airport_code,
                origin_airport_name=preserved_origin_airport_name
            )
            info(f"Created new session with preserved origin information for user {user_id}", agent_module="NewSessionRoutes")
        
        orchestrator.active_sessions[new_session_id] = new_session
        
        info(f"Successfully created new session {new_session_id} for user {user_id}, cleared {cleared_sessions} active sessions", agent_module="NewSessionRoutes")
        
        return {
            "success": True,
            "message": "New vacation session created successfully",
            "session_id": new_session_id,
            "cleared_sessions": cleared_sessions,
            "user_id": user_id,
            "preserved_origin": preserved_origin_location is not None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        error(f"Error creating new session: {e}", agent_module="NewSessionRoutes")
        raise HTTPException(
            status_code=500,
            detail="Failed to create new vacation session"
        )