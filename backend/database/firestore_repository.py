"""
Firestore repository layer for the vacation planner application.
This module provides high-level Firestore operations and business logic.
"""

from typing import List, Optional, Dict, Any, Union
from google.cloud import firestore
from google.cloud.exceptions import NotFound, GoogleCloudError
from datetime import datetime, date
import uuid
import logging

from .firestore_config import get_firestore_client
from .firestore_models import (
    UserModel, ChatConversationModel, VacationPlanModel,
    VacationEventModel, MediaFileModel, BlogPostModel,
    TermsAcceptanceModel, FlightSearchModel, COLLECTIONS
)

# Import global constant with fallback for import issues
try:
    from ..constants import MAX_CHAT_HISTORY_CONTEXT
except ImportError:
    try:
        from api.constants import MAX_CHAT_HISTORY_CONTEXT
    except ImportError:
        try:
            # Fallback - import from agents constants
            from ..agents.constants import MAX_CHAT_HISTORY_CONTEXT
        except ImportError:
            # Final fallback - define the constant directly
            MAX_CHAT_HISTORY_CONTEXT = 50

logger = logging.getLogger(__name__)

class FirestoreBaseRepository:
    """Base repository class for common Firestore operations."""
    
    def __init__(self, collection_name: str, model_class):
        self.db = get_firestore_client()
        self.collection_name = collection_name
        self.model_class = model_class
        self.collection = None
        
        # Only initialize collection if we have a valid client
        if self.db is not None:
            self.collection = self.db.collection(collection_name)
        else:
            logger.warning(f"Firestore client not available for collection: {collection_name}")
    
    def create(self, data: Dict[str, Any], doc_id: str = None) -> Optional[str]:
        """
        Create a new document.
        
        Args:
            data: Document data
            doc_id: Optional document ID (auto-generated if not provided)
            
        Returns:
            str: Document ID or None if creation failed
        """
        if not self.collection:
            logger.warning(f"Firestore not available, cannot create document in {self.collection_name}")
            return None
            
        try:
            # Validate data using the model
            model_instance = self.model_class(**data)
            doc_data = model_instance.to_dict()
            
            if doc_id:
                doc_ref = self.collection.document(doc_id)
                doc_ref.set(doc_data)
                logger.info(f"Document created with ID: {doc_id}")
                return doc_id
            else:
                doc_ref = self.collection.add(doc_data)[1]
                logger.info(f"Document created with ID: {doc_ref.id}")
                return doc_ref.id
                
        except Exception as e:
            logger.error(f"Error creating document in {self.collection_name}: {e}")
            return None
    
    def get_by_id(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get document {doc_id} from {self.collection_name}")
            return None
            
        try:
            doc_ref = self.collection.document(doc_id)
            doc = doc_ref.get()
            
            if doc.exists:
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving document {doc_id} from {self.collection_name}: {e}")
            return None
    
    def update(self, doc_id: str, data: Dict[str, Any]) -> bool:
        """Update document by ID."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot update document {doc_id} in {self.collection_name}")
            return False
            
        try:
            doc_ref = self.collection.document(doc_id)
            
            # Add update timestamp and transaction ID
            data['updated_at'] = firestore.SERVER_TIMESTAMP
            data['updated_transaction_id'] = str(uuid.uuid4())
            
            doc_ref.update(data)
            logger.info(f"Document updated: {doc_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating document {doc_id} in {self.collection_name}: {e}")
            return False
    
    def delete(self, doc_id: str) -> bool:
        """Delete document by ID."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot delete document {doc_id} from {self.collection_name}")
            return False
            
        try:
            doc_ref = self.collection.document(doc_id)
            doc_ref.delete()
            logger.info(f"Document deleted: {doc_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting document {doc_id} from {self.collection_name}: {e}")
            return False
    
    def list_all(self, limit: int = 100, order_by: str = 'created_at',
                 descending: bool = True) -> List[Dict[str, Any]]:
        """List all documents with optional ordering and limiting."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot list documents from {self.collection_name}")
            return []
            
        try:
            query = self.collection
            
            if order_by:
                direction = firestore.Query.DESCENDING if descending else firestore.Query.ASCENDING
                query = query.order_by(order_by, direction=direction)
            
            if limit:
                query = query.limit(limit)
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error listing documents from {self.collection_name}: {e}")
            return []

class UserRepository(FirestoreBaseRepository):
    """Repository for user-related Firestore operations."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['users'], UserModel)
    
    def create_user(self, email: str, password: str = None, 
                   transaction_id: str = None, role: str = "user") -> Optional[str]:
        """Create a new user account with default role."""
        try:
            # Check if user already exists
            existing_user = self.get_user_by_email(email)
            if existing_user:
                logger.warning(f"User already exists: {email}")
                return None
            
            user_data = {
                'email': email,
                'password': password,
                'role': role,  # Default to "user", can be changed to "publisher" manually
                'created_transaction_id': transaction_id or str(uuid.uuid4())
            }
            
            user_id = self.create(user_data)
            if user_id:
                logger.info(f"User created successfully: {email} with role: {role}")
            return user_id
            
        except Exception as e:
            logger.error(f"Error creating user {email}: {e}")
            return None
    
    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user by email address."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get user by email {email}")
            return None
            
        try:
            query = self.collection.where(filter=firestore.FieldFilter('email', '==', email.lower()))
            docs = list(query.stream())
            
            if docs:
                doc = docs[0]
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving user by email {email}: {e}")
            return None
    
    def update_user_password(self, user_id: str, new_password: str, 
                           transaction_id: str = None) -> bool:
        """Update user password."""
        try:
            update_data = {
                'password': new_password,
                'updated_transaction_id': transaction_id or str(uuid.uuid4())
            }
            
            success = self.update(user_id, update_data)
            if success:
                logger.info(f"Password updated for user ID: {user_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error updating password for user {user_id}: {e}")
            return False

