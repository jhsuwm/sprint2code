"""
Google OAuth routes for handling OAuth 2.0 authentication flow.
This module provides endpoints for initiating OAuth flow and handling callbacks.
"""

import os
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Query
from pydantic import BaseModel
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
import requests
from log_config import info, debug, error, warning, critical

# Create router
router = APIRouter()

# OAuth Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:3000/api/auth/google")

# OAuth scopes - using full URLs to match Google's response format
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile'
]

class GoogleCallbackRequest(BaseModel):
    """Request model for Google OAuth callback."""
    code: str
    state: Optional[str] = None

def create_oauth_flow():
    """Create and configure Google OAuth flow."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth credentials not configured"
        )
    
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI]
        }
    }
    
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    
    return flow

@router.get("/authorize")
async def google_authorize(
    email: Optional[str] = Query(None),
    redirect: Optional[str] = Query(None)
):
    """
    Initiate Google OAuth authorization flow.
    
    Args:
        email: Optional email hint for OAuth flow
        redirect: Optional redirect path (our-faith, vacation-planner, etc.)
    
    Returns:
        dict: Authorization URL for redirect
    """
    try:
        info(f"Initiating Google OAuth flow for email: {email}, redirect: {redirect}")
        
        flow = create_oauth_flow()
        
        # Build state with redirect information
        import json
        state_data = {}
        if redirect:
            state_data['redirect'] = redirect
        
        # Generate authorization URL
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            login_hint=email if email else None,
            state=json.dumps(state_data) if state_data else None
        )
        
        info(f"Generated authorization URL: {authorization_url}")
        
        return {
            "authorization_url": authorization_url,
            "state": state
        }
        
    except Exception as e:
        error(f"Google OAuth authorization error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate Google OAuth flow"
        )

@router.post("/callback")
async def google_callback(request: GoogleCallbackRequest):
    """
    Handle Google OAuth callback and exchange code for tokens.
    
    Args:
        request: Google callback request containing code and state
    
    Returns:
        dict: Access token and user information
    """
    try:
        info(f"Processing Google OAuth callback with code: {request.code[:20]}...")
        
        # Extract redirect from state if present
        import json
        redirect_path = None
        if request.state:
            try:
                state_data = json.loads(request.state)
                redirect_path = state_data.get('redirect')
                info(f"Extracted redirect from state: {redirect_path}")
            except (json.JSONDecodeError, AttributeError):
                warning(f"Failed to parse state: {request.state}")
        
        flow = create_oauth_flow()
        
        # Exchange authorization code for tokens
        flow.fetch_token(code=request.code)
        
        # Get user info from Google
        credentials = flow.credentials
        user_info_response = requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {credentials.token}'},
            timeout=10
        )
        
        if user_info_response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Failed to get user information from Google"
            )
        
        user_info = user_info_response.json()
        
        info(f"Successfully retrieved user info for: {user_info.get('email')}")
        
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
                warning(f"User not on waitlist: {user_info['email']}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="not_on_waitlist"
                )
            
            if not waitlist_entry.get('approved', False):
                warning(f"User not approved on waitlist: {user_info['email']}")
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
                transaction_id=f"google_oauth_{request.code[:10]}"
            )
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create user account"
                )
            user = user_repo.get_by_id(user_id)
            info(f"Created new Google OAuth user: {user_info['email']}")
        else:
            user = existing_user
            info(f"Google OAuth login for existing user: {user_info['email']}")
        
        # Create JWT token with OAuth metadata
        from .jwt_utils import create_access_token
        from datetime import timedelta
        
        token_data = {
            "user_id": user['id'],
            "email": user['email'],
            "auth_method": "google_oauth",  # Add OAuth method metadata
            "type": "access_token",
            "has_restricted_access": user.get('has_restricted_access', False)  # Include restricted access flag
        }
        
        # Use 24-hour expiration to support long-running operations like autonomous dev
        access_token = create_access_token(token_data, expires_delta=timedelta(hours=24))
        
        response_data = {
            "success": True,
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 86400,  # 24 hours
            "auth_method": "google_oauth",  # Include in response for frontend
            "user": user,
            "google_user_info": {
                "name": user_info.get('name'),
                "picture": user_info.get('picture'),
                "verified_email": user_info.get('verified_email', False)
            }
        }
        
        # Include redirect path if it was in the state
        if redirect_path:
            response_data['redirect'] = redirect_path
            info(f"Including redirect in response: {redirect_path}")
        
        return response_data
        
    except HTTPException:
        raise
    except Exception as e:
        error(f"Google OAuth callback error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process Google OAuth callback"
        )
