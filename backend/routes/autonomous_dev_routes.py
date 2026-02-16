from fastapi import APIRouter, Depends, HTTPException, Header
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from services.jira_service import JiraService
from agents.autonomous_dev_agent import AutonomousDevAgent
from database.firestore_config import get_firestore_client
from log_config import logger
from auth.jwt_utils import verify_token
from datetime import datetime

router = APIRouter()
jira_service = JiraService()
agent = AutonomousDevAgent()

class GenerateRequest(BaseModel):
    story_id: str
    config_name: Optional[str] = None # Legacy/Grouped
    frontend_config_name: Optional[str] = None
    backend_config_name: Optional[str] = None

class ConfigRequest(BaseModel):
    name: str
    type: Optional[str] = "grouped" # "frontend", "backend", or "grouped"
    content: str
    # Fields for grouped config (backward compatibility or combined approach)
    frontend_content: Optional[str] = None
    backend_content: Optional[str] = None

from fastapi import UploadFile, File, Form

async def get_user_from_header(authorization: str):
    """
    For OSS standalone version: No authentication required.
    Always return demo user for local desktop usage.
    """
    logger.info(f"OSS Mode: Bypassing authentication for local desktop app")
    
    # Always return demo user for standalone local app
    return {
        "user_id": "demo-user",
        "email": "demo@orion-dev-orchestrator.local",
        "has_restricted_access": True
    }

@router.get("/structure")
async def get_jira_structure(authorization: str = Header(...)):
    """
    Get JIRA Projects and their Epics.
    """
    try:
        await get_user_from_header(authorization)
        return jira_service.get_jira_structure()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting JIRA structure: {e}")
        raise HTTPException(status_code=500, detail=str(e))

import os
from pathlib import Path

