"""
Base Agent class for the Multi-Agent Vacation Planning System
"""
import asyncio
import logging
import time
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import google.genai as genai
from .models import AgentResponse
from utils.enhanced_logging import google_ai_metrics, GoogleAPIMetricsLogger

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv not available, continue without it
    pass


class AgentLogger:
    """Custom logger wrapper that automatically includes agent module name"""
    
    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.logger = logging.getLogger(f"agent.{agent_name}")
    
    def info(self, message: str):
        """Log info message with agent module name"""
        self.logger.info(message, extra={'agent_module': self.agent_name})
    
    def debug(self, message: str):
        """Log debug message with agent module name"""
        self.logger.debug(message, extra={'agent_module': self.agent_name})
    
    def warning(self, message: str):
        """Log warning message with agent module name"""
        self.logger.warning(message, extra={'agent_module': self.agent_name})
    
    def error(self, message: str):
        """Log error message with agent module name"""
        self.logger.error(message, extra={'agent_module': self.agent_name})
    
    def critical(self, message: str):
        """Log critical message with agent module name"""
        self.logger.critical(message, extra={'agent_module': self.agent_name})


class BaseAgent(ABC):
    """Base class for all vacation planning agents"""
    
    def __init__(self, name: str):
        self.name = name
        # Create a custom logger that includes agent name in all messages
        self.logger = AgentLogger(name)
        
        # Configure Gemini - defer initialization until needed
        self._api_key = None
        self._client = None
        self._model_name = None
        self._gemini_configured = False
        
        # Agent configuration - optimized for faster response times
        self.max_retries = int(os.getenv("MAX_AGENT_RETRIES", "2"))
        self.timeout = int(os.getenv("AGENT_TIMEOUT_SECONDS", "90"))
        
    async def execute(self, input_data: Dict[str, Any]) -> AgentResponse:
        """
        Execute the agent's main functionality with error handling and retries
        """
        start_time = time.time()
        
        for attempt in range(self.max_retries):
            try:
                self.logger.info(f"Executing {self.name} (attempt {attempt + 1})")
                
                # Validate input
                if not self._validate_input(input_data):
                    return AgentResponse(
                        agent_name=self.name,
                        success=False,
                        error="Invalid input data",
                        execution_time=time.time() - start_time
                    )
                
                # Execute with timeout
                result = await asyncio.wait_for(
                    self._process(input_data),
                    timeout=self.timeout
                )
                
                execution_time = time.time() - start_time
                self.logger.info(f"{self.name} completed successfully in {execution_time:.2f}s")
                
                return AgentResponse(
                    agent_name=self.name,
                    success=True,
                    data=result,
                    execution_time=execution_time
                )
                
            except asyncio.TimeoutError:
                self.logger.warning(f"{self.name} timed out (attempt {attempt + 1})")
                if attempt == self.max_retries - 1:
                    return AgentResponse(
                        agent_name=self.name,
                        success=False,
                        error="Agent execution timed out",
                        execution_time=time.time() - start_time
                    )
                    
            except Exception as e:
                self.logger.error(f"{self.name} failed: {str(e)} (attempt {attempt + 1})")
                if attempt == self.max_retries - 1:
                    return AgentResponse(
                        agent_name=self.name,
                        success=False,
                        error=str(e),
                        execution_time=time.time() - start_time
                    )
                
                # Wait before retry
                await asyncio.sleep(1 * (attempt + 1))
        
        return AgentResponse(
            agent_name=self.name,
            success=False,
            error="Max retries exceeded",
            execution_time=time.time() - start_time
        )
    
    @abstractmethod
    async def _process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process the input data and return results
        Must be implemented by each agent
        """
        pass
    
    def _validate_input(self, input_data: Dict[str, Any]) -> bool:
        """
        Validate input data
        Can be overridden by specific agents
        """
        return input_data is not None
    
    def _ensure_gemini_configured(self):
        """
        Ensure Gemini is configured and ready to use with new google.genai.Client
        Uses API key for authentication with Google AI API
        """
        if not self._gemini_configured:
            # Get API key from environment variable
            # For Cloud Run: Use an unrestricted API key (HTTP referrer restrictions don't work for backend)
            # The API key should be restricted to "Generative Language API" only for security
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
            
            if not api_key:
                error_msg = "GEMINI_API_KEY environment variable not set"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
            try:
                # Create new genai.Client instance WITH API key
                # This is required for Google AI API (generativelanguage.googleapis.com)
                self._client = genai.Client(api_key=api_key)
                
                self._model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp")
                self.logger.info(f"Attempting to configure Gemini model: {self._model_name}")
                self._gemini_configured = True
                self.logger.info(f"Gemini configured successfully for {self.name} with model {self._model_name}")
            except Exception as e:
                self.logger.error(f"Failed to configure Gemini for {self.name}: {e}")
                raise ValueError(f"Failed to configure Gemini API: {e}")

    @google_ai_metrics('gemini')
    async def _generate_with_gemini(self, prompt: str, context: Optional[str] = None) -> Dict[str, Any]:
        """
        Generate response using Gemini model with new google.genai.Client
        Returns dict with text and token usage information
        """
        try:
            # Ensure Gemini is configured before use
            self._ensure_gemini_configured()
            
            full_prompt = f"{context}\n\n{prompt}" if context else prompt
            self.logger.info(f"Generating with Gemini for {self.name}")
            
            # Use new genai.Client approach for proper token usage metadata
            # Run the synchronous call in an executor to maintain async compatibility
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._client.models.generate_content(
                    model=self._model_name,
                    contents=full_prompt,
                    config=genai.types.GenerateContentConfig(
                        temperature=float(os.getenv("GEMINI_TEMPERATURE", "0.7")),
                        max_output_tokens=int(os.getenv("GEMINI_MAX_TOKENS", "8192")),
                    )
                )
            )
            
            if not response or not response.text:
                raise ValueError("Empty response from Gemini")
            
            # Extract token usage information from response
            input_tokens = None
            output_tokens = None
            total_tokens = None
            
            # Debug: Log response attributes to understand structure
            self.logger.debug(f"Response type: {type(response)}")
            self.logger.debug(f"Response attributes: {dir(response)}")
            
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                self.logger.debug(f"Usage metadata found: {response.usage_metadata}")
                self.logger.debug(f"Usage metadata type: {type(response.usage_metadata)}")
                self.logger.debug(f"Usage metadata attributes: {dir(response.usage_metadata)}")
                
                input_tokens = getattr(response.usage_metadata, 'prompt_token_count', None)
                output_tokens = getattr(response.usage_metadata, 'candidates_token_count', None)
                total_tokens = getattr(response.usage_metadata, 'total_token_count', None)
                
                self.logger.debug(f"Extracted tokens - Input: {input_tokens}, Output: {output_tokens}, Total: {total_tokens}")
            else:
                self.logger.debug("No usage_metadata found in response")
            
            self.logger.info(f"Gemini generation successful for {self.name}")
            
            # Return dict with text and token information for metrics logging
            return {
                'text': response.text,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'total_tokens': total_tokens
            }
            
        except Exception as e:
            self.logger.error(f"Gemini generation failed for {self.name}: {str(e)}")
            import traceback
            self.logger.error(f"Full traceback: {traceback.format_exc()}")
            raise
    
    @google_ai_metrics('gemini')
    async def _search_with_gemini(self, query: str) -> Dict[str, Any]:
        """
        Perform web search using Gemini with real-time search capabilities
        Returns dict with text and token usage information
        """
        try:
            # Use Gemini's web search capabilities by explicitly requesting current information
            search_prompt = f"""
            Search the web for current, up-to-date information about: {query}
            
            Please provide specific, current information including:
            - Current prices and costs (with specific dollar amounts)
            - Recent market rates and pricing trends
            - Seasonal variations in pricing
            - Specific numerical data and cost ranges
            - Sources and dates of information when available
            
            Focus on providing concrete, actionable pricing data rather than general information.
            Include specific dollar amounts, price ranges, and current market conditions.
            """
            
            # Configure Gemini to use web search if available
            self._ensure_gemini_configured()
            
            # Use new genai.Client approach for proper token usage metadata
            # Run the synchronous call in an executor to maintain async compatibility
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._client.models.generate_content(
                    model=self._model_name,
                    contents=search_prompt,
                    config=genai.types.GenerateContentConfig(
                        temperature=0.3,  # Lower temperature for more factual responses
                        max_output_tokens=int(os.getenv("GEMINI_MAX_TOKENS", "8192")),
                    )
                )
            )
            
            if not response or not response.text:
                raise ValueError("Empty response from Gemini web search")
            
            # Extract token usage information from response
            input_tokens = None
            output_tokens = None
            total_tokens = None
            
            # Debug: Log response attributes to understand structure
            self.logger.debug(f"Search response type: {type(response)}")
            self.logger.debug(f"Search response attributes: {dir(response)}")
            
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                self.logger.debug(f"Search usage metadata found: {response.usage_metadata}")
                self.logger.debug(f"Search usage metadata type: {type(response.usage_metadata)}")
                self.logger.debug(f"Search usage metadata attributes: {dir(response.usage_metadata)}")
                
                input_tokens = getattr(response.usage_metadata, 'prompt_token_count', None)
                output_tokens = getattr(response.usage_metadata, 'candidates_token_count', None)
                total_tokens = getattr(response.usage_metadata, 'total_token_count', None)
                
                self.logger.debug(f"Search extracted tokens - Input: {input_tokens}, Output: {output_tokens}, Total: {total_tokens}")
            else:
                self.logger.debug("No usage_metadata found in search response")
            
            search_results = response.text
            self.logger.info(f"Web search completed for query: {query[:50]}...")
            self.logger.info(f"Web search results: {search_results[:500]}...")  # Log first 500 chars of results
            
            # Return dict with text and token information for metrics logging
            return {
                'text': search_results,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'total_tokens': total_tokens
            }
            
        except Exception as e:
            self.logger.error(f"Web search failed for query '{query}': {str(e)}")
            # Return a more informative error message instead of raising
            error_message = f"Unable to retrieve current pricing information for: {query}. Please check manually for the most up-to-date rates."
            return {
                'text': error_message,
                'input_tokens': None,
                'output_tokens': None,
                'total_tokens': None
            }
    
    def get_agent_info(self) -> Dict[str, Any]:
        """
        Get information about this agent
        """
        return {
            "name": self.name,
            "type": self.__class__.__name__,
            "max_retries": self.max_retries,
            "timeout": self.timeout
        }
