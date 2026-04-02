"""
Enhanced Logging System for Cost Reporting by User
Adds user_id and session_id fields to all log lines and implements Google API call metrics logging
"""

import os
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Union
from functools import wraps
from contextlib import contextmanager
import asyncio
import threading

# Thread-local storage for user context
_context = threading.local()

class EnhancedFormatter(logging.Formatter):
    """
    Enhanced formatter that adds user_id and session_id to all log records
    """
    
    def format(self, record):
        # Get user context from thread-local storage
        user_id = getattr(_context, 'user_id', None)
        session_id = getattr(_context, 'session_id', None)
        
        # Add user context to the record
        record.user_id = user_id or 'unknown'
        record.session_id = session_id or 'unknown'
        
        # Use the original format without injecting user context.
        # User sessions are not tracked in this local OSS mode, so avoid unknown tags.
        return super().format(record)

class GoogleAPIMetricsLogger:
    """
    Logger for Google API call metrics
    """
    
    def __init__(self):
        self.logger = logging.getLogger('google_api_metrics')
        
    async def log_google_ai_call(self,
                                api_name: str,
                                model_name: str,
                                input_tokens: Optional[int] = None,
                                output_tokens: Optional[int] = None,
                                response_time_ms: Optional[float] = None,
                                success: bool = True,
                                error_message: Optional[str] = None,
                                source_location: Optional[str] = None,
                                additional_metadata: Optional[Dict[str, Any]] = None):
        """
        Log Google AI (Gemini) API call metrics
        
        Args:
            api_name: Name of the API (e.g., 'gemini-2.5-flash', 'gemini-1.5-pro')
            model_name: Full model name used
            input_tokens: Number of input tokens (if available)
            output_tokens: Number of output tokens (if available)
            response_time_ms: Response time in milliseconds
            success: Whether the call was successful
            error_message: Error message if call failed
            source_location: Where in the codebase this call was triggered from
            additional_metadata: Any additional metadata to store
        """
        try:
            # Get user context from thread-local storage
            user_id = getattr(_context, 'user_id', None)
            session_id = getattr(_context, 'session_id', None)
            
            # Use 'unknown' only if context is truly not available
            user_id = user_id if user_id is not None else 'unknown'
            session_id = session_id if session_id is not None else 'unknown'
            
            # Create metrics record
            metrics = {
                'timestamp': datetime.utcnow().isoformat(),
                'user_id': user_id,
                'session_id': session_id,
                'api_type': 'google_ai',
                'api_name': api_name,
                'model_name': model_name,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'response_time_ms': response_time_ms,
                'success': success,
                'error_message': error_message,
                'source_location': source_location,
                'metadata': additional_metadata or {}
            }
            
            # Log to standard logging
            response_time_int = int(round(response_time_ms)) if response_time_ms is not None else 0
            code_info = source_location if source_location else "unknown"
            token_info = f"{input_tokens or 'null'}/{output_tokens or 'null'}"
            self.logger.info(f"Google AI_API Call: {api_name} - Code: {code_info} - Success: {success} - Tokens: {token_info} - Time: {response_time_int} ms")
                
        except Exception as e:
            self.logger.error(f"Error logging Google AI metrics: {e}")
    
    async def log_google_api_call(self,
                                 api_name: str,
                                 function_name: str,
                                 method: str = 'GET',
                                 response_time_ms: Optional[float] = None,
                                 success: bool = True,
                                 error_message: Optional[str] = None,
                                 request_size_bytes: Optional[int] = None,
                                 response_size_bytes: Optional[int] = None,
                                 source_location: Optional[str] = None,
                                 additional_metadata: Optional[Dict[str, Any]] = None):
        """
        Log other Google API call metrics (Maps, Firestore, Storage, etc.)
        
        Args:
            api_name: Name of the API (e.g., 'google_maps', 'firestore', 'cloud_storage')
            function_name: Name of the function that made the API call
            method: HTTP method used
            response_time_ms: Response time in milliseconds
            success: Whether the call was successful
            error_message: Error message if call failed
            request_size_bytes: Size of request in bytes
            response_size_bytes: Size of response in bytes
            source_location: Where in the codebase this call was triggered from
            additional_metadata: Any additional metadata to store
        """
        try:
            # Get user context from thread-local storage
            user_id = getattr(_context, 'user_id', None)
            session_id = getattr(_context, 'session_id', None)
            
            # Use 'unknown' only if context is truly not available
            user_id = user_id if user_id is not None else 'unknown'
            session_id = session_id if session_id is not None else 'unknown'
            
            # Create metrics record
            metrics = {
                'timestamp': datetime.utcnow().isoformat(),
                'user_id': user_id,
                'session_id': session_id,
                'api_type': 'google_api',
                'api_name': api_name,
                'function_name': function_name,
                'method': method,
                'response_time_ms': response_time_ms,
                'success': success,
                'error_message': error_message,
                'request_size_bytes': request_size_bytes,
                'response_size_bytes': response_size_bytes,
                'source_location': source_location,
                'metadata': additional_metadata or {}
            }
            
            # Log to standard logging
            response_time_int = int(round(response_time_ms)) if response_time_ms is not None else 0
            code_info = source_location if source_location else "unknown"
            # Non-AI Google APIs don't use tokens, so we include 0/0 for consistent log format
            self.logger.info(f"Google API Call: {api_name} - Code: {code_info} - Success: {success} - Tokens: 0/0 - Time: {response_time_int} ms")
                
        except Exception as e:
            self.logger.error(f"Error logging Google API metrics: {e}")

