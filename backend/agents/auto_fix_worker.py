"""
AUTO-FIX WORKER AGENT - Processes individual file fixes in parallel
"""

import os
import asyncio
import subprocess
from typing import Dict, Any, List
from log_config import logger
from agents.deployment_fixer import DeploymentFixer


class AutoFixWorker:
    """Worker agent that processes auto-fix tasks from a queue"""
    
    def __init__(self, worker_id: int, job_manager, gemini_service, github_service, jira_service):
        self.worker_id = worker_id
        self.job_manager = job_manager
        self.gemini_service = gemini_service
        self.github_service = github_service
        self.jira_service = jira_service
        
        # Each worker has its own fixer instance to avoid conflicts
        self.fixer = DeploymentFixer(job_manager, gemini_service, github_service, jira_service)
        
        self.is_busy = False
        self.current_task = None
        
        # Validation cache is intentionally disabled for pass decisions.
        # File-level pass/fail can change as other files change in the same cycle.
        self._validation_cache = {}
    
    def _log(self, job_id: str, message: str, category: str = "Worker"):
        """
        Safe logging that writes to agent_execution.log for UI visibility.
        Falls back to logger if job_manager fails.
        """
        try:
            self.job_manager.log(job_id, message, category)
        except (KeyError, AttributeError):
            logger.info(f"[Job {job_id}] [{category}] {message}")

    def _snapshot_files(self, repo_dir: str, file_paths: List[str]) -> Dict[str, Dict[str, Any]]:
        """Capture current local file state so failed attempts can be rolled back."""
        snapshot: Dict[str, Dict[str, Any]] = {}
        for rel_path in file_paths:
            abs_path = os.path.join(repo_dir, rel_path)
            if os.path.exists(abs_path):
                try:
                    with open(abs_path, 'r', encoding='utf-8') as f:
                        snapshot[rel_path] = {'exists': True, 'content': f.read()}
                except Exception:
                    snapshot[rel_path] = {'exists': True, 'content': None}
            else:
                snapshot[rel_path] = {'exists': False, 'content': None}
        return snapshot

    def _restore_snapshot(self, repo_dir: str, snapshot: Dict[str, Dict[str, Any]]):
        """Restore files captured by _snapshot_files."""
        for rel_path, state in snapshot.items():
            abs_path = os.path.join(repo_dir, rel_path)
            if state.get('exists'):
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                content = state.get('content')
                if content is not None:
                    with open(abs_path, 'w', encoding='utf-8') as f:
                        f.write(content)
            else:
                if os.path.exists(abs_path):
                    try:
                        os.remove(abs_path)
                    except Exception:
                        pass

    def _refresh_frontend_dependencies(self, repo_dir: str, job_id: str) -> bool:
        """Install/update frontend deps when package.json is changed during a fix attempt."""
        frontend_dir = os.path.join(repo_dir, 'frontend')
        package_json = os.path.join(frontend_dir, 'package.json')
        if not os.path.exists(package_json):
            return False

        lock_exists = os.path.exists(os.path.join(frontend_dir, 'package-lock.json'))
        npm_cmd = ['npm', 'ci'] if lock_exists else ['npm', 'install', '--prefer-offline', '--no-audit']
        try:
            self._log(job_id, f"📦 Worker {self.worker_id}: Refreshing frontend deps ({' '.join(npm_cmd)})", f"Worker {self.worker_id}")
            result = subprocess.run(
                npm_cmd,
                cwd=frontend_dir,
                capture_output=True,
                text=True,
                timeout=180
            )
            if result.returncode != 0:
                # package.json often changes before package-lock is regenerated; fallback to npm install.
                fallback_cmd = ['npm', 'install', '--prefer-offline', '--no-audit']
                self._log(job_id, f"⚠️ Worker {self.worker_id}: Dependency refresh failed, retrying with {' '.join(fallback_cmd)}", f"Worker {self.worker_id}")
                fallback = subprocess.run(
                    fallback_cmd,
                    cwd=frontend_dir,
                    capture_output=True,
                    text=True,
                    timeout=240
                )
                if fallback.returncode != 0:
                    self._log(
                        job_id,
                        f"⚠️ Worker {self.worker_id}: Dependency refresh failed: {fallback.stderr[:500]}",
                        f"Worker {self.worker_id}"
                    )
                    return False
                return True
            return True
        except Exception as e:
            self._log(job_id, f"⚠️ Worker {self.worker_id}: Dependency refresh error: {e}", f"Worker {self.worker_id}")
            return False
    
    async def process_fix_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Process a single file fix task"""
        self.is_busy = True
        self.current_task = task
        
        try:
            job_id = task['job_id']
            file_path = task['file_path']
            file_info = task['file_info']
            github_repo = task['github_repo']
            github_branch = task['github_branch']
            repo_dir = task['repo_dir']
            repo_lock = task.get('repo_lock')
            yaml_context = task.get('yaml_context', '')
            story_context = task.get('story_context', '')
            
            self._log(job_id, f"🤖 Worker {self.worker_id}: Fixing {file_path}...", f"Worker {self.worker_id}")
            
            # Try programmatic fix first
            if self.fixer._should_try_programmatic(file_path, file_info):
                if await self.fixer._apply_programmatic_fix(file_path, file_info, github_repo, github_branch, repo_dir, job_id):
                    self._log(job_id, f"✅ Worker {self.worker_id}: Programmatic fix succeeded for {file_path}", f"Worker {self.worker_id}")
                    return {'success': True, 'file_path': file_path, 'method': 'programmatic'}
            
            # AI-based fix with validation
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                # Generate fix bundle (target + related files from same repo)
                fix_bundle = await self.fixer._generate_file_fix_bundle(
                    job_id, file_path, file_info, github_repo, github_branch, 
                    repo_dir, yaml_context, story_context
                )
                
                if not fix_bundle or not fix_bundle.get('target_content'):
                    self._log(job_id, f"⏱️ Worker {self.worker_id}: Failed to generate fix for {file_path} (attempt {attempt})", f"Worker {self.worker_id}")
                    continue
                fixed_content = fix_bundle['target_content']
                related_files = fix_bundle.get('related_files', [])

                staged_related = [
                    related for related in related_files
                    if related.get('file_path') and related.get('content') is not None
                ]
                stage_paths = [file_path] + [r['file_path'] for r in staged_related]

                # Serialize local workspace mutations to prevent workers from corrupting each other's validation context.
                lock_ctx = repo_lock if repo_lock is not None else asyncio.Lock()
                async with lock_ctx:
                    # Capture baseline global error count; do not commit file-local passes that regress overall validation.
                    from agents.deployment_validator import DeploymentValidator
                    validator = DeploymentValidator()
                    baseline_errors = validator.validate_all(repo_dir)
                    baseline_error_count = len(baseline_errors)

                    snapshot = self._snapshot_files(repo_dir, stage_paths)
                    try:
                        # Write locally for validation.
                        local_path = os.path.join(repo_dir, file_path)
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        with open(local_path, 'w', encoding='utf-8') as f:
                            f.write(fixed_content)

                        for related in staged_related:
                            related_path = related['file_path']
                            related_content = related['content']
                            related_local = os.path.join(repo_dir, related_path)
                            os.makedirs(os.path.dirname(related_local), exist_ok=True)
                            with open(related_local, 'w', encoding='utf-8') as f:
                                f.write(related_content)
                        if staged_related:
                            self._log(job_id, f"📦 Worker {self.worker_id}: Staged {len(staged_related)} related file(s) for {file_path}", f"Worker {self.worker_id}")

                        # If package.json changed, refresh node_modules before TS validation
                        # so "Cannot find module" checks reflect new dependencies.
                        if any(path.endswith('package.json') for path in stage_paths):
                            deps_ok = self._refresh_frontend_dependencies(repo_dir, job_id)
                            if deps_ok:
                                # Keep lockfile synced with package.json in commits to avoid repeated npm ci failures.
                                lock_rel = "frontend/package-lock.json"
                                lock_abs = os.path.join(repo_dir, lock_rel)
                                if os.path.exists(lock_abs) and lock_rel not in stage_paths:
                                    try:
                                        with open(lock_abs, 'r', encoding='utf-8') as f:
                                            lock_content = f.read()
                                        staged_related.append({'file_path': lock_rel, 'content': lock_content})
                                        stage_paths.append(lock_rel)
                                        if lock_rel not in snapshot:
                                            snapshot[lock_rel] = {'exists': True, 'content': lock_content}
                                        self._log(job_id, f"📦 Worker {self.worker_id}: Staged refreshed package-lock.json", f"Worker {self.worker_id}")
                                    except Exception:
                                        pass

                        # Always validate freshly before committing.
                        self._log(job_id, f"🔍 Worker {self.worker_id}: Validating {file_path} (attempt {attempt})...", f"Worker {self.worker_id}")

                        all_errors = validator.validate_all(repo_dir)

                        # Check if this specific file still has errors
                        file_specific_errors = [
                            e for e in all_errors
                            if self.fixer._error_mentions_file(e, file_path)
                        ]

                        # Include cross-file dependency errors mapped to this file.
                        try:
                            parsed_error_map = self.fixer._parse_all_errors(all_errors, repo_dir)
                            mapped_entry = parsed_error_map.get(file_path, {})
                            mapped_errors = list(mapped_entry.get('missing', []))
                            for pkg in mapped_entry.get('packages', set()):
                                mapped_errors.append(f"Missing dependency: {pkg}")
                            for mapped in mapped_errors:
                                if mapped not in file_specific_errors:
                                    file_specific_errors.append(mapped)
                        except Exception:
                            pass

                        # Keep lightweight observability cache only; not used to skip validation.
                        self._validation_cache[file_path] = (attempt, len(file_specific_errors) > 0)

                        if not file_specific_errors:
                            # Guardrail: avoid committing changes that increase total static-analysis errors.
                            if len(all_errors) > baseline_error_count:
                                self._log(
                                    job_id,
                                    f"⚠️ Worker {self.worker_id}: Rejecting {file_path} attempt {attempt} due to global regression ({baseline_error_count}→{len(all_errors)} errors)",
                                    f"Worker {self.worker_id}"
                                )
                                file_info['missing'] = [f"Global regression: {baseline_error_count}->{len(all_errors)} errors"]
                                self._restore_snapshot(repo_dir, snapshot)
                                continue

                            # SUCCESS - commit
                            self._log(job_id, f"✅ Worker {self.worker_id}: Validation passed for {file_path} - committing...", f"Worker {self.worker_id}")

                            if await self.fixer._commit_file_fix(job_id, file_path, fixed_content, github_repo, github_branch):
                                committed_related = 0
                                for related in staged_related:
                                    related_path = related['file_path']
                                    related_content = related['content']
                                    if await self.fixer._commit_file_fix(job_id, related_path, related_content, github_repo, github_branch):
                                        committed_related += 1
                                if committed_related > 0:
                                    self._log(job_id, f"📦 Worker {self.worker_id}: Committed {committed_related} related file(s)", f"Worker {self.worker_id}")
                                self._log(job_id, f"✅ Worker {self.worker_id}: Successfully fixed and committed {file_path}", f"Worker {self.worker_id}")
                                return {'success': True, 'file_path': file_path, 'method': 'ai', 'attempts': attempt}

                            self._log(job_id, f"❌ Worker {self.worker_id}: Commit failed for {file_path}", f"Worker {self.worker_id}")
                            # Commit failed: restore local snapshot to avoid poisoning next tasks.
                            self._restore_snapshot(repo_dir, snapshot)
                            continue

                        self._log(job_id, f"⚠️ Worker {self.worker_id}: {file_path} still has {len(file_specific_errors)} errors (attempt {attempt})", f"Worker {self.worker_id}")
                        file_info['missing'] = file_specific_errors
                        # Validation failed: roll back staged edits.
                        self._restore_snapshot(repo_dir, snapshot)

                    except Exception:
                        self._restore_snapshot(repo_dir, snapshot)
                        raise
            
            # Failed after all attempts
            self._log(job_id, f"❌ Worker {self.worker_id}: Failed to fix {file_path} after {max_attempts} attempts", f"Worker {self.worker_id}")
            return {'success': False, 'file_path': file_path, 'attempts': max_attempts}
            
        except Exception as e:
            logger.error(f"Worker {self.worker_id} error: {e}")
            return {'success': False, 'file_path': task.get('file_path', 'unknown'), 'error': str(e)}
        finally:
            self.is_busy = False
            self.current_task = None
