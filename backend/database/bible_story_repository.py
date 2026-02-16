"""
Repository for Bible Story content management operations.
Provides high-level Firestore operations for the Our Faith publishing system.
"""

from typing import List, Optional, Dict, Any
from google.cloud import firestore
from datetime import datetime
import uuid
import logging

from .firestore_config import get_firestore_client
from .bible_story_models import (
    BibleStoryPage, BibleStoryContentSection, BibleStoryMediaFile,
    BIBLE_STORY_COLLECTIONS
)

logger = logging.getLogger(__name__)


class BibleStoryRepository:
    """Repository for Bible story page operations."""
    
    def __init__(self):
        self.db = get_firestore_client()
        self.collection_name = BIBLE_STORY_COLLECTIONS['bible_stories']
        self.collection = None
        
        if self.db is not None:
            self.collection = self.db.collection(self.collection_name)
        else:
            logger.warning(f"Firestore client not available for collection: {self.collection_name}")
    
    def create_or_update_story(self, story_data: Dict[str, Any], user_id: str) -> Optional[str]:
        """Create a new story or update existing one by slug."""
        if not self.collection:
            logger.warning("Firestore not available, cannot create/update story")
            return None
        
        try:
            story_slug = story_data.get('story_slug')
            if not story_slug:
                logger.error("story_slug is required")
                return None
            
            # Check if story exists
            doc_ref = self.collection.document(story_slug)
            doc = doc_ref.get()
            
            story_data['last_edited_by'] = user_id
            story_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            if doc.exists:
                # Update existing
                doc_ref.update(story_data)
                logger.info(f"Story updated: {story_slug}")
            else:
                # Create new
                story_data['created_at'] = firestore.SERVER_TIMESTAMP
                doc_ref.set(story_data)
                logger.info(f"Story created: {story_slug}")
            
            return story_slug
            
        except Exception as e:
            logger.error(f"Error creating/updating story: {e}")
            return None
    
    def get_story_by_slug(self, story_slug: str) -> Optional[Dict[str, Any]]:
        """Get story by slug."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get story {story_slug}")
            return None
        
        try:
            doc_ref = self.collection.document(story_slug)
            doc = doc_ref.get()
            
            if doc.exists:
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving story {story_slug}: {e}")
            return None
    
    def list_all_stories(self, published_only: bool = False) -> List[Dict[str, Any]]:
        """List all Bible stories."""
        if not self.collection:
            logger.warning("Firestore not available, cannot list stories")
            return []
        
        try:
            query = self.collection
            
            if published_only:
                query = query.where(filter=firestore.FieldFilter('is_published', '==', True))
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error listing stories: {e}")
            return []
    
    def publish_story(self, story_slug: str, user_id: str) -> bool:
        """Publish a story."""
        try:
            update_data = {
                'is_published': True,
                'published_at': firestore.SERVER_TIMESTAMP,
                'last_edited_by': user_id,
                'updated_at': firestore.SERVER_TIMESTAMP
            }
            
            doc_ref = self.collection.document(story_slug)
            doc_ref.update(update_data)
            logger.info(f"Story published: {story_slug}")
            return True
            
        except Exception as e:
            logger.error(f"Error publishing story {story_slug}: {e}")
            return False
    
    def unpublish_story(self, story_slug: str, user_id: str) -> bool:
        """Unpublish a story."""
        try:
            update_data = {
                'is_published': False,
                'last_edited_by': user_id,
                'updated_at': firestore.SERVER_TIMESTAMP
            }
            
            doc_ref = self.collection.document(story_slug)
            doc_ref.update(update_data)
            logger.info(f"Story unpublished: {story_slug}")
            return True
            
        except Exception as e:
            logger.error(f"Error unpublishing story {story_slug}: {e}")
            return False
    
    def reorder_stories(self, story_updates: List[Dict[str, Any]], user_id: str) -> bool:
        """
        Reorder multiple stories in a batch.
        story_updates: List of {'story_slug': str, 'new_order': int}
        """
        if not self.collection:
            logger.warning("Firestore not available, cannot reorder stories")
            return False
        
        try:
            batch = self.db.batch()
            
            for update in story_updates:
                story_slug = update['story_slug']
                new_order = update['new_order']
                
                doc_ref = self.collection.document(story_slug)
                batch.update(doc_ref, {
                    'display_order': new_order,
                    'last_edited_by': user_id,
                    'updated_at': firestore.SERVER_TIMESTAMP
                })
            
            batch.commit()
            logger.info(f"Reordered {len(story_updates)} stories")
            return True
            
        except Exception as e:
            logger.error(f"Error reordering stories: {e}")
            return False


class BibleStorySectionRepository:
    """Repository for Bible story content section operations."""
    
    def __init__(self):
        self.db = get_firestore_client()
        self.collection_name = BIBLE_STORY_COLLECTIONS['bible_story_sections']
        self.collection = None
        
        if self.db is not None:
            self.collection = self.db.collection(self.collection_name)
        else:
            logger.warning(f"Firestore client not available for collection: {self.collection_name}")
    
    def create_section(self, section_data: Dict[str, Any], user_id: str) -> Optional[str]:
        """Create a new content section."""
        if not self.collection:
            logger.warning("Firestore not available, cannot create section")
            return None
        
        try:
            section_data['created_by'] = user_id
            section_data['created_at'] = firestore.SERVER_TIMESTAMP
            section_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            doc_ref = self.collection.add(section_data)[1]
            logger.info(f"Section created: {doc_ref.id} for story {section_data.get('story_id')}")
            return doc_ref.id
            
        except Exception as e:
            logger.error(f"Error creating section: {e}")
            return None
    
    def update_section(self, section_id: str, section_data: Dict[str, Any], user_id: str) -> bool:
        """Update an existing section."""
        if not self.collection:
            logger.warning("Firestore not available, cannot update section")
            return False
        
        try:
            section_data['updated_by'] = user_id
            section_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            doc_ref = self.collection.document(section_id)
            doc_ref.update(section_data)
            logger.info(f"Section updated: {section_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating section {section_id}: {e}")
            return False
    
    def delete_section(self, section_id: str) -> bool:
        """Delete a section."""
        if not self.collection:
            logger.warning("Firestore not available, cannot delete section")
            return False
        
        try:
            doc_ref = self.collection.document(section_id)
            doc_ref.delete()
            logger.info(f"Section deleted: {section_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting section {section_id}: {e}")
            return False
    
    def get_sections_by_story(self, story_id: str) -> List[Dict[str, Any]]:
        """Get published sections for a story, ordered by section_order."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get sections for story {story_id}")
            return []
        
        try:
            # Use single index: story_id + section_order, then filter published in memory
            query = (self.collection
                    .where(filter=firestore.FieldFilter('story_id', '==', story_id))
                    .order_by('section_order', direction=firestore.Query.ASCENDING))
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                # Filter for published sections only
                if data.get('is_published', False):
                    data['id'] = doc.id
                    results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving sections for story {story_id}: {e}")
            return []
    
    def get_all_sections_by_story(self, story_id: str) -> List[Dict[str, Any]]:
        """Get all sections for a story including unpublished, ordered by section_order."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get all sections for story {story_id}")
            return []
        
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('story_id', '==', story_id))
                    .order_by('section_order', direction=firestore.Query.ASCENDING))
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving all sections for story {story_id}: {e}")
            return []
    
    def reorder_sections(self, section_updates: List[Dict[str, Any]], user_id: str) -> bool:
        """
        Reorder multiple sections in a batch.
        section_updates: List of {'section_id': str, 'new_order': int}
        """
        if not self.collection:
            logger.warning("Firestore not available, cannot reorder sections")
            return False
        
        try:
            batch = self.db.batch()
            
            for update in section_updates:
                section_id = update['section_id']
                new_order = update['new_order']
                
                doc_ref = self.collection.document(section_id)
                batch.update(doc_ref, {
                    'section_order': new_order,
                    'updated_by': user_id,
                    'updated_at': firestore.SERVER_TIMESTAMP
                })
            
            batch.commit()
            logger.info(f"Reordered {len(section_updates)} sections")
            return True
            
        except Exception as e:
            logger.error(f"Error reordering sections: {e}")
            return False


class BibleStoryMediaRepository:
    """Repository for Bible story media file operations."""
    
    def __init__(self):
        self.db = get_firestore_client()
        self.collection_name = BIBLE_STORY_COLLECTIONS['bible_story_media']
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
            logger.info(f"Media file created: {doc_ref.id} for story {media_data.get('story_id')}")
            return doc_ref.id
            
        except Exception as e:
            logger.error(f"Error creating media file: {e}")
            return None
    
    def get_media_by_story(self, story_id: str) -> List[Dict[str, Any]]:
        """Get all media files for a story."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get media for story {story_id}")
            return []
        
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('story_id', '==', story_id))
                    .order_by('created_at', direction=firestore.Query.DESCENDING))
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving media for story {story_id}: {e}")
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


