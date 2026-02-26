"""
Abstract base class for AI service providers (Gemini, Anthropic, etc.)
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class BaseAIService(ABC):
    """Abstract base class for AI service implementations."""
    
    def __init__(self):
        self.api_key = None
        self.client = None
        self.model_name = None
        self.model = None
    
    @abstractmethod
    async def generate_work_plan(self, story_description: str, subtasks: List[Dict[str, Any]] = None) -> str:
        """Generate a structured work plan based on story description."""
        pass
    
    @abstractmethod
    def parse_work_plan(self, work_plan: str) -> List[Dict[str, str]]:
        """Parse work plan to extract subtasks."""
        pass
    
    @abstractmethod
    async def generate_prd(self, prompt: str, attachments: List[Dict[str, Any]]) -> str:
        """Generate a PRD based on user prompt and attachments."""
        pass
    
    @abstractmethod
    async def generate_code(
        self,
        task_description: str,
        context: str = "",
        story_context: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        repo_files: Optional[List[str]] = None,
        temperature: float = 0.7,
        timeout: float = 300.0,
        max_output_tokens: int = 65536
    ) -> tuple:
        """
        Generate code for a specific task.
        Returns tuple of (code_text, finish_reason)
        """
        pass
    
    @abstractmethod
    def parse_generated_code(self, response: str) -> List[Dict[str, str]]:
        """Extract file paths and content from generated code response."""
        pass
