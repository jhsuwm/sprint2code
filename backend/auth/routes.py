"""
Authentication routes for the vacation planner API.
This module handles login, logout, token refresh, and OAuth authentication.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Header
from typing import Optional, Dict, Any
import uuid
import logging
from datetime import datetime

# Import logging functions from centralized log config
from log_config import info, error, warning, debug, critical

# Import enhanced logging functions
from utils.enhanced_logging import set_user_context

# Import database dependencies
from database.firestore_repository import UserRepository

# Import authentication utilities
from .jwt_utils import create_user_token, verify_static_token, refresh_token, get_user_from_token, get_user_from_token_with_refresh
from .password_utils import hash_password, verify_password, is_password_strong
from .oauth_utils import verify_oauth_token
from .schemas import (
    LoginRequest, LoginResponse, ErrorResponse, TokenRefreshRequest,
    TokenRefreshResponse, PasswordResetRequest, PasswordResetResponse,
    OAuthLoginRequest, UserInfo, TokenVerifyRequest, TokenVerifyResponse
)

logger = logging.getLogger(__name__)

# Create router
router = APIRouter()

# Import email check route
from .check_email_route import router as email_check_router
router.include_router(email_check_router)

# Import Google OAuth routes
from .google_oauth_routes import router as google_oauth_router
router.include_router(google_oauth_router, prefix="/google")

# Import Apple OAuth routes
from .apple_oauth_routes import router as apple_oauth_router
router.include_router(apple_oauth_router, prefix="/apple")

def verify_middleware_token(authorization: Optional[str] = Header(None)):
    """
    Verify the static JWT token from Next.js middleware.
    
    Args:
        authorization: Authorization header with Bearer token
    
    Raises:
        HTTPException: If token is invalid or missing
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing"
        )
    
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme"
            )
        
        if not verify_static_token(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid middleware token"
            )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format"
        )

@router.post("/login", response_model=LoginResponse, responses={401: {"model": ErrorResponse}})
async def login(
    request: LoginRequest,
    _: None = Depends(verify_middleware_token)
):
    """
    User login endpoint supporting both password and OAuth authentication.
    
    This endpoint handles:
    - Regular email/password authentication
    - OAuth authentication (Google/Apple)
    - Password reset functionality
    - User account creation for new users
    
    Args:
        request: Login request containing email, password, and OAuth info
        db: Database session
    
    Returns:
        LoginResponse: JWT token and user information
    
    Raises:
        HTTPException: If authentication fails
    """
    transaction_id = str(uuid.uuid4())
    
    try:
        info(f"Login attempt for {request.email} with redirect={request.redirect} (transaction: {transaction_id})", agent_module="AuthRoutes")
        
        # Initialize user repository
        user_repo = UserRepository()
        
        # Check if user exists
        existing_user = user_repo.get_user_by_email(request.email)
        
        # Handle OAuth authentication
        if request.oauth_provider and request.oauth_token:
            return await handle_oauth_login(request, existing_user, user_repo, transaction_id)
        
        # Handle password authentication
        if request.password:
            return await handle_password_login(request, existing_user, user_repo, transaction_id)
        
        # Neither password nor OAuth provided
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either password or OAuth credentials must be provided"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        error(f"Login error for {request.email}: {e} (transaction: {transaction_id})", agent_module="AuthRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during authentication"
        )

