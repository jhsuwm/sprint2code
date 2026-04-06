from typing import Dict, Any, List, Optional
import re
import os
from log_config import logger, error
from agents.job_manager import job_store
from agents.skill_registry import SkillRegistry

class RequirementsManager:
    def __init__(self, job_manager, jira_service, ai_service, github_service):
        self.job_manager = job_manager
        self.jira_service = jira_service
        self.ai_service = ai_service
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
        # Identify ALL unique repositories from skill context
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
            work_plan_context += self._build_skill_requirements_context(technical_config)

        prd_requirements = self._extract_prd_requirements(clean_prd)
        if prd_requirements:
            work_plan_context += (
                "PRD REQUIREMENTS (MUST be covered by subtasks):\n- "
                + "\n- ".join(prd_requirements)
                + "\n\n"
            )
        
        # Pass per-domain minimums from UI overrides to the AI work plan prompt
        override_backend = None
        override_frontend = None
        if job_id and job_id in job_store:
            override_backend = job_store[job_id].get("min_backend_subtasks")
            override_frontend = job_store[job_id].get("min_frontend_subtasks")
        
        work_plan = await self.ai_service.generate_work_plan(
            work_plan_context,
            min_backend_subtasks=override_backend,
            min_frontend_subtasks=override_frontend
        )
        job_store[job_id]["work_plan"] = work_plan
        
        # CRITICAL: Check if work plan generation failed due to AI unavailability
        if not work_plan or "REMOTE AI MODEL UNAVAILABLE" in work_plan or "Error generating work plan" in work_plan:
            error_msg = (
                "🚫 PIPELINE ABORTED: Unable to generate work plan because the AI service is unavailable. "
                "No subtasks were created and no code can be generated. "
                "Please wait a few minutes for the AI service to recover, then try again."
            )
            self.job_manager.log(job_id, error_msg, "AI Service Unavailable", level="ERROR")
            error(error_msg)
            raise Exception(error_msg)
        
        # Subtasks
        parsed_subtasks = self.ai_service.parse_work_plan(work_plan)
        min_subtasks = self._determine_min_subtasks(clean_prd, technical_config, job_id=job_id)
        missing_coverage = self._detect_missing_skill_coverage(parsed_subtasks, technical_config)
        domain_deficit = self._get_domain_deficit(parsed_subtasks, technical_config, clean_prd, job_id=job_id)

        # Recovery path: if model produced too few subtasks, retry and enforce the minimum.
        if len(parsed_subtasks) < min_subtasks or missing_coverage or domain_deficit:
            max_regeneration_attempts = 2
            best_work_plan = work_plan
            best_subtasks = parsed_subtasks
            best_missing_coverage = missing_coverage
            best_domain_deficit = domain_deficit

            for attempt in range(1, max_regeneration_attempts + 1):
                if best_missing_coverage or best_domain_deficit:
                    missing_labels = ", ".join(best_missing_coverage + best_domain_deficit)
                    recovery_reason = (
                        f"⚠️ Work plan missing coverage for selected skill domains: {missing_labels}. "
                        f"Regenerating expanded work plan (attempt {attempt}/{max_regeneration_attempts})."
                    )
                else:
                    recovery_reason = (
                        f"⚠️ Work plan too short ({len(best_subtasks)} subtasks, expected at least {min_subtasks}). "
                        f"Regenerating expanded work plan (attempt {attempt}/{max_regeneration_attempts})."
                    )
                self.job_manager.log(
                    job_id,
                    recovery_reason,
                    "Work Plan Recovery",
                    level="WARNING"
                )
                expansion_instruction = (
                    f"\n\nCRITICAL OUTPUT REQUIREMENT:\n"
                    f"- Generate at least {min_subtasks} subtasks.\n"
                    f"- Use strict format for every item:\n"
                    f"  SUBTASK: <short title>\n"
                    f"  Desc: <implementation details>\n"
                    f"  ---\n"
                    f"- Prefix every subtask title with a domain tag: [Backend], [Frontend], or [Fullstack].\n"
                    f"- Use [Fullstack] only when the task truly spans both backend and frontend.\n"
                    f"- Cover backend and frontend completely when both skills are present.\n"
                    f"- Ensure EVERY PRD REQUIREMENT is covered by at least one subtask.\n"
                    f"- Keep each subtask focused and implementation-ready.\n"
                    f"- Avoid combining the full implementation into one broad subtask.\n"
                )
                if best_missing_coverage:
                    expansion_instruction += (
                        "- Mandatory skill coverage:\n"
                        + "".join([f"  - Include explicit subtasks for: {domain}.\n" for domain in best_missing_coverage])
                    )
                if best_domain_deficit:
                    expansion_instruction += (
                        "- Mandatory minimum subtask counts:\n"
                        + "".join([f"  - Ensure at least {label}.\n" for label in best_domain_deficit])
                    )
                candidate_work_plan = await self.ai_service.generate_work_plan(
                    work_plan_context + expansion_instruction,
                    min_backend_subtasks=override_backend,
                    min_frontend_subtasks=override_frontend
                )
                candidate_subtasks = self.ai_service.parse_work_plan(candidate_work_plan)
                candidate_missing_coverage = self._detect_missing_skill_coverage(candidate_subtasks, technical_config)
                candidate_domain_deficit = self._get_domain_deficit(candidate_subtasks, technical_config, clean_prd, job_id=job_id)

                candidate_is_better = False
                if len(candidate_missing_coverage) < len(best_missing_coverage):
                    candidate_is_better = True
                elif len(candidate_missing_coverage) == len(best_missing_coverage) and len(candidate_subtasks) > len(best_subtasks):
                    candidate_is_better = True
                elif len(candidate_domain_deficit) < len(best_domain_deficit):
                    candidate_is_better = True

                if candidate_is_better:
                    best_work_plan = candidate_work_plan
                    best_subtasks = candidate_subtasks
                    best_missing_coverage = candidate_missing_coverage
                    best_domain_deficit = candidate_domain_deficit

                if len(best_subtasks) >= min_subtasks and not best_missing_coverage and not best_domain_deficit:
                    break

            work_plan = best_work_plan
            parsed_subtasks = best_subtasks
            missing_coverage = best_missing_coverage
            domain_deficit = best_domain_deficit
            job_store[job_id]["work_plan"] = work_plan

        # Deterministic fallback: never abort solely because AI under-scoped the plan.
        if len(parsed_subtasks) < min_subtasks or missing_coverage:
            self.job_manager.log(
                job_id,
                "⚠️ AI work plan is still under-scoped after retries. Building deterministic fallback subtasks from PRD + selected skills.",
                "Work Plan Fallback",
                level="WARNING",
            )
            parsed_subtasks = self._build_fallback_subtasks(
                clean_prd=clean_prd,
                technical_config=technical_config,
                min_subtasks=min_subtasks,
                existing_subtasks=parsed_subtasks,
            )
            missing_coverage = self._detect_missing_skill_coverage(parsed_subtasks, technical_config)
            self.job_manager.log(
                job_id,
                f"Fallback planner produced {len(parsed_subtasks)} subtasks; remaining missing coverage: {missing_coverage or 'none'}",
                "Work Plan Fallback",
            )

        # Hard guarantee: ensure frontend/backend subtasks exist when those skills are selected.
        parsed_subtasks = self._ensure_skill_coverage_subtasks(parsed_subtasks, technical_config, clean_prd)
        missing_coverage = self._detect_missing_skill_coverage(parsed_subtasks, technical_config)
        # After fallback, use non-strict counting — the fallback is already the last resort
        # and its explicit [Backend]/[Frontend] subtasks should count fully.
        domain_deficit = self._get_domain_deficit(parsed_subtasks, technical_config, clean_prd, job_id=job_id, strict=False)
        # Hard guardrail: require minimum frontend/backend subtask counts before JIRA creation.
        if domain_deficit:
            # Retry once more with an explicit requirement before proceeding with fallback.
            recovery_hint = (
                "\n\nCRITICAL OUTPUT REQUIREMENT:\n"
                "- Add explicit frontend and backend subtasks to meet required minimum counts.\n"
                "- Prefix every subtask title with [Backend], [Frontend], or [Fullstack].\n"
                "- Use [Fullstack] only when the task truly spans both backend and frontend.\n"
                "- Ensure EVERY PRD REQUIREMENT is covered by at least one subtask.\n"
                + "".join([f"- Ensure at least {label}.\n" for label in domain_deficit])
            )
            candidate_work_plan = await self.ai_service.generate_work_plan(
                work_plan_context + recovery_hint,
                min_backend_subtasks=override_backend,
                min_frontend_subtasks=override_frontend
            )
            candidate_subtasks = self.ai_service.parse_work_plan(candidate_work_plan)
            # Only use AI retry output if it's actually better than what we already have.
            # Never replace good fallback subtasks with worse AI output.
            if candidate_subtasks:
                candidate_deficit = self._get_domain_deficit(candidate_subtasks, technical_config, clean_prd, job_id=job_id, strict=False)
                candidate_is_better = (
                    len(candidate_deficit) < len(domain_deficit) or
                    (len(candidate_deficit) == len(domain_deficit) and len(candidate_subtasks) > len(parsed_subtasks))
                )
                if candidate_is_better:
                    parsed_subtasks = candidate_subtasks
                    missing_coverage = self._detect_missing_skill_coverage(parsed_subtasks, technical_config)
                    domain_deficit = candidate_deficit
                    job_store[job_id]["work_plan"] = candidate_work_plan
                else:
                    self.job_manager.log(
                        job_id,
                        f"⚠️ AI retry produced worse results ({len(candidate_subtasks)} subtasks, {len(candidate_deficit)} deficits) than fallback ({len(parsed_subtasks)} subtasks, {len(domain_deficit)} deficits). Keeping fallback.",
                        "Work Plan Fallback",
                        level="WARNING",
                    )
        # After fallback + AI retry, relax the strict domain assertion.
        # If we have at least some dedicated backend and frontend subtasks, proceed.
        self._assert_min_domain_subtasks(job_id, parsed_subtasks, technical_config, clean_prd, relaxed=True)

        # CRITICAL: Reject under-scoped plans to prevent incomplete code generation.
        if len(parsed_subtasks) < min_subtasks:
            error_msg = (
                f"🚫 PIPELINE ABORTED: Work plan contains only {len(parsed_subtasks)} subtasks, "
                f"but at least {min_subtasks} are required for this story. "
                "The AI response is under-scoped, so code generation was stopped to avoid incomplete output. "
                "Please retry after AI service stabilizes."
            )
            self.job_manager.log(job_id, error_msg, "Insufficient Subtasks", level="ERROR")
            error(error_msg)
            raise Exception(error_msg)

        # CRITICAL: Ensure required technical domains from selected skills are represented.
        if missing_coverage:
            missing_labels = ", ".join(missing_coverage)
            error_msg = (
                f"🚫 PIPELINE ABORTED: Work plan is missing required technical coverage for: {missing_labels}. "
                "Selected skills are mandatory requirements, so code generation was stopped to avoid incomplete implementation."
            )
            self.job_manager.log(job_id, error_msg, "Missing Skill Coverage", level="ERROR")
            error(error_msg)
            raise Exception(error_msg)
        
        # CRITICAL: Verify that subtasks were actually parsed
        if not parsed_subtasks or len(parsed_subtasks) == 0:
            error_msg = (
                "🚫 PIPELINE ABORTED: No subtasks could be parsed from the work plan. "
                "This likely means the AI service failed to generate a valid work plan. "
                "Without subtasks, no code can be generated. Please try again later."
            )
            self.job_manager.log(job_id, error_msg, "No Subtasks Parsed", level="ERROR")
            error(error_msg)
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
            error(error_msg)
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
            if any(l_lower.startswith(k) for k in ['github:', 'skills:', 'skill context:', 'technical skills:']): continue
            cleaned.append(line)
        return '\n'.join(cleaned).strip()

    def _get_mime_type(self, filename):
        ext = filename.lower().split('.')[-1]
        mimes = {'log':'text/plain', 'txt':'text/plain', 'png':'image/png', 'jpg':'image/jpeg', 'pdf':'application/pdf'}
        return mimes.get(ext, 'application/octet-stream')

    def _determine_min_subtasks(self, clean_prd: str, technical_config: Dict[str, Any], job_id: Optional[str] = None) -> int:
        """Set a practical lower bound to avoid under-scoped plans."""
        has_frontend = bool((technical_config or {}).get("frontend"))
        has_backend = bool((technical_config or {}).get("backend"))
        is_fullstack = has_frontend and has_backend
        # Longer PRDs need more decomposition; keep floor conservative.
        prd_len = len(clean_prd or "")
        base = 8 if prd_len > 600 else 6
        if not is_fullstack:
            base = 6 if prd_len > 600 else 4

        # Allow UI override to set hard minimums (no hidden scaling when overrides are present).
        override_backend = None
        override_frontend = None
        if job_id and job_id in job_store:
            override_backend = job_store[job_id].get("min_backend_subtasks")
            override_frontend = job_store[job_id].get("min_frontend_subtasks")

        # If UI overrides exist, compute total minimum directly from them (or env defaults).
        if is_fullstack and (override_backend is not None or override_frontend is not None):
            backend_min = int(override_backend) if override_backend is not None else int(os.getenv("BACKEND_MIN_SUBTASKS", "4"))
            frontend_min = int(override_frontend) if override_frontend is not None else int(os.getenv("FRONTEND_MIN_SUBTASKS", "3"))
            return backend_min + frontend_min

        if not is_fullstack and (override_backend is not None or override_frontend is not None):
            if has_backend:
                return int(override_backend) if override_backend is not None else int(os.getenv("BACKEND_MIN_SUBTASKS", "4"))
            if has_frontend:
                return int(override_frontend) if override_frontend is not None else int(os.getenv("FRONTEND_MIN_SUBTASKS", "3"))

        req_count = len(self._extract_prd_requirements(clean_prd))
        if req_count:
            if is_fullstack:
                base = max(base, min(18, 4 + req_count))
            else:
                base = max(base, min(12, 3 + req_count))

        return base

    def _build_skill_requirements_context(self, technical_config: Dict[str, Any]) -> str:
        """Build explicit skill-driven planning instructions for work-plan generation."""
        if not technical_config:
            return ""

        selected_skills = technical_config.get("skills", [])
        skill_lines: List[str] = []
        for skill in selected_skills:
            name = skill.get("name", "unknown-skill")
            skill_type = skill.get("type", "full")
            description = skill.get("description", "")
            line = f"- {name} [{skill_type}]"
            if description:
                line += f": {description}"
            skill_lines.append(line)

        has_backend = bool(technical_config.get("backend"))
        has_frontend = bool(technical_config.get("frontend"))
        has_full = bool(technical_config.get("full"))

        sections: List[str] = []
        sections.append("Technical Requirements (Selected Skills):")
        if skill_lines:
            sections.extend(skill_lines)
        else:
            sections.append("- No explicit skill metadata found.")

        sections.append("")
        sections.append("MANDATORY SKILL-TO-SUBTASK COVERAGE RULES:")
        sections.append("- Every selected skill must be reflected in one or more subtasks.")
        sections.append("- Do not omit technical layers required by the selected skills.")
        if has_backend and has_frontend:
            sections.append("- This is full-stack: include backend subtasks and frontend subtasks.")
            sections.append("- Include API integration subtasks that connect frontend and backend flows.")
        elif has_backend:
            sections.append("- Backend is required: include backend architecture, API, and data-layer subtasks.")
        elif has_frontend:
            sections.append("- Frontend is required: include frontend architecture, pages/components, and API client subtasks.")
        if has_full:
            sections.append("- Include cross-cutting quality/reliability subtasks from full-stack skills.")

        if has_backend:
            sections.append("")
            sections.append("BACKEND SKILL REQUIREMENTS:")
            sections.append("=" * 80)
            sections.append(technical_config["backend"])
        if has_frontend:
            sections.append("")
            sections.append("FRONTEND SKILL REQUIREMENTS:")
            sections.append("=" * 80)
            sections.append(technical_config["frontend"])
        if has_full:
            sections.append("")
            sections.append("CROSS-CUTTING / FULL-STACK SKILL REQUIREMENTS:")
            sections.append("=" * 80)
            sections.append(technical_config["full"])

        return "\n".join(sections) + "\n\n"

    def _detect_missing_skill_coverage(self, parsed_subtasks: List[Dict[str, str]], technical_config: Dict[str, Any]) -> List[str]:
        """Heuristic coverage check to ensure required domains from selected skills appear in the plan."""
        if not technical_config:
            return []

        plan_text = " ".join(
            [f"{st.get('summary', '')} {st.get('description', '')}".lower() for st in (parsed_subtasks or [])]
        )
        summary_text = " ".join([st.get("summary", "").lower() for st in (parsed_subtasks or [])])

        missing: List[str] = []
        if technical_config.get("backend"):
            backend_keywords = ["backend", "fastapi", "api", "route", "service", "model", "database", "python"]
            summary_has_backend = any(keyword in summary_text for keyword in backend_keywords)
            summary_has_backend = summary_has_backend or any(self._extract_domain_tag(st.get("summary", "")) in ("backend", "fullstack") for st in (parsed_subtasks or []))
            if not summary_has_backend and not any(keyword in plan_text for keyword in backend_keywords):
                missing.append("backend")

        if technical_config.get("frontend"):
            frontend_keywords = ["frontend", "next.js", "nextjs", "react", "ui", "component", "page", "typescript"]
            summary_has_frontend = any(keyword in summary_text for keyword in frontend_keywords)
            summary_has_frontend = summary_has_frontend or any(self._extract_domain_tag(st.get("summary", "")) in ("frontend", "fullstack") for st in (parsed_subtasks or []))
            if not summary_has_frontend and not any(keyword in plan_text for keyword in frontend_keywords):
                missing.append("frontend")

        if technical_config.get("full"):
            full_keywords = ["test", "quality", "validation", "lint", "integration", "deployment", "e2e"]
            if not any(keyword in plan_text for keyword in full_keywords):
                missing.append("cross-cutting quality")

        return missing

    def _extract_prd_focus_areas(self, clean_prd: str, max_items: int = 6) -> List[str]:
        """Extract implementation-focus phrases from PRD bullets/numbered lines."""
        areas: List[str] = []
        if not clean_prd:
            return areas
        lines = [ln.strip() for ln in clean_prd.splitlines() if ln.strip()]
        for line in lines:
            normalized = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
            if len(normalized) < 12:
                continue
            lowered = normalized.lower()
            if any(marker in lowered for marker in ("requirement", "must", "should", "user can", "allow", "support", "feature", "flow")):
                if normalized not in areas:
                    areas.append(normalized)
            if len(areas) >= max_items:
                break
        return areas

    def _extract_prd_requirements(self, clean_prd: str) -> List[str]:
        """Extract PRD requirement lines (bullets/numbered) for stricter subtask coverage."""
        if not clean_prd:
            return []
        reqs: List[str] = []
        lines = [ln.strip() for ln in clean_prd.splitlines() if ln.strip()]
        for line in lines:
            normalized = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
            if len(normalized) < 10:
                continue
            lowered = normalized.lower()
            if any(marker in lowered for marker in ("shall", "must", "should", "require", "provide", "allow")):
                reqs.append(normalized)
        return reqs[:20]

    def _count_prd_domain_requirements(self, clean_prd: str, keywords: List[str]) -> int:
        """Count PRD requirement lines that mention any of the given keywords."""
        reqs = self._extract_prd_requirements(clean_prd)
        if not reqs:
            return 0
        lowered_keywords = [k.lower() for k in keywords]
        count = 0
        for req in reqs:
            lowered = req.lower()
            if any(k in lowered for k in lowered_keywords):
                count += 1
        return count

    def _extract_domain_tag(self, summary: str) -> str:
        lowered = (summary or '').lower()
        if '[backend]' in lowered:
            return 'backend'
        if '[frontend]' in lowered:
            return 'frontend'
        if '[fullstack]' in lowered:
            return 'fullstack'
        return ''

    def _infer_domain_from_keywords(self, keywords: List[str]) -> str:
        lowered = [k.lower() for k in (keywords or [])]
        if 'backend' in lowered:
            return 'backend'
        if 'frontend' in lowered:
            return 'frontend'
        return ''


    def _has_domain_subtask(self, subtasks: List[Dict[str, str]], keywords: List[str]) -> bool:
        """Return True if any subtask summary/description contains any keyword or explicit domain tag."""
        if not subtasks:
            return False
        lowered_keywords = [k.lower() for k in keywords]
        domain = self._infer_domain_from_keywords(keywords)
        for st in subtasks:
            summary_raw = (st.get("summary", "") or "")
            desc_raw = (st.get("description", "") or "")
            tag = self._extract_domain_tag(summary_raw)
            if domain and tag:
                if tag == domain or tag == "fullstack":
                    return True
                if tag in ("backend", "frontend"):
                    continue
            summary = summary_raw.lower()
            desc = desc_raw.lower()
            if any(k in summary for k in lowered_keywords):
                return True
            if any(k in desc for k in lowered_keywords):
                return True
        return False

    def _count_domain_subtasks(self, subtasks: List[Dict[str, str]], keywords: List[str], strict: bool = False) -> float:
        """Count subtasks belonging to a domain.

        When strict=True (user-set UI overrides), [Fullstack] subtasks count as 0.5
        toward each domain to prevent inflating the count and masking deficits.
        When strict=False, [Fullstack] counts fully toward both domains (legacy behavior).
        """
        if not subtasks:
            return 0
        lowered_keywords = [k.lower() for k in keywords]
        domain = self._infer_domain_from_keywords(keywords)
        count = 0.0
        for st in subtasks:
            summary_raw = (st.get("summary", "") or "")
            desc_raw = (st.get("description", "") or "")
            tag = self._extract_domain_tag(summary_raw)
            if domain and tag:
                if tag == domain:
                    count += 1.0
                elif tag == "fullstack":
                    count += 0.5 if strict else 1.0
                else:
                    continue
                continue
            summary = summary_raw.lower()
            desc = desc_raw.lower()
            if any(k in summary for k in lowered_keywords) or any(k in desc for k in lowered_keywords):
                count += 1.0
        return count

    def _get_domain_deficit(self, subtasks: List[Dict[str, str]], technical_config: Dict[str, Any], clean_prd: str, job_id: Optional[str] = None, strict: bool = None) -> List[str]:
        """Return deficit labels when required frontend/backend subtask counts are not met.
        
        When strict=True, [Fullstack] subtasks count as 0.5 toward each domain.
        When strict=False, [Fullstack] subtasks count fully (1.0) toward both domains.
        When strict=None (default), strictness is determined by UI overrides/env vars.
        """
        if not technical_config:
            return []
        backend_keywords = ["backend", "fastapi", "api", "service", "database", "model", "python"]
        frontend_keywords = ["frontend", "next.js", "nextjs", "react", "ui", "component", "page", "typescript"]

        override_backend = None
        override_frontend = None
        if job_id and job_id in job_store:
            override_backend = job_store[job_id].get("min_backend_subtasks")
            override_frontend = job_store[job_id].get("min_frontend_subtasks")

        min_backend = int(override_backend) if override_backend is not None else int(os.getenv("BACKEND_MIN_SUBTASKS", "4"))
        min_frontend = int(override_frontend) if override_frontend is not None else int(os.getenv("FRONTEND_MIN_SUBTASKS", "3"))
        strict_backend = str(os.getenv("BACKEND_MIN_SUBTASKS_STRICT", "false")).lower() in ("1", "true", "yes")
        strict_frontend = str(os.getenv("FRONTEND_MIN_SUBTASKS_STRICT", "false")).lower() in ("1", "true", "yes")
        # Scale minimums with PRD requirement counts for each domain (unless strict override is enabled).
        backend_req = self._count_prd_domain_requirements(clean_prd, backend_keywords)
        frontend_req = self._count_prd_domain_requirements(clean_prd, frontend_keywords)
        if backend_req and not strict_backend and override_backend is None:
            min_backend = max(min_backend, min(8, (backend_req + 1) // 2 + 1))
        if frontend_req and not strict_frontend and override_frontend is None:
            min_frontend = max(min_frontend, min(8, (frontend_req + 1) // 2 + 1))

        has_backend = bool(technical_config.get("backend"))
        has_frontend = bool(technical_config.get("frontend"))

        # When caller explicitly passes strict flag, use it directly.
        # Otherwise, use strict counting only when user set UI overrides.
        if strict is not None:
            strict_backend = strict
            strict_frontend = strict
        # When user explicitly set UI overrides, use strict counting so [Fullstack]
        # doesn't inflate the domain count and mask a real deficit.
        strict_backend = strict_backend or (override_backend is not None)
        strict_frontend = strict_frontend or (override_frontend is not None)

        backend_count = self._count_domain_subtasks(subtasks, backend_keywords, strict=strict_backend) if has_backend else 0
        frontend_count = self._count_domain_subtasks(subtasks, frontend_keywords, strict=strict_frontend) if has_frontend else 0

        missing_labels: List[str] = []
        if has_backend and backend_count < min_backend:
            missing_labels.append(f"backend ({int(backend_count) if backend_count == int(backend_count) else backend_count}/{min_backend})")
        if has_frontend and frontend_count < min_frontend:
            missing_labels.append(f"frontend ({int(frontend_count) if frontend_count == int(frontend_count) else frontend_count}/{min_frontend})")

        return missing_labels

    def _assert_min_domain_subtasks(self, job_id: str, subtasks: List[Dict[str, str]], technical_config: Dict[str, Any], clean_prd: str, relaxed: bool = False) -> None:
        """Abort when required frontend/backend subtask counts are not met.

        When relaxed=True (used after fallback path), only abort if there are
        ZERO dedicated backend or frontend subtasks. A deficit below the UI
        minimum is logged as a warning but does not abort the pipeline.
        """
        missing_labels = self._get_domain_deficit(subtasks, technical_config, clean_prd, job_id=job_id)
        if not missing_labels:
            return

        if relaxed:
            # In relaxed mode, check if we at least have SOME dedicated subtasks.
            # If yes, log a warning but proceed — the fallback is the last resort.
            has_backend = bool(technical_config.get("backend"))
            has_frontend = bool(technical_config.get("frontend"))
            backend_keywords = ["backend", "fastapi", "api", "service", "database", "model", "python"]
            frontend_keywords = ["frontend", "next.js", "nextjs", "react", "ui", "component", "page", "typescript"]
            has_some_backend = self._has_domain_subtask(subtasks, backend_keywords) if has_backend else True
            has_some_frontend = self._has_domain_subtask(subtasks, frontend_keywords) if has_frontend else True

            if has_some_backend and has_some_frontend:
                self.job_manager.log(
                    job_id,
                    f"⚠️ Work plan has dedicated backend/frontend subtasks but below UI minimums ({', '.join(missing_labels)}). "
                    f"Proceeding with fallback subtasks.",
                    "Domain Subtasks Warning",
                    level="WARNING",
                )
                return

        error_msg = (
            "🚫 PIPELINE ABORTED: Work plan does not include enough required subtasks: "
            + ", ".join(missing_labels)
            + ". Please regenerate the work plan to include full backend and frontend coverage."
        )
        self.job_manager.log(job_id, error_msg, "Insufficient Domain Subtasks", level="ERROR")
        error(error_msg)
        raise Exception(error_msg)

    def _ensure_skill_coverage_subtasks(
        self, subtasks: List[Dict[str, str]], technical_config: Dict[str, Any], clean_prd: str
    ) -> List[Dict[str, str]]:
        """Guarantee at least one backend/frontend subtask when those skills are selected."""
        if not technical_config:
            return subtasks

        has_backend = bool(technical_config.get("backend"))
        has_frontend = bool(technical_config.get("frontend"))
        focus_areas = self._extract_prd_focus_areas(clean_prd or "")
        focus_text = "; ".join(focus_areas[:2]) if focus_areas else "core user journeys and required product flows"

        backend_keywords = ["backend", "fastapi", "api", "service", "database", "model", "python"]
        frontend_keywords = ["frontend", "next.js", "nextjs", "react", "ui", "component", "page", "typescript"]

        merged = list(subtasks or [])
        seen = {(s.get("summary", "").strip().lower(), s.get("description", "").strip().lower()) for s in merged}

        def add(summary: str, description: str) -> None:
            key = (summary.strip().lower(), description.strip().lower())
            if not summary.strip() or not description.strip() or key in seen:
                return
            seen.add(key)
            merged.append({"summary": summary.strip(), "description": description.strip()})

        if has_backend and not self._has_domain_subtask(merged, backend_keywords):
            add(
                "[Backend] Backend API foundations",
                f"Implement backend API routes, services, and models for: {focus_text}.",
            )

        if has_frontend and not self._has_domain_subtask(merged, frontend_keywords):
            add(
                "[Frontend] Frontend UI foundations",
                f"Build Next.js pages/components and UI flows for: {focus_text}.",
            )

        return merged

    def _build_fallback_subtasks(
        self,
        clean_prd: str,
        technical_config: Dict[str, Any],
        min_subtasks: int,
        existing_subtasks: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        """Construct deterministic subtasks when AI work-plan generation is under-scoped."""
        merged: List[Dict[str, str]] = []
        seen = set()

        def add(summary: str, description: str) -> None:
            key = (summary.strip().lower(), description.strip().lower())
            if not summary.strip() or not description.strip() or key in seen:
                return
            seen.add(key)
            merged.append({"summary": summary.strip(), "description": description.strip()})

        for st in (existing_subtasks or []):
            add(st.get("summary", ""), st.get("description", ""))

        has_backend = bool((technical_config or {}).get("backend"))
        has_frontend = bool((technical_config or {}).get("frontend"))
        has_full = bool((technical_config or {}).get("full"))
        focus_areas = self._extract_prd_focus_areas(clean_prd)
        focus_text = "; ".join(focus_areas[:3]) if focus_areas else "core user journeys and required product flows"

        if has_backend:
            add(
                "[Backend] Backend foundation and dependencies",
                "Setup FastAPI backend project structure, configuration, environment variables, and required dependencies aligned to selected backend skill standards.",
            )
            add(
                "[Backend] Backend domain models and persistence",
                f"Implement backend data models/schemas and persistence layer for: {focus_text}. Ensure validation and serialization are consistent.",
            )
            add(
                "[Backend] Backend services and business logic",
                "Implement service layer, business rules, and reusable domain operations required by the PRD.",
            )
            add(
                "[Backend] Backend API routes and request contracts",
                "Implement API endpoints, request/response contracts, and error handling for all required backend flows.",
            )

        if has_frontend:
            add(
                "[Frontend] Frontend foundation and configuration",
                "Setup Next.js/TypeScript frontend configuration, build tooling, and shared project scaffolding aligned to selected frontend skill standards.",
            )
            add(
                "[Frontend] Frontend shared types and API client integration",
                "Implement shared frontend types and API client modules matching backend contracts.",
            )
            add(
                "[Frontend] Frontend pages and reusable components",
                f"Build pages/components for primary product journeys from PRD: {focus_text}.",
            )

        if has_backend and has_frontend:
            add(
                "[Fullstack] Frontend-backend integration",
                "Integrate frontend flows with backend endpoints, including loading/error states, data mapping, and contract validation.",
            )

        if has_full or (has_backend and has_frontend):
            add(
                "[Fullstack] Quality gates and validation",
                "Add static analysis checks, lint/type checks, and validation tasks to ensure generated code starts successfully without unresolved errors.",
            )

        # Ensure minimum count with PRD-driven implementation slices.
        idx = 1
        while len(merged) < min_subtasks:
            feature = focus_areas[(idx - 1) % len(focus_areas)] if focus_areas else f"PRD feature set #{idx}"
            add(
                f"Implementation slice {idx}: {feature[:60]}",
                f"Implement and validate this PRD requirement end-to-end: {feature}. Include code, integration, and verification updates.",
            )
            idx += 1

        return merged
