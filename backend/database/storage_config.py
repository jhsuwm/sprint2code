"""
Google Cloud Storage configuration for the vacation planner application.
This module handles Cloud Storage bucket connection and file operations.
"""

import os
import logging
from typing import Optional, BinaryIO, Dict, Any
from google.cloud import storage
from google.cloud.exceptions import GoogleCloudError, NotFound
from google.oauth2 import service_account
import uuid
from datetime import datetime, timedelta

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

class CloudStorageConfig:
    """Google Cloud Storage configuration and file management."""
    
    def __init__(self):
        self.project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
        self.credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        self.bucket_name = os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET")
        self._client = None
        self._bucket = None
        
        if not self.project_id:
            raise ValueError("GOOGLE_CLOUD_PROJECT_ID environment variable is required")
        if not self.bucket_name:
            raise ValueError("GOOGLE_CLOUD_STORAGE_BUCKET environment variable is required")
    
    @property
    def client(self) -> storage.Client:
        """Get or create Cloud Storage client."""
        if self._client is None:
            try:
                if self.credentials_path and os.path.exists(self.credentials_path):
                    # Use service account credentials
                    credentials = service_account.Credentials.from_service_account_file(
                        self.credentials_path
                    )
                    self._client = storage.Client(
                        project=self.project_id,
                        credentials=credentials
                    )
                else:
                    # Use default credentials (for local development with gcloud auth)
                    self._client = storage.Client(project=self.project_id)
                
                logger.info(f"Cloud Storage client initialized for project: {self.project_id}")
                
            except GoogleCloudError as e:
                logger.error(f"Error initializing Cloud Storage client: {e}")
                raise
            except Exception as e:
                logger.error(f"Unexpected error initializing Cloud Storage client: {e}")
                raise
        
        return self._client
    
    @property
    def bucket(self) -> storage.Bucket:
        """Get or create Cloud Storage bucket."""
        if self._bucket is None:
            try:
                self._bucket = self.client.bucket(self.bucket_name)
                # Check if bucket exists
                if not self._bucket.exists():
                    logger.warning(f"Bucket {self.bucket_name} does not exist")
                    raise ValueError(f"Bucket {self.bucket_name} does not exist")
                
            except Exception as e:
                logger.error(f"Error accessing bucket {self.bucket_name}: {e}")
                raise
        
        return self._bucket
    
    def upload_file(self, file_data: BinaryIO, file_name: str, content_type: str, 
                   user_id: int, folder: str = "media") -> Dict[str, Any]:
        """
        Upload a file to Cloud Storage.
        
        Args:
            file_data: Binary file data
            file_name: Original file name
            content_type: MIME type of the file
            user_id: ID of the user uploading the file
            folder: Folder to store the file in
            
        Returns:
            Dict containing file metadata
        """
        try:
            # Generate unique file name
            file_extension = os.path.splitext(file_name)[1]
            unique_filename = f"{uuid.uuid4()}{file_extension}"
            blob_name = f"{folder}/{user_id}/{unique_filename}"
            
            # Create blob and upload
            blob = self.bucket.blob(blob_name)
            blob.upload_from_file(file_data, content_type=content_type)
            
            # Make the blob publicly readable (optional, based on your security requirements)
            # blob.make_public()
            
            # Get file metadata
            file_metadata = {
                "file_id": unique_filename.split('.')[0],
                "original_name": file_name,
                "blob_name": blob_name,
                "bucket_name": self.bucket_name,
                "content_type": content_type,
                "size": blob.size,
                "public_url": blob.public_url,
                "created_at": datetime.utcnow().isoformat(),
                "user_id": user_id
            }
            
            logger.info(f"File uploaded successfully: {blob_name}")
            return file_metadata
            
        except Exception as e:
            logger.error(f"Error uploading file {file_name}: {e}")
            raise
    
    def delete_file(self, blob_name: str) -> bool:
        """
        Delete a file from Cloud Storage.
        
        Args:
            blob_name: Name of the blob to delete
            
        Returns:
            bool: True if deletion was successful, False otherwise
        """
        try:
            blob = self.bucket.blob(blob_name)
            blob.delete()
            logger.info(f"File deleted successfully: {blob_name}")
            return True
            
        except NotFound:
            logger.warning(f"File not found for deletion: {blob_name}")
            return False
        except Exception as e:
            logger.error(f"Error deleting file {blob_name}: {e}")
            return False
    
    def get_signed_url(self, blob_name: str, expiration_hours: int = 1) -> Optional[str]:
        """
        Generate a signed URL for private file access.
        
        Args:
            blob_name: Name of the blob
            expiration_hours: Hours until the URL expires
            
        Returns:
            str: Signed URL or None if error
        """
        try:
            blob = self.bucket.blob(blob_name)
            expiration = datetime.utcnow() + timedelta(hours=expiration_hours)
            
            signed_url = blob.generate_signed_url(
                expiration=expiration,
                method='GET'
            )
            
            return signed_url
            
        except Exception as e:
            logger.error(f"Error generating signed URL for {blob_name}: {e}")
            return None
    
    def list_user_files(self, user_id: int, folder: str = "media") -> list:
        """
        List all files for a specific user.
        
        Args:
            user_id: User ID
            folder: Folder to search in
            
        Returns:
            list: List of file metadata
        """
        try:
            prefix = f"{folder}/{user_id}/"
            blobs = self.bucket.list_blobs(prefix=prefix)
            
            files = []
            for blob in blobs:
                files.append({
                    "blob_name": blob.name,
                    "size": blob.size,
                    "content_type": blob.content_type,
                    "created_at": blob.time_created.isoformat() if blob.time_created else None,
                    "updated_at": blob.updated.isoformat() if blob.updated else None,
                    "public_url": blob.public_url
                })
            
            return files
            
        except Exception as e:
            logger.error(f"Error listing files for user {user_id}: {e}")
            return []
    
    def health_check(self) -> bool:
        """
        Check if Cloud Storage connection is healthy.
        
        Returns:
            bool: True if connection is healthy, False otherwise
        """
        try:
            # Try to access the bucket
            self.bucket.exists()
            return True
        except Exception as e:
            logger.error(f"Cloud Storage health check failed: {e}")
            return False