async def handle_oauth_login(
    request: LoginRequest,
    existing_user: Optional[Dict[str, Any]],
    user_repo: UserRepository,
    transaction_id: str
) -> LoginResponse:
    """
    Handle OAuth authentication (Google/Apple).
    
    Args:
        request: Login request with OAuth credentials
        existing_user: Existing user from database (if any)
        db: Database session
        transaction_id: Transaction ID for logging
    
    Returns:
        LoginResponse: JWT token and user information
    """
    try:
        # Verify OAuth token and get user info
        oauth_user_info = verify_oauth_token(request.oauth_provider, request.oauth_token)
        
        # Verify email matches
        if oauth_user_info["email"] != request.email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="OAuth email does not match provided email"
            )
        
        # Create user if doesn't exist
        if not existing_user:
            user_id = user_repo.create_user(
                email=request.email,
                password=None,  # No password for OAuth users
                transaction_id=transaction_id
            )
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create user account"
                )
            user = user_repo.get_by_id(user_id)
            info(f"Created new OAuth user: {request.email} (transaction: {transaction_id})", agent_module="AuthRoutes")
        else:
            user = existing_user
            info(f"OAuth login for existing user: {request.email} (transaction: {transaction_id})", agent_module="AuthRoutes")
        
        # Create JWT token with restricted access flag
        access_token = create_user_token(user['id'], user['email'], user.get('has_restricted_access', False))
        
        # Set enhanced logging context for this user
        set_user_context(user['id'], "unknown")
        
        return LoginResponse(
            success=True,
            access_token=access_token,
            token_type="bearer",
            expires_in=86400,  # 24 hours
            user=user
        )
        
    except HTTPException:
        raise
    except Exception as e:
        error(f"OAuth login error: {e} (transaction: {transaction_id})", agent_module="AuthRoutes")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OAuth authentication failed"
        )

async def handle_password_login(
    request: LoginRequest,
    existing_user: Optional[Dict[str, Any]],
    user_repo: UserRepository,
    transaction_id: str
) -> LoginResponse:
    """
    Handle password-based authentication.
    
    Args:
        request: Login request with password
        existing_user: Existing user from database (if any)
        db: Database session
        transaction_id: Transaction ID for logging
    
    Returns:
        LoginResponse: JWT token and user information
    """
    # Handle password reset
    if request.password_reset:
        return await handle_password_reset(request, existing_user, user_repo, transaction_id)
    
    # Check waitlist approval before proceeding with login
    from database.firestore_repository import WaitlistRepository
    import os
    
    waitlist_repo = WaitlistRepository()
    app_owner_email = os.getenv("APP_OWNER_EMAIL")
    is_app_owner = app_owner_email and request.email.lower() == app_owner_email.lower()
    
    if not is_app_owner:
        # Check if user is on waitlist and approved
        waitlist_entry = waitlist_repo.get_waitlist_by_email(request.email)
        
        if not waitlist_entry:
            warning(f"User not on waitlist: {request.email} (transaction: {transaction_id})", agent_module="AuthRoutes")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="not_on_waitlist"
            )
        
        if not waitlist_entry.get('approved', False):
            warning(f"User not approved on waitlist: {request.email} (transaction: {transaction_id})", agent_module="AuthRoutes")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="not_approved"
            )
    
    # Handle regular login
    if existing_user:
        # Verify password
        if not existing_user.get('password'):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This account uses OAuth authentication. Please login with your OAuth provider."
            )
        
        if not verify_password(request.password, existing_user['password']):
            warning(f"Invalid password for {request.email} (transaction: {transaction_id})", agent_module="AuthRoutes")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        
        user = existing_user
        info(f"Successful login for existing user: {request.email} (transaction: {transaction_id})", agent_module="AuthRoutes")
    else:
        # Create new user account
        # Validate password strength
        is_strong, error_message = is_password_strong(request.password)
        if not is_strong:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Password validation failed: {error_message}"
            )
        
        hashed_password = hash_password(request.password)
        user_id = user_repo.create_user(
            email=request.email,
            password=hashed_password,
            transaction_id=transaction_id
        )
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create user account"
            )
        user = user_repo.get_by_id(user_id)
        info(f"Created new user account: {request.email} (transaction: {transaction_id})", agent_module="AuthRoutes")
    
    # Check if user is trying to access Life Journey without restricted access
    if request.redirect and request.redirect == 'life-journey':
        if not user.get('has_restricted_access', False):
            warning(f"User {request.email} attempted to access Life Journey without restricted access (transaction: {transaction_id})", agent_module="AuthRoutes")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="restricted_access_required"
            )
    
    # Create JWT token with restricted access flag
    access_token = create_user_token(user['id'], user['email'], user.get('has_restricted_access', False))
    
    # Set enhanced logging context for this user
    set_user_context(user['id'], "unknown")
    
    return LoginResponse(
        success=True,
        access_token=access_token,
        token_type="bearer",
        expires_in=86400,  # 24 hours
        user=user
    )