class ChatRepository(FirestoreBaseRepository):
    """Repository for chat conversation-related Firestore operations."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['chat_conversations'], ChatConversationModel)
    
    async def add_chat_message(self, user_id: str, session_id: str, chat_origin: str, chat_text: str,
                        transaction_id: str = None) -> Optional[str]:
        """Add a new chat message to the conversation."""
        try:
            if chat_origin not in ['user', 'chatbot']:
                logger.error(f"Invalid chat_origin: {chat_origin}")
                return None
            
            chat_data = {
                'user_id': user_id,
                'session_id': session_id,
                'chat_origin': chat_origin,
                'chat_text': chat_text,
                'created_transaction_id': transaction_id or str(uuid.uuid4())
            }
            
            chat_id = self.create(chat_data)
            if chat_id:
                logger.info(f"Chat message added for user {user_id}, session {session_id}: {chat_origin}")
            return chat_id
            
        except Exception as e:
            logger.error(f"Error adding chat message for user {user_id}, session {session_id}: {e}")
            return None
    
    def get_user_chat_history(self, user_id: str, limit: int = MAX_CHAT_HISTORY_CONTEXT,
                             offset: int = 0) -> List[Dict[str, Any]]:
        """Get chat history for a user."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get chat history for user {user_id}")
            return []
            
        try:
            # First try with ordering (requires index)
            try:
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .order_by('created_at', direction=firestore.Query.DESCENDING)
                        .limit(limit))
                
                docs = list(query.stream())
                logger.info(f"Successfully retrieved {len(docs)} chat messages with ordering for user {user_id}")
                
            except Exception as index_error:
                if "requires an index" in str(index_error):
                    logger.warning(f"Ordered query failed (missing index), falling back to simple query with Python sorting: {index_error}")
                else:
                    logger.error(f"Unexpected error in ordered query: {index_error}")
                
                # Fallback to simple query without ordering
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .limit(limit * 2))  # Get more docs to ensure we have enough after sorting
                
                docs = list(query.stream())
                logger.info(f"Retrieved {len(docs)} chat messages without ordering for user {user_id}")
            
            results = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            # Sort in Python if we couldn't sort in Firestore
            if results and 'created_at' in results[0]:
                results.sort(key=lambda x: x.get('created_at', datetime.min), reverse=True)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving chat history for user {user_id}: {e}")
            return []

    def get_recent_chat_messages(self, user_id: str, session_id: str = None, limit: int = 2) -> List[Dict[str, Any]]:
        """Get recent chat messages for a user with limit for performance, optionally filtered by session_id"""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get recent chat messages for user {user_id}")
            return []
            
        try:
            # Use simpler query strategy to avoid complex index requirements
            # Strategy: Query by user_id only (uses existing simple index), then filter and sort in Python
            
            # Get more messages than needed to account for session filtering
            fetch_limit = limit * 10 if session_id else limit * 2
            
            try:
                # Try simple query with user_id and ordering (requires only user_id + created_at index)
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .order_by('created_at', direction=firestore.Query.DESCENDING)
                        .limit(fetch_limit))
                
                docs = list(query.stream())
                logger.info(f"Retrieved {len(docs)} recent chat messages with simple ordering for user {user_id}")
                
            except Exception as index_error:
                if "requires an index" in str(index_error):
                    logger.warning(f"Simple ordered query failed (missing index), falling back to unordered query: {index_error}")
                else:
                    logger.error(f"Unexpected error in simple ordered query: {index_error}")
                
                # Fallback to unordered query by user_id only (no index required beyond user_id)
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .limit(fetch_limit))
                
                docs = list(query.stream())
                logger.info(f"Retrieved {len(docs)} recent chat messages without ordering for user {user_id}")
            
            # Convert to results and filter by session_id in Python if needed
            results = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                
                # Filter by session_id in Python if provided
                if session_id and data.get('session_id') != session_id:
                    continue
                    
                results.append(data)
            
            # Sort in Python by created_at (most recent first)
            results.sort(key=lambda x: x.get('created_at', datetime.min), reverse=True)
            
            # Take only the requested limit
            results = results[:limit]
            
            # Reverse to get chronological order (oldest first)
            results.reverse()
            
            session_info = f", session {session_id}" if session_id else ""
            logger.info(f"Retrieved {len(results)} recent chat messages after Python filtering/sorting for user {user_id}{session_info}")
            return results
                
        except Exception as e:
            session_info = f", session {session_id}" if session_id else ""
            logger.error(f"Error getting recent chat messages for user {user_id}{session_info}: {e}")
            return []

class VacationPlanRepository(FirestoreBaseRepository):
    """Repository for vacation plan-related Firestore operations."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['vacation_plans'], VacationPlanModel)
    
    def create_vacation_plan(self, user_id: str, vacation_name: str,
                           vacation_start_date: date = None, vacation_days: int = None,
                           transaction_id: str = None) -> Optional[str]:
        """Create a new vacation plan or update existing one."""
        try:
            # Check if plan with same name exists for user
            existing_plan = self.get_user_vacation_plan_by_name(user_id, vacation_name)
            if existing_plan:
                logger.info(f"Updating existing vacation plan for user {user_id}: {vacation_name}")
                # Update the existing vacation plan instead of creating a new one
                plan_id = existing_plan['id']
                
                update_data = {
                    'vacation_start_date': vacation_start_date,
                    'vacation_days': vacation_days,
                    'updated_transaction_id': transaction_id or str(uuid.uuid4())
                }
                
                # Remove None values to avoid overwriting existing data with None
                update_data = {k: v for k, v in update_data.items() if v is not None}
                
                success = self.update(plan_id, update_data)
                if success:
                    logger.info(f"Updated existing vacation plan {plan_id} for user {user_id}")
                    return plan_id
                else:
                    logger.error(f"Failed to update existing vacation plan {plan_id} for user {user_id}")
                    return None
            
            # Create new vacation plan if none exists
            plan_data = {
                'user_id': user_id,
                'vacation_name': vacation_name,
                'vacation_start_date': vacation_start_date,
                'vacation_days': vacation_days,
                'is_confirmed': False,
                'created_transaction_id': transaction_id or str(uuid.uuid4())
            }
            
            plan_id = self.create(plan_data)
            if plan_id:
                logger.info(f"Vacation plan created for user {user_id}: {vacation_name}")
            return plan_id
            
        except Exception as e:
            logger.error(f"Error creating vacation plan for user {user_id}: {e}")
            return None
    
    def get_user_vacation_plans(self, user_id: str, confirmed_only: bool = False) -> List[Dict[str, Any]]:
        """Get vacation plans for a user."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get vacation plans for user {user_id}")
            return []
            
        try:
            # Use simple query strategy to avoid complex index requirements
            # Query by user_id only with ordering, then filter in Python
            try:
                # Use simple query with user_id and created_at ordering
                # This should work with a basic (user_id, created_at) index
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .order_by('created_at', direction=firestore.Query.DESCENDING))
                
                docs = list(query.stream())
                logger.info(f"Successfully retrieved {len(docs)} vacation plans with ordering for user {user_id}")
                
            except Exception as index_error:
                if "requires an index" in str(index_error):
                    logger.warning(f"Ordered vacation plans query failed (missing index), falling back to simple query: {index_error}")
                else:
                    logger.error(f"Unexpected error in vacation plans query: {index_error}")
                
                # Fallback to simple query without ordering
                query = self.collection.where(filter=firestore.FieldFilter('user_id', '==', user_id))
                docs = list(query.stream())
                logger.info(f"Retrieved {len(docs)} vacation plans without ordering for user {user_id}")
            
            # Process results and apply confirmed_only filter in Python
            results = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                
                # Apply confirmed_only filter in Python
                if confirmed_only and not data.get('is_confirmed', False):
                    continue
                    
                results.append(data)
            
            # Sort in Python if we couldn't sort in Firestore
            if results and 'created_at' in results[0]:
                results.sort(key=lambda x: x.get('created_at', datetime.min), reverse=True)
            
            logger.info(f"Returning {len(results)} vacation plans for user {user_id} (confirmed_only={confirmed_only})")
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving vacation plans for user {user_id}: {e}")
            return []
    
    def get_user_vacation_plan_by_name(self, user_id: str, vacation_name: str) -> Optional[Dict[str, Any]]:
        """Get vacation plan by user ID and name."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get vacation plan {vacation_name} for user {user_id}")
            return None
            
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                    .where(filter=firestore.FieldFilter('vacation_name', '==', vacation_name)))
            
            docs = list(query.stream())
            
            if docs:
                doc = docs[0]
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving vacation plan {vacation_name} for user {user_id}: {e}")
            return None
    
    def confirm_vacation_plan(self, plan_id: str, transaction_id: str = None) -> bool:
        """Confirm a vacation plan."""
        try:
            update_data = {
                'is_confirmed': True,
                'updated_transaction_id': transaction_id or str(uuid.uuid4())
            }
            
            success = self.update(plan_id, update_data)
            if success:
                logger.info(f"Vacation plan confirmed: {plan_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error confirming vacation plan {plan_id}: {e}")
            return False

class VacationEventRepository(FirestoreBaseRepository):
    """Repository for vacation event-related Firestore operations."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['vacation_events'], VacationEventModel)
    
    def add_vacation_event(self, vacation_plan_id: str, event_sequence: int, 
                          event_name: str, event_start_time: datetime = None,
                          event_duration: float = None, event_cost: float = None) -> Optional[str]:
        """Add a new event to a vacation plan."""
        try:
            # Check if event sequence already exists for this plan
            existing_event = self.get_event_by_sequence(vacation_plan_id, event_sequence)
            if existing_event:
                logger.warning(f"Event sequence {event_sequence} already exists for plan {vacation_plan_id}")
                return None
            
            event_data = {
                'vacation_plan_id': vacation_plan_id,
                'event_sequence': event_sequence,
                'event_name': event_name,
                'event_start_time': event_start_time,
                'event_duration': event_duration,
                'event_cost': event_cost
            }
            
            event_id = self.create(event_data)
            if event_id:
                logger.info(f"Vacation event added to plan {vacation_plan_id}: {event_name}")
            return event_id
            
        except Exception as e:
            logger.error(f"Error adding vacation event to plan {vacation_plan_id}: {e}")
            return None
    
    def get_vacation_events(self, vacation_plan_id: str) -> List[Dict[str, Any]]:
        """Get all events for a vacation plan, ordered by sequence."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get vacation events for plan {vacation_plan_id}")
            return []
            
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('vacation_plan_id', '==', vacation_plan_id))
                    .order_by('event_sequence', direction=firestore.Query.ASCENDING))
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving vacation events for plan {vacation_plan_id}: {e}")
            return []
    
    def get_event_by_sequence(self, vacation_plan_id: str, event_sequence: int) -> Optional[Dict[str, Any]]:
        """Get event by vacation plan ID and sequence number."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get event sequence {event_sequence} for plan {vacation_plan_id}")
            return None
            
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('vacation_plan_id', '==', vacation_plan_id))
                    .where(filter=firestore.FieldFilter('event_sequence', '==', event_sequence)))
            
            docs = list(query.stream())
            
            if docs:
                doc = docs[0]
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving event sequence {event_sequence} for plan {vacation_plan_id}: {e}")
            return None

