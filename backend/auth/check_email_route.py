"""
Email check route for Terms and Services acceptance flow.
This endpoint checks if a user email exists in the database.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Header
from typing import Optional
import logging

# Import database dependencies
from database.firestore_repository import UserRepository, TermsAcceptanceRepository

# Import authentication utilities
from .jwt_utils import verify_static_token
from .schemas import (
    EmailCheckRequest, EmailCheckResponse, ErrorResponse,
    TermsAcceptanceRequest, TermsAcceptanceResponse
)

logger = logging.getLogger(__name__)

# Create router
router = APIRouter()

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

@router.post("/check-email", response_model=EmailCheckResponse, responses={400: {"model": ErrorResponse}})
async def check_email_exists(
    request: EmailCheckRequest,
    _: None = Depends(verify_middleware_token)
):
    """
    Check if user email exists in database for Terms and Services acceptance flow.
    
    This endpoint is used by the frontend to determine if a user needs to accept
    Terms and Services before proceeding with login/registration.
    
    Args:
        request: Email check request containing email address
        authorization: Authorization header with middleware token
    
    Returns:
        EmailCheckResponse: Whether email exists and if Terms acceptance is required
    
    Raises:
        HTTPException: If email validation fails
    """
    try:
        logger.info(f"Email existence check for: {request.email}")
        
        # Initialize repositories
        user_repo = UserRepository()
        terms_repo = TermsAcceptanceRepository()
        
        # Check if user exists
        existing_user = user_repo.get_user_by_email(request.email)
        email_exists = existing_user is not None
        
        # Check if user has accepted current Terms and Services
        has_accepted_terms = terms_repo.has_accepted_terms(request.email, "1.0")
        
        # User needs to accept Terms if they don't exist OR haven't accepted current terms
        requires_terms_acceptance = not email_exists or not has_accepted_terms
        
        logger.info(f"Email check result - exists: {email_exists}, has_accepted_terms: {has_accepted_terms}, requires_terms: {requires_terms_acceptance}")
        
        return EmailCheckResponse(
            success=True,
            email_exists=email_exists,
            requires_terms_acceptance=requires_terms_acceptance
        )
        
    except Exception as e:
        logger.error(f"Email check error for {request.email}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during email check"
        )

@router.post("/terms-acceptance", response_model=TermsAcceptanceResponse, responses={400: {"model": ErrorResponse}})
async def record_terms_acceptance(
    request: TermsAcceptanceRequest,
    _: None = Depends(verify_middleware_token)
):
    """
    Record Terms and Services acceptance for legal compliance.
    
    This endpoint stores user's acceptance of Terms and Services with
    timestamp, IP address, and user agent for legal evidence.
    
    Args:
        request: Terms acceptance request with user details
        authorization: Authorization header with middleware token
    
    Returns:
        TermsAcceptanceResponse: Success status and acceptance ID
    
    Raises:
        HTTPException: If recording fails
    """
    try:
        logger.info(f"Recording Terms acceptance for: {request.email}")
        
        # Initialize terms acceptance repository
        terms_repo = TermsAcceptanceRepository()
        
        # Record the acceptance
        acceptance_id = terms_repo.record_terms_acceptance(
            email=request.email,
            accepted=request.accepted,
            terms_version=request.terms_version,
            ip_address=request.ip_address,
            user_agent=request.user_agent
        )
        
        if not acceptance_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to record Terms and Services acceptance"
            )
        
        logger.info(f"Terms acceptance recorded successfully for {request.email}: {acceptance_id}")
        
        return TermsAcceptanceResponse(
            success=True,
            message="Terms and Services acceptance recorded successfully",
            acceptance_id=acceptance_id
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Terms acceptance recording error for {request.email}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during Terms acceptance recording"
        )