async def handle_password_reset(
    request: LoginRequest,
    existing_user: Optional[Dict[str, Any]],
    user_repo: UserRepository,
    transaction_id: str
) -> LoginResponse:
    """
    Handle password reset functionality.
    
    Args:
        request: Login request with password reset flag
        existing_user: Existing user from database
        db: Database session
        transaction_id: Transaction ID for logging
    
    Returns:
        LoginResponse: JWT token and user information
    """
    if not existing_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User account not found"
        )
    
    # Validate new password strength
    is_strong, error_message = is_password_strong(request.password)
    if not is_strong:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password validation failed: {error_message}"
        )
    
    # Update password
    hashed_password = hash_password(request.password)
    success = user_repo.update_user_password(
        user_id=existing_user['id'],
        new_password=hashed_password,
        transaction_id=transaction_id
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update password"
        )
    
    # Get updated user
    updated_user = user_repo.get_by_id(existing_user['id'])
    
    info(f"Password reset successful for {request.email} (transaction: {transaction_id})", agent_module="AuthRoutes")
    
    # Create JWT token with restricted access flag
    access_token = create_user_token(updated_user['id'], updated_user['email'], updated_user.get('has_restricted_access', False))
    
    # Set enhanced logging context for this user
    set_user_context(updated_user['id'], "unknown")
    
    return LoginResponse(
        success=True,
        access_token=access_token,
        token_type="bearer",
        expires_in=86400,  # 24 hours
        user=updated_user
    )

@router.post("/refresh", response_model=TokenRefreshResponse, responses={401: {"model": ErrorResponse}})
async def refresh_access_token(
    request: TokenRefreshRequest,
    _: None = Depends(verify_middleware_token)
):
    """
    Refresh an existing JWT access token.
    
    Args:
        request: Token refresh request with current token
    
    Returns:
        TokenRefreshResponse: New JWT token
    """
    try:
        new_token = refresh_token(request.refresh_token)
        
        return TokenRefreshResponse(
            success=True,
            access_token=new_token,
            token_type="bearer",
            expires_in=86400  # 24 hours
        )
        
    except HTTPException:
        raise
    except Exception as e:
        error(f"Token refresh error: {e}", agent_module="AuthRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not refresh token"
        )

@router.get("/me", response_model=UserInfo, responses={401: {"model": ErrorResponse}})
async def get_current_user(
    authorization: str = Header(...)
):
    """
    Get current user information from JWT token.
    
    Args:
        authorization: Authorization header with Bearer token
        db: Database session
    
    Returns:
        UserInfo: Current user information
    """
    try:
        # Extract token from header
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme"
            )
        
        # Get user info from token
        user_info = get_user_from_token(token)
        
        # Get full user details from database
        user_repo = UserRepository()
        user = user_repo.get_user_by_email(user_info["email"])
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        return UserInfo(
            user_id=user['id'],
            email_address=user['email'],
            created_timestamp=user.get('created_at'),
            updated_timestamp=user.get('updated_at')
        )
        
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format"
        )
    except HTTPException:
        raise
    except Exception as e:
        error(f"Get current user error: {e}", agent_module="AuthRoutes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not get user information"
        )

@router.post("/logout")
async def logout():
    """
    User logout endpoint.
    Since we're using stateless JWT tokens, logout is handled client-side
    by removing the token from storage.
    
    Returns:
        dict: Logout success message
    """
    return {
        "success": True,
        "message": "Logged out successfully"
    }

