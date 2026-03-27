"""
Apple OAuth routes for handling OAuth 2.0 authentication flow.
This module provides endpoints for initiating Apple OAuth flow and handling callbacks.
"""

import os
import logging
import jwt
import time
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Query
from pydantic import BaseModel
import requests

logger = logging.getLogger(__name__)

# Create router
router = APIRouter()

# Apple OAuth Configuration
APPLE_CLIENT_ID = os.getenv("APPLE_CLIENT_ID")  # Your Services ID
APPLE_TEAM_ID = os.getenv("APPLE_TEAM_ID")
APPLE_KEY_ID = os.getenv("APPLE_KEY_ID")
APPLE_PRIVATE_KEY = os.getenv("APPLE_PRIVATE_KEY")  # Your private key content
APPLE_REDIRECT_URI = os.getenv("APPLE_REDIRECT_URI", "http://localhost:3000/api/auth/apple")

class AppleCallbackRequest(BaseModel):
    """Request model for Apple OAuth callback."""
    code: str
    state: Optional[str] = None

def create_client_secret():
    """Create Apple client secret JWT."""
    if not all([APPLE_CLIENT_ID, APPLE_TEAM_ID, APPLE_KEY_ID, APPLE_PRIVATE_KEY]):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Apple OAuth credentials not configured"
        )
    
    # Create JWT header
    headers = {
        "kid": APPLE_KEY_ID,
        "alg": "ES256"
    }
    
    # Create JWT payload
    now = int(time.time())
    payload = {
        "iss": APPLE_TEAM_ID,
        "iat": now,
        "exp": now + 3600,  # 1 hour
        "aud": "https://appleid.apple.com",
        "sub": APPLE_CLIENT_ID
    }
    
    # Sign JWT with private key
    client_secret = jwt.encode(payload, APPLE_PRIVATE_KEY, algorithm="ES256", headers=headers)
    return client_secret

@router.get("/authorize")
async def apple_authorize(
    email: Optional[str] = Query(None),
    redirect: Optional[str] = Query(None)
):
    """
    Initiate Apple OAuth authorization flow.
    
    Args:
        email: Optional email hint for OAuth flow
        redirect: Optional redirect path (our-faith, vacation-planner, etc.)
    
    Returns:
        dict: Authorization URL for redirect
    """
    try:
        logger.info(f"Initiating Apple OAuth flow for email: {email}, redirect: {redirect}")
        
        if not APPLE_CLIENT_ID:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Apple OAuth not configured"
            )
        
        # Build state with redirect information
        import json
        state_data = {
            "timestamp": int(time.time())
        }
        if redirect:
            state_data['redirect'] = redirect
        
        # Apple OAuth parameters
        params = {
            "client_id": APPLE_CLIENT_ID,
            "redirect_uri": APPLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "name email",
            "response_mode": "form_post",  # Apple recommends form_post
            "state": json.dumps(state_data)
        }
        
        if email:
            params["login_hint"] = email
        
        # Build authorization URL
        base_url = "https://appleid.apple.com/auth/authorize"
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        authorization_url = f"{base_url}?{query_string}"
        
        logger.info(f"Generated Apple authorization URL: {authorization_url}")
        
        return {
            "authorization_url": authorization_url,
            "state": params["state"]
        }
        
    except Exception as e:
        logger.error(f"Apple OAuth authorization error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate Apple OAuth flow"
        )

@router.post("/callback")
async def apple_callback(request: AppleCallbackRequest):
    """
    Handle Apple OAuth callback and exchange code for tokens.
    
    Args:
        request: Apple callback request containing code and state
    
    Returns:
        dict: Access token and user information
    """
    try:
        logger.info(f"Processing Apple OAuth callback with code: {request.code[:20]}...")
        
        # Extract redirect from state if present
        import json
        redirect_path = None
        if request.state:
            try:
                state_data = json.loads(request.state)
                redirect_path = state_data.get('redirect')
                logger.info(f"Extracted redirect from state: {redirect_path}")
            except (json.JSONDecodeError, AttributeError):
                logger.warning(f"Failed to parse state: {request.state}")
        
        # Create client secret
        client_secret = create_client_secret()
        
        # Exchange authorization code for tokens
        token_data = {
            "client_id": APPLE_CLIENT_ID,
            "client_secret": client_secret,
            "code": request.code,
            "grant_type": "authorization_code",
            "redirect_uri": APPLE_REDIRECT_URI
        }
        
        token_response = requests.post(
            "https://appleid.apple.com/auth/token",
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10
        )
        
        if token_response.status_code != 200:
            logger.error(f"Apple token exchange failed: {token_response.text}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Failed to exchange Apple authorization code"
            )
        
        tokens = token_response.json()
        id_token = tokens.get("id_token")
        
        if not id_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No ID token received from Apple"
            )
        
        # Verify and decode the ID token
        from .oauth_utils import AppleOAuth
        apple_oauth = AppleOAuth()
        user_info = apple_oauth.get_user_info(id_token)
        
        logger.info(f"Successfully retrieved Apple user info for: {user_info.get('email')}")
        
        # Import authentication utilities
        from .jwt_utils import create_user_token
        from database.firestore_repository import UserRepository, WaitlistRepository
        
        # Initialize repositories
        user_repo = UserRepository()
        waitlist_repo = WaitlistRepository()
        
        # Check waitlist approval before proceeding
        app_owner_email = os.getenv("APP_OWNER_EMAIL")
        is_app_owner = app_owner_email and user_info['email'].lower() == app_owner_email.lower()
        
        if not is_app_owner:
            # Check if user is on waitlist and approved
            waitlist_entry = waitlist_repo.get_waitlist_by_email(user_info['email'])
            
            if not waitlist_entry:
                logger.warning(f"User not on waitlist: {user_info['email']}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="not_on_waitlist"
                )
            
            if not waitlist_entry.get('approved', False):
                logger.warning(f"User not approved on waitlist: {user_info['email']}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="not_approved"
                )
        
        # Check if user exists or create new user
        existing_user = user_repo.get_user_by_email(user_info['email'])
        
        if not existing_user:
            # Create new user
            user_id = user_repo.create_user(
                email=user_info['email'],
                password=None,  # No password for OAuth users
                transaction_id=f"apple_oauth_{request.code[:10]}"
            )
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create user account"
                )
            user = user_repo.get_by_id(user_id)
            logger.info(f"Created new Apple OAuth user: {user_info['email']}")
        else:
            user = existing_user
            logger.info(f"Apple OAuth login for existing user: {user_info['email']}")
        
        # Create JWT token
        access_token = create_user_token(user['id'], user['email'])
        
        response_data = {
            "success": True,
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 86400,  # 24 hours
            "auth_method": "apple_oauth",  # Include auth method
            "user": user,
            "apple_user_info": {
                "name": user_info.get('name'),
                "verified_email": user_info.get('verified_email', False),
                "apple_user_id": user_info.get('apple_user_id')
            }
        }
        
        # Include redirect path if it was in the state
        if redirect_path:
            response_data['redirect'] = redirect_path
            logger.info(f"Including redirect in response: {redirect_path}")
        
        return response_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Apple OAuth callback error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process Apple OAuth callback"
        )