# Global metrics logger instance
_metrics_logger = GoogleAPIMetricsLogger()

def set_user_context(user_id: str, session_id: str):
    """
    Set user context for current thread
    
    Args:
        user_id: User identifier
        session_id: Session identifier
    """
    _context.user_id = user_id
    _context.session_id = session_id

def get_user_context() -> Dict[str, str]:
    """
    Get current user context
    
    Returns:
        Dictionary with user_id and session_id
    """
    return {
        'user_id': getattr(_context, 'user_id', 'unknown'),
        'session_id': getattr(_context, 'session_id', 'unknown')
    }

def clear_user_context():
    """
    Clear user context for current thread
    """
    if hasattr(_context, 'user_id'):
        delattr(_context, 'user_id')
    if hasattr(_context, 'session_id'):
        delattr(_context, 'session_id')

@contextmanager
def user_context(user_id: str, session_id: str):
    """
    Context manager for setting user context
    
    Args:
        user_id: User identifier
        session_id: Session identifier
    
    Usage:
        with user_context('user123', 'session456'):
            # All logging within this block will include user context
            logger.info("This log will include user_id and session_id")
    """
    # Store previous context
    prev_user_id = getattr(_context, 'user_id', None)
    prev_session_id = getattr(_context, 'session_id', None)
    
    try:
        # Set new context
        set_user_context(user_id, session_id)
        yield
    finally:
        # Restore previous context
        if prev_user_id is not None:
            _context.user_id = prev_user_id
        elif hasattr(_context, 'user_id'):
            delattr(_context, 'user_id')
            
        if prev_session_id is not None:
            _context.session_id = prev_session_id
        elif hasattr(_context, 'session_id'):
            delattr(_context, 'session_id')

def google_ai_metrics(api_name: str, model_name: str = None):
    """
    Decorator for Google AI API calls to automatically log metrics
    
    Args:
        api_name: Name of the API
        model_name: Model name (optional, can be determined from function args)
    
    Usage:
        @google_ai_metrics('gemini-2.5-flash')
        async def my_ai_function():
            # Function implementation
            pass
    """
    def decorator(func):
        # Capture source location information
        import inspect
        source_location = f"{func.__module__}.{func.__name__}"
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            success = True
            error_message = None
            input_tokens = None
            output_tokens = None
            
            try:
                result = await func(*args, **kwargs)
                
                # Try to extract token information from result if available
                if isinstance(result, dict):
                    input_tokens = result.get('input_tokens')
                    output_tokens = result.get('output_tokens')
                
                return result
            except Exception as e:
                success = False
                error_message = str(e)
                raise
            finally:
                response_time_ms = (time.time() - start_time) * 1000
                
                # Log metrics
                try:
                    await _metrics_logger.log_google_ai_call(
                        api_name=api_name,
                        model_name=model_name or api_name,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        response_time_ms=response_time_ms,
                        success=success,
                        error_message=error_message,
                        source_location=source_location
                    )
                except Exception as metrics_error:
                    # Don't let metrics logging errors affect the main function
                    logging.getLogger('enhanced_logging').error(f"Error logging AI metrics: {metrics_error}")
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time()
            success = True
            error_message = None
            input_tokens = None
            output_tokens = None
            
            try:
                result = func(*args, **kwargs)
                
                # Try to extract token information from result if available
                if isinstance(result, dict):
                    input_tokens = result.get('input_tokens')
                    output_tokens = result.get('output_tokens')
                
                return result
            except Exception as e:
                success = False
                error_message = str(e)
                raise
            finally:
                response_time_ms = (time.time() - start_time) * 1000
                
                # Log metrics asynchronously
                try:
                    # Try to get existing event loop, create one if needed
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            # If loop is running, create a task
                            loop.create_task(_metrics_logger.log_google_ai_call(
                                api_name=api_name,
                                model_name=model_name or api_name,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                response_time_ms=response_time_ms,
                                success=success,
                                error_message=error_message,
                                source_location=source_location
                            ))
                        else:
                            # If loop is not running, run the coroutine directly
                            loop.run_until_complete(_metrics_logger.log_google_ai_call(
                                api_name=api_name,
                                model_name=model_name or api_name,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                response_time_ms=response_time_ms,
                                success=success,
                                error_message=error_message,
                                source_location=source_location
                            ))
                    except RuntimeError:
                        # No event loop exists, create a new one
                        asyncio.run(_metrics_logger.log_google_ai_call(
                            api_name=api_name,
                            model_name=model_name or api_name,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            response_time_ms=response_time_ms,
                            success=success,
                            error_message=error_message,
                            source_location=source_location
                        ))
                except Exception as metrics_error:
                    # Don't let metrics logging errors affect the main function
                    logging.getLogger('enhanced_logging').error(f"Error logging AI metrics: {metrics_error}")
        
        # Return appropriate wrapper based on whether function is async
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator

