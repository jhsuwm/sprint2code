import asyncio
from typing import Dict, Any, List, Optional
from log_config import logger, error
from agents.job_manager import job_store

class CodeExecutionManager:
    def __init__(self, job_manager, gemini_service, github_service, jira_service):
        self.job_manager = job_manager
        self.gemini_service = gemini_service
        self.github_service = github_service
        self.jira_service = jira_service

    async def execute_subtasks(self, job_id, story_key, subtasks, clean_prd, technical_config, attachments_data):
        if not subtasks:
            self.job_manager.log(job_id, "No subtasks created. Generating code for main story.", "Coding: Main Story", level="WARNING")
            
            # Handle technical_config being a dict
            config_str = technical_config
            if isinstance(technical_config, dict):
                config_str = technical_config.get("full") or f"Frontend:\n{technical_config.get('frontend')}\n\nBackend:\n{technical_config.get('backend')}"
                
            code_context = f"Technical Configuration:\n{config_str}" if config_str else ""
            await self.gemini_service.generate_code("Implement story requirements", code_context, clean_prd, attachments_data)
            return

        for i, subtask in enumerate(subtasks):
            subtask_id, subtask_key = subtask["id"], subtask["key"]
            subtask_fields = subtask.get("fields", {})
            summary = subtask_fields.get("summary", "")
            desc = subtask_fields.get("description", summary)
            if not isinstance(desc, str): desc = summary # ADF handling removed for brevity in extract

            # Determine task type using AI and filter context
            task_type = await self._determine_task_type_ai(summary, desc)
            filtered_config = self._filter_technical_config(technical_config, task_type)
            
            # Determine correct GitHub repo for this task type
            github_repo = self._determine_github_repo(job_id, technical_config, task_type)
            
            # CONTEXT OPTIMIZATION: Use strictly filtered config and specialized context
            code_context = f"JIRA Story Product Requirements (PRD):\n{clean_prd}\n\n"
            if filtered_config:
                code_context += f"TECHNICAL REQUIREMENTS ({task_type.upper()}):\n{filtered_config}"
            elif isinstance(technical_config, str):
                code_context += f"TECHNICAL REQUIREMENTS:\n{technical_config}"

            self.job_manager.log(job_id, f"Processing {task_type} subtask {i+1}/{len(subtasks)}: {subtask_key}", f"Coding: {subtask_key}")
            
            # Log technical requirement source but NOT the whole content
            if filtered_config:
                logger.info(f"⚙️ [Job {job_id}] Using filtered technical config for {task_type} ({len(filtered_config)} characters)")
            
            # CRITICAL DEBUG: Log that we're about to call AI
            logger.info(f"🤖 [Job {job_id}] About to generate code for subtask {subtask_key} using AI...")
            
            # Add existing repo files to context for better awareness (fetched for the specific repo)
            repo_files = []
            if github_repo:
                try:
                    owner, repo = github_repo.split('/')
                    base_branch = job_store[job_id].get("base_branch_for_pr") or self.github_service.get_default_branch(owner, repo)
                    repo_files = self.github_service.list_files(owner, repo, base_branch)
                    logger.info(f"Fetched {len(repo_files)} files from {github_repo} for context awareness")
                except Exception as e:
                    logger.warning(f"Failed to fetch files for {github_repo}: {e}")
                    repo_files = job_store[job_id].get("repo_files", []) # Fallback to primary repo files
            
            # Generate code with retry logic
            parsed_files = await self._generate_code_with_retry(job_id, subtask_key, desc, code_context, clean_prd, attachments_data, repo_files)
            
            # CRITICAL DEBUG: Log AI generation result
            if parsed_files:
                logger.info(f"✅ [Job {job_id}] AI generated {len(parsed_files)} files for subtask {subtask_key}")
                for f in parsed_files:
                    logger.info(f"   📄 {f['file_path']} ({len(f['content'])} chars)")
            else:
                logger.error(f"❌ [Job {job_id}] AI generated ZERO files for subtask {subtask_key}! This is a critical issue.")
                self.job_manager.log(job_id, f"❌ AI generated no files for {subtask_key}", f"AI Error: {subtask_key}", level="ERROR")
            
            if parsed_files:
                # Intelligent File Routing: Commit each file to the correct repository
                committed = []; failed = []
                github_branch = job_store[job_id].get("github_branch")
                all_repos_info = job_store[job_id].get("all_repos", [])
                
                for file_info in parsed_files:
                    path, content = file_info['file_path'], file_info['content']
                    
                    # 1. Determine the best repository for this specific file
                    target_repo_str = github_repo # Default to the one determined for the subtask
                    file_path_lower = path.lower()
                    
                    # Heuristic to identify file type
                    inferred_file_type = None
                    if "backend/" in file_path_lower or file_path_lower.endswith((".py", "requirements.txt")):
                        inferred_file_type = "backend"
                    elif "frontend/" in file_path_lower or file_path_lower.endswith((".tsx", ".jsx", ".ts", ".js", ".css", ".scss", "package.json")):
                        inferred_file_type = "frontend"
                        
                    if inferred_file_type:
                        # Find a repository that matches this inferred type
                        matching_repo_info = next((r for r in all_repos_info if r.get("type") == inferred_file_type), None)
                        if matching_repo_info:
                            target_repo_str = f"{matching_repo_info['owner']}/{matching_repo_info['repo']}"
                    
                    if not target_repo_str or not github_branch:
                        failed.append(path)
                        continue

                    # 2. Path normalization (handle monorepo prefixes in separate repos)
                    final_path = path
                    owner, repo_name = target_repo_str.split('/')
                    
                    # Check if we should strip 'backend/' or 'frontend/' prefix
                    # We strip if the target repo is specialized and doesn't seem to use the prefix
                    target_repo_type = next((r.get("type", "unknown") for r in all_repos_info if f"{r['owner']}/{r['repo']}" == target_repo_str), "unknown")
                    
                    if target_repo_type == "backend" and path.startswith("backend/"):
                        final_path = path[len("backend/"):]
                    elif target_repo_type == "frontend" and path.startswith("frontend/"):
                        final_path = path[len("frontend/"):]
                    
                    # 3. Commit the file
                    msg = f"[{subtask_key}] {summary}\n\nGenerated file: {final_path}"
                    if self.github_service.commit_file(owner, repo_name, github_branch, final_path, content, msg):
                        committed.append(f"{target_repo_str}:{final_path}")
                        # Track modified repos for PR creation later
                        modified_repos = job_store[job_id].get("modified_repos", [])
                        if target_repo_str not in modified_repos:
                            modified_repos.append(target_repo_str)
                        job_store[job_id]["modified_repos"] = modified_repos
                    else:
                        failed.append(path)
                
                # Update JIRA and log
                status_updated = self.jira_service.update_issue_status(subtask_id, "DONE")
                
                # Create repo dict for logging
                current_repo_dict = None
                if github_repo:
                    owner, repo_name = github_repo.split('/')
                    current_repo_dict = {"owner": owner, "repo": repo_name}
                
                log_adf = self.job_manager.format_subtask_execution_log(subtask_key, summary, job_id, parsed_files, status_updated, current_repo_dict, github_branch, committed, failed)
                self.jira_service.add_comment(subtask_id, log_adf)

    async def _generate_code_with_retry(self, job_id, subtask_key, desc, code_context, clean_prd, attachments, repo_files=None):
        max_retries = 10; retry_count = 0
        while retry_count < max_retries:
            try:
                result = await self.gemini_service.generate_code(desc, code_context, clean_prd, attachments, repo_files=repo_files)
                code = result[0] if isinstance(result, tuple) else result
                reason = result[1] if isinstance(result, tuple) else 'STOP'
                
                parsed_files = self.gemini_service.parse_generated_code(code)
                real_files = [f for f in parsed_files if not f['file_path'].startswith('generated_code.') and not f['file_path'].startswith('error.')]
                
                # Retry on MAX_TOKENS, RECITATION, or parsing failures
                if reason in ['MAX_TOKENS', 'RECITATION', 'SAFETY', 'OTHER'] or (parsed_files and not real_files):
                    retry_count += 1
                    if retry_count < max_retries:
                        if reason == 'MAX_TOKENS':
                            self.job_manager.log(job_id, f"Code truncated due to token limit (attempt {retry_count}), retrying...", f"Retrying: {subtask_key}", level="WARNING")
                        elif reason == 'RECITATION':
                            self.job_manager.log(job_id, f"Response blocked due to RECITATION/copyright (attempt {retry_count}), retrying with modified prompt...", f"Retrying: {subtask_key}", level="WARNING")
                        elif reason in ['SAFETY', 'OTHER']:
                            self.job_manager.log(job_id, f"Response blocked due to {reason} (attempt {retry_count}), retrying...", f"Retrying: {subtask_key}", level="WARNING")
                        else:
                            self.job_manager.log(job_id, f"Code parsing failed (attempt {retry_count}), retrying...", f"Retrying: {subtask_key}", level="WARNING")
                        await asyncio.sleep(2)  # Longer wait for blocked responses
                        continue
                    else:
                        # Max retries reached
                        if reason in ['RECITATION', 'SAFETY', 'OTHER']:
                            self.job_manager.log(job_id, f"Code generation blocked after {max_retries} attempts (reason: {reason}). Skipping subtask.", f"Blocked: {subtask_key}", level="ERROR")
                        else:
                            self.job_manager.log(job_id, f"Code generation failed after {max_retries} attempts. Using partial result if available.", f"Failed: {subtask_key}", level="ERROR")
                        return real_files  # Return whatever we have, even if empty
                return real_files
            except Exception as e:
                self.job_manager.log(job_id, f"Code generation failed: {e}", "Code Error", level="ERROR"); return []
        return []

    async def _determine_task_type_ai(self, summary: str, description: str) -> str:
        """Use AI to determine the task type (frontend, backend, or fullstack)."""
        prompt = f"""
        Analyze the following JIRA subtask and categorize it into one of these three types:
        1. frontend (Next.js, React, UI, Styling, CSS, Components, Pages)
        2. backend (Python, FastAPI, API, Database, Firestore, Models, Services, Auth)
        3. fullstack (Tasks involving both frontend and backend changes)
        
        Subtask Summary: {summary}
        Subtask Description: {description}
        
        Respond with ONLY the category name (frontend, backend, or fullstack).
        """
        try:
            # Use Gemini to categorize via the existing service's retry logic
            # Use self.gemini_service.client.models.generate_content (the method) NOT as an attribute access on a call
            response = await self.gemini_service._call_with_retry(
                'models.generate_content', # Pass as string to avoid split error in _call_with_retry
                model=self.gemini_service.model_name, # Use model_name string
                contents=prompt,
                config={'temperature': 0.1}
            )
            category = response.text.strip().lower()
            # Clean up potential extra output
            for possible in ["frontend", "backend", "fullstack"]:
                if possible in category:
                    return possible
        except Exception as e:
            logger.warning(f"AI categorization failed: {e}")
        
        # If AI fails, default to fullstack to ensure all context is provided to the generator
        return "fullstack"

    def _filter_technical_config(self, technical_config: Any, task_type: str) -> Optional[str]:
        if not isinstance(technical_config, dict):
            return technical_config
            
        if task_type == "frontend":
            return technical_config.get("frontend") or technical_config.get("full")
        elif task_type == "backend":
            return technical_config.get("backend") or technical_config.get("full")
        else:
            # For fullstack or unknown, combine both
            config_str = technical_config.get("full")
            if not config_str:
                config_str = ""
                if technical_config.get("frontend"):
                    config_str += f"FRONTEND CONFIGURATION:\n{technical_config['frontend']}\n"
                if technical_config.get("backend"):
                    config_str += f"BACKEND CONFIGURATION:\n{technical_config['backend']}\n"
            return config_str

    def _determine_github_repo(self, job_id: str, technical_config: Any, task_type: str) -> Optional[str]:
        """
        Determine the correct GitHub repository for the given task type.
        """
        import yaml
        
        # Default repo from job store (primary repo)
        default_repo = job_store[job_id].get("github_repo")
        
        if not isinstance(technical_config, dict):
            return default_repo
            
        repo_url = None
        if task_type == "frontend" and technical_config.get("frontend"):
            try:
                cfg = yaml.safe_load(technical_config["frontend"])
                repo_url = cfg.get("github_repository") or cfg.get("github_url")
                if repo_url:
                    logger.info(f"Determined frontend repo from config: {repo_url}")
            except Exception as e:
                logger.warning(f"Failed to parse frontend config for repo extraction: {e}")
        elif task_type == "backend" and technical_config.get("backend"):
            try:
                cfg = yaml.safe_load(technical_config["backend"])
                repo_url = cfg.get("github_repository") or cfg.get("github_url")
                if repo_url:
                    logger.info(f"Determined backend repo from config: {repo_url}")
            except Exception as e:
                logger.warning(f"Failed to parse backend config for repo extraction: {e}")
            
        if repo_url:
            repo_info = self.github_service.extract_github_repo_from_description(f"Github: {repo_url}")
            if repo_info:
                repo_str = f"{repo_info['owner']}/{repo_info['repo']}"
                return repo_str
                
        if default_repo:
            logger.info(f"Using default repository for {task_type} task: {default_repo}")
        return default_repo

    async def create_pull_request(self, job_id, story_key, summary):
        modified_repos = job_store[job_id].get("modified_repos", [])
        if not modified_repos:
            # Fallback to default repo if no files were committed yet (unlikely)
            repo = job_store[job_id].get("github_repo")
            if repo: modified_repos.append(repo)
            
        branch = job_store[job_id].get("github_branch")
        base = job_store[job_id].get("base_branch_for_pr")
        
        if not branch or not base:
            return None
            
        pr_urls = []
        for repo in modified_repos:
            owner, repo_name = repo.split('/')
            title = f"[{story_key}] {summary}"
            body = f"## Autonomous Dev Agent - Generated Code\n\n**JIRA Story:** {story_key}\n\nGenerated for Job ID: {job_id}"
            url = self.github_service.create_pull_request(owner, repo_name, branch, base, title, body)
            if url:
                pr_urls.append(url)
        
        if pr_urls:
            job_store[job_id]["pull_request_url"] = pr_urls[0] # UI expects single URL, return first
            job_store[job_id]["all_pull_request_urls"] = pr_urls
            return pr_urls[0]
            
        return None
