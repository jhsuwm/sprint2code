"""
Firestore Collections Schema for Enhanced Logging System
Defines the structure for google_api_calls and log_metrics collections
"""

from typing import Dict, Any, Optional
from datetime import datetime
from enum import Enum

class APIType(Enum):
    """Types of API calls we track"""
    GOOGLE_AI = "google_ai"
    GOOGLE_API = "google_api"

class GoogleAPICallSchema:
    """
    Schema for google_api_calls collection
    Stores metrics for all Google API calls including AI models
    """
    
    @staticmethod
    def create_document(
        user_id: str,
        session_id: str,
        api_type: APIType,
        api_name: str,
        success: bool,
        response_time_ms: Optional[float] = None,
        timestamp: Optional[datetime] = None,
        # Google AI specific fields
        model_name: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        # Other Google API specific fields
        function_name: Optional[str] = None,
        method: Optional[str] = None,
        request_size_bytes: Optional[int] = None,
        response_size_bytes: Optional[int] = None,
        # Common fields
        error_message: Optional[str] = None,
        source_location: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a document for the google_api_calls collection
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            api_type: Type of API (google_ai or google_api)
            api_name: Name of the API (e.g., 'gemini-2.5-flash', 'google_maps')
            success: Whether the call was successful
            response_time_ms: Response time in milliseconds
            timestamp: When the call was made (defaults to now)
            model_name: AI model name (for Google AI calls)
            input_tokens: Number of input tokens (for Google AI calls)
            output_tokens: Number of output tokens (for Google AI calls)
            endpoint: API endpoint (for other Google APIs)
            method: HTTP method (for other Google APIs)
            request_size_bytes: Request size in bytes
            response_size_bytes: Response size in bytes
            error_message: Error message if call failed
            metadata: Additional metadata
            
        Returns:
            Dictionary representing the document to store
        """
        doc = {
            # Core identification fields
            'user_id': user_id,
            'session_id': session_id,
            'timestamp': timestamp or datetime.utcnow(),
            
            # API call details
            'api_type': api_type.value,
            'api_name': api_name,
            'success': success,
            'response_time_ms': response_time_ms,
            'error_message': error_message,
            
            # Metadata
            'metadata': metadata or {},
            
            # Cost calculation fields (to be populated by cost calculation service)
            'estimated_cost_usd': None,
            'cost_calculated_at': None,
            'cost_calculation_version': None,
        }
        
        # Add Google AI specific fields
        if api_type == APIType.GOOGLE_AI:
            doc.update({
                'model_name': model_name,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'total_tokens': (input_tokens or 0) + (output_tokens or 0) if input_tokens and output_tokens else None
            })
        
        # Add other Google API specific fields
        elif api_type == APIType.GOOGLE_API:
            doc.update({
                'endpoint': endpoint,
                'method': method or 'GET',
                'request_size_bytes': request_size_bytes,
                'response_size_bytes': response_size_bytes
            })
        
        return doc

class LogMetricsSchema:
    """
    Schema for log_metrics collection
    Stores aggregated metrics and summaries for reporting
    """
    
    @staticmethod
    def create_user_daily_summary(
        user_id: str,
        date: str,  # YYYY-MM-DD format
        total_api_calls: int = 0,
        successful_calls: int = 0,
        failed_calls: int = 0,
        total_cost_usd: float = 0.0,
        google_ai_calls: int = 0,
        google_ai_tokens: int = 0,
        google_ai_cost_usd: float = 0.0,
        google_api_calls: int = 0,
        google_api_cost_usd: float = 0.0,
        avg_response_time_ms: Optional[float] = None,
        sessions: Optional[list] = None,
        api_breakdown: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a daily summary document for a user
        
        Args:
            user_id: User identifier
            date: Date in YYYY-MM-DD format
            total_api_calls: Total number of API calls
            successful_calls: Number of successful calls
            failed_calls: Number of failed calls
            total_cost_usd: Total estimated cost in USD
            google_ai_calls: Number of Google AI calls
            google_ai_tokens: Total tokens used in AI calls
            google_ai_cost_usd: Cost of Google AI calls
            google_api_calls: Number of other Google API calls
            google_api_cost_usd: Cost of other Google API calls
            avg_response_time_ms: Average response time
            sessions: List of session IDs
            api_breakdown: Breakdown by API type
            
        Returns:
            Dictionary representing the daily summary document
        """
        return {
            # Identification
            'user_id': user_id,
            'date': date,
            'summary_type': 'daily_user',
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow(),
            
            # Overall metrics
            'total_api_calls': total_api_calls,
            'successful_calls': successful_calls,
            'failed_calls': failed_calls,
            'success_rate': successful_calls / total_api_calls if total_api_calls > 0 else 0.0,
            'total_cost_usd': total_cost_usd,
            'avg_response_time_ms': avg_response_time_ms,
            
            # Google AI specific metrics
            'google_ai': {
                'calls': google_ai_calls,
                'tokens': google_ai_tokens,
                'cost_usd': google_ai_cost_usd,
                'avg_tokens_per_call': google_ai_tokens / google_ai_calls if google_ai_calls > 0 else 0
            },
            
            # Other Google API metrics
            'google_api': {
                'calls': google_api_calls,
                'cost_usd': google_api_cost_usd
            },
            
            # Session information
            'sessions': sessions or [],
            'unique_sessions': len(set(sessions)) if sessions else 0,
            
            # API breakdown
            'api_breakdown': api_breakdown or {},
            
            # Cost breakdown
            'cost_breakdown': {
                'google_ai_percentage': (google_ai_cost_usd / total_cost_usd * 100) if total_cost_usd > 0 else 0,
                'google_api_percentage': (google_api_cost_usd / total_cost_usd * 100) if total_cost_usd > 0 else 0
            }
        }
    
    @staticmethod
    def create_session_summary(
        user_id: str,
        session_id: str,
        start_time: datetime,
        end_time: Optional[datetime] = None,
        total_api_calls: int = 0,
        successful_calls: int = 0,
        failed_calls: int = 0,
        total_cost_usd: float = 0.0,
        google_ai_calls: int = 0,
        google_ai_tokens: int = 0,
        google_api_calls: int = 0,
        duration_minutes: Optional[float] = None,
        api_breakdown: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a session summary document
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            start_time: When the session started
            end_time: When the session ended
            total_api_calls: Total number of API calls in session
            successful_calls: Number of successful calls
            failed_calls: Number of failed calls
            total_cost_usd: Total estimated cost for session
            google_ai_calls: Number of Google AI calls
            google_ai_tokens: Total tokens used in session
            google_api_calls: Number of other Google API calls
            duration_minutes: Session duration in minutes
            api_breakdown: Breakdown by API type
            
        Returns:
            Dictionary representing the session summary document
        """
        return {
            # Identification
            'user_id': user_id,
            'session_id': session_id,
            'summary_type': 'session',
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow(),
            
            # Session timing
            'start_time': start_time,
            'end_time': end_time,
            'duration_minutes': duration_minutes,
            
            # API call metrics
            'total_api_calls': total_api_calls,
            'successful_calls': successful_calls,
            'failed_calls': failed_calls,
            'success_rate': successful_calls / total_api_calls if total_api_calls > 0 else 0.0,
            'total_cost_usd': total_cost_usd,
            
            # Google AI metrics
            'google_ai_calls': google_ai_calls,
            'google_ai_tokens': google_ai_tokens,
            
            # Other Google API metrics
            'google_api_calls': google_api_calls,
            
            # API breakdown
            'api_breakdown': api_breakdown or {},
            
            # Performance metrics
            'calls_per_minute': total_api_calls / duration_minutes if duration_minutes and duration_minutes > 0 else 0,
            'cost_per_minute': total_cost_usd / duration_minutes if duration_minutes and duration_minutes > 0 else 0
        }

# Collection names
COLLECTIONS = {
    'GOOGLE_API_CALLS': 'google_api_calls',
    'LOG_METRICS': 'log_metrics'
}

# Firestore indexes that should be created
REQUIRED_INDEXES = [
    {
        'collection': 'google_api_calls',
        'fields': [
            {'field': 'user_id', 'order': 'ASCENDING'},
            {'field': 'timestamp', 'order': 'DESCENDING'}
        ]
    },
    {
        'collection': 'google_api_calls',
        'fields': [
            {'field': 'user_id', 'order': 'ASCENDING'},
            {'field': 'api_type', 'order': 'ASCENDING'},
            {'field': 'timestamp', 'order': 'DESCENDING'}
        ]
    },
    {
        'collection': 'google_api_calls',
        'fields': [
            {'field': 'user_id', 'order': 'ASCENDING'},
            {'field': 'session_id', 'order': 'ASCENDING'},
            {'field': 'timestamp', 'order': 'DESCENDING'}
        ]
    },
    {
        'collection': 'google_api_calls',
        'fields': [
            {'field': 'api_name', 'order': 'ASCENDING'},
            {'field': 'timestamp', 'order': 'DESCENDING'}
        ]
    },
    {
        'collection': 'log_metrics',
        'fields': [
            {'field': 'user_id', 'order': 'ASCENDING'},
            {'field': 'date', 'order': 'DESCENDING'}
        ]
    },
    {
        'collection': 'log_metrics',
        'fields': [
            {'field': 'user_id', 'order': 'ASCENDING'},
            {'field': 'summary_type', 'order': 'ASCENDING'},
            {'field': 'created_at', 'order': 'DESCENDING'}
        ]
    }
]

def get_collection_name(collection_key: str) -> str:
    """
    Get collection name by key
    
    Args:
        collection_key: Key from COLLECTIONS dict
        
    Returns:
        Collection name
    """
    return COLLECTIONS.get(collection_key, collection_key)

def validate_google_api_call_document(doc: Dict[str, Any]) -> bool:
    """
    Validate a google_api_calls document structure
    
    Args:
        doc: Document to validate
        
    Returns:
        True if valid, False otherwise
    """
    required_fields = [
        'user_id', 'session_id', 'timestamp', 'api_type', 
        'api_name', 'success'
    ]
    
    # Check required fields
    for field in required_fields:
        if field not in doc:
            return False
    
    # Validate api_type
    if doc['api_type'] not in [APIType.GOOGLE_AI.value, APIType.GOOGLE_API.value]:
        return False
    
    # Validate Google AI specific fields
    if doc['api_type'] == APIType.GOOGLE_AI.value:
        if 'model_name' not in doc:
            return False
    
    # Validate other Google API specific fields
    elif doc['api_type'] == APIType.GOOGLE_API.value:
        if 'endpoint' not in doc:
            return False
    
    return True

def validate_log_metrics_document(doc: Dict[str, Any]) -> bool:
    """
    Validate a log_metrics document structure
    
    Args:
        doc: Document to validate
        
    Returns:
        True if valid, False otherwise
    """
    required_fields = [
        'user_id', 'summary_type', 'created_at', 'updated_at'
    ]
    
    # Check required fields
    for field in required_fields:
        if field not in doc:
            return False
    
    # Validate summary_type specific fields
    if doc['summary_type'] == 'daily_user':
        if 'date' not in doc:
            return False
    elif doc['summary_type'] == 'session':
        if 'session_id' not in doc or 'start_time' not in doc:
            return False
    
    return True