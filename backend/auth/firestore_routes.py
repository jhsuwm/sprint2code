"""
Firestore-based authentication routes for the vacation planner API.
This module handles login, logout, token refresh, and OAuth authentication using Firestore.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Header
from typing import Optional
import uuid
import logging
from datetime import datetime

# Import Firestore dependencies
from ..database.firestore_repository import UserRepository

# Import authentication utilities
from .jwt_utils import create_user_token, verify_static_token, refresh_token, get_user_from_token
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

# Initialize repository
user_repo = UserRepository()

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
    
    Returns:
        LoginResponse: JWT token and user information
    
    Raises:
        HTTPException: If authentication fails
    """
    transaction_id = str(uuid.uuid4())
    
    try:
        logger.info(f"Login attempt for {request.email} (transaction: {transaction_id})")
        
        # Check if user exists
        existing_user = user_repo.get_user_by_email(request.email)
        
        # Handle OAuth authentication
        if request.oauth_provider and request.oauth_token:
            return await handle_oauth_login(request, existing_user, transaction_id)
        
        # Handle password authentication
        if request.password:
            return await handle_password_login(request, existing_user, transaction_id)
        
        # Neither password nor OAuth provided
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either password or OAuth credentials must be provided"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error for {request.email}: {e} (transaction: {transaction_id})")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during authentication"
        )

async def handle_oauth_login(
    request: LoginRequest,
    existing_user: Optional[dict],
    transaction_id: str
) -> LoginResponse:
    """
    Handle OAuth authentication (Google/Apple).
    
    Args:
        request: Login request with OAuth credentials
        existing_user: Existing user from Firestore (if any)
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
            logger.info(f"Created new OAuth user: {request.email} (transaction: {transaction_id})")
        else:
            user = existing_user
            logger.info(f"OAuth login for existing user: {request.email} (transaction: {transaction_id})")
        
        # Create JWT token
        access_token = create_user_token(user['id'], user['email'])
        
        return LoginResponse(
            success=True,
            access_token=access_token,
            token_type="bearer",
            expires_in=1800,  # 30 minutes
            user={
                "user_id": user['id'],
                "email_address": user['email'],
                "created_timestamp": user.get('created_at'),
                "updated_timestamp": user.get('updated_at')
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth login error: {e} (transaction: {transaction_id})")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OAuth authentication failed"
        )

async def handle_password_login(
    request: LoginRequest,
    existing_user: Optional[dict],
    transaction_id: str
) -> LoginResponse:
    """
    Handle password-based authentication.
    
    Args:
        request: Login request with password
        existing_user: Existing user from Firestore (if any)
        transaction_id: Transaction ID for logging
    
    Returns:
        LoginResponse: JWT token and user information
    """
    # Handle password reset
    if request.password_reset:
        return await handle_password_reset(request, existing_user, transaction_id)
    
    # Handle regular login
    if existing_user:
        # Verify password
        if not existing_user.get('password'):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This account uses OAuth authentication. Please login with your OAuth provider."
            )
        
        if not verify_password(request.password, existing_user['password']):
            logger.warning(f"Invalid password for {request.email} (transaction: {transaction_id})")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        
        user = existing_user
        logger.info(f"Successful login for existing user: {request.email} (transaction: {transaction_id})")
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
        logger.info(f"Created new user account: {request.email} (transaction: {transaction_id})")
    
    # Create JWT token
    access_token = create_user_token(user['id'], user['email'])
    
    return LoginResponse(
        success=True,
        access_token=access_token,
        token_type="bearer",
        expires_in=1800,  # 30 minutes
        user={
            "user_id": user['id'],
            "email_address": user['email'],
            "created_timestamp": user.get('created_at'),
            "updated_timestamp": user.get('updated_at')
        }
    )

async def handle_password_reset(
    request: LoginRequest,
    existing_user: Optional[dict],
    transaction_id: str
) -> LoginResponse:
    """
    Handle password reset functionality.
    
    Args:
        request: Login request with password reset flag
        existing_user: Existing user from Firestore
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
    
    logger.info(f"Password reset successful for {request.email} (transaction: {transaction_id})")
    
    # Create JWT token
    access_token = create_user_token(updated_user['id'], updated_user['email'])
    
    return LoginResponse(
        success=True,
        access_token=access_token,
        token_type="bearer",
        expires_in=1800,  # 30 minutes
        user={
            "user_id": updated_user['id'],
            "email_address": updated_user['email'],
            "created_timestamp": updated_user.get('created_at'),
            "updated_timestamp": updated_user.get('updated_at')
        }
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
            expires_in=1800  # 30 minutes
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
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
        
        # Get full user details from Firestore
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
        logger.error(f"Get current user error: {e}")
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
    Verify JWT token validity.
    
    This endpoint is used by the Next.js middleware to verify user tokens
    for protected routes like /chat.
    
    Args:
        request: Token verification request with JWT token
    
    Returns:
        TokenVerifyResponse: Token validity and user information
    """
    try:
        # Verify the token and get user information
        user_info = get_user_from_token(request.token)
        
        # Convert expiration timestamp to datetime
        expires_at = None
        if user_info.get("exp"):
            expires_at = datetime.fromtimestamp(user_info["exp"])
        
        return TokenVerifyResponse(
            valid=True,
            user_id=user_info["user_id"],
            email=user_info["email"],
            expires_at=expires_at
        )
        
    except HTTPException as e:
        # Token is invalid or expired
        if e.status_code == status.HTTP_401_UNAUTHORIZED:
            return TokenVerifyResponse(
                valid=False,
                user_id=None,
                email=None,
                expires_at=None
            )
        raise
    except Exception as e:
        logger.error(f"Token verification error: {e}")
        return TokenVerifyResponse(
            valid=False,
            user_id=None,
            email=None,
            expires_at=None
        )