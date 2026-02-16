"""
Minimal Firestore data models for the Autonomous Development Orchestrator
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class UserModel(BaseModel):
    """User model for authentication and profile data"""
    id: str
    email: str
    display_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ChatConversationModel(BaseModel):
    """Chat conversation model"""
    id: str
    user_id: str
    session_id: str
    chat_origin: str  # 'user' or 'chatbot'
    chat_text: str
    transaction_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)


class VacationPlanModel(BaseModel):
    """Placeholder - not used in autonomous dev orchestrator"""
    id: str
    user_id: str
    created_at: datetime = Field(default_factory=datetime.now)


class VacationEventModel(BaseModel):
    """Placeholder - not used in autonomous dev orchestrator"""
    id: str
    user_id: str
    created_at: datetime = Field(default_factory=datetime.now)
