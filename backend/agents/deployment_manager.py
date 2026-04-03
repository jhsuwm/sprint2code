import os
import sys
import asyncio
import subprocess
import tempfile
import re
import yaml
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from log_config import logger, error, info
from agents.job_manager import job_store
from agents.deployment_validator import DeploymentValidator
from agents.deployment_fixer import DeploymentFixer
from agents.auto_fix_orchestrator import AutoFixOrchestrator
from agents.skill_registry import SkillRegistry

class DeploymentManager:
    def __init__(self, job_manager, local_app_service, github_service, ai_service, jira_service):
        self.job_manager = job_manager
        self.local_app_service = local_app_service
        self.github_service = github_service
        self.ai_service = ai_service
        self.jira_service = jira_service
        
        self.validator = DeploymentValidator()
        self.fixer = DeploymentFixer(job_manager, ai_service, github_service, jira_service)
        self.skill_registry = SkillRegistry()
        
        # Parallel auto-fix orchestrator with 2 workers
        num_workers = int(os.getenv('AUTO_FIX_WORKERS', '2'))
        self.orchestrator = AutoFixOrchestrator(job_manager, ai_service, github_service, jira_service, num_workers=num_workers)

    async def start_app_locally(self, job_id: str, epic_key: str, story_key: str, project_key: Optional[str] = None):
        """
        Run static analysis, auto-fix errors, then start AI-generated app locally on user's desktop.
        """
        try:
            # Set PATH environment variable at the very start
            path_dirs = [
                '/usr/local/bin',
                '/usr/bin',
                '/bin',
                os.environ.get('PATH', '')
            ]
            os.environ['PATH'] = ':'.join(filter(None, path_dirs))
            
            # Log PATH for debugging
            self.job_manager.log(job_id, f"System PATH: {os.environ['PATH']}", "Environment Setup")
            
            self.job_manager.log(job_id, "Preparing local deployment pipeline (static analysis → auto-fix → start locally)", "Pipeline Start")
            
            # 1. Determine company name from YAML config or fallback
            job_data = job_store.get(job_id, {})
            config_name = job_data.get("config_name")
            skill_names = job_data.get("skill_names", [])
            company_name = self._determine_company_name(job_id, config_name, skill_names, project_key, epic_key)
            
            # 2. GitHub setup
            github_repo = job_data.get("github_repo")
            github_branch = job_data.get("github_branch")
            
            if not github_repo:
                self.job_manager.log(job_id, "❌ Deployment failed: GitHub repository not specified in job store.", "Deployment Failed", level="ERROR")
                return
            if not github_branch:
                self.job_manager.log(job_id, "❌ Deployment failed: GitHub branch not specified in job store.", "Deployment Failed", level="ERROR")
                return
                
            story_id = job_data.get("story_id")
            
            # 3. Clone and validate
            import tempfile
            modified_repos = job_data.get("modified_repos", [])
            all_repos_info = job_data.get("all_repos", [])
            
            # MULTI-REPO SUPPORT: Clone all modified repositories into appropriate subdirectories
            # Create a persistent deployment directory for the lifetime of the job
            deployments_root = os.path.join(os.getcwd(), 'deployments')
            os.makedirs(deployments_root, exist_ok=True)
            temp_dir = os.path.join(deployments_root, job_id)
            os.makedirs(temp_dir, exist_ok=True)

            try:
                # Determine which repos to clone for local start. If the project is split
                # across backend/frontend repos, we need BOTH even if only one was modified.
                repos_to_clone = []
                if all_repos_info:
                    repos_to_clone = [f"{r['owner']}/{r['repo']}" for r in all_repos_info if r.get('owner') and r.get('repo')]
                if not repos_to_clone:
                    repos_to_clone = list(modified_repos) if modified_repos else [github_repo]

                # Multi-repo if we have more than one repo OR explicit frontend/backend types.
                repo_type_map = {
                    f"{r['owner']}/{r['repo']}": (r.get("type") or "unknown")
                    for r in all_repos_info
                    if r.get('owner') and r.get('repo')
                }
                is_multi_repo = len(repos_to_clone) > 1 or any(
                    t for t in repo_type_map.values()
                    if "frontend" in t.lower() or "backend" in t.lower()
                )

                if is_multi_repo:
                    self.job_manager.log(job_id, f"Preparing multi-repo deployment for: {', '.join(repos_to_clone)}", "Deployment")
                    repo_dir = temp_dir  # Build context is the parent directory
                    for repo_str in repos_to_clone:
                        repo_type = repo_type_map.get(repo_str, "unknown")
                        repo_name = repo_str.split('/')[-1].lower()
                        target = (
                            "backend" if "backend" in repo_type.lower() or "backend" in repo_name
                            else ("frontend" if "frontend" in repo_type.lower() or "frontend" in repo_name
                                  else repo_str.split('/')[-1])
                        )
                        self._clone_repo(temp_dir, repo_str, github_branch, target_subdir=target)
                else:
                    # Single repo case (standard fullstack repo structure or primary only)
                    repo_dir = self._clone_repo(temp_dir, github_repo, github_branch)

                # Pre-deployment validation and auto-fix
                all_errors = self.validator.validate_all(repo_dir)
                
                # CRITICAL: Store original errors with correct GitHub line numbers
                original_errors = all_errors.copy() if all_errors else []
                
                if all_errors:
                    await self._perform_static_analysis_autofix(job_id, story_key, all_errors, github_repo, github_branch, repo_dir)
                    # Always re-validate after auto-fix attempts.
                    all_errors = self.validator.validate_all(repo_dir)

                if all_errors:
                    # Report current unresolved errors from latest validation.
                    await self._handle_validation_failure(job_id, story_id, story_key, company_name, epic_key, all_errors, repo_dir)
                    return

                # 4. Start app locally with runtime-recovery loop
                startup_retries = int(os.getenv('STARTUP_RECOVERY_RETRIES', '2'))
                startup_timeout_sec = int(os.getenv('LOCAL_START_TIMEOUT_SEC', '600'))
                startup_failure_message = ""

                for startup_attempt in range(1, startup_retries + 2):
                    self.job_manager.log(
                        job_id,
                        f"Starting app locally (attempt {startup_attempt}/{startup_retries + 1}, timeout: {startup_timeout_sec//60}m)...",
                        "Local Startup"
                    )

                    success, message, urls = await asyncio.wait_for(
                        self.local_app_service.start_app_locally(repo_dir, job_id),
                        timeout=startup_timeout_sec
                    )

                    if success:
                        await self._handle_startup_success(job_id, urls)
                        return

                    startup_failure_message = message
                    self.job_manager.log(
                        job_id,
                        f"⚠️ Startup attempt {startup_attempt} failed: {message[:1200]}",
                        "Startup Failure Details",
                        level="WARNING"
                    )

                    if startup_attempt > startup_retries:
                        break

                    runtime_errors = self._extract_runtime_fixable_errors(
                        message,
                        self.local_app_service.get_startup_diagnostics(),
                        repo_dir
                    )
                    current_static_errors = self.validator.validate_all(repo_dir)
                    merged_errors = current_static_errors + [
                        err for err in runtime_errors if err not in current_static_errors
                    ]

                    if not merged_errors:
                        self.job_manager.log(
                            job_id,
                            "No fixable static/runtime errors were extracted after startup failure; retrying startup once without code changes.",
                            "Startup Recovery",
                            level="WARNING"
                        )
                        continue

                    self.job_manager.log(
                        job_id,
                        f"🔧 Startup recovery: running auto-fix on {len(merged_errors)} extracted error(s)",
                        "Startup Recovery"
                    )
                    await self._perform_static_analysis_autofix(
                        job_id, story_key, merged_errors, github_repo, github_branch, repo_dir
                    )

                await self._handle_startup_failure(job_id, story_id, story_key, startup_failure_message)
                return
            except Exception as e:
                # Ensure we log the exception if it happens inside the try block
                logger.error(f"Error during deployment execution: {e}")
                raise e

        except asyncio.TimeoutError:
            self.job_manager.log(job_id, "❌ Local startup timed out after 10 minutes.", "Startup Failed", level="ERROR")
            job_store[job_id]["app_status"] = "FAILED"
        except Exception as e:
            error(f"Local deployment failed: {e}")
            self.job_manager.log(job_id, f"Local deployment failed: {e}", "Deployment Failed", level="ERROR")
            job_store[job_id]["app_status"] = "FAILED"

    def _determine_company_name(self, job_id, config_name, skill_names, project_key, epic_key):
        company_name = None
        for skill_name in skill_names or []:
            skill = self.skill_registry.load_skill(skill_name)
            if not skill:
                continue
            # Optional frontmatter field in SKILL.md.
            content = skill.get("content", "")
            match = re.search(r"(?im)^company_name:\s*['\"]?([a-zA-Z0-9 _-]+)['\"]?$", content)
            if match:
                company_name = "".join(c for c in match.group(1) if c.isalnum()).lower()
                break

        if config_name:
            content = self._fetch_config_from_firestore(config_name)
            if content:
                try:
                    config_dict = yaml.safe_load(content)
                    company_name = config_dict.get('company_name')
                    if company_name:
                        company_name = ''.join(c for c in company_name if c.isalnum()).lower()
                except Exception: pass
        
        if not company_name:
            if project_key: company_name = project_key.lower()
            elif epic_key and '-' in epic_key: company_name = epic_key.split('-')[0].lower()
            else: company_name = "client"
        
        if company_name in ["lunarxpress", "lun"]: company_name = "lunarxpress"
        return company_name

    def _clone_repo(self, temp_dir, github_repo, github_branch, target_subdir='repo'):
        owner, repo = github_repo.split('/')
        clone_url = f"https://github.com/{owner}/{repo}.git"
        github_token = os.getenv('GITHUB_TOKEN')
        if github_token: clone_url = f"https://{github_token}@github.com/{owner}/{repo}.git"
        
        env = os.environ.copy()
        
        required_tools = {
            'git': "Required for code management",
            'npm': "Required for frontend dependency validation"
        }
        
        for tool, desc in required_tools.items():
            try:
                subprocess.run([tool, '--version'], capture_output=True, check=True, env=env)
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Try fallback to /usr/bin or /usr/local/bin if not in PATH
                tool_path = None
                for path in ['/usr/local/bin', '/usr/bin']:
                    full_path = os.path.join(path, tool)
                    if os.path.exists(full_path):
                        tool_path = full_path
                        break
                
                if tool_path:
                    try:
                        subprocess.run([tool_path, '--version'], capture_output=True, check=True, env=env)
                        continue # Tool found at full path, proceed
                    except Exception: pass

                path_env = env.get('PATH', 'Not set')
                msg = f"❌ Critical tool missing: '{tool}' ({desc}). Current PATH: {path_env}"
                self.job_manager.log(None, msg, "Tool Check", level="ERROR")
                raise Exception(f"Deployment failed: '{tool}' command not found. Please ensure it is installed.")
    
        self.job_manager.log(None, f"Cloning {github_repo} into {target_subdir}...", "Cloning")
        result = subprocess.run(['git', 'clone', '--branch', github_branch, '--depth', '1', clone_url, target_subdir], cwd=temp_dir, capture_output=True, text=True, timeout=300, env=env)
        
        if result.returncode != 0:
            msg = f"❌ Failed to clone repository {github_repo} (branch: {github_branch}): {result.stderr}"
            self.job_manager.log(None, msg, "Cloning Failed", level="ERROR")
            raise Exception(msg)
            
        repo_dir = os.path.join(temp_dir, target_subdir)
        
        # Install frontend deps for analysis
        frontend_dir = None
        if target_subdir == 'frontend' and os.path.exists(os.path.join(repo_dir, "package.json")):
            frontend_dir = repo_dir
        elif os.path.exists(os.path.join(repo_dir, "frontend", "package.json")):
            frontend_dir = os.path.join(repo_dir, "frontend")
        elif os.path.exists(os.path.join(repo_dir, "package.json")):
            frontend_dir = repo_dir

        if frontend_dir and os.path.exists(os.path.join(frontend_dir, "package.json")):
            npm_path = self._find_tool_path('npm')
            if not npm_path:
                self.job_manager.log(None, "⚠️ npm not found, skipping frontend dependency installation", "Tool Warning", level="WARNING")
                return repo_dir
            
            install_cmd = [npm_path, 'install', '--prefer-offline', '--no-audit']
            
            npm_env = os.environ.copy()
            npm_env['PATH'] = '/usr/local/bin:/usr/bin:/bin:' + npm_env.get('PATH', '')
            
            try:
                result = subprocess.run(install_cmd, cwd=frontend_dir, capture_output=True, timeout=120, env=npm_env, text=True)
                if result.returncode != 0:
                    logger.warning(f"npm install failed: {result.stderr[:500]}")
            except Exception as e:
                logger.warning(f"npm install error: {e}")
        return repo_dir

    def _find_tool_path(self, tool: str) -> Optional[str]:
        """Find the full path to a tool, checking common locations."""
        # First try which/command -v
        try:
            result = subprocess.run(['which', tool], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        
        # Check common paths
        for path in ['/usr/local/bin', '/usr/bin', '/bin']:
            full_path = os.path.join(path, tool)
            if os.path.exists(full_path) and os.access(full_path, os.X_OK):
                return full_path
        
        return None

    def _has_dependency_env_errors(self, errors: List[str]) -> bool:
        """Detect environment errors that indicate dependency install is missing/invalid."""
        if not errors:
            return False
        for err in errors:
            if "ENVIRONMENT ERROR: TypeScript validation skipped" in err:
                return True
            if "Missing 'node_modules'" in err or "Incomplete 'node_modules'" in err:
                return True
        return False

    async def _perform_static_analysis_autofix(self, job_id, story_key, all_errors, github_repo, github_branch, repo_dir) -> bool:
        """
        Perform static analysis auto-fix
        Returns: True if all errors are resolved, False otherwise
        """
        # Generic proactive fix for structural issues
        if self.fixer.proactively_fix_dependencies(repo_dir, all_errors):
            self.job_manager.log(job_id, "Proactively fixed structural issues in dependency files.", "Proactive Fix")

        # If dependency environment errors exist, try to refresh before establishing baseline.
        env_refresh_attempts = 0
        while self._has_dependency_env_errors(all_errors) and env_refresh_attempts < 2:
            self.job_manager.log(
                job_id,
                "⚠️ Dependency environment errors detected. Refreshing frontend dependencies before auto-fix baseline...",
                "Environment",
                level="WARNING"
            )
            await self.fixer._refresh_frontend_dependencies(repo_dir, job_id)
            all_errors = self.validator.validate_all(repo_dir)
            env_refresh_attempts += 1
        
        # IMPROVED ADAPTIVE APPROACH: Continue until all errors resolved or truly stuck
        max_attempts = int(os.getenv('AUTO_FIX_MAX_CYCLES', '12'))
        max_runtime_minutes = int(os.getenv('AUTO_FIX_MAX_RUNTIME_MINUTES', '25'))
        progress_grace_minutes = int(os.getenv('AUTO_FIX_PROGRESS_GRACE_MINUTES', '8'))
        progress_extension_minutes = int(os.getenv('AUTO_FIX_PROGRESS_EXTENSION_MINUTES', '10'))
        max_progress_extensions = int(os.getenv('AUTO_FIX_PROGRESS_MAX_EXTENSIONS', '2'))
        progress_extensions_used = 0
        cycle_timeout_sec = int(os.getenv('AUTO_FIX_CYCLE_TIMEOUT_SEC', '300'))
        start_time = datetime.now(timezone.utc)
        last_progress_time = start_time

        baseline_initialized = False
        initial_error_count = len(all_errors)
        lowest_error_count = initial_error_count
        attempt = 0
        consecutive_no_change = 0
        consecutive_same_errors = 0
        last_error_signature = None
        last_significant_progress_cycle = 0
        regression_strikes = 0
        consecutive_regressions = 0
        cycles_without_best_improvement = 0
        
        while attempt < max_attempts:
            attempt += 1
            current_error_count = len(all_errors)
            if not baseline_initialized and not self._has_dependency_env_errors(all_errors):
                initial_error_count = current_error_count
                lowest_error_count = current_error_count
                baseline_initialized = True
                consecutive_no_change = 0
                consecutive_same_errors = 0
                cycles_without_best_improvement = 0
                regression_strikes = 0
                consecutive_regressions = 0
                self.job_manager.log(job_id, f"📌 Auto-fix baseline established at {initial_error_count} errors", "Auto-Fix Baseline")
            
            # Check runtime limit
            elapsed_minutes = (datetime.now(timezone.utc) - start_time).total_seconds() / 60
            if elapsed_minutes >= max_runtime_minutes:
                minutes_since_progress = (datetime.now(timezone.utc) - last_progress_time).total_seconds() / 60
                if progress_extensions_used < max_progress_extensions and minutes_since_progress <= progress_grace_minutes:
                    max_runtime_minutes += progress_extension_minutes
                    progress_extensions_used += 1
                    self.job_manager.log(
                        job_id,
                        f"⏱️ Extending auto-fix runtime by {progress_extension_minutes}m due to recent progress (extension {progress_extensions_used}/{max_progress_extensions})",
                        "Auto-Fix Timeout",
                        level="WARNING"
                    )
                else:
                    self.job_manager.log(job_id, f"⏱️ Stopping: Reached {max_runtime_minutes} minute runtime limit (elapsed: {elapsed_minutes:.1f}m)", "Auto-Fix Timeout", level="WARNING")
                    break
            
            # Create signature of current errors to detect if we're truly stuck
            error_signature = hash(tuple(sorted(all_errors)))
            
            # Check if user wants to stop (via job_store flag)
            if job_store.get(job_id, {}).get("stop_requested"):
                self.job_manager.log(job_id, "🛑 Stop requested by user", "Auto-Fix Stopped")
                break
            
            self.job_manager.log(job_id, f"Auto-fix cycle {attempt}: Found {current_error_count} error(s)", "Auto-Fix Cycle")
            
            # Run auto-fix using parallel orchestrator
            use_parallel = os.getenv('PARALLEL_AUTOFIX', 'true').lower() == 'true'
            timeout_occurred = False
            all_errors_after_cycle = None
            
            try:
                if use_parallel:
                    self.job_manager.log(job_id, f"🚀 Using parallel auto-fix with {len(self.orchestrator.workers)} workers", "Parallel Mode")
                    fixed = await asyncio.wait_for(
                        self.orchestrator.orchestrate_auto_fix(job_id, story_key, all_errors, github_repo, github_branch, repo_dir),
                        timeout=cycle_timeout_sec
                    )
                else:
                    self.job_manager.log(job_id, "Using sequential auto-fix (legacy mode)", "Sequential Mode")
                    fixed = await asyncio.wait_for(
                        self.fixer.auto_fix_static_analysis_errors(job_id, story_key, all_errors, github_repo, github_branch, repo_dir),
                        timeout=cycle_timeout_sec
                    )
            except asyncio.TimeoutError:
                self.job_manager.log(
                    job_id,
                    f"⏱️ Auto-fix cycle {attempt} exceeded {cycle_timeout_sec}s; checking whether partial fixes were applied before deciding next step",
                    "Auto-Fix Timeout",
                    level="WARNING"
                )
                timeout_occurred = True
                fixed = False

            if timeout_occurred:
                all_errors_after_cycle = self.validator.validate_all(repo_dir)
                post_timeout_count = len(all_errors_after_cycle)
                if post_timeout_count != current_error_count:
                    fixed = True
                    self.job_manager.log(
                        job_id,
                        f"⏱️ Timeout recovery: detected error count change ({current_error_count} → {post_timeout_count}); continuing with this cycle's results",
                        "Auto-Fix Timeout Recovery",
                        level="WARNING"
                    )
                else:
                    self.job_manager.log(
                        job_id,
                        f"⏱️ Timeout recovery: no observable error change ({post_timeout_count}); treating cycle as no-op",
                        "Auto-Fix Timeout Recovery",
                        level="WARNING"
                    )
            
            if not fixed:
                self.job_manager.log(job_id, "Auto-fix returned False - no more fixes attempted this cycle", "Auto-Fix Skip")
                consecutive_no_change += 1
                if consecutive_no_change >= 5:
                    self.job_manager.log(job_id, "No fixes possible for 5+ cycles - stopping", "Auto-Fix Stop")
                    break
                continue
            
            # Re-validate
            all_errors = all_errors_after_cycle if all_errors_after_cycle is not None else self.validator.validate_all(repo_dir)
            
            # UPDATE ERROR HISTORY with latest results
            self.fixer.update_error_history(job_id, all_errors, repo_dir)
            
            new_error_count = len(all_errors)
            if self._has_dependency_env_errors(all_errors):
                # Environment not stable yet; don't treat as regression/stagnation.
                self.job_manager.log(
                    job_id,
                    "⚠️ Dependency environment errors still present after cycle; skipping regression/stagnation checks this round.",
                    "Auto-Fix Environment",
                    level="WARNING"
                )
                consecutive_no_change = 0
                consecutive_same_errors = 0
                cycles_without_best_improvement = 0
                consecutive_regressions = 0
                continue
            
            # Early-cycle regressions are common while related files are being converged.
            # Do not hard-stop after cycle 1/2; continue with recovery mode.
            if baseline_initialized and attempt <= 2 and new_error_count > (initial_error_count * 1.5):
                self.job_manager.log(
                    job_id,
                    f"⚠️ Early regression detected ({initial_error_count} → {new_error_count}). Continuing with recovery mode instead of stopping.",
                    "Early Regression",
                    level="WARNING"
                )
                self.fixer.clear_unfixable_files(job_id)
                # Keep iterating; later guards handle persistent regressions.
            
            new_error_signature = hash(tuple(sorted(all_errors)))
            
            # SUCCESS: All errors resolved
            if new_error_count == 0:
                self.job_manager.log(job_id, f"✅ All errors resolved after {attempt} cycle(s)!", "Auto-Fix Success")
                break
            
            # Check if errors are EXACTLY the same (truly stuck)
            if error_signature == new_error_signature:
                consecutive_same_errors += 1
                consecutive_no_change += 1
                self.job_manager.log(job_id, f"⚠️ Exact same errors persist (#{consecutive_same_errors})", "Auto-Fix Stuck", level="WARNING")
                
                # RESURRECTION: Try clearing unfixable list once
                if consecutive_same_errors == 3:
                    self.fixer.clear_unfixable_files(job_id)
                    self.job_manager.log(job_id, f"🔄 Cycle {attempt}: Clearing unfixable list (resurrection attempt)", "Resurrection")
                
                # STOP if truly stuck for 3+ cycles (lowered from 5 for faster bailout)
                if consecutive_same_errors >= 3:
                    # Check if we have a good enough result to accept
                    best_success_rate = ((initial_error_count - lowest_error_count) / initial_error_count * 100) if initial_error_count > 0 else 0
                    if best_success_rate >= 50 and lowest_error_count < new_error_count:
                        self.job_manager.log(
                            job_id,
                            f"⚠️ Best result so far is {lowest_error_count} errors ({best_success_rate:.1f}% improvement), but unresolved errors remain. Continuing instead of exiting early.",
                            "Best Result (Continue)",
                            level="WARNING"
                        )
                    
                    # Continue instead of hard-stopping; clear unfixable state and try a fresh cycle.
                    self.job_manager.log(job_id, f"⚠️ Identical errors for {consecutive_same_errors} cycles - forcing fresh retry instead of stopping", "Auto-Fix Exhausted", level="WARNING")
                    self.fixer.clear_unfixable_files(job_id)
                    consecutive_same_errors = 0
                    continue
            else:
                # Errors changed - we're making SOME progress
                consecutive_same_errors = 0
                consecutive_no_change = 0
                self.job_manager.log(job_id, "Errors changed - attempting to fix new/different issues", "Auto-Fix Progress")
            
            # Track if we achieved a new best (lowest error count)
            # CRITICAL: Only update 'best' state if the validation result is CLEAN of environmental failures.
            # Environmental failures (like missing node_modules) artificially lower the error count.
            has_env_error = any("ENVIRONMENT ERROR" in err for err in all_errors)
            
            if new_error_count < lowest_error_count and not has_env_error:
                lowest_error_count = new_error_count
                last_significant_progress_cycle = attempt
                last_progress_time = datetime.now(timezone.utc)
                cycles_without_best_improvement = 0
                reduction = current_error_count - new_error_count
                consecutive_regressions = 0
                self.job_manager.log(job_id, f"📉 Progress: {reduction} error(s) fixed ({new_error_count} remaining, best: {lowest_error_count})", "Auto-Fix Improving")
                
                if lowest_error_count <= 3:
                    self.job_manager.log(job_id, f"🎯 Excellent result achieved ({lowest_error_count} errors). Being conservative - will stop if any regression occurs.", "Near Success")
                
                # RESURRECTION: When we make progress, clear unfixable list every 4 cycles
                if attempt > 0 and attempt % 4 == 0:
                    self.fixer.clear_unfixable_files(job_id)
                    self.job_manager.log(job_id, f"🔄 Cycle {attempt}: Cleared unfixable list - giving all files fresh chance after progress", "Resurrect All")
                
                continue
            else:
                cycles_without_best_improvement += 1
                if baseline_initialized and cycles_without_best_improvement >= 4 and attempt >= 4:
                    self.job_manager.log(
                        job_id,
                        f"⚠️ No new best error count for {cycles_without_best_improvement} cycles (best: {lowest_error_count}). Stopping to avoid runaway auto-fix.",
                        "Auto-Fix Stagnation",
                        level="WARNING"
                    )
                    break
            
            # CRITICAL: Error count increased - track and potentially stop
            if new_error_count > current_error_count:
                increase = new_error_count - current_error_count
                increase_pct = (increase / current_error_count * 100) if current_error_count > 0 else 0
                consecutive_regressions += 1
                self.job_manager.log(job_id, f"⚠️ Error count increased by {increase} ({current_error_count} → {new_error_count}, +{increase_pct:.1f}%)", "Auto-Fix Regression", level="WARNING")
                
                # CRITICAL: Stop immediately if 3 consecutive regressions
                if consecutive_regressions >= 3:
                    best_success_rate = ((initial_error_count - lowest_error_count) / initial_error_count * 100) if initial_error_count > 0 else 0
                    if best_success_rate >= 50:
                        self.job_manager.log(
                            job_id,
                            f"⚠️ 3 consecutive regressions detected. Best interim state is {lowest_error_count} errors ({best_success_rate:.1f}% improvement); continuing recovery mode.",
                            "Best Result (Continue)",
                            level="WARNING"
                        )
                        self.fixer.clear_unfixable_files(job_id)
                        consecutive_regressions = 0
                        continue
                    else:
                        self.job_manager.log(job_id, f"⚠️ 3 consecutive regressions ({initial_error_count}→{lowest_error_count}→{new_error_count}); continuing with recovery mode instead of stopping.", "Multiple Consecutive Regressions", level="WARNING")
                        self.fixer.clear_unfixable_files(job_id)
                        consecutive_regressions = 0
                        continue
                
                # Adaptive regression detection
                is_major_regression = False
                if current_error_count < 10:
                    is_major_regression = increase > 30 or new_error_count > (current_error_count * 2.5)
                else:
                    is_major_regression = increase_pct > 20 or new_error_count > (current_error_count * 1.8)
                
                if is_major_regression:
                    best_success_rate = ((initial_error_count - lowest_error_count) / initial_error_count * 100) if initial_error_count > 0 else 0
                    
                    if best_success_rate >= 50:
                        self.job_manager.log(
                            job_id,
                            f"⚠️ Oscillation detected ({new_error_count} errors) after reaching best {lowest_error_count}. Continuing recovery mode; not exiting early.",
                            "Best Result (Continue)",
                            level="WARNING"
                        )
                        self.fixer.clear_unfixable_files(job_id)
                        continue
                    
                    if lowest_error_count <= 10:
                        self.job_manager.log(job_id, f"⚠️ Major regression after achieving {lowest_error_count} errors. Continuing in recovery mode.", "Stop - Best Result Achieved", level="WARNING")
                        self.fixer.clear_unfixable_files(job_id)
                        continue
                    elif lowest_error_count <= 20:
                        if consecutive_same_errors < 1:
                            self.job_manager.log(job_id, f"⚠️ MAJOR regression: +{increase} errors (+{increase_pct:.1f}%). ONE recovery attempt allowed.", "Regression Warning", level="WARNING")
                        else:
                            self.job_manager.log(job_id, f"⚠️ MAJOR regression: +{increase} errors (+{increase_pct:.1f}). Continuing in recovery mode.", "Fatal Regression", level="WARNING")
                            self.fixer.clear_unfixable_files(job_id)
                            continue
                    else:
                        if consecutive_same_errors < 1:
                            self.job_manager.log(job_id, f"⚠️ MAJOR regression: +{increase} errors (+{increase_pct:.1f}%). Attempting recovery cycle before stopping.", "Regression Warning", level="WARNING")
                            regression_strikes += 1
                            if regression_strikes >= 2:
                                # Check if best result is good enough before stopping
                                best_success_rate = ((initial_error_count - lowest_error_count) / initial_error_count * 100) if initial_error_count > 0 else 0
                                if best_success_rate >= 50:
                                    self.job_manager.log(
                                        job_id,
                                        f"⚠️ 2 major regressions with best interim result {lowest_error_count} ({best_success_rate:.1f}% improvement). Continuing recovery mode.",
                                        "Best Result (Continue)",
                                        level="WARNING"
                                    )
                                    self.fixer.clear_unfixable_files(job_id)
                                    regression_strikes = 0
                                    continue
                                else:
                                    self.job_manager.log(job_id, f"⚠️ 2 major regressions occurred; continuing with recovery mode (best was {best_success_rate:.1f}%).", "Multiple Regressions", level="WARNING")
                                    self.fixer.clear_unfixable_files(job_id)
                                    regression_strikes = 0
                                    continue
                        else:
                            self.job_manager.log(job_id, f"⚠️ MAJOR regression: +{increase} errors (+{increase_pct:.1f}%). Continuing with recovery mode.", "Fatal Regression", level="WARNING")
                            self.fixer.clear_unfixable_files(job_id)
                            continue
                
                if consecutive_same_errors == 0:
                    self.job_manager.log(job_id, "Continuing to fix new/different errors in next cycle", "Auto-Fix Continue")
                    continue
                else:
                    self.job_manager.log(job_id, "⚠️ Errors increased with some repetition; clearing state and continuing.", "Fatal Regression", level="WARNING")
                    self.fixer.clear_unfixable_files(job_id)
                    continue
            
            # NO CHANGE in count but errors are different - keep trying
            if new_error_count == current_error_count and consecutive_same_errors == 0:
                self.job_manager.log(job_id, f"Error count unchanged ({new_error_count}) but errors are different - continuing", "Auto-Fix Working")
                continue
            
            last_error_signature = new_error_signature
        
        # Final summary
        final_count = len(all_errors)
        if not baseline_initialized:
            initial_error_count = max(initial_error_count, final_count)
            lowest_error_count = min(lowest_error_count, final_count)
        
        # If final result is worse than best achieved, report it; do not pretend success.
        if final_count > lowest_error_count:
            best_success_rate = ((initial_error_count - lowest_error_count) / initial_error_count * 100) if initial_error_count > 0 else 0
            self.job_manager.log(
                job_id,
                f"⚠️ Final state ({final_count} errors) is worse than best achieved ({lowest_error_count} errors, {best_success_rate:.1f}% improvement).",
                "Regression Warning",
                level="WARNING"
            )
        
        if final_count == 0:
            self.job_manager.log(job_id, f"🎉 SUCCESS: All errors resolved in {attempt} cycles", "Auto-Fix Complete")
            return True
        elif final_count < initial_error_count:
            improvement = initial_error_count - final_count
            self.job_manager.log(job_id, f"Partial success: Fixed {improvement} errors ({initial_error_count} → {final_count})", "Auto-Fix Partial")
            if final_count > lowest_error_count:
                self.job_manager.log(job_id, f"Best result was {lowest_error_count} errors at cycle {last_significant_progress_cycle}", "Best Result")
            return False
        else:
            self.job_manager.log(job_id, f"Unable to resolve all errors: {final_count} remaining after {attempt} cycles", "Auto-Fix Incomplete", level="WARNING")
            return False
        
        if attempt >= max_attempts:
            self.job_manager.log(job_id, f"⚠️ Reached maximum {max_attempts} cycles", "Auto-Fix Limit", level="WARNING")
        
        return False

    async def _handle_validation_failure(self, job_id, story_id, story_key, company_name, epic_key, errors, repo_dir):
        # Parse and group errors by file for clear actionable feedback
        total_errors = len(errors)
        
        # Helper function to read file lines safely
        def get_file_line(file_path, line_num):
            """Get a specific line from a file, return None if not found"""
            try:
                # Try to find the file in repo_dir
                full_path = None
                if os.path.exists(os.path.join(repo_dir, file_path)):
                    full_path = os.path.join(repo_dir, file_path)
                elif os.path.exists(os.path.join(repo_dir, 'backend', file_path)):
                    full_path = os.path.join(repo_dir, 'backend', file_path)
                elif os.path.exists(os.path.join(repo_dir, 'frontend', file_path)):
                    full_path = os.path.join(repo_dir, 'frontend', file_path)
                
                if full_path and os.path.exists(full_path):
                    with open(full_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                        if 0 < line_num <= len(lines):
                            return lines[line_num - 1].strip()
            except:
                pass
            return None
        
        # Group errors by file
        file_errors = {}
        for error in errors:
            # Extract file path from error message
            file_path = None
            
            # Python ImportError: "ImportError in 'path/to/file.py'"
            import_match = re.search(r"ImportError in '([^']+)'", error)
            if import_match:
                module_path = import_match.group(1)
                # Keep explicit source file paths as-is (TS/JS/Python paths with slash separators).
                if "/" in module_path or module_path.endswith(('.ts', '.tsx', '.js', '.jsx')):
                    file_path = module_path
                elif module_path.endswith('.py'):
                    # Dotted Python module path (e.g. auth.utils.py) -> auth/utils.py
                    module_path = module_path[:-3]
                    file_path = f"{module_path.replace('.', '/')}.py"
                else:
                    # Fallback for bare Python module names (e.g. "main" -> main.py)
                    file_path = f"{module_path.replace('.', '/')}.py"
            
            # TypeScript error: "TypeScript error in 'path/to/file.tsx'"
            ts_match = re.search(r"TypeScript error in '([^']+)'", error)
            if ts_match:
                # Don't add "frontend/" prefix - repos are separate
                file_path = ts_match.group(1)
            
            # Missing dependency: "Missing dependency: 'package' in requirements.txt"
            if "requirements.txt" in error:
                file_path = "requirements.txt"  # No prefix - separate repo
            elif "package.json" in error:
                file_path = "package.json"  # No prefix - separate repo
            
            # Add to grouped errors
            if file_path:
                if file_path not in file_errors:
                    file_errors[file_path] = []
                # Extract just the error description (remove file prefix if present)
                error_desc = error
                if import_match:
                    error_desc = error.replace(f"ImportError in '{import_match.group(1)}': ", "")
                elif ts_match:
                    error_desc = error.replace(f"TypeScript error in '{ts_match.group(1)}' ", "")
                file_errors[file_path].append(error_desc)
            else:
                # Ungrouped errors
                if "ungrouped" not in file_errors:
                    file_errors["ungrouped"] = []
                file_errors["ungrouped"].append(error)
        
        # Build detailed message for storage (used by UI components)
        summary_parts = [f"❌ Static analysis found {total_errors} error(s) that couldn't be auto-fixed:"]
        summary_parts.append("")
        summary_parts.append("📁 Files with errors:")
        
        # Get GitHub repo information from job store for display
        job_data = job_store.get(job_id, {})
        github_repo = job_data.get("github_repo", "")
        modified_repos = job_data.get("modified_repos", [])
        all_repos_info = job_data.get("all_repos", [])
        
        for file_path, file_error_list in sorted(file_errors.items()):
            if file_path == "ungrouped":
                continue
            
            # Determine which repo this file belongs to
            repo_indicator = ""
            if len(modified_repos) > 1:
                # Multi-repo: Determine by file extension and path patterns
                if file_path.endswith(('.py', '.txt')) or file_path.startswith(('models/', 'routes/', 'services/', 'auth/', 'database/')):
                    # Backend file
                    backend_repo = next((r for r in all_repos_info if "backend" in r.get("type", "").lower()), None)
                    if backend_repo:
                        repo_indicator = f" [Backend Repo: {backend_repo['owner']}/{backend_repo['repo']}]"
                    else:
                        repo_indicator = " [Backend Repo]"
                elif file_path.endswith(('.tsx', '.ts', '.jsx', '.js', '.json')) or file_path.startswith(('src/', 'app/', 'components/', 'pages/')):
                    # Frontend file
                    frontend_repo = next((r for r in all_repos_info if "frontend" in r.get("type", "").lower()), None)
                    if frontend_repo:
                        repo_indicator = f" [Frontend Repo: {frontend_repo['owner']}/{frontend_repo['repo']}]"
                    else:
                        repo_indicator = " [Frontend Repo]"
            elif github_repo:
                # Single repo - show which one
                repo_indicator = f" [Repo: {github_repo}]"
            
            error_count = len(file_error_list)
            summary_parts.append(f"  • {file_path}{repo_indicator} ({error_count} error{'s' if error_count > 1 else ''})")
            
            for err in file_error_list[:3]:
                # Parse line numbers from TypeScript errors like "at 2,9: 1005: ';' expected."
                line_match = re.search(r'at (\d+),\d+:', err)
                if line_match:
                    line_num = int(line_match.group(1))
                    # Extract the actual error message
                    error_msg = err.split(':', 2)[-1].strip() if ':' in err else err
                    
                    # Get the actual code line to help users find it
                    code_line = get_file_line(file_path, line_num)
                    if code_line:
                        # Truncate if too long
                        code_display = code_line[:80] + "..." if len(code_line) > 80 else code_line
                        summary_parts.append(f"    - Line {line_num}: {error_msg}")
                        summary_parts.append(f"      Code: {code_display}")
                    else:
                        # Fallback if we can't read the file
                        summary_parts.append(f"    - Line {line_num}: {error_msg}")
                else:
                    err_display = err[:150] + "..." if len(err) > 150 else err
                    summary_parts.append(f"    - {err_display}")
            
            if len(file_error_list) > 3:
                summary_parts.append(f"    - ...and {len(file_error_list) - 3} more error(s)")
            summary_parts.append("")
        
        if "ungrouped" in file_errors and file_errors["ungrouped"]:
            summary_parts.append("  • Other errors:")
            for err in file_errors["ungrouped"][:3]:
                err_display = err[:150] + "..." if len(err) > 150 else err
                summary_parts.append(f"    - {err_display}")
            if len(file_errors["ungrouped"]) > 3:
                summary_parts.append(f"    - ...and {len(file_errors['ungrouped']) - 3} more")
            summary_parts.append("")
        
        summary_parts.append("📋 Next steps:")
        summary_parts.append("  1. Review the errors above and identify which files need manual fixes")
        summary_parts.append("  2. Fix the errors in your GitHub repository")
        summary_parts.append("  3. Click the 'Rerun Deployment' button to pull latest code and retry")
        summary_parts.append("")
        summary_parts.append("💡 Tip: The auto-fix attempted multiple cycles but couldn't resolve all errors - manual fixes are needed")
        
        # Add a prominent header to draw attention
        self.job_manager.log(job_id, "", "Validation Failed")  # Blank line
        self.job_manager.log(job_id, "="*80, "CRITICAL ERRORS", level="ERROR")
        self.job_manager.log(job_id, "🚨 UNRESOLVED CODE ERRORS - MANUAL FIXES REQUIRED 🚨", "CRITICAL ERRORS", level="ERROR")
        self.job_manager.log(job_id, "="*80, "CRITICAL ERRORS", level="ERROR")
        self.job_manager.log(job_id, "", "CRITICAL ERRORS", level="ERROR")
        
        # Log each line separately for log viewer - USE ERROR LEVEL for visibility
        for line in summary_parts:
            if line.startswith("❌"):
                self.job_manager.log(job_id, line, "Validation Failed", level="ERROR")
            elif line.startswith("📁") or line.startswith("  •"):
                self.job_manager.log(job_id, line, "Error Details", level="ERROR")
            elif line.startswith("    -"):
                # Individual error descriptions - make them ERROR too
                self.job_manager.log(job_id, line, "Error Details", level="ERROR")
            elif line.startswith("📋"):
                self.job_manager.log(job_id, line, "Next Steps", level="ERROR")
            elif line.startswith("  1") or line.startswith("  2") or line.startswith("  3"):
                self.job_manager.log(job_id, line, "Next Steps", level="ERROR")
            elif line.startswith("💡"):
                self.job_manager.log(job_id, line, "Next Steps", level="ERROR")
            elif line == "":
                # Blank lines
                self.job_manager.log(job_id, line, "Error Details", level="ERROR")
            else:
                self.job_manager.log(job_id, line, "Error Details", level="ERROR")
        
        # Store as structured list for UI to render properly
        # UI will receive this as JSON array and can display each line separately
        validation_errors_list = summary_parts  # Already a list
        
        # Also store as formatted string for backwards compatibility
        user_friendly_msg = "\n".join(summary_parts)
        
        # Store detailed errors separately (not shown in UI logs, but available in job_store)
        detailed_errors = "\n".join(errors)
        job_store[job_id].update({
            "app_status": "VALIDATION_FAILED",
            "validation_error_summary": user_friendly_msg,  # String with newlines
            "validation_errors_list": validation_errors_list,  # Array for UI rendering
            "validation_error_details": detailed_errors[:10000]  # Store full details separately
        })

    async def _handle_startup_success(self, job_id: str, urls: Dict[str, str]):
        self.job_manager.log(job_id, "✅ App started successfully on local machine", "Startup Complete")
        backend_url = urls.get('backend_url')
        frontend_url = urls.get('frontend_url')

        # EXPLICIT LOGGING FOR USER VISIBILITY (support backend-only/frontend-only repos)
        if frontend_url:
            self.job_manager.log(job_id, f"🚀 AI-generated frontend is running at: {frontend_url}", "App URL")
        if backend_url:
            self.job_manager.log(job_id, f"📡 Backend API is running at: {backend_url}", "API URL")

        update_payload = {"app_status": "RUNNING_LOCALLY"}
        if backend_url:
            update_payload["backend_url"] = backend_url
        if frontend_url:
            update_payload["frontend_url"] = frontend_url
        job_store[job_id].update(update_payload)

        # FETCH INITIAL LOGS FOR UI CONTAINERS
        # This ensures the "Frontend Log" and "Backend Log" boxes in the UI are not empty on start
        try:
            backend_logs = await self.local_app_service.get_app_logs('backend', limit=5000)
            frontend_logs = await self.local_app_service.get_app_logs('frontend', limit=5000)
            
            if backend_logs: 
                job_store[job_id]["backend_logs"] = backend_logs
                self.job_manager.log(job_id, f"Captured {len(backend_logs)} lines of backend startup logs", "Logs")
            if frontend_logs: 
                job_store[job_id]["frontend_logs"] = frontend_logs
                self.job_manager.log(job_id, f"Captured {len(frontend_logs)} lines of frontend startup logs", "Logs")
        except Exception as e:
            logger.error(f"Failed to fetch initial logs: {e}")
        
        # Check health
        backend_healthy, frontend_healthy = await self.local_app_service.check_health(job_id)

        # Determine expected services based on started URLs.
        expects_backend = bool(backend_url)
        expects_frontend = bool(frontend_url)

        if expects_backend and expects_frontend:
            if backend_healthy and frontend_healthy:
                self.job_manager.log(job_id, "✅ Both services are running and healthy", "Health Check Success")
                job_store[job_id]["app_status"] = "HEALTHY"
            else:
                self.job_manager.log(job_id, "⚠️ One or more services may have issues", "Health Check Warning", level="WARNING")
        elif expects_backend:
            if backend_healthy:
                self.job_manager.log(job_id, "✅ Backend service is running and healthy", "Health Check Success")
                job_store[job_id]["app_status"] = "HEALTHY"
            else:
                self.job_manager.log(job_id, "⚠️ Backend service may have issues", "Health Check Warning", level="WARNING")
        elif expects_frontend:
            if frontend_healthy:
                self.job_manager.log(job_id, "✅ Frontend service is running and healthy", "Health Check Success")
                job_store[job_id]["app_status"] = "HEALTHY"
            else:
                self.job_manager.log(job_id, "⚠️ Frontend service may have issues", "Health Check Warning", level="WARNING")
        else:
            # Should not happen, but keep behavior deterministic.
            self.job_manager.log(job_id, "⚠️ Startup reported success but no service URLs were returned", "Health Check Warning", level="WARNING")

    async def _handle_startup_failure(self, job_id, story_id, story_key, error_output):
        """Handle local startup failure."""
        self.job_manager.log(job_id, "❌ Local startup failed", "Startup Failed", level="ERROR")
        if error_output:
            self.job_manager.log(job_id, f"Startup error details: {error_output[:2000]}", "Startup Failed", level="ERROR")
        
        # Capture error output
        job_store[job_id].update({"app_status": "STARTUP_FAILED", "startup_error": error_output[:5000]})
        
        # Get logs from processes
        backend_logs = await self.local_app_service.get_app_logs('backend', limit=5000)
        frontend_logs = await self.local_app_service.get_app_logs('frontend', limit=5000)
        
        if backend_logs: job_store[job_id]["backend_logs"] = backend_logs
        if frontend_logs: job_store[job_id]["frontend_logs"] = frontend_logs

    def _extract_runtime_fixable_errors(self, startup_message: str, startup_diag: Dict[str, Any], repo_dir: str) -> List[str]:
        """
        Convert runtime startup failures into fixer-compatible synthetic errors.
        """
        errors: List[str] = []
        combined = "\n".join(
            [
                startup_message or "",
                str(startup_diag.get('backend_error', '')) if startup_diag else "",
                str(startup_diag.get('frontend_error', '')) if startup_diag else "",
                str(startup_diag.get('exception', '')) if startup_diag else ""
            ]
        )

        # Python runtime import failures
        missing_mod = re.search(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", combined)
        if missing_mod:
            missing_name = missing_mod.group(1)
            errors.append(f"Missing dependency: '{missing_name}' in requirements.txt")

        # FastAPI file upload runtime guard:
        # RuntimeError: Form data requires "python-multipart" to be installed.
        # This often appears without ModuleNotFoundError, so map it explicitly.
        if re.search(r'Form data requires ["\']python-multipart["\'] to be installed', combined, re.IGNORECASE):
            errors.append("Missing dependency: 'python-multipart' in requirements.txt")

        cannot_import = re.search(
            r"ImportError:\s+cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
            combined
        )
        if cannot_import:
            symbol = cannot_import.group(1)
            module = cannot_import.group(2)
            importer_rel = "backend/main.py"
            tb_file = re.search(r'File "([^"]+/backend/[^"]+)"', combined)
            if tb_file:
                full = tb_file.group(1)
                marker = "/backend/"
                idx = full.rfind(marker)
                if idx != -1:
                    importer_rel = "backend/" + full[idx + len(marker):]
            errors.append(f"ImportError in '{importer_rel}': '{symbol}' not found in '{module}'")

        # pip install failures: "ERROR: Invalid requirement: '<bad_line>'"
        invalid_req = re.search(
            r"ERROR:\s*Invalid requirement:\s*'([^']+)'",
            combined
        )
        if not invalid_req:
            # Fallback for pip exceptions that occur during requirement parsing
            # (e.g. "parsed = _parse_requirement(requirement_string)")
            # Try to find the line that followed the traceback if possible,
            # or look for common AI-generated markers in the vicinity of "requirement".
            if "Traceback" in combined and "packaging/requirements.py" in combined:
                # If we're here, pip crashed while parsing a requirement.
                # Inspect combined for obvious bad lines.
                for line in combined.splitlines():
                    s = line.strip()
                    if s.startswith('```') or s.startswith('FILE_PATH:') or re.match(r'^-{3,}$', s):
                        invalid_req = re.match(r'(.*)', s) # Synthetic match
                        break
        
        if invalid_req:
            bad_line = invalid_req.group(1)
            errors.append(
                f"Invalid pip requirement in 'requirements.txt': "
                f"'{bad_line}' is not a valid pip package spec - "
                f"remove this line (it is a code-block marker, not a package)"
            )

        # pip version not found failures: "Could not find a version that satisfies the requirement X==Y (from versions: ...)"
        version_mismatch = re.search(
            r"Could not find a version that satisfies the requirement\s+([^\s]+)\s+\(from versions:\s*([^)]+)\)",
            combined
        )
        if version_mismatch:
            bad_req = version_mismatch.group(1).strip()
            available = version_mismatch.group(2).strip()
            errors.append(
                "Invalid pip version in 'requirements.txt': "
                f"'{bad_req}' not available. Available versions: {available}"
            )

        # Some pip outputs include a follow-up: "No matching distribution found for X==Y"
        no_match = re.search(
            r"No matching distribution found for\s+([^\s]+)",
            combined
        )
        if no_match:
            bad_req = no_match.group(1).strip()
            # Avoid duplicate entries if we already captured version list above.
            if not any(bad_req in e for e in errors):
                errors.append(
                    "Invalid pip version in 'requirements.txt': "
                    f"'{bad_req}' not available."
                )

        # Frontend runtime build errors can still be fixed by TS pipeline.
        missing_resolve = re.search(r"Module not found:.*Can't resolve ['\"]([^'\"]+)['\"]", combined, re.IGNORECASE)
        if missing_resolve:
            module_name = missing_resolve.group(1)
            if (
                module_name.startswith(('@/','./','../','~/', '#/', '/'))
                or module_name in ('~', '#', '@')
                or module_name == '@types'
                or module_name.startswith('@types/')
                or module_name.startswith('@api/')
            ):
                errors.append(f"TypeScript error in 'src/app/page.tsx' at 1,1: 2307: Cannot find module '{module_name}'")
            else:
                errors.append(f"Missing dependency: '{module_name}' in package.json")

        # Backend pip install failures during startup should trigger requirements auto-fix.
        if "Failed to install dependencies" in combined or "subprocess-exited-with-error" in combined:
            errors.append("Runtime pip install failure in requirements.txt")
            # Common known trap: deprecated azure meta-package.
            if re.search(r"\bazure\b", combined, re.IGNORECASE):
                errors.append("Invalid pip requirement in 'requirements.txt': 'azure'")
                errors.append("Missing dependency: 'azure-storage-blob' in requirements.txt")

        # Runtime undefined symbol errors (NameError) from backend startup traceback.
        name_error = re.search(r"NameError:\s*name ['\"]([^'\"]+)['\"] is not defined", combined)
        if name_error:
            missing_name = name_error.group(1)
            frame_matches = re.findall(r'File "([^"]+)", line \d+, in [^\n]+', combined)
            target_file = "main.py"
            for frame in reversed(frame_matches):
                normalized = frame.replace("\\", "/")
                if "/backend/" in normalized:
                    target_file = normalized.split("/backend/", 1)[1]
                    break
            errors.append(
                f"RuntimeNameError in '{target_file}': name '{missing_name}' is not defined"
            )

        # Pydantic Settings validation errors (missing env vars) during startup.
        # Example:
        # pydantic_core._pydantic_core.ValidationError: 2 validation errors for Settings
        # FIRESTORE_PROJECT_ID
        #   Field required [type=missing, ...]
        if re.search(r"validation errors for Settings", combined, re.IGNORECASE) or re.search(r"ValidationError", combined):
            missing_fields = re.findall(
                r"^([A-Z_][A-Z0-9_]*)\s*\n\s*Field required",
                combined,
                re.MULTILINE
            )
            if missing_fields:
                # Best-effort locate the settings file from traceback.
                settings_file = "config.py"
                tb_file = re.search(r'File "([^"]+/(?:config|settings)\.py)"', combined)
                if tb_file:
                    settings_file = tb_file.group(1).replace("\\", "/")
                    if "/backend/" in settings_file:
                        settings_file = settings_file.rsplit("/backend/", 1)[1]
                fields_csv = ", ".join(sorted(set(missing_fields)))
                errors.append(
                    f"PydanticSettingsError in '{settings_file}': Missing required settings: {fields_csv}"
                )

        return errors

    def _fetch_config_from_firestore(self, name: str) -> Optional[str]:
        # Firestore removed - no longer needed for local OSS usage
        return None
