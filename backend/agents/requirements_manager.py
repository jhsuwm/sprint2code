from typing import Dict, Any, List, Optional
from log_config import logger, error
from agents.job_manager import job_store
from agents.skill_registry import SkillRegistry

class RequirementsManager:
    def __init__(self, job_manager, jira_service, gemini_service, github_service):
        self.job_manager = job_manager
        self.jira_service = jira_service
        self.gemini_service = gemini_service
        self.github_service = github_service
        self.skill_registry = SkillRegistry()

    async def analyze_and_plan(self, job_id: str, story_id: str):
        story = self.jira_service.get_story_details(story_id)
        if not story: raise Exception("Story not found or access denied")
        story_key = story.get("key", story_id)
        
        self.job_manager.log(job_id, f"Starting analysis for Story: {story_key}", "Fetching Story")
        fields = story.get("fields", {})
        description = fields.get("description", "")
        
        # Attachments
        attachments_data = await self._process_attachments(job_id, story_id)
        
        # Load skill context
        skill_names = job_store[job_id].get("skill_names") or []

        technical_context = self.skill_registry.build_skill_context(skill_names)
        if technical_context.get("skills"):
            skill_list = ", ".join([s["name"] for s in technical_context["skills"]])
            self.job_manager.log(job_id, f"Loaded skill context: {skill_list}", "Skills Loaded")

        technical_config = technical_context

        if technical_config:
            job_store[job_id]["technical_config"] = technical_config
        
        # GitHub Repo & Branch Setup
        # Identify ALL unique repositories from technical config
        unique_repos = self._identify_all_repos(job_id, description, technical_config)
        
        if not unique_repos:
            self.job_manager.log(job_id, "⚠️ No GitHub repositories identified from skill frontmatter or JIRA description. Set github_repository in your SKILL.md files.", "Repo Error", level="WARNING")

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
            # Assemble skill content for the work plan prompt
            skill_str = technical_config.get("full")
            if not skill_str:
                has_frontend = bool(technical_config.get("frontend"))
                has_backend = bool(technical_config.get("backend"))

                if has_frontend and has_backend:
                    skill_str = "⚠️ FULL-STACK APPLICATION - YOU MUST GENERATE WORK PLAN FOR BOTH BACKEND AND FRONTEND\n\n"
                    skill_str += "=" * 80 + "\n"
                    skill_str += "BACKEND SKILL REQUIREMENTS:\n"
                    skill_str += "=" * 80 + "\n"
                    skill_str += technical_config['backend'] + "\n\n"
                    skill_str += "=" * 80 + "\n"
                    skill_str += "FRONTEND SKILL REQUIREMENTS:\n"
                    skill_str += "=" * 80 + "\n"
                    skill_str += technical_config['frontend'] + "\n"
                elif has_frontend:
                    skill_str = f"Frontend Skills:\n{technical_config['frontend']}\n"
                elif has_backend:
                    skill_str = f"Backend Skills:\n{technical_config['backend']}\n"
                else:
                    skill_str = ""
            work_plan_context += f"Technical Requirements (Skills):\n{skill_str}"
        
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
        """Identify all unique GitHub repositories from skill metadata.

        Priority order:
          1. ``github_repository`` field in each SKILL.md frontmatter — the canonical
             per-repo configuration set by the user (update this in your SKILL.md files).
          2. JIRA story description — final fallback.
        """
        repos = []
        seen = set()

        def add_repo(url, source, repo_type="unknown"):
            if not url:
                return
            repo_info = self.github_service.extract_github_repo_from_description(f"Github: {url}")
            if repo_info:
                repo_str = f"{repo_info['owner']}/{repo_info['repo']}"
                if repo_str not in seen:
                    repo_info["type"] = repo_type
                    repos.append(repo_info)
                    seen.add(repo_str)
                    self.job_manager.log(
                        job_id,
                        f"Identified {repo_type} repository from {source}: {repo_str}",
                        "Repo Identified",
                    )

        # ── Priority 1: github_repository from SKILL.md frontmatter ─────────────
        if technical_config:
            for skill_info in technical_config.get("skills", []):
                repo_url = skill_info.get("github_repository", "")
                if repo_url:
                    skill_type = skill_info.get("type", "full")
                    repo_type = skill_type if skill_type in ("frontend", "backend") else "full"
                    add_repo(repo_url, f"{skill_info['name']} skill (frontmatter)", repo_type=repo_type)

        # ── Priority 2: JIRA description fallback ────────────────────────────────
        if not repos:
            repo_info = self.github_service.extract_github_repo_from_description(description)
            if repo_info:
                add_repo(
                    f"https://github.com/{repo_info['owner']}/{repo_info['repo']}",
                    "JIRA description",
                )

        return repos

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

    def _get_mime_type(self, filename):
        ext = filename.lower().split('.')[-1]
        mimes = {'log':'text/plain', 'txt':'text/plain', 'png':'image/png', 'jpg':'image/jpeg', 'pdf':'application/pdf'}
        return mimes.get(ext, 'application/octet-stream')
