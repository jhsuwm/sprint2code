"""
Centralized logging configuration for the Sprint2Code API
Provides consistent timestamp formatting across all backend components
ENHANCED: Now includes user_id and session_id support for cost reporting
ENHANCED: Now automatically captures module and function names from caller
ENHANCED: Custom Logger class ensures all logging methods capture correct caller info
"""

import logging
import sys
import inspect
from datetime import datetime
from typing import Optional, Tuple

# Import enhanced logging system
from utils.enhanced_logging import (
    setup_enhanced_logging,
    set_user_context,
    get_user_context,
    clear_user_context,
    user_context,
    google_ai_metrics,
    google_api_metrics,
    info as enhanced_info,
    debug as enhanced_debug,
    warning as enhanced_warning,
    error as enhanced_error,
    critical as enhanced_critical
)

class CustomLogger(logging.Logger):
    """
    Custom logger that automatically captures the actual caller's module and function name.
    This ensures accurate caller info even when called through wrapper functions or indirection.
    """
    
    def _get_caller_module_function(self, depth=2):
        """
        Get the module name and function name from the actual caller.
        Skips frames from this logging system to find the real originating code.
        
        Args:
            depth: Number of additional frames to skip beyond standard skips
        
        Returns:
            Tuple of (module_name, function_name)
        """
        frame = inspect.currentframe()
        try:
            # Skip: _get_caller_module_function -> _log -> [wrapper or direct call] -> actual caller
            # So we need to skip frames until we find one not in our logging system
            skip_modules = {'logging', 'log_config', 'enhanced_logging'}
            skip_module_names = ['logging.', 'log_config', 'enhanced_logging']
            
            skipped = 0
            while frame:
                module_name = frame.f_globals.get('__name__', 'unknown')
                function_name = frame.f_code.co_name
                
                # Check if this frame is in our logging system
                is_logging_frame = (
                    module_name in skip_modules or
                    any(module_name.startswith(skip) for skip in skip_module_names) or
                    function_name in ('_log', '_log_internal', 'info', 'debug', 'warning', 'error', 'critical', '_get_caller_module_function')
                )
                
                if not is_logging_frame:
                    # Found the actual caller
                    # Remove 'backend.' prefix for cleaner logs
                    if module_name.startswith('backend.'):
                        module_name = module_name[8:]
                    return module_name, function_name
                
                frame = frame.f_back
                skipped += 1
                
                # Safety check to avoid infinite loops
                if skipped > 20:
                    break
            
            return 'unknown', 'unknown'
        finally:
            del frame
    
    def _log(self, level, msg, args, exc_info=None, extra=None, stack_info=None):
        """
        Override _log to capture caller module and function automatically.
        """
        if extra is None:
            extra = {}
        
        # Get the actual caller's module and function
        module_name, function_name = self._get_caller_module_function(depth=2)
        caller_info = f"{module_name}:{function_name}"
        
        # Add caller info to extra so it can be used in formatting
        extra['caller_module_func'] = caller_info
        
        # Call parent _log with the extra info
        super()._log(level, msg, args, exc_info=exc_info, extra=extra, stack_info=stack_info)


class EnhancedFormatter(logging.Formatter):
    """
    Enhanced formatter that adds ISO timestamp and caller module:function to log messages.
    Works seamlessly with CustomLogger to ensure complete logging information.
    """
    
    def format(self, record):
        # Add timestamp to the record
        timestamp = datetime.now().isoformat()
        
        # Get caller module:function from the custom logger
        caller_info = getattr(record, 'caller_module_func', 'unknown:unknown')
        
        # Format the message
        message = record.getMessage()
        
        # Define format with timestamp, log level, caller info, and message
        format_str = f'[{timestamp}] [{record.levelname}] [{caller_info}] {message}'
        return format_str

def setup_logging(level: str = "INFO", log_file: Optional[str] = None):
    """
    Setup centralized logging configuration that automatically applies to all loggers.
    Uses CustomLogger to ensure all logging calls capture correct caller module and function.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path to write logs to (in addition to console)
    """
    
    # Register CustomLogger as the logger class for the logging module
    logging.setLoggerClass(CustomLogger)
    
    # Get root logger
    root_logger = logging.getLogger()
    
    # Remove existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Create console handler with our enhanced formatter
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = EnhancedFormatter()
    console_handler.setFormatter(console_formatter)
    
    # Set logging level
    log_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(log_level)
    console_handler.setLevel(log_level)
    
    # Add handler to root logger
    root_logger.addHandler(console_handler)
    
    # Set specific logger levels for noisy libraries (backward compatibility)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('google').setLevel(logging.WARNING)
    logging.getLogger('firebase_admin').setLevel(logging.WARNING)
    # Suppress httpx logging to prevent API keys from being logged
    logging.getLogger('httpx').setLevel(logging.WARNING)
    
    # If file logging is requested, add file handler to root logger
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(console_formatter)
        file_handler.setLevel(log_level)
        root_logger.addHandler(file_handler)
    
    return root_logger

# Initialize logging with CustomLogger when module is imported
setup_logging()

# Create a centralized logger instance that the entire codebase can use
# This will be a CustomLogger instance that automatically captures caller info
logger = logging.getLogger('sprint2code_api')

# Wrapper functions
# The log prefix is now based on actual caller module:function from CustomLogger.
# No manual legacy module_tag argument is required.
def info(message: str):
    """
    Log info message with automatic module and function detection.
    """
    logger.info(message)


def debug(message: str):
    """
    Log debug message with automatic module and function detection.
    """
    logger.debug(message)


def warning(message: str):
    """
    Log warning message with automatic module and function detection.
    """
    logger.warning(message)


def error(message: str):
    """
    Log error message with automatic module and function detection.
    """
    logger.error(message)


def critical(message: str):
    """
    Log critical message with automatic module and function detection.
    """
    logger.critical(message)

# Re-export enhanced logging functions for user context management
set_user_context = set_user_context
get_user_context = get_user_context
clear_user_context = clear_user_context
user_context = user_context
google_ai_metrics = google_ai_metrics
google_api_metrics = google_api_metrics
