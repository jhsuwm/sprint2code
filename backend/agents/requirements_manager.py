import re
import yaml
from typing import Dict, Any, List, Optional
from pathlib import Path
from log_config import logger, error
from agents.job_manager import job_store

class RequirementsManager:
    def __init__(self, job_manager, jira_service, gemini_service, github_service):
        self.job_manager = job_manager
        self.jira_service = jira_service
        self.gemini_service = gemini_service
        self.github_service = github_service

    async def analyze_and_plan(self, job_id: str, story_id: str):
        story = self.jira_service.get_story_details(story_id)
        if not story: raise Exception("Story not found or access denied")
        story_key = story.get("key", story_id)
        
        self.job_manager.log(job_id, f"Starting analysis for Story: {story_key}", "Fetching Story")
        fields = story.get("fields", {})
        description = fields.get("description", "")
        
        # Attachments
        attachments_data = await self._process_attachments(job_id, story_id)
        
        # Config
        # Check for individual configs first, then fallback to grouped
        frontend_config_name = job_store[job_id].get("frontend_config_name")
        backend_config_name = job_store[job_id].get("backend_config_name")
        config_name = job_store[job_id].get("config_name") # Unified or Legacy grouped config

        technical_config = {}
        
        # Helper to process a config document from Firestore
        def process_config_doc(doc_name, label):
            if not doc_name: return
            self.job_manager.log(job_id, f"Fetching {label} config: {doc_name}", "Fetching Config")
            doc = self._fetch_config_from_firestore(doc_name)
            if not doc:
                self.job_manager.log(job_id, f"⚠️ Config not found in Firestore: {doc_name}. Please ensure config exists in Firestore with exact name match.", "Config Error", level="WARNING")
                return
            
            c_type = doc.get("type", "grouped")
            content = doc.get("content", "")
            self.job_manager.log(job_id, f"Successfully loaded {label} config '{doc_name}' (type: {c_type})", "Config Loaded")
            
            if c_type == "frontend":
                technical_config["frontend"] = content
            elif c_type == "backend":
                technical_config["backend"] = content
            elif c_type == "grouped":
                # For grouped/fullstack, we might have both fields or everything in content
                technical_config["full"] = content
                if doc.get("frontend_content"): technical_config["frontend"] = doc.get("frontend_content")
                if doc.get("backend_content"): technical_config["backend"] = doc.get("backend_content")
        
        # Process all provided config names
        process_config_doc(frontend_config_name, "Frontend")
        process_config_doc(backend_config_name, "Backend")
        process_config_doc(config_name, "Unified/Grouped")
        
        if technical_config: job_store[job_id]["technical_config"] = technical_config
        
        # GitHub Repo & Branch Setup
        # Identify ALL unique repositories from technical config
        unique_repos = self._identify_all_repos(job_id, description, technical_config)
        
        if not unique_repos:
            self.job_manager.log(job_id, "⚠️ No GitHub repositories identified from config or JIRA description. Code cannot be committed.", "Repo Error", level="WARNING")

        file_list = []
        if unique_repos:
            # Set primary repo (usually backend or first found)
            primary_repo = unique_repos[0]
            job_store[job_id]["github_repo"] = f"{primary_repo['owner']}/{primary_repo['repo']}"
            # Store full repo info including type for deployment support
            job_store[job_id]["all_repos"] = unique_repos
            
            # Setup branches in ALL repos
            for repo_info in unique_repos:
                await self._setup_branch(job_id, story_key, repo_info, fields)
            
            # Fetch existing file list for context awareness (from primary repo)
            owner, repo = primary_repo["owner"], primary_repo["repo"]
            base_branch = job_store[job_id].get("base_branch_for_pr") or self.github_service.get_default_branch(owner, repo)
            file_list = self.github_service.list_files(owner, repo, base_branch)
            job_store[job_id]["repo_files"] = file_list

        # Work Plan
        clean_prd = self._clean_story_description(description)
        work_plan_context = f"JIRA Story Product Requirements (PRD):\n{clean_prd}\n\n"
        if file_list:
            work_plan_context += f"Existing Project Files:\n" + "\n".join(file_list[:100]) + ("\n...(truncated)" if len(file_list) > 100 else "") + "\n\n"
        
        if technical_config:
            # Use full config for planning if available, otherwise combine both
            config_str = technical_config.get("full")
            if not config_str:
                # IMPORTANT: When separate configs are provided, make it CRYSTAL CLEAR to AI that this is a full-stack app
                has_frontend = bool(technical_config.get("frontend"))
                has_backend = bool(technical_config.get("backend"))
                
                if has_frontend and has_backend:
                    config_str = "⚠️ FULL-STACK APPLICATION - YOU MUST GENERATE WORK PLAN FOR BOTH BACKEND AND FRONTEND\n\n"
                    config_str += "=" * 80 + "\n"
                    config_str += "BACKEND TECHNICAL REQUIREMENTS:\n"
                    config_str += "=" * 80 + "\n"
                    config_str += technical_config['backend'] + "\n\n"
                    config_str += "=" * 80 + "\n"
                    config_str += "FRONTEND TECHNICAL REQUIREMENTS:\n"
                    config_str += "=" * 80 + "\n"
                    config_str += technical_config['frontend'] + "\n"
                elif has_frontend:
                    config_str = f"Frontend Config:\n{technical_config['frontend']}\n"
                elif has_backend:
                    config_str = f"Backend Config:\n{technical_config['backend']}\n"
                else:
                    config_str = ""
            work_plan_context += f"Technical Requirements (YAML Config):\n{config_str}"
        
        work_plan = await self.gemini_service.generate_work_plan(work_plan_context)
        job_store[job_id]["work_plan"] = work_plan
        
        # CRITICAL: Check if work plan generation failed due to AI unavailability
        if not work_plan or "REMOTE AI MODEL UNAVAILABLE" in work_plan or "Error generating work plan" in work_plan:
            error_msg = (
                "🚫 PIPELINE ABORTED: Unable to generate work plan because the AI service is unavailable. "
                "No subtasks were created and no code can be generated. "
                "Please wait a few minutes for the AI service to recover, then try again."
            )
            self.job_manager.log(job_id, error_msg, "AI Service Unavailable", level="ERROR")
            error(error_msg, "RequirementsManager")
            raise Exception(error_msg)
        
        # Subtasks
        parsed_subtasks = self.gemini_service.parse_work_plan(work_plan)
        
        # CRITICAL: Verify that subtasks were actually parsed
        if not parsed_subtasks or len(parsed_subtasks) == 0:
            error_msg = (
                "🚫 PIPELINE ABORTED: No subtasks could be parsed from the work plan. "
                "This likely means the AI service failed to generate a valid work plan. "
                "Without subtasks, no code can be generated. Please try again later."
            )
            self.job_manager.log(job_id, error_msg, "No Subtasks Parsed", level="ERROR")
            error(error_msg, "RequirementsManager")
            raise Exception(error_msg)
        
        self.job_manager.log(
            job_id, 
            f"✅ Successfully parsed {len(parsed_subtasks)} subtasks from work plan", 
            "Subtasks Parsed"
        )
        
        created_subtasks = await self._manage_subtasks(job_id, story_id, parsed_subtasks)
        
        # CRITICAL: Verify that subtasks were actually created in JIRA
        if not created_subtasks or len(created_subtasks) == 0:
            error_msg = (
                "🚫 PIPELINE ABORTED: No subtasks were created in JIRA. "
                "Without subtasks, the pipeline cannot proceed to code generation. "
                "Please check JIRA permissions and try again."
            )
            self.job_manager.log(job_id, error_msg, "No Subtasks Created", level="ERROR")
            error(error_msg, "RequirementsManager")
            raise Exception(error_msg)
        
        self.job_manager.log(
            job_id, 
            f"✅ Successfully created {len(created_subtasks)} subtasks in JIRA", 
            "Subtasks Created"
        )
        
        return {
            "story_key": story_key,
            "fields": fields,
            "clean_prd": clean_prd,
            "technical_config": technical_config,
            "attachments_data": attachments_data,
            "subtasks": created_subtasks
        }

    async def _process_attachments(self, job_id, story_id):
        attachments_data = []
        raw = self.jira_service.get_issue_attachments(story_id)
        if raw:
            for att in raw:
                filename = att.get('filename', 'unknown')
                att_type = self.jira_service.identify_attachment_type(att)
                if att_type == 'other': continue
                content_bytes = self.jira_service.download_attachment_content(att)
                if content_bytes:
                    if att_type == 'text':
                        try:
                            attachments_data.append({'type': 'text', 'filename': filename, 'content': content_bytes.decode('utf-8'), 'mime_type': 'text/plain'})
                        except Exception: pass
                    else:
                        attachments_data.append({'type': att_type, 'filename': filename, 'content': content_bytes, 'mime_type': self._get_mime_type(filename)})
        return attachments_data

    def _identify_all_repos(self, job_id: str, description: str, technical_config: Dict[str, Any]) -> List[Dict[str, str]]:
        """Identify all unique GitHub repositories from technical config and description."""
        repos = []
        seen = set()
        
        def add_repo(url, source, repo_type="unknown"):
            if not url: return
            repo_info = self.github_service.extract_github_repo_from_description(f"Github: {url}")
            if repo_info:
                repo_str = f"{repo_info['owner']}/{repo_info['repo']}"
                if repo_str not in seen:
                    repo_info["type"] = repo_type
                    repos.append(repo_info)
                    seen.add(repo_str)
                    self.job_manager.log(job_id, f"Identified {repo_type} repository from {source}: {repo_str}", "Repo Identified")

        if technical_config:
            # Check backend, frontend, then full/legacy
            for key in ["backend", "frontend", "full"]:
                content = technical_config.get(key)
                if content:
                    try:
                        cfg = yaml.safe_load(content)
                        url = cfg.get('github_repository') or cfg.get('github_url')
                        if url:
                            add_repo(url, f"{key} config", repo_type=key)
                    except Exception as e:
                        logger.warning(f"Failed to parse {key} config YAML for repo extraction: {e}")
        
        # Finally check description as fallback
        if not repos:
            repo_info = self.github_service.extract_github_repo_from_description(description)
            if repo_info:
                add_repo(f"https://github.com/{repo_info['owner']}/{repo_info['repo']}")
                
        return repos

    def _setup_github_repo(self, description, technical_config):
        # Legacy method kept for backward compatibility if needed, but analyze_and_plan uses _identify_all_repos
        repos = self._identify_all_repos(description, technical_config)
        return repos[0] if repos else None

    async def _setup_branch(self, job_id, story_key, github_repo, fields):
        owner, repo = github_repo["owner"], github_repo["repo"]
        repo_name = f"{owner}/{repo}"
        self.job_manager.log(job_id, f"Setting up branches in repository: {repo_name}", "Branch Setup")
        
        epic_key = fields.get("parent", {}).get("key") if "parent" in fields else None
        if epic_key:
            epic_branch = epic_key.lower().replace("_", "-")
            story_branch = f"{epic_branch}-{story_key.lower().replace('_', '-')}"
            
            # Ensure epic branch exists
            if not self.github_service.branch_exists(owner, repo, epic_branch):
                self.job_manager.log(job_id, f"Creating epic branch '{epic_branch}' in {repo_name}", "Branch Setup")
                self.github_service.create_branch(owner, repo, epic_branch)
            
            # Create story branch from epic branch
            self.job_manager.log(job_id, f"Creating story branch '{story_branch}' from '{epic_branch}' in {repo_name}", "Branch Setup")
            if self.github_service.create_branch(owner, repo, story_branch, source_branch=epic_branch):
                job_store[job_id]["github_branch"] = story_branch
                job_store[job_id]["base_branch_for_pr"] = epic_branch
                self.job_manager.log(job_id, f"✅ Successfully setup story branch '{story_branch}' in {repo_name}", "Branch Setup")
            else:
                self.job_manager.log(job_id, f"❌ Failed to create story branch in {repo_name}", "Branch Setup", level="ERROR")
        else:
            story_branch = story_key.lower().replace("_", "-")
            base = self.github_service.get_default_branch(owner, repo)
            self.job_manager.log(job_id, f"Creating story branch '{story_branch}' from default branch '{base}' in {repo_name}", "Branch Setup")
            if self.github_service.create_branch(owner, repo, story_branch):
                job_store[job_id]["github_branch"] = story_branch
                job_store[job_id]["base_branch_for_pr"] = base
                self.job_manager.log(job_id, f"✅ Successfully setup story branch '{story_branch}' in {repo_name}", "Branch Setup")
            else:
                self.job_manager.log(job_id, f"❌ Failed to create story branch in {repo_name}", "Branch Setup", level="ERROR")

    async def _manage_subtasks(self, job_id, story_id, parsed_subtasks):
        created_subtasks = []
        story_refreshed = self.jira_service.get_story_details(story_id)
        existing = story_refreshed.get("fields", {}).get("subtasks", []) if story_refreshed else []
        if existing:
            for st in existing: created_subtasks.append({'id': st.get('id'), 'key': st.get('key'), 'fields': st.get('fields', {})})
        elif parsed_subtasks:
            for i, st_data in enumerate(parsed_subtasks):
                # Prepend global index to ensure strict ordering (1..N)
                numbered_summary = f"{i + 1}. {st_data['summary']}"
                created = self.jira_service.create_subtask(story_id, numbered_summary, st_data['description'])
                if created: created_subtasks.append({'id': created.get('id'), 'key': created.get('key'), 'fields': st_data})
        return created_subtasks

    def _extract_config_name(self, description):
        if not description: return None
        if isinstance(description, dict): description = self._adf_to_text(description)
        patterns = [r'config:\s*([a-zA-Z0-9_.-]+)', r'technical config:\s*([a-zA-Z0-9_.-]+)', r'\[config:\s*([a-zA-Z0-9_.-]+)\]', r'tech-config:\s*([a-zA-Z0-9_.-]+)']
        for pattern in patterns:
            match = re.search(pattern, description, re.IGNORECASE)
            if match: return match.group(1).strip()
        return None

    def _adf_to_text(self, adf):
        if not adf: return ''
        if isinstance(adf, str): return adf
        text = ''
        if isinstance(adf, dict):
            if adf.get('type') == 'doc' and 'content' in adf: return self._adf_to_text(adf['content'])
            if adf.get('type') == 'text': return adf.get('text', '')
            if 'content' in adf:
                node_texts = [self._adf_to_text(n) for n in adf['content']]
                return ''.join(node_texts).strip() + ('\n' if adf.get('type') in ['paragraph', 'heading', 'listItem'] else '')
        if isinstance(adf, list): return ''.join([self._adf_to_text(item) for item in adf])
        return text

    def _clean_story_description(self, description):
        text = self._adf_to_text(description)
        lines = text.split('\n')
        cleaned = []
        for line in lines:
            l_lower = line.lower().strip()
            if any(l_lower.startswith(k) for k in ['github:', 'config:', 'technical config:', 'tech-config:']): continue
            cleaned.append(line)
        return '\n'.join(cleaned).strip()

    def _fetch_config_from_firestore(self, config_name: str):
        """
        Fetch config from local file system (OSS standalone version).
        Reads directly from the config folder.
        """
        try:
            # Get the config folder path (3 levels up from agents/ directory)
            config_dir = Path(__file__).parent.parent.parent / "config"
            
            if not config_dir.exists():
                logger.warning(f"Config directory not found: {config_dir}")
                return None
            
            # Try to find the config file
            # Look for both .yaml and .yml extensions
            possible_files = [
                config_dir / f"{config_name}.yaml",
                config_dir / f"{config_name}.yml",
                config_dir / config_name  # In case the full filename with extension is provided
            ]
            
            config_file = None
            for file_path in possible_files:
                if file_path.exists():
                    config_file = file_path
                    break
            
            if not config_file:
                logger.warning(f"Config file not found for '{config_name}' in {config_dir}")
                return None
            
            # Read the YAML file
            with open(config_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Detect type from filename
            filename = config_file.name
            if 'frontend' in filename.lower():
                config_type = 'frontend'
            elif 'backend' in filename.lower():
                config_type = 'backend'
            else:
                config_type = 'grouped'
            
            logger.info(f"Successfully loaded local config file: {filename} (type: {config_type})")
            
            # Return in the same format as Firestore would
            return {
                "name": config_name,
                "type": config_type,
                "content": content
            }
            
        except Exception as e:
            logger.warning(f"Error reading local config '{config_name}': {e}")
            return None

    def _get_mime_type(self, filename):
        ext = filename.lower().split('.')[-1]
        mimes = {'log':'text/plain', 'txt':'text/plain', 'png':'image/png', 'jpg':'image/jpeg', 'pdf':'application/pdf'}
        return mimes.get(ext, 'application/octet-stream')
