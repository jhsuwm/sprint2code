"""
Initialize Firestore collections for enhanced logging system
"""
import os
import sys
from datetime import datetime
from typing import Dict, Any

# Add the parent directory to the path so we can import from database
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.firestore_config import get_firestore_client
from database.collections_schema import GoogleAPICallSchema, LogMetricsSchema


def create_firestore_indexes():
    """
    Create required Firestore indexes for efficient querying
    
    Note: These indexes need to be created manually in the Firebase Console
    or using the Firebase CLI. This function documents the required indexes.
    """
    required_indexes = [
        {
            "collection": "google_api_calls",
            "fields": [
                {"field": "user_id", "order": "ASCENDING"},
                {"field": "timestamp", "order": "DESCENDING"}
            ],
            "description": "Query API calls by user ordered by timestamp"
        },
        {
            "collection": "google_api_calls", 
            "fields": [
                {"field": "api_type", "order": "ASCENDING"},
                {"field": "timestamp", "order": "DESCENDING"}
            ],
            "description": "Query API calls by type ordered by timestamp"
        },
        {
            "collection": "google_api_calls",
            "fields": [
                {"field": "user_id", "order": "ASCENDING"},
                {"field": "api_type", "order": "ASCENDING"},
                {"field": "timestamp", "order": "DESCENDING"}
            ],
            "description": "Query API calls by user and type ordered by timestamp"
        },
        {
            "collection": "google_api_calls",
            "fields": [
                {"field": "session_id", "order": "ASCENDING"},
                {"field": "timestamp", "order": "DESCENDING"}
            ],
            "description": "Query API calls by session ordered by timestamp"
        },
        {
            "collection": "log_metrics",
            "fields": [
                {"field": "user_id", "order": "ASCENDING"},
                {"field": "date", "order": "DESCENDING"}
            ],
            "description": "Query log metrics by user ordered by date"
        },
        {
            "collection": "log_metrics",
            "fields": [
                {"field": "metric_type", "order": "ASCENDING"},
                {"field": "date", "order": "DESCENDING"}
            ],
            "description": "Query log metrics by type ordered by date"
        }
    ]
    
    print("Required Firestore Indexes:")
    print("=" * 50)
    
    for index in required_indexes:
        print(f"\nCollection: {index['collection']}")
        print(f"Description: {index['description']}")
        print("Fields:")
        for field in index['fields']:
            print(f"  - {field['field']}: {field['order']}")
    
    print("\n" + "=" * 50)
    print("To create these indexes, use the Firebase CLI:")
    print("firebase deploy --only firestore:indexes")
    print("\nOr create them manually in the Firebase Console:")
    print("https://console.firebase.google.com/project/YOUR_PROJECT/firestore/indexes")


def initialize_collections():
    """
    Initialize Firestore collections with proper structure and validation
    """
    try:
        # Get Firestore client
        db = get_firestore_client()
        
        print("Initializing Firestore collections for enhanced logging...")
        
        # Test connection by creating a test document
        test_doc_ref = db.collection('system_status').document('logging_system')
        test_doc_ref.set({
            'initialized_at': datetime.utcnow(),
            'version': '1.0.0',
            'status': 'active',
            'collections': ['google_api_calls', 'log_metrics'],
            'features': [
                'user_context_tracking',
                'google_api_metrics',
                'cost_reporting',
                'session_tracking'
            ]
        })
        
        print("✅ System status document created successfully")
        
        # Create a sample google_api_calls document to establish collection structure
        sample_api_call = GoogleAPICallSchema.create_document(
            user_id="system_init",
            session_id="init_session",
            api_type="gemini",
            model_name="gemini-2.5-flash",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            response_time_ms=1500,
            success=True,
            cost_usd=0.001,
            metadata={
                "initialization": True,
                "purpose": "collection_structure_setup"
            }
        )
        
        # Add the sample document
        api_calls_ref = db.collection('google_api_calls').document('init_sample')
        api_calls_ref.set(sample_api_call)
        
        print("✅ google_api_calls collection structure created")
        
        # Create a sample log_metrics document to establish collection structure
        sample_log_metrics = LogMetricsSchema.create_daily_summary(
            user_id="system_init",
            date=datetime.utcnow().date(),
            total_api_calls=1,
            total_cost_usd=0.001,
            api_breakdown={
                "gemini": {"calls": 1, "cost": 0.001}
            },
            session_count=1,
            error_count=0
        )
        
        # Add the sample document
        log_metrics_ref = db.collection('log_metrics').document('init_sample_daily')
        log_metrics_ref.set(sample_log_metrics)
        
        print("✅ log_metrics collection structure created")
        
        # Clean up sample documents
        api_calls_ref.delete()
        log_metrics_ref.delete()
        
        print("✅ Sample documents cleaned up")
        print("\n🎉 Firestore collections initialized successfully!")
        
        # Display index requirements
        create_firestore_indexes()
        
        return True
        
    except Exception as e:
        print(f"❌ Error initializing Firestore collections: {e}")
        return False