class PopularStoryLinkRepository:
    """Repository for popular Bible story links shown in the left panel."""
    
    def __init__(self):
        self.db = get_firestore_client()
        self.collection_name = BIBLE_STORY_COLLECTIONS['popular_story_links']
        self.collection = None
        
        if self.db is not None:
            self.collection = self.db.collection(self.collection_name)
        else:
            logger.warning(f"Firestore client not available for collection: {self.collection_name}")
    
    def create_link(self, link_data: Dict[str, Any], user_id: str) -> Optional[str]:
        """Create a new popular story link."""
        if not self.collection:
            logger.warning("Firestore not available, cannot create popular story link")
            return None
        
        try:
            link_data['created_by'] = user_id
            link_data['created_at'] = firestore.SERVER_TIMESTAMP
            link_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            doc_ref = self.collection.add(link_data)[1]
            logger.info(f"Popular story link created: {doc_ref.id}")
            return doc_ref.id
            
        except Exception as e:
            logger.error(f"Error creating popular story link: {e}")
            return None
    
    def update_link(self, link_id: str, link_data: Dict[str, Any], user_id: str) -> bool:
        """Update an existing popular story link."""
        if not self.collection:
            logger.warning("Firestore not available, cannot update popular story link")
            return False
        
        try:
            link_data['last_edited_by'] = user_id
            link_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            doc_ref = self.collection.document(link_id)
            doc_ref.update(link_data)
            logger.info(f"Popular story link updated: {link_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating popular story link {link_id}: {e}")
            return False
    
    def delete_link(self, link_id: str) -> bool:
        """Delete a popular story link."""
        if not self.collection:
            logger.warning("Firestore not available, cannot delete popular story link")
            return False
        
        try:
            doc_ref = self.collection.document(link_id)
            doc_ref.delete()
            logger.info(f"Popular story link deleted: {link_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting popular story link {link_id}: {e}")
            return False
    
    def list_all_links(self, published_only: bool = True) -> List[Dict[str, Any]]:
        """List all popular story links, ordered by display_order."""
        if not self.collection:
            logger.warning("Firestore not available, cannot list popular story links")
            return []
        
        try:
            query = self.collection.order_by('display_order', direction=firestore.Query.ASCENDING)
            
            if published_only:
                # Filter for published links in memory since we can't have composite index yet
                docs = query.stream()
                results = []
                
                for doc in docs:
                    data = doc.to_dict()
                    if data.get('is_published', False):
                        data['id'] = doc.id
                        results.append(data)
                
                return results
            else:
                docs = query.stream()
                results = []
                
                for doc in docs:
                    data = doc.to_dict()
                    data['id'] = doc.id
                    results.append(data)
                
                return results
            
        except Exception as e:
            logger.error(f"Error listing popular story links: {e}")
            return []
    
    def reorder_links(self, link_updates: List[Dict[str, Any]], user_id: str) -> bool:
        """
        Reorder multiple popular story links in a batch.
        link_updates: List of {'link_id': str, 'new_order': int}
        """
        if not self.collection:
            logger.warning("Firestore not available, cannot reorder popular story links")
            return False
        
        try:
            batch = self.db.batch()
            
            for update in link_updates:
                link_id = update['link_id']
                new_order = update['new_order']
                
                doc_ref = self.collection.document(link_id)
                batch.update(doc_ref, {
                    'display_order': new_order,
                    'last_edited_by': user_id,
                    'updated_at': firestore.SERVER_TIMESTAMP
                })
            
            batch.commit()
            logger.info(f"Reordered {len(link_updates)} popular story links")
            return True
            
        except Exception as e:
            logger.error(f"Error reordering popular story links: {e}")
            return False
