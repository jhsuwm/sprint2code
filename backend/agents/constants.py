"""
Constants for the agents module
"""

# Import shared constants with fallback for import issues
try:
    from ..constants import MAX_CHAT_HISTORY_CONTEXT
except ImportError:
    try:
        from api.constants import MAX_CHAT_HISTORY_CONTEXT
    except ImportError:
        # Final fallback - define the constant directly
        MAX_CHAT_HISTORY_CONTEXT = 50

# Re-export for backward compatibility
__all__ = ['MAX_CHAT_HISTORY_CONTEXT']