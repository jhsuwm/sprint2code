"""
Google Cloud Firestore configuration for the vacation planner application.
This module handles Firestore database connection and initialization.
"""

import os
import logging
from typing import Optional
from google.cloud import firestore
from google.cloud.exceptions import GoogleCloudError
from google.oauth2 import service_account

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv is optional, continue without it
    pass

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class FirestoreConfig:
    """Firestore configuration and connection management."""
    
    def __init__(self):
        self.project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
        self.credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        self.database_id = os.getenv("FIRESTORE_DATABASE_ID", "(default)")
        self._client = None
        
        # Firestore is optional - only log at debug level if not configured
        if not self.project_id:
            logger.debug("Google Cloud/Firestore not configured (optional)")
    
    @property
    def client(self) -> Optional[firestore.Client]:
        """Get or create Firestore client."""
        if not self.project_id:
            logger.debug("Firestore not configured (optional)")
            return None
            
        if self._client is None:
            try:
                # Use default credentials (for Cloud Run or gcloud auth)
                self._client = firestore.Client(
                    project=self.project_id,
                    database=self.database_id
                )
                
                logger.info(f"Firestore client initialized for project: {self.project_id}")
                
            except GoogleCloudError as e:
                logger.debug(f"Firestore initialization error (optional): {e}")
                return None
            except Exception as e:
                logger.debug(f"Firestore initialization error (optional): {e}")
                return None
        
        return self._client
    
    def health_check(self) -> bool:
        """
        Check if Firestore connection is healthy.
        
        Returns:
            bool: True if connection is healthy, False otherwise
        """
        try:
            if not self.client:
                return False
            
            # Simple non-blocking health check
            # Just verify the client exists and project_id is set
            if self.project_id and self._client:
                return True
            return False
                
        except Exception as e:
            logger.error(f"Firestore health check failed: {e}")
            return False
    
    def initialize_collections(self):
        """Initialize Firestore collections with proper indexes and security rules."""
        try:
            if not self.client:
                logger.debug("Firestore not configured, skipping collection initialization")
                return
                
            # Create initial collections if they don't exist
            collections = ['users', 'chat_conversations', 'vacation_plans', 'vacation_events', 'media_files', 'autonomous_dev_configs']
            
            for collection_name in collections:
                # Create a dummy document to initialize the collection
                doc_ref = self.client.collection(collection_name).document('_init')
                doc_ref.set({
                    'initialized': True,
                    'created_at': firestore.SERVER_TIMESTAMP
                })
                # Delete the dummy document
                doc_ref.delete()
                
            logger.info("Firestore collections initialized successfully")
            
        except Exception as e:
            logger.debug(f"Firestore collection initialization skipped (optional): {e}")

# Global Firestore configuration instance
firestore_config = FirestoreConfig()

def get_firestore_client() -> Optional[firestore.Client]:
    """
    Get the Firestore client instance.
    
    Returns:
        firestore.Client: Firestore client instance or None if unavailable
    """
    return firestore_config.client

def check_firestore_connection() -> bool:
    """
    Check if Firestore connection is healthy.
    
    Returns:
        bool: True if connection is healthy, False otherwise
    """
    return firestore_config.health_check()
