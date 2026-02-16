"""
API routes for Our Faith Bible Story content management system.
Provides endpoints for publishers to manage Bible story content.
"""

from fastapi import APIRouter, HTTPException, Depends, File, UploadFile, Form, Header
from fastapi.responses import JSONResponse
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
import logging
from datetime import datetime

from auth.jwt_utils import verify_token as verify_jwt_token
from database.bible_story_repository import (
    BibleStoryRepository, BibleStorySectionRepository, BibleStoryMediaRepository,
    PopularStoryLinkRepository
)
from database.firestore_repository import UserRepository
from database.storage_config import upload_bible_story_media, delete_bible_story_media

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bible-stories", tags=["bible-stories"])

# Pydantic models for request/response
class StoryMetadataRequest(BaseModel):
    story_slug: str
    story_title: str
    story_description: str
    story_reference: Optional[str] = None
    gradient_colors: str = "from-emerald-500 to-teal-600"

class SectionRequest(BaseModel):
    story_id: str
    section_title: str
    section_order: int
    content_type: str  # 'text', 'markdown', 'image', 'video'
    content: str

class SectionUpdateRequest(BaseModel):
    section_title: Optional[str] = None
    section_order: Optional[int] = None
    content_type: Optional[str] = None
    content: Optional[str] = None
    is_published: Optional[bool] = None

class SectionReorderRequest(BaseModel):
    section_updates: List[Dict[str, Any]]  # [{'section_id': str, 'new_order': int}]

class StoryReorderRequest(BaseModel):
    story_updates: List[Dict[str, Any]]  # [{'story_slug': str, 'new_order': int}]

class UserRoleResponse(BaseModel):
    user_id: str
    email: str
    role: str
    is_publisher: bool


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


