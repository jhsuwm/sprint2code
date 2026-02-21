import asyncio
from typing import Dict, Any, List, Optional
from services.jira_service import JiraService
from services.gemini_service import GeminiService
from services.github_service import GitHubService
from services.local_app_service import LocalAppService
from agents.job_manager import JobManager, job_store
from agents.requirements_manager import RequirementsManager
from agents.code_execution_manager import CodeExecutionManager
from agents.deployment_manager import DeploymentManager
from log_config import logger

class AutonomousDevAgent:
    def __init__(self):
        self.jira_service = JiraService()
        self.gemini_service = GeminiService()
        self.github_service = GitHubService()
        self.local_app_service = LocalAppService()
        
        self.job_manager = JobManager(self.jira_service)
        self.requirements_manager = RequirementsManager(self.job_manager, self.jira_service, self.gemini_service, self.github_service)
        self.code_execution_manager = CodeExecutionManager(self.job_manager, self.gemini_service, self.github_service, self.jira_service)
        self.deployment_manager = DeploymentManager(self.job_manager, self.local_app_service, self.github_service, self.gemini_service, self.jira_service)

    async def create_story_from_chat(self, prompt: str, attachments: List[Dict[str, Any]], user_email: str, project_key: str = None, epic_key: str = None) -> Dict[str, Any]:
        logger.info(f"Creating story from chat for user: {user_email}")
        
        # Fix MIME types
        for att in attachments:
            if 'filename' in att:
                att['mime_type'] = self.requirements_manager._get_mime_type(att['filename'])
        
        prd = await self.gemini_service.generate_prd(prompt, attachments)
        story = self.jira_service.create_story(summary=f"AI Story: {prompt[:80]}...", description=prd, project_key=project_key, epic_key=epic_key)
        if not story or not story.get("id"): raise Exception("Failed to create JIRA story")
        
        if epic_key: self.jira_service.update_issue_status(epic_key, "IN PROGRESS")
        
        for att in attachments:
            self.jira_service.add_attachment(issue_id=story["id"], filename=att["filename"], content=att["content"], mime_type=att["mime_type"])
            
        return {"story_id": story["id"], "story_key": story["key"], "prd": prd}

    async def start_job(
        self, 
        story_id: str, 
        user_email: str, 
        config_name: Optional[str] = None, # Legacy grouped config name
        frontend_config_name: Optional[str] = None,
        backend_config_name: Optional[str] = None
    ) -> str:
        job_id = self.job_manager.create_job(
            story_id, 
            user_email, 
            config_name, 
            frontend_config_name=frontend_config_name,
            backend_config_name=backend_config_name
        )
        asyncio.create_task(self._run_pipeline(job_id, story_id))
        return job_id

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        return self.job_manager.get_job(job_id)
    
    async def rerun_deployment(self, job_id: str, user_email: str) -> Dict[str, Any]:
        """
        Rerun the deployment pipeline after user has manually fixed validation errors.
        Pulls latest code from GitHub and attempts to start the app locally.
        """
        try:
            # Get the original job data
            job_data = job_store.get(job_id)
            if not job_data:
                return {"success": False, "error": "Job not found"}
            
            # Extract necessary info from original job
            story_key = job_data.get("story_key")
            epic_key = job_data.get("epic_key")
            project_key = job_data.get("project_key")
            
            # Log the rerun
            self.job_manager.log(job_id, f"🔄 Rerunning deployment after manual fixes by {user_email}", "Rerun Deployment")
            
            # Reset app status to indicate we're retrying
            job_store[job_id]["app_status"] = "RERUNNING"
            job_store[job_id]["validation_error_summary"] = None
            job_store[job_id]["validation_error_details"] = None
            
            # Start the deployment process again
            await self.deployment_manager.start_app_locally(job_id, epic_key, story_key, project_key)
            
            return {
                "success": True,
                "job_id": job_id,
                "message": "Deployment rerun started"
            }
            
        except Exception as e:
            logger.error(f"Error rerunning deployment for job {job_id}: {e}")
            return {"success": False, "error": str(e)}

    async def _run_pipeline(self, job_id: str, story_id: str):
        try:
            # 1. Requirements and Planning
            plan_data = await self.requirements_manager.analyze_and_plan(job_id, story_id)
            story_key = plan_data["story_key"]
            epic_key = plan_data["fields"].get("parent", {}).get("key")

            # Update status
            self.jira_service.update_issue_status(story_id, "IN PROGRESS")

            # 2. Code Execution
            await self.code_execution_manager.execute_subtasks(
                job_id, story_key, plan_data["subtasks"], 
                plan_data["clean_prd"], plan_data["technical_config"], plan_data["attachments_data"]
            )
            
            # 3. Pull Request
            await self.code_execution_manager.create_pull_request(job_id, story_key, plan_data["fields"].get('summary'))

            # 4. Start App Locally (Static Analysis → Auto-Fix → Local Startup)
            await self.deployment_manager.start_app_locally(job_id, epic_key, story_key)
            
            # Check if deployment succeeded before marking as complete
            final_app_status = job_store.get(job_id, {}).get("app_status")
            
            if final_app_status in ["VALIDATION_FAILED", "STARTUP_FAILED", "FAILED"]:
                # Deployment failed - mark job as FAILED
                self.job_manager.set_job_completion(job_id, False)
                self._post_final_logs(job_id, story_id, story_key)
                # Don't update JIRA to DONE if deployment failed
                
                # Log final status with helpful message
                if final_app_status == "VALIDATION_FAILED":
                    error_count = len(job_store.get(job_id, {}).get("validation_errors_list", []))
                    self.job_manager.log(job_id, f"❌ Pipeline failed with status: {final_app_status} - Review detailed file errors listed above and fix them in your GitHub repository", "Pipeline Failed", level="ERROR")
                else:
                    self.job_manager.log(job_id, f"❌ Pipeline failed with status: {final_app_status}", "Pipeline Failed", level="ERROR")
            else:
                # Deployment succeeded - mark as complete
                self.job_manager.set_job_completion(job_id, True)
                self._post_final_logs(job_id, story_id, story_key)
                self.jira_service.update_issue_status(story_id, "DONE")

        except Exception as e:
            self.job_manager.log(job_id, f"Pipeline failed: {e}", "Error", level="ERROR")
            self.job_manager.set_job_completion(job_id, False)

    def _post_final_logs(self, job_id, story_id, story_key):
        all_logs = job_store[job_id].get("logs", [])
        chunks = self.job_manager.split_logs_into_chunks(all_logs)
        for i, chunk in enumerate(chunks, 1):
            summary = self.job_manager.format_execution_logs(job_id, story_key, chunk, i, len(chunks))
            self.jira_service.add_comment(story_id, summary)
