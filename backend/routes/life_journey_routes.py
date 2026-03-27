"""
API routes for Life Journey content management system.
Provides endpoints for publishers to manage life journey event content.
"""

from fastapi import APIRouter, HTTPException, Depends, File, UploadFile, Form, Header
from fastapi.responses import JSONResponse
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
import logging
from datetime import datetime

from auth.jwt_utils import verify_token as verify_jwt_token
from database.life_journey_repository import (
    LifeJourneyEventRepository, LifeJourneyMediaRepository
)
from database.firestore_repository import UserRepository
from database.storage_config import upload_life_journey_media, delete_life_journey_media

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/life-journey", tags=["life-journey"])

# Pydantic models for request/response
class EventRequest(BaseModel):
    event_slug: str
    event_title: str
    event_description: str
    event_content: str
    event_date: str
    city: str
    country: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    image_url: Optional[str] = None
    topic_id: Optional[str] = None

class EventUpdateRequest(BaseModel):
    event_title: Optional[str] = None
    event_description: Optional[str] = None
    event_content: Optional[str] = None
    event_date: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    image_url: Optional[str] = None
    is_published: Optional[bool] = None
    topic_id: Optional[str] = None

class EventReorderRequest(BaseModel):
    event_updates: List[Dict[str, Any]]  # [{'event_slug': str, 'new_order': int}]

class TopicRequest(BaseModel):
    topic_name: str
    topic_description: Optional[str] = None

class TopicUpdateRequest(BaseModel):
    topic_name: Optional[str] = None
    topic_description: Optional[str] = None

class TopicReorderRequest(BaseModel):
    topic_updates: List[Dict[str, Any]]  # [{'topic_id': str, 'new_order': int}]


# Dependency to extract and verify token from Authorization header
async def get_token_from_header(authorization: Optional[str] = Header(None)) -> str:
    """Extract Bearer token from Authorization header."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    
    return authorization.replace("Bearer ", "")


# Dependency to verify restricted access
async def verify_restricted_access(token: str = Depends(get_token_from_header)) -> Dict[str, Any]:
    """Verify that the user has restricted content access."""
    try:
        payload = verify_jwt_token(token)
        user_id = payload.get('user_id')
        has_restricted_access = payload.get('has_restricted_access', False)
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        # Check if user has restricted access
        if not has_restricted_access:
            raise HTTPException(status_code=403, detail="Restricted content access required")
        
        return {
            'user_id': user_id,
            'email': payload.get('email'),
            'has_restricted_access': has_restricted_access
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error verifying restricted access: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify restricted access")


# Dependency to verify publisher role
async def verify_publisher(token: str = Depends(get_token_from_header)) -> Dict[str, Any]:
    """Verify that the user has publisher role."""
    try:
        payload = verify_jwt_token(token)
        user_id = payload.get('user_id')
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        # Get user from database
        user_repo = UserRepository()
        user = user_repo.get_by_id(user_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Check if user has publisher role
        if user.get('role') != 'publisher':
            raise HTTPException(status_code=403, detail="Publisher access required")
        
        return {'user_id': user_id, 'email': user.get('email'), 'role': user.get('role')}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error verifying publisher: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify publisher access")


@router.get("/events")
async def list_events(
    published_only: bool = True,
    topic_id: Optional[str] = None,
    user: Dict[str, Any] = Depends(verify_restricted_access)
):
    """List all life journey events. Requires restricted content access."""
    try:
        event_repo = LifeJourneyEventRepository()
        events = event_repo.list_all_events(published_only=published_only, topic_id=topic_id)
        
        return {
            "success": True,
            "events": events
        }
        
    except Exception as e:
        logger.error(f"Error listing life journey events: {e}")
        raise HTTPException(status_code=500, detail="Failed to list events")


@router.get("/events/{event_slug}")
async def get_event(
    event_slug: str,
    user: Dict[str, Any] = Depends(verify_restricted_access)
):
    """Get a specific life journey event. Requires restricted content access."""
    try:
        event_repo = LifeJourneyEventRepository()
        
        event = event_repo.get_event_by_slug(event_slug)
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        
        return {
            "success": True,
            "event": event
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting event {event_slug}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get event")


@router.post("/events")
async def create_or_update_event(
    event_data: EventRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Create or update a life journey event. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        
        event_dict = event_data.dict()
        event_id = event_repo.create_or_update_event(event_dict, publisher['user_id'])
        
        if not event_id:
            raise HTTPException(status_code=500, detail="Failed to create/update event")
        
        return {
            "success": True,
            "event_id": event_id,
            "message": "Event saved successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating/updating event: {e}")
        raise HTTPException(status_code=500, detail="Failed to save event")


@router.put("/events/{event_slug}")
async def update_event(
    event_slug: str,
    event_data: EventUpdateRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Update a life journey event. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        
        # Get existing event
        existing_event = event_repo.get_event_by_slug(event_slug)
        if not existing_event:
            raise HTTPException(status_code=404, detail="Event not found")
        
        # Only include fields that are not None
        update_dict = {k: v for k, v in event_data.dict().items() if v is not None}
        
        if not update_dict:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        # Merge with existing data
        update_dict['event_slug'] = event_slug
        event_id = event_repo.create_or_update_event(update_dict, publisher['user_id'])
        
        if not event_id:
            raise HTTPException(status_code=500, detail="Failed to update event")
        
        return {
            "success": True,
            "message": "Event updated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating event {event_slug}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update event")


@router.delete("/events/{event_slug}")
async def delete_event(
    event_slug: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Delete a life journey event. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        success = event_repo.delete_event(event_slug)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete event")
        
        return {
            "success": True,
            "message": "Event deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting event {event_slug}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete event")


@router.post("/events/{event_slug}/publish")
async def publish_event(
    event_slug: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Publish a life journey event. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        success = event_repo.publish_event(event_slug, publisher['user_id'])
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to publish event")
        
        return {
            "success": True,
            "message": "Event published successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error publishing event {event_slug}: {e}")
        raise HTTPException(status_code=500, detail="Failed to publish event")


@router.post("/events/{event_slug}/unpublish")
async def unpublish_event(
    event_slug: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Unpublish a life journey event. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        success = event_repo.unpublish_event(event_slug, publisher['user_id'])
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to unpublish event")
        
        return {
            "success": True,
            "message": "Event unpublished successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unpublishing event {event_slug}: {e}")
        raise HTTPException(status_code=500, detail="Failed to unpublish event")


@router.post("/events/reorder")
async def reorder_events(
    reorder_data: EventReorderRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Reorder multiple events. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        success = event_repo.reorder_events(
            reorder_data.event_updates,
            publisher['user_id']
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to reorder events")
        
        return {
            "success": True,
            "message": "Events reordered successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reordering events: {e}")
        raise HTTPException(status_code=500, detail="Failed to reorder events")


@router.post("/upload-media")
async def upload_media(
    event_id: str = Form(...),
    file: UploadFile = File(...),
    description: Optional[str] = Form(None),
    alt_text: Optional[str] = Form(None),
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Upload media file (image/video) for a life journey event. Publisher only."""
    try:
        # Validate file type
        content_type = file.content_type or ''
        if not (content_type.startswith('image/') or content_type.startswith('video/')):
            raise HTTPException(status_code=400, detail="Only image and video files are allowed")
        
        # Determine file type
        file_type = 'image' if content_type.startswith('image/') else 'video'
        
        # Read file content
        file_content = await file.read()
        file_size = len(file_content)
        
        # Upload to Google Cloud Storage
        storage_path, public_url = await upload_life_journey_media(
            event_id, file.filename, file_content, content_type
        )
        
        # Save media record to Firestore
        media_repo = LifeJourneyMediaRepository()
        media_data = {
            'event_id': event_id,
            'file_name': file.filename,
            'file_type': file_type,
            'content_type': content_type,
            'file_size': file_size,
            'storage_path': storage_path,
            'public_url': public_url,
            'description': description,
            'alt_text': alt_text
        }
        
        media_id = media_repo.create_media_file(media_data, publisher['user_id'])
        
        if not media_id:
            raise HTTPException(status_code=500, detail="Failed to save media record")
        
        return {
            "success": True,
            "media_id": media_id,
            "public_url": public_url,
            "message": "Media uploaded successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading media: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload media")