@router.get("/check-publisher-role")
async def check_publisher_role(token: str = Depends(get_token_from_header)) -> UserRoleResponse:
    """Check if the current user has publisher role."""
    try:
        payload = verify_jwt_token(token)
        user_id = payload.get('user_id')
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_repo = UserRepository()
        user = user_repo.get_by_id(user_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        role = user.get('role', 'user')
        is_publisher = (role == 'publisher')
        
        return UserRoleResponse(
            user_id=user_id,
            email=user.get('email'),
            role=role,
            is_publisher=is_publisher
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking publisher role: {e}")
        raise HTTPException(status_code=500, detail="Failed to check publisher role")


@router.get("/stories")
async def list_stories(
    published_only: bool = True,
    user: Dict[str, Any] = Depends(verify_restricted_access)
):
    """List all Bible stories. Requires restricted content access - defaults to published only."""
    try:
        story_repo = BibleStoryRepository()
        stories = story_repo.list_all_stories(published_only=published_only)
        
        return {
            "success": True,
            "stories": stories
        }
        
    except Exception as e:
        logger.error(f"Error listing stories: {e}")
        raise HTTPException(status_code=500, detail="Failed to list stories")


@router.get("/stories/{story_slug}")
async def get_story(
    story_slug: str,
    user: Dict[str, Any] = Depends(verify_restricted_access)
):
    """Get a specific Bible story with its sections. Requires restricted content access."""
    try:
        story_repo = BibleStoryRepository()
        section_repo = BibleStorySectionRepository()
        
        story = story_repo.get_story_by_slug(story_slug)
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        
        # Get published sections
        sections = section_repo.get_sections_by_story(story_slug)
        
        return {
            "success": True,
            "story": story,
            "sections": sections
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting story {story_slug}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get story")


@router.get("/stories/{story_slug}/all-sections")
async def get_story_all_sections(
    story_slug: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Get a story with all sections including unpublished. Publisher only."""
    try:
        story_repo = BibleStoryRepository()
        section_repo = BibleStorySectionRepository()
        
        story = story_repo.get_story_by_slug(story_slug)
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        
        # Get all sections including unpublished
        sections = section_repo.get_all_sections_by_story(story_slug)
        
        return {
            "success": True,
            "story": story,
            "sections": sections
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting story {story_slug} with all sections: {e}")
        raise HTTPException(status_code=500, detail="Failed to get story")


@router.post("/stories")
async def create_or_update_story(
    story_data: StoryMetadataRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Create or update a Bible story metadata. Publisher only."""
    try:
        story_repo = BibleStoryRepository()
        
        story_dict = story_data.dict()
        story_id = story_repo.create_or_update_story(story_dict, publisher['user_id'])
        
        if not story_id:
            raise HTTPException(status_code=500, detail="Failed to create/update story")
        
        return {
            "success": True,
            "story_id": story_id,
            "message": "Story saved successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating/updating story: {e}")
        raise HTTPException(status_code=500, detail="Failed to save story")


@router.post("/stories/{story_slug}/publish")
async def publish_story(
    story_slug: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Publish a Bible story. Publisher only."""
    try:
        story_repo = BibleStoryRepository()
        success = story_repo.publish_story(story_slug, publisher['user_id'])
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to publish story")
        
        return {
            "success": True,
            "message": "Story published successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error publishing story {story_slug}: {e}")
        raise HTTPException(status_code=500, detail="Failed to publish story")


@router.post("/stories/{story_slug}/unpublish")
async def unpublish_story(
    story_slug: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Unpublish a Bible story. Publisher only."""
    try:
        story_repo = BibleStoryRepository()
        success = story_repo.unpublish_story(story_slug, publisher['user_id'])
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to unpublish story")
        
        return {
            "success": True,
            "message": "Story unpublished successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unpublishing story {story_slug}: {e}")
        raise HTTPException(status_code=500, detail="Failed to unpublish story")


@router.post("/stories/reorder")
async def reorder_stories(
    reorder_data: StoryReorderRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Reorder multiple stories. Publisher only."""
    try:
        story_repo = BibleStoryRepository()
        success = story_repo.reorder_stories(
            reorder_data.story_updates,
            publisher['user_id']
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to reorder stories")
        
        return {
            "success": True,
            "message": "Stories reordered successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reordering stories: {e}")
        raise HTTPException(status_code=500, detail="Failed to reorder stories")


@router.post("/sections")
async def create_section(
    section_data: SectionRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Create a new content section. Publisher only."""
    try:
        section_repo = BibleStorySectionRepository()
        
        section_dict = section_data.dict()
        section_id = section_repo.create_section(section_dict, publisher['user_id'])
        
        if not section_id:
            raise HTTPException(status_code=500, detail="Failed to create section")
        
        return {
            "success": True,
            "section_id": section_id,
            "message": "Section created successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating section: {e}")
        raise HTTPException(status_code=500, detail="Failed to create section")


@router.put("/sections/{section_id}")
async def update_section(
    section_id: str,
    section_data: SectionUpdateRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Update a content section. Publisher only."""
    try:
        section_repo = BibleStorySectionRepository()
        
        # Only include fields that are not None
        update_dict = {k: v for k, v in section_data.dict().items() if v is not None}
        
        if not update_dict:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        success = section_repo.update_section(section_id, update_dict, publisher['user_id'])
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update section")
        
        return {
            "success": True,
            "message": "Section updated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating section {section_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update section")


@router.delete("/sections/{section_id}")
async def delete_section(
    section_id: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Delete a content section. Publisher only."""
    try:
        section_repo = BibleStorySectionRepository()
        success = section_repo.delete_section(section_id)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete section")
        
        return {
            "success": True,
            "message": "Section deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting section {section_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete section")


@router.post("/sections/reorder")
async def reorder_sections(
    reorder_data: SectionReorderRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Reorder multiple sections. Publisher only."""
    try:
        section_repo = BibleStorySectionRepository()
        success = section_repo.reorder_sections(
            reorder_data.section_updates,
            publisher['user_id']
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to reorder sections")
        
        return {
            "success": True,
            "message": "Sections reordered successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reordering sections: {e}")
        raise HTTPException(status_code=500, detail="Failed to reorder sections")


@router.post("/upload-media")
async def upload_media(
    story_id: str = Form(...),
    file: UploadFile = File(...),
    description: Optional[str] = Form(None),
    alt_text: Optional[str] = Form(None),
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Upload media file (image/video) for a Bible story. Publisher only."""
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
        storage_path, public_url = await upload_bible_story_media(
            story_id, file.filename, file_content, content_type
        )
        
        # Save media record to Firestore
        media_repo = BibleStoryMediaRepository()
        media_data = {
            'story_id': story_id,
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


@router.get("/stories/{story_id}/media")
async def get_story_media(
    story_id: str,
    user: Dict[str, Any] = Depends(verify_restricted_access)
):
    """Get all media files for a story. Requires restricted content access."""
    try:
        media_repo = BibleStoryMediaRepository()
        media_files = media_repo.get_media_by_story(story_id)
        
        return {
            "success": True,
            "media": media_files
        }
        
    except Exception as e:
        logger.error(f"Error getting media for story {story_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get media")


@router.delete("/media/{media_id}")
async def delete_media(
    media_id: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Delete a media file. Publisher only."""
    try:
        media_repo = BibleStoryMediaRepository()
        
        # TODO: Also delete from Google Cloud Storage
        # This would require getting the storage_path first, then calling delete_bible_story_media()
        
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


# Popular Story Links endpoints

class PopularStoryLinkRequest(BaseModel):
    story_short_name: str
    story_slug: str
    bible_book: str
    bible_chapter: str
    bible_verses: Optional[str] = None
    display_order: int = 0

class PopularStoryLinkUpdateRequest(BaseModel):
    story_short_name: Optional[str] = None
    story_slug: Optional[str] = None
    bible_book: Optional[str] = None
    bible_chapter: Optional[str] = None
    bible_verses: Optional[str] = None
    display_order: Optional[int] = None
    is_published: Optional[bool] = None

class PopularStoryLinkReorderRequest(BaseModel):
    link_updates: List[Dict[str, Any]]  # [{'link_id': str, 'new_order': int}]


@router.get("/popular-story-links")
async def list_popular_story_links(
    published_only: bool = True,
    user: Dict[str, Any] = Depends(verify_restricted_access)
):
    """List all popular story links. Requires restricted content access - defaults to published only."""
    try:
        link_repo = PopularStoryLinkRepository()
        links = link_repo.list_all_links(published_only=published_only)
        
        return {
            "success": True,
            "links": links
        }
        
    except Exception as e:
        logger.error(f"Error listing popular story links: {e}")
        raise HTTPException(status_code=500, detail="Failed to list popular story links")


@router.post("/popular-story-links")
async def create_popular_story_link(
    link_data: PopularStoryLinkRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Create a new popular story link. Publisher only."""
    try:
        link_repo = PopularStoryLinkRepository()
        
        link_dict = link_data.dict()
        link_id = link_repo.create_link(link_dict, publisher['user_id'])
        
        if not link_id:
            raise HTTPException(status_code=500, detail="Failed to create popular story link")
        
        return {
            "success": True,
            "link_id": link_id,
            "message": "Popular story link created successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating popular story link: {e}")
        raise HTTPException(status_code=500, detail="Failed to create popular story link")


@router.put("/popular-story-links/{link_id}")
async def update_popular_story_link(
    link_id: str,
    link_data: PopularStoryLinkUpdateRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Update a popular story link. Publisher only."""
    try:
        link_repo = PopularStoryLinkRepository()
        
        # Only include fields that are not None
        update_dict = {k: v for k, v in link_data.dict().items() if v is not None}
        
        if not update_dict:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        success = link_repo.update_link(link_id, update_dict, publisher['user_id'])
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update popular story link")
        
        return {
            "success": True,
            "message": "Popular story link updated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating popular story link {link_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update popular story link")


@router.delete("/popular-story-links/{link_id}")
async def delete_popular_story_link(
    link_id: str,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Delete a popular story link. Publisher only."""
    try:
        link_repo = PopularStoryLinkRepository()
        success = link_repo.delete_link(link_id)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete popular story link")
        
        return {
            "success": True,
            "message": "Popular story link deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting popular story link {link_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete popular story link")


@router.post("/popular-story-links/reorder")
async def reorder_popular_story_links(
    reorder_data: PopularStoryLinkReorderRequest,
    publisher: Dict[str, Any] = Depends(verify_publisher)
):
    """Reorder multiple popular story links. Publisher only."""
    try:
        link_repo = PopularStoryLinkRepository()
        success = link_repo.reorder_links(
            reorder_data.link_updates,
            publisher['user_id']
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to reorder popular story links")
        
        return {
            "success": True,
            "message": "Popular story links reordered successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reordering popular story links: {e}")
        raise HTTPException(status_code=500, detail="Failed to reorder popular story links")
