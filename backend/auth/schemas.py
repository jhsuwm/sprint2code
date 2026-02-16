"""
Pydantic schemas for authentication API requests and responses.
These models define the structure and validation for API payloads.
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime

class LoginRequest(BaseModel):
    """
    Login request schema for user authentication.
    """
    email: EmailStr = Field(..., description="User's email address")
    password: Optional[str] = Field(None, description="User's password (null for OAuth)")
    oauth_provider: Optional[str] = Field(None, description="OAuth provider (google/apple)")
    oauth_token: Optional[str] = Field(None, description="OAuth access token")
    password_reset: Optional[bool] = Field(False, description="Flag for password reset")
    redirect: Optional[str] = Field(None, description="Redirect destination after login (e.g., 'life-journey')")
    
    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "password": "securepassword123",
                "oauth_provider": None,
                "oauth_token": None,
                "password_reset": False,
                "redirect": None
            }
        }

class LoginResponse(BaseModel):
    """
    Login response schema for successful authentication.
    """
    success: bool = Field(..., description="Authentication success status")
    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field("bearer", description="Token type")
    expires_in: int = Field(..., description="Token expiration time in seconds")
    user: dict = Field(..., description="User information")
    
    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "token_type": "bearer",
                "expires_in": 1800,
                "user": {
                    "user_id": "riOlwtWJDyRVtsXZqsYW",
                    "email": "user@example.com",
                    "created_timestamp": "2025-01-08T19:41:00Z"
                }
            }
        }

class ErrorResponse(BaseModel):
    """
    Error response schema for authentication failures.
    """
    success: bool = Field(False, description="Authentication success status")
    error: str = Field(..., description="Error message")
    error_code: Optional[str] = Field(None, description="Error code for client handling")
    details: Optional[dict] = Field(None, description="Additional error details")
    
    class Config:
        json_schema_extra = {
            "example": {
                "success": False,
                "error": "Invalid credentials",
                "error_code": "AUTH_FAILED",
                "details": None
            }
        }

class UserInfo(BaseModel):
    """
    User information schema.
    """
    user_id: str = Field(..., description="User's database ID")
    email: str = Field(..., description="User's email address")
    created_timestamp: datetime = Field(..., description="Account creation timestamp")
    updated_timestamp: Optional[datetime] = Field(None, description="Last update timestamp")
    
    class Config:
        json_schema_extra = {
            "example": {
                "user_id": "riOlwtWJDyRVtsXZqsYW",
                "email": "user@example.com",
                "created_timestamp": "2025-01-08T19:41:00Z",
                "updated_timestamp": None
            }
        }

class TokenRefreshRequest(BaseModel):
    """
    Token refresh request schema.
    """
    refresh_token: str = Field(..., description="Current JWT token to refresh")
    
    class Config:
        json_schema_extra = {
            "example": {
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
            }
        }

class TokenRefreshResponse(BaseModel):
    """
    Token refresh response schema.
    """
    success: bool = Field(..., description="Refresh success status")
    access_token: str = Field(..., description="New JWT access token")
    token_type: str = Field("bearer", description="Token type")
    expires_in: int = Field(..., description="Token expiration time in seconds")
    
    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "token_type": "bearer",
                "expires_in": 1800
            }
        }

class PasswordResetRequest(BaseModel):
    """
    Password reset request schema.
    """
    email: EmailStr = Field(..., description="User's email address")
    new_password: str = Field(..., min_length=8, description="New password")
    reset_token: Optional[str] = Field(None, description="Password reset token")
    
    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "new_password": "newsecurepassword123",
                "reset_token": "reset-token-here"
            }
        }

class PasswordResetResponse(BaseModel):
    """
    Password reset response schema.
    """
    success: bool = Field(..., description="Password reset success status")
    message: str = Field(..., description="Success message")
    
    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Password updated successfully"
            }
        }

class OAuthLoginRequest(BaseModel):
    """
    OAuth login request schema.
    """
    email: EmailStr = Field(..., description="User's email address from OAuth")
    oauth_provider: str = Field(..., description="OAuth provider (google/apple)")
    oauth_token: str = Field(..., description="OAuth access token or ID token")
    user_name: Optional[str] = Field(None, description="User's name from OAuth")
    
    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "oauth_provider": "google",
                "oauth_token": "oauth-access-token-here",
                "user_name": "John Doe"
            }
        }

class HealthCheckResponse(BaseModel):
    """
    Health check response schema.
    """
    status: str = Field(..., description="Service health status")
    database: str = Field(..., description="Database connection status")
    timestamp: str = Field(..., description="Health check timestamp")
    
    class Config:
        json_schema_extra = {
            "example": {
                "status": "healthy",
                "database": "connected",
                "timestamp": "2025-01-08T19:41:00Z"
            }
        }

class TokenVerifyRequest(BaseModel):
    """
    Token verification request schema.
    """
    token: str = Field(..., description="JWT token to verify")
    
    class Config:
        json_schema_extra = {
            "example": {
                "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
            }
        }

class EmailCheckRequest(BaseModel):
    """
    Email check request schema for Terms and Services acceptance flow.
    """
    email: EmailStr = Field(..., description="User's email address to check")
    
    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com"
            }
        }

class EmailCheckResponse(BaseModel):
    """
    Email check response schema.
    """
    success: bool = Field(..., description="Check success status")
    email_exists: bool = Field(..., description="Whether email exists in database")
    requires_terms_acceptance: bool = Field(..., description="Whether user needs to accept Terms and Services")
    
    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "email_exists": False,
                "requires_terms_acceptance": True
            }
        }

class TermsAcceptanceRequest(BaseModel):
    """
    Terms and Services acceptance request schema.
    """
    email: EmailStr = Field(..., description="User's email address")
    accepted: bool = Field(..., description="Whether user accepted Terms and Services")
    terms_version: str = Field(default="1.0", description="Version of Terms and Services accepted")
    ip_address: Optional[str] = Field(None, description="User's IP address for legal records")
    user_agent: Optional[str] = Field(None, description="User's browser user agent")
    
    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "accepted": True,
                "terms_version": "1.0",
                "ip_address": "192.168.1.1",
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        }

class TermsAcceptanceResponse(BaseModel):
    """
    Terms and Services acceptance response schema.
    """
    success: bool = Field(..., description="Acceptance recording success status")
    message: str = Field(..., description="Success message")
    acceptance_id: Optional[str] = Field(None, description="Unique ID for the acceptance record")
    
    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Terms and Services acceptance recorded successfully",
                "acceptance_id": "terms_acceptance_123456"
            }
        }

class TokenVerifyResponse(BaseModel):
    """
    Token verification response schema.
    """
    valid: bool = Field(..., description="Token validity status")
    user_id: Optional[str] = Field(None, description="User ID if token is valid")
    email: Optional[str] = Field(None, description="User email if token is valid")
    expires_at: Optional[datetime] = Field(None, description="Token expiration time")
    new_token: Optional[str] = Field(None, description="New token if automatically refreshed")
    token_refreshed: bool = Field(default=False, description="Whether token was automatically refreshed")
    
    class Config:
        json_schema_extra = {
            "example": {
                "valid": True,
                "user_id": "riOlwtWJDyRVtsXZqsYW",
                "email": "user@example.com",
                "expires_at": "2025-01-08T20:11:00Z",
                "new_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "token_refreshed": True
            }
        }
