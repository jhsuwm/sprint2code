"""
DEPLOYMENT FIXER - Optimized with proper batching and retry logic
"""

import os
import re
import asyncio
import subprocess
import json
from typing import List, Dict, Any, Optional
from urllib.parse import unquote
from log_config import logger
from agents.job_manager import job_store
from agents.frontend_fixer import FrontendFixer
from agents.backend_fixer import BackendFixer

class DeploymentFixer:
    """Optimized auto-fixer with batching"""
    
    def __init__(self, job_manager, ai_service, github_service, jira_service):
        self.job_manager = job_manager
        self.ai_service = ai_service
        self.github_service = github_service
        self.jira_service = jira_service
        
        # Import validator for per-file validation
        from agents.deployment_validator import DeploymentValidator
        self.validator = DeploymentValidator()
        self.frontend_fixer = FrontendFixer(self)
        self.backend_fixer = BackendFixer(self)
        self._fix_attempt_history = {}
        self._fix_code_history = {}  # Track actual code sent in each attempt
        self._fix_error_history = {}  # Track errors present in each attempt
        self._last_cycle_files = {}  # Track which files were modified in the last cycle
        self._programmatic_fix_history = {}
        self._file_error_hashes = {}  # Track error signatures per file to detect no progress
        self._unfixable_files = {}  # Files that made things worse or showed no progress
        self._resurrection_count = {}  # Track how many times each file has been resurrected
        self._invalid_npm_packages = set()  # Track invalid npm packages reported by ETARGET
    
    def _safe_log(self, job_id: str, message: str, category: str = "Fix", level: str = "INFO"):
        """
        Safe logging that works even when called from parallel workers.
        Falls back to logger.info if job doesn't exist in job_manager.
        """
        try:
            # Try to log through job_manager first
            self.job_manager.log(job_id, message, category, level=level)
        except (KeyError, AttributeError):
            # Fall back to direct logging if job not in manager (e.g., from worker)
            logger.info(f"[Job {job_id}] [{category}] {message}")

    def _repo_prefixes(self, repo_dir: Optional[str]) -> Dict[str, str]:
        """Detect repo layout prefixes for frontend/backend paths."""
        if not repo_dir:
            return {"frontend": "frontend", "backend": "backend"}
        frontend_prefix = "frontend" if os.path.isdir(os.path.join(repo_dir, "frontend")) else ""
        backend_prefix = "backend" if os.path.isdir(os.path.join(repo_dir, "backend")) else ""
        return {"frontend": frontend_prefix, "backend": backend_prefix}

    def _frontend_prefix(self, repo_dir: Optional[str]) -> str:
        return self._repo_prefixes(repo_dir)["frontend"]

    def _backend_prefix(self, repo_dir: Optional[str]) -> str:
        return self._repo_prefixes(repo_dir)["backend"]

    def _apply_prefix(self, prefix: str, path: str) -> str:
        return f"{prefix}/{path}" if prefix else path

    def _normalize_repo_relative_path(self, path: str) -> str:
        """Normalize optional frontend/backend prefixes for robust file-path comparisons."""
        p = (path or "").strip().replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        if p.startswith("frontend/"):
            p = p[9:]
        elif p.startswith("backend/"):
            p = p[8:]
        # Normalize dotted backend module-path files, e.g. routes.ticket_routes.py -> routes/ticket_routes.py
        if p.endswith(".py") and "/" not in p and "." in p[:-3]:
            p = p[:-3].replace(".", "/") + ".py"
        return p

    def _is_frontend_alias_package(self, pkg: str) -> bool:
        """Return True if token is a frontend path alias/local spec, not an npm package."""
        p = (pkg or "").strip().strip("'\"")
        if not p:
            return False
        if p.startswith(('@/','./','../','~/', '#/', '/')) or p in ('@', '~', '#'):
            return True
        # Sprint2Code alias scopes in generated apps.
        if p.startswith('@api/'):
            return True
        # npm/package manager protocol specs should never appear as package names here.
        if ':' in p or '\\' in p:
            return True
        return False

    def _paths_match(self, candidate_path: str, target_path: str) -> bool:
        """Check whether candidate and target refer to the same repo-relative file."""
        c = self._normalize_repo_relative_path(candidate_path)
        t = self._normalize_repo_relative_path(target_path)
        return c == t or c.endswith(f"/{t}") or t.endswith(f"/{c}")

    def _canonical_path_key(self, path: str) -> str:
        """Create comparable module/file key across alias, extension, and prefix variants."""
        p = (path or "").strip().replace("\\", "/").strip("'\"")
        if p.startswith("./"):
            p = p[2:]
        if p.startswith("@/"):
            p = p[2:]
        if p.startswith("frontend/"):
            p = p[9:]
        elif p.startswith("backend/"):
            p = p[8:]
        if p.endswith(".py") and "/" not in p and "." in p[:-3]:
            p = p[:-3].replace(".", "/") + ".py"
        p = re.sub(r"\.(tsx?|jsx?|py)$", "", p)
        p = re.sub(r"/index$", "", p)
        return p

    def _error_mentions_file(self, error: str, target_file: str) -> bool:
        """Check whether an error string points to the target file using normalized path matching."""
        if not error or not target_file:
            return False

        target_norm = self._normalize_repo_relative_path(target_file)
        target_key = self._canonical_path_key(target_file)
        quoted_paths = re.findall(r"'([^']+)'", error)
        candidate_paths = list(quoted_paths)

        # Also capture leading absolute/relative path before "(line,col): error TS..."
        if "error TS" in error and "(" in error:
            left = error.split("(", 1)[0].strip()
            if left:
                candidate_paths.append(left)

        for candidate in candidate_paths:
            cand_norm = self._normalize_repo_relative_path(candidate)
            cand_key = self._canonical_path_key(candidate)
            if not cand_norm:
                continue
            if cand_norm == target_norm or cand_norm.endswith(f"/{target_norm}") or target_norm.endswith(f"/{cand_norm}"):
                return True
            if cand_key and target_key and (cand_key == target_key or cand_key.endswith(f"/{target_key}") or target_key.endswith(f"/{cand_key}")):
                return True

        # Fallback substring checks for legacy/non-standard error formatting
        return (
            target_file in error
            or target_norm in error.replace("\\", "/")
            or target_key in error.replace("\\", "/")
            or f"/{target_norm}" in error.replace("\\", "/")
        )

    def _target_repo_prefix(self, target_file: str, repo_dir: Optional[str] = None) -> str:
        """Infer repo prefix for a target path."""
        p = (target_file or "").replace("\\", "/")
        if p.startswith("frontend/"):
            return "frontend"
        if p.startswith("backend/"):
            return "backend"
        if p.endswith(".py"):
            return "backend" if self._backend_prefix(repo_dir) == "backend" else ""
        return "frontend" if self._frontend_prefix(repo_dir) == "frontend" else ""

    def _coerce_generated_path(self, generated_path: str, target_file: str, repo_dir: Optional[str] = None) -> str:
        """Coerce generated file path to full workspace-relative path with repo prefix."""
        gp = (generated_path or "").strip().replace("\\", "/")
        gp = gp[2:] if gp.startswith("./") else gp
        if gp.startswith(("frontend/", "backend/")):
            return gp
        prefix = self._target_repo_prefix(target_file, repo_dir)
        return self._apply_prefix(prefix, gp)

    def _is_cross_repo_generated_file(self, generated_full_path: str, target_file: str, repo_dir: Optional[str] = None) -> bool:
        """Reject generated files that cross repo boundaries for a single-file task."""
        target_prefix = self._target_repo_prefix(target_file, repo_dir)
        if not target_prefix:
            return False
        return not generated_full_path.startswith(f"{target_prefix}/")
    
    def update_error_history(self, job_id: str, all_errors: List[str], repo_dir: str):
        """Update error history for files modified in the last cycle"""
        if job_id not in self._last_cycle_files:
            return
            
        modified_files = self._last_cycle_files[job_id]
        if not modified_files:
            return
            
        # Parse current errors to see which files still have issues
        current_files_to_fix = self._parse_all_errors(all_errors, repo_dir)
        
        for file_path in modified_files:
            if file_path not in self._fix_error_history[job_id]:
                self._fix_error_history[job_id][file_path] = []
                
            if file_path in current_files_to_fix:
                # File still has errors - record them
                errors = current_files_to_fix[file_path].get('missing', [])
                if not errors:
                    # Might be a TypeScript error not parsed correctly into 'missing'
                    errors = [e for e in all_errors if self._error_mentions_file(e, file_path)]
                
                self._fix_error_history[job_id][file_path].append(errors or ["Unknown errors remaining"])
            else:
                # File no longer has errors!
                self._fix_error_history[job_id][file_path].append(["SUCCESS - all errors resolved for this file"])
        
        # Clear last cycle files
        self._last_cycle_files[job_id] = []

    def clear_unfixable_files(self, job_id: str):
        """Clear the unfixable files list AND reset attempt counters - gives all files a truly fresh chance"""
        if job_id in self._unfixable_files:
            # Get list of files that were unfixable before clearing
            unfixable_files = list(self._unfixable_files[job_id])
            self._unfixable_files[job_id].clear()
            
            # CRITICAL: Also reset attempt counters for those files
            # Otherwise they'll be immediately marked unfixable again
            if job_id in self._fix_attempt_history:
                for file_path in unfixable_files:
                    if file_path in self._fix_attempt_history[job_id]:
                        self._fix_attempt_history[job_id][file_path] = 0
    
    def proactively_fix_dependencies(self, repo_dir: str, all_errors: List[str]) -> bool:
        """Fix package.json structural issues"""
        modified = False
        pj_path, _ = self._find_dependency_file(repo_dir, 'package.json')
        if pj_path and os.path.exists(pj_path):
            try:
                with open(pj_path, 'r') as f:
                    data = json.load(f)
                scripts = data.get('scripts', {})
                if "Missing script: 'build'" in str(all_errors) and 'build' not in scripts:
                    scripts['build'] = "next build"
                    data['scripts'] = scripts
                    modified = True
                if modified:
                    with open(pj_path, 'w') as f:
                        json.dump(data, f, indent=2)
            except Exception as e:
                logger.warning(f"Failed to fix package.json: {e}")
        return modified
    
    async def auto_fix_static_analysis_errors(self, job_id, story_key, import_errors, github_repo, github_branch, repo_dir, cycle_number: int = 1) -> bool:
        """Main auto-fix with batching and retries"""
        try:
            # Get or create fix history for this job
            if job_id not in self._fix_attempt_history:
                self._fix_attempt_history[job_id] = {}
                self._fix_code_history[job_id] = {}
                self._fix_error_history[job_id] = {}
                self._last_cycle_files[job_id] = []
                self._programmatic_fix_history[job_id] = set()
                self._file_error_hashes[job_id] = {}
                self._unfixable_files[job_id] = set()
                self._cycle_count[job_id] = 0
            
            # Track cycle number
            self._cycle_count = getattr(self, '_cycle_count', {})
            if job_id not in self._cycle_count:
                self._cycle_count[job_id] = 0
            self._cycle_count[job_id] += 1
            current_cycle = self._cycle_count[job_id]
            
            job_fix_history = self._fix_attempt_history[job_id]
            job_programmatic_history = self._programmatic_fix_history[job_id]
            job_error_hashes = self._file_error_hashes[job_id]
            job_unfixable = self._unfixable_files[job_id]
            
            # Load story context (same for all files)
            story_context = self._load_story_context(job_id)
            
            # Parse errors into files to fix
            files_to_fix = self._parse_all_errors(import_errors, repo_dir)
            
            # Note: Test files are already filtered out by validator - only production code errors here
            
            if not files_to_fix:
                return False
            
            # Try programmatic fixes FIRST
            programmatic_count = 0
            for file_path, file_info in list(files_to_fix.items()):
                if file_path in job_programmatic_history:
                    continue
                
                if self._should_try_programmatic(file_path, file_info):
                    self.job_manager.log(job_id, f"🔧 Programmatic: {file_path}", "Prog")
                    if await self._apply_programmatic_fix(file_path, file_info, github_repo, github_branch, repo_dir, job_id):
                        self.job_manager.log(job_id, f"✅ Programmatic OK: {file_path}", "Prog")
                        job_programmatic_history.add(file_path)
                        programmatic_count += 1
                        del files_to_fix[file_path]
            
            # IMPROVED: Filter out files that have been tried too many times OR are unfixable
            # But be more lenient when showing dependency context
            filtered_files = {}
            for file_path, file_info in files_to_fix.items():
                attempts = job_fix_history.get(file_path, 0)
                
                # Check if errors are identical to last cycle (no progress)
                error_signature = hash(tuple(sorted(file_info.get('missing', []))))
                
                # CRITICAL FIX: If file is marked unfixable BUT errors changed, give it another chance!
                if file_path in job_unfixable:
                    # Check if errors changed since it was marked unfixable
                    if file_path in job_error_hashes and job_error_hashes[file_path] != error_signature:
                        # Errors changed! Remove from unfixable and reset attempt counter
                        job_unfixable.remove(file_path)
                        job_fix_history[file_path] = 0  # Reset attempts - fresh start
                        attempts = 0
                        self.job_manager.log(job_id, f"🔄 Retry {file_path} - errors changed since marked unfixable", "Retry Unfixable")
                    else:
                        self.job_manager.log(job_id, f"⏭️ Skip {file_path} - marked unfixable", "Skip")
                        continue
                
                if file_path in job_error_hashes:
                    if job_error_hashes[file_path] == error_signature:
                        # IMPROVED: With better dependency context, give more attempts (6 instead of 5)
                        if attempts >= 6:
                            self.job_manager.log(job_id, f"⏭️ Skip {file_path} - no progress after {attempts} attempts with same errors", "Skip")
                            job_unfixable.add(file_path)
                            continue
                        # If only 1-5 attempts, continue trying with better context
                        self.job_manager.log(job_id, f"Retry {file_path} (attempt {attempts + 1}, same errors - now with full dependency context)", "Retry")
                    else:
                        # Errors changed - update signature but DON'T reset attempts aggressively
                        # If errors keep changing without resolution, we're in a loop
                        job_error_hashes[file_path] = error_signature
                        # Only reset attempts if this is the FIRST attempt (attempts == 0)
                        # After that, keep counting to prevent infinite oscillation
                        if attempts == 0:
                            self.job_manager.log(job_id, f"🔄 {file_path} - errors changed on first attempt, allowing one retry", "First Retry")
                        else:
                            # Keep counting attempts even though errors changed
                            # This file is oscillating and needs to be stopped eventually
                            self.job_manager.log(job_id, f"🔄 {file_path} - errors changed but keeping attempt count ({attempts}) to prevent infinite loop", "Oscillating")
                else:
                    job_error_hashes[file_path] = error_signature
                
                # IMPROVED: With better context, allow up to 8 attempts (increased from 7)
                if attempts >= 8:
                    self.job_manager.log(job_id, f"⏭️ Skip {file_path} - max {attempts} attempts reached (oscillating errors)", "Skip")
                    job_unfixable.add(file_path)
                    continue
                
                # Don't increment counter yet - will increment after actual attempt
                filtered_files[file_path] = file_info
            
            if not filtered_files and programmatic_count == 0:
                # CRITICAL: Before stopping, check if we have unfixable files we could resurrect
                # But limit resurrections to prevent infinite loops
                if job_id not in self._resurrection_count:
                    self._resurrection_count[job_id] = 0
                
                if job_unfixable and len(files_to_fix) > 0 and self._resurrection_count[job_id] < 1:
                    self._resurrection_count[job_id] += 1
                    self.job_manager.log(job_id, f"🔄 All {len(files_to_fix)} error files marked unfixable - final resurrection attempt ({self._resurrection_count[job_id]}/1)", "Final Resurrection")
                    # Clear unfixable list and reset attempts for ALL files
                    unfixable_list = list(job_unfixable)
                    job_unfixable.clear()
                    for file_path in unfixable_list:
                        if file_path in job_fix_history:
                            job_fix_history[file_path] = 0
                    # Return True to trigger re-validation and another cycle
                    return True
                else:
                    if self._resurrection_count[job_id] >= 1:
                        self.job_manager.log(job_id, f"All files still unfixable after resurrection attempt - stopping", "Stop")
                    else:
                        self.job_manager.log(job_id, "All errors from previously-attempted files. Stopping.", "Stop")
                    return False
            
            if programmatic_count > 0:
                self.job_manager.log(job_id, f"✅ {programmatic_count} programmatic fixes", "Prog Done")
            
            if not filtered_files:
                return programmatic_count > 0
            
            # CRITICAL: STAGED FIXING - Fix dependencies first, then validate each file
            any_committed = False
            total_files = len(filtered_files)
            
            # Stage 1: Dependency files MUST be fixed first and completely
            dependency_files = {}
            type_files = {}
            implementation_files = {}
            
            for file_path, file_info in filtered_files.items():
                if file_path.endswith(('requirements.txt', 'package.json')):
                    dependency_files[file_path] = file_info
                elif '/types/' in file_path or '/models/' in file_path:
                    type_files[file_path] = file_info
                else:
                    implementation_files[file_path] = file_info
            
            # Process stages in order with immediate validation
            for stage_name, stage_files in [
                ("Dependencies", dependency_files),
                ("Types/Models", type_files),
                ("Implementation", implementation_files)
            ]:
                if not stage_files:
                    continue
                    
                self.job_manager.log(job_id, f"🎯 Stage: {stage_name} ({len(stage_files)} files)", "Staged Fixing")
                
                for file_path, file_info in stage_files.items():
                    self.job_manager.log(job_id, f"Fixing {file_path}...", "File Fix")
                    
                    # Load context for this specific file
                    yaml_context = self._load_yaml_context(job_id, repo_dir, file_path)
                    
                    # Try to fix the file
                    max_file_attempts = 3  # Each file gets 3 attempts max
                    file_fixed = False
                    
                    for file_attempt in range(1, max_file_attempts + 1):
                        # Generate fix but DON'T commit yet
                        fixed_content = await self._generate_file_fix(job_id, file_path, file_info, github_repo, github_branch, repo_dir, yaml_context, story_context)
                        
                        if fixed_content is None:  # Timeout or failed
                            self.job_manager.log(job_id, f"⏱️ {file_path}: Failed to generate fix on attempt {file_attempt}", "Gen Failed")
                            continue
                        
                        # Write to local file for validation
                        local_path = os.path.join(repo_dir, file_path)
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        with open(local_path, 'w') as f:
                            f.write(fixed_content)
                        
                        # CRITICAL: Validate BEFORE committing
                        self.job_manager.log(job_id, f"🔍 Validating {file_path} (attempt {file_attempt})...", "Pre-Commit Validation")
                        file_errors = self.validator.validate_all(repo_dir)
                        
                        # Check if THIS specific file still has errors
                        file_specific_errors = [e for e in file_errors if self._error_mentions_file(e, file_path)]
                        
                        if not file_specific_errors:
                            # ✅ Validation passed - NOW commit
                            self.job_manager.log(job_id, f"✅ {file_path}: Validation passed - committing...", "Committing")
                            
                            if await self._commit_file_fix(job_id, file_path, fixed_content, github_repo, github_branch):
                                self.job_manager.log(job_id, f"✅ {file_path}: VERIFIED and COMMITTED", "Success")
                                any_committed = True
                                file_fixed = True
                                
                                # Update attempt counter
                                attempts = job_fix_history.get(file_path, 0)
                                job_fix_history[file_path] = attempts + file_attempt
                                break
                            else:
                                self.job_manager.log(job_id, f"❌ {file_path}: Commit failed", "Commit Failed")
                                continue
                        else:
                            self.job_manager.log(job_id, f"⚠️ {file_path}: Still has {len(file_specific_errors)} errors - NOT committing", "Validation Failed")
                            self.job_manager.log(job_id, f"Errors: {file_specific_errors[:3]}", "Error Details")

                            # Check for cross-file dependency errors (missing exports)
                            dependency_files = self._extract_dependency_files_from_errors(file_specific_errors, file_path, repo_dir)
                            if dependency_files and file_attempt < max_file_attempts:
                                # CRITICAL: Fix dependencies IMMEDIATELY before next retry
                                self.job_manager.log(job_id, f"🔍 Detected {len(dependency_files)} dependency files - fixing NOW before retry", "Dependency Fix")
                                
                                dep_fixed_count = 0
                                for dep_file, missing_exports in dependency_files.items():
                                    self.job_manager.log(job_id, f"  📄 Fixing {dep_file}: Missing {missing_exports}", "Dep")
                                    
                                    # Load dependency file
                                    local = os.path.join(repo_dir, dep_file)
                                    content = ''
                                    if os.path.exists(local):
                                        with open(local, 'r') as f:
                                            content = f.read()
                                    
                                    dep_file_info = {
                                        'missing': [f"Add missing export(s): {', '.join(missing_exports)}"],
                                        'content': content
                                    }
                                    
                                    # Generate and commit fix for dependency immediately
                                    yaml_ctx = self._load_yaml_context(job_id, repo_dir, dep_file)
                                    dep_content = await self._generate_file_fix(job_id, dep_file, dep_file_info, github_repo, github_branch, repo_dir, yaml_ctx, story_context)
                                    
                                    if dep_content:
                                        # Write locally
                                        dep_local = os.path.join(repo_dir, dep_file)
                                        os.makedirs(os.path.dirname(dep_local), exist_ok=True)
                                        with open(dep_local, 'w') as f:
                                            f.write(dep_content)
                                        
                                        # Commit immediately
                                        if await self._commit_file_fix(job_id, dep_file, dep_content, github_repo, github_branch):
                                            self.job_manager.log(job_id, f"  ✅ Fixed and committed {dep_file}", "Dep Fixed")
                                            dep_fixed_count += 1
                                        else:
                                            self.job_manager.log(job_id, f"  ❌ Failed to commit {dep_file}", "Dep Failed")
                                
                                if dep_fixed_count > 0:
                                    # Pull latest changes before retrying original file
                                    await asyncio.sleep(1)
                                    subprocess.run(['git', 'fetch', 'origin', github_branch], cwd=repo_dir, capture_output=True, timeout=60)
                                    subprocess.run(['git', 'reset', '--hard', f'origin/{github_branch}'], cwd=repo_dir, capture_output=True, timeout=30)
                                    self.job_manager.log(job_id, f"  🔄 Fixed {dep_fixed_count} dependencies - retrying {file_path}", "Retry with Deps")
                                    # Continue to next file_attempt iteration which will retry

                            # Update file_info with remaining errors for next attempt
                            file_info['missing'] = file_specific_errors

                            if file_attempt >= max_file_attempts:
                                self.job_manager.log(job_id, f"❌ {file_path}: Failed after {max_file_attempts} attempts - NO CODE COMMITTED", "File Failed")
                                attempts = job_fix_history.get(file_path, 0)
                                job_fix_history[file_path] = attempts + file_attempt
                    
                    if not file_fixed:
                        self.job_manager.log(job_id, f"⏭️ {file_path}: Moving to next file (will retry in next cycle)", "Skip to Next")
            
            if any_committed or programmatic_count > 0:
                if any_committed:
                    await asyncio.sleep(2)
                    subprocess.run(['git', 'fetch', 'origin', github_branch], cwd=repo_dir, capture_output=True, timeout=60)
                    subprocess.run(['git', 'reset', '--hard', f'origin/{github_branch}'], cwd=repo_dir, capture_output=True, timeout=30)
                return True
            
            return False
        except Exception as e:
            logger.error(f"Auto-fix failed: {e}")
            return False
    
    def _create_smart_batches(self, files_to_fix: Dict[str, Dict]) -> List[List[tuple]]:
        """Group files into intelligent batches for parallel processing with size limits"""
        # Categorize files by type AND size
        type_files = []
        model_files = []
        api_files = []
        component_files = []
        config_files = []
        large_files = []  # Files too large for batching
        other_files = []
        
        for file_path, file_info in files_to_fix.items():
            item = (file_path, file_info)
            content_size = len(file_info.get('content') or '')
            
            # Files > 5000 chars should be processed individually to avoid MAX_TOKENS
            if content_size > 5000:
                large_files.append(item)
                continue
            
            if '/types/' in file_path and file_path.endswith('.ts'):
                type_files.append(item)
            elif '/models/' in file_path and file_path.endswith('.py'):
                model_files.append(item)
            elif '/api/' in file_path or 'api' in file_path.lower():
                api_files.append(item)
            elif file_path.endswith(('.tsx', '.jsx')) and '/components/' in file_path:
                component_files.append(item)
            elif file_path.endswith(('.json', '.txt', '.yaml')):
                config_files.append(item)
            else:
                other_files.append(item)
        
        # Create batches with smart grouping and SIZE LIMITS to prevent MAX_TOKENS
        batches = []
        
        # Config files first (quick programmatic fixes) - max 3 per batch
        if config_files:
            batches.append(config_files[:3])
            if len(config_files) > 3:
                for i in range(3, len(config_files), 3):
                    batches.append(config_files[i:i+3])
        
        # Type files together (small files) - max 2 per batch (reduced from 3)
        for i in range(0, len(type_files), 2):
            batches.append(type_files[i:i+2])
        
        # Model files together - max 2 per batch (reduced from 3)
        for i in range(0, len(model_files), 2):
            batches.append(model_files[i:i+2])
        
        # API files - ONLY 1 per batch (API files are often large)
        for item in api_files:
            batches.append([item])
        
        # Component files - ONLY 1 per batch (components are large!)
        for item in component_files:
            batches.append([item])
        
        # Large files - ONLY 1 per batch
        for item in large_files:
            batches.append([item])
        
        # Other files individually
        for item in other_files:
            batches.append([item])
        
        return [b for b in batches if b]  # Remove empty batches
    
    async def _fix_batch(self, job_id: str, batch: List[tuple], github_repo: str, github_branch: str, repo_dir: str, yaml_context: str = "", story_context: str = "") -> List[bool]:
        """Fix multiple files in a batch - returns list of success flags"""
        results = []
        
        # If batch has only 1 file, use single file logic
        if len(batch) == 1:
            file_path, file_info = batch[0]
            result = await self._fix_single_file(job_id, file_path, file_info, github_repo, github_branch, repo_dir, yaml_context, story_context)
            return [result]
        
        # For multiple files, create a combined prompt with context
        # Check history for files in batch
        fix_history = self._fix_attempt_history.get(job_id, {})
        code_history = self._fix_code_history.get(job_id, {})
        error_history = self._fix_error_history.get(job_id, {})
        
        combined_prompt = self._build_batch_fix_prompt(batch, yaml_context, story_context, fix_history, code_history, error_history, repo_dir)
        
        max_retries = 2
        for retry in range(max_retries):
            try:
                # OPTIMIZATION D: Reduced timeout for faster failure
                timeout = 30.0  # Reduced from 45s for faster failure
                
                result = await self.ai_service.generate_code(
                    task_description=f"Fix {len(batch)} files",
                    context=combined_prompt,
                    story_context="",
                    attachments=None,
                    temperature=0.0,  # Deterministic for consistent, conservative fixes
                    timeout=timeout,
                    max_output_tokens=65536
                )
                
                code = result[0] if isinstance(result, tuple) else result
                finish_reason = result[1] if isinstance(result, tuple) else 'STOP'
                
                if finish_reason == 'MAX_TOKENS':
                    self.job_manager.log(job_id, f"⚠️ Batch MAX_TOKENS - splitting into individual files", "Batch Split", level="WARNING")
                    
                    # FALLBACK: Split batch and process each file individually
                    individual_results = []
                    for file_path, file_info in batch:
                        self.job_manager.log(job_id, f"Processing {file_path} individually (MAX_TOKENS fallback)", "Individual Fix")
                        result = await self._fix_single_file(job_id, file_path, file_info, github_repo, github_branch, repo_dir)
                        individual_results.append(result)
                    return individual_results
                
                parsed = self.ai_service.parse_generated_code(code)
                if not parsed:
                    self.job_manager.log(job_id, f"❌ Batch: Failed to parse (retry {retry+1}/{max_retries})", "Parse Failed", level="WARNING")
                    if retry < max_retries - 1:
                        continue
                    return [False] * len(batch)
                
                # Commit all files in batch
                job_data = job_store.get(job_id, {})
                all_repos = job_data.get("all_repos", [])
                
                for f in parsed:
                    path, content = f['file_path'], f['content']
                    
                    # Route to correct repo
                    target_repo = github_repo
                    if path.endswith('.py') or 'backend/' in path.lower():
                        backend_repo = next((r for r in all_repos if r.get('type') == 'backend'), None)
                        if backend_repo:
                            target_repo = f"{backend_repo['owner']}/{backend_repo['repo']}"
                    elif path.endswith(('.tsx', '.ts', '.jsx', '.js')) or 'frontend/' in path.lower():
                        frontend_repo = next((r for r in all_repos if r.get('type') == 'frontend'), None)
                        if frontend_repo:
                            target_repo = f"{frontend_repo['owner']}/{frontend_repo['repo']}"
                    
                    # FIXED: Normalize path - remove ONLY top-level repo directory prefix
                    final_path = path
                    owner, repo_name = target_repo.split('/')
                    repo_type = next((r.get('type') for r in all_repos if f"{r['owner']}/{r['repo']}" == target_repo), 'unknown')
                    
                    # Remove ONLY the top-level directory prefix (backend/ or frontend/)
                    # Do NOT remove src/ as it is usually part of the repo structure
                    if final_path.startswith('backend/'):
                        final_path = final_path[8:]
                    elif final_path.startswith('frontend/'):
                        final_path = final_path[9:]
                    
                    if self.github_service.commit_file(owner, repo_name, github_branch, final_path, content, f"[BATCH-FIX] {final_path}"):
                        self.job_manager.log(job_id, f"✅ Batch fixed: {final_path}", "Batch Commit")
                        
                        # Sync locally
                        full_path = os.path.join(repo_dir, path)
                        os.makedirs(os.path.dirname(full_path), exist_ok=True)
                        with open(full_path, 'w') as file:
                            file.write(content)
                        
                        # Record attempt in history
                        if path not in self._fix_code_history[job_id]: self._fix_code_history[job_id][path] = []
                        self._fix_code_history[job_id][path].append(content)
                        self._last_cycle_files[job_id].append(path)

                        # Mark as success for this file
                        for i, (batch_path, _) in enumerate(batch):
                            if batch_path in path or path in batch_path:
                                results.append(True)
                                break
                
                # If we got here, return results (pad with True if needed)
                while len(results) < len(batch):
                    results.append(True)
                return results
                
            except asyncio.TimeoutError:
                self.job_manager.log(job_id, f"⏱️ Batch timeout on retry {retry+1}/{max_retries}", "Timeout", level="WARNING")
                if retry < max_retries - 1:
                    continue
                return [False] * len(batch)
            except Exception as e:
                self.job_manager.log(job_id, f"❌ Batch exception on retry {retry+1}/{max_retries}: {str(e)[:100]}", "Exception", level="ERROR")
                if retry < max_retries - 1:
                    continue
                return [False] * len(batch)
        
        return [False] * len(batch)
    
    @staticmethod
    def _strip_skill_frontmatter(content: str) -> str:
        """Strip YAML frontmatter from SKILL.md content.

        Per the agent skills spec, the frontmatter block (between ``---`` delimiters)
        is machine-parseable metadata (name, type, github_repository, …) and should
        NOT be sent to the AI as instructional context.  Only the Markdown body that
        follows the closing ``---`` is human-readable skill guidance intended for AI.
        """
        return re.sub(r"^\s*---\s*\n.*?\n---\s*\n?", "", content, flags=re.DOTALL).strip()

    def _load_yaml_context(self, job_id: str, repo_dir: str, file_path: str = None) -> str:
        """Load technical context (skills/config) from job store - scoped by file type.

        Only the human-readable *body* of each SKILL.md is forwarded to the AI.
        The YAML frontmatter (name, type, github_repository, …) is metadata for the
        runtime and must be stripped before the content reaches the AI model.
        """
        try:
            context = ""
            job_data = job_store.get(job_id, {})
            
            # Get technical_config dict which contains frontend/backend skill/config context.
            technical_config = job_data.get("technical_config", {})
            
            # Determine if this is a frontend or backend file
            is_frontend = False
            is_backend = False
            
            if file_path:
                # Check file type based on path and extension
                if file_path.startswith('frontend/') or file_path.endswith(('.ts', '.tsx', '.jsx', '.js', '.css', '.html')):
                    is_frontend = True
                elif file_path.startswith('backend/') or file_path.endswith('.py'):
                    is_backend = True
            
            # If can't determine, include both (fallback)
            if not is_frontend and not is_backend:
                is_frontend = True
                is_backend = True
            
            # Load ONLY relevant skill body to reduce context size.
            # Strip YAML frontmatter — it is machine-readable metadata, not AI instructions.
            if is_frontend:
                frontend_raw = technical_config.get("frontend", "")
                if frontend_raw:
                    frontend_body = self._strip_skill_frontmatter(frontend_raw)
                    if frontend_body:
                        context += "=== FRONTEND SKILL REQUIREMENTS ===\n"
                        context += frontend_body[:4000]
                        context += "\n\n"
            
            if is_backend:
                backend_raw = technical_config.get("backend", "")
                if backend_raw:
                    backend_body = self._strip_skill_frontmatter(backend_raw)
                    if backend_body:
                        context += "=== BACKEND SKILL REQUIREMENTS ===\n"
                        context += backend_body[:4000]
                        context += "\n\n"
            
            return context
        except Exception as e:
            logger.warning(f"Failed to load technical context: {e}")
            return ""
    
    def _load_story_context(self, job_id: str) -> str:
        """Load story/PRD context from job store"""
        try:
            job_data = job_store.get(job_id, {})
            
            context = "=== PRODUCT REQUIREMENTS ===\n"
            
            # Story description
            story_description = job_data.get("story_description", "")
            if story_description:
                context += f"Story: {story_description[:500]}\n\n"
            
            # Epic/project context
            epic_key = job_data.get("epic_key", "")
            if epic_key:
                context += f"Epic: {epic_key}\n"
            
            return context
        except Exception as e:
            logger.warning(f"Failed to load story context: {e}")
            return ""
    
    def _build_batch_fix_prompt(self, batch: List[tuple], yaml_context: str = "", story_context: str = "", fix_history: dict = None, code_history: dict = None, error_history: dict = None, repo_dir: str = None) -> str:
        """Build prompt for fixing multiple files at once with full context and history"""
        prompt = "You are a senior software engineer fixing bugs in an autonomous development environment.\n\n"
        prompt += "REPOSITORY PATH RULE: Use repository-root FILE_PATH values only.\n"
        prompt += "Do NOT prefix FILE_PATH with 'backend/' or 'frontend/'.\n\n"
        
        # Add context FIRST so AI understands the requirements
        if yaml_context:
            prompt += yaml_context
        
        if story_context:
            prompt += story_context + "\n"
        
        prompt += f"\n{'='*80}\n"
        prompt += f"Fix errors in {len(batch)} files according to the above requirements.\n"
        prompt += "Be CONCISE in output to avoid MAX_TOKENS.\n\n"
        
        total_content_shown = 0
        max_total_content = 4000  # Cap total content across all files
        
        for file_path, file_info in batch:
            prompt += f"\n{'='*60}\n"
            prompt += f"FILE: {file_path}\n"
            prompt += f"{'='*60}\n"

            # NEW: Show history for this specific file in the batch
            if code_history and file_path in code_history and error_history and file_path in error_history:
                prompt += "\n[PREVIOUS FAILED ATTEMPT FOR THIS FILE]\n"
                prev_code = code_history[file_path][-1]
                prev_errors = error_history[file_path][-1]
                for err in prev_errors[:3]:
                    prompt += f"  - {err}\n"
                snippet = prev_code
                if len(snippet) > 600:
                    snippet = snippet[:300] + "\n... [truncated] ...\n" + snippet[-300:]
                prompt += f"Previous Code Snippet:\n```\n{snippet}\n```\n"

            # Show errors (limit to 5 most important)
            if file_info.get('missing'):
                prompt += "Errors:\n"
                for err in file_info['missing'][:5]:  # Reduced from 10 to 5
                    prompt += f"  - {err}\n"
            
            # Show packages
            if 'packages' in file_info and file_info['packages']:
                prompt += f"Required packages: {', '.join(list(file_info['packages'])[:3])}\n"
            
            # NEW: Add Dependency Context to batch items
            content = file_info.get('content', '')
            if content and repo_dir:
                imports = self._extract_imports(content, file_path)
                dep_context = self._get_dependency_context(imports, repo_dir, file_path)
                if dep_context:
                    prompt += dep_context

            # Show content with AGGRESSIVE truncation per file and overall
                # Calculate how much content we can show for this file
                remaining_budget = max_total_content - total_content_shown
                max_this_file = min(1200, remaining_budget)  # Max 1200 chars per file, or less if budget low
                
                if len(content) > max_this_file and max_this_file > 0:
                    prompt += f"\nCurrent content (truncated to {max_this_file} chars):\n```\n{content[:max_this_file]}\n... [truncated]\n```\n"
                    total_content_shown += max_this_file
                elif max_this_file > 0:
                    prompt += f"\nCurrent content:\n```\n{content}\n```\n"
                    total_content_shown += len(content)
                else:
                    prompt += "\n⚠️ Content truncated (input budget exceeded)\n"
            else:
                prompt += "\n⚠️ File doesn't exist - CREATE IT\n"
        
        prompt += f"\n{'='*60}\n"
        prompt += "⚠️ IMPORTANT: Keep fixes CONCISE to avoid MAX_TOKENS!\n"
        prompt += "### INSTRUCTIONS ###\n"
        prompt += "1. Analyze each file and its history.\n"
        prompt += "2. Use Chain of Thought: Briefly explain your overall strategy.\n"
        prompt += "3. Output ALL fixed files in this format:\n\n"
        prompt += "PLAN: <strategy description>\n\n"
        prompt += "FILE_PATH: <path>\n---\n<complete code>\n---\n\n"
        prompt += "Repeat the FILE_PATH block for each file.\n"
        return prompt
    
    async def _generate_file_fix_bundle(self, job_id: str, target_file: str, info: dict, github_repo: str, github_branch: str, repo_dir: str, yaml_context: str = "", story_context: str = "") -> Optional[Dict[str, Any]]:
        """Generate fix bundle with target content and companion files from same repo."""
        job_fix_history = self._fix_attempt_history.get(job_id, {})
        code_history = self._fix_code_history.get(job_id, {}).get(target_file, [])
        error_history = self._fix_error_history.get(job_id, {}).get(target_file, [])
        
        prompt = self._build_fix_prompt(target_file, info, yaml_context, story_context, job_fix_history, code_history, error_history, repo_dir)
        
        try:
            content_size = len(info.get('content', ''))
            base_timeout = 60.0 if target_file.endswith(('.tsx', '.jsx', '.ts')) else 45.0
            timeout = base_timeout + (content_size / 1000) * 10.0
            timeout = min(timeout, 150.0)
            
            result = await self.ai_service.generate_code(
                task_description=f"Fix {target_file}",
                context=prompt,
                story_context="",
                attachments=None,
                temperature=0.0,
                timeout=timeout,
                max_output_tokens=65536
            )
            
            code = result[0] if isinstance(result, tuple) else result
            finish_reason = result[1] if isinstance(result, tuple) else 'STOP'
            
            if finish_reason == 'MAX_TOKENS':
                return None
            
            parsed = self.ai_service.parse_generated_code(code)
            if not parsed:
                fallback_content = self._fallback_extract_target_content(code, target_file, repo_dir)
                if fallback_content:
                    self._safe_log(
                        job_id,
                        f"⚠️ AI output parse fallback applied for {target_file} (using inferred code block)",
                        "Parse Fallback",
                        level="WARNING"
                    )
                    return {'target_content': fallback_content, 'related_files': []}
                return None

            target_content = None
            related_files: List[Dict[str, str]] = []

            for parsed_file in parsed:
                raw_path = parsed_file.get('file_path', '')
                content = parsed_file.get('content')
                if not raw_path or content is None:
                    continue
                full_path = self._coerce_generated_path(raw_path, target_file, repo_dir)

                # Never apply cross-repo files in this context.
                if self._is_cross_repo_generated_file(full_path, target_file, repo_dir):
                    continue

                if self._paths_match(full_path, target_file):
                    target_content = content
                    continue

                # Avoid dependency churn from incidental package/lock edits here.
                if full_path.endswith(("package.json", "package-lock.json", "requirements.txt")):
                    continue

                related_files.append({'file_path': full_path, 'content': content})

            if target_content is None:
                if len(parsed) == 1:
                    raw_path = parsed[0].get('file_path', '')
                    full_path = self._coerce_generated_path(raw_path, target_file, repo_dir)
                    if raw_path and self._paths_match(full_path, target_file):
                        target_content = parsed[0].get('content')
                    else:
                        self._safe_log(
                            job_id,
                            f"⚠️ AI output block path mismatch for {target_file} (got '{raw_path}'). Discarding.",
                            "Parse Mismatch",
                            level="WARNING"
                        )
                        return None
                else:
                    self._safe_log(job_id, f"⚠️ AI output missing target file block for {target_file}", "Parse Mismatch", level="WARNING")
                    return None

            deduped_related = []
            seen = set()
            for entry in related_files:
                fp = entry['file_path']
                if fp in seen or self._paths_match(fp, target_file):
                    continue
                seen.add(fp)
                deduped_related.append(entry)

            return {'target_content': target_content, 'related_files': deduped_related}
            
        except Exception as e:
            logger.error(f"Generate fix failed for {target_file}: {e}")
            return None

    def _strip_code_wrappers(self, content: str) -> str:
        text = (content or "").strip()
        if not text:
            return ""
        text = re.sub(r'^```(?:\w+)?\n', '', text)
        text = re.sub(r'\n```$', '', text)
        text = re.sub(r'^-{3,}\n', '', text)
        text = re.sub(r'\n-{3,}$', '', text)
        return text.strip()

    def _looks_like_code(self, text: str) -> bool:
        if not text:
            return False
        snippet = text[:2000]
        tokens = ("import ", "export ", "const ", "function ", "class ", "interface ", "type ", "from ")
        if any(tok in snippet for tok in tokens) and snippet.count("\n") >= 3:
            return True
        if "{" in snippet and "}" in snippet and ";" in snippet and snippet.count("\n") >= 3:
            return True
        return False

    def _fallback_extract_target_content(self, response: str, target_file: str, repo_dir: str) -> Optional[str]:
        """Recover code when AI output omits strict FILE_PATH/--- formatting."""
        if not response:
            return None

        # Attempt FILE_PATH blocks without separators.
        loose_pattern = re.compile(
            r"(?:^|\n)\s*(?:FILE_PATH:|FILE:)\s*(?P<path>[^\n]+)\n(?P<content>.*?)(?=(?:\n\s*(?:FILE_PATH:|FILE:)|\Z))",
            re.DOTALL
        )
        loose_matches = list(loose_pattern.finditer(response))
        if loose_matches:
            # Prefer the block whose path matches the target.
            for m in loose_matches:
                raw_path = m.group('path').strip()
                content = self._strip_code_wrappers(m.group('content'))
                full_path = self._coerce_generated_path(raw_path, target_file, repo_dir)
                if self._paths_match(full_path, target_file) and content:
                    return content
            # Fallback to the first block if there is only one.
            if len(loose_matches) == 1:
                return self._strip_code_wrappers(loose_matches[0].group('content'))

        # Attempt to recover from markdown code fences.
        fence_blocks = re.findall(r"```(?:\w+)?\n([\s\S]*?)\n```", response)
        if fence_blocks:
            candidate = max(fence_blocks, key=len)
            candidate = self._strip_code_wrappers(candidate)
            if candidate:
                return candidate

        # Last resort: strip common headings and use raw text if it looks like code.
        cleaned = re.sub(r"(?m)^(ROOT_CAUSE|COMPLETE_FIX|VERIFICATION|PLAN):.*$", "", response).strip()
        cleaned = self._strip_code_wrappers(cleaned)
        if self._looks_like_code(cleaned):
            return cleaned
        return None

    async def _generate_file_fix(self, job_id: str, target_file: str, info: dict, github_repo: str, github_branch: str, repo_dir: str, yaml_context: str = "", story_context: str = "") -> Optional[str]:
        """Generate fixed content for a file without committing - returns content or None."""
        bundle = await self._generate_file_fix_bundle(
            job_id, target_file, info, github_repo, github_branch, repo_dir, yaml_context, story_context
        )
        if not bundle:
            return None
        return bundle.get('target_content')
    
    async def _commit_file_fix(self, job_id: str, file_path: str, content: str, github_repo: str, github_branch: str) -> bool:
        """Commit a fixed file to GitHub"""
        try:
            job_data = job_store.get(job_id, {})
            all_repos = job_data.get("all_repos", [])
            
            # Route to correct repo
            target_repo = github_repo
            if file_path.endswith('.py') or 'backend/' in file_path.lower():
                backend_repo = next((r for r in all_repos if r.get('type') == 'backend'), None)
                if backend_repo:
                    target_repo = f"{backend_repo['owner']}/{backend_repo['repo']}"
            elif file_path.endswith(('.tsx', '.ts', '.jsx', '.js')) or 'frontend/' in file_path.lower():
                frontend_repo = next((r for r in all_repos if r.get('type') == 'frontend'), None)
                if frontend_repo:
                    target_repo = f"{frontend_repo['owner']}/{frontend_repo['repo']}"
            
            # Normalize path
            final_path = file_path
            if final_path.startswith('backend/'):
                final_path = final_path[8:]
            elif final_path.startswith('frontend/'):
                final_path = final_path[9:]
            
            owner, repo_name = target_repo.split('/')
            
            return self.github_service.commit_file(owner, repo_name, github_branch, final_path, content, f"[STAGED-FIX] {final_path}")
            
        except Exception as e:
            logger.error(f"Commit failed for {file_path}: {e}")
            return False

    async def _commit_programmatic_fix(self, job_id: str, file_path: str, content: str, github_repo: str, github_branch: str, message_prefix: str = "[PROG]") -> bool:
        """Commit helper for programmatic fixes with correct multi-repo routing."""
        try:
            success = await self._commit_file_fix(job_id, file_path, content, github_repo, github_branch)
            if not success:
                self._safe_log(job_id, f"❌ Failed to commit programmatic fix for {file_path}", "Programmatic Commit", level="WARNING")
            return success
        except Exception as e:
            logger.error(f"Programmatic commit failed for {file_path}: {e}")
            return False
    
    async def _fix_single_file(self, job_id: str, target_file: str, info: dict, github_repo: str, github_branch: str, repo_dir: str, yaml_context: str = "", story_context: str = "") -> bool:
        """Fix a single file with retries - FAST FAIL approach"""
        max_retries = 2  # Reduced from 3 - fail faster
        
        # Ensure history structures exist for this job
        if job_id not in self._fix_code_history: self._fix_code_history[job_id] = {}
        if job_id not in self._fix_error_history: self._fix_error_history[job_id] = {}
        
        for retry in range(max_retries):
            # Build prompt with full context including fix history
            job_fix_history = self._fix_attempt_history.get(job_id, {})
            code_history = self._fix_code_history[job_id].get(target_file, [])
            error_history = self._fix_error_history[job_id].get(target_file, [])
            
            prompt = self._build_fix_prompt(target_file, info, yaml_context, story_context, job_fix_history, code_history, error_history, repo_dir)
            
            try:
                # Dynamically calculate timeout based on file size and retry
                content_size = len(info.get('content', ''))
                base_timeout = 60.0 if target_file.endswith(('.tsx', '.jsx', '.ts')) else 45.0
                timeout = base_timeout + (content_size / 1000) * 10.0
                if retry > 0: timeout += 30.0 # More time on retries
                timeout = min(timeout, 150.0) # Cap at 2.5 minutes
                
                result = await self.ai_service.generate_code(
                    task_description=f"Fix {target_file}",
                    context=prompt,
                    story_context="",
                    attachments=None,
                    temperature=0.0,  # Deterministic for consistent, conservative fixes
                    timeout=timeout,
                    max_output_tokens=65536
                )
                
                code = result[0] if isinstance(result, tuple) else result
                finish_reason = result[1] if isinstance(result, tuple) else 'STOP'
                
                if finish_reason == 'MAX_TOKENS':
                    self.job_manager.log(job_id, f"❌ {target_file}: MAX_TOKENS on retry {retry+1}/{max_retries}", "Fix Failed", level="WARNING")
                    if retry < max_retries - 1:
                        continue
                    else:
                        return False
                
                parsed = self.ai_service.parse_generated_code(code)
                if not parsed:
                    self.job_manager.log(job_id, f"❌ {target_file}: Failed to parse AI output (retry {retry+1}/{max_retries})", "Parse Failed", level="WARNING")
                    if retry < max_retries - 1:
                        continue
                    else:
                        return False
                
                if parsed:
                    target_entry = next((f for f in parsed if self._paths_match(f.get('file_path', ''), target_file)), None)
                    if target_entry is None:
                        if len(parsed) == 1:
                            target_entry = parsed[0]
                        else:
                            self.job_manager.log(job_id, f"❌ {target_file}: AI response missing target file block", "Parse Failed", level="WARNING")
                            continue

                    content = target_entry['content']
                    if await self._commit_file_fix(job_id, target_file, content, github_repo, github_branch):
                        self.job_manager.log(job_id, f"Successfully committed fix for {target_file}", "Fix Applied")

                        # Sync locally to the intended target file.
                        full_path = os.path.join(repo_dir, target_file)
                        os.makedirs(os.path.dirname(full_path), exist_ok=True)
                        with open(full_path, 'w') as file:
                            file.write(content)

                        # Record attempt in history
                        if target_file not in self._fix_code_history[job_id]:
                            self._fix_code_history[job_id][target_file] = []
                        self._fix_code_history[job_id][target_file].append(content)
                        self._last_cycle_files[job_id].append(target_file)
                        return True
                    continue
            except asyncio.TimeoutError:
                self.job_manager.log(job_id, f"⏱️ {target_file}: Timeout on retry {retry+1}/{max_retries} ({timeout}s)", "Timeout", level="WARNING")
                if retry < max_retries - 1:
                    continue
                else:
                    # TIMEOUT: Return None instead of False to signal this wasn't a real failure
                    return None
            except Exception as e:
                error_str = str(e)
                # Check if this is an AI timeout (not asyncio timeout)
                if "TIMEOUT" in error_str.upper() or "did not respond" in error_str.lower():
                    self.job_manager.log(job_id, f"⏱️ {target_file}: AI service timeout on retry {retry+1}/{max_retries}", "AI Timeout", level="WARNING")
                    if retry < max_retries - 1:
                        continue
                    else:
                        # AI TIMEOUT: Return None instead of False
                        return None
                else:
                    self.job_manager.log(job_id, f"❌ {target_file}: Exception on retry {retry+1}/{max_retries}: {error_str[:100]}", "Exception", level="ERROR")
                    if retry < max_retries - 1:
                        continue
                    else:
                        return False
        
        return False
    
    def _extract_imports(self, content: str, file_path: str) -> List[str]:
        """Extract imported modules/files from content"""
        imports = []
        if file_path.endswith('.py'):
            # Python imports
            matches = re.findall(r'^(?:from|import)\s+([\w\.]+)', content, re.MULTILINE)
            imports.extend(matches)
        else:
            # TS/JS imports
            matches = re.findall(r"import\s+.*\s+from\s+['\"](.+)['\"]", content)
            imports.extend(matches)
        return list(set(imports))

    def _get_dependency_context(self, imports: List[str], repo_dir: str, current_file: str) -> str:
        """Get context from imported files (class/function definitions)"""
        context = "### DEPENDENCY CONTEXT (Imported modules) ###\n"
        found = False
        
        for imp in imports[:5]: # Limit to first 5 imports to save context
            target_path = self._resolve_import_path(current_file, imp, repo_dir)
            full_path = os.path.join(repo_dir, target_path)
            
            if os.path.exists(full_path):
                try:
                    with open(full_path, 'r') as f:
                        lines = f.readlines()
                    
                    # Extract definitions (class, def, interface, export)
                    defs = []
                    for line in lines:
                        if re.match(r'^(class|def|export|interface|type|const|function)\s', line.strip()):
                            defs.append(line.strip())
                    
                    if defs:
                        context += f"\nFile: {target_path}\nDefinitions:\n"
                        context += "\n".join(defs[:15]) + "\n"
                        found = True
                except Exception:
                    pass
        
        return context + "\n" if found else ""
    
    def _get_full_dependency_context(self, imports: List[str], repo_dir: str, current_file: str) -> str:
        """Get FULL content from the most critical imported files to help AI understand dependencies"""
        context = "\n### 📚 FULL DEPENDENCY FILES (What you're importing FROM) ###\n"
        context += "⚠️ Check these files to ensure the items you're importing actually exist!\n\n"
        found = False
        
        # Prioritize local imports (not node_modules or stdlib)
        local_imports = [imp for imp in imports if imp.startswith(('.', '@/')) or (not imp.startswith(('@testing', 'react', 'next', 'jose', 'bcrypt', 'firebase')))]
        
        for imp in local_imports[:3]:  # Show FULL content of up to 3 most relevant imports
            target_path = self._resolve_import_path(current_file, imp, repo_dir)
            full_path = os.path.join(repo_dir, target_path)
            
            if os.path.exists(full_path):
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        dep_content = f.read()
                    
                    # Only include if file is reasonably sized (< 3000 chars)
                    if len(dep_content) < 3000:
                        context += f"{'='*60}\n"
                        context += f"DEPENDENCY FILE: {target_path}\n"
                        context += f"{'='*60}\n"
                        context += f"```\n{dep_content}\n```\n\n"
                        found = True
                    else:
                        # For large files, show exports only
                        exports = re.findall(r'^export .+$', dep_content, re.MULTILINE)
                        if exports:
                            context += f"{'='*60}\n"
                            context += f"DEPENDENCY FILE: {target_path} (exports only)\n"
                            context += f"{'='*60}\n"
                            context += "\n".join(exports[:20]) + "\n\n"
                            found = True
                except Exception as e:
                    logger.warning(f"Failed to read dependency {target_path}: {e}")
                    pass
        
        return context if found else ""

    def _build_fix_prompt(self, file_path: str, info: dict, yaml_context: str = "", story_context: str = "", fix_history: dict = None, code_history: list = None, error_history: list = None, repo_dir: str = None) -> str:
        """Build comprehensive fix prompt with full context including fix history and FAILED ATTEMPTS"""
        prompt = "You are a PRODUCTION CODE FIXER. CRITICAL: All errors MUST be COMPLETELY resolved.\n"
        prompt += "PARTIAL FIXES ARE FAILURES - changing errors without eliminating them causes deployment to fail.\n"
        prompt += "If you can't fix ALL errors completely, say so explicitly.\n\n"
        prompt += "REPOSITORY PATH RULE: output FILE_PATH must be repository-root relative.\n"
        prompt += "Never prefix FILE_PATH with 'backend/' or 'frontend/'.\n\n"
        
        # CRITICAL: Add technical skill/config context FIRST with strong emphasis
        prompt += "🚨 MANDATORY TECHNICAL REQUIREMENTS 🚨\n"
        prompt += "="*80 + "\n"
        prompt += "You MUST follow the technical requirements specified in the skill/config context below.\n"
        prompt += "DO NOT use libraries, frameworks, or approaches that are NOT specified.\n"
        prompt += "DO NOT hallucinate or assume technologies - USE ONLY what's defined below.\n"
        prompt += "="*80 + "\n\n"
        
        # CRITICAL: For syntax errors, extract and show the problematic line
        if repo_dir and info.get('missing'):
            syntax_errors = [e for e in info['missing'] if 'TypeScript error' in e and ' at ' in e]
            if syntax_errors:
                prompt += "\n### 🚨 SYNTAX ERROR DETAILS ###\n"
                full_path = os.path.join(repo_dir, file_path)
                if os.path.exists(full_path):
                    try:
                        with open(full_path, 'r', encoding='utf-8') as f:
                            lines = f.readlines()
                        
                        for err in syntax_errors[:3]:  # Show details for first 3 syntax errors
                            # Extract line number from error like "at 169,125"
                            match = re.search(r' at (\d+),(\d+)', err)
                            if match:
                                line_num = int(match.group(1))
                                col_num = int(match.group(2))
                                
                                if 0 < line_num <= len(lines):
                                    problem_line = lines[line_num - 1].rstrip()
                                    prompt += f"\n❌ {err}\n"
                                    prompt += f"   Line {line_num}: ```{problem_line}```\n"
                                    
                                    # Show surrounding context
                                    if line_num > 1:
                                        prompt += f"   Line {line_num-1}: ```{lines[line_num-2].rstrip()}```\n"
                                    if line_num < len(lines):
                                        prompt += f"   Line {line_num+1}: ```{lines[line_num].rstrip()}```\n"
                                    
                                    # Add specific guidance for common TypeScript errors
                                    if "'...' expected" in err:
                                        prompt += "   ⚠️ This means there's a syntax error - likely missing comma, bracket, or spread operator (...)\n"
                                    elif "';' expected" in err:
                                        prompt += "   ⚠️ Missing semicolon or statement terminator\n"
                                    elif "')' expected" in err:
                                        prompt += "   ⚠️ Unclosed parenthesis or function call\n"
                    except Exception:
                        pass
                prompt += "\n### END OF SYNTAX ERROR DETAILS ###\n\n"
        
        # Add context FIRST with DEBUG logging
        if yaml_context:
            # DEBUG: Log that we're sending context
            config_type = "FRONTEND" if ("FRONTEND TECHNICAL" in yaml_context or "FRONTEND SKILL" in yaml_context) else "BACKEND" if ("BACKEND TECHNICAL" in yaml_context or "BACKEND SKILL" in yaml_context) else "UNKNOWN"
            config_length = len(yaml_context)
            logger.info(f"🔍 DEBUG: Sending {config_type} technical context to AI ({config_length} chars) for {file_path}")
            # Log first 200 chars as preview
            preview = yaml_context[:200].replace('\n', ' ')
            logger.info(f"   Config Preview: {preview}...")
            prompt += yaml_context
        else:
            # WARNING: No config sent
            logger.warning(f"⚠️ WARNING: NO technical context sent to AI for {file_path}")
        
        if story_context:
            prompt += story_context + "\n"
        
        prompt += f"\n{'='*80}\n"
        prompt += f"🎯 TARGET FILE: {file_path}\n"
        prompt += f"{'='*80}\n"
        
        # CRITICAL: Show complete error messages with line numbers and details
        prompt += "\n### 🚨 CRITICAL ERRORS (Will cause deployment FAILURE) ###\n"
        if info.get('missing'):
            for idx, err in enumerate(info['missing'], 1):
                prompt += f"{idx}. {err}\n"
        prompt += "\n"
        prompt += "⚠️ These errors MUST be fixed completely - partial fixes will fail deployment!\n\n"
        
        # NEW: Show history of failed attempts to avoid repeating mistakes
        if code_history and error_history:
            prompt += "\n### 🛑 PREVIOUS FAILED ATTEMPTS (Do NOT repeat these mistakes) ###\n"
            # Show last 2 attempts to save context
            for i, (prev_code, prev_errors) in enumerate(zip(code_history[-2:], error_history[-2:])):
                prompt += f"\n--- Attempt {len(code_history) - len(code_history[-2:]) + i + 1} FAILED with these errors ---\n"
                for err in prev_errors[:8]:  # Show more errors to give better context
                    prompt += f"  ❌ {err}\n"
                # Show snippet of what was tried if too long
                snippet = prev_code
                if len(snippet) > 1000:
                    snippet = snippet[:500] + "\n... [truncated] ...\n" + snippet[-500:]
                prompt += f"\n[Previous Code that FAILED]:\n```\n{snippet}\n```\n"
            prompt += "\n### END OF FAILED ATTEMPTS - TRY A DIFFERENT APPROACH ###\n\n"

        # Add fix history warning if file has been attempted before
        if fix_history and file_path in fix_history:
            attempts = fix_history[file_path]
            if attempts >= 3:
                prompt += f"\n🚨 CRITICAL: This is attempt #{attempts + 1}. Previous {attempts} attempts ALL FAILED with IDENTICAL errors.\n"
                prompt += "The problem is NOT being fixed. You MUST:\n"
                prompt += "1. READ THE ERRORS CAREFULLY - they tell you EXACTLY what's wrong\n"
                prompt += "2. TRY A COMPLETELY DIFFERENT SOLUTION - what you tried before does NOT work\n"
                prompt += "3. If it's a missing import/type/function, ADD IT - don't just reference it\n"
                prompt += "4. If it's a type error, FIX THE ACTUAL TYPE - not just the usage\n"
                prompt += "5. CHECK DEPENDENCY FILES - maybe the error is in what you're importing FROM\n\n"
            elif attempts >= 2:
                prompt += f"\n⚠️ CRITICAL WARNING: This file has been attempted {attempts} time(s) before and is still failing.\n"
                prompt += "Analyze why the previous attempts failed and try a DIFFERENT architectural approach.\n"
                prompt += "Be EXTREMELY CONSERVATIVE with changes but ensure the fix actually addresses the root cause.\n\n"
            elif attempts == 1:
                prompt += f"\n⚠️ WARNING: This file was attempted once before and produced new errors.\n\n"
        
        # CRITICAL NEW: Add FULL CONTENT of imported dependencies
        content = info.get('content', '')
        if content and repo_dir:
            imports = self._extract_imports(content, file_path)
            dep_context = self._get_full_dependency_context(imports, repo_dir, file_path)
            if dep_context:
                prompt += dep_context

        # Errors already shown at top with clear formatting
        
        # Show packages to add
        if 'packages' in info and info['packages']:
            prompt += f"\nRequired packages: {', '.join(info['packages'])}\n"
        
        # Show FULL file content - no truncation (1M input token limit!)
        content = info.get('content', '')
        if content:
            prompt += f"\nCurrent file content:\n```\n{content}\n```\n"
        else:
            prompt += "\n⚠️ File doesn't exist - CREATE IT\n"
        
        # Show dependent files (who imports this file)
        if info.get('dependent_files'):
            prompt += f"\n⚠️ IMPORTANT: These files depend on this file:\n"
            for dep_file in info['dependent_files'][:5]:
                prompt += f"  - {dep_file}\n"
            prompt += "Make sure you export everything they need!\n\n"
        
        prompt += "\n### 🔧 CRITICAL FIXING INSTRUCTIONS ###\n"
        prompt += "1. UNDERSTAND THE ROOT CAUSE - Don't just patch symptoms\n"
        prompt += "2. FIX ALL ERRORS COMPLETELY - Each error in the list must be 100% resolved\n"
        prompt += "3. VERIFY YOUR FIX - After fixing, mentally check if ALL errors are gone\n"
        prompt += "4. NO PARTIAL FIXES - Changing error messages without fixing is FAILURE\n"
        prompt += "5. ADD ALL MISSING PIECES - imports, types, functions, exports\n"
        prompt += "6. TEST MENTALLY - Would this code compile and run without any errors?\n\n"
        
        prompt += "### ❌ COMMON MISTAKES TO AVOID ###\n"
        prompt += "- Adding an import but not installing the package (still fails)\n"
        prompt += "- Fixing type A but breaking type B (still fails)\n"
        prompt += "- Creating a partial implementation with TODOs (still fails)\n"
        prompt += "- Importing something that doesn't exist in the source file (still fails)\n"
        prompt += "- Changing error type without fixing the underlying issue (still fails)\n\n"

        if file_path.endswith("requirements.txt"):
            prompt += "### REQUIREMENTS.TXT RULES ###\n"
            prompt += "- NEVER remove existing dependencies unless an error explicitly says the package is invalid.\n"
            prompt += "- ONLY add missing packages required by the listed errors.\n"
            prompt += "- Keep currently working dependencies intact.\n\n"
        elif file_path.endswith("package.json"):
            prompt += "### PACKAGE.JSON RULES ###\n"
            prompt += "- NEVER remove existing dependencies unless an error explicitly says package/version is invalid.\n"
            prompt += "- ONLY add missing packages required by the listed errors.\n"
            prompt += "- Keep currently working dependencies intact.\n\n"
        
        prompt += "### ✅ SUCCESS = ZERO ERRORS ###\n"
        prompt += "After your fix, ALL these must be true:\n"
        prompt += "- Every error in the list above is completely gone\n"
        prompt += "- No new errors are introduced\n"
        prompt += "- Code compiles without any errors\n"
        prompt += "- All imports resolve to real packages/files\n"
        prompt += "- All exports exist for dependent files\n\n"

        # NEW: Check for redundant type definitions and warn AI
        if repo_dir:
            redundant_context = self.frontend_fixer._get_redundant_type_context(repo_dir, file_path, info)
            if redundant_context:
                prompt += redundant_context

        output_file_path = file_path
        if output_file_path.startswith("backend/"):
            output_file_path = output_file_path[len("backend/"):]
        elif output_file_path.startswith("frontend/"):
            output_file_path = output_file_path[len("frontend/"):]

        prompt += "Output format:\n"
        prompt += "ROOT_CAUSE: <Why these errors exist - the fundamental problem>\n"
        prompt += "COMPLETE_FIX: <Exact changes that will eliminate ALL errors listed above>\n"
        prompt += "VERIFICATION: <How you verified ALL errors will be gone after this fix>\n\n"
        prompt += f"FILE_PATH: {output_file_path}\n---\n[COMPLETE ERROR-FREE CODE]\n---\n"
        prompt += "🚨 STRICT REQUIREMENT: Output ONLY the file above. Do NOT output any other FILE_PATH blocks.\n"
        return prompt
    
    def _parse_all_errors(self, errors: List[str], repo_dir: str) -> Dict[str, Dict]:
        """Parse ALL error types with proper path normalization and CROSS-FILE dependency detection"""
        files = {}
        backend_prefix = self._backend_prefix(repo_dir)
        frontend_prefix = self._frontend_prefix(repo_dir)

        def _safe_read(local_path: str) -> str:
            """Read file content only when path is a regular file."""
            try:
                if os.path.isfile(local_path):
                    with open(local_path, 'r', encoding='utf-8') as f:
                        return f.read()
            except Exception:
                pass
            return ""
        
        for error in errors:
            # Missing mandatory frontend configuration file (e.g., next.config.js, tsconfig.json)
            # These errors come from _validate_frontend_dependencies and look like:
            # "Missing mandatory file: 'next.config.js' (Next.js configuration) is required"
            mandatory_match = re.search(r"Missing mandatory file: '([^']+)' \(([^)]+)\) is required", error)
            if mandatory_match:
                filename = mandatory_match.group(1)
                description = mandatory_match.group(2)
                # These files live at the root of the frontend directory
                file_path = self._apply_prefix(frontend_prefix, filename)
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(
                    f"File does not exist - CREATE IT: {filename} ({description})"
                )
                continue

            # NEW: AI Concatenation Error detection (source files that need splitting)
            concat_match = re.search(r"AI Concatenation Error in '([^']+)'", error)
            if concat_match:
                file_path = concat_match.group(1)
                file_path = self._normalize_file_path(file_path, repo_dir)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(error)
                continue

            # Invalid pip requirement line (e.g. "---" separator from AI code-block format)
            # Detected by deployment_validator._validate_python_imports
            invalid_pip_match = re.search(
                r"Invalid pip requirement in 'requirements\.txt': '([^']+)'", error
            )
            if invalid_pip_match:
                file_path = self._apply_prefix(backend_prefix, "requirements.txt")
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                files[file_path]['missing'].append(
                    f"Strip invalid pip line: '{invalid_pip_match.group(1)}'"
                )
                continue
            # NEW: Invalid pip version (package pinned to a non-existent version)
            version_mismatch = re.search(
                r"Invalid pip version in 'requirements\.txt': '([^']+)' not available\.(?: Available versions: (.+))?",
                error
            )
            if version_mismatch:
                bad_req = version_mismatch.group(1).strip()
                available = (version_mismatch.group(2) or "").strip()
                file_path = self._apply_prefix(backend_prefix, "requirements.txt")
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                if available:
                    files[file_path]['missing'].append(
                        f"Invalid version: '{bad_req}' not available. Available versions: {available}"
                    )
                else:
                    files[file_path]['missing'].append(
                        f"Invalid version: '{bad_req}' not available."
                    )
                continue
            if "Runtime pip install failure in requirements.txt" in error:
                file_path = self._apply_prefix(backend_prefix, "requirements.txt")
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                files[file_path]['missing'].append("Runtime pip install failure: sanitize invalid/deprecated requirements entries")
                continue
            # Missing dependency
            if "Missing dependency:" in error:
                pkg_match = re.search(r"Missing dependency: '([^']+)'", error)
                if pkg_match:
                    pkg = pkg_match.group(1)
                    if self._is_frontend_alias_package(pkg):
                        continue
                    if pkg == "google":
                        pkg = "google-cloud-firestore"
                    if "requirements.txt" in error:
                        file_path = self._apply_prefix(backend_prefix, "requirements.txt")
                    else:
                        file_path = self._apply_prefix(frontend_prefix, "package.json")
                    
                    if file_path not in files:
                        local = os.path.join(repo_dir, file_path)
                        content = ''
                        if os.path.exists(local):
                            with open(local, 'r') as f:
                                content = f.read()
                        files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                    files[file_path]['packages'].add(pkg)
            
            # NEW: package.json syntax errors (e.g. Extra data, Expecting value)
            json_syntax_match = re.search(r"Invalid package\.json: JSON parsing failed \(([^)]+)\)", error)
            if json_syntax_match:
                details = json_syntax_match.group(1)
                file_path = self._apply_prefix(frontend_prefix, "package.json")
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                files[file_path]['missing'].append(f"Fix JSON syntax: {details}")
                continue

            # NEW: Missing mandatory frontend directory (app/ or pages/)
            dir_match = re.search(r"(?:ENVIRONMENT ERROR: )?Missing mandatory directory: Frontend must have either an 'app' directory \(App Router\) or 'pages' directory \(Pages Router\)", error)
            if dir_match:
                # Target the most common entry point to force creation of the directory
                file_path = self._apply_prefix(frontend_prefix, "app/page.tsx")
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(
                    "Missing mandatory routing directory (app/ or pages/). CREATE 'app/page.tsx' with a basic Next.js page component to establish the App Router."
                )
                continue

            # NEW: Missing node_modules bootstrapping
            if "Missing 'node_modules'" in error:
                file_path = self._apply_prefix(frontend_prefix, "package.json")
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                files[file_path]['missing'].append("Missing 'node_modules': Forces fresh dependency installation.")
                continue

            # Python syntax errors from validator:
            # "SyntaxError in 'app.py' at line 1: invalid syntax"
            syntax_match = re.search(r"SyntaxError in '([^']+)' at line (\d+): (.+)", error)
            if syntax_match:
                syntax_target = syntax_match.group(1).strip()
                if syntax_target.startswith("backend/"):
                    file_path = syntax_target
                elif "/" in syntax_target:
                    file_path = self._apply_prefix(backend_prefix, syntax_target)
                elif syntax_target.endswith(".py"):
                    if "." in syntax_target[:-3] and "/" not in syntax_target:
                        syntax_target = syntax_target[:-3].replace(".", "/") + ".py"
                    file_path = self._apply_prefix(backend_prefix, syntax_target)
                else:
                    file_path = self._apply_prefix(backend_prefix, f"{syntax_target}.py")
                file_path = self._normalize_file_path(file_path, repo_dir)
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(error)
                continue

            # Pydantic Settings missing required environment variables
            pyd_settings_match = re.search(
                r"PydanticSettingsError in '([^']+)': Missing required settings: (.+)",
                error
            )
            if pyd_settings_match:
                settings_path = pyd_settings_match.group(1).strip()
                missing_list = pyd_settings_match.group(2).strip()
                if settings_path.startswith("backend/"):
                    file_path = settings_path
                elif "/" in settings_path:
                    file_path = self._apply_prefix(backend_prefix, settings_path)
                else:
                    file_path = self._apply_prefix(backend_prefix, settings_path)
                file_path = self._normalize_file_path(file_path, repo_dir)
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(f"Missing settings: {missing_list}")
                continue

            runtime_name_match = re.search(
                r"RuntimeNameError in '([^']+)': name '([^']+)' is not defined",
                error
            )
            if runtime_name_match:
                target = runtime_name_match.group(1).strip()
                missing_name = runtime_name_match.group(2).strip()
                if target.startswith("backend/"):
                    file_path = target
                elif "/" in target:
                    file_path = self._apply_prefix(backend_prefix, target)
                elif target.endswith(".py"):
                    if "." in target[:-3] and "/" not in target:
                        target = target[:-3].replace(".", "/") + ".py"
                    file_path = self._apply_prefix(backend_prefix, target)
                else:
                    file_path = self._apply_prefix(backend_prefix, f"{target}.py")
                file_path = self._normalize_file_path(file_path, repo_dir)
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(f"NameError: '{missing_name}' is not defined")
                continue
            match = re.search(r"ImportError in '([^']+)': '([^']+)' not found in '([^']+)'", error)
            if match:
                importer_path = match.group(1)
                missing_name = match.group(2)
                target_module = match.group(3)
                
                # CRITICAL: Do NOT strip .py from importer_path before calling _resolve_import_path.
                # _resolve_import_path uses the .py suffix to detect backend (Python) imports.
                # Stripping .py causes Python files to be misclassified as frontend TypeScript files,
                # generating phantom paths like "frontend/src/models.user.py.ts".
                src_module = importer_path
                
                # PRIMARY FIX: The dependency file that's missing the export
                file_path = self._resolve_import_path(src_module, target_module, repo_dir)
                file_path = self._normalize_file_path(file_path, repo_dir)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content, 'dependent_files': []}
                
                # Track which items are missing
                if 'missing' not in files[file_path]:
                    files[file_path]['missing'] = []
                files[file_path]['missing'].append(f"Missing export: {missing_name}")
                
                # Track which files depend on this (for context)
                if 'dependent_files' not in files[file_path]:
                    files[file_path]['dependent_files'] = []
                files[file_path]['dependent_files'].append(importer_path)
            
            # NEW: Handle "Cannot import from 'X' - file 'X.py' does not exist"
            cannot_import_match = re.search(r"ImportError in '([^']+)': Cannot import from '([^']+)' - file '([^']+)' does not exist", error)
            if cannot_import_match:
                file_with_error = cannot_import_match.group(1)
                module_trying_to_import = cannot_import_match.group(2)
                missing_file = cannot_import_match.group(3)
                
                # CRITICAL: Distinguish between pip packages and local files.
                # Dotted module paths like "google.cloud.firestore" are still package imports.
                top_module = module_trying_to_import.split('.')[0]
                third_party_roots = {
                    'pydantic', 'jose', 'bcrypt', 'jwt', 'fastapi', 'uvicorn',
                    'google', 'firebase', 'sqlalchemy', 'alembic', 'redis',
                    'celery', 'requests', 'httpx', 'aiohttp', 'boto3', 'stripe',
                    'flask', 'django', 'numpy', 'pandas', 'scipy', 'sklearn',
                    'tensorflow', 'torch', 'keras', 'passlib', 'pytest', 'bson', 'pyjwt',
                    'click', 'jinja2', 'email_validator'
                }
                is_package = (
                    '/' not in module_trying_to_import and
                    not module_trying_to_import.startswith(('.', '@')) and
                    top_module in third_party_roots
                )
                
                if is_package:
                    # This is a missing pip package, not a file!
                    file_path = self._apply_prefix(backend_prefix, "requirements.txt")
                    if file_path not in files:
                        local = os.path.join(repo_dir, file_path)
                        content = _safe_read(local)
                        files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                    # Map to correct package name
                    pkg_name = {
                        'pydantic_settings': 'pydantic-settings',
                        'jose': 'python-jose[cryptography]',
                        'jwt_utils': 'PyJWT',
                        'pyjwt': 'PyJWT',
                        'email_validator': 'email-validator',
                        'passlib': 'passlib[bcrypt]',
                        'passlib.context': 'passlib[bcrypt]',
                        'bson': 'pymongo',
                        'google.cloud.firestore': 'google-cloud-firestore',
                        'google.cloud.storage': 'google-cloud-storage',
                        'google.cloud.secretmanager': 'google-cloud-secret-manager',
                        'google.api_core': 'google-api-core',
                        'firebase_admin': 'firebase-admin',
                    }.get(module_trying_to_import, module_trying_to_import)
                    if pkg_name == module_trying_to_import:
                        pkg_name = {
                            'google': 'google-cloud-firestore',
                            'firebase': 'firebase-admin',
                            'pydantic': 'pydantic',
                        }.get(top_module, module_trying_to_import)
                    files[file_path]['packages'].add(pkg_name)
                    continue
                
                # It's a local file that needs to be created
                file_path = self._apply_prefix(backend_prefix, missing_file)
                file_path = self._normalize_file_path(file_path, repo_dir)
                # Avoid creating a top-level module file that shadows an existing package directory.
                # Example: creating backend/routes.py breaks "from routes import auth_routes"
                # when backend/routes/ already exists.
                if file_path.endswith(".py"):
                    module_name = os.path.basename(file_path)[:-3]
                    package_root = os.path.join(repo_dir, backend_prefix) if backend_prefix else repo_dir
                    package_dir = os.path.join(package_root, module_name)
                    if os.path.isdir(package_dir):
                        file_path = self._apply_prefix(backend_prefix, f"{module_name}/__init__.py")
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(f"File does not exist - CREATE IT with module: {module_trying_to_import}")
                continue
            
            # NEW: Handle both variations of wrong import format errors
            # Pattern 1: New format: "WRONG - 'from backend.X' | CORRECT - 'from X'"
            # Pattern 2: Legacy format: "Using 'from backend.X' - MUST use 'from X' instead"
            wrong_import_match = re.search(r"ImportError in '([^']+)': (?:Using 'from backend\.([^']+)'|Cannot import from 'backend\.([^']+)')", error)
            if wrong_import_match:
                file_with_error = wrong_import_match.group(1)
                # Try both capture groups since we have two patterns
                wrong_import = wrong_import_match.group(2) or wrong_import_match.group(3)
                if not wrong_import:
                    continue
                    
                # The correct import is just the module name without backend. prefix
                correct_import = wrong_import
                
                # The file that has the wrong import
                # Fix: Strip .py if it exists before replacing dots
                module_base = file_with_error
                if module_base.endswith('.py'):
                    module_base = module_base[:-3]
                file_path = self._apply_prefix(backend_prefix, f"{module_base.replace('.', '/')}.py")
                file_path = self._normalize_file_path(file_path, repo_dir)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(f"Fix import: Change 'from backend.{wrong_import}' to 'from {correct_import}'")
                continue

            # main.py relative import error:
            # "ImportError in 'main.py': Using 'from .env' - main.py is the entry point and CANNOT use relative imports..."
            main_relative_match = re.search(
                r"ImportError in 'main\.py': Using 'from (\.[^']+)' - main\.py is the entry point and CANNOT use relative imports",
                error
            )
            if main_relative_match:
                rel_import = main_relative_match.group(1)
                file_path = self._apply_prefix(backend_prefix, "main.py")
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(
                    f"CANNOT use relative imports in main.py ({rel_import}); convert to absolute import"
                )
                continue
            
            # Legacy pattern kept for backwards compatibility
            wrong_import_legacy = re.search(r"ImportError in '([^']+)': Using 'from backend\.([^']+)' - MUST use 'from ([^']+)' instead", error)
            if wrong_import_legacy:
                file_with_error = wrong_import_legacy.group(1)
                wrong_import = wrong_import_legacy.group(2)
                correct_import = wrong_import_legacy.group(3)
                
                # The file that has the wrong import
                file_path = self._apply_prefix(backend_prefix, f"{file_with_error.replace('.', '/')}.py")
                file_path = self._normalize_file_path(file_path, repo_dir)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(f"Fix import: Change 'from backend.{wrong_import}' to 'from {correct_import}'")
                continue
            
            # TypeScript error - check if it's a missing module (package)
            tsc_match = re.search(r"TypeScript error in '([^']+)' at", error)
            if tsc_match:
                importer_rel = tsc_match.group(1)
                if frontend_prefix:
                    importer_path = importer_rel if importer_rel.startswith(f"{frontend_prefix}/") else f"{frontend_prefix}/{importer_rel}"
                else:
                    importer_path = importer_rel[9:] if importer_rel.startswith("frontend/") else importer_rel
                importer_path = self._normalize_file_path(importer_path, repo_dir)

                # Missing type declarations (TS7016) - add @types package.
                decl_match = re.search(r"Could not find a declaration file for module '([^']+)'", error)
                if decl_match:
                    module_name = decl_match.group(1).strip()
                    if module_name.startswith('@'):
                        # Scoped package => @types/scope__name
                        parts = module_name.split('/', 1)
                        if len(parts) == 2:
                            types_pkg = f"@types/{parts[0][1:]}__{parts[1]}"
                        else:
                            types_pkg = f"@types/{module_name[1:]}"
                    else:
                        types_pkg = f"@types/{module_name}"
                    file_path = self._apply_prefix(frontend_prefix, "package.json")
                    if file_path not in files:
                        local = os.path.join(repo_dir, file_path)
                        content = _safe_read(local)
                        files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                    files[file_path]['packages'].add(types_pkg)
                    continue

                # JSX intrinsic errors often mean React types are missing.
                if "JSX.IntrinsicElements" in error:
                    file_path = self._apply_prefix(frontend_prefix, "package.json")
                    if file_path not in files:
                        local = os.path.join(repo_dir, file_path)
                        content = _safe_read(local)
                        files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                    files[file_path]['packages'].add("@types/react")
                    files[file_path]['packages'].add("@types/react-dom")
                    continue

                # Check if it's a "missing exported member" error and route to dependency file.
                export_match = re.search(r"2305:\s*Module '\"?([^']+)\"?' has no exported member '([^']+)'", error)
                if export_match:
                    target_module = export_match.group(1).strip().strip('"')
                    missing_export = export_match.group(2).strip()
                    dep_path = self._resolve_import_path(importer_path, target_module, repo_dir)
                    dep_path = self._normalize_file_path(dep_path, repo_dir)
                    if dep_path not in files:
                        local = os.path.join(repo_dir, dep_path)
                        content = _safe_read(local)
                        files[dep_path] = {'missing': [], 'content': content, 'dependent_files': []}
                    files[dep_path]['missing'].append(f"Missing export: {missing_export}")
                    files[dep_path].setdefault('dependent_files', []).append(importer_path)

                # Check if it's a "not found in" export error (non-TS2305 formats).
                not_found_match = re.search(r"'([^']+)'\s+not found in '([^']+)'", error)
                if not_found_match:
                    missing_export = not_found_match.group(1).strip()
                    target_module = not_found_match.group(2).strip()
                    dep_path = self._resolve_import_path(importer_path, target_module, repo_dir)
                    dep_path = self._normalize_file_path(dep_path, repo_dir)
                    if dep_path not in files:
                        local = os.path.join(repo_dir, dep_path)
                        content = _safe_read(local)
                        files[dep_path] = {'missing': [], 'content': content, 'dependent_files': []}
                    files[dep_path]['missing'].append(f"Missing export: {missing_export}")
                    files[dep_path].setdefault('dependent_files', []).append(importer_path)
                    continue

                # Check if it's a "Cannot find module" error indicating missing package
                module_match = re.search(r"(?:2307:\s*)?Cannot find module '([^']+)'", error)
                if module_match:
                    missing_module = module_match.group(1)
                    if missing_module.startswith("src/"):
                        src_file_path = self._normalize_file_path(importer_path, repo_dir)
                        if src_file_path not in files:
                            local = os.path.join(repo_dir, src_file_path)
                            content = _safe_read(local)
                            files[src_file_path] = {'missing': [], 'content': content}
                        files[src_file_path]['missing'].append("Fix import alias: replace 'src/' with '@/'")
                        continue
                    # NEW: Alias import mistakenly includes /src/ segment (e.g., "@/src/...")
                    if missing_module.startswith("@/src/") or missing_module.startswith("~/src/"):
                        src_file_path = self._normalize_file_path(importer_path, repo_dir)
                        if src_file_path not in files:
                            local = os.path.join(repo_dir, src_file_path)
                            content = _safe_read(local)
                            files[src_file_path] = {'missing': [], 'content': content}
                        if missing_module.startswith("@/src/"):
                            files[src_file_path]['missing'].append("Fix import alias: replace '@/src/' with '@/'")
                        else:
                            files[src_file_path]['missing'].append("Fix import alias: replace '~/src/' with '~/'")
                        continue
                    # Local alias/path imports are not npm packages; treat them as missing local modules.
                    if missing_module.startswith(("@/", "~/")):
                        dep_path = self._resolve_import_path(importer_path, missing_module, repo_dir)
                        dep_path = self._normalize_file_path(dep_path, repo_dir)
                        dep_abs = os.path.join(repo_dir, dep_path)
                        if os.path.exists(dep_abs):
                            alias_key = "@/*" if missing_module.startswith("@/") else "~/*"
                            tsconfig_path = self._apply_prefix(frontend_prefix, "tsconfig.json")
                            jsconfig_path = self._apply_prefix(frontend_prefix, "jsconfig.json")
                            target_config = tsconfig_path
                            if not os.path.exists(os.path.join(repo_dir, tsconfig_path)) and os.path.exists(os.path.join(repo_dir, jsconfig_path)):
                                target_config = jsconfig_path
                            if target_config not in files:
                                local = os.path.join(repo_dir, target_config)
                                content = _safe_read(local)
                                files[target_config] = {'missing': [], 'content': content}
                            files[target_config]['missing'].append(f"Missing path alias for '{alias_key}' imports; add paths mapping for {alias_key} to src/* in tsconfig/jsconfig.")
                            continue
                    if self._is_frontend_alias_package(missing_module) or missing_module.startswith(('./', '../')):
                        dep_path = self._resolve_import_path(importer_path, missing_module, repo_dir)
                        dep_path = self._normalize_file_path(dep_path, repo_dir)
                        if dep_path not in files:
                            local = os.path.join(repo_dir, dep_path)
                            content = _safe_read(local)
                            files[dep_path] = {'missing': [], 'content': content, 'dependent_files': []}
                        files[dep_path]['missing'].append(f"Missing local module for import: {missing_module}")
                        files[dep_path].setdefault('dependent_files', []).append(importer_path)
                        continue

                    # Extract package name.
                    pkg = missing_module
                    if '/' in missing_module:
                        pkg = missing_module.split('/')[0]
                        # If it starts with @, include the scope
                        if missing_module.startswith('@'):
                            parts = missing_module.split('/')
                            if len(parts) >= 2:
                                pkg = f"{parts[0]}/{parts[1]}"
                    if self._is_frontend_alias_package(pkg):
                        continue

                    # Check whether the package is already installed in package.json.
                    # If it is, the problem is a bad internal sub-path import in the SOURCE FILE
                    # (e.g. `import X from '@headlessui/react/dist/components/description'` when
                    # @headlessui/react is installed but that deep path doesn't exist).
                    # In that case route the error to the source file for AI to fix, NOT package.json.
                    pj_local = os.path.join(
                        repo_dir,
                        frontend_prefix,
                        "package.json"
                    ) if frontend_prefix else os.path.join(repo_dir, "package.json")
                    try:
                        with open(pj_local, 'r', encoding='utf-8') as _pjf:
                            _pj = json.load(_pjf)
                        installed_deps = {
                            **_pj.get('dependencies', {}),
                            **_pj.get('devDependencies', {}),
                        }
                    except Exception:
                        installed_deps = {}

                    if pkg in installed_deps:
                        # Package exists – the import path itself is wrong; fix the source file.
                        src_file_path = self._normalize_file_path(importer_path, repo_dir)
                        if src_file_path not in files:
                            local = os.path.join(repo_dir, src_file_path)
                            content = _safe_read(local)
                            files[src_file_path] = {'missing': [], 'content': content}
                        files[src_file_path]['missing'].append(error)
                        continue

                    # Package is genuinely missing - add to package.json
                    file_path = self._apply_prefix(frontend_prefix, "package.json")
                    if file_path not in files:
                        local = os.path.join(repo_dir, file_path)
                        content = _safe_read(local)
                        files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                    files[file_path]['packages'].add(pkg)
                    continue
                
                # Regular TypeScript error
                file_rel = importer_rel
                if frontend_prefix:
                    file_path = file_rel if file_rel.startswith(f"{frontend_prefix}/") else f"{frontend_prefix}/{file_rel}"
                else:
                    file_path = file_rel[9:] if file_rel.startswith("frontend/") else file_rel
                file_path = self._normalize_file_path(file_path, repo_dir)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(error)
                continue

            # PropertyError from _validate_typescript_properties:
            # "PropertyError in 'src/components/Foo.tsx': 'bar' does not exist on type 'Baz'. Did you mean 'baz'?"
            prop_match = re.search(r"PropertyError in '([^']+)':", error)
            if prop_match:
                prop_rel = prop_match.group(1)
                if frontend_prefix:
                    file_path = prop_rel if prop_rel.startswith(f"{frontend_prefix}/") else f"{frontend_prefix}/{prop_rel}"
                else:
                    file_path = prop_rel[9:] if prop_rel.startswith("frontend/") else prop_rel
                file_path = self._normalize_file_path(file_path, repo_dir)
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(error)
                continue

            # TypeError from _validate_frontend_dependencies (function arity check):
            # "TypeError in 'app/tickets/page.tsx': Function 'getTickets' expects at least 2 arguments, but found 1."
            type_err_match = re.search(r"TypeError in '([^']+)':", error)
            if type_err_match:
                type_rel = type_err_match.group(1)
                if frontend_prefix:
                    file_path = type_rel if type_rel.startswith(f"{frontend_prefix}/") else f"{frontend_prefix}/{type_rel}"
                else:
                    file_path = type_rel[9:] if type_rel.startswith("frontend/") else type_rel
                file_path = self._normalize_file_path(file_path, repo_dir)
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = _safe_read(local)
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(error)

        return files
    
    async def _refresh_frontend_dependencies(self, repo_dir: str, job_id: str) -> bool:
        """Shared frontend dependency refresh logic (programmatic or worker)."""
        frontend_dir = os.path.join(repo_dir, 'frontend')
        package_json = os.path.join(frontend_dir, 'package.json')
        if not os.path.exists(package_json) and os.path.exists(os.path.join(repo_dir, 'package.json')):
            frontend_dir = repo_dir
            package_json = os.path.join(frontend_dir, 'package.json')
        if not os.path.exists(package_json):
            return False

        # Always use npm install instead of npm ci in the auto-fix loop. 
        # npm ci is too fragile as it requires package.json and package-lock.json to be in exact sync,
        # which is frequently not the case when AI is adding/removing dependencies.
        npm_cmd = ['npm', 'install', '--prefer-offline', '--no-audit', '--legacy-peer-deps']
        try:
            for attempt in range(1, 3):
                self._safe_log(job_id, f"📦 Refreshing frontend deps in {repo_dir} ({' '.join(npm_cmd)})", "Environment")
                process = await asyncio.create_subprocess_exec(
                    *npm_cmd,
                    cwd=frontend_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await process.communicate()
                stderr_text = (stderr or b"").decode(errors='replace')
                if process.returncode == 0:
                    self._safe_log(job_id, "✅ Successfully refreshed frontend dependencies", "Environment")
                    return True

                self._safe_log(job_id, f"⚠️ Dependency refresh failed (rc={process.returncode})", "Environment", level="WARNING")
                self._safe_log(job_id, f"Error details: {stderr_text[:1000]}", "Environment", level="DEBUG")

                # Auto-recover from package names not found in npm registry.
                bad_pkg = None
                e404_match = re.search(r"'([^']+@\S+)' is not in this registry", stderr_text)
                if e404_match:
                    spec = e404_match.group(1)
                    if spec.startswith('@'):
                        at_idx = spec.find('@', 1)
                        bad_pkg = spec if at_idx == -1 else spec[:at_idx]
                    else:
                        bad_pkg = spec.split('@', 1)[0]
                if bad_pkg is None:
                    url_match = re.search(r"registry\.npmjs\.org/([^\s]+)\s+-\s+Not found", stderr_text)
                    if url_match:
                        bad_pkg = unquote(url_match.group(1))

                # ETARGET: version constraint exists in registry but the specific version does not.
                # e.g. "No matching version found for qs@~6.14.1"
                # Remove the package from package.json so npm can resolve transitive deps freely.
                if bad_pkg is None and 'ETARGET' in stderr_text:
                    etarget_match = re.search(r"No matching version found for ([^\s.]+)", stderr_text)
                    if etarget_match:
                        spec = etarget_match.group(1)
                        if spec.startswith('@'):
                            at_idx = spec.find('@', 1)
                            bad_pkg = spec if at_idx == -1 else spec[:at_idx]
                        else:
                            bad_pkg = spec.split('@', 1)[0]

                if bad_pkg and (self._is_frontend_alias_package(bad_pkg) or bad_pkg):
                    try:
                        with open(package_json, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        deps = data.get('dependencies', {})
                        dev_deps = data.get('devDependencies', {})
                        removed = False
                        if bad_pkg in deps:
                            del deps[bad_pkg]
                            removed = True
                        if bad_pkg in dev_deps:
                            del dev_deps[bad_pkg]
                            removed = True
                        if removed:
                            data['dependencies'] = deps
                            data['devDependencies'] = dev_deps
                            with open(package_json, 'w', encoding='utf-8') as f:
                                json.dump(data, f, indent=2)
                                f.write('\n')
                            self._invalid_npm_packages.add(bad_pkg)
                            self._safe_log(job_id, f"🧹 Removed invalid npm alias package '{bad_pkg}' and retrying install", "Environment")
                            continue
                    except Exception as e:
                        self._safe_log(job_id, f"⚠️ Failed package.json cleanup after npm E404: {e}", "Environment", level="WARNING")

                # No recoverable cleanup or cleanup failed.
                if attempt >= 2:
                    self._safe_log(job_id, f"❌ Dependency refresh failed: {stderr_text[:1000]}", "Environment", level="ERROR")
                    return False
            
            return False
        except Exception as e:
            self._safe_log(job_id, f"❌ Dependency refresh error: {e}", "Environment", level="ERROR")
            return False

    def _normalize_file_path(self, path: str, repo_dir: Optional[str] = None) -> str:
        """Normalize file path to prevent duplication (e.g., frontend/src/src/api -> frontend/src/api)"""
        # Remove duplicate path segments and current directory markers
        parts = path.split('/')
        normalized = []
        
        for part in parts:
            # Skip empty strings (from double slashes) and current directory markers
            if not part or part == '.':
                continue
            # Don't add duplicate consecutive parts
            if not normalized or part != normalized[-1]:
                normalized.append(part)
        
        result = '/'.join(normalized)

        prefixes = self._repo_prefixes(repo_dir)
        frontend_prefix = prefixes["frontend"]
        backend_prefix = prefixes["backend"]
        had_frontend_prefix = result.startswith("frontend/")
        had_backend_prefix = result.startswith("backend/")
        if had_frontend_prefix:
            result = result[9:]
        if had_backend_prefix:
            result = result[8:]

        is_backend = had_backend_prefix or result.endswith('.py')
        is_frontend = had_frontend_prefix or not is_backend

        if is_frontend:
            # Frontend files should be: src/... or app/... or pages/... or config files at root
            if result.startswith(('src/', 'app/', 'pages/', 'package.json', 'package-lock.json', 'tsconfig.json', 'jsconfig.json', 'next.config', 'public/')):
                return self._apply_prefix(frontend_prefix, result)

            app_router_files = ('layout.tsx', 'layout.jsx', 'page.tsx', 'page.jsx', 'loading.tsx', 'loading.jsx', 'error.tsx', 'error.jsx', 'not-found.tsx', 'global.css', 'globals.css')
            if result in app_router_files:
                return self._apply_prefix(frontend_prefix, f"app/{result}")

            return self._apply_prefix(frontend_prefix, f"src/{result}")
        elif is_backend:
            backend_rest = result
            if backend_rest.endswith('.py') and '/' not in backend_rest and '.' in backend_rest[:-3]:
                backend_rest = backend_rest[:-3].replace('.', '/') + '.py'
            return self._apply_prefix(backend_prefix, backend_rest)

        return result
    
    def _extract_dependency_files_from_errors(self, errors: List[str], current_file: str, repo_dir: str) -> Dict[str, List[str]]:
        """
        Extract dependency files that need fixing from error messages.
        Returns dict mapping dependency file path -> list of missing exports.
        
        Example: {'frontend/src/store/authStore.ts': ['AuthState', 'useAuthStore']}
        """
        dependency_files = {}
        
        for error in errors:
            # Match ImportError pattern: 'X' not found in 'Y'
            match = re.search(r"'([^']+)' not found in '([^']+)'", error)
            if match:
                missing_export = match.group(1)
                target_module = match.group(2)
                
                # Resolve the dependency file path
                dep_file_path = self._resolve_import_path(current_file, target_module, repo_dir)
                dep_file_path = self._normalize_file_path(dep_file_path, repo_dir)
                
                # Add to dict
                if dep_file_path not in dependency_files:
                    dependency_files[dep_file_path] = []
                dependency_files[dep_file_path].append(missing_export)
        
        return dependency_files
    
    def _resolve_import_path(self, importer: str, target: str, repo_dir: str) -> str:
        """Resolve import to file path with robust prefix handling."""
        frontend_prefix = self._frontend_prefix(repo_dir)
        backend_prefix = self._backend_prefix(repo_dir)
        def _pick_existing_frontend_file(base_no_ext: str) -> str:
            candidates = [
                f"{base_no_ext}.ts",
                f"{base_no_ext}.tsx",
                f"{base_no_ext}.js",
                f"{base_no_ext}.jsx",
                f"{base_no_ext}/index.ts",
                f"{base_no_ext}/index.tsx",
                f"{base_no_ext}/index.js",
                f"{base_no_ext}/index.jsx",
            ]
            for candidate in candidates:
                if os.path.exists(os.path.join(repo_dir, candidate)):
                    return candidate

            # Prefer TSX for components and app-level files, otherwise default to TS.
            lower = base_no_ext.lower()
            if (
                '/components/' in lower
                or '/ui/' in lower
                or lower.endswith('/page')
                or lower.endswith('/layout')
                or lower.endswith('/error')
                or os.path.basename(base_no_ext)[0].isupper()
            ):
                return f"{base_no_ext}.tsx"
            return f"{base_no_ext}.ts"

        if importer.endswith('.py'):
            # Strip common prefixes from target if they exist
            clean_target = target
            for prefix in ['backend.', 'app.', 'src.']:
                if clean_target.startswith(prefix):
                    clean_target = clean_target[len(prefix):]
            
            # Fix: Avoid double .py extension
            if clean_target.endswith('.py'):
                clean_target = clean_target[:-3]
                
            return self._apply_prefix(backend_prefix, f"{clean_target.replace('.', '/')}.py")
        else:
            importer_norm = importer.replace("\\", "/")
            if importer_norm.startswith("frontend/") and not frontend_prefix:
                importer_norm = importer_norm[9:]
            if frontend_prefix and not importer_norm.startswith(f"{frontend_prefix}/"):
                if importer_norm.startswith("src/") or importer_norm.startswith("app/"):
                    importer_norm = f"{frontend_prefix}/{importer_norm}"
                else:
                    importer_norm = f"{frontend_prefix}/src/{importer_norm.lstrip('./')}"

            target_norm = target.replace("\\", "/")

            # Resolve relative imports against importer directory (critical for ./ and ../ paths).
            if target_norm.startswith(("./", "../")):
                importer_dir = os.path.dirname(importer_norm)
                base = os.path.normpath(os.path.join(importer_dir, target_norm)).replace("\\", "/")
                return _pick_existing_frontend_file(base)

            # Handle @/ alias (Next.js)
            if target_norm.startswith("@/src/"):
                alias_base = self._apply_prefix(frontend_prefix, target_norm[2:])
                return _pick_existing_frontend_file(alias_base)
            if target_norm.startswith("@/"):
                alias_base = self._apply_prefix(frontend_prefix, f"src/{target_norm[2:]}")
                return _pick_existing_frontend_file(alias_base)

            # Handle ~/ alias often used as src root alias.
            if target_norm.startswith("~/src/"):
                alias_base = self._apply_prefix(frontend_prefix, target_norm[2:])
                return _pick_existing_frontend_file(alias_base)
            if target_norm.startswith("~/"):
                alias_base = self._apply_prefix(frontend_prefix, f"src/{target_norm[2:]}")
                return _pick_existing_frontend_file(alias_base)

            # Handle direct src/* imports
            if target_norm.startswith("src/"):
                src_base = self._apply_prefix(frontend_prefix, target_norm)
                return _pick_existing_frontend_file(src_base)

            # Fallback to frontend/src/<target>
            return _pick_existing_frontend_file(self._apply_prefix(frontend_prefix, f"src/{target_norm}"))
    
    def _is_test_file(self, file_path: str) -> bool:
        """Check if file is a test file"""
        test_indicators = [
            '/__tests__/',
            '.test.ts',
            '.test.tsx',
            '.test.js',
            '.test.jsx',
            '.spec.ts',
            '.spec.tsx',
            '/tests/',
            'test_',
            '_test.',
            '/mocks/',
            'playwright.config',
            'jest.config',
            'jest.setup'
        ]
        return any(indicator in file_path for indicator in test_indicators)
    
    def _should_try_programmatic(self, file_path: str, file_info: dict) -> bool:
        """Should we try programmatic fix?"""
        missing_items = [str(err) for err in file_info.get('missing', [])]
        file_content = file_info.get('content') or ""
        
        # CRITICAL: Always prioritize AI Concatenation Errors (embedded markers)
        # This must come BEFORE any file-path specific early returns that might skip it.
        if any("AI Concatenation Error" in err for err in missing_items):
            return True
            
        if file_path.endswith(('requirements.txt', 'package.json')):
            return True
        # Missing mandatory frontend config files - always create programmatically
        if file_path in ('frontend/next.config.js', 'frontend/tsconfig.json', 'frontend/jsconfig.json', 'next.config.js', 'tsconfig.json', 'jsconfig.json'):
            return True
        if file_path.endswith('.py') and '/models/' in file_path:
            return True
        if file_path.endswith('.py') and any('File does not exist - CREATE IT' in err for err in missing_items):
            return True
        # NEW: Python shim files with missing exports - fix without AI to avoid credit waste
        if file_path.endswith('.py') and any('Missing export:' in err for err in missing_items):
            return True
        if file_path.endswith('.py') and any('SyntaxError in' in err for err in missing_items):
            return True
        # Pydantic settings missing env vars -> add defaults programmatically.
        if file_path.endswith(('config.py', 'settings.py')) and any('Missing settings:' in err for err in missing_items):
            return True
        # Deterministic fix: main.py cannot use relative imports (from .x import y).
        if file_path.endswith('main.py') and any('CANNOT use relative imports' in err for err in missing_items):
            return True
        if self._is_test_file(file_path):
            return True  # Always try programmatic for test files
        # Deterministic syntax cleanup for common JSX inline-comment breakage (TS1005).
        if file_path.endswith(('.tsx', '.jsx')) and any(("1005" in err) or ("'...' expected" in err) for err in missing_items):
            return True
        # Deterministic fix for JSX accidentally generated inside .ts hooks/files.
        if file_path.endswith('.ts') and any(
            token in err for err in missing_items for token in ("'>' expected", "')' expected", "Property assignment expected")
        ):
            return True
        # Deterministic fix for missing TS exports in non-type files (e.g., src/api/*.ts).
        if file_path.endswith(('.ts', '.tsx')) and any(('Missing export:' in err) or ('not found in' in err) for err in missing_items):
            return True
        if file_path.endswith(('.ts', '.tsx', '.js', '.jsx')) and any(
            'File doesn\'t exist - CREATE IT' in err or 'Missing local module for import:' in err
            for err in missing_items
        ):
            return True
        # Deterministic fix for alias imports that incorrectly include /src/ segment.
        if file_path.endswith(('.ts', '.tsx', '.js', '.jsx')) and any('Fix import alias:' in err for err in missing_items):
            return True
        # Deterministic fix for auth page/component prop contract drift:
        # Type '{}' is missing required FormProps when rendering <LoginForm /> etc.
        if file_path.endswith(('.tsx', '.jsx')) and any(
            ("Type '{}' is missing the following properties from type" in err and "FormProps" in err)
            for err in missing_items
        ):
            return True
        # Deterministic fix for missing object properties in TS types
        if file_path.endswith(('.ts', '.tsx')) and any(
            ("does not exist in type" in err) or ("Property '" in err and "does not exist on type" in err)
            for err in missing_items
        ):
            return True
        if '/types/' in file_path and file_path.endswith('.ts'):
            # For missing exports in shared type files, deterministic export stubs are safer than repeated AI loops.
            if any('Missing export:' in err or 'not found in' in err for err in missing_items):
                return True
            if any("*/ expected" in err for err in missing_items):
                return True
            if any("Expression expected" in err or "Unterminated regular expression literal" in err for err in missing_items):
                return True
            return len(file_content) < 500
        # NEW: Try programmatic fix for files with "from backend.X" errors
        if file_path.endswith('.py') and any('backend.' in err for err in missing_items):
            return True
        # NEW: Pydantic BaseSettings re-export fix for config.py-style files
        if file_path.endswith('.py') and any("not found in '" in err for err in missing_items):
            return True
        if file_path.endswith(('.ts', '.tsx')) and any(("is not assignable to parameter of type" in err) or ("Type '" in err and "' is not assignable to type '" in err) for err in missing_items):
            # Potential type mismatch due to redundant definitions
            return True
        # NEW: Axios interceptor type mismatch (AxiosRequestConfig vs InternalAxiosRequestConfig)
        if file_path.endswith(('.ts', '.tsx')) and any(
            'AxiosRequestConfig' in err and 'InternalAxiosRequestConfig' in err
            for err in missing_items
        ):
            return True
        # NEW: Missing CSS/asset files
        if any('AssetError' in err and 'CSS file' in err for err in missing_items):
            return True
        return False
    
    async def _apply_programmatic_fix(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Apply programmatic fixes"""
        try:
            if file_path.endswith('requirements.txt'):
                return await self.backend_fixer._fix_requirements(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith('package.json'):
                return await self.frontend_fixer._fix_package_json(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            # Missing mandatory frontend configuration files - create with known-good boilerplate
            elif file_path in ('frontend/next.config.js', 'next.config.js'):
                return await self.frontend_fixer._create_next_config(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('tsconfig.json', 'jsconfig.json')) and any(
                'Missing path alias' in str(err) for err in file_info.get('missing', [])
            ):
                return await self.frontend_fixer._fix_tsconfig_path_alias(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path in ('frontend/tsconfig.json', 'tsconfig.json'):
                return await self.frontend_fixer._create_tsconfig(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            # NEW: Programmatic fix for AI Concatenation Errors
            elif any("AI Concatenation Error" in str(err) for err in file_info.get('missing', [])):
                return await self._split_concatenated_files(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            # Pydantic Settings missing required env vars
            elif file_path.endswith(('config.py', 'settings.py')) and any(
                'Missing settings:' in str(err) for err in file_info.get('missing', [])
            ):
                return await self.backend_fixer._fix_pydantic_settings_missing(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith('.py') and '/models/' in file_path:
                return await self.backend_fixer._fix_backend_model(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith('.py') and any('File does not exist - CREATE IT' in str(err) for err in file_info.get('missing', [])):
                return await self.backend_fixer._create_missing_backend_module(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            # NEW: Python files with "Missing export:" — append stubs without touching the AI
            elif file_path.endswith('.py') and any('Missing export:' in str(err) for err in file_info.get('missing', [])):
                if any('SyntaxError in' in str(err) for err in file_info.get('missing', [])):
                    await self.backend_fixer._fix_python_syntax_artifacts(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
                    local = os.path.join(repo_dir, file_path)
                    if os.path.exists(local):
                        try:
                            with open(local, 'r', encoding='utf-8') as f:
                                file_info = {**file_info, 'content': f.read()}
                        except Exception:
                            pass
                return await self.backend_fixer._fix_python_missing_exports(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith('.py') and any('SyntaxError in' in str(err) for err in file_info.get('missing', [])):
                if any('parameter without a default follows parameter with a default' in str(err) for err in file_info.get('missing', [])):
                    return await self.backend_fixer._fix_python_parameter_order(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
                if any(
                    ('unterminated triple-quoted f-string literal' in str(err)) or
                    ('unterminated triple-quoted string literal' in str(err))
                    for err in file_info.get('missing', [])
                ):
                    return await self.backend_fixer._fix_python_unterminated_triple_quote(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
                return await self.backend_fixer._fix_python_syntax_artifacts(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith('main.py') and any('CANNOT use relative imports' in str(err) for err in file_info.get('missing', [])):
                return await self.backend_fixer._fix_main_relative_imports(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.test.tsx', '.test.ts')):
                return await self.frontend_fixer._fix_test_file(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.tsx', '.jsx')) and any(("1005" in str(err)) or ("'...' expected" in str(err)) for err in file_info.get('missing', [])):
                return await self.frontend_fixer._fix_jsx_inline_comment_syntax(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith('.ts') and any(
                token in str(err) for err in file_info.get('missing', []) for token in ("'>' expected", "')' expected", "Property assignment expected")
            ):
                return await self.frontend_fixer._fix_ts_with_jsx_syntax(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.ts', '.tsx')) and any((('Missing export:' in str(err)) or ('not found in' in str(err))) for err in file_info.get('missing', [])) and '/types/' not in file_path:
                return await self.frontend_fixer._fix_ts_missing_exports(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.ts', '.tsx', '.js', '.jsx')) and any(
                'File doesn\'t exist - CREATE IT' in str(err) or 'Missing local module for import:' in str(err)
                for err in file_info.get('missing', [])
            ):
                return await self.frontend_fixer._create_missing_frontend_module(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.ts', '.tsx', '.js', '.jsx')) and any('Fix import alias:' in str(err) for err in file_info.get('missing', [])):
                return await self.frontend_fixer._fix_frontend_alias_src_prefix(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.tsx', '.jsx')) and any(
                ("Type '{}' is missing the following properties from type" in str(err) and "FormProps" in str(err))
                for err in file_info.get('missing', [])
            ):
                return await self.frontend_fixer._fix_auth_page_missing_form_props(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.ts', '.tsx')) and any(
                "TypeError in '" in str(err) and "expects at least" in str(err)
                for err in file_info.get('missing', [])
            ):
                return await self.frontend_fixer._fix_ts_call_arity(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.ts', '.tsx')) and any(
                "does not exist on type 'string'" in str(err)
                for err in file_info.get('missing', [])
            ):
                return await self.frontend_fixer._fix_ts_string_property_access(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.ts', '.tsx')) and any(
                ("does not exist in type" in str(err)) or ("Property '" in str(err) and "does not exist on type" in str(err))
                for err in file_info.get('missing', [])
            ):
                return await self.frontend_fixer._fix_ts_missing_type_property(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            # NEW: Axios interceptor type mismatch fix
            elif file_path.endswith(('.ts', '.tsx')) and any(
                'AxiosRequestConfig' in str(err) and 'InternalAxiosRequestConfig' in str(err)
                for err in file_info.get('missing', [])
            ):
                return await self.frontend_fixer._fix_axios_interceptor_type(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            # NEW: Missing CSS file fix
            elif file_path.endswith(('.ts', '.tsx', '.js', '.jsx')) and any('AssetError' in str(err) and 'CSS file' in str(err) for err in file_info.get('missing', [])):
                return await self.frontend_fixer._fix_missing_css_file(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif '/types/' in file_path and file_path.endswith('.ts'):
                return await self.frontend_fixer._fix_type_file(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            # NEW: Programmatic fix for "from backend.X" errors
            elif file_path.endswith('.py') and any('backend.' in str(err) for err in file_info.get('missing', [])):
                return await self.backend_fixer._fix_backend_prefix(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            # NEW: Pydantic BaseSettings re-export fix (e.g., config.py with SECRET_KEY: str = ...)
            elif file_path.endswith('.py') and any("not found in '" in str(err) for err in file_info.get('missing', [])):
                return await self.backend_fixer._fix_pydantic_settings_reexport(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            # NEW: Programmatic fix for redundant types
            elif file_path.endswith(('.ts', '.tsx')) and any(("is not assignable to parameter of type" in str(err)) or ("Type '" in str(err) and "' is not assignable to type '" in str(err)) for err in file_info.get('missing', [])):
                # First try deterministic auth contract fix (common login page drift).
                auth_fixed = await self.frontend_fixer._fix_frontend_auth_contract(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
                if auth_fixed:
                    return True
                return await self.frontend_fixer._fix_redundant_types(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            return False
        except Exception as e:
            logger.error(f"Programmatic fix error: {e}")
            return False

    async def _split_concatenated_files(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Split a file containing embedded AI --- / FILE_PATH: markers into separate files."""
        content = file_info.get('content', '')
        if not content:
            local = os.path.join(repo_dir, file_path)
            if os.path.exists(local):
                with open(local, 'r', encoding='utf-8') as f:
                    content = f.read()

        if not content:
            return False

        lines = content.splitlines()
        segments = []
        current_path = file_path
        current_lines = []
        
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            
            is_marker = False
            next_path = None
            
            if stripped == '---':
                if i + 1 < len(lines) and lines[i+1].strip().startswith('FILE_PATH:'):
                    is_marker = True
                    next_path = lines[i+1].strip().replace('FILE_PATH:', '').strip()
                    i += 1
                elif i + 2 < len(lines) and lines[i+2].strip().startswith('FILE_PATH:'):
                    is_marker = True
                    next_path = lines[i+2].strip().replace('FILE_PATH:', '').strip()
                    i += 2
            elif stripped.startswith('FILE_PATH:'):
                is_marker = True
                next_path = stripped.replace('FILE_PATH:', '').strip()
                if i + 1 < len(lines) and lines[i+1].strip() == '---':
                    i += 1
                elif i + 2 < len(lines) and lines[i+2].strip() == '---':
                    i += 2
                    
            if is_marker and next_path:
                if current_lines:
                    segments.append((current_path, '\\n'.join(current_lines)))
                current_path = self._coerce_generated_path(next_path, file_path, repo_dir)
                current_lines = []
            else:
                current_lines.append(line)
            i += 1
            
        if current_lines:
             segments.append((current_path, '\\n'.join(current_lines)))
             
        if len(segments) <= 1:
            return False
            
        success = True
        for p, seg_content in segments:
            # Aggressive cleanup of markdown artifacts and repeat markers
            # 1. Strip leading/trailing whitespaces and repeat newlines
            seg_content = seg_content.strip()
            
            # 2. Strip backticks if AI wrapped the segment
            if seg_content.startswith('```'):
                seg_content = re.sub(r'^```(?:\w+)?\n', '', seg_content)
            if seg_content.endswith('```'):
                seg_content = re.sub(r'\n```$', '', seg_content)
                
            # 3. Strip any residual FILE_PATH: markers that might be at the very top (redundant)
            seg_content = re.sub(r'^FILE_PATH:[^\n]+\n', '', seg_content).strip()
            
            # 4. Strip residual separator lines
            seg_content = re.sub(r'^-{3,}\n', '', seg_content)
            seg_content = re.sub(r'\n-{3,}$', '', seg_content)
            seg_content = seg_content.strip()
                    
            local_p = os.path.join(repo_dir, p)
            os.makedirs(os.path.dirname(local_p), exist_ok=True)
            with open(local_p, 'w', encoding='utf-8') as f:
                f.write(seg_content)
                
            cmt = await self._commit_programmatic_fix(job_id, p, seg_content, github_repo, github_branch)
            if not cmt:
                success = False
                
        if success:
            self._safe_log(job_id, f"🧹 Orchestrator: Split concatenated file into {len(segments)} files", "Programmatic Fix")
            
            # Additional cleanup for package.json segments that might have leaked AI commentary
            for p, _ in segments:
                if p.endswith('package.json'):
                    local_p = os.path.join(repo_dir, p)
                    if os.path.exists(local_p):
                        with open(local_p, 'r', encoding='utf-8') as f:
                            p_content = f.read()
                        p_data = self._resilient_json_parse(p_content)
                        if p_data:
                            cleaned_json = json.dumps(p_data, indent=2)
                            if cleaned_json.strip() != p_content.strip():
                                with open(local_p, 'w', encoding='utf-8') as f:
                                    f.write(cleaned_json)
                                await self._commit_programmatic_fix(job_id, p, cleaned_json, github_repo, github_branch)
                                self._safe_log(job_id, f"🧹 Post-split cleanup: Resiliently fixed JSON in {p}", "Programmatic Fix")
        return success
    
    def _find_dependency_file(self, repo_dir, filename):
        for d in ['frontend', 'backend', '']:
            p = os.path.join(repo_dir, d, filename)
            if os.path.exists(p):
                return p, os.path.relpath(p, repo_dir)
        return None, None
    
    def _format_deployment_failure_comment(self, job_id, story_key, backend_service, backend_logs, backend_health, frontend_service, frontend_logs, frontend_health, title, error_output):
        return {
            "version": 1,
            "type": "doc",
            "content": [
                {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": f"🚨 {title}"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": f"Story: {story_key}"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": f"Error: {error_output[:1000]}"}]}
            ]
        }

    def _resilient_json_parse(self, content: str) -> Optional[dict]:
        """
        Attempt to parse JSON even if it has leading/trailing noise or multiple blocks.
        Extracts the FIRST valid-looking JSON object or array.
        """
        content = content.strip()
        if not content:
            return None
            
        # Try direct parse first
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
            
        # Try all possible '{' start positions
        start_pos = 0
        while True:
            start_idx = content.find('{', start_pos)
            if start_idx == -1:
                break
                
            # Attempt to find the matching brace for THIS start position
            bracket_count = 0
            for i in range(start_idx, len(content)):
                if content[i] == '{':
                    bracket_count += 1
                elif content[i] == '}':
                    bracket_count -= 1
                    if bracket_count == 0:
                        try:
                            potential_json = content[start_idx:i+1]
                            return json.loads(potential_json)
                        except json.JSONDecodeError:
                            # Not valid JSON, keep looking for a later closing brace for THIS start
                            continue
            
            # If no valid JSON found starting at start_idx, try the next '{'
            start_pos = start_idx + 1
            
        return None