class MediaFileRepository(FirestoreBaseRepository):
    """Repository for media file-related Firestore operations."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['media_files'], MediaFileModel)
    
    def create_media_file(self, user_id: str, file_data: Dict[str, Any]) -> Optional[str]:
        """Create a new media file record."""
        try:
            media_data = {
                'user_id': user_id,
                **file_data
            }
            
            media_id = self.create(media_data)
            if media_id:
                logger.info(f"Media file record created for user {user_id}: {file_data.get('file_name')}")
            return media_id
            
        except Exception as e:
            logger.error(f"Error creating media file record for user {user_id}: {e}")
            return None
    
    def get_user_media_files(self, user_id: str, file_type: str = None,
                           vacation_plan_id: str = None) -> List[Dict[str, Any]]:
        """Get media files for a user with optional filtering."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get media files for user {user_id}")
            return []
            
        try:
            query = self.collection.where(filter=firestore.FieldFilter('user_id', '==', user_id))
            
            if file_type:
                query = query.where(filter=firestore.FieldFilter('file_type', '==', file_type))
            
            if vacation_plan_id:
                query = query.where(filter=firestore.FieldFilter('vacation_plan_id', '==', vacation_plan_id))
            
            query = query.order_by('created_at', direction=firestore.Query.DESCENDING)
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving media files for user {user_id}: {e}")
            return []

class BlogPostRepository(FirestoreBaseRepository):
    """Repository for blog post-related Firestore operations."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['blog_posts'], BlogPostModel)
    
    def create_blog_post(self, user_id: str, title: str, content: str, 
                        vacation_plan_id: str = None, media_file_ids: List[str] = None) -> Optional[str]:
        """Create a new blog post."""
        try:
            post_data = {
                'user_id': user_id,
                'title': title,
                'content': content,
                'vacation_plan_id': vacation_plan_id,
                'media_file_ids': media_file_ids or [],
                'is_published': False,
                'is_public': False
            }
            
            post_id = self.create(post_data)
            if post_id:
                logger.info(f"Blog post created for user {user_id}: {title}")
            return post_id
            
        except Exception as e:
            logger.error(f"Error creating blog post for user {user_id}: {e}")
            return None
    
    def get_user_blog_posts(self, user_id: str, published_only: bool = False) -> List[Dict[str, Any]]:
        """Get blog posts for a user."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get blog posts for user {user_id}")
            return []
            
        try:
            query = self.collection.where(filter=firestore.FieldFilter('user_id', '==', user_id))
            
            if published_only:
                query = query.where(filter=firestore.FieldFilter('is_published', '==', True))
            
            query = query.order_by('created_at', direction=firestore.Query.DESCENDING)
            
            docs = query.stream()
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving blog posts for user {user_id}: {e}")
            return []
    
    def publish_blog_post(self, post_id: str, is_public: bool = False) -> bool:
        """Publish a blog post."""
        try:
            update_data = {
                'is_published': True,
                'is_public': is_public,
                'published_at': firestore.SERVER_TIMESTAMP
            }
            
            success = self.update(post_id, update_data)
            if success:
                logger.info(f"Blog post published: {post_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error publishing blog post {post_id}: {e}")
            return False