@router.get("/local-configs")
async def get_local_configs(authorization: str = Header(...)):
    """
    List all YAML config files from the local config folder.
    """
    try:
        await get_user_from_header(authorization)
        
        # Get the config folder path (relative to backend root)
        config_dir = Path(__file__).parent.parent.parent / "config"
        
        if not config_dir.exists():
            logger.warning(f"Config directory not found: {config_dir}")
            return []
        
        # List all .yaml and .yml files
        config_files = []
        for file_path in config_dir.glob("*.yaml"):
            config_files.append({
                "name": file_path.name,
                "path": str(file_path.relative_to(config_dir.parent))
            })
        for file_path in config_dir.glob("*.yml"):
            config_files.append({
                "name": file_path.name,
                "path": str(file_path.relative_to(config_dir.parent))
            })
        
        logger.info(f"Found {len(config_files)} local config files")
        return config_files
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing local configs: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/local-config/{filename}")
async def get_local_config(filename: str, authorization: str = Header(...)):
    """
    Read a specific config file from the local config folder.
    """
    try:
        await get_user_from_header(authorization)
        
        # Get the config folder path
        config_dir = Path(__file__).parent.parent.parent / "config"
        config_file = config_dir / filename
        
        # Security check - ensure the file is within the config directory
        if not str(config_file.resolve()).startswith(str(config_dir.resolve())):
            raise HTTPException(status_code=403, detail="Access denied")
        
        if not config_file.exists():
            raise HTTPException(status_code=404, detail=f"Config file '{filename}' not found")
        
        # Read the file content
        with open(config_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        logger.info(f"Read local config file: {filename}")
        return {
            "name": filename,
            "content": content
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading local config: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/stories")
async def list_todo_stories(authorization: str = Header(...)):
    """
    List JIRA stories in 'TO DO' status.
    """
    try:
        user_payload = await get_user_from_header(authorization)
        email = user_payload.get("email")
        logger.info(f"Listing stories for user: {email}")
        return jira_service.get_todo_stories(email)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing stories: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate")
async def start_generation(request: GenerateRequest, authorization: str = Header(...)):
    """
    Kick off the code generation pipeline.
    """
    try:
        user_payload = await get_user_from_header(authorization)
        email = user_payload.get("email")
        logger.info(f"Starting generation for user: {email}, story: {request.story_id}, config: {request.config_name}, frontend_config: {request.frontend_config_name}, backend_config: {request.backend_config_name}")
        job_id = await agent.start_job(
            request.story_id, 
            email, 
            request.config_name,
            frontend_config_name=request.frontend_config_name,
            backend_config_name=request.backend_config_name
        )
        return {"job_id": job_id, "status": "RUNNING"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting generation: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/progress/{job_id}")
async def get_progress(job_id: str, authorization: str = Header(...)):
    """
    Get progress of a running job.
    """
    try:
        # Check auth
        await get_user_from_header(authorization)
        
        status = agent.get_job_status(job_id)
        if status.get("status") == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="Job not found")
        
        # Debug logging to verify logs are in response
        backend_logs_count = len(status.get("backend_logs", [])) if status.get("backend_logs") else 0
        frontend_logs_count = len(status.get("frontend_logs", [])) if status.get("frontend_logs") else 0
        logger.info(f"Progress API returning: backend_logs={backend_logs_count}, frontend_logs={frontend_logs_count}, app_status={status.get('app_status')}")
        
        return status
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting progress: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/config")
async def save_config(config: ConfigRequest, authorization: str = Header(...)):
    """
    Save a YAML technical configuration to Firestore.
    """
    try:
        user_payload = await get_user_from_header(authorization)
        email = user_payload.get("email")
        
        # Get Firestore client
        db = get_firestore_client()
        if not db:
            raise HTTPException(status_code=503, detail="Firestore unavailable")
        
        # Create config document
        config_data = {
            "name": config.name,
            "type": config.type,
            "content": config.content,
            "frontend_content": config.frontend_content,
            "backend_content": config.backend_content,
            "created_by": email,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        
        # Use name as document ID directly as requested by user
        doc_id = config.name
        
        # Save to Firestore
        doc_ref = db.collection("autonomous_dev_configs").document(doc_id)
        doc_ref.set(config_data)
        
        logger.info(f"Config '{config.name}' saved by user: {email}")
        return {"message": "Config saved successfully", "name": config.name}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/config/{config_name}")
async def get_config(config_name: str, authorization: str = Header(...)):
    """
    Retrieve a YAML technical configuration from Firestore.
    """
    try:
        await get_user_from_header(authorization)
        
        # Get Firestore client
        db = get_firestore_client()
        if not db:
            raise HTTPException(status_code=503, detail="Firestore unavailable")
        
        # Fetch config from Firestore
        doc_ref = db.collection("autonomous_dev_configs").document(config_name)
        doc = doc_ref.get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail=f"Config '{config_name}' not found")
        
        config_data = doc.to_dict()
        logger.info(f"Config '{config_name}' retrieved")
        return config_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving config: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/configs")
async def list_configs(authorization: str = Header(...)):
    """
    List all available YAML technical configurations.
    """
    try:
        await get_user_from_header(authorization)
        
        # Get Firestore client
        db = get_firestore_client()
        if not db:
            raise HTTPException(status_code=503, detail="Firestore unavailable")
        
        # Fetch all configs
        configs_ref = db.collection("autonomous_dev_configs")
        docs = configs_ref.stream()
        
        configs = []
        for doc in docs:
            config_data = doc.to_dict()
            configs.append({
                "name": config_data.get("name"),
                "type": config_data.get("type", "grouped"),
                "created_by": config_data.get("created_by"),
                "created_at": config_data.get("created_at"),
                "updated_at": config_data.get("updated_at")
            })
        
        logger.info(f"Listed {len(configs)} configs")
        return configs
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing configs: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/config/{config_name}")
async def delete_config(config_name: str, authorization: str = Header(...)):
    """
    Delete a YAML technical configuration from Firestore.
    """
    try:
        await get_user_from_header(authorization)
        
        # Get Firestore client
        db = get_firestore_client()
        if not db:
            raise HTTPException(status_code=503, detail="Firestore unavailable")
        
        # Configs are stored by name or type_name
        # We try both if the passed name doesn't exist
        doc_ref = db.collection("autonomous_dev_configs").document(config_name)
        doc = doc_ref.get()
        
        if not doc.exists:
            # Maybe it's frontend_name or backend_name
            # But the UI usually passes the doc_id if it's listing them
            raise HTTPException(status_code=404, detail=f"Config '{config_name}' not found")
            
        doc_ref.delete()
        logger.info(f"Config '{config_name}' deleted")
        return {"message": "Config deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting config: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate-prd")
async def generate_prd(
    prompt: str = Form(...),
    project_key: str = Form(None),
    epic_key: str = Form(None),
    attachments: List[UploadFile] = File(None),
    authorization: str = Header(...)
):
    """
    Generate PRD and create JIRA story from chat.
    """
    try:
        user_payload = await get_user_from_header(authorization)
        email = user_payload.get("email")
        
        processed_attachments = []
        if attachments:
            for att in attachments:
                content = await att.read()
                processed_attachments.append({
                    "filename": att.filename,
                    "content": content,
                    "mime_type": att.content_type,
                    "type": "image" if att.content_type.startswith("image/") else "pdf"
                })
        
        # Call the agent to create story from chat
        result = await agent.create_story_from_chat(
            prompt=prompt,
            attachments=processed_attachments,
            user_email=email,
            project_key=project_key,
            epic_key=epic_key
        )
        
        # The result from create_story_from_chat already includes PRD and story_key
        # If the front-end is displaying the story_key with markdown, it's because
        # the original prompt was included, which may have been formatted for a user.
        # We will strip strong markdown formatting from the story key for safe display.
        story_key = result.get("story_key", "N/A")
        if story_key.startswith("**") and story_key.endswith("**"):
            result["story_key"] = story_key[2:-2]
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating PRD: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/fix-startup-failure/{job_id}")
async def fix_startup_failure(
    job_id: str,
    project_key: str = Form(None),
    epic_key: str = Form(None),
    authorization: str = Header(...)
):
    """
    Create a JIRA story to fix startup failures, automatically including failed logs as context.
    This is triggered by the "Fix Startup Issues" button in the UI.
    """
    try:
        user_payload = await get_user_from_header(authorization)
        email = user_payload.get("email")
        
        # Get the job status to retrieve logs
        job_status = agent.get_job_status(job_id)
        if job_status.get("status") == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="Job not found")
        
        # Get logs and app status
        backend_logs = job_status.get("backend_logs", [])
        frontend_logs = job_status.get("frontend_logs", [])
        app_status = job_status.get("app_status")
        
        # Check if there are actually startup failures
        if app_status not in ["STARTUP_FAILED", "STARTUP_FAILED_WITH_ERRORS", "DEPLOYMENT_FAILED", "FAILED"]:
            raise HTTPException(
                status_code=400, 
                detail="No startup failures detected for this job. Fix button should only be used when app_status indicates failure."
            )
        
        # Determine which logs to include
        failures = []
        processed_attachments = []
        
        if backend_logs:
            failures.append("Backend")
            # Convert backend log array to text content
            log_content = "=== BACKEND APP STARTUP LOG (FAILED) ===\n\n"
            log_content += "\n".join(backend_logs)
            log_content += "\n\n=== END BACKEND LOG ===\n"
            
            processed_attachments.append({
                'type': 'text',
                'filename': f'backend_startup_failure_{job_id}.txt',
                'content': log_content,
                'mime_type': 'text/plain'
            })
            logger.info(f"Added {len(backend_logs)} backend failure logs to fix request")
        
        if frontend_logs:
            failures.append("Frontend")
            # Convert frontend log array to text content
            log_content = "=== FRONTEND APP STARTUP LOG (FAILED) ===\n\n"
            log_content += "\n".join(frontend_logs)
            log_content += "\n\n=== END FRONTEND LOG ===\n"
            
            processed_attachments.append({
                'type': 'text',
                'filename': f'frontend_startup_failure_{job_id}.txt',
                'content': log_content,
                'mime_type': 'text/plain'
            })
            logger.info(f"Added {len(frontend_logs)} frontend failure logs to fix request")
        
        if not processed_attachments:
            raise HTTPException(
                status_code=400,
                detail="No failure logs found. Cannot create fix story without logs to analyze."
            )
        
        # Generate appropriate prompt based on failures
        failure_components = " and ".join(failures)
        prompt = f"Fix the {failure_components} startup failure. Review the attached logs to identify and resolve all errors preventing the application from starting successfully."
        
        logger.info(f"Creating fix story for {failure_components} failures from job {job_id}")
        
        # Call the agent to create story from chat with logs attached
        result = await agent.create_story_from_chat(
            prompt=prompt,
            attachments=processed_attachments,
            user_email=email,
            project_key=project_key,
            epic_key=epic_key
        )
        
        # Add metadata to result to help UI understand this was an auto-fix
        result["auto_fix"] = True
        result["original_job_id"] = job_id
        result["fixed_components"] = failures
        
        story_key = result.get("story_key", "N/A")
        if story_key.startswith("**") and story_key.endswith("**"):
            result["story_key"] = story_key[2:-2]
        
        logger.info(f"Fix story {story_key} created successfully for job {job_id}")
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating fix story: {e}")
        raise HTTPException(status_code=500, detail=str(e))
