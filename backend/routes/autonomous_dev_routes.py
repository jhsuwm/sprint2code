from fastapi import APIRouter, HTTPException, Header
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from services.jira_service import JiraService
from agents.autonomous_dev_agent import AutonomousDevAgent
from agents.skill_registry import SkillRegistry
from log_config import logger

router = APIRouter()
jira_service = JiraService()
agent = AutonomousDevAgent()
skill_registry = SkillRegistry()

class GenerateRequest(BaseModel):
    story_id: str
    skill_names: Optional[List[str]] = None
    min_backend_subtasks: Optional[int] = None
    min_frontend_subtasks: Optional[int] = None

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
        "email": "demo@sprint2code.local",
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

@router.get("/local-skills")
async def get_local_skills(authorization: str = Header(...)):
    """
    List local SKILL.md files from skills/.
    """
    try:
        await get_user_from_header(authorization)
        skills = skill_registry.list_local_skills()
        logger.info(f"Found {len(skills)} local skills")
        return skills
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing local skills: {e}")
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
        logger.info(
            f"Starting generation for user: {email}, story: {request.story_id}, "
            f"skills: {request.skill_names}"
        )
        job_id = await agent.start_job(
            request.story_id,
            email,
            skill_names=request.skill_names or [],
            min_backend_subtasks=request.min_backend_subtasks,
            min_frontend_subtasks=request.min_frontend_subtasks
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

@router.post("/rerun-deployment/{job_id}")
async def rerun_deployment(
    job_id: str,
    authorization: str = Header(...)
):
    """
    Rerun the deployment pipeline after user has manually fixed validation errors.
    This is triggered by the "Rerun Deployment" button in the UI when validation fails.
    """
    try:
        user_payload = await get_user_from_header(authorization)
        email = user_payload.get("email")
        
        # Get the job status to check if it's in a rerunnable state
        job_status = agent.get_job_status(job_id)
        if job_status.get("status") == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="Job not found")
        
        app_status = job_status.get("app_status")
        
        # Only allow rerun for validation failures
        if app_status not in ["VALIDATION_FAILED", "STARTUP_FAILED"]:
            raise HTTPException(
                status_code=400, 
                detail=f"Cannot rerun deployment. Current status: {app_status}. Only VALIDATION_FAILED or STARTUP_FAILED jobs can be rerun."
            )
        
        logger.info(f"Rerunning deployment for job {job_id} by user {email}")
        
        # Call the agent to rerun the deployment
        result = await agent.rerun_deployment(job_id, email)
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rerunning deployment: {e}")
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
