"""
Centralized logging configuration for the ROOSTER API
Provides consistent timestamp formatting across all backend components
ENHANCED: Now includes user_id and session_id support for cost reporting
"""

import logging
import sys
from datetime import datetime
from typing import Optional

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

class TimestampFormatter(logging.Formatter):
    """Custom formatter that adds ISO timestamp and agent module name to all log messages"""
    
    def format(self, record):
        # Add timestamp to the record
        timestamp = datetime.now().isoformat()
        
        # Get agent module name from record, default to "NULL" if not provided
        agent_module = getattr(record, 'agent_module', 'NULL')
        
        # Define format with timestamp, log level, agent module, and message
        if record.levelno >= logging.ERROR:
            # Error and critical messages get more detail
            format_str = f'[{timestamp}] [{record.levelname}] [{agent_module}] {record.name}:{record.lineno} - {record.getMessage()}'
        elif record.levelno >= logging.WARNING:
            # Warning messages get module name
            format_str = f'[{timestamp}] [{record.levelname}] [{agent_module}] {record.name} - {record.getMessage()}'
        else:
            # Info and debug messages get basic format
            format_str = f'[{timestamp}] [{record.levelname}] [{agent_module}] {record.getMessage()}'
        
        return format_str

def setup_logging(level: str = "INFO", log_file: Optional[str] = None):
    """
    Setup centralized logging configuration that automatically applies to all loggers
    ENHANCED: Now uses enhanced logging system with user context support
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path to write logs to (in addition to console)
    """
    
    # Use enhanced logging system instead of legacy system
    setup_enhanced_logging()
    
    # Set specific logger levels for noisy libraries (backward compatibility)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('google').setLevel(logging.WARNING)
    logging.getLogger('firebase_admin').setLevel(logging.WARNING)
    # Suppress httpx logging to prevent API keys from being logged
    logging.getLogger('httpx').setLevel(logging.WARNING)
    
    # If file logging is requested, add file handler to root logger
    if log_file:
        root_logger = logging.getLogger()
        file_handler = logging.FileHandler(log_file)
        
        # Use enhanced formatter for file handler too
        from utils.enhanced_logging import EnhancedFormatter
        enhanced_formatter = EnhancedFormatter(
            fmt='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(enhanced_formatter)
        file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        root_logger.addHandler(file_handler)
    
    return logging.getLogger()

# Initialize enhanced logging when module is imported
setup_logging()

# Create a centralized logger instance that the entire codebase can use
logger = logging.getLogger('rooster_api')

# Export enhanced logging functions for backward compatibility
def info(message: str, agent_module: str = None):
    """
    Log info message with timestamp, user context, and optional agent module
    ENHANCED: Now includes user_id and session_id in log output
    """
    if agent_module:
        # If agent_module is provided, use it in the logger name for better organization
        agent_logger = logging.getLogger(f'rooster_api.{agent_module}')
        agent_logger.info(message)
    else:
        enhanced_info(message)

def debug(message: str, agent_module: str = None):
    """
    Log debug message with timestamp, user context, and optional agent module
    ENHANCED: Now includes user_id and session_id in log output
    """
    if agent_module:
        agent_logger = logging.getLogger(f'rooster_api.{agent_module}')
        agent_logger.debug(message)
    else:
        enhanced_debug(message)

def warning(message: str, agent_module: str = None):
    """
    Log warning message with timestamp, user context, and optional agent module
    ENHANCED: Now includes user_id and session_id in log output
    """
    if agent_module:
        agent_logger = logging.getLogger(f'rooster_api.{agent_module}')
        agent_logger.warning(message)
    else:
        enhanced_warning(message)

def error(message: str, agent_module: str = None):
    """
    Log error message with timestamp, user context, and optional agent module
    ENHANCED: Now includes user_id and session_id in log output
    """
    if agent_module:
        agent_logger = logging.getLogger(f'rooster_api.{agent_module}')
        agent_logger.error(message)
    else:
        enhanced_error(message)

def critical(message: str, agent_module: str = None):
    """
    Log critical message with timestamp, user context, and optional agent module
    ENHANCED: Now includes user_id and session_id in log output
    """
    if agent_module:
        agent_logger = logging.getLogger(f'rooster_api.{agent_module}')
        agent_logger.critical(message)
    else:
        enhanced_critical(message)

# Re-export enhanced logging functions for new code
set_user_context = set_user_context
get_user_context = get_user_context
clear_user_context = clear_user_context
user_context = user_context
google_ai_metrics = google_ai_metrics
google_api_metrics = google_api_metrics