@router.post("/verify", response_model=TokenVerifyResponse, responses={401: {"model": ErrorResponse}})
async def verify_token_endpoint(
    request: TokenVerifyRequest,
    _: None = Depends(verify_middleware_token)
):
    """
    Verify JWT token validity with automatic refresh capability.
    
    This endpoint is used by the Next.js middleware to verify user tokens
    for protected routes like /chat. It automatically refreshes tokens that
    are expired but within the grace period, or proactively refreshes tokens
    that are close to expiring.
    
    Args:
        request: Token verification request with JWT token
    
    Returns:
        TokenVerifyResponse: Token validity, user information, and new token if refreshed
    """
    try:
        info(f"Token verification request received for token: {request.token[:20]}...", agent_module="AuthRoutes")
        
        # Use the new function that handles automatic refresh
        user_info = get_user_from_token_with_refresh(request.token)
        
        info(f"Token verification successful, token_refreshed: {user_info.get('token_refreshed', False)}", agent_module="AuthRoutes")
        
        # Convert expiration timestamp to datetime
        expires_at = None
        if user_info.get("exp"):
            expires_at = datetime.fromtimestamp(user_info["exp"])
        
        return TokenVerifyResponse(
            valid=True,
            user_id=user_info["user_id"],
            email=user_info["email"],
            expires_at=expires_at,
            new_token=user_info.get("new_token"),
            token_refreshed=user_info.get("token_refreshed", False)
        )
        
    except Exception as e:
        error(f"Exception in token verification: {type(e).__name__}: {e}", agent_module="AuthRoutes")
        import traceback
        error(f"Traceback: {traceback.format_exc()}", agent_module="AuthRoutes")
        
        # If it's an HTTPException, check if it's auth-related
        if isinstance(e, HTTPException):
            if e.status_code == status.HTTP_401_UNAUTHORIZED:
                info("Returning invalid token response due to auth failure", agent_module="AuthRoutes")
                return TokenVerifyResponse(
                    valid=False,
                    user_id=None,
                    email=None,
                    expires_at=None,
                    new_token=None,
                    token_refreshed=False
                )
            else:
                raise
        else:
            # For any other exception, return invalid
            return TokenVerifyResponse(
                valid=False,
                user_id=None,
                email=None,
                expires_at=None,
                new_token=None,
                token_refreshed=False
            )

@router.post("/verify-oauth", response_model=TokenVerifyResponse, responses={401: {"model": ErrorResponse}})
async def verify_oauth_token_endpoint(
    request: TokenVerifyRequest,
    _: None = Depends(verify_middleware_token)
):
    """
    Verify OAuth JWT token validity with automatic refresh capability.
    
    This endpoint is specifically designed for OAuth tokens (Google, Apple) and
    handles automatic token refresh differently from local account tokens.
    
    Args:
        request: Token verification request with JWT token
    
    Returns:
        TokenVerifyResponse: Token validity, user information, and new token if refreshed
    """
    try:
        info(f"OAuth token verification request received for token: {request.token[:20]}...", agent_module="AuthRoutes")
        
        # Use the OAuth-specific token refresh logic
        user_info = get_user_from_token_with_refresh(request.token)
        
        info(f"OAuth token verification successful, token_refreshed: {user_info.get('token_refreshed', False)}", agent_module="AuthRoutes")
        
        # Convert expiration timestamp to datetime
        expires_at = None
        if user_info.get("exp"):
            expires_at = datetime.fromtimestamp(user_info["exp"])
        
        return TokenVerifyResponse(
            valid=True,
            user_id=user_info["user_id"],
            email=user_info["email"],
            expires_at=expires_at,
            new_token=user_info.get("new_token"),
            token_refreshed=user_info.get("token_refreshed", False)
        )
        
    except Exception as e:
        error(f"Exception in OAuth token verification: {type(e).__name__}: {e}", agent_module="AuthRoutes")
        import traceback
        error(f"Traceback: {traceback.format_exc()}", agent_module="AuthRoutes")
        
        # If it's an HTTPException, check if it's auth-related
        if isinstance(e, HTTPException):
            if e.status_code == status.HTTP_401_UNAUTHORIZED:
                info("Returning invalid OAuth token response due to auth failure", agent_module="AuthRoutes")
                return TokenVerifyResponse(
                    valid=False,
                    user_id=None,
                    email=None,
                    expires_at=None,
                    new_token=None,
                    token_refreshed=False
                )
            else:
                raise
        else:
            # For any other exception, return invalid
            return TokenVerifyResponse(
                valid=False,
                user_id=None,
                email=None,
                expires_at=None,
                new_token=None,
                token_refreshed=False
            )