def verify_collections():
    """
    Verify that collections are properly set up and accessible
    """
    try:
        db = get_firestore_client()
        
        print("Verifying Firestore collections...")
        
        # Test writing and reading from google_api_calls
        test_api_call = GoogleAPICallSchema.create_document(
            user_id="test_user",
            session_id="test_session",
            api_type="gemini",
            model_name="gemini-2.5-flash",
            prompt_tokens=5,
            completion_tokens=10,
            total_tokens=15,
            response_time_ms=1000,
            success=True,
            cost_usd=0.0005
        )
        
        # Write test document
        test_ref = db.collection('google_api_calls').document('verification_test')
        test_ref.set(test_api_call)
        
        # Read it back
        doc = test_ref.get()
        if doc.exists:
            print("✅ google_api_calls collection: Write/Read successful")
        else:
            print("❌ google_api_calls collection: Read failed")
            return False
        
        # Clean up test document
        test_ref.delete()
        
        # Test log_metrics collection
        test_metrics = LogMetricsSchema.create_session_summary(
            user_id="test_user",
            session_id="test_session",
            start_time=datetime.utcnow(),
            end_time=datetime.utcnow(),
            total_api_calls=1,
            total_cost_usd=0.0005,
            api_breakdown={"gemini": {"calls": 1, "cost": 0.0005}}
        )
        
        # Write test document
        test_metrics_ref = db.collection('log_metrics').document('verification_test_session')
        test_metrics_ref.set(test_metrics)
        
        # Read it back
        metrics_doc = test_metrics_ref.get()
        if metrics_doc.exists:
            print("✅ log_metrics collection: Write/Read successful")
        else:
            print("❌ log_metrics collection: Read failed")
            return False
        
        # Clean up test document
        test_metrics_ref.delete()
        
        print("✅ All collections verified successfully!")
        return True
        
    except Exception as e:
        print(f"❌ Error verifying collections: {e}")
        return False


def main():
    """
    Main function to initialize and verify Firestore collections
    """
    print("Enhanced Logging System - Firestore Collections Setup")
    print("=" * 60)
    
    # Check environment variables
    required_env_vars = [
        'GOOGLE_CLOUD_PROJECT_ID',
        'GOOGLE_APPLICATION_CREDENTIALS'
    ]
    
    missing_vars = []
    for var in required_env_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print("❌ Missing required environment variables:")
        for var in missing_vars:
            print(f"   - {var}")
        print("\nPlease set these variables before running the initialization.")
        return False
    
    print(f"✅ Environment variables configured")
    print(f"   - Project ID: {os.getenv('GOOGLE_CLOUD_PROJECT_ID')}")
    print(f"   - Credentials: {os.getenv('GOOGLE_APPLICATION_CREDENTIALS')}")
    
    # Initialize collections
    if not initialize_collections():
        return False
    
    # Verify collections
    if not verify_collections():
        return False
    
    print("\n🎉 Enhanced logging system setup complete!")
    print("\nNext steps:")
    print("1. Create the required Firestore indexes (see output above)")
    print("2. Test the logging system with actual API calls")
    print("3. Monitor cost reporting in the Firestore console")
    
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)