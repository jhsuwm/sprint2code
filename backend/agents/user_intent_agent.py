"""
User Intent Agent - Parses user input for autonomous development tasks
"""
import json
import re
from typing import Any, Dict, List, Optional
from .base_agent import BaseAgent
from .constants import MAX_CHAT_HISTORY_CONTEXT
from .models import UserPreferences
from log_config import info, debug, warning, error, critical


class UserIntentAgent(BaseAgent):
    """Agent responsible for parsing user chat input and extracting development task preferences"""
    
    def __init__(self):
        super().__init__("UserIntentAgent")
        
    async def _process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse user input and extract structured preferences"""
        user_message = input_data.get("user_message", "")
        chat_history = input_data.get("chat_history", [])
        existing_preferences = input_data.get("existing_preferences")
        
        context = self._build_context(chat_history)
        
        # Extract preferences using simple parsing
        preferences = await self._extract_preferences(user_message, context, existing_preferences)
        
        # Identify any clarifications needed
        clarifications = await self._identify_clarifications(preferences, user_message, context)
        
        return {
            "preferences": preferences.dict(),
            "clarifications_needed": clarifications,
            "confidence_score": self._calculate_confidence(preferences),
            "extracted_entities": self._extract_entities(user_message)
        }
    
    def _build_context(self, chat_history: List[Dict]) -> str:
        """Build context from previous chat messages"""
        if not chat_history:
            return ""
        
        context_parts = ["Previous conversation:"]
        for msg in chat_history[-MAX_CHAT_HISTORY_CONTEXT:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            context_parts.append(f"{role}: {content}")
        
        return "\n".join(context_parts)
    
    async def _extract_preferences(self, user_message: str, context: str, existing_preferences: Optional[UserPreferences] = None) -> UserPreferences:
        """Extract structured preferences from user message"""
        
        system_prompt = """You are an autonomous development assistant that extracts user preferences from conversations.

Extract information and return as JSON:
{
    "preferences": {}
}

Return empty preferences object if no specific information provided."""

        full_prompt = f"""
CONVERSATION HISTORY:
{context}

CURRENT USER MESSAGE: {user_message}

Based on the conversation, extract relevant preferences.
"""
        
        try:
            response = await self._generate_with_gemini(full_prompt, system_prompt)
            response_text = response['text'] if isinstance(response, dict) else response
            
            # Extract JSON from response
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                extracted_data = json.loads(json_match.group())
                return UserPreferences(**extracted_data.get('preferences', {}))
            else:
                return UserPreferences()
                
        except Exception as e:
            error(f"Error in _extract_preferences: {e}", "UserIntentAgent")
            return UserPreferences()
    
    async def _identify_clarifications(self, preferences: UserPreferences, user_message: str, context: str = "") -> List[str]:
        """Identify what clarifications are needed from the user"""
        # For now, no clarifications needed - simplified for autonomous dev
        return []
    
    def _calculate_confidence(self, preferences: UserPreferences) -> float:
        """Calculate confidence score based on extracted information"""
        return 0.5  # Default confidence
    
    def _extract_entities(self, user_message: str) -> Dict[str, List[str]]:
        """Extract named entities from user message"""
        return {
            "keywords": [],
            "technical_terms": []
        }
