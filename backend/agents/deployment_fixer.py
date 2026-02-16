"""
DEPLOYMENT FIXER - Optimized with proper batching and retry logic
"""

import os
import re
import asyncio
import subprocess
import json
from typing import List, Dict, Any, Optional
from log_config import logger
from agents.job_manager import job_store

class DeploymentFixer:
    """Optimized auto-fixer with batching"""
    
    def __init__(self, job_manager, gemini_service, github_service, gcloud_service, jira_service):
        self.job_manager = job_manager
        self.gemini_service = gemini_service
        self.github_service = github_service
        self.gcloud_service = gcloud_service
        self.jira_service = jira_service
        
        # Import validator for per-file validation
        from agents.deployment_validator import DeploymentValidator
        self.validator = DeploymentValidator()
        self._fix_attempt_history = {}
        self._fix_code_history = {}  # Track actual code sent in each attempt
        self._fix_error_history = {}  # Track errors present in each attempt
        self._last_cycle_files = {}  # Track which files were modified in the last cycle
        self._programmatic_fix_history = {}
        self._file_error_hashes = {}  # Track error signatures per file to detect no progress
        self._unfixable_files = {}  # Files that made things worse or showed no progress
        self._resurrection_count = {}  # Track how many times each file has been resurrected
    
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
                    errors = [e for e in all_errors if file_path in e or (file_path.replace('frontend/', '') in e)]
                
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
                        file_specific_errors = [e for e in file_errors if file_path in e or file_path.replace('backend/', '') in e or file_path.replace('frontend/', '') in e]
                        
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
            content_size = len(file_info.get('content', ''))
            
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
                timeout = 45.0  # Slightly longer for batches
                
                result = await self.gemini_service.generate_code(
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
                
                parsed = self.gemini_service.parse_generated_code(code)
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
    
    def _load_yaml_context(self, job_id: str, repo_dir: str, file_path: str = None) -> str:
        """Load YAML config context from job store - only relevant config based on file path"""
        try:
            context = ""
            job_data = job_store.get(job_id, {})
            
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
            
            # Load ONLY relevant config to reduce context size
            if is_frontend:
                frontend_config = job_data.get("frontend_config_content")
                if frontend_config:
                    context += "=== FRONTEND TECHNICAL REQUIREMENTS ===\n"
                    context += frontend_config[:4000]  # Increased from 3000 since we're only including one
                    context += "\n\n"
            
            if is_backend:
                backend_config = job_data.get("backend_config_content")
                if backend_config:
                    context += "=== BACKEND TECHNICAL REQUIREMENTS ===\n"
                    context += backend_config[:4000]  # Increased from 3000 since we're only including one
                    context += "\n\n"
            
            return context
        except Exception as e:
            logger.warning(f"Failed to load YAML context: {e}")
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
    
    async def _generate_file_fix(self, job_id: str, target_file: str, info: dict, github_repo: str, github_branch: str, repo_dir: str, yaml_context: str = "", story_context: str = "") -> Optional[str]:
        """Generate fixed content for a file without committing - returns content or None"""
        job_fix_history = self._fix_attempt_history.get(job_id, {})
        code_history = self._fix_code_history[job_id].get(target_file, [])
        error_history = self._fix_error_history[job_id].get(target_file, [])
        
        prompt = self._build_fix_prompt(target_file, info, yaml_context, story_context, job_fix_history, code_history, error_history, repo_dir)
        
        try:
            content_size = len(info.get('content', ''))
            base_timeout = 60.0 if target_file.endswith(('.tsx', '.jsx', '.ts')) else 45.0
            timeout = base_timeout + (content_size / 1000) * 10.0
            timeout = min(timeout, 150.0)
            
            result = await self.gemini_service.generate_code(
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
            
            parsed = self.gemini_service.parse_generated_code(code)
            if not parsed or not parsed[0]:
                return None
            
            # Return the content
            return parsed[0]['content']
            
        except Exception as e:
            logger.error(f"Generate fix failed for {target_file}: {e}")
            return None
    
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
                
                result = await self.gemini_service.generate_code(
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
                
                parsed = self.gemini_service.parse_generated_code(code)
                if not parsed:
                    self.job_manager.log(job_id, f"❌ {target_file}: Failed to parse AI output (retry {retry+1}/{max_retries})", "Parse Failed", level="WARNING")
                    if retry < max_retries - 1:
                        continue
                    else:
                        return False
                
                if parsed:
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
                        if final_path.startswith('backend/'):
                            final_path = final_path[8:]
                        elif final_path.startswith('frontend/'):
                            final_path = final_path[9:]
                        
                        if self.github_service.commit_file(owner, repo_name, github_branch, final_path, content, f"[FIX] {final_path}"):
                            self.job_manager.log(job_id, f"Successfully committed fix for {final_path} to {target_repo}", "Fix Applied")
                            
                            # Sync locally
                            full_path = os.path.join(repo_dir, path)
                            os.makedirs(os.path.dirname(full_path), exist_ok=True)
                            with open(full_path, 'w') as file:
                                file.write(content)
                            
                            # Record attempt in history
                            if target_file not in self._fix_code_history[job_id]: self._fix_code_history[job_id][target_file] = []
                            self._fix_code_history[job_id][target_file].append(content)
                            self._last_cycle_files[job_id].append(target_file)
                            
                            return True
                    return True
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
        
        # CRITICAL: Add YAML config context FIRST with strong emphasis
        prompt += "🚨 MANDATORY TECHNICAL REQUIREMENTS 🚨\n"
        prompt += "="*80 + "\n"
        prompt += "You MUST follow the technical requirements specified in the config files below.\n"
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
            # DEBUG: Log that we're sending config
            config_type = "FRONTEND" if "FRONTEND TECHNICAL" in yaml_context else "BACKEND" if "BACKEND TECHNICAL" in yaml_context else "UNKNOWN"
            config_length = len(yaml_context)
            logger.info(f"🔍 DEBUG: Sending {config_type} tech config to AI ({config_length} chars) for {file_path}")
            # Log first 200 chars as preview
            preview = yaml_context[:200].replace('\n', ' ')
            logger.info(f"   Config Preview: {preview}...")
            prompt += yaml_context
        else:
            # WARNING: No config sent
            logger.warning(f"⚠️ WARNING: NO technical config sent to AI for {file_path}")
        
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
        
        prompt += "### ✅ SUCCESS = ZERO ERRORS ###\n"
        prompt += "After your fix, ALL these must be true:\n"
        prompt += "- Every error in the list above is completely gone\n"
        prompt += "- No new errors are introduced\n"
        prompt += "- Code compiles without any errors\n"
        prompt += "- All imports resolve to real packages/files\n"
        prompt += "- All exports exist for dependent files\n\n"
        
        prompt += "Output format:\n"
        prompt += "ROOT_CAUSE: <Why these errors exist - the fundamental problem>\n"
        prompt += "COMPLETE_FIX: <Exact changes that will eliminate ALL errors listed above>\n"
        prompt += "VERIFICATION: <How you verified ALL errors will be gone after this fix>\n\n"
        prompt += f"FILE_PATH: {file_path}\n---\n[COMPLETE ERROR-FREE CODE]\n---\n"
        return prompt
    
    def _parse_all_errors(self, errors: List[str], repo_dir: str) -> Dict[str, Dict]:
        """Parse ALL error types with proper path normalization and CROSS-FILE dependency detection"""
        files = {}
        
        for error in errors:
            # Missing dependency
            if "Missing dependency:" in error:
                pkg_match = re.search(r"Missing dependency: '([^']+)'", error)
                if pkg_match:
                    pkg = pkg_match.group(1)
                    file_path = "backend/requirements.txt" if "requirements.txt" in error else "frontend/package.json"
                    
                    if file_path not in files:
                        local = os.path.join(repo_dir, file_path)
                        content = ''
                        if os.path.exists(local):
                            with open(local, 'r') as f:
                                content = f.read()
                        files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                    files[file_path]['packages'].add(pkg)
            
            # ImportError - CRITICAL: Fix BOTH the dependency file AND the importer
            match = re.search(r"ImportError in '([^']+)': '([^']+)' not found in '([^']+)'", error)
            if match:
                importer_path = match.group(1)
                missing_name = match.group(2)
                target_module = match.group(3)
                
                # PRIMARY FIX: The dependency file that's missing the export
                file_path = self._resolve_import_path(importer_path, target_module, repo_dir)
                file_path = self._normalize_file_path(file_path)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = ''
                    if os.path.exists(local):
                        with open(local, 'r') as f:
                            content = f.read()
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
                
                # CRITICAL: Distinguish between pip packages and local files
                # If module name has no dots/slashes and matches common package patterns, it's a package
                is_package = (
                    '/' not in module_trying_to_import and
                    '.' not in module_trying_to_import and
                    not module_trying_to_import.startswith(('.', '@')) and
                    (
                        # Common package name patterns (prefix match)
                        any(module_trying_to_import.startswith(prefix) for prefix in [
                            'pydantic', 'jose', 'bcrypt', 'jwt', 'fastapi', 'uvicorn',
                            'google', 'firebase', 'sqlalchemy', 'alembic', 'redis',
                            'celery', 'requests', 'httpx', 'aiohttp', 'boto3', 'stripe',
                            'flask', 'django', 'numpy', 'pandas', 'scipy', 'sklearn',
                            'tensorflow', 'torch', 'keras'
                        ]) or
                        # Exact package names (common packages with short names)
                        module_trying_to_import in [
                            'jose', 'bcrypt', 'passlib', 'email_validator', 
                            'flask', 'django', 'redis', 'celery', 'pytest',
                            'numpy', 'pandas', 'scipy', 'click', 'jinja2'
                        ]
                    )
                )
                
                if is_package:
                    # This is a missing pip package, not a file!
                    file_path = "backend/requirements.txt"
                    if file_path not in files:
                        local = os.path.join(repo_dir, file_path)
                        content = ''
                        if os.path.exists(local):
                            with open(local, 'r') as f:
                                content = f.read()
                        files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                    # Map to correct package name
                    pkg_name = {
                        'pydantic_settings': 'pydantic-settings',
                        'jose': 'python-jose[cryptography]',
                        'jwt_utils': 'PyJWT',
                        'email_validator': 'email-validator',
                        'passlib': 'passlib[bcrypt]'
                    }.get(module_trying_to_import, module_trying_to_import)
                    files[file_path]['packages'].add(pkg_name)
                    continue
                
                # It's a local file that needs to be created
                file_path = f"backend/{missing_file}"
                file_path = self._normalize_file_path(file_path)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = ''
                    if os.path.exists(local):
                        with open(local, 'r') as f:
                            content = f.read()
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(f"File does not exist - CREATE IT with module: {module_trying_to_import}")
                continue
            
            # NEW: Handle both variations of wrong import format errors
            # Pattern 1: "Using 'from backend.X' - MUST use 'from X' instead"
            # Pattern 2: "Cannot import from 'backend.X' - file 'backend/X.py' does not exist" (when file exists but import is wrong)
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
                file_path = f"backend/{file_with_error.replace('.', '/')}.py"
                file_path = self._normalize_file_path(file_path)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = ''
                    if os.path.exists(local):
                        with open(local, 'r') as f:
                            content = f.read()
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(f"Fix import: Change 'from backend.{wrong_import}' to 'from {correct_import}'")
                continue
            
            # Legacy pattern kept for backwards compatibility
            wrong_import_legacy = re.search(r"ImportError in '([^']+)': Using 'from backend\.([^']+)' - MUST use 'from ([^']+)' instead", error)
            if wrong_import_legacy:
                file_with_error = wrong_import_match.group(1)
                wrong_import = wrong_import_match.group(2)
                correct_import = wrong_import_match.group(3)
                
                # The file that has the wrong import
                file_path = f"backend/{file_with_error.replace('.', '/')}.py"
                file_path = self._normalize_file_path(file_path)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = ''
                    if os.path.exists(local):
                        with open(local, 'r') as f:
                            content = f.read()
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(f"Fix import: Change 'from backend.{wrong_import}' to 'from {correct_import}'")
                continue
            
            # TypeScript error - check if it's a missing module (package)
            tsc_match = re.search(r"TypeScript error in '([^']+)' at", error)
            if tsc_match:
                # Check if it's a "Cannot find module" error indicating missing package
                module_match = re.search(r"2307: Cannot find module '([^']+)'", error)
                if module_match:
                    missing_module = module_match.group(1)
                    # Extract package name (first part before /)
                    if '/' in missing_module:
                        pkg = missing_module.split('/')[0]
                        # If it starts with @, include the scope
                        if missing_module.startswith('@'):
                            parts = missing_module.split('/')
                            if len(parts) >= 2:
                                pkg = f"{parts[0]}/{parts[1]}"
                        
                        # This is a missing package - add to package.json
                        file_path = "frontend/package.json"
                        if file_path not in files:
                            local = os.path.join(repo_dir, file_path)
                            content = ''
                            if os.path.exists(local):
                                with open(local, 'r') as f:
                                    content = f.read()
                            files[file_path] = {'missing': [], 'content': content, 'packages': set()}
                        files[file_path]['packages'].add(pkg)
                        continue
                
                # Regular TypeScript error
                file_rel = tsc_match.group(1)
                file_path = f"frontend/{file_rel}" if not file_rel.startswith('frontend/') else file_rel
                file_path = self._normalize_file_path(file_path)
                
                if file_path not in files:
                    local = os.path.join(repo_dir, file_path)
                    content = ''
                    if os.path.exists(local):
                        with open(local, 'r') as f:
                            content = f.read()
                    files[file_path] = {'missing': [], 'content': content}
                files[file_path]['missing'].append(error)
        
        return files
    
    def _normalize_file_path(self, path: str) -> str:
        """Normalize file path to prevent duplication (e.g., frontend/src/src/api -> frontend/src/api)"""
        # Remove duplicate path segments
        parts = path.split('/')
        normalized = []
        
        for part in parts:
            # Don't add duplicate consecutive parts
            if not normalized or part != normalized[-1]:
                normalized.append(part)
        
        result = '/'.join(normalized)
        
        # Ensure proper prefix structure
        if result.startswith('frontend/'):
            # Frontend files should be: frontend/src/... or frontend/package.json etc.
            after_frontend = result[9:]  # Remove 'frontend/'
            # If it doesn't start with src/, app/, or is a config file, add src/
            if not after_frontend.startswith(('src/', 'app/', 'package.json', 'tsconfig', 'next.config')):
                result = f"frontend/src/{after_frontend}"
        elif result.startswith('backend/'):
            # Backend files are fine as-is
            pass
        
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
                dep_file_path = self._normalize_file_path(dep_file_path)
                
                # Add to dict
                if dep_file_path not in dependency_files:
                    dependency_files[dep_file_path] = []
                dependency_files[dep_file_path].append(missing_export)
        
        return dependency_files
    
    def _resolve_import_path(self, importer: str, target: str, repo_dir: str) -> str:
        """Resolve import to file path with robust prefix handling"""
        if importer.endswith('.py'):
            # Strip common prefixes from target if they exist
            clean_target = target
            for prefix in ['backend.', 'app.', 'src.']:
                if clean_target.startswith(prefix):
                    clean_target = clean_target[len(prefix):]
            
            # Fix: Avoid double .py extension
            if clean_target.endswith('.py'):
                clean_target = clean_target[:-3]
                
            return f"backend/{clean_target.replace('.', '/')}.py"
        else:
            if target.startswith('../types/'):
                return f"frontend/src/types/{target.split('/')[-1]}.ts"
            elif target.startswith('../'):
                return f"frontend/{target.replace('../', 'src/')}.ts"
            else:
                # Handle @/ prefix common in Next.js
                clean_target = target
                if clean_target.startswith('@/'):
                    clean_target = clean_target[2:]
                return f"frontend/src/{clean_target}.ts"
    
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
        if file_path.endswith(('requirements.txt', 'package.json')):
            return True
        if file_path.endswith('.py') and '/models/' in file_path:
            return True
        if self._is_test_file(file_path):
            return True  # Always try programmatic for test files
        if '/types/' in file_path and file_path.endswith('.ts'):
            return len(file_info.get('content', '')) < 500
        return False
    
    async def _apply_programmatic_fix(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Apply programmatic fixes"""
        try:
            if file_path.endswith('requirements.txt'):
                return await self._fix_requirements(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith('package.json'):
                return await self._fix_package_json(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith('.py') and '/models/' in file_path:
                return await self._fix_backend_model(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif file_path.endswith(('.test.tsx', '.test.ts')):
                return await self._fix_test_file(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            elif '/types/' in file_path and file_path.endswith('.ts'):
                return await self._fix_type_file(file_path, file_info, github_repo, github_branch, repo_dir, job_id)
            return False
        except Exception as e:
            logger.error(f"Programmatic fix error: {e}")
            return False
    
    async def _fix_requirements(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Add missing packages"""
        packages = file_info.get('packages', set())
        if not packages:
            return False
        
        local = os.path.join(repo_dir, file_path)
        original_content = file_info.get('content', '')
        content = original_content
        
        # Map module names to actual PyPI packages
        pkg_map = {
            'jwt_utils': 'PyJWT',
            'jose': 'python-jose[cryptography]',
            'dotenv': 'python-dotenv',
            'email-validator': 'email-validator',
            'pydantic': 'pydantic',
            'pydantic_settings': 'pydantic-settings',
            'bcrypt': 'bcrypt',
            'passlib': 'passlib[bcrypt]'
        }
        
        # CRITICAL: Filter out invalid package names (model names, local modules, etc.)
        invalid_packages = {
            'ticket', 'user', 'auth', 'database', 'models', 'services',
            'routes', 'utils', 'config', 'main', 'app', 'backend', 'frontend'
        }
        
        for pkg in packages:
            # Skip invalid packages
            if pkg.lower() in invalid_packages:
                self.job_manager.log(job_id, f"⏭️ Skipping invalid package: {pkg} (likely a model/module name)", "Package Filter")
                continue
            
            real_pkg = pkg_map.get(pkg, pkg)
            
            # Only add if it's a known mapping or starts with common package prefixes
            is_known = pkg in pkg_map
            is_valid_prefix = any(real_pkg.startswith(prefix) for prefix in ['python-', 'google-', 'firebase-', 'django-', 'flask-', 'fastapi'])
            
            if not is_known and not is_valid_prefix:
                self.job_manager.log(job_id, f"⏭️ Skipping unknown package: {pkg} (not in package map)", "Package Filter")
                continue
            
            # Use regex for more robust package check (avoid matching 'pydantic' in 'pydantic-settings')
            if not re.search(rf'^{re.escape(real_pkg)}(\[.*\])?([<>=!].*)?$', content, re.MULTILINE):
                content += f"\n{real_pkg}"
        
        if content == original_content:
            return False

        with open(local, 'w') as f:
            f.write(content)
        
        final = file_path.replace('backend/', '')
        owner, repo = github_repo.split('/')
        return self.github_service.commit_file(owner, repo, github_branch, final, content, f"[PROG] {final}")
    
    async def _fix_package_json(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Add missing packages and regenerate package-lock.json"""
        packages = file_info.get('packages', set())
        if not packages:
            return False
        
        local = os.path.join(repo_dir, file_path)
        original_content = file_info.get('content', '{}')
        data = json.loads(original_content)
        
        deps = data.get('dependencies', {})
        dev_deps = data.get('devDependencies', {})
        
        modified = False
        
        # Well-known packages that should be added
        known_packages = {
            'zustand': '^4.5.0',
            'react-icons': '^5.0.0',
            '@heroicons/react': '^2.1.0',
            'react-hook-form': '^7.50.0',
            '@types/react': '^18.2.0',
            '@types/node': '^20.11.0'
        }
        
        for pkg in packages:
            if pkg not in deps and pkg not in dev_deps:
                if pkg.startswith('@testing-library') or pkg in ['jest', 'ts-jest', '@types/jest']:
                    dev_deps[pkg] = '^14.0.0' if pkg.startswith('@testing-library') else 'latest'
                elif pkg in known_packages:
                    # Use known version for well-known packages
                    deps[pkg] = known_packages[pkg]
                    modified = True
                    self.job_manager.log(job_id, f"✅ Adding well-known package: {pkg}@{known_packages[pkg]}", "Package Add")
                elif pkg.startswith(('@', 'react-', 'next-')):
                    # Common frontend packages
                    deps[pkg] = 'latest'
                    modified = True
                else:
                    deps[pkg] = 'latest'
                    modified = True
        
        if not modified:
            return False

        data['dependencies'] = deps
        data['devDependencies'] = dev_deps
        content = json.dumps(data, indent=2)
        
        with open(local, 'w') as f:
            f.write(content)
        
        # CRITICAL FIX: Regenerate package-lock.json to match package.json
        # This prevents "npm ci" failures due to lock file being out of sync
        frontend_dir = os.path.dirname(local)
        self.job_manager.log(job_id, f"Regenerating package-lock.json to match package.json", "Lock File Sync")
        
        try:
            # Run npm install --package-lock-only to update lock file without installing
            result = subprocess.run(
                ['npm', 'install', '--package-lock-only', '--legacy-peer-deps'],
                cwd=frontend_dir,
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode == 0:
                self.job_manager.log(job_id, "✅ Successfully regenerated package-lock.json", "Lock File Sync")
                
                # Commit both package.json and package-lock.json
                final = file_path.replace('frontend/', '')
                owner, repo = github_repo.split('/')
                
                # Commit package.json
                if not self.github_service.commit_file(owner, repo, github_branch, final, content, f"[PROG] {final}"):
                    return False
                
                # Commit package-lock.json
                lock_file_path = os.path.join(frontend_dir, 'package-lock.json')
                if os.path.exists(lock_file_path):
                    with open(lock_file_path, 'r') as f:
                        lock_content = f.read()
                    
                    lock_final = 'package-lock.json' if 'frontend/' in file_path else final.replace('package.json', 'package-lock.json')
                    self.github_service.commit_file(owner, repo, github_branch, lock_final, lock_content, f"[PROG] Sync {lock_final}")
                    self.job_manager.log(job_id, "✅ Committed synchronized package-lock.json", "Lock File Sync")
                
                return True
            else:
                self.job_manager.log(job_id, f"⚠️ Failed to regenerate lock file: {result.stderr[:200]}", "Lock File Warning", level="WARNING")
                # Still commit package.json even if lock regeneration failed
                final = file_path.replace('frontend/', '')
                owner, repo = github_repo.split('/')
                return self.github_service.commit_file(owner, repo, github_branch, final, content, f"[PROG] {final}")
        except Exception as e:
            self.job_manager.log(job_id, f"⚠️ Exception during lock file regen: {str(e)[:100]}", "Lock File Error", level="WARNING")
            # Still commit package.json
            final = file_path.replace('frontend/', '')
            owner, repo = github_repo.split('/')
            return self.github_service.commit_file(owner, repo, github_branch, final, content, f"[PROG] {final}")
    
    async def _fix_backend_model(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Add missing classes"""
        missing = [m for m in file_info.get('missing', []) if isinstance(m, str) and not m.startswith('Property')]
        if not missing:
            return False
        
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        
        if len(content) < 100:
            content = "from pydantic import BaseModel\n\n"
            for item in missing:
                content += f"class {item.strip()}(BaseModel):\n    pass\n\n"
        else:
            # Check for existing imports
            if "from pydantic import" not in content and "import pydantic" not in content:
                content = "from pydantic import BaseModel\n" + content
                
            for item in missing:
                # Use regex for more robust check of existing class definition
                if not re.search(rf"class\s+{item.strip()}\b", content):
                    # Ensure there's a newline before the new class
                    if not content.endswith("\n\n"):
                        content += "\n" if content.endswith("\n") else "\n\n"
                    content += f"class {item.strip()}(BaseModel):\n    pass\n"
        
        with open(local, 'w') as f:
            f.write(content)
        
        # Route to backend repo
        job_data = job_store.get(job_id, {})
        all_repos = job_data.get("all_repos", [])
        target_repo = github_repo
        backend_repo = next((r for r in all_repos if r.get('type') == 'backend'), None)
        if backend_repo:
            target_repo = f"{backend_repo['owner']}/{backend_repo['repo']}"
        
        final = file_path.replace('backend/', '')
        owner, repo = target_repo.split('/')
        return self.github_service.commit_file(owner, repo, github_branch, final, content, f"[PROG] {final}")
    
    async def _fix_test_file(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Add jest types"""
        local = os.path.join(repo_dir, file_path)
        if not os.path.exists(local):
            return False
        
        with open(local, 'r') as f:
            content = f.read()
        
        if '/// <reference types="jest" />' not in content:
            content = '/// <reference types="jest" />\n' + content
            with open(local, 'w') as f:
                f.write(content)
            
            final = file_path.replace('frontend/', '')
            owner, repo = github_repo.split('/')
            return self.github_service.commit_file(owner, repo, github_branch, final, content, f"[PROG] {final}")
        return False
    
    async def _fix_type_file(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Add missing type exports"""
        missing = file_info.get('missing', [])
        if not missing:
            return False
        
        local = os.path.join(repo_dir, file_path)
        original_content = file_info.get('content', '')
        content = original_content
        
        if len(content) < 100:
            content = "// Auto-generated\n\n"
            for item in missing:
                content += f"export interface {item.strip()} {{\n  // TODO\n}}\n\n"
        else:
            for item in missing:
                if f"export interface {item.strip()}" not in content:
                    content += f"\nexport interface {item.strip()} {{\n  // TODO\n}}\n"
        
        if content == original_content:
            return False

        with open(local, "w") as f:
            f.write(content)
        
        final = file_path.replace("frontend/", "")
        owner, repo = github_repo.split("/")
        return self.github_service.commit_file(owner, repo, github_branch, final, content, f"[PROG] {final}")
    
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
