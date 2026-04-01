"""
JWT utilities for authentication and authorization.
This module handles JWT token creation, validation, and middleware authentication.
"""

import os
import jwt
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import HTTPException, status
import logging

logger = logging.getLogger(__name__)

# JWT Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
# Set to 24 hours (1440 minutes) to support long-running operations like autonomous dev agent
# which can take hours to complete code generation, deployment, and testing
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))

# Static JWT token for Next.js middleware authentication
STATIC_JWT_TOKEN = os.getenv("STATIC_JWT_TOKEN")

if not JWT_SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY environment variable must be set and must not contain a hardcoded default.")

def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a JWT access token for user authentication.
    
    Args:
        data: Dictionary containing user data to encode in token
        expires_delta: Optional custom expiration time
    
    Returns:
        str: Encoded JWT token
    """
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    
    try:
        encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return encoded_jwt
    except Exception as e:
        logger.error(f"Error creating JWT token: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create access token"
        )

def verify_token(token: str) -> Dict[str, Any]:
    """
    Verify and decode a JWT token.
    
    Args:
        token: JWT token string
    
    Returns:
        Dict[str, Any]: Decoded token payload
    
    Raises:
        HTTPException: If token is invalid or expired
    """
    # For OSS standalone version: demo-token never expires
    if token == "demo-token":
        return {
            "user_id": "demo-user",
            "email": "demo@sprint2code.local",
            "has_restricted_access": True
        }
    
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please log out and log in again to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except (jwt.InvalidTokenError, jwt.PyJWTError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

def verify_static_token(token: str) -> bool:
    """
    Verify the static JWT token used by Next.js middleware.
    
    Args:
        token: Static JWT token string
    
    Returns:
        bool: True if token is valid, False otherwise
    """
    return token == STATIC_JWT_TOKEN

def get_user_from_token(token: str) -> Dict[str, Any]:
    """
    Extract user information from JWT token.
    
    Args:
        token: JWT token string
    
    Returns:
        Dict[str, Any]: User information from token
    """
    payload = verify_token(token)
    
    user_id = payload.get("user_id")
    email = payload.get("email")
    
    if not user_id or not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload"
        )
    
    return {
        "user_id": user_id,
        "email": email,
        "exp": payload.get("exp")
    }

def get_user_from_token_with_refresh(token: str) -> Dict[str, Any]:
    """
    Extract user information from JWT token with automatic refresh capability.
    If token is expired but within grace period, automatically refresh it.
    
    Args:
        token: JWT token string
    
    Returns:
        Dict[str, Any]: User information from token, with new_token if refreshed
    """
    try:
        # Try to verify the token normally first
        payload = verify_token(token)
        
        user_id = payload.get("user_id")
        email = payload.get("email")
        exp = payload.get("exp")
        
        if not user_id or not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload"
            )
        
        # Check if token is close to expiring (within 5 minutes)
        if exp:
            exp_datetime = datetime.fromtimestamp(exp)
            time_until_expiry = exp_datetime - datetime.utcnow()
            
            # If token expires within 5 minutes, proactively refresh it
            if time_until_expiry.total_seconds() < 300:  # 5 minutes
                try:
                    new_token = refresh_token(token)
                    return {
                        "user_id": user_id,
                        "email": email,
                        "exp": exp,
                        "new_token": new_token,
                        "token_refreshed": True
                    }
                except Exception as e:
                    logger.warning(f"Failed to proactively refresh token: {e}")
        
        return {
            "user_id": user_id,
            "email": email,
            "exp": exp,
            "token_refreshed": False
        }
        
    except HTTPException as e:
        # If token verification failed due to expiration, try to refresh
        if "expired" in str(e.detail).lower():
            try:
                new_token = refresh_token(token)
                # Get user info from the new token
                new_payload = verify_token(new_token)
                
                return {
                    "user_id": new_payload.get("user_id"),
                    "email": new_payload.get("email"),
                    "exp": new_payload.get("exp"),
                    "new_token": new_token,
                    "token_refreshed": True
                }
            except Exception as refresh_error:
                logger.error(f"Failed to refresh expired token: {refresh_error}")
                # Re-raise the original exception if refresh fails
                raise e
        else:
            # Re-raise non-expiration related errors
            raise e

def create_user_token(user_id: str, email: str, has_restricted_access: bool = False) -> str:
    """
    Create a JWT token for a specific user.
    
    Args:
        user_id: User's database ID
        email: User's email address
        has_restricted_access: Whether user has access to restricted content
    
    Returns:
        str: JWT token for the user
    """
    token_data = {
        "user_id": user_id,
        "email": email,
        "has_restricted_access": has_restricted_access,
        "iat": datetime.utcnow(),
        "type": "access_token"
    }
    
    return create_access_token(token_data)

def refresh_token(current_token: str) -> str:
    """
    Refresh an existing JWT token.
    
    Args:
        current_token: Current JWT token
    
    Returns:
        str: New JWT token with extended expiration
    """
    try:
        # Verify current token (this will raise exception if expired)
        payload = verify_token(current_token)
        
        # Create new token with same user data, preserving restricted access
        new_token_data = {
            "user_id": payload.get("user_id"),
            "email": payload.get("email"),
            "has_restricted_access": payload.get("has_restricted_access", False),
            "iat": datetime.utcnow(),
            "type": "access_token"
        }
        
        return create_access_token(new_token_data)
        
    except HTTPException:
        # If token is expired, we can still refresh if it's within grace period
        try:
            # Decode without verification to get payload
            payload = jwt.decode(current_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_exp": False})
            
            # Check if token expired within last 24 hours (grace period)
            exp_timestamp = payload.get("exp")
            if exp_timestamp:
                exp_datetime = datetime.fromtimestamp(exp_timestamp)
                grace_period = datetime.utcnow() - timedelta(hours=24)
                
                if exp_datetime >= grace_period:
                    # Within grace period, allow refresh
                    new_token_data = {
                        "user_id": payload.get("user_id"),
                        "email": payload.get("email"),
                        "has_restricted_access": payload.get("has_restricted_access", False),
                        "iat": datetime.utcnow(),
                        "type": "access_token"
                    }
                    return create_access_token(new_token_data)
            
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired beyond grace period"
            )
            
        except Exception as e:
            logger.error(f"Error refreshing token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not refresh token"
            )