def google_api_metrics(api_name: str, endpoint: str = None):
    """
    Decorator for other Google API calls to automatically log metrics
    
    Args:
        api_name: Name of the API (e.g., 'google_maps', 'firestore')
        endpoint: API endpoint (optional, will use function name if not provided)
    
    Usage:
        @google_api_metrics('google_maps')
        async def geocode_location():
            # Function implementation
            pass
    """
    def decorator(func):
        # Capture source location information
        import inspect
        source_location = f"{func.__module__}.{func.__name__}"
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            success = True
            error_message = None
            
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                success = False
                error_message = str(e)
                raise
            finally:
                response_time_ms = (time.time() - start_time) * 1000
                
                # Log metrics
                try:
                    await _metrics_logger.log_google_api_call(
                        api_name=api_name,
                        function_name=endpoint or func.__name__,
                        response_time_ms=response_time_ms,
                        success=success,
                        error_message=error_message,
                        source_location=source_location
                    )
                except Exception as metrics_error:
                    # Don't let metrics logging errors affect the main function
                    logging.getLogger('enhanced_logging').error(f"Error logging API metrics: {metrics_error}")
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time()
            success = True
            error_message = None
            
            try:
                result = func(*args, **kwargs)
                return result
            except Exception as e:
                success = False
                error_message = str(e)
                raise
            finally:
                response_time_ms = (time.time() - start_time) * 1000
                
                # Log metrics asynchronously
                try:
                    # Try to get existing event loop, create one if needed
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            # If loop is running, create a task
                            loop.create_task(_metrics_logger.log_google_api_call(
                                api_name=api_name,
                                function_name=endpoint or func.__name__,
                                response_time_ms=response_time_ms,
                                success=success,
                                error_message=error_message,
                                source_location=source_location
                            ))
                        else:
                            # If loop is not running, run the coroutine directly
                            loop.run_until_complete(_metrics_logger.log_google_api_call(
                                api_name=api_name,
                                function_name=endpoint or func.__name__,
                                response_time_ms=response_time_ms,
                                success=success,
                                error_message=error_message,
                                source_location=source_location
                            ))
                    except RuntimeError:
                        # No event loop exists, create a new one
                        asyncio.run(_metrics_logger.log_google_api_call(
                            api_name=api_name,
                            function_name=endpoint or func.__name__,
                            response_time_ms=response_time_ms,
                            success=success,
                            error_message=error_message,
                            source_location=source_location
                        ))
                except Exception as metrics_error:
                    # Don't let metrics logging errors affect the main function
                    logging.getLogger('enhanced_logging').error(f"Error logging API metrics: {metrics_error}")
        
        # Return appropriate wrapper based on whether function is async
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator

# Enhanced logging functions that maintain compatibility with existing code
def setup_enhanced_logging():
    """
    Set up enhanced logging with user context support
    """
    # Get root logger
    root_logger = logging.getLogger()
    
    # Remove existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Create console handler with enhanced formatter
    console_handler = logging.StreamHandler()
    enhanced_formatter = EnhancedFormatter(
        fmt='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(enhanced_formatter)
    
    # Set logging level
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))
    console_handler.setLevel(getattr(logging, log_level, logging.INFO))
    
    # Add handler to root logger
    root_logger.addHandler(console_handler)
    
    # Ensure metrics logger is properly configured
    metrics_logger = logging.getLogger('google_api_metrics')
    metrics_logger.setLevel(logging.INFO)
    
    # Suppress httpx logging to prevent API keys from being logged in URLs
    logging.getLogger('httpx').setLevel(logging.WARNING)

# Convenience functions for backward compatibility
def info(message: str, *args, **kwargs):
    """Enhanced info logging function"""
    logging.getLogger().info(message, *args, **kwargs)

def debug(message: str, *args, **kwargs):
    """Enhanced debug logging function"""
    logging.getLogger().debug(message, *args, **kwargs)

def warning(message: str, *args, **kwargs):
    """Enhanced warning logging function"""
    logging.getLogger().warning(message, *args, **kwargs)

def error(message: str, *args, **kwargs):
    """Enhanced error logging function"""
    logging.getLogger().error(message, *args, **kwargs)

def critical(message: str, *args, **kwargs):
    """Enhanced critical logging function"""
    logging.getLogger().critical(message, *args, **kwargs)

# Initialize enhanced logging on module import
setup_enhanced_logging()
