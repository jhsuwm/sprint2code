"""
Database package initialization for the vacation planner application.
This module provides easy access to Firestore database configuration and utilities.
"""

from .firestore_config import (
    check_firestore_connection,
    firestore_config
)

from .firestore_models import (
    UserModel as User,
    ChatConversationModel as ChatConversation,
    VacationPlanModel as VacationPlan,
    VacationEventModel as VacationEvent
)

from .firestore_repository import (
    UserRepository,
    ChatRepository,
    VacationPlanRepository,
    VacationEventRepository
)

__all__ = [
    # Firestore configuration
    "check_firestore_connection",
    "firestore_config",
    
    # Models
    "User",
    "ChatConversation",
    "VacationPlan",
    "VacationEvent",
    
    # Repository classes
    "UserRepository",
    "ChatRepository",
    "VacationPlanRepository",
    "VacationEventRepository"
]