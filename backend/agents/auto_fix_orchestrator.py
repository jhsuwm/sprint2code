"""
AUTO-FIX ORCHESTRATOR - Manages parallel auto-fix workers
"""

import asyncio
import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from log_config import logger
from agents.auto_fix_worker import AutoFixWorker
from agents.deployment_fixer import DeploymentFixer


class AutoFixOrchestrator:
    """Main orchestrator that detects errors and assigns work to parallel auto-fix agents"""
    
    def __init__(self, job_manager, ai_service, github_service, jira_service, num_workers: int = 2):
        self.job_manager = job_manager
        self.ai_service = ai_service
        self.github_service = github_service
        self.jira_service = jira_service
        
        # Create worker pool
        self.workers = [
            AutoFixWorker(i+1, job_manager, ai_service, github_service, jira_service)
            for i in range(num_workers)
        ]
        
        # Shared fixer for parsing and validation
        self.fixer = DeploymentFixer(job_manager, ai_service, github_service, jira_service)
        
        # CRITICAL FIX: Use separate queues per worker to avoid routing deadlocks
        self.worker_queues = {worker.worker_id: asyncio.Queue() for worker in self.workers}
        
        # Single-writer gate for shared repo workspace mutations.
        # Workers still run AI generation in parallel, but file writes/validation/commit
        # are serialized to avoid cross-worker corruption of local validation state.
        self._repo_lock = asyncio.Lock()
        
        # Tracking
        self._cycle_metrics = {}
        
        # CRITICAL: Track file assignments to prevent race conditions
        # Maps file_path -> worker_id to ensure same file always goes to same worker
        self._file_assignments = {}
        
        # OPTIMIZATION A: Track programmatic fixes to avoid redundancy
        self._programmatic_fix_history = {}  # job_id -> set of successfully fixed files

        # Track files that were programmatically fixed — exclude them from AI workers
        # to prevent AI from overwriting programmatic fixes
        self._programmatic_fix_lock = {}  # job_id -> set of file paths locked from AI

        # Track repeated worker failures to avoid infinite loops on stubborn files
        self._failed_file_signatures = {}  # job_id -> {file_path: {'signature': int, 'count': int}}
        # Track total cross-cycle attempts per file to bound churn.
        self._file_total_attempts = {}  # job_id -> {file_path: int}
        # Track consecutive orchestrator cycles where a file fails with same signature
        self._consecutive_fail_cycles = {}  # job_id -> {file_path: {'signature': int, 'cycles': int}}
    
    async def orchestrate_auto_fix(self, job_id: str, story_key: str, all_errors: List[str], 
                                   github_repo: str, github_branch: str, repo_dir: str) -> bool:
        """
        Orchestrate parallel auto-fix process:
        1. Detect errors via static analysis
        2. Create fix tasks from errors
        3. Distribute tasks to parallel workers
        4. Monitor progress and re-validate
        
        Returns: True if fixes were applied, False otherwise
        """
        try:
            # Initialize metrics
            if job_id not in self._cycle_metrics:
                self._cycle_metrics[job_id] = {
                    'total_cycles': 0,
                    'total_fixes': 0,
                    'start_time': datetime.now(timezone.utc)
                }
            
            self._cycle_metrics[job_id]['total_cycles'] += 1
            current_cycle = self._cycle_metrics[job_id]['total_cycles']
            
            self.job_manager.log(job_id, f"🎯 Orchestrator: Starting parallel fix cycle {current_cycle}", "Orchestrator")
            
            # Load contexts once (shared across all workers)
            # Load story context first (simpler, no file-specific logic)
            story_context = self.fixer._load_story_context(job_id)
            
            # Parse errors into files to fix
            files_to_fix = self.fixer._parse_all_errors(all_errors, repo_dir)
            
            if not files_to_fix:
                self.job_manager.log(job_id, "No files to fix after parsing", "Orchestrator")
                return False
            
            # OPTIMIZATION A: Initialize programmatic fix history for this job
            if job_id not in self._programmatic_fix_history:
                self._programmatic_fix_history[job_id] = set()
            if job_id not in self._failed_file_signatures:
                self._failed_file_signatures[job_id] = {}
            if job_id not in self._file_total_attempts:
                self._file_total_attempts[job_id] = {}
            
            # PROACTIVE REFRESH: If node_modules is missing from the start, we must refresh it 
            # to allow valid error reporting and prevent workers from hitting guardrails.
            if any("Missing 'node_modules'" in str(e) for e in all_errors):
                self.job_manager.log(job_id, "🚀 Orchestrator: Detected missing node_modules at start of cycle. Triggering refresh...", "Orchestrator")
                await self.fixer._refresh_frontend_dependencies(repo_dir, job_id)
                # Re-validate to clear the error so workers can proceed if possible
                all_errors = self.fixer.validator.validate_all(repo_dir)
                files_to_fix = self.fixer._parse_all_errors(all_errors, repo_dir)
                if not files_to_fix:
                    self.job_manager.log(job_id, "✅ Orchestrator: All errors resolved via initial dependency refresh", "Orchestrator")
                    return True

            # Try programmatic fixes first (fast and deterministic)
            programmatic_fixed = []
            for file_path, file_info in list(files_to_fix.items()):
                if self.fixer._should_try_programmatic(file_path, file_info):
                    # If this file was previously fixed programmatically but still appears in current errors,
                    # retry once because the error context may have changed.
                    if file_path in self._programmatic_fix_history[job_id]:
                        self.job_manager.log(job_id, f"🔁 Orchestrator: Retrying programmatic fix for {file_path} (still failing in latest validation)", "Orchestrator")
                    self.job_manager.log(job_id, f"🔧 Orchestrator: Trying programmatic fix for {file_path}", "Orchestrator")
                    if await self.fixer._apply_programmatic_fix(file_path, file_info, github_repo, github_branch, repo_dir, job_id):
                        self.job_manager.log(job_id, f"✅ Orchestrator: Programmatic fix succeeded for {file_path}", "Orchestrator")
                        programmatic_fixed.append(file_path)
                        # OPTIMIZATION A: Mark as successfully fixed
                        self._programmatic_fix_history[job_id].add(file_path)
                        # Lock this file from AI workers to prevent overwrite
                        if job_id not in self._programmatic_fix_lock:
                            self._programmatic_fix_lock[job_id] = set()
                        self._programmatic_fix_lock[job_id].add(file_path)
                        del files_to_fix[file_path]
                    else:
                        self.job_manager.log(job_id, f"⚠️ Orchestrator: Programmatic fix failed for {file_path} (falling back to AI)", "Orchestrator", level="WARNING")
            
            if programmatic_fixed:
                self.job_manager.log(job_id, f"✅ Orchestrator: {len(programmatic_fixed)} programmatic fixes applied. Re-validating codebase before AI generation...", "Orchestrator")
                
                # NEW: If package.json was fixed, refresh dependencies before re-validating
                if any(p.endswith('package.json') for p in programmatic_fixed):
                    self.job_manager.log(job_id, "📦 Orchestrator: Detected package.json update, refreshing local node_modules...", "Orchestrator")
                    await self.fixer._refresh_frontend_dependencies(repo_dir, job_id)

                # CRITICAL: Re-validate state so AI workers don't work on stale errors
                all_errors = self.fixer.validator.validate_all(repo_dir)
                files_to_fix = self.fixer._parse_all_errors(all_errors, repo_dir)
                
                if not files_to_fix:
                    self.job_manager.log(job_id, "✅ Orchestrator: All errors resolved via programmatic fixes", "Orchestrator")
                    return True

            # Skip files that repeatedly failed with the exact same error signature
            filtered_files_to_fix = {}
            max_file_cycles = int(os.getenv('AUTO_FIX_MAX_FILE_CYCLES', '12'))
            max_consecutive_fail_cycles = int(os.getenv('AUTO_FIX_MAX_CONSECUTIVE_FAIL_CYCLES', '2'))

            # Exclude files that were programmatically fixed — AI workers will overwrite them
            prog_locked = self._programmatic_fix_lock.get(job_id, set())

            for file_path, file_info in files_to_fix.items():
                # Skip files locked by programmatic fixes (AI would overwrite them)
                if file_path in prog_locked:
                    self.job_manager.log(
                        job_id,
                        f"🔒 Orchestrator: Skipping {file_path} (programmatically fixed — excluding from AI to prevent overwrite)",
                        "Skip"
                    )
                    continue

                total_attempts = self._file_total_attempts[job_id].get(file_path, 0)
                if max_file_cycles > 0 and total_attempts >= max_file_cycles:
                    self.job_manager.log(
                        job_id,
                        f"⏭️ Orchestrator: Skipping {file_path} (reached {total_attempts}/{max_file_cycles} cycle attempts)",
                        "Skip"
                    )
                    continue

                signature = hash(tuple(sorted(file_info.get('missing', []))))
                failure_state = self._failed_file_signatures[job_id].get(file_path)

                if (
                    failure_state
                    and failure_state.get('signature') == signature
                    and max_file_cycles > 0
                    and failure_state.get('count', 0) >= max_file_cycles
                ):
                    self.job_manager.log(
                        job_id,
                        f"⏭️ Orchestrator: Skipping {file_path} (failed {failure_state['count']} times with unchanged errors)",
                        "Skip"
                    )
                    continue

                # Early skip: file has failed with same signature across 2+ consecutive orchestrator cycles
                if job_id in self._consecutive_fail_cycles:
                    consec = self._consecutive_fail_cycles[job_id].get(file_path)
                    if consec and consec.get('signature') == signature and consec.get('cycles', 0) >= max_consecutive_fail_cycles:
                        self.job_manager.log(
                            job_id,
                            f"⏭️ Orchestrator: Skipping {file_path} (same error for {consec['cycles']} consecutive cycles — AI cannot resolve this)",
                            "Skip"
                        )
                        continue

                filtered_files_to_fix[file_path] = file_info

            files_to_fix = filtered_files_to_fix
            if not files_to_fix:
                return len(programmatic_fixed) > 0

            # Prioritize files (dependencies first)
            prioritized_files = self._prioritize_files(files_to_fix)

            # Bound per-cycle workload to avoid very long cycles on large oscillating error sets.
            max_files_per_cycle = int(os.getenv('AUTO_FIX_MAX_FILES_PER_CYCLE', '12'))
            total_errors = len(all_errors)
            if total_errors >= 200:
                max_files_per_cycle = max(max_files_per_cycle, 24)
            elif total_errors >= 100:
                max_files_per_cycle = max(max_files_per_cycle, 18)
            if max_files_per_cycle > 0 and len(prioritized_files) > max_files_per_cycle:
                self.job_manager.log(
                    job_id,
                    f"⚖️ Orchestrator: Limiting cycle workload to top {max_files_per_cycle}/{len(prioritized_files)} files",
                    "Orchestrator"
                )
                prioritized_files = prioritized_files[:max_files_per_cycle]
            
            self.job_manager.log(job_id, f"🔄 Orchestrator: Distributing {len(prioritized_files)} files to {len(self.workers)} workers", "Orchestrator")
            
            # CRITICAL: Assign files to workers to prevent race conditions
            # Same file always goes to same worker to see latest changes
            file_to_worker_assignments = {}
            
            # CRITICAL FIX: Use round-robin for new files to guarantee even distribution
            # Hash-based assignment can cluster files on same worker, causing deadlock
            next_worker_idx = len(self._file_assignments) % len(self.workers)
            
            for file_path, file_info in prioritized_files:
                # Check if file was previously assigned to a worker
                if file_path in self._file_assignments:
                    assigned_worker_id = self._file_assignments[file_path]
                else:
                    # First time seeing this file - use round-robin for even distribution
                    assigned_worker_id = next_worker_idx + 1
                    self._file_assignments[file_path] = assigned_worker_id
                    
                    # Increment for next new file
                    next_worker_idx = (next_worker_idx + 1) % len(self.workers)
                
                file_to_worker_assignments[file_path] = assigned_worker_id
                self.job_manager.log(job_id, f"📌 Orchestrator: Assigned {file_path} → Worker {assigned_worker_id}", "File Assignment")
            
            # Create tasks and put them directly into worker-specific queues
            for file_path, file_info in prioritized_files:
                # Determine relevant technical context (skills/config) based on file type
                file_yaml_context = self.fixer._load_yaml_context(job_id, repo_dir, file_path)
                
                assigned_worker_id = file_to_worker_assignments[file_path]
                task = {
                    'job_id': job_id,
                    'file_path': file_path,
                    'file_info': file_info,
                    'github_repo': github_repo,
                    'github_branch': github_branch,
                    'repo_dir': repo_dir,
                    'repo_lock': self._repo_lock,
                    'yaml_context': file_yaml_context,
                    'story_context': story_context
                }
                # CRITICAL FIX: Put task directly into the assigned worker's queue
                await self.worker_queues[assigned_worker_id].put(task)
                self._file_total_attempts[job_id][file_path] = self._file_total_attempts[job_id].get(file_path, 0) + 1
            
            cycle_results = []

            # Start worker processors
            worker_tasks = [
                asyncio.create_task(self._worker_processor(worker, cycle_results))
                for worker in self.workers
            ]
            
            # CRITICAL FIX: Wait for all worker-specific queues to be empty
            await asyncio.gather(*[
                self.worker_queues[worker.worker_id].join()
                for worker in self.workers
            ])
            
            # Cancel worker tasks (they run in infinite loop)
            for task in worker_tasks:
                task.cancel()
            
            # Gather results (ignore cancellation errors)
            try:
                await asyncio.gather(*worker_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            
            # Check if any fixes were actually applied (programmatic or successful worker commits)
            successful_worker_fixes = [
                r for r in cycle_results
                if isinstance(r, dict) and r.get('success') is True
            ]
            fixes_applied = len(programmatic_fixed) > 0 or len(successful_worker_fixes) > 0

            # Update repeated-failure tracking for smarter retries in future cycles
            successful_files = {
                r.get('file_path') for r in successful_worker_fixes
                if isinstance(r, dict) and r.get('file_path')
            }
            failed_worker_results = [
                r for r in cycle_results
                if isinstance(r, dict) and r.get('success') is False
            ]

            for file_path, file_info in prioritized_files:
                signature = hash(tuple(sorted(file_info.get('missing', []))))
                if file_path in successful_files:
                    self._failed_file_signatures[job_id].pop(file_path, None)
                    self._file_total_attempts[job_id][file_path] = 0
                    self._consecutive_fail_cycles.get(job_id, {}).pop(file_path, None)
                    continue

                failed_result = next((r for r in failed_worker_results if r.get('file_path') == file_path), None)
                if failed_result:
                    prev = self._failed_file_signatures[job_id].get(file_path)
                    if prev and prev.get('signature') == signature:
                        prev['count'] = prev.get('count', 0) + 1
                    else:
                        self._failed_file_signatures[job_id][file_path] = {'signature': signature, 'count': 1}

                    # Track consecutive orchestrator cycles with same failure signature
                    if job_id not in self._consecutive_fail_cycles:
                        self._consecutive_fail_cycles[job_id] = {}
                    consec = self._consecutive_fail_cycles[job_id].get(file_path)
                    if consec and consec.get('signature') == signature:
                        consec['cycles'] = consec.get('cycles', 0) + 1
                    else:
                        self._consecutive_fail_cycles[job_id][file_path] = {'signature': signature, 'cycles': 1}
                else:
                    # File not attempted by worker in this cycle (e.g., programmatic only). Keep state unchanged.
                    pass

            if fixes_applied:
                self.job_manager.log(job_id, f"✅ Orchestrator: Cycle {current_cycle} complete - fixes applied", "Orchestrator")
                self._cycle_metrics[job_id]['total_fixes'] += 1
                
                # Pull latest changes for each local repo directory.
                await self._sync_local_repos(repo_dir, github_branch)
            else:
                self.job_manager.log(job_id, f"⚠️ Orchestrator: Cycle {current_cycle} - no fixes applied", "Orchestrator")
            
            return fixes_applied
            
        except Exception as e:
            logger.error(f"Orchestrator error: {e}")
            return False
    
    async def _worker_processor(self, worker: AutoFixWorker, cycle_results: List[Dict[str, Any]]):
        """
        Process tasks from worker-specific queue.
        CRITICAL: Each worker has its own queue - no routing needed!
        """
        worker_queue = self.worker_queues[worker.worker_id]
        
        while True:
            try:
                # Get task from THIS worker's queue (timeout to allow cancellation)
                try:
                    task = await asyncio.wait_for(worker_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Process task (always assigned to this worker)
                result = await worker.process_fix_task(task)
                cycle_results.append(result)
                
                # Mark task as done in this worker's queue
                worker_queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker.worker_id} processor error: {e}")
                # Still mark as done on error to prevent hanging
                worker_queue.task_done()
                await asyncio.sleep(1)

    async def _sync_local_repos(self, repo_dir: str, github_branch: str):
        """Sync any local git repos under repo_dir (single-repo and multi-repo layouts)."""
        import os
        import subprocess

        candidate_dirs = [repo_dir, os.path.join(repo_dir, 'backend'), os.path.join(repo_dir, 'frontend')]
        synced_dirs = set()

        await asyncio.sleep(1)
        for candidate in candidate_dirs:
            if candidate in synced_dirs:
                continue
            if not os.path.isdir(candidate):
                continue
            if not os.path.isdir(os.path.join(candidate, '.git')):
                continue

            try:
                subprocess.run(['git', 'fetch', 'origin', github_branch], cwd=candidate, capture_output=True, timeout=60)
                subprocess.run(['git', 'reset', '--hard', f'origin/{github_branch}'], cwd=candidate, capture_output=True, timeout=30)
                synced_dirs.add(candidate)
            except Exception as e:
                logger.warning(f"Failed to sync local repo at {candidate}: {e}")
    
    def _prioritize_files(self, files_to_fix: Dict[str, Dict]) -> List[tuple]:
        """
        Prioritize files for fixing:
        1. Dependencies (requirements.txt, package.json) - must be fixed first
        2. Types/Models - needed by other files
        3. Implementation files - can be fixed in parallel
        """
        dependency_files = []
        type_files = []
        implementation_files = []
        
        for file_path, file_info in files_to_fix.items():
            if file_path.endswith(('requirements.txt', 'package.json')):
                dependency_files.append((file_path, file_info))
            elif '/types/' in file_path or '/models/' in file_path:
                type_files.append((file_path, file_info))
            else:
                implementation_files.append((file_path, file_info))
        
        # Return in priority order
        return dependency_files + type_files + implementation_files
    
    def get_metrics(self, job_id: str) -> Dict[str, Any]:
        """Get orchestrator metrics for a job"""
        if job_id not in self._cycle_metrics:
            return {}
        
        metrics = self._cycle_metrics[job_id].copy()
        if 'start_time' in metrics:
            elapsed = (datetime.now(timezone.utc) - metrics['start_time']).total_seconds()
            metrics['elapsed_seconds'] = elapsed
        
        return metrics
