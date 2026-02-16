"""
Repository for Life Journey content management operations.
Provides high-level Firestore operations for the Life Journey publishing system.
"""

from typing import List, Optional, Dict, Any
from google.cloud import firestore
from datetime import datetime
import logging
import os
import requests

from .firestore_config import get_firestore_client
from .life_journey_models import LIFE_JOURNEY_COLLECTIONS

logger = logging.getLogger(__name__)


class LifeJourneyEventRepository:
    """Repository for life journey event operations."""
    
    def __init__(self):
        self.db = get_firestore_client()
        self.collection_name = LIFE_JOURNEY_COLLECTIONS['life_events']
        self.topics_collection_name = LIFE_JOURNEY_COLLECTIONS.get('life_journey_topics', 'life_journey_topics')
        self.collection = None
        self.topics_collection = None
        self.google_maps_api_key = os.getenv('GOOGLE_MAPS_API_KEY')
        
        if self.db is not None:
            self.collection = self.db.collection(self.collection_name)
            self.topics_collection = self.db.collection(self.topics_collection_name)
        else:
            logger.warning(f"Firestore client not available for collection: {self.collection_name}")
    
    def _geocode_location(self, city: str, country: str) -> Optional[Dict[str, float]]:
        """
        Geocode a location using Google Geocoding API.
        Returns dict with 'lat' and 'lng' keys, or None if geocoding fails.
        """
        if not self.google_maps_api_key:
            logger.warning("Google Maps API key not configured, skipping geocoding")
            return None
        
        try:
            # Construct address with city and country for better accuracy
            address = f"{city}, {country}"
            
            geocoding_url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                'address': address,
                'key': self.google_maps_api_key
            }
            
            logger.info(f"Geocoding location: '{address}'")
            
            response = requests.get(geocoding_url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get('status') == 'OK' and data.get('results'):
                location = data['results'][0]['geometry']['location']
                formatted_address = data['results'][0].get('formatted_address', 'Unknown')
                
                logger.info(f"Successfully geocoded '{address}' to {location['lat']}, {location['lng']} - Address: {formatted_address}")
                
                return {
                    'lat': location['lat'],
                    'lng': location['lng']
                }
            else:
                logger.warning(f"Geocoding failed for '{address}': {data.get('status')}")
                return None
                
        except Exception as e:
            logger.error(f"Error geocoding location '{city}, {country}': {e}")
            return None
    
    def create_or_update_event(self, event_data: Dict[str, Any], user_id: str) -> Optional[str]:
        """Create a new event or update existing one by slug."""
        if not self.collection:
            logger.warning("Firestore not available, cannot create/update event")
            return None
        
        try:
            event_slug = event_data.get('event_slug')
            if not event_slug:
                logger.error("event_slug is required")
                return None
            
            # Auto-geocode if coordinates are not provided
            city = event_data.get('city')
            country = event_data.get('country')
            latitude = event_data.get('latitude')
            longitude = event_data.get('longitude')
            
            if city and country and (latitude is None or longitude is None):
                logger.info(f"Coordinates not provided for {city}, {country}. Attempting to geocode...")
                coords = self._geocode_location(city, country)
                
                if coords:
                    event_data['latitude'] = coords['lat']
                    event_data['longitude'] = coords['lng']
                    logger.info(f"Auto-geocoded {city}, {country} to ({coords['lat']}, {coords['lng']})")
                else:
                    logger.warning(f"Failed to geocode {city}, {country}. Event will be created without coordinates.")
            
            # Check if event exists
            doc_ref = self.collection.document(event_slug)
            doc = doc_ref.get()
            
            event_data['last_edited_by'] = user_id
            event_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            if doc.exists:
                # Update existing
                doc_ref.update(event_data)
                logger.info(f"Life journey event updated: {event_slug}")
            else:
                # Create new - set defaults
                event_data['created_by'] = user_id
                event_data['created_at'] = firestore.SERVER_TIMESTAMP
                event_data.setdefault('is_published', False)  # Default to draft
                event_data.setdefault('display_order', 0)  # Default display order
                doc_ref.set(event_data)
                logger.info(f"Life journey event created: {event_slug}")
            
            return event_slug
            
        except Exception as e:
            logger.error(f"Error creating/updating life journey event: {e}")
            return None
    
    def get_event_by_slug(self, event_slug: str) -> Optional[Dict[str, Any]]:
        """Get event by slug."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get event {event_slug}")
            return None
        
        try:
            doc_ref = self.collection.document(event_slug)
            doc = doc_ref.get()
            
            if doc.exists:
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving event {event_slug}: {e}")
            return None
    
    def list_all_events(self, published_only: bool = False, topic_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all life journey events, optionally filtered by topic."""
        if not self.collection:
            logger.warning("Firestore not available, cannot list events")
            return []
        
        try:
            query = self.collection
            
            if published_only:
                query = query.where(filter=firestore.FieldFilter('is_published', '==', True))
            
            if topic_id:
                query = query.where(filter=firestore.FieldFilter('topic_id', '==', topic_id))
            
            # Order by display_order
            query = query.order_by('display_order', direction=firestore.Query.ASCENDING)
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            logger.info(f"Listed {len(results)} events (published_only={published_only}, topic_id={topic_id})")
            return results
            
        except Exception as e:
            logger.error(f"Error listing life journey events: {e}")
            return []
    
    def publish_event(self, event_slug: str, user_id: str) -> bool:
        """Publish an event."""
        try:
            update_data = {
                'is_published': True,
                'published_at': firestore.SERVER_TIMESTAMP,
                'last_edited_by': user_id,
                'updated_at': firestore.SERVER_TIMESTAMP
            }
            
            doc_ref = self.collection.document(event_slug)
            doc_ref.update(update_data)
            logger.info(f"Life journey event published: {event_slug}")
            return True
            
        except Exception as e:
            logger.error(f"Error publishing event {event_slug}: {e}")
            return False
    
    def unpublish_event(self, event_slug: str, user_id: str) -> bool:
        """Unpublish an event."""
        try:
            update_data = {
                'is_published': False,
                'last_edited_by': user_id,
                'updated_at': firestore.SERVER_TIMESTAMP
            }
            
            doc_ref = self.collection.document(event_slug)
            doc_ref.update(update_data)
            logger.info(f"Life journey event unpublished: {event_slug}")
            return True
            
        except Exception as e:
            logger.error(f"Error unpublishing event {event_slug}: {e}")
            return False
    
    def delete_event(self, event_slug: str) -> bool:
        """Delete an event."""
        if not self.collection:
            logger.warning("Firestore not available, cannot delete event")
            return False
        
        try:
            doc_ref = self.collection.document(event_slug)
            doc_ref.delete()
            logger.info(f"Life journey event deleted: {event_slug}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting event {event_slug}: {e}")
            return False
    
    def reorder_events(self, event_updates: List[Dict[str, Any]], user_id: str) -> bool:
        """
        Reorder multiple events in a batch.
        event_updates: List of {'event_slug': str, 'new_order': int}
        """
        if not self.collection:
            logger.warning("Firestore not available, cannot reorder events")
            return False
        
        try:
            batch = self.db.batch()
            
            for update in event_updates:
                event_slug = update['event_slug']
                new_order = update['new_order']
                
                doc_ref = self.collection.document(event_slug)
                batch.update(doc_ref, {
                    'display_order': new_order,
                    'last_edited_by': user_id,
                    'updated_at': firestore.SERVER_TIMESTAMP
                })
            
            batch.commit()
            logger.info(f"Reordered {len(event_updates)} life journey events")
            return True
            
        except Exception as e:
            logger.error(f"Error reordering events: {e}")
            return False
    
    # Topic Management Methods
    
    def list_all_topics(self) -> List[Dict[str, Any]]:
        """List all life journey topics."""
        if not self.topics_collection:
            logger.warning("Firestore not available, cannot list topics")
            return []
        
        try:
            # Try to order by display_order first, fallback to created_at if display_order doesn't exist
            try:
                query = self.topics_collection.order_by('display_order', direction=firestore.Query.ASCENDING)
                docs = query.stream()
            except Exception:
                # Fallback to created_at if display_order field doesn't exist yet
                query = self.topics_collection.order_by('created_at', direction=firestore.Query.ASCENDING)
                docs = query.stream()
            
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['topic_id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error listing life journey topics: {e}")
            return []
    
    def create_topic(self, topic_data: Dict[str, Any], user_id: str) -> Optional[str]:
        """Create a new topic."""
        if not self.topics_collection:
            logger.warning("Firestore not available, cannot create topic")
            return None
        
        try:
            # Calculate the next display_order by finding the max current order
            existing_topics = self.list_all_topics()
            max_order = -1
            for topic in existing_topics:
                order = topic.get('display_order', 0)
                if order > max_order:
                    max_order = order
            
            next_display_order = max_order + 1
            
            # Remove topic_icon if present (not used in the system)
            if 'topic_icon' in topic_data:
                del topic_data['topic_icon']
            
            topic_data['created_by'] = user_id
            topic_data['created_at'] = firestore.SERVER_TIMESTAMP
            topic_data['updated_at'] = firestore.SERVER_TIMESTAMP
            topic_data['display_order'] = next_display_order
            
            doc_ref = self.topics_collection.add(topic_data)[1]
            logger.info(f"Life journey topic created: {doc_ref.id} with display_order: {next_display_order}")
            return doc_ref.id
            
        except Exception as e:
            logger.error(f"Error creating life journey topic: {e}")
            return None
    
    def update_topic(self, topic_id: str, topic_data: Dict[str, Any], user_id: str) -> bool:
        """Update a topic."""
        if not self.topics_collection:
            logger.warning("Firestore not available, cannot update topic")
            return False
        
        try:
            topic_data['last_edited_by'] = user_id
            topic_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            doc_ref = self.topics_collection.document(topic_id)
            doc_ref.update(topic_data)
            logger.info(f"Life journey topic updated: {topic_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating topic {topic_id}: {e}")
            return False
    
    def delete_topic(self, topic_id: str) -> bool:
        """Delete a topic."""
        if not self.topics_collection:
            logger.warning("Firestore not available, cannot delete topic")
            return False
        
        try:
            doc_ref = self.topics_collection.document(topic_id)
            doc_ref.delete()
            logger.info(f"Life journey topic deleted: {topic_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting topic {topic_id}: {e}")
            return False
    
    def reorder_topics(self, topic_updates: List[Dict[str, Any]], user_id: str) -> bool:
        """
        Reorder multiple topics in a batch.
        topic_updates: List of {'topic_id': str, 'new_order': int}
        """
        if not self.topics_collection:
            logger.warning("Firestore not available, cannot reorder topics")
            return False
        
        try:
            batch = self.db.batch()
            
            for update in topic_updates:
                topic_id = update['topic_id']
                new_order = update['new_order']
                
                doc_ref = self.topics_collection.document(topic_id)
                batch.update(doc_ref, {
                    'display_order': new_order,
                    'last_edited_by': user_id,
                    'updated_at': firestore.SERVER_TIMESTAMP
                })
            
            batch.commit()
            logger.info(f"Reordered {len(topic_updates)} life journey topics")
            return True
            
        except Exception as e:
            logger.error(f"Error reordering topics: {e}")
            return False


class LifeJourneyMediaRepository:
    """Repository for life journey media file operations."""
    
    def __init__(self):
        self.db = get_firestore_client()
        self.collection_name = LIFE_JOURNEY_COLLECTIONS['life_journey_media']
        self.collection = None
        
        if self.db is not None:
            self.collection = self.db.collection(self.collection_name)
        else:
            logger.warning(f"Firestore client not available for collection: {self.collection_name}")
    
    def create_media_file(self, media_data: Dict[str, Any], user_id: str) -> Optional[str]:
        """Create a new media file record."""
        if not self.collection:
            logger.warning("Firestore not available, cannot create media file")
            return None
        
        try:
            media_data['uploaded_by'] = user_id
            media_data['created_at'] = firestore.SERVER_TIMESTAMP
            media_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            doc_ref = self.collection.add(media_data)[1]
            logger.info(f"Media file created: {doc_ref.id} for event {media_data.get('event_id')}")
            return doc_ref.id
            
        except Exception as e:
            logger.error(f"Error creating media file: {e}")
            return None
    
    def get_media_by_event(self, event_id: str) -> List[Dict[str, Any]]:
        """Get all media files for an event."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get media for event {event_id}")
            return []
        
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('event_id', '==', event_id))
                    .order_by('created_at', direction=firestore.Query.DESCENDING))
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving media for event {event_id}: {e}")
            return []
    
    def delete_media_file(self, media_id: str) -> bool:
        """Delete a media file record."""
        if not self.collection:
            logger.warning("Firestore not available, cannot delete media file")
            return False
        
        try:
            doc_ref = self.collection.document(media_id)
            doc_ref.delete()
            logger.info(f"Media file deleted: {media_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting media file {media_id}: {e}")
            return False
