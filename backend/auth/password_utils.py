"""
Password hashing and verification utilities.
This module handles secure password hashing using bcrypt.
"""

from passlib.context import CryptContext
from passlib.hash import bcrypt
import logging

logger = logging.getLogger(__name__)

# Create password context with bcrypt
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    """
    Hash a plain text password using bcrypt.
    
    Args:
        password: Plain text password to hash
    
    Returns:
        str: Hashed password
    """
    try:
        return pwd_context.hash(password)
    except Exception as e:
        logger.error(f"Error hashing password: {e}")
        raise ValueError("Could not hash password")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain text password against a hashed password.
    
    Args:
        plain_password: Plain text password to verify
        hashed_password: Hashed password from database
    
    Returns:
        bool: True if password matches, False otherwise
    """
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception as e:
        logger.error(f"Error verifying password: {e}")
        return False

def is_password_strong(password: str) -> tuple[bool, str]:
    """
    Check if password meets security requirements.
    
    Args:
        password: Password to validate
    
    Returns:
        tuple[bool, str]: (is_valid, error_message)
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    
    if not any(c.islower() for c in password):
        return False, "Password must contain at least one lowercase letter"
    
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    
    special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?"
    if not any(c in special_chars for c in password):
        return False, "Password must contain at least one special character"
    
    return True, "Password is strong"

def generate_password_reset_token() -> str:
    """
    Generate a secure token for password reset.
    
    Returns:
        str: Random token for password reset
    """
    import secrets
    return secrets.token_urlsafe(32)

def hash_password_reset_token(token: str) -> str:
    """
    Hash a password reset token for secure storage.
    
    Args:
        token: Plain text reset token
    
    Returns:
        str: Hashed reset token
    """
    return hash_password(token)

def verify_password_reset_token(plain_token: str, hashed_token: str) -> bool:
    """
    Verify a password reset token.
    
    Args:
        plain_token: Plain text reset token
        hashed_token: Hashed reset token from database
    
    Returns:
        bool: True if token matches, False otherwise
    """
    return verify_password(plain_token, hashed_token)