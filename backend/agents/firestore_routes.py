"""
Firestore-based FastAPI routes for the Multi-Agent Vacation Planning System
"""
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer
from typing import Optional

from ..auth.jwt_utils import verify_token as verify_jwt_token
from ..database.firestore_repository import ChatRepository, VacationPlanRepository
from .orchestrator import OrchestratorAgent
from .models import ChatRequest, ChatResponse
from ..log_config import info, warning, error

# Import global constant with fallback for import issues
try:
    from ..constants import MAX_CHAT_HISTORY_CONTEXT
except ImportError:
    try:
        from api.constants import MAX_CHAT_HISTORY_CONTEXT
    except ImportError:
        # Fallback - import from agents constants
        from .constants import MAX_CHAT_HISTORY_CONTEXT

# Create router
router = APIRouter()

# Security
security = HTTPBearer()

# Initialize orchestrator and repositories
orchestrator = OrchestratorAgent()
chat_repo = ChatRepository()
vacation_repo = VacationPlanRepository()

async def get_current_user_id(token: str = Depends(security)) -> str:
    """
    Extract user ID from JWT token (returns string for Firestore compatibility)
    """
    try:
        payload = verify_jwt_token(token.credentials)
        user_id = payload.get("user_id")
        
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: user_id not found"
            )
        
        # Convert to string for Firestore compatibility
        return str(user_id)
        
    except Exception as e:
        error(f"Token verification failed: {e}", "FirestoreRoutes")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )

@router.post("/chat", response_model=ChatResponse)
async def process_chat_message(
    chat_request: ChatRequest,
    user_id: str = Depends(get_current_user_id)
):
    """
    Process a chat message through the multi-agent vacation planning system
    """
    try:
        info(f"Processing chat message for user {user_id}: {chat_request.message[:100]}...", "FirestoreRoutes")
        
        # Store user message in Firestore
        user_message_id = chat_repo.add_chat_message(
            user_id=user_id,
            chat_origin="user",
            chat_text=chat_request.message
        )
        
        if not user_message_id:
            warning(f"Failed to store user message for user {user_id}", "FirestoreRoutes")
        
        # Process through orchestrator
        response = await orchestrator.process_chat_message(user_id, chat_request)
        
        # Store bot response in Firestore
        if response.response:
            bot_message_id = chat_repo.add_chat_message(
                user_id=user_id,
                chat_origin="chatbot",
                chat_text=response.response
            )
            
            if not bot_message_id:
                warning(f"Failed to store bot response for user {user_id}", "FirestoreRoutes")
        
        # If a vacation plan was created/updated, store it in Firestore
        if hasattr(response, 'vacation_plan') and response.vacation_plan:
            plan_data = response.vacation_plan
            
            # Check if plan already exists
            existing_plan = vacation_repo.get_user_vacation_plan_by_name(
                user_id=user_id,
                vacation_name=plan_data.get('name', 'Vacation Plan')
            )
            
            if not existing_plan:
                # Create new vacation plan
                plan_id = vacation_repo.create_vacation_plan(
                    user_id=user_id,
                    vacation_name=plan_data.get('name', 'Vacation Plan'),
                    vacation_start_date=plan_data.get('start_date'),
                    vacation_days=plan_data.get('duration_days')
                )
                
                if plan_id:
                    info(f"Created vacation plan {plan_id} for user {user_id}", "FirestoreRoutes")
                    # Add plan_id to response
                    response.vacation_plan_id = plan_id
        
        info(f"Chat processing completed for user {user_id}, session {response.session_id}", "FirestoreRoutes")
        
        return response
        
    except Exception as e:
        error(f"Error processing chat message for user {user_id}: {e}", "FirestoreRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process chat message"
        )

@router.get("/chat/history")
async def get_chat_history(
    limit: int = MAX_CHAT_HISTORY_CONTEXT,
    user_id: str = Depends(get_current_user_id)
):
    """
    Get chat history for the current user
    """
    try:
        chat_history = chat_repo.get_user_chat_history(
            user_id=user_id,
            limit=limit
        )
        
        return {
            "success": True,
            "chat_history": chat_history,
            "total_messages": len(chat_history)
        }
        
    except Exception as e:
        error(f"Error getting chat history for user {user_id}: {e}", "FirestoreRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get chat history"
        )

