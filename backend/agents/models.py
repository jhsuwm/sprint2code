"""
Data models for the Autonomous Development Orchestrator
"""
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class UserPreferences(BaseModel):
    """User preferences for development tasks"""
    # Minimal fields for future extension
    preferences: Optional[Dict[str, Any]] = None


class AgentResponse(BaseModel):
    """Standard response from an agent"""
    agent_name: str
    success: bool
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    error: Optional[str] = None
    execution_time: Optional[float] = None


class ChatMessage(BaseModel):
    """Chat message for user interaction"""
    role: str  # user, assistant, system
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)


class PlanningSession(BaseModel):
    """Planning session state"""
    session_id: str
    user_id: str
    user_preferences: Optional[UserPreferences] = None
    chat_history: List[ChatMessage] = Field(default_factory=list)
    active_agents: List[str] = Field(default_factory=list)
    status: str = "active"  # active, completed, cancelled
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ChatRequest(BaseModel):
    """Request model for chat endpoint"""
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    """Response model for chat endpoint"""
    message: str
    session_id: str
    agent_responses: List[AgentResponse] = Field(default_factory=list)
    plan_update: Optional[Dict[str, Any]] = None
    clarifications_needed: List[str] = Field(default_factory=list)