class ConfirmedItineraryRepository(FirestoreBaseRepository):
    """Repository for confirmed itinerary-related Firestore operations."""
    
    def __init__(self):
        from .firestore_models import ConfirmedItineraryModel
        super().__init__(COLLECTIONS['itineraries'], ConfirmedItineraryModel)
    
    def _serialize_maps_for_firestore(self, maps_data: Any) -> List[Dict[str, Any]]:
        """
        Serialize MapInfo objects for Firestore storage.
        
        Args:
            maps_data: List of MapInfo objects or dictionaries
            
        Returns:
            List of serialized dictionaries safe for Firestore
        """
        if not maps_data:
            return []
        
        serialized_maps = []
        for map_item in maps_data:
            try:
                # Check if it's already a dictionary
                if isinstance(map_item, dict):
                    serialized_maps.append(map_item)
                # Check if it's a MapInfo object with to_dict method
                elif hasattr(map_item, 'to_dict') and callable(getattr(map_item, 'to_dict')):
                    serialized_maps.append(map_item.to_dict())
                # Try to convert using dict() if it's a Pydantic model
                elif hasattr(map_item, 'dict') and callable(getattr(map_item, 'dict')):
                    serialized_maps.append(map_item.dict())
                else:
                    # Fallback: try to convert to dict directly
                    serialized_maps.append(dict(map_item))
                    
            except Exception as e:
                logger.warning(f"Failed to serialize map item: {e}, skipping item")
                continue
        
        logger.info(f"Serialized {len(serialized_maps)} maps for Firestore storage")
        return serialized_maps
    
    def _serialize_daily_routes_for_firestore(self, daily_routes_data: Any) -> List[Dict[str, Any]]:
        """
        Serialize daily routes data for Firestore storage.
        
        Args:
            daily_routes_data: List of daily route objects or dictionaries
            
        Returns:
            List of serialized dictionaries safe for Firestore
        """
        if not daily_routes_data:
            return []
        
        serialized_routes = []
        for route_item in daily_routes_data:
            try:
                # Check if it's already a dictionary
                if isinstance(route_item, dict):
                    serialized_routes.append(route_item)
                # Check if it has to_dict method
                elif hasattr(route_item, 'to_dict') and callable(getattr(route_item, 'to_dict')):
                    serialized_routes.append(route_item.to_dict())
                # Try to convert using dict() if it's a Pydantic model
                elif hasattr(route_item, 'dict') and callable(getattr(route_item, 'dict')):
                    serialized_routes.append(route_item.dict())
                else:
                    # Fallback: try to convert to dict directly
                    serialized_routes.append(dict(route_item))
                    
            except Exception as e:
                logger.warning(f"Failed to serialize daily route item: {e}, skipping item")
                continue
        
        logger.info(f"Serialized {len(serialized_routes)} daily routes for Firestore storage")
        return serialized_routes
    
    def update(self, doc_id: str, data: Dict[str, Any]) -> bool:
        """Update document by ID with proper map serialization."""
        # Make a copy of data to avoid modifying the original
        update_data = data.copy()
        
        # Serialize maps and daily_routes if they exist in the update data
        if 'maps' in update_data:
            update_data['maps'] = self._serialize_maps_for_firestore(update_data['maps'])
        
        if 'daily_routes' in update_data:
            update_data['daily_routes'] = self._serialize_daily_routes_for_firestore(update_data['daily_routes'])
        
        # Call the parent update method with serialized data
        return super().update(doc_id, update_data)
    
    def create_itinerary(self, user_id: str, vacation_plan_id: str,
                        itinerary_data: Dict[str, Any], is_confirmed: bool = False,
                        transaction_id: str = None) -> Optional[str]:
        """
        Create a new itinerary (draft or confirmed).
        
        Args:
            user_id: The user ID
            vacation_plan_id: The vacation plan ID
            itinerary_data: Dictionary containing itinerary data
            is_confirmed: True for confirmed itinerary, False for draft
            transaction_id: Optional transaction ID
            
        Returns:
            str: Document ID or None if creation failed
        """
        if not self.collection:
            logger.warning(f"Firestore not available, cannot create itinerary")
            return None
            
        try:
            # Handle existing itineraries based on type
            if is_confirmed:
                # For confirmed itineraries, replace any existing confirmed itinerary for this vacation plan
                self._replace_existing_confirmed_itinerary(user_id, vacation_plan_id)
            else:
                # For draft itineraries, check if we should update existing draft or create new one
                existing_draft_id = self._get_existing_draft_itinerary_id(user_id, vacation_plan_id)
                if existing_draft_id:
                    # Update existing draft instead of creating new one
                    return self._update_existing_draft_itinerary(existing_draft_id, itinerary_doc_data)
            
            # Make a copy of itinerary_data to avoid modifying the original
            itinerary_doc_data = itinerary_data.copy()
            
            # Serialize maps and daily_routes if they exist
            if 'maps' in itinerary_doc_data:
                itinerary_doc_data['maps'] = self._serialize_maps_for_firestore(itinerary_doc_data['maps'])
            
            if 'daily_routes' in itinerary_doc_data:
                itinerary_doc_data['daily_routes'] = self._serialize_daily_routes_for_firestore(itinerary_doc_data['daily_routes'])
            
            # Add required fields
            itinerary_doc_data.update({
                'user_id': user_id,
                'vacation_plan_id': vacation_plan_id,
                'is_confirmed': is_confirmed,
                'created_transaction_id': transaction_id or str(uuid.uuid4())
            })
            
            # Add confirmed_at timestamp only for confirmed itineraries
            if is_confirmed:
                itinerary_doc_data['confirmed_at'] = firestore.SERVER_TIMESTAMP
            
            # Use the base class create method which handles created_at and updated_at timestamps
            doc_id = self.create(itinerary_doc_data)
            if doc_id:
                itinerary_type = "confirmed" if is_confirmed else "draft"
                logger.info(f"{itinerary_type.capitalize()} itinerary created with ID: {doc_id} for vacation plan {vacation_plan_id}")
            return doc_id
            
        except Exception as e:
            logger.error(f"Error creating itinerary for vacation plan {vacation_plan_id}: {e}")
            return None

    def create_confirmed_itinerary(self, user_id: str, vacation_plan_id: str,
                                 itinerary_data: Dict[str, Any],
                                 transaction_id: str = None) -> Optional[str]:
        """
        Create a confirmed itinerary (wrapper for backward compatibility).
        """
        return self.create_itinerary(user_id, vacation_plan_id, itinerary_data,
                                   is_confirmed=True, transaction_id=transaction_id)

    def create_draft_itinerary(self, user_id: str, vacation_plan_id: str,
                             itinerary_data: Dict[str, Any],
                             transaction_id: str = None) -> Optional[str]:
        """
        Create a draft itinerary (wrapper for convenience).
        """
        return self.create_itinerary(user_id, vacation_plan_id, itinerary_data,
                                   is_confirmed=False, transaction_id=transaction_id)
    
    def _get_existing_draft_itinerary_id(self, user_id: str, vacation_plan_id: str) -> Optional[str]:
        """Get the ID of an existing draft itinerary for the same vacation plan."""
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                    .where(filter=firestore.FieldFilter('is_confirmed', '==', False))
                    .where(filter=firestore.FieldFilter('status', '==', 'draft'))
                    .where(filter=firestore.FieldFilter('vacation_plan_id', '==', vacation_plan_id))
                    .limit(1))
            
            docs = list(query.stream())
            
            if docs:
                return docs[0].id
            return None
            
        except Exception as e:
            logger.error(f"Error finding existing draft itinerary for user {user_id}, plan {vacation_plan_id}: {e}")
            return None

    def _update_existing_draft_itinerary(self, itinerary_id: str, itinerary_data: Dict[str, Any]) -> Optional[str]:
        """Update an existing draft itinerary with new data."""
        try:
            # Remove fields that shouldn't be updated
            update_data = itinerary_data.copy()
            update_data.pop('created_at', None)
            update_data.pop('created_transaction_id', None)
            
            success = self.update(itinerary_id, update_data)
            if success:
                logger.info(f"Updated existing draft itinerary {itinerary_id}")
                return itinerary_id
            return None
            
        except Exception as e:
            logger.error(f"Error updating existing draft itinerary {itinerary_id}: {e}")
            return None

    def _replace_existing_confirmed_itinerary(self, user_id: str, vacation_plan_id: str) -> bool:
        """Replace any existing confirmed itinerary for the given vacation plan by deleting it."""
        try:
            # Query for existing confirmed itineraries for this vacation plan
            query = (self.collection
                    .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                    .where(filter=firestore.FieldFilter('is_confirmed', '==', True))
                    .where(filter=firestore.FieldFilter('vacation_plan_id', '==', vacation_plan_id)))
            
            docs = list(query.stream())
            
            for doc in docs:
                # Delete the old confirmed itinerary
                doc.reference.delete()
                logger.info(f"Deleted previous confirmed itinerary {doc.id} for vacation plan {vacation_plan_id}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error replacing existing confirmed itinerary for vacation plan {vacation_plan_id}: {e}")
            return False
    
    def get_confirmed_itinerary_by_vacation_plan(self, user_id: str, vacation_plan_id: str) -> Optional[Dict[str, Any]]:
        """Get the confirmed itinerary for a specific vacation plan."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get confirmed itinerary for vacation plan {vacation_plan_id}")
            return None
            
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                    .where(filter=firestore.FieldFilter('is_confirmed', '==', True))
                    .where(filter=firestore.FieldFilter('vacation_plan_id', '==', vacation_plan_id))
                    .limit(1))
            
            docs = list(query.stream())
            
            if docs:
                doc = docs[0]
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving confirmed itinerary for vacation plan {vacation_plan_id}: {e}")
            return None
    
    def get_user_confirmed_itineraries(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get all confirmed itineraries for a user, ordered by confirmation date."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get confirmed itineraries for user {user_id}")
            return []
            
        try:
            logger.info(f"Querying confirmed itineraries for user {user_id} with limit {limit}")
            
            # First, let's see ALL itineraries for this user for debugging
            debug_query = self.collection.where(filter=firestore.FieldFilter('user_id', '==', user_id)).limit(20)
            all_docs = list(debug_query.stream())
            logger.info(f"DEBUG: Found {len(all_docs)} total itineraries for user {user_id}")
            for doc in all_docs:
                data = doc.to_dict()
                logger.info(f"DEBUG: Itinerary {doc.id}: status={data.get('status')}, is_confirmed={data.get('is_confirmed')}, created_at={data.get('created_at')}")
            
            # Try ordered query first - use simple query and filter in Python to avoid complex indexing
            try:
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .where(filter=firestore.FieldFilter('is_confirmed', '==', True))
                        .where(filter=firestore.FieldFilter('status', '==', 'confirmed'))
                        .order_by('confirmed_at', direction=firestore.Query.DESCENDING)
                        .limit(limit * 2))  # Get more to account for filtering
                
                docs = list(query.stream())
                logger.info(f"Successfully retrieved {len(docs)} confirmed itineraries with ordering for user {user_id}")
                
                # Apply limit directly since we no longer have revoked status
                docs = docs[:limit]
                logger.info(f"Retrieved {len(docs)} confirmed itineraries for user {user_id}")
                
            except Exception as index_error:
                if "requires an index" in str(index_error):
                    logger.warning(f"Ordered query failed (missing index), falling back to simple query: {index_error}")
                else:
                    logger.error(f"Unexpected error in ordered query: {index_error}")
                
                # Fallback to simple query without ordering - exclude revoked itineraries
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .where(filter=firestore.FieldFilter('is_confirmed', '==', True))
                        .limit(limit * 2))  # Get more to account for filtering
                
                docs = list(query.stream())
                logger.info(f"Retrieved {len(docs)} confirmed itineraries without ordering for user {user_id}")
                
                # Apply limit directly since we no longer have revoked status
                docs = docs[:limit]
                logger.info(f"Retrieved {len(docs)} confirmed itineraries for user {user_id}")
            
            # If no results with is_confirmed=True, try with status="confirmed" as fallback (excluding revoked)
            if not docs:
                logger.info(f"No results with is_confirmed=True, trying status='confirmed' for user {user_id}")
                try:
                    query = (self.collection
                            .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                            .where(filter=firestore.FieldFilter('is_confirmed', '==', True))
                            .where(filter=firestore.FieldFilter('status', '==', 'confirmed'))
                            .limit(limit))
                    
                    docs = list(query.stream())
                    logger.info(f"Retrieved {len(docs)} itineraries with status='confirmed' for user {user_id}")
                    
                    # No need to filter since we only have draft and confirmed statuses
                    logger.info(f"Retrieved {len(docs)} confirmed status itineraries for user {user_id}")
                except Exception as e:
                    logger.warning(f"Fallback query with status='confirmed' failed: {e}")
            
            results = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                logger.info(f"Retrieved itinerary {doc.id} with keys: {list(data.keys())}")
                if 'markdown_content' in data:
                    logger.info(f"Itinerary {doc.id} markdown_content length: {len(data['markdown_content'])}")
                else:
                    logger.warning(f"Itinerary {doc.id} missing markdown_content field")
                results.append(data)
            
            # Sort in Python if we couldn't sort in Firestore
            if results and 'confirmed_at' in results[0]:
                results.sort(key=lambda x: x.get('confirmed_at', datetime.min), reverse=True)
            elif results and 'created_at' in results[0]:
                # Fallback to created_at if confirmed_at is not available
                results.sort(key=lambda x: x.get('created_at', datetime.min), reverse=True)
            
            logger.info(f"Returning {len(results)} confirmed itineraries for user {user_id}")
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving confirmed itineraries for user {user_id}: {e}")
            return []
    
    def get_most_recent_itinerary(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent itinerary for a user (confirmed or draft)."""
        logger.info(f"Retrieving most recent itinerary for user {user_id}")
        
        # First try to get confirmed itineraries
        confirmed_itineraries = self.get_user_confirmed_itineraries(user_id, limit=1)
        
        if confirmed_itineraries:
            itinerary = confirmed_itineraries[0]
            logger.info(f"Found confirmed itinerary {itinerary.get('id')} with keys: {list(itinerary.keys())}")
            logger.info(f"Itinerary status: is_confirmed={itinerary.get('is_confirmed')}, status={itinerary.get('status')}")
            logger.info(f"Markdown content length: {len(itinerary.get('markdown_content', ''))}")
            return itinerary
        
        # If no confirmed itineraries found, try to get any recent itinerary (including drafts)
        logger.info(f"No confirmed itineraries found, searching for any recent itinerary for user {user_id}")
        
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get any itinerary for user {user_id}")
            return None
            
        try:
            # First, let's see ALL itineraries for this user for debugging
            debug_query = self.collection.where(filter=firestore.FieldFilter('user_id', '==', user_id)).limit(20)
            all_docs = list(debug_query.stream())
            logger.info(f"DEBUG: Found {len(all_docs)} total itineraries for user {user_id}")
            for doc in all_docs:
                data = doc.to_dict()
                logger.info(f"DEBUG: Itinerary {doc.id}: status={data.get('status')}, is_confirmed={data.get('is_confirmed')}, created_at={data.get('created_at')}")
            
            # Try ordered query first - use simple query and filter in Python to avoid complex indexing
            try:
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .order_by('created_at', direction=firestore.Query.DESCENDING)
                        .limit(10))  # Get more docs to filter in Python
                
                docs = list(query.stream())
                logger.info(f"Successfully retrieved {len(docs)} itineraries with ordering for user {user_id}")
                
                # No need to filter since we only have draft and confirmed statuses
                logger.info(f"Retrieved {len(docs)} itineraries for user {user_id}")
                
            except Exception as index_error:
                if "requires an index" in str(index_error):
                    logger.warning(f"Ordered query failed (missing index), falling back to simple query: {index_error}")
                else:
                    logger.error(f"Unexpected error in ordered query: {index_error}")
                
                # Fallback to simple query without ordering - exclude revoked itineraries
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .limit(10))  # Get more docs to filter and sort in Python
                
                docs = list(query.stream())
                logger.info(f"Retrieved {len(docs)} itineraries without ordering for user {user_id}")
                
                # No need to filter since we only have draft and confirmed statuses
                logger.info(f"Retrieved {len(docs)} itineraries for user {user_id}")
            
            if docs:
                # If we have multiple docs and couldn't sort in Firestore, sort in Python
                if len(docs) > 1:
                    docs.sort(key=lambda x: x.to_dict().get('created_at', datetime.min), reverse=True)
                
                doc = docs[0]
                data = doc.to_dict()
                data['id'] = doc.id
                logger.info(f"Found any itinerary {doc.id} with keys: {list(data.keys())}")
                logger.info(f"Itinerary status: is_confirmed={data.get('is_confirmed')}, status={data.get('status')}")
                logger.info(f"Markdown content length: {len(data.get('markdown_content', ''))}")
                return data
            else:
                logger.info(f"No itinerary found for user {user_id}")
                return None
                
        except Exception as e:
            logger.error(f"Error retrieving any itinerary for user {user_id}: {e}")
            return None

    def get_most_recent_confirmed_itinerary(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent confirmed itinerary for a user. DEPRECATED: Use get_most_recent_itinerary instead."""
        logger.warning("get_most_recent_confirmed_itinerary is deprecated. Use get_most_recent_itinerary instead.")
        return self.get_most_recent_itinerary(user_id)

class TermsAcceptanceRepository(FirestoreBaseRepository):
    """Repository for Terms and Services acceptance records."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['terms_acceptances'], TermsAcceptanceModel)
    
    def record_terms_acceptance(self, email: str, accepted: bool,
                              terms_version: str = "1.0", ip_address: str = None,
                              user_agent: str = None, transaction_id: str = None) -> Optional[str]:
        """
        Record a Terms and Services acceptance.
        This method is idempotent - it will only create a new record if the user
        hasn't already accepted the specified version of the terms.
        """
        try:
            email_lower = email.lower()
            
            # Check if user has already accepted this version of terms
            existing_acceptance = self.get_user_terms_acceptance(email_lower, terms_version)
            
            if existing_acceptance and existing_acceptance.get('accepted', False):
                logger.info(f"Terms acceptance already exists for {email} version {terms_version}: {existing_acceptance['id']}")
                return existing_acceptance['id']
            
            # Create new acceptance record only if none exists or previous was rejected
            acceptance_data = {
                'email': email_lower,
                'accepted': accepted,
                'terms_version': terms_version,
                'ip_address': ip_address,
                'user_agent': user_agent,
                'created_transaction_id': transaction_id or str(uuid.uuid4())
            }
            
            acceptance_id = self.create(acceptance_data)
            if acceptance_id:
                logger.info(f"New terms acceptance recorded for {email}: {accepted}")
            return acceptance_id
            
        except Exception as e:
            logger.error(f"Error recording terms acceptance for {email}: {e}")
            return None
    
    def get_user_terms_acceptance(self, email: str,
                                terms_version: str = None) -> Optional[Dict[str, Any]]:
        """Get the most recent Terms acceptance for a user."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get terms acceptance for {email}")
            return None
            
        try:
            query = self.collection.where(filter=firestore.FieldFilter('email', '==', email.lower()))
            
            if terms_version:
                query = query.where(filter=firestore.FieldFilter('terms_version', '==', terms_version))
            
            # Try to order by acceptance timestamp
            try:
                query = query.order_by('acceptance_timestamp', direction=firestore.Query.DESCENDING).limit(1)
                docs = list(query.stream())
            except Exception as index_error:
                if "requires an index" in str(index_error):
                    logger.warning(f"Ordered query failed, falling back to simple query: {index_error}")
                    # Fallback without ordering
                    query = self.collection.where(filter=firestore.FieldFilter('email', '==', email.lower()))
                    if terms_version:
                        query = query.where(filter=firestore.FieldFilter('terms_version', '==', terms_version))
                    docs = list(query.stream())
                else:
                    raise
            
            if docs:
                # If we have multiple docs and couldn't sort in Firestore, sort in Python
                if len(docs) > 1:
                    docs.sort(key=lambda x: x.to_dict().get('acceptance_timestamp', datetime.min), reverse=True)
                
                doc = docs[0]
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving terms acceptance for {email}: {e}")
            return None
    
    def has_accepted_terms(self, email: str, terms_version: str = "1.0") -> bool:
        """Check if user has accepted the specified version of Terms and Services."""
        try:
            acceptance = self.get_user_terms_acceptance(email, terms_version)
            return acceptance is not None and acceptance.get('accepted', False)
        except Exception as e:
            logger.error(f"Error checking terms acceptance for {email}: {e}")
            return False

class BookingPageRepository(FirestoreBaseRepository):
    """Repository for booking page-related Firestore operations."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['booking_pages'], None)  # We'll handle model validation manually
    
    def create_booking_page(self, user_id: str, itinerary_id: str,
                           booking_data: Dict[str, Any], transaction_id: str = None) -> Optional[str]:
        """Create a new booking page."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot create booking page")
            return None
            
        try:
            booking_page_data = {
                'user_id': user_id,
                'itinerary_id': itinerary_id,
                'created_transaction_id': transaction_id or str(uuid.uuid4()),
                **booking_data
            }
            
            doc_ref = self.collection.add(booking_page_data)[1]
            logger.info(f"Booking page created with ID: {doc_ref.id} for itinerary {itinerary_id}")
            return doc_ref.id
            
        except Exception as e:
            logger.error(f"Error creating booking page for itinerary {itinerary_id}: {e}")
            return None
    
    def get_booking_page_by_itinerary(self, user_id: str, itinerary_id: str) -> Optional[Dict[str, Any]]:
        """Get booking page by user ID and itinerary ID."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get booking page for itinerary {itinerary_id}")
            return None
            
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                    .where(filter=firestore.FieldFilter('itinerary_id', '==', itinerary_id))
                    .limit(1))
            
            docs = list(query.stream())
            
            if docs:
                doc = docs[0]
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving booking page for itinerary {itinerary_id}: {e}")
            return None
    
    def get_most_recent_booking_page(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent booking page for a user."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get recent booking page for user {user_id}")
            return None
            
        try:
            # Try ordered query first
            try:
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .order_by('created_at', direction=firestore.Query.DESCENDING)
                        .limit(1))
                
                docs = list(query.stream())
                logger.info(f"Successfully retrieved {len(docs)} booking pages with ordering for user {user_id}")
                
            except Exception as index_error:
                if "requires an index" in str(index_error):
                    logger.warning(f"Ordered query failed (missing index), falling back to simple query: {index_error}")
                else:
                    logger.error(f"Unexpected error in ordered query: {index_error}")
                
                # Fallback to simple query without ordering
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .limit(10))  # Get more docs to sort in Python
                
                docs = list(query.stream())
                logger.info(f"Retrieved {len(docs)} booking pages without ordering for user {user_id}")
            
            if docs:
                # If we have multiple docs and couldn't sort in Firestore, sort in Python
                if len(docs) > 1:
                    docs.sort(key=lambda x: x.to_dict().get('created_at', datetime.min), reverse=True)
                
                doc = docs[0]
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving most recent booking page for user {user_id}: {e}")
            return None