# Global Cloud Storage configuration instance (lazy initialization)
_storage_config = None

def get_storage_config() -> CloudStorageConfig:
    """Get or create the storage configuration instance."""
    global _storage_config
    if _storage_config is None:
        _storage_config = CloudStorageConfig()
    return _storage_config

def get_storage_client() -> storage.Client:
    """
    Get the Cloud Storage client instance.
    
    Returns:
        storage.Client: Cloud Storage client instance
    """
    return get_storage_config().client

def get_storage_bucket() -> storage.Bucket:
    """
    Get the Cloud Storage bucket instance.
    
    Returns:
        storage.Bucket: Cloud Storage bucket instance
    """
    return get_storage_config().bucket

def check_storage_connection() -> bool:
    """
    Check if Cloud Storage connection is healthy.
    
    Returns:
        bool: True if connection is healthy, False otherwise
    """
    try:
        return get_storage_config().health_check()
    except ValueError:
        # Storage not configured yet
        return False


async def upload_bible_story_media(story_id: str, file_name: str, file_content: bytes, 
                                   content_type: str) -> tuple[str, str]:
    """
    Upload media file for Bible story to Cloud Storage.
    
    Args:
        story_id: Story slug/ID
        file_name: Original file name
        file_content: Binary file content
        content_type: MIME type of the file
        
    Returns:
        tuple: (storage_path, public_url)
    """
    try:
        # Generate unique file name
        file_extension = os.path.splitext(file_name)[1]
        unique_filename = f"{uuid.uuid4()}{file_extension}"
        blob_name = f"bible-stories/{story_id}/{unique_filename}"
        
        # Create blob and upload
        bucket = get_storage_bucket()
        blob = bucket.blob(blob_name)
        blob.upload_from_string(file_content, content_type=content_type)
        
        # Make the blob publicly readable for Bible stories
        blob.make_public()
        
        logger.info(f"Bible story media uploaded successfully: {blob_name}")
        return blob_name, blob.public_url
        
    except Exception as e:
        logger.error(f"Error uploading Bible story media {file_name}: {e}")
        raise


async def delete_bible_story_media(blob_name: str) -> bool:
    """
    Delete Bible story media file from Cloud Storage.
    
    Args:
        blob_name: Name of the blob to delete
        
    Returns:
        bool: True if deletion was successful, False otherwise
    """
    return get_storage_config().delete_file(blob_name)


async def upload_life_journey_media(event_slug: str, file_name: str, file_content: bytes, 
                                    content_type: str) -> tuple[str, str]:
    """
    Upload media file for Life Journey event to Cloud Storage.
    
    Args:
        event_slug: Event slug/ID
        file_name: Original file name
        file_content: Binary file content
        content_type: MIME type of the file
        
    Returns:
        tuple: (storage_path, public_url)
    """
    try:
        # Generate unique file name
        file_extension = os.path.splitext(file_name)[1]
        unique_filename = f"{uuid.uuid4()}{file_extension}"
        blob_name = f"life-journey/{event_slug}/{unique_filename}"
        
        # Create blob and upload
        bucket = get_storage_bucket()
        blob = bucket.blob(blob_name)
        blob.upload_from_string(file_content, content_type=content_type)
        
        # Don't call make_public() - bucket already has public read permissions via IAM
        # (Uniform bucket-level access doesn't support legacy ACLs)
        
        logger.info(f"Life Journey media uploaded successfully: {blob_name}")
        return blob_name, blob.public_url
        
    except Exception as e:
        logger.error(f"Error uploading Life Journey media {file_name}: {e}")
        raise


async def delete_life_journey_media(blob_name: str) -> bool:
    """
    Delete Life Journey media file from Cloud Storage.
    
    Args:
        blob_name: Name of the blob to delete
        
    Returns:
        bool: True if deletion was successful, False otherwise
    """
    return get_storage_config().delete_file(blob_name)
