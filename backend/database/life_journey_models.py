"""
Data models for Life Journey content management system.
Defines the structure for documenting personal life events with geographical locations.
"""

from typing import Optional
from pydantic import BaseModel, Field
from datetime import datetime

# Collection names for Life Journey system
LIFE_JOURNEY_COLLECTIONS = {
    'life_events': 'life_journey_events',
    'life_journey_media': 'life_journey_media',
    'life_journey_topics': 'life_journey_topics'
}


class LifeJourneyEvent(BaseModel):
    """Model for a life journey event entry."""
    event_slug: str = Field(..., description="Unique URL-friendly identifier")
    event_title: str = Field(..., description="Title of the life event")
    event_description: str = Field(..., description="Short description of the event")
    event_content: str = Field(..., description="Detailed content (markdown supported)")
    event_date: str = Field(..., description="Date of the event (YYYY-MM-DD or similar)")
    
    # Location data
    city: str = Field(..., description="City where event occurred")
    country: str = Field(..., description="Country where event occurred")
    latitude: Optional[float] = Field(None, description="Latitude coordinate")
    longitude: Optional[float] = Field(None, description="Longitude coordinate")
    
    # Media
    image_url: Optional[str] = Field(None, description="Main image URL for the event")
    
    # Publishing metadata
    is_published: bool = Field(False, description="Whether the event is published")
    display_order: int = Field(0, description="Order for displaying events")
    
    # Timestamps and tracking
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    last_edited_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    
    class Config:
        json_schema_extra = {
            "example": {
                "event_slug": "childhood-taiwan",
                "event_title": "Childhood in Taiwan",
                "event_description": "Early years growing up in Taipei",
                "event_content": "Detailed story of childhood experiences...",
                "event_date": "1990-2000",
                "city": "Taipei",
                "country": "Taiwan",
                "latitude": 25.0330,
                "longitude": 121.5654,
                "image_url": "https://storage.googleapis.com/.../image.jpg",
                "is_published": True,
                "display_order": 0
            }
        }


class LifeJourneyMediaFile(BaseModel):
    """Model for media files associated with life journey events."""
    event_id: str = Field(..., description="Reference to the life event")
    file_name: str = Field(..., description="Original file name")
    file_type: str = Field(..., description="Type: 'image' or 'video'")
    content_type: str = Field(..., description="MIME type")
    file_size: int = Field(..., description="File size in bytes")
    storage_path: str = Field(..., description="Path in Google Cloud Storage")
    public_url: str = Field(..., description="Public URL to access the file")
    
    # Optional metadata
    description: Optional[str] = Field(None, description="Description of the media")
    alt_text: Optional[str] = Field(None, description="Alt text for accessibility")
    
    # Tracking
    uploaded_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    class Config:
        json_schema_extra = {
            "example": {
                "event_id": "childhood-taiwan",
                "file_name": "family-photo.jpg",
                "file_type": "image",
                "content_type": "image/jpeg",
                "file_size": 1024000,
                "storage_path": "life-journey/childhood-taiwan/family-photo.jpg",
                "public_url": "https://storage.googleapis.com/.../family-photo.jpg",
                "description": "Family photo from childhood",
                "alt_text": "Family standing together"
            }
        }
