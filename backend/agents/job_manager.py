import uuid
import re
import json
from typing import Dict, Any, List, Optional
from datetime import datetime
from log_config import logger, error

# Global job store (In-memory for simplicity)
job_store: Dict[str, Dict[str, Any]] = {}

class JobManager:
    def __init__(self, jira_service):
        self.jira_service = jira_service

    def create_job(
        self, 
        story_id: str, 
        user_email: str, 
        config_name: Optional[str] = None, # Legacy grouped config name
        frontend_config_name: Optional[str] = None,
        backend_config_name: Optional[str] = None
    ) -> str:
        job_id = str(uuid.uuid4())
        job_store[job_id] = {
            "id": job_id,
            "story_id": story_id,
            "user_email": user_email,
            "config_name": config_name,
            "frontend_config_name": frontend_config_name,
            "backend_config_name": backend_config_name,
            "status": "RUNNING",
            "logs": [],
            "frontend_logs": [],
            "backend_logs": [],
            "app_status": None,
            "steps": [],
            "current_step": "Initializing",
            "start_time": datetime.now().isoformat(),
            "updated_time": datetime.now().isoformat()
        }
        return job_id

    def get_job(self, job_id: str) -> Dict[str, Any]:
        return job_store.get(job_id, {"status": "NOT_FOUND"})

    def update_job_status(self, job_id: str, status: str):
        if job_id in job_store:
            job_store[job_id]["status"] = status
            job_store[job_id]["updated_time"] = datetime.now().isoformat()

    def set_job_completion(self, job_id: str, success: bool):
        if job_id in job_store:
            job_store[job_id]["status"] = "COMPLETED" if success else "FAILED"
            job_store[job_id]["completion_time"] = datetime.now().isoformat()

    def log(self, job_id: str, message: str, step: str = None, level: str = "INFO"):
        if job_id in job_store:
            job = job_store[job_id]
            timestamp = datetime.now().isoformat()
            log_entry = f"[{timestamp}] [{level}] {message}"
            job["logs"].append(log_entry)
            job["updated_time"] = timestamp
            if step:
                job["current_step"] = step
                if not job["steps"] or job["steps"][-1]["name"] != step:
                    job["steps"].append({"name": step, "status": "COMPLETED", "timestamp": timestamp})
            
            if level == "ERROR":
                error(f"Job {job_id}: {message}", "AutonomousDevAgent")
            elif level == "WARNING":
                logger.warning(f"Job {job_id}: {message}")
            else:
                logger.info(f"Job {job_id}: {message}")

    def split_logs_into_chunks(self, logs: List[str], max_logs_per_chunk: int = 50) -> List[List[str]]:
        chunks = []
        for i in range(0, len(logs), max_logs_per_chunk):
            chunks.append(logs[i:i + max_logs_per_chunk])
        return chunks

    def format_execution_logs(self, job_id: str, story_key: str, log_chunk: List[str] = None, chunk_num: int = 1, total_chunks: int = 1) -> Dict[str, Any]:
        if job_id not in job_store:
            return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Execution logs not found"}]}]}
        
        job = job_store[job_id]
        logs = job.get("logs", [])
        logs_to_display = log_chunk if log_chunk is not None else logs
        
        content = []
        heading_text = "🤖 Autonomous Dev Agent - Complete Execution Log"
        if total_chunks > 1:
            heading_text = f"🤖 Autonomous Dev Agent - Execution Log (Part {chunk_num}/{total_chunks})"
        
        content.append({"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": heading_text}]})
        
        bullet_items = [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"Story: {story_key}", "marks": [{"type": "strong"}]}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"Job ID: {job_id}", "marks": [{"type": "strong"}]}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"Status: {job.get('status', 'UNKNOWN')}", "marks": [{"type": "strong"}]}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"Started: {job.get('start_time', 'N/A')}", "marks": [{"type": "strong"}]}]}]}
        ]
        
        if job.get('completion_time'):
            bullet_items.append({"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"Completed: {job.get('completion_time')}", "marks": [{"type": "strong"}]}]}]})
        if job.get('github_repo'):
            bullet_items.append({"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"GitHub Repo: {job.get('github_repo')}", "marks": [{"type": "strong"}]}]}]})
        if job.get('github_branch'):
            bullet_items.append({"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"Branch: {job.get('github_branch')}", "marks": [{"type": "strong"}]}]}]})
        if job.get('pull_request_url'):
            pr_url = job.get('pull_request_url')
            bullet_items.append({"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Pull Request: ", "marks": [{"type": "strong"}]}, {"type": "text", "text": pr_url, "marks": [{"type": "link", "attrs": {"href": pr_url}}]}]}]})
        if job.get('technical_config'):
            bullet_items.append({"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"Technical Config: {job.get('technical_config')}", "marks": [{"type": "strong"}]}]}]})
        
        content.append({"type": "bulletList", "content": bullet_items})
        content.append({"type": "rule"})
        content.append({"type": "heading", "attrs": {"level": 4}, "content": [{"type": "text", "text": "Execution Log:"}]})
        
        log_text = ""
        filtered_logs = [log for log in logs_to_display if not any(skip in log for skip in ["COMPLETE PROMPT SENT TO AI:", "TECHNICAL REQUIREMENTS FROM YAML CONFIG:", "END OF PROMPT", "=" * 80])]
        start_idx = (chunk_num - 1) * 50 + 1 if total_chunks > 1 else 1
        for i, log in enumerate(filtered_logs, start_idx):
            log_text += f"{i} {log}\n"

        content.append({"type": "codeBlock", "attrs": {"language": "text"}, "content": [{"type": "text", "text": log_text.rstrip() if log_text else "No logs to display"}]})
        content.append({"type": "rule"})
        content.append({"type": "paragraph", "content": [{"type": "text", "text": "Generated by Autonomous Dev Agent", "marks": [{"type": "em"}]}]})
        
        return {"type": "doc", "version": 1, "content": content}

    def format_subtask_execution_log(self, subtask_key: str, subtask_summary: str, job_id: str, parsed_files: List[Dict], status_updated: bool, github_repo: Optional[Dict], github_branch: Optional[str], committed_files: List[str], failed_files: List[str]) -> Dict[str, Any]:
        content = []
        content.append({"type": "heading", "attrs": {"level": 4}, "content": [{"type": "text", "text": f"ðŸ¤– Autonomous Dev Agent - Execution Log"}]})
        
        metadata_items = [f"Subtask: {subtask_key} - {subtask_summary}", "Status: Code Generated âœ…", f"Timestamp: {datetime.now().isoformat()}"]
        bullet_items = [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": item, "marks": [{"type": "strong"}]}]}]} for item in metadata_items]
        content.append({"type": "bulletList", "content": bullet_items})
        
        if parsed_files:
            content.append({"type": "heading", "attrs": {"level": 5}, "content": [{"type": "text", "text": f"Generated Files ({len(parsed_files)}):"}]})
            file_items = [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f['file_path'], "marks": [{"type": "code"}]}]}]} for f in parsed_files]
            content.append({"type": "bulletList", "content": file_items})
        
        content.append({"type": "paragraph", "content": [{"type": "text", "text": "JIRA Status: " + ("Updated to DONE âœ…" if status_updated else "Update failed âš ï¸ "), "marks": [{"type": "strong"}]}]})
        
        if github_repo and github_branch and (committed_files or failed_files):
            content.append({"type": "rule"})
            content.append({"type": "heading", "attrs": {"level": 5}, "content": [{"type": "text", "text": f"GitHub Commits ({github_repo['owner']}/{github_repo['repo']} @ {github_branch}):"}]})
            commit_items = [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "âœ… "}, {"type": "text", "text": file_path, "marks": [{"type": "code"}]}]}]} for file_path in committed_files]
            commit_items += [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "â Œ "}, {"type": "text", "text": file_path, "marks": [{"type": "code"}]}]}]} for file_path in failed_files]
            if commit_items: content.append({"type": "bulletList", "content": commit_items})
            content.append({"type": "paragraph", "content": [{"type": "text", "text": f"Summary: {len(committed_files)} committed, {len(failed_files)} failed", "marks": [{"type": "strong"}]}]})
        
        content.append({"type": "rule"})
        content.append({"type": "paragraph", "content": [{"type": "text", "text": f"Generated by Autonomous Dev Agent | Job ID: {job_id}", "marks": [{"type": "em"}]}]})
        
        return {"type": "doc", "version": 1, "content": content}
