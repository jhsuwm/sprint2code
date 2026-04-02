"""
OAuth utilities for Google and Apple authentication.
This module handles OAuth token verification and user information extraction.
"""

import os
import requests
import jwt
from typing import Optional, Dict, Any
from fastapi import HTTPException, status
from log_config import info, debug, error, warning, critical

# OAuth Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
APPLE_CLIENT_ID = os.getenv("APPLE_CLIENT_ID")
APPLE_TEAM_ID = os.getenv("APPLE_TEAM_ID")
APPLE_KEY_ID = os.getenv("APPLE_KEY_ID")

class OAuthProvider:
    """Base class for OAuth providers."""
    
    def verify_token(self, token: str) -> Dict[str, Any]:
        """Verify OAuth token and return user information."""
        raise NotImplementedError
    
    def get_user_info(self, token: str) -> Dict[str, Any]:
        """Get user information from OAuth provider."""
        raise NotImplementedError

class GoogleOAuth(OAuthProvider):
    """Google OAuth implementation."""
    
    def __init__(self):
        self.client_id = GOOGLE_CLIENT_ID
        self.token_info_url = "https://oauth2.googleapis.com/tokeninfo"
        self.userinfo_url = "https://www.googleapis.com/oauth2/v2/userinfo"
    
    def verify_token(self, token: str) -> Dict[str, Any]:
        """
        Verify Google OAuth token.
        
        Args:
            token: Google OAuth access token
        
        Returns:
            Dict[str, Any]: Token verification result
        
        Raises:
            HTTPException: If token verification fails
        """
        try:
            # Verify token with Google
            response = requests.get(
                self.token_info_url,
                params={"access_token": token},
                timeout=10
            )
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid Google OAuth token"
                )
            
            token_info = response.json()
            
            # Verify audience (client_id)
            if self.client_id and token_info.get("aud") != self.client_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid Google OAuth client"
                )
            
            return token_info
            
        except requests.RequestException as e:
            error(f"Google OAuth verification error: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not verify Google OAuth token"
            )
    
    def get_user_info(self, token: str) -> Dict[str, Any]:
        """
        Get user information from Google.
        
        Args:
            token: Google OAuth access token
        
        Returns:
            Dict[str, Any]: User information
        """
        try:
            # First verify the token
            self.verify_token(token)
            
            # Get user info
            response = requests.get(
                self.userinfo_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not get Google user info"
                )
            
            user_info = response.json()
            
            return {
                "email": user_info.get("email"),
                "name": user_info.get("name"),
                "picture": user_info.get("picture"),
                "verified_email": user_info.get("verified_email", False),
                "provider": "google"
            }
            
        except requests.RequestException as e:
            error(f"Google user info error: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not get Google user information"
            )

class AppleOAuth(OAuthProvider):
    """Apple OAuth implementation."""
    
    def __init__(self):
        self.client_id = APPLE_CLIENT_ID
        self.team_id = APPLE_TEAM_ID
        self.key_id = APPLE_KEY_ID
        self.keys_url = "https://appleid.apple.com/auth/keys"
    
    def verify_token(self, token: str) -> Dict[str, Any]:
        """
        Verify Apple OAuth token (ID token).
        
        Args:
            token: Apple OAuth ID token (JWT)
        
        Returns:
            Dict[str, Any]: Token verification result
        
        Raises:
            HTTPException: If token verification fails
        """
        try:
            # Get Apple's public keys
            response = requests.get(self.keys_url, timeout=10)
            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Could not get Apple public keys"
                )
            
            keys = response.json()["keys"]
            
            # Decode token header to get key ID
            unverified_header = jwt.get_unverified_header(token)
            key_id = unverified_header.get("kid")
            
            # Find the matching key
            public_key = None
            for key in keys:
                if key["kid"] == key_id:
                    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
                    break
            
            if not public_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not find Apple public key"
                )
            
            # Verify and decode the token
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer="https://appleid.apple.com"
            )
            
            return payload
            
        except jwt.InvalidTokenError as e:
            error(f"Apple OAuth token error: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Apple OAuth token"
            )
        except requests.RequestException as e:
            error(f"Apple OAuth verification error: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not verify Apple OAuth token"
            )
    
    def get_user_info(self, token: str) -> Dict[str, Any]:
        """
        Get user information from Apple ID token.
        
        Args:
            token: Apple OAuth ID token
        
        Returns:
            Dict[str, Any]: User information
        """
        payload = self.verify_token(token)
        
        return {
            "email": payload.get("email"),
            "name": payload.get("name"),
            "verified_email": payload.get("email_verified", False),
            "provider": "apple",
            "apple_user_id": payload.get("sub")
        }

def get_oauth_provider(provider: str) -> OAuthProvider:
    """
    Get OAuth provider instance.
    
    Args:
        provider: Provider name ('google' or 'apple')
    
    Returns:
        OAuthProvider: Provider instance
    
    Raises:
        HTTPException: If provider is not supported
    """
    if provider.lower() == "google":
        return GoogleOAuth()
    elif provider.lower() == "apple":
        return AppleOAuth()
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported OAuth provider: {provider}"
        )

def verify_oauth_token(provider: str, token: str) -> Dict[str, Any]:
    """
    Verify OAuth token for any supported provider.
    
    Args:
        provider: Provider name ('google' or 'apple')
        token: OAuth token
    
    Returns:
        Dict[str, Any]: User information from OAuth provider
    """
    oauth_provider = get_oauth_provider(provider)
    return oauth_provider.get_user_info(token)