@router.get("/vacation-plans")
async def get_user_vacation_plans(
    confirmed_only: bool = False,
    user_id: str = Depends(get_current_user_id)
):
    """
    Get vacation plans for the current user
    """
    try:
        vacation_plans = vacation_repo.get_user_vacation_plans(
            user_id=user_id,
            confirmed_only=confirmed_only
        )
        
        return {
            "success": True,
            "vacation_plans": vacation_plans,
            "total_plans": len(vacation_plans)
        }
        
    except Exception as e:
        error(f"Error getting vacation plans for user {user_id}: {e}", "FirestoreRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get vacation plans"
        )

@router.post("/vacation-plans/{plan_id}/confirm")
async def confirm_vacation_plan(
    plan_id: str,
    user_id: str = Depends(get_current_user_id)
):
    """
    Confirm a vacation plan
    """
    try:
        # Verify plan belongs to user
        plan = vacation_repo.get_by_id(plan_id)
        if not plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Vacation plan not found"
            )
        
        if plan.get('user_id') != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this vacation plan"
            )
        
        # Confirm the plan
        success = vacation_repo.confirm_vacation_plan(plan_id)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to confirm vacation plan"
            )
        
        return {
            "success": True,
            "message": "Vacation plan confirmed successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        error(f"Error confirming vacation plan {plan_id}: {e}", "FirestoreRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to confirm vacation plan"
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
        session_info = await orchestrator.get_session_info(session_id)
        
        if not session_info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )
        
        # Verify session belongs to user (convert user_id to string for comparison)
        if str(session_info["user_id"]) != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this session"
            )
        
        return session_info
        
    except HTTPException:
        raise
    except Exception as e:
        error(f"Error getting session info for {session_id}: {e}", "FirestoreRoutes")
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
        # First verify session belongs to user
        session_info = await orchestrator.get_session_info(session_id)
        
        if not session_info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )
        
        if str(session_info["user_id"]) != user_id:
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
        error(f"Error clearing session {session_id}: {e}", "FirestoreRoutes")
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
        active_sessions = orchestrator.get_active_sessions_count()
        
        # Test agent initialization
        agent_status = {
            "user_intent_agent": orchestrator.user_intent_agent.name,
            "destination_research_agent": orchestrator.destination_research_agent.name,
            "map_routing_agent": orchestrator.map_routing_agent.name
        }
        
        # Test Firestore connectivity
        firestore_status = "healthy"
        try:
            from ..database.firestore_config import check_firestore_connection
            if not check_firestore_connection():
                firestore_status = "unhealthy"
        except Exception as e:
            error(f"Firestore health check failed: {e}", "FirestoreRoutes")
            firestore_status = "error"
        
        return {
            "status": "healthy" if firestore_status == "healthy" else "degraded",
            "active_sessions": active_sessions,
            "agents": agent_status,
            "orchestrator": orchestrator.name,
            "firestore_status": firestore_status
        }
        
    except Exception as e:
        error(f"Health check failed: {e}", "FirestoreRoutes")
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
        agents_info = {
            "orchestrator": orchestrator.get_agent_info(),
            "user_intent_agent": orchestrator.user_intent_agent.get_agent_info(),
            "destination_research_agent": orchestrator.destination_research_agent.get_agent_info(),
            "map_routing_agent": orchestrator.map_routing_agent.get_agent_info()
        }
        
        return {
            "agents": agents_info,
            "total_agents": len(agents_info),
            "system_status": "operational"
        }
        
    except Exception as e:
        error(f"Error getting agents info: {e}", "FirestoreRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get agents information"
        )

@router.get("/stats")
async def get_user_stats(user_id: str = Depends(get_current_user_id)):
    """
    Get user statistics (chat messages, vacation plans, etc.)
    """
    try:
        # Get chat message count
        chat_history = chat_repo.get_user_chat_history(user_id=user_id, limit=1000)
        total_messages = len(chat_history)
        user_messages = len([msg for msg in chat_history if msg.get('chat_origin') == 'user'])
        bot_messages = len([msg for msg in chat_history if msg.get('chat_origin') == 'chatbot'])
        
        # Get vacation plan count
        vacation_plans = vacation_repo.get_user_vacation_plans(user_id=user_id)
        total_plans = len(vacation_plans)
        confirmed_plans = len([plan for plan in vacation_plans if plan.get('is_confirmed')])
        
        return {
            "user_id": user_id,
            "chat_stats": {
                "total_messages": total_messages,
                "user_messages": user_messages,
                "bot_messages": bot_messages
            },
            "vacation_plan_stats": {
                "total_plans": total_plans,
                "confirmed_plans": confirmed_plans,
                "draft_plans": total_plans - confirmed_plans
            }
        }
        
    except Exception as e:
        error(f"Error getting user stats for {user_id}: {e}", "FirestoreRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user statistics"
        )