class WaitlistRepository(FirestoreBaseRepository):
    """Repository for waitlist-related Firestore operations."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['wait_lists'], None)  # We'll handle model validation manually
    
    def create_waitlist_entry(self, waitlist_data: Dict[str, Any]) -> Optional[str]:
        """Create a new waitlist entry."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot create waitlist entry")
            return None
            
        try:
            # Add timestamps
            waitlist_data['created_at'] = firestore.SERVER_TIMESTAMP
            waitlist_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            doc_ref = self.collection.add(waitlist_data)[1]
            logger.info(f"Waitlist entry created with ID: {doc_ref.id}")
            return doc_ref.id
            
        except Exception as e:
            logger.error(f"Error creating waitlist entry: {e}")
            return None
    
    def get_waitlist_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get waitlist entry by email address."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get waitlist entry for {email}")
            return None
            
        try:
            query = self.collection.where(filter=firestore.FieldFilter('email', '==', email.lower()))
            docs = list(query.stream())
            
            if docs:
                doc = docs[0]
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving waitlist entry for {email}: {e}")
            return None
    
    def approve_waitlist_user(self, waitlist_id: str, transaction_id: str = None) -> bool:
        """Approve a waitlist user."""
        try:
            update_data = {
                'approved': True,
                'approved_at': firestore.SERVER_TIMESTAMP,
                'updated_transaction_id': transaction_id or str(uuid.uuid4())
            }
            
            success = self.update(waitlist_id, update_data)
            if success:
                logger.info(f"Waitlist user approved: {waitlist_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error approving waitlist user {waitlist_id}: {e}")
            return False
    
    def get_all_waitlist_entries(self, approved_only: bool = False, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all waitlist entries with optional filtering."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get waitlist entries")
            return []
            
        try:
            query = self.collection
            
            if approved_only:
                query = query.where(filter=firestore.FieldFilter('approved', '==', True))
            
            # Try to order by created_at
            try:
                query = query.order_by('created_at', direction=firestore.Query.DESCENDING)
                if limit:
                    query = query.limit(limit)
                
                docs = list(query.stream())
                logger.info(f"Successfully retrieved {len(docs)} waitlist entries with ordering")
                
            except Exception as index_error:
                if "requires an index" in str(index_error):
                    logger.warning(f"Ordered query failed (missing index), falling back to simple query: {index_error}")
                else:
                    logger.error(f"Unexpected error in ordered query: {index_error}")
                
                # Fallback to simple query without ordering
                if limit:
                    query = query.limit(limit)
                
                docs = list(query.stream())
                logger.info(f"Retrieved {len(docs)} waitlist entries without ordering")
            
            results = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            # Sort in Python if we couldn't sort in Firestore
            if results and 'created_at' in results[0]:
                results.sort(key=lambda x: x.get('created_at', datetime.min), reverse=True)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving waitlist entries: {e}")
            return []
    
    def create_feedback_entry(self, feedback_data: Dict[str, Any]) -> Optional[str]:
        """Create a new feedback entry."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot create feedback entry")
            return None
            
        try:
            # Add timestamps
            feedback_data['created_at'] = firestore.SERVER_TIMESTAMP
            feedback_data['updated_at'] = firestore.SERVER_TIMESTAMP
            
            doc_ref = self.collection.add(feedback_data)[1]
            logger.info(f"Feedback entry created with ID: {doc_ref.id}")
            return doc_ref.id
            
        except Exception as e:
            logger.error(f"Error creating feedback entry: {e}")
            return None
    
    def create_user_feedback_entry(self, feedback_data: Dict[str, Any], user_token: str, user_email: str = None) -> Optional[str]:
        """Create a new feedback entry tied to a specific waitlist user."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot create user feedback entry")
            return None
            
        try:
            # Use provided user_email if available, otherwise try to decode from token
            if user_email:
                extracted_email = user_email.lower()
            else:
                # Decode user token to get user email (simplified - in production use proper JWT decoding)
                try:
                    import base64
                    import json
                    # This is a simplified token decoding - in production use proper JWT library
                    decoded_token = base64.b64decode(user_token + '==').decode('utf-8')
                    token_data = json.loads(decoded_token)
                    extracted_email = token_data.get('email', '').lower()
                except:
                    # Fallback: assume token is the email itself (for testing)
                    extracted_email = user_token.lower()
            
            if not extracted_email:
                logger.error("Could not extract user email from token or parameter")
                return None
            
            # Find the waitlist entry for this user
            waitlist_entry = self.get_waitlist_by_email(extracted_email)
            if not waitlist_entry:
                logger.error(f"No waitlist entry found for user email: {extracted_email}")
                return None
            
            waitlist_id = waitlist_entry['id']
            
            # Create feedback entry in a separate "feedbacks" collection
            from database.firestore_config import get_firestore_client
            from database.firestore_models import COLLECTIONS
            
            db = get_firestore_client()
            if not db:
                logger.error("Firestore client not available")
                return None
            
            # Create feedbacks collection if it doesn't exist
            feedbacks_collection = db.collection('feedbacks')
            
            # Prepare feedback document
            feedback_doc = {
                'waitlist_id': waitlist_id,
                'user_email': extracted_email,
                'comments': feedback_data['comments'],
                'would_recommend': feedback_data['would_recommend'],
                'submitted_at': feedback_data['submitted_at'],
                'created_transaction_id': feedback_data['created_transaction_id'],
                'resolved': feedback_data.get('resolved', False),
                'resolved_at': feedback_data.get('resolved_at', None),
                'created_at': firestore.SERVER_TIMESTAMP,
                'updated_at': firestore.SERVER_TIMESTAMP
            }
            
            # Add the feedback document
            doc_ref = feedbacks_collection.add(feedback_doc)[1]
            feedback_id = doc_ref.id
            
            logger.info(f"User feedback entry created with ID: {feedback_id} for waitlist ID: {waitlist_id}")
            return feedback_id
            
        except Exception as e:
            logger.error(f"Error creating user feedback entry: {e}")
            return None
    
    def get_user_feedbacks(self, user_email: str) -> List[Dict[str, Any]]:
        """Get all feedback entries for a specific user."""
        try:
            from database.firestore_config import get_firestore_client
            
            db = get_firestore_client()
            if not db:
                logger.warning("Firestore client not available")
                return []
            
            feedbacks_collection = db.collection('feedbacks')
            
            # Query feedbacks by user email
            query = feedbacks_collection.where(filter=firestore.FieldFilter('user_email', '==', user_email.lower()))
            docs = list(query.stream())
            
            results = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving user feedbacks for {user_email}: {e}")
            return []
    
    def mark_feedback_resolved(self, feedback_id: str) -> bool:
        """Mark a feedback entry as resolved."""
        try:
            from database.firestore_config import get_firestore_client
            
            db = get_firestore_client()
            if not db:
                logger.warning("Firestore client not available")
                return False
            
            feedbacks_collection = db.collection('feedbacks')
            feedback_doc = feedbacks_collection.document(feedback_id)
            
            feedback_doc.update({
                'resolved': True,
                'resolved_at': firestore.SERVER_TIMESTAMP,
                'updated_at': firestore.SERVER_TIMESTAMP
            })
            
            logger.info(f"Feedback {feedback_id} marked as resolved")
            return True
            
        except Exception as e:
            logger.error(f"Error marking feedback {feedback_id} as resolved: {e}")
            return False

