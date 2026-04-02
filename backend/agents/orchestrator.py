"""
Orchestrator Agent - Coordinates autonomous development agents
"""
import asyncio
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base_agent import BaseAgent
from log_config import info, debug, warning, error, critical
from .constants import MAX_CHAT_HISTORY_CONTEXT
from .models import (
    PlanningSession, ChatMessage, AgentResponse, ChatRequest, ChatResponse
)
from .user_intent_agent import UserIntentAgent

# Firestore not used in sprint2code
# from database.firestore_repository import ChatRepository


class OrchestratorAgent(BaseAgent):
    """
    Orchestrator Agent - Coordinates autonomous development agents
    """
    
    # Class-level shared session storage
    _shared_active_sessions: Dict[str, PlanningSession] = {}
    
    def __init__(self):
        super().__init__("OrchestratorAgent")
        
        # Initialize agents
        self.user_intent_agent = UserIntentAgent()
        
        # Use shared session storage
        self.active_sessions = OrchestratorAgent._shared_active_sessions
        
    async def process_chat_message(self, user_id: str, chat_request: ChatRequest) -> ChatResponse:
        """
        Main entry point for processing user chat messages
        """
        try:
            info(f"Orchestrator received session_id: {chat_request.session_id} for user {user_id}")
            
            # Get or create planning session
            session = self._get_or_create_session(user_id, chat_request.session_id)
            
            # Add user message to session chat history (in-memory only)
            user_message = ChatMessage(
                role="user",
                content=chat_request.message,
                timestamp=datetime.now()
            )
            session.chat_history.append(user_message)
            
            # Process through agent workflow
            agent_responses, assistant_message, clarifications = await self._execute_agent_workflow(
                session, chat_request.message, None
            )
            
            # Add assistant response to session history
            if assistant_message:
                assistant_msg = ChatMessage(
                    role="assistant",
                    content=assistant_message,
                    timestamp=datetime.now()
                )
                session.chat_history.append(assistant_msg)
            
            # Update session
            session.updated_at = datetime.now()
            self.active_sessions[session.session_id] = session
            
            info(f"Chat conversation processed for user {user_id}")
            
            return ChatResponse(
                message=assistant_message or "Processing your request...",
                session_id=session.session_id,
                agent_responses=agent_responses,
                plan_update=None,
                clarifications_needed=clarifications
            )
            
        except Exception as e:
            error(f"Error processing chat message: {e}")
            import traceback
            error(f"Full traceback: {traceback.format_exc()}", "Orchestrator")
            
            return ChatResponse(
                message="I encountered an error processing your request. Please try again.",
                session_id=chat_request.session_id or str(uuid.uuid4()),
                agent_responses=[],
                clarifications_needed=[]
            )
    
    def _get_or_create_session(self, user_id: str, session_id: Optional[str]) -> PlanningSession:
        """Get existing session or create new one"""
        
        if session_id and session_id in self.active_sessions:
            session = self.active_sessions[session_id]
            if session.user_id == user_id:
                self.logger.info(f"Found existing session {session_id} for user {user_id}")
                return session
        
        # Check if session already exists in active_sessions
        if session_id and session_id in self.active_sessions:
            existing_session = self.active_sessions[session_id]
            self.logger.info(f"Found session {session_id} in active_sessions")
            return existing_session
        
        # Create new session
        if session_id:
            self.logger.info(f"Creating session with provided session_id {session_id}")
            session = PlanningSession(
                session_id=session_id,
                user_id=user_id,
                status="active"
            )
        else:
            new_session_id = str(uuid.uuid4())
            self.logger.info(f"Creating new session with generated session_id {new_session_id}")
            session = PlanningSession(
                session_id=new_session_id,
                user_id=user_id,
                status="active"
            )
        
        return session
    
    async def _execute_agent_workflow(self, session: PlanningSession, user_message: str, user_chat_id: str = None) -> tuple:
        """
        Execute the agent workflow
        """
        agent_responses = []
        assistant_message = ""
        clarifications = []
        
        # Step 1: Parse user intent
        intent_response = await self._execute_user_intent_agent(session, user_message)
        agent_responses.append(intent_response)
        
        if not intent_response.success:
            return agent_responses, "I'd love to help you! Can you provide more details about what you'd like to do?", []
        
        # Extract data from intent response
        intent_data = intent_response.data or {}
        clarifications = intent_data.get("clarifications_needed", [])
        
        if clarifications:
            clarification_message = self._format_clarification_message(clarifications)
            return agent_responses, clarification_message, clarifications
        
        # Generate response
        assistant_message = await self._generate_response(agent_responses, session, user_message)
        
        return agent_responses, assistant_message, clarifications
    
    async def _execute_user_intent_agent(self, session: PlanningSession, user_message: str) -> AgentResponse:
        """Execute User Intent Agent"""
        
        input_data = {
            "user_message": user_message,
            "chat_history": [msg.dict() for msg in session.chat_history[-MAX_CHAT_HISTORY_CONTEXT:]],
            "existing_preferences": session.user_preferences
        }
        
        return await self.user_intent_agent.execute(input_data)
    
    def _format_clarification_message(self, clarifications: List[str]) -> str:
        """Format clarification questions"""
        
        if not clarifications:
            return "I'd love to help you! What would you like to do?"
        
        # Ask only ONE question at a time
        first_question = clarifications[0]
        return first_question.strip() if first_question.endswith('?') else f"{first_question}?"
    
    async def _generate_response(self, agent_responses: List[AgentResponse], session: PlanningSession, user_message: str) -> str:
        """Generate response based on agent outputs"""
        
        # Collect successful agent data
        successful_data = {}
        for response in agent_responses:
            if response.success and response.data:
                successful_data[response.agent_name] = response.data
        
        # Generate simple response
        if "UserIntentAgent" in successful_data:
            return "I understand your request. How can I help you further?"
        
        return "I'm processing your request. Please provide more details."
    
    async def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a planning session"""
        
        if session_id not in self.active_sessions:
            return None
        
        session = self.active_sessions[session_id]
        
        return {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "status": session.status,
            "chat_history_count": len(session.chat_history),
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat()
        }
    
    async def clear_session(self, session_id: str) -> bool:
        """Clear a planning session"""
        
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]
            return True
        
        return False
    
    def get_active_sessions_count(self) -> int:
        """Get count of active sessions"""
        return len(self.active_sessions)
    
    async def _process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Base agent process method"""
        return {"message": "Orchestrator agent should use process_chat_message method"}
