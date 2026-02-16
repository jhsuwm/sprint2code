"""
Firestore data models for Our Faith Bible Story content management.
These models provide structure and validation for Bible story publishing system.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, validator
from google.cloud import firestore
import uuid

from .firestore_models import FirestoreBaseModel

class BibleStoryContentSection(FirestoreBaseModel):
    """Model for individual content sections within a Bible story page."""
    
    id: Optional[str] = None  # Document ID
    story_id: str = Field(..., description="Reference to parent Bible story")
    section_title: str = Field(..., description="Title of the content section")
    section_order: int = Field(..., description="Order/sequence of the section on the page")
    content_type: str = Field(..., description="Type of content: 'text', 'markdown', 'image', 'video'")
    content: str = Field(..., description="Content text or URL for media")
    
    # Metadata
    created_by: str = Field(..., description="User ID of publisher who created this")
    updated_by: Optional[str] = Field(None, description="User ID of last updater")
    is_published: bool = Field(default=True, description="Whether section is published")
    
    @validator('content_type')
    def validate_content_type(cls, v):
        valid_types = ['text', 'markdown', 'image', 'video']
        if v not in valid_types:
            raise ValueError(f"content_type must be one of {valid_types}")
        return v
    
    @validator('section_order')
    def validate_section_order(cls, v):
        if v < 0:
            raise ValueError('section_order must be non-negative')
        return v
    
    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        if 'id' in data and data['id'] is None:
            del data['id']
        return data


class BibleStoryPage(FirestoreBaseModel):
    """Model for Bible story page metadata."""
    
    id: Optional[str] = None  # Document ID (use story slug as ID)
    story_slug: str = Field(..., description="URL slug for the story (e.g., 'creation', 'noahs-ark')")
    story_title: str = Field(..., description="Display title of the story")
    story_description: str = Field(..., description="Short description for story card")
    story_reference: Optional[str] = Field(None, description="Bible reference (e.g., 'Genesis 1-2')")
    gradient_colors: str = Field(default="from-emerald-500 to-teal-600", description="Tailwind gradient classes")
    
    # Publishing metadata
    is_published: bool = Field(default=False, description="Whether story is published")
    published_at: Optional[datetime] = Field(None, description="Publication timestamp")
    last_edited_by: Optional[str] = Field(None, description="User ID of last editor")
    
    @validator('story_slug')
    def validate_slug(cls, v):
        if not v or not v.replace('-', '').replace('_', '').isalnum():
            raise ValueError('story_slug must contain only alphanumeric characters, hyphens, and underscores')
        return v.lower()
    
    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        if 'id' in data and data['id'] is None:
            del data['id']
        return data


class BibleStoryMediaFile(FirestoreBaseModel):
    """Model for media files attached to Bible stories."""
    
    id: Optional[str] = None  # Document ID
    story_id: str = Field(..., description="Reference to parent Bible story")
    file_name: str = Field(..., description="Original file name")
    file_type: str = Field(..., description="File type: 'image', 'video', 'document'")
    content_type: str = Field(..., description="MIME type")
    file_size: int = Field(..., description="File size in bytes")
    storage_path: str = Field(..., description="Path in Google Cloud Storage")
    public_url: Optional[str] = Field(None, description="Public URL")
    
    # Metadata
    uploaded_by: str = Field(..., description="User ID of publisher who uploaded")
    description: Optional[str] = Field(None, description="Media description")
    alt_text: Optional[str] = Field(None, description="Alt text for accessibility")
    
    @validator('file_type')
    def validate_file_type(cls, v):
        valid_types = ['image', 'video', 'document']
        if v not in valid_types:
            raise ValueError(f"file_type must be one of {valid_types}")
        return v
    
    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        if 'id' in data and data['id'] is None:
            del data['id']
        return data


class PopularStoryLink(FirestoreBaseModel):
    """Model for popular Bible story links shown in the left panel."""
    
    id: Optional[str] = None  # Document ID
    story_short_name: str = Field(..., description="Short display name for the story link")
    story_slug: str = Field(..., description="Reference to the Bible story slug")
    bible_book: str = Field(..., description="Bible book name (e.g., 'Genesis', 'Matthew')")
    bible_chapter: str = Field(..., description="Bible chapter(s) (e.g., '1', '1-2')")
    bible_verses: Optional[str] = Field(None, description="Bible verse(s) (e.g., '1-5', '1,3,5')")
    display_order: int = Field(default=0, description="Order in the left panel")
    
    # Publishing metadata
    is_published: bool = Field(default=True, description="Whether link is published")
    created_by: str = Field(..., description="User ID of publisher who created this")
    last_edited_by: Optional[str] = Field(None, description="User ID of last editor")
    
    @validator('display_order')
    def validate_display_order(cls, v):
        if v < 0:
            raise ValueError('display_order must be non-negative')
        return v
    
    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        if 'id' in data and data['id'] is None:
            del data['id']
        return data


# Collection names for Bible Story system
BIBLE_STORY_COLLECTIONS = {
    'bible_stories': 'bible_stories',
    'bible_story_sections': 'bible_story_sections',
    'bible_story_media': 'bible_story_media',
    'popular_story_links': 'popular_story_links'
}