@router.get("/events/{event_id}/media")
async def get_event_media(
    event_id: str,
    user: Dict[str, Any] = Depends(verify_restricted_access)
):
    """Get all media files for an event. Requires restricted content access."""
    try:
        media_repo = LifeJourneyMediaRepository()
        media_files = media_repo.get_media_by_event(event_id)
        
        return {
            "success": True,
            "media": media_files
        }
        
    except Exception as e:
        logger.error(f"Error getting media for event {event_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get media")


@router.delete("/media/{media_id}")
async def delete_media(
    media_id: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Delete a media file. Publisher only."""
    try:
        media_repo = LifeJourneyMediaRepository()
        success = media_repo.delete_media_file(media_id)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete media")
        
        return {
            "success": True,
            "message": "Media deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting media {media_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete media")


# Topic Management Endpoints

@router.get("/topics")
async def list_topics(user: Dict[str, Any] = Depends(verify_restricted_access)):
    """List all life journey topics. Requires restricted content access."""
    try:
        event_repo = LifeJourneyEventRepository()
        topics = event_repo.list_all_topics()
        
        return {
            "success": True,
            "topics": topics
        }
        
    except Exception as e:
        logger.error(f"Error listing life journey topics: {e}")
        raise HTTPException(status_code=500, detail="Failed to list topics")


@router.post("/topics")
async def create_topic(
    topic_data: TopicRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Create a new life journey topic. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        
        topic_dict = topic_data.dict()
        topic_id = event_repo.create_topic(topic_dict, publisher['user_id'])
        
        if not topic_id:
            raise HTTPException(status_code=500, detail="Failed to create topic")
        
        return {
            "success": True,
            "topic_id": topic_id,
            "message": "Topic created successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating topic: {e}")
        raise HTTPException(status_code=500, detail="Failed to create topic")


@router.put("/topics/{topic_id}")
async def update_topic(
    topic_id: str,
    topic_data: TopicUpdateRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Update a life journey topic. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        
        # Only include fields that are not None
        update_dict = {k: v for k, v in topic_data.dict().items() if v is not None}
        
        if not update_dict:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        success = event_repo.update_topic(topic_id, update_dict, publisher['user_id'])
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update topic")
        
        return {
            "success": True,
            "message": "Topic updated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating topic {topic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update topic")


@router.delete("/topics/{topic_id}")
async def delete_topic(
    topic_id: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Delete a life journey topic. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        success = event_repo.delete_topic(topic_id)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete topic")
        
        return {
            "success": True,
            "message": "Topic deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting topic {topic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete topic")


@router.post("/topics/reorder")
async def reorder_topics(
    reorder_data: TopicReorderRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Reorder multiple topics. Publisher only."""
    try:
        event_repo = LifeJourneyEventRepository()
        success = event_repo.reorder_topics(
            reorder_data.topic_updates,
            publisher['user_id']
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to reorder topics")
        
        return {
            "success": True,
            "message": "Topics reordered successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reordering topics: {e}")
        raise HTTPException(status_code=500, detail="Failed to reorder topics")