class FlightSearchRepository(FirestoreBaseRepository):
    """Repository for flight search-related Firestore operations."""
    
    def __init__(self):
        super().__init__(COLLECTIONS['flight_searches'], FlightSearchModel)
    
    def create_flight_search(self, user_id: str, search_data: Dict[str, Any],
                           transaction_id: str = None) -> Optional[str]:
        """Create a new flight search record."""
        try:
            flight_search_data = {
                'user_id': user_id,
                'created_transaction_id': transaction_id or str(uuid.uuid4()),
                **search_data
            }
            
            search_id = self.create(flight_search_data)
            if search_id:
                logger.info(f"Flight search record created for user {user_id}: {search_data.get('origin_airport')} -> {search_data.get('destination_airport')}")
            return search_id
            
        except Exception as e:
            logger.error(f"Error creating flight search record for user {user_id}: {e}")
            return None
    
    def get_user_flight_searches(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get flight searches for a user, ordered by search date."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get flight searches for user {user_id}")
            return []
            
        try:
            # Try ordered query first
            try:
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .order_by('search_date', direction=firestore.Query.DESCENDING)
                        .limit(limit))
                
                docs = list(query.stream())
                logger.info(f"Successfully retrieved {len(docs)} flight searches with ordering for user {user_id}")
                
            except Exception as index_error:
                if "requires an index" in str(index_error):
                    logger.warning(f"Ordered flight search query failed (missing index), falling back to simple query: {index_error}")
                else:
                    logger.error(f"Unexpected error in flight search query: {index_error}")
                
                # Fallback to simple query without ordering
                query = (self.collection
                        .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                        .limit(limit))
                
                docs = list(query.stream())
                logger.info(f"Retrieved {len(docs)} flight searches without ordering for user {user_id}")
            
            results = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            # Sort in Python if we couldn't sort in Firestore
            if results and 'search_date' in results[0]:
                results.sort(key=lambda x: x.get('search_date', datetime.min), reverse=True)
            elif results and 'created_at' in results[0]:
                # Fallback to created_at if search_date is not available
                results.sort(key=lambda x: x.get('created_at', datetime.min), reverse=True)
            
            logger.info(f"Returning {len(results)} flight searches for user {user_id}")
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving flight searches for user {user_id}: {e}")
            return []
    
    def get_flight_search_by_id(self, search_id: str, user_id: str = None) -> Optional[Dict[str, Any]]:
        """Get flight search by ID, optionally filtered by user."""
        try:
            search_data = self.get_by_id(search_id)
            
            # If user_id is provided, verify ownership
            if user_id and search_data and search_data.get('user_id') != user_id:
                logger.warning(f"Flight search {search_id} does not belong to user {user_id}")
                return None
                
            return search_data
            
        except Exception as e:
            logger.error(f"Error retrieving flight search {search_id}: {e}")
            return None
    
    def update_flight_search_results(self, search_id: str, flight_results: List[Dict[str, Any]],
                                   screenshot_url: str = None, transaction_id: str = None) -> bool:
        """Update flight search with results from AI vision extraction."""
        try:
            update_data = {
                'flight_results': flight_results,
                'results_count': len(flight_results),
                'status': 'completed',
                'updated_transaction_id': transaction_id or str(uuid.uuid4())
            }
            
            if screenshot_url:
                update_data['screenshot_url'] = screenshot_url
            
            success = self.update(search_id, update_data)
            if success:
                logger.info(f"Flight search results updated for search {search_id}: {len(flight_results)} flights found")
            return success
            
        except Exception as e:
            logger.error(f"Error updating flight search results for {search_id}: {e}")
            return False
    
    def mark_flight_search_failed(self, search_id: str, error_message: str = None,
                                transaction_id: str = None) -> bool:
        """Mark flight search as failed with optional error message."""
        try:
            update_data = {
                'status': 'failed',
                'updated_transaction_id': transaction_id or str(uuid.uuid4())
            }
            
            if error_message:
                update_data['error_message'] = error_message
            
            success = self.update(search_id, update_data)
            if success:
                logger.info(f"Flight search marked as failed: {search_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error marking flight search as failed for {search_id}: {e}")
            return False
    
    def get_flight_searches_by_route(self, user_id: str, departure_airport: str,
                                   arrival_airport: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get flight searches for a specific route."""
        if not self.collection:
            logger.warning(f"Firestore not available, cannot get flight searches by route for user {user_id}")
            return []
            
        try:
            query = (self.collection
                    .where(filter=firestore.FieldFilter('user_id', '==', user_id))
                    .where(filter=firestore.FieldFilter('departure_airport', '==', departure_airport))
                    .where(filter=firestore.FieldFilter('arrival_airport', '==', arrival_airport))
                    .limit(limit))
            
            docs = list(query.stream())
            results = []
            
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            
            # Sort by search date (most recent first)
            if results and 'search_date' in results[0]:
                results.sort(key=lambda x: x.get('search_date', datetime.min), reverse=True)
            
            logger.info(f"Retrieved {len(results)} flight searches for route {departure_airport} -> {arrival_airport}")
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving flight searches by route for user {user_id}: {e}")
            return []
    
    def delete_flight_search(self, search_id: str, user_id: str = None) -> bool:
        """Delete a flight search record."""
        try:
            # If user_id is provided, verify ownership first
            if user_id:
                search_data = self.get_by_id(search_id)
                if not search_data or search_data.get('user_id') != user_id:
                    logger.warning(f"Flight search {search_id} does not belong to user {user_id} or does not exist")
                    return False
            
            success = self.delete(search_id)
            if success:
                logger.info(f"Flight search deleted: {search_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error deleting flight search {search_id}: {e}")
            return False
