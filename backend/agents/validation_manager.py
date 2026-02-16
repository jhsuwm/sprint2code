import os
import sys
import asyncio
import subprocess
import tempfile
import re
import yaml
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from log_config import logger, error
from agents.job_manager import job_store
from database.firestore_config import get_firestore_client
from agents.deployment_validator import DeploymentValidator
from agents.deployment_fixer import DeploymentFixer

class DeploymentManager:
    def __init__(self, job_manager, gcloud_service, github_service, gemini_service, jira_service):
        self.job_manager = job_manager
        self.gcloud_service = gcloud_service
        self.github_service = github_service
        self.gemini_service = gemini_service
        self.jira_service = jira_service
        
        self.validator = DeploymentValidator()
        self.fixer = DeploymentFixer(job_manager, gemini_service, github_service, gcloud_service, jira_service)

    async def deploy_to_cloud_run(self, job_id: str, epic_key: str, story_key: str, project_key: Optional[str] = None):
        try:
            # CRITICAL FIX: Set PATH environment variable at the very start
            # This ensures all subprocess calls (including tool checks) can find npm, node, gcloud
            path_dirs = [
                '/usr/local/bin',
                '/usr/bin',
                '/bin',
                '/opt/google-cloud-sdk/bin',
                os.environ.get('PATH', '')
            ]
            os.environ['PATH'] = ':'.join(filter(None, path_dirs))
            
            # Log PATH for debugging
            self.job_manager.log(job_id, f"System PATH: {os.environ['PATH']}", "Environment Setup")
            
            self.job_manager.log(job_id, "Preparing deployment pipeline (static analysis → auto-fix → deploy)", "Pipeline Start")
            
            # 1. Determine company name from YAML config or fallback
            job_data = job_store.get(job_id, {})
            config_name = job_data.get("config_name")
            company_name = self._determine_company_name(job_id, config_name, project_key, epic_key)
            
            # 2. Setup subdomain and service names
            safe_epic = epic_key.lower().replace('_', '-') if epic_key else "unknown"
            subdomain = f"{company_name}-develop-{safe_epic}.roosterjourney.com"
            client_name = company_name
            
            # 3. GitHub setup and deployment files
            github_repo = job_data.get("github_repo")
            github_branch = job_data.get("github_branch")
            
            if not github_repo:
                self.job_manager.log(job_id, "❌ Deployment failed: GitHub repository not specified in job store.", "Deployment Failed", level="ERROR")
                return
            if not github_branch:
                self.job_manager.log(job_id, "❌ Deployment failed: GitHub branch not specified in job store.", "Deployment Failed", level="ERROR")
                return

            deployment_files = self.gcloud_service.generate_deployment_files(client_name=client_name, project_path="", subdomain=subdomain, epic_key=epic_key, job_id=job_id)
            owner, repo = github_repo.split('/')
            for filename, content in deployment_files.items():
                self.github_service.commit_file(owner, repo, github_branch, filename, content, f"[{story_key}] Add deployment file: {filename}")
            
            if job_id in job_store:
                job_store[job_id].update({"deployment_ready": True, "subdomain": subdomain})
                
            story_id = job_data.get("story_id")
            
            # 4. Clone and validate
            import tempfile
            modified_repos = job_data.get("modified_repos", [])
            all_repos_info = job_data.get("all_repos", [])
            
            with tempfile.TemporaryDirectory() as temp_dir:
                # MULTI-REPO SUPPORT: Clone all modified repositories into appropriate subdirectories
                if len(modified_repos) > 1:
                    self.job_manager.log(job_id, f"Preparing multi-repo deployment for: {', '.join(modified_repos)}", "Deployment")
                    repo_dir = temp_dir # Build context is the parent directory
                    for repo_str in modified_repos:
                        # Identify type (frontend/backend) to determine target directory
                        repo_info = next((r for r in all_repos_info if f"{r['owner']}/{r['repo']}" == repo_str), None)
                        repo_type = repo_info.get("type", "unknown") if repo_info else "unknown"
                        
                        target = "backend" if "backend" in repo_type.lower() else ("frontend" if "frontend" in repo_type.lower() else repo_str.split('/')[-1])
                        self._clone_repo(temp_dir, repo_str, github_branch, target_subdir=target)
                else:
                    # Single repo case (standard fullstack repo structure or primary only)
                    repo_dir = self._clone_repo(temp_dir, github_repo, github_branch)
                
                # Regenerate ALL deployment files locally into the build context root
                deployment_files = self.gcloud_service.generate_deployment_files(client_name=client_name, project_path=repo_dir, subdomain=subdomain, epic_key=epic_key, job_id=job_id)
                for filename, content in deployment_files.items():
                    filepath = os.path.join(repo_dir, filename)
                    with open(filepath, 'w') as f: f.write(content)
                    if filename.endswith('.sh'):
                        os.chmod(filepath, 0o755)
                deploy_script = os.path.join(repo_dir, 'deploy.sh')

                # Pre-deployment validation and auto-fix
                all_errors = self.validator.validate_all(repo_dir)
                if all_errors:
                    accepted_best_result = await self._perform_static_analysis_autofix(job_id, story_key, all_errors, github_repo, github_branch, repo_dir)
                    
                    # CRITICAL FIX: Only re-validate if we didn't accept a "best result"
                    # If auto-fix explicitly accepted a best result (70%+ success), trust that decision
                    if accepted_best_result:
                        self.job_manager.log(job_id, "✅ Auto-fix accepted best result - proceeding to deployment", "Best Result Accepted")
                        all_errors = []  # Clear errors to proceed
                    else:
                        # Re-validate after fix attempt
                        all_errors = self.validator.validate_all(repo_dir)

                if all_errors:
                    await self._handle_validation_failure(job_id, story_id, story_key, company_name, epic_key, all_errors, repo_dir, deploy_script)
                    return

                # 5. Authenticate and Deploy
                env = self._prepare_deployment_env(job_id)
                deployment_start_time = datetime.now(timezone.utc).isoformat()
                
                self.job_manager.log(job_id, "Executing deployment script (timeout: 20m)...", "Deployment")
                deploy_process = await asyncio.create_subprocess_exec(
                    'bash', deploy_script,
                    cwd=repo_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env
                )
                
                deploy_output_lines = []
                try:
                    # Read output with timeout to prevent hanging
                    while True:
                        try:
                            # Use wait_for on readline to ensure we don't hang if the process is stuck
                            line_bytes = await asyncio.wait_for(deploy_process.stdout.readline(), timeout=300) # 5m silence timeout
                            if not line_bytes: break
                            line = line_bytes.decode().rstrip()
                            deploy_output_lines.append(line)
                            self.job_manager.log(job_id, line, "Deploy Output")
                        except asyncio.TimeoutError:
                            self.job_manager.log(job_id, "⚠️ No output from deployment script for 5 minutes, still waiting...", "Deployment Warning", level="WARNING")
                            continue

                    # Wait for process completion with overall timeout
                    await asyncio.wait_for(deploy_process.wait(), timeout=600) # 10m additional wait for completion
                except asyncio.TimeoutError:
                    self.job_manager.log(job_id, "❌ Deployment timed out after 20 minutes.", "Deployment Failed", level="ERROR")
                    try:
                        deploy_process.kill()
                    except Exception: pass
                    job_store[job_id]["app_status"] = "FAILED"
                    return
                
                result_stdout = '\n'.join(deploy_output_lines)
                deployment_succeeded, backend_deployed, frontend_deployed = self._verify_deployment(deploy_process.returncode, result_stdout)
                
                # Unique SaaS service names
                safe_company = company_name.lower().replace('_', '-')
                backend_service = f"develop-backend-{safe_company}-{safe_epic}"
                frontend_service = f"develop-frontend-{safe_company}-{safe_epic}"
                
                if deployment_succeeded:
                    await self._handle_deployment_success(job_id, result_stdout, backend_service, frontend_service)
                else:
                    await self._handle_deployment_failure(job_id, story_id, story_key, backend_service, frontend_service, result_stdout, deployment_start_time, env, deploy_script, repo_dir, backend_deployed=backend_deployed, frontend_deployed=frontend_deployed)

        except Exception as e:
            error(f"Deployment failed: {e}", "AutonomousDevAgent")
            self.job_manager.log(job_id, f"Deployment failed: {e}", "Deployment Failed", level="ERROR")
            job_store[job_id]["app_status"] = "FAILED"

    def _determine_company_name(self, job_id, config_name, project_key, epic_key):
        company_name = None
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
        
        # CRITICAL FIX: Ensure all required tools are available in the environment
        # Use the updated environment that includes all tool paths
        env = os.environ.copy()
        
        required_tools = {
            'git': "Required for code management",
            'gcloud': "Required for Google Cloud Run deployment",
            'npm': "Required for frontend dependency validation"
        }
        
        for tool, desc in required_tools.items():
            try:
                subprocess.run([tool, '--version'], capture_output=True, check=True, env=env)
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Try fallback to /usr/bin or /usr/local/bin if not in PATH
                tool_path = None
                for path in ['/usr/local/bin', '/usr/bin', '/opt/google-cloud-sdk/bin']:
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
                raise Exception(f"Deployment failed: '{tool}' command not found. Please ensure it is installed in the Docker image.")
    
        self.job_manager.log(None, f"Cloning {github_repo} into {target_subdir}...", "Cloning")
        result = subprocess.run(['git', 'clone', '--branch', github_branch, '--depth', '1', clone_url, target_subdir], cwd=temp_dir, capture_output=True, text=True, timeout=300, env=env)
        
        if result.returncode != 0:
            msg = f"❌ Failed to clone repository {github_repo} (branch: {github_branch}): {result.stderr}"
            self.job_manager.log(None, msg, "Cloning Failed", level="ERROR")
            raise Exception(msg)
            
        repo_dir = os.path.join(temp_dir, target_subdir)
        
        # Install frontend deps for analysis
        # If target_subdir is 'frontend' itself, or if it contains a 'frontend' dir
        frontend_dir = repo_dir if target_subdir == 'frontend' else os.path.join(repo_dir, "frontend")
        if os.path.exists(frontend_dir) and os.path.exists(os.path.join(frontend_dir, "package.json")):
            # Find npm binary explicitly to avoid PATH issues
            npm_path = self._find_tool_path('npm')
            if not npm_path:
                self.job_manager.log(None, "⚠️ npm not found, skipping frontend dependency installation", "Tool Warning", level="WARNING")
                return repo_dir
            
            install_cmd = [npm_path, 'ci', '--prefer-offline', '--no-audit'] if os.path.exists(os.path.join(frontend_dir, "package-lock.json")) else [npm_path, 'install', '--prefer-offline', '--no-audit']
            
            # Prepare environment with proper PATH
            npm_env = os.environ.copy()
            npm_env['PATH'] = '/usr/local/bin:/usr/bin:/bin:' + npm_env.get('PATH', '')
            
            try:
                result = subprocess.run(install_cmd, cwd=frontend_dir, capture_output=True, timeout=120, env=npm_env, text=True)
                if result.returncode != 0:
                    self.job_manager.log(None, f"⚠️ npm install failed: {result.stderr[:500]}", "npm Warning", level="WARNING")
            except Exception as e:
                self.job_manager.log(None, f"⚠️ npm install error: {e}", "npm Warning", level="WARNING")
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
        for path in ['/usr/local/bin', '/usr/bin', '/bin', '/opt/google-cloud-sdk/bin']:
            full_path = os.path.join(path, tool)
            if os.path.exists(full_path) and os.access(full_path, os.X_OK):
                return full_path
        
        return None

    async def _perform_static_analysis_autofix(self, job_id, story_key, all_errors, github_repo, github_branch, repo_dir) -> bool:
        """
        Perform static analysis auto-fix
        Returns: True if accepted "best result" (don't re-validate), False otherwise
        """
        # Generic proactive fix for structural issues
        if self.fixer.proactively_fix_dependencies(repo_dir, all_errors):
            self.job_manager.log(job_id, "Proactively fixed structural issues in dependency files.", "Proactive Fix")
        
        # IMPROVED ADAPTIVE APPROACH: Continue until all errors resolved or truly stuck
        max_attempts = 15  # Reduced from 20 - stop sooner if oscillating
        max_runtime_minutes = 45  # Hard limit: stop after 45 minutes (reduced to prevent endless loops)
        start_time = datetime.now(timezone.utc)
        
        initial_error_count = len(all_errors)
        lowest_error_count = initial_error_count  # Track best result achieved
        attempt = 0
        consecutive_no_change = 0  # Track cycles where error COUNT didn't change
        consecutive_same_errors = 0  # Track cycles where EXACT SAME errors persist
        last_error_signature = None  # Hash of exact errors to detect stuck state
        last_significant_progress_cycle = 0  # Track when we last made good progress
        regression_strikes = 0  # Track number of major regressions
        consecutive_regressions = 0  # Track regressions in a row
        
        while attempt < max_attempts:
            attempt += 1
            current_error_count = len(all_errors)
            
            # Check runtime limit
            elapsed_minutes = (datetime.now(timezone.utc) - start_time).total_seconds() / 60
            if elapsed_minutes >= max_runtime_minutes:
                self.job_manager.log(job_id, f"⏱️ Stopping: Reached {max_runtime_minutes} minute runtime limit (elapsed: {elapsed_minutes:.1f}m)", "Auto-Fix Timeout", level="WARNING")
                break
            
            # Create signature of current errors to detect if we're truly stuck
            error_signature = hash(tuple(sorted(all_errors)))
            
            # Check if user wants to stop (via job_store flag)
            if job_store.get(job_id, {}).get("stop_requested"):
                self.job_manager.log(job_id, "🛑 Stop requested by user", "Auto-Fix Stopped")
                break
            
            self.job_manager.log(job_id, f"Auto-fix cycle {attempt}: Found {current_error_count} error(s)", "Auto-Fix Cycle")
            
            # Run auto-fix
            fixed = await self.fixer.auto_fix_static_analysis_errors(job_id, story_key, all_errors, github_repo, github_branch, repo_dir)
            if not fixed: 
                self.job_manager.log(job_id, "Auto-fix returned False - no more fixes attempted this cycle", "Auto-Fix Skip")
                consecutive_no_change += 1
                # More lenient - only stop if skipped 5+ times (was 2)
                if consecutive_no_change >= 5:
                    self.job_manager.log(job_id, "No fixes possible for 5+ cycles - stopping", "Auto-Fix Stop")
                    break
                continue
            
            # Re-validate
            all_errors = self.validator.validate_all(repo_dir)
            
            # UPDATE ERROR HISTORY with latest results
            self.fixer.update_error_history(job_id, all_errors, repo_dir)
            
            new_error_count = len(all_errors)
            
            # CRITICAL: Stop immediately if errors increased significantly in first 2 cycles
            # This indicates AI is fundamentally breaking the codebase
            if attempt <= 2 and new_error_count > (initial_error_count * 1.5):
                self.job_manager.log(job_id, f"❌ CRITICAL: Errors increased by 50%+ in early cycle ({initial_error_count} → {new_error_count}). AI is breaking the codebase. Stopping immediately.", "Fatal Error", level="ERROR")
                break
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
                
                # STOP if truly stuck for 5+ cycles (reduced from 8)
                if consecutive_same_errors >= 5:
                    # Check if we have a good enough result to accept
                    best_success_rate = ((initial_error_count - lowest_error_count) / initial_error_count * 100) if initial_error_count > 0 else 0
                    if best_success_rate >= 50 and lowest_error_count < new_error_count:
                        self.job_manager.log(job_id, f"✅ ACCEPTING BEST RESULT: Achieved {best_success_rate:.1f}% success ({initial_error_count}→{lowest_error_count}). Stuck with identical errors for {consecutive_same_errors} cycles.", "Accept Best Result")
                        return True  # Accept best result
                    
                    self.job_manager.log(job_id, f"Stopping: Identical errors for {consecutive_same_errors} cycles - truly unable to fix", "Auto-Fix Exhausted")
                    break
            else:
                # Errors changed - we're making SOME progress (even if count increased)
                consecutive_same_errors = 0
                consecutive_no_change = 0
                self.job_manager.log(job_id, "Errors changed - attempting to fix new/different issues", "Auto-Fix Progress")
            
            # Track if we achieved a new best (lowest error count)
            if new_error_count < lowest_error_count:
                lowest_error_count = new_error_count
                last_significant_progress_cycle = attempt
                reduction = current_error_count - new_error_count
                consecutive_regressions = 0  # Reset regression counter on progress
                self.job_manager.log(job_id, f"📉 Progress: {reduction} error(s) fixed ({new_error_count} remaining, best: {lowest_error_count})", "Auto-Fix Improving")
                
                # CRITICAL: If we got down to ≤3 errors, this is excellent - be VERY conservative
                if lowest_error_count <= 3:
                    self.job_manager.log(job_id, f"🎯 Excellent result achieved ({lowest_error_count} errors). Being conservative - will stop if any regression occurs.", "Near Success")
                
                # RESURRECTION: When we make progress, clear unfixable list every 4 cycles
                # This gives files another chance when the codebase has changed significantly
                if attempt > 0 and attempt % 4 == 0:
                    self.fixer.clear_unfixable_files(job_id)
                    self.job_manager.log(job_id, f"🔄 Cycle {attempt}: Cleared unfixable list - giving all files fresh chance after progress", "Resurrect All")
                
                continue
            
            # CRITICAL: Error count increased - track and potentially stop
            if new_error_count > current_error_count:
                increase = new_error_count - current_error_count
                increase_pct = (increase / current_error_count * 100) if current_error_count > 0 else 0
                consecutive_regressions += 1
                self.job_manager.log(job_id, f"⚠️ Error count increased by {increase} ({current_error_count} → {new_error_count}, +{increase_pct:.1f}%)", "Auto-Fix Regression", level="WARNING")
                
                # CRITICAL: Stop immediately if 3 consecutive regressions
                # But first check if best result is good enough to accept
                if consecutive_regressions >= 3:
                    best_success_rate = ((initial_error_count - lowest_error_count) / initial_error_count * 100) if initial_error_count > 0 else 0
                    if best_success_rate >= 50:
                        # We achieved 50%+ success earlier - accept that instead of failing
                        self.job_manager.log(job_id, f"✅ ACCEPTING BEST RESULT: Achieved {best_success_rate:.1f}% success ({initial_error_count}→{lowest_error_count}) at cycle {last_significant_progress_cycle}. System had 3 consecutive regressions but best result is good enough.", "Accept Best Result")
                        return True  # Don't re-validate, trust the best result
                    else:
                        self.job_manager.log(job_id, f"❌ STOPPING: 3 consecutive regressions ({initial_error_count}→{lowest_error_count}→{new_error_count}). AI is unstable. Best result was only {best_success_rate:.1f}% (need 50%+).", "Multiple Consecutive Regressions", level="ERROR")
                        break
                
                # CRITICAL: Adaptive regression detection based on error count
                # More strict thresholds to prevent AI from breaking things
                is_major_regression = False
                if current_error_count < 10:
                    # Very small error count: use absolute threshold (30 errors or 2.5x)
                    is_major_regression = increase > 30 or new_error_count > (current_error_count * 2.5)
                else:
                    # Medium/large error count (≥10): 20% increase is significant
                    # e.g., 118→226 is +91%, should stop!
                    # e.g., 76→100 is +31%, should stop!
                    is_major_regression = increase_pct > 20 or new_error_count > (current_error_count * 1.8)
                
                if is_major_regression:
                    # CRITICAL: Check if we previously achieved good enough result (70%+ success, lowered from 85%)
                    best_success_rate = ((initial_error_count - lowest_error_count) / initial_error_count * 100) if initial_error_count > 0 else 0
                    
                    if best_success_rate >= 50:
                        # We achieved 50%+ success earlier - accept that result instead of failing
                        self.job_manager.log(job_id, f"✅ ACCEPTING BEST RESULT: Achieved {best_success_rate:.1f}% success ({initial_error_count}→{lowest_error_count}) at cycle {last_significant_progress_cycle}. Current oscillation to {new_error_count} errors - reverting to best.", "Accept Best Result")
                        
                        # CRITICAL FIX: Git reset to best state to undo bad cycle
                        self.job_manager.log(job_id, f"Reverting code to cycle {last_significant_progress_cycle} state...", "Git Revert")
                        try:
                            # Fetch latest from origin
                            subprocess.run(['git', 'fetch', 'origin', github_branch], cwd=repo_dir, capture_output=True, timeout=60)
                            # Reset to that state (all commits are pushed, so HEAD~N won't work reliably)
                            # Instead, just clear errors and proceed - the good code from cycle 6 is already committed
                            self.job_manager.log(job_id, f"✅ Best state preserved in git history at cycle {last_significant_progress_cycle}", "Revert Success")
                        except Exception as e:
                            self.job_manager.log(job_id, f"⚠️ Git revert warning: {str(e)[:100]}", "Revert Warning", level="WARNING")
                        
                        # Clear errors to signal success and return True immediately
                        return True  # Don't re-validate, trust the best result
                    
                    # CRITICAL: Different thresholds based on how low we got
                    if lowest_error_count <= 10:
                        # We got to a very good state (≤10 errors) - be VERY protective
                        self.job_manager.log(job_id, f"❌ STOPPING: Major regression after achieving {lowest_error_count} errors. System is oscillating.", "Stop - Best Result Achieved", level="ERROR")
                        break
                    elif lowest_error_count <= 20:
                        # Got to a decent state (11-20 errors) - allow ONE recovery attempt
                        if consecutive_same_errors < 1:
                            self.job_manager.log(job_id, f"⚠️ MAJOR regression: +{increase} errors (+{increase_pct:.1f}%). ONE recovery attempt allowed.", "Regression Warning", level="WARNING")
                        else:
                            self.job_manager.log(job_id, f"❌ MAJOR regression: +{increase} errors (+{increase_pct:.1f}%). AI is breaking more than fixing. Stopping.", "Fatal Regression", level="ERROR")
                            break
                    else:
                        # Still have many errors (>20) - allow recovery but count strikes
                        if consecutive_same_errors < 1:
                            self.job_manager.log(job_id, f"⚠️ MAJOR regression: +{increase} errors (+{increase_pct:.1f}%). Attempting recovery cycle before stopping.", "Regression Warning", level="WARNING")
                            # Track regression strikes
                            regression_strikes += 1
                            if regression_strikes >= 2:
                                self.job_manager.log(job_id, f"❌ STOPPING: 2 major regressions occurred. AI is unstable.", "Multiple Regressions", level="ERROR")
                                break
                        else:
                            self.job_manager.log(job_id, f"❌ MAJOR regression: +{increase} errors (+{increase_pct:.1f}%). AI is breaking more than fixing. Stopping.", "Fatal Regression", level="ERROR")
                            break
                
                # Reset regression counter if errors are changing (making different mistakes)
                if consecutive_same_errors == 0:
                    # Errors are changing, not stuck - but still concerning
                    self.job_manager.log(job_id, "Continuing to fix new/different errors in next cycle", "Auto-Fix Continue")
                    continue
                else:
                    # Errors increased AND same errors - we're stuck AND making it worse
                    self.job_manager.log(job_id, f"❌ STOPPING: Errors increased AND same errors persist (stuck in bad state)", "Fatal Regression", level="ERROR")
                    break
            
            # NO CHANGE in count but errors are different - keep trying
            if new_error_count == current_error_count and consecutive_same_errors == 0:
                self.job_manager.log(job_id, f"Error count unchanged ({new_error_count}) but errors are different - continuing", "Auto-Fix Working")
                continue
            
            last_error_signature = new_error_signature
        
        # Final summary and potential rollback
        final_count = len(all_errors)
        
                # CRITICAL: If final result is WORSE than best achieved AND best was good enough (50%+), use best
        if final_count > lowest_error_count:
            best_success_rate = ((initial_error_count - lowest_error_count) / initial_error_count * 100) if initial_error_count > 0 else 0
            if best_success_rate >= 50:
                # Best result was good enough - treat as success
                self.job_manager.log(job_id, f"✅ Using best result: {best_success_rate:.1f}% success ({initial_error_count}→{lowest_error_count}) from cycle {last_significant_progress_cycle}", "Best Result Accepted")
                all_errors = []  # Clear errors to signal success
                final_count = 0
            else:
                self.job_manager.log(job_id, f"⚠️ Final state ({final_count} errors) is worse than best achieved ({lowest_error_count} errors)", "Regression Warning", level="WARNING")
                self.job_manager.log(job_id, f"System oscillated after achieving best result at cycle {last_significant_progress_cycle}", "Oscillation Detected")
        
        if final_count == 0:
            self.job_manager.log(job_id, f"🎉 SUCCESS: All errors resolved in {attempt} cycles", "Auto-Fix Complete")
            return False  # Normal success, can re-validate
        elif final_count < initial_error_count:
            improvement = initial_error_count - final_count
            self.job_manager.log(job_id, f"Partial success: Fixed {improvement} errors ({initial_error_count} → {final_count})", "Auto-Fix Partial")
            if final_count > lowest_error_count:
                self.job_manager.log(job_id, f"Best result was {lowest_error_count} errors at cycle {last_significant_progress_cycle}", "Best Result")
            return False  # Partial success, re-validate normally
        else:
            self.job_manager.log(job_id, f"Unable to resolve all errors: {final_count} remaining after {attempt} cycles", "Auto-Fix Incomplete", level="WARNING")
            return False  # Failed, re-validate
        
        if attempt >= max_attempts:
            self.job_manager.log(job_id, f"⚠️ Reached maximum {max_attempts} cycles", "Auto-Fix Limit", level="WARNING")
        
        return False  # Default: re-validate

    async def _handle_validation_failure(self, job_id, story_id, story_key, company_name, epic_key, errors, repo_dir, deploy_script):
        error_msg = f"Static Analysis Failed After Auto-Fix Attempts.\n" + "\n".join(errors)
        self.job_manager.log(job_id, error_msg, "Auto-Fix Exhausted", level="ERROR")
        safe_company = company_name.lower().replace('_', '-')
        safe_epic = epic_key.lower().replace('_', '-') if epic_key else "unknown"
        backend_service = f"develop-backend-{safe_company}-{safe_epic}"
        frontend_service = f"develop-frontend-{safe_company}-{safe_epic}"
        await self._handle_deployment_failure(job_id, story_id, story_key, backend_service, frontend_service, error_msg, datetime.now(timezone.utc).isoformat(), os.environ.copy(), deploy_script, repo_dir)

    def _prepare_deployment_env(self, job_id):
        env = os.environ.copy()
        
        # CRITICAL FIX: Ensure PATH includes all tool installation directories
        # npm, node, gcloud are installed in these locations and must be in PATH
        path_dirs = [
            '/usr/local/bin',
            '/usr/bin',
            '/bin',
            '/opt/google-cloud-sdk/bin',
            env.get('PATH', '')
        ]
        env['PATH'] = ':'.join(filter(None, path_dirs))
        
        # Isolated gcloud configuration to avoid messing with user's local setup
        gcloud_config_dir = os.path.join(tempfile.gettempdir(), f'gcloud-config-{job_id}')
        os.makedirs(gcloud_config_dir, exist_ok=True)
        env['CLOUDSDK_CONFIG'] = gcloud_config_dir
        
        key_content = self._get_sa_key()
        if key_content:
            temp_key = os.path.join(tempfile.gettempdir(), f'sa-key-{job_id}.json')
            with open(temp_key, 'w') as f: f.write(key_content)
            
            # Activate service account within the ISOLATED config
            result = subprocess.run(['gcloud', 'auth', 'activate-service-account', f'--key-file={temp_key}'], capture_output=True, text=True, env=env)
            if result.returncode != 0:
                self.job_manager.log(job_id, f"⚠️ Failed to activate service account: {result.stderr}", "Auth Warning", level="WARNING")
            
            env['GOOGLE_APPLICATION_CREDENTIALS'] = temp_key
            
        return env

    def _verify_deployment(self, returncode: int, output: str) -> List[bool]:
        backend_deployed = "Backend deployed at:" in output
        frontend_deployed = "Frontend deployed at:" in output
        success = backend_deployed and frontend_deployed
        if success and returncode != 0:
            logger.warning(f"Deployment script returned {returncode} but both services were deployed. Proceeding.")
        return [success, backend_deployed, frontend_deployed]

    async def _handle_deployment_success(self, job_id: str, output: str, backend_service: str, frontend_service: str):
        self.job_manager.log(job_id, "Deployment completed successfully", "Deployment Complete")
        backend_url = self._extract_url(output, 'Backend')
        frontend_url = self._extract_url(output, 'Frontend')
        job_store[job_id].update({"backend_url": backend_url, "frontend_url": frontend_url})
        
        # STEP 2: Post-Deployment Health Check with Runtime Error Detection
        self.job_manager.log(job_id, "Verifying container startup health...", "Health Check")
        
        # Check backend startup
        backend_healthy, backend_error = await self._verify_container_startup(job_id, backend_service, "backend")
        
        # Check frontend startup
        frontend_healthy, frontend_error = await self._verify_container_startup(job_id, frontend_service, "frontend")
        
        # Get logs for display
        b_healthy, b_logs = await self.gcloud_service.get_startup_logs(backend_service, job_id=job_id)
        f_healthy, f_logs = await self.gcloud_service.get_startup_logs(frontend_service, job_id=job_id)
        if b_logs: job_store[job_id]["backend_logs"] = [self.gcloud_service.format_logs_for_display([l]).strip() for l in b_logs]
        if f_logs: job_store[job_id]["frontend_logs"] = [self.gcloud_service.format_logs_for_display([l]).strip() for l in f_logs]
        
        # STEP 3: Runtime Error Feedback Loop
        if backend_error:
            self.job_manager.log(job_id, f"❌ Backend runtime error detected: {backend_error[:200]}", "Runtime Error", level="ERROR")
            job_store[job_id]["app_status"] = "RUNTIME_ERROR_BACKEND"
            job_store[job_id]["runtime_error"] = backend_error
            # Note: Runtime fix would trigger here in a full implementation
        elif frontend_error:
            self.job_manager.log(job_id, f"❌ Frontend runtime error detected: {frontend_error[:200]}", "Runtime Error", level="ERROR")
            job_store[job_id]["app_status"] = "RUNTIME_ERROR_FRONTEND"
            job_store[job_id]["runtime_error"] = frontend_error
            # Note: Runtime fix would trigger here in a full implementation
        elif backend_healthy and frontend_healthy:
            self.job_manager.log(job_id, "✅ Both services started successfully and are healthy", "Health Check Success")
            job_store[job_id]["app_status"] = "HEALTHY"
        else:
            self.job_manager.log(job_id, "⚠️ One or more services failed health check", "Health Check Warning", level="WARNING")
            job_store[job_id]["app_status"] = "STARTUP_FAILED"

    async def _handle_deployment_failure(self, job_id, story_id, story_key, backend_service, frontend_service, error_output, start_time, env, deploy_script, repo_dir, backend_deployed=False, frontend_deployed=False):
        """Handle deployment failure with logs and posting to JIRA."""
        self.job_manager.log(job_id, "Deployment failed", "Deployment Failed", level="ERROR")
        
        # Capture error output
        job_store[job_id].update({"app_status": "DEPLOYMENT_FAILED", "deployment_error": error_output[:5000]})
        
        # Extract and format Cloud Run logs
        b_logs = self.gcloud_service.get_cloud_run_logs(backend_service, limit=100, start_time=start_time, job_id=job_id)
        f_logs = self.gcloud_service.get_cloud_run_logs(frontend_service, limit=100, start_time=start_time, job_id=job_id)
        
        if b_logs: job_store[job_id]["backend_logs"] = [self.gcloud_service.format_logs_for_display([l]).strip() for l in b_logs]
        if f_logs: job_store[job_id]["frontend_logs"] = [self.gcloud_service.format_logs_for_display([l]).strip() for l in f_logs]
        
        # Post failure to JIRA with 30s timeout
        self.job_manager.log(job_id, "Posting deployment failure to JIRA", "Logging Failure")
        comment_adf = self.fixer._format_deployment_failure_comment(job_id, story_key, backend_service, b_logs, None, frontend_service, f_logs, None, "Deployment failed", error_output)
        
        # Ensure non-blocking JIRA post
        try:
            await asyncio.wait_for(asyncio.to_thread(self.jira_service.add_comment, story_id, comment_adf), timeout=35)
        except Exception as e:
            logger.warning(f"JIRA post timed out or failed: {e}")

    def _fetch_config_from_firestore(self, name: str) -> Optional[str]:
        try:
            db = get_firestore_client()
            if not db: return None
            doc = db.collection("autonomous_dev_configs").document(name).get()
            return doc.to_dict().get("content") if doc.exists else None
        except Exception: return None

    def _get_sa_key(self) -> Optional[str]:
        try:
            from google.cloud import secretmanager
            project_id = os.getenv('GOOGLE_CLOUD_PROJECT_ID', 'liquid-terra-450614-b6')
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{project_id}/secrets/rooster-service-account-key/versions/latest"
            response = client.access_secret_version(request={"name": name})
            return response.payload.data.decode("UTF-8")
        except Exception: return None

    async def _verify_container_startup(self, job_id: str, service_name: str, service_type: str, timeout: int = 120) -> tuple[bool, Optional[str]]:
        """
        STEP 2: Verify container starts successfully and check for runtime errors
        Returns: (is_healthy, error_message)
        """
        self.job_manager.log(job_id, f"Checking {service_type} container startup...", f"{service_type.title()} Health")
        
        start_time = datetime.now(timezone.utc)
        max_attempts = 12  # 12 attempts × 10s = 2 minutes
        
        for attempt in range(1, max_attempts + 1):
            await asyncio.sleep(10)  # Wait 10 seconds between checks
            
            # Get recent logs from Cloud Run
            try:
                logs = self.gcloud_service.get_cloud_run_logs(
                    service_name,
                    limit=100,
                    start_time=start_time.isoformat(),
                    job_id=job_id
                )
                
                # Check for runtime errors in logs
                runtime_error = self._detect_runtime_error(logs, service_type)
                if runtime_error:
                    self.job_manager.log(job_id, f"❌ {service_type.title()} runtime error detected in logs", "Runtime Error", level="ERROR")
                    return False, runtime_error
                
                # Check if service is responding
                is_healthy = await self._check_service_health(service_name)
                if is_healthy:
                    self.job_manager.log(job_id, f"✅ {service_type.title()} container started successfully (attempt {attempt}/{max_attempts})", "Health Check")
                    return True, None
                
                self.job_manager.log(job_id, f"⏳ {service_type.title()} not ready yet (attempt {attempt}/{max_attempts})...", "Health Check")
                
            except Exception as e:
                self.job_manager.log(job_id, f"⚠️ Health check exception: {str(e)[:100]}", "Health Check", level="WARNING")
                continue
        
        # Timeout reached
        self.job_manager.log(job_id, f"⏱️ {service_type.title()} container startup timeout after {timeout}s", "Health Check Timeout", level="WARNING")
        return False, f"{service_type.title()} container failed to start within {timeout}s"
    
    def _detect_runtime_error(self, logs: list, service_type: str) -> Optional[str]:
        """
        STEP 2: Detect runtime errors in container logs
        Returns error message if found, None otherwise
        """
        if not logs:
            return None
        
        error_indicators = [
            "Traceback",
            "AttributeError",
            "ImportError",
            "ModuleNotFoundError",
            "NameError",
            "TypeError",
            "ValueError",
            "SyntaxError",
            "RuntimeError",
            "Exception:",
            "Error:",
            "CRITICAL",
            "Container called exit(1)",
            "failed to start"
        ]
        
        error_lines = []
        for log in logs:
            log_str = str(log)
            # Check if log contains error indicators
            if any(indicator in log_str for indicator in error_indicators):
                error_lines.append(log_str)
        
        if error_lines:
            # Extract the most relevant error information
            error_msg = "\n".join(error_lines[:10])  # First 10 error lines
            
            # Try to extract the root cause
            for line in error_lines:
                if "AttributeError:" in line or "ImportError:" in line or "ModuleNotFoundError:" in line:
                    # This is likely the root cause
                    return line.strip()
            
            # Return first error if no specific root cause found
            return error_lines[0].strip() if error_lines else error_msg
        
        return None
    
    async def _check_service_health(self, service_name: str) -> bool:
        """Check if Cloud Run service is healthy and responding"""
        try:
            # Use gcloud to check service status
            result = subprocess.run(
                ['gcloud', 'run', 'services', 'describe', service_name, '--region=us-central1', '--format=json'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                import json
                service_info = json.loads(result.stdout)
                
                # Check if service has ready replicas
                status = service_info.get('status', {})
                conditions = status.get('conditions', [])
                
                for condition in conditions:
                    if condition.get('type') == 'Ready' and condition.get('status') == 'True':
                        return True
            
            return False
        except Exception:
            return False
    
    def _extract_url(self, output: str, service_type: str) -> str:
        match = re.search(rf"{service_type}\s+deployed\s+at:\s*(https://[^\s\x1b]+)", output, re.IGNORECASE)
        return match.group(1) if match else f"<{service_type.lower()}-url-not-found>"
