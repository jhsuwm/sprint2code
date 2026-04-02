import os
import re
import base64
from typing import Dict, Any, Optional, List
import requests
from log_config import logger, error, info

class GitHubService:
    def __init__(self):
        # GitHub Personal Access Token should be set in environment
        self.token = os.getenv("GITHUB_TOKEN", "")
        
        # Detect token type and use appropriate authorization format
        # Classic PAT (ghp_*): Use "token" prefix
        # Fine-grained PAT (github_pat_*): Use "Bearer" prefix (recommended for SaaS)
        # OAuth tokens: Use "Bearer" prefix
        self.token_type = self._detect_token_type()
        auth_prefix = "Bearer" if self.token_type == "fine-grained" else "token"
        
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"{auth_prefix} {self.token}" if self.token else "",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        
        # Default GitHub repository for autonomous dev (can be overridden in JIRA description)
        self.default_owner = os.getenv("GITHUB_DEFAULT_OWNER", "")
        self.default_repo = os.getenv("GITHUB_DEFAULT_REPO", "")
        
        if not self.token:
            warning("GitHubService initialized without GITHUB_TOKEN. Set environment variable for GitHub integration.")
        else:
            # Mask token for security - only show first 7 characters
            token_preview = self.token[:7] + "..." if len(self.token) > 7 else "***"
            info(f"GitHubService initialized with GitHub token ({self.token_type}): {token_preview}")
            if self.default_owner and self.default_repo:
                info(f"Default GitHub repository: {self.default_owner}/{self.default_repo}")
            
            # Validate token on initialization
            self._validate_token()
    
    def _detect_token_type(self) -> str:
        """
        Detect the type of GitHub token based on its prefix.
        
        Returns:
            "fine-grained" for Fine-grained PATs (github_pat_*)
            "classic" for Classic PATs (ghp_*)
            "oauth" for OAuth tokens (gho_*, ghu_*, ghs_*)
        """
        if not self.token:
            return "unknown"
        
        if self.token.startswith("github_pat_"):
            return "fine-grained"
        elif self.token.startswith("ghp_"):
            return "classic"
        elif self.token.startswith(("gho_", "ghu_", "ghs_")):
            return "oauth"
        else:
            return "unknown"
    
    def _validate_token(self):
        """
        Validate the GitHub token by making a test API call.
        This helps detect authentication issues early.
        """
        if not self.token:
            return
        
        try:
            # Test authentication by getting the authenticated user
            url = "https://api.github.com/user"
            response = requests.get(url, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                user_data = response.json()
                username = user_data.get("login", "unknown")
                info(f"✓ GitHub token validated successfully. Authenticated as: {username}")
                
                # Log rate limit info
                rate_limit = response.headers.get("X-RateLimit-Remaining")
                rate_limit_reset = response.headers.get("X-RateLimit-Reset")
                if rate_limit:
                    info(f"GitHub API rate limit: {rate_limit} requests remaining")
            elif response.status_code == 401:
                error("✗ GitHub token validation FAILED: 401 Unauthorized. Token may be invalid or expired.", "GitHubService")
                error(f"Response: {response.text}")
                error("SOLUTION: Generate a new Classic Personal Access Token at https://github.com/settings/tokens")
            elif response.status_code == 403:
                error("✗ GitHub token validation FAILED: 403 Forbidden. Token may lack required permissions.", "GitHubService")
                error(f"Response: {response.text}")
                error("SOLUTION: Ensure token has 'repo' scope permissions")
            else:
                error(f"✗ GitHub token validation returned unexpected status: {response.status_code}", "GitHubService")
                error(f"Response: {response.text}")
        except requests.exceptions.Timeout:
            warning("GitHub token validation timed out. Network may be slow.")
        except Exception as e:
            error(f"Error validating GitHub token: {e}", "GitHubService")

    def _make_github_api_request_with_repo_fallback(self, method: str, url_template: str, owner: str, repo: str, **kwargs) -> requests.Response:
        """
        Makes a GitHub API request, with a fallback to append ".git" to the repo name if the initial request returns a a 404 Not Found.
        """
        # Try with the original repo name first
        url = url_template.format(owner=owner, repo=repo)
        debug(f"Attempting GitHub API request: {method} {url}")
        response = requests.request(method, url, headers=self.headers, **kwargs)
        
        if response.status_code == 404 and not repo.endswith(".git"):
            info(f"Received 404 for {url}, retrying with {repo}.git")
            # Retry with .git appended to repo name
            fallback_repo = f"{repo}.git"
            fallback_url = url_template.format(owner=owner, repo=fallback_repo)
            response = requests.request(method, fallback_url, headers=self.headers, **kwargs)
            debug(f"Retried GitHub API request: {method} {fallback_url} -> {response.status_code}")
            
        return response

    def extract_github_repo_from_description(self, description: str) -> Optional[Dict[str, str]]:
        """
        Extract GitHub repository URL from JIRA description.
        Supports formats:
        - https://github.com/owner/repo
        - https://github.com/owner/repo.git
        - github.com/owner/repo
        
        Returns dict with 'owner' and 'repo' or None if not found.
        """
        if not description:
            warning("Description is None or empty")
            return None
        
        # Log the original description for debugging
        info(f"Original description type: {type(description)}")
        
        # Convert description to string if it's an object (ADF format)
        if isinstance(description, dict):
            description = self._extract_text_from_adf(description)
        
        # Log the extracted text
        info(f"Extracted text from description: {description[:500] if description else 'None'}")
        
        if not description:
            warning("Extracted text is empty")
            return None
        
        # Pattern to match GitHub URLs - strips .git suffix if present
        patterns = [
            r'https?://github\.com/([^/\s]+)/([^/\s]+?)(\.git)?(?:\s|$)',
            r'github\.com/([^/\s]+)/([^/\s]+?)(\.git)?(?:\s|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, str(description), re.IGNORECASE)
            if match:
                owner = match.group(1)
                # Strip .git suffix - GitHub API doesn't accept it in repo names
                repo = match.group(2)
                info(f"✓ Extracted GitHub repo from description: {owner}/{repo}")
                return {"owner": owner, "repo": repo}
        
        warning(f"No GitHub repository URL found in description text: {description[:200]}")
        return None
    
    def _extract_text_from_adf(self, adf: Any) -> str:
        """
        Extract text from Atlassian Document Format (ADF).
        Handles text nodes, links (marks), inlineCard nodes, and nested content.
        """
        if not adf:
            return ''
        if isinstance(adf, str):
            return adf
        
        text = ''
        if isinstance(adf, dict):
            # Extract direct text
            if 'text' in adf:
                text += adf['text'] + ' '
            
            # Check for inlineCard nodes (like GitHub URLs in JIRA)
            if adf.get('type') == 'inlineCard' and 'attrs' in adf:
                url = adf['attrs'].get('url', '')
                if url:
                    text += url + ' '
                    info(f"Found inlineCard URL: {url}")
            
            # Check for marks (like links) which contain the URL
            if 'marks' in adf and isinstance(adf['marks'], list):
                for mark in adf['marks']:
                    if isinstance(mark, dict) and mark.get('type') == 'link':
                        # Extract URL from link mark
                        href = mark.get('attrs', {}).get('href', '')
                        if href:
                            text += href + ' '
            
            # Recursively extract from nested content
            if 'content' in adf and isinstance(adf['content'], list):
                for node in adf['content']:
                    text += self._extract_text_from_adf(node) + ' '
        
        elif isinstance(adf, list):
            # Handle lists directly
            for item in adf:
                text += self._extract_text_from_adf(item) + ' '
        
        return text.strip()
    
    def get_default_branch(self, owner: str, repo: str) -> Optional[str]:
        """Get the default branch name for a repository."""
        url = f"https://api.github.com/repos/{owner}/{repo}"
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 404:
                error(f"Repository {owner}/{repo} not found. If this is a private repo, ensure your GitHub token has access to it.", "GitHubService")
                error(f"💡 SOLUTION: The GitHub token may need access to this private repository.")
                error(f"   1. Go to https://github.com/settings/tokens")
                error(f"   2. Regenerate your token or create a new one")
                error(f"   3. Ensure it has 'repo' scope (full control of private repositories)")
                error(f"   4. For organization repos, ensure the token has organization access")
                return "main"
            response.raise_for_status()
            data = response.json()
            default_branch = data.get("default_branch", "main")
            info(f"Default branch for {owner}/{repo}: {default_branch}")
            return default_branch
        except requests.exceptions.HTTPError as e:
            error(f"Failed to get default branch: {e}", "GitHubService")
            return "main"  # Fallback to main
        except Exception as e:
            error(f"Failed to get default branch: {e}", "GitHubService")
            return "main"  # Fallback to main
    
    def get_branch_sha(self, owner: str, repo: str, branch: str, retry_attempts: int = 3, retry_delay: float = 1.0) -> Optional[str]:
        """
        Get the SHA of the latest commit on a branch.
        Retries on 404 errors to handle GitHub API propagation delays.
        """
        import time
        url = f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}"
        
        for attempt in range(retry_attempts):
            try:
                response = requests.get(url, headers=self.headers)
                response.raise_for_status()
                data = response.json()
                sha = data["object"]["sha"]
                info(f"Got SHA for {owner}/{repo} branch {branch}: {sha}")
                return sha
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404 and attempt < retry_attempts - 1:
                    # Branch might not be visible yet due to API propagation delay
                    info(f"Branch {branch} not found (attempt {attempt + 1}/{retry_attempts}), retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    continue
                error(f"Failed to get branch SHA: {e}", "GitHubService")
                return None
            except Exception as e:
                error(f"Failed to get branch SHA: {e}", "GitHubService")
                return None
        
        return None
    
    def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        """
        Check if a branch exists in the repository.
        
        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name to check
        
        Returns:
            True if branch exists, False otherwise
        """
        url = f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}"
        try:
            response = requests.get(url, headers=self.headers)
            exists = response.status_code == 200
            if exists:
                info(f"Branch {branch} exists in {owner}/{repo}")
            else:
                info(f"Branch {branch} does not exist in {owner}/{repo}")
            return exists
        except Exception as e:
            error(f"Error checking if branch exists: {e}", "GitHubService")
            return False
    
    def create_branch(self, owner: str, repo: str, branch_name: str, source_branch: str = None) -> bool:
        """
        Create a new branch from the source branch (defaults to default branch).
        
        Args:
            owner: Repository owner
            repo: Repository name
            branch_name: Name of the new branch to create
            source_branch: Source branch to branch from (optional, uses default branch if not provided)
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Get the default branch if source not specified
            if not source_branch:
                source_branch = self.get_default_branch(owner, repo)
            
            # Get SHA of the source branch
            sha = self.get_branch_sha(owner, repo, source_branch)
            if not sha:
                # If we couldn't get SHA, it's likely a 404 or permission issue
                # and get_branch_sha already logged it
                return False
            
            # Create new branch
            url = f"https://api.github.com/repos/{owner}/{repo}/git/refs"
            payload = {
                "ref": f"refs/heads/{branch_name}",
                "sha": sha
            }
            
            response = requests.post(url, headers=self.headers, json=payload)
            
            if response.status_code == 201:
                info(f"Successfully created branch {branch_name} in {owner}/{repo}")
                return True
            elif response.status_code == 422:
                # Branch might already exist
                warning(f"Branch {branch_name} might already exist in {owner}/{repo}")
                return True  # Consider this a success for idempotency
            elif response.status_code == 404:
                error(f"Repository {owner}/{repo} not found during branch creation. Token may lack access to private repo.", "GitHubService")
                return False
            else:
                error(f"Failed to create branch: {response.status_code} - {response.text}", "GitHubService")
                return False
                
        except Exception as e:
            error(f"Error creating branch: {e}", "GitHubService")
            return False
    
    def commit_file(self, owner: str, repo: str, branch: str, file_path: str, 
                    content: str, commit_message: str) -> bool:
        """
        Commit a file to a branch. Creates or updates the file.
        Uses GitHub tree API to ensure parent directories are created.
        
        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name to commit to
            file_path: Path of the file in the repository
            content: File content
            commit_message: Commit message
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # First, try the simple Contents API approach
            url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
            params = {"ref": branch}
            
            file_sha = None
            response = requests.get(url, headers=self.headers, params=params)
            if response.status_code == 200:
                file_sha = response.json().get("sha")
            
            # Encode content to base64
            content_encoded = base64.b64encode(content.encode()).decode()
            
            # Create or update file
            payload = {
                "message": commit_message,
                "content": content_encoded,
                "branch": branch
            }
            
            if file_sha:
                payload["sha"] = file_sha
            
            response = requests.put(url, headers=self.headers, json=payload)
            
            if response.status_code in [200, 201]:
                info(f"Successfully committed {file_path} to {owner}/{repo}:{branch}")
                return True
            elif response.status_code == 403:
                # Permission error - log reference to documentation
                error(f"Failed to commit file: 403 Forbidden - {response.text}", "GitHubService")
                error(f"🚨 GITHUB TOKEN PERMISSION ERROR: Token lacks permissions for {owner}/{repo}")
                error(f"📖 See GITHUB_TOKEN_PERMISSIONS.md for detailed fix instructions")
                error(f"Quick fix: Generate new token at https://github.com/settings/tokens with 'repo' scope")
                return False
            elif response.status_code == 404:
                # 404 likely means branch doesn't exist or was just created
                # Try using the tree API to create the file with parent directories
                info(f"Retrying {file_path} with tree API to ensure parent directories exist...")
                return self._commit_file_with_tree_api(owner, repo, branch, file_path, content, commit_message)
            else:
                error(f"Failed to commit file: {response.status_code} - {response.text}", "GitHubService")
                return False
                
        except Exception as e:
            error(f"Error committing file: {e}", "GitHubService")
            return False
    
    def _commit_file_with_tree_api(self, owner: str, repo: str, branch: str, 
                                    file_path: str, content: str, commit_message: str) -> bool:
        """
        Commit a file using GitHub's tree API, which can create parent directories.
        This is used as a fallback when the simple Contents API fails.
        """
        try:
            # Get the latest commit SHA on the branch
            ref_url = f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}"
            ref_response = requests.get(ref_url, headers=self.headers)
            
            if ref_response.status_code != 200:
                error(f"Failed to get branch ref: {ref_response.status_code}", "GitHubService")
                return False
            
            latest_commit_sha = ref_response.json()["object"]["sha"]
            
            # Get the tree SHA from the latest commit
            commit_url = f"https://api.github.com/repos/{owner}/{repo}/git/commits/{latest_commit_sha}"
            commit_response = requests.get(commit_url, headers=self.headers)
            
            if commit_response.status_code != 200:
                error(f"Failed to get commit: {commit_response.status_code}", "GitHubService")
                return False
            
            base_tree_sha = commit_response.json()["tree"]["sha"]
            
            # Create a blob for the file content
            blob_url = f"https://api.github.com/repos/{owner}/{repo}/git/blobs"
            blob_payload = {
                "content": base64.b64encode(content.encode()).decode(),
                "encoding": "base64"
            }
            blob_response = requests.post(blob_url, headers=self.headers, json=blob_payload)
            
            if blob_response.status_code != 201:
                error(f"Failed to create blob: {blob_response.status_code}", "GitHubService")
                return False
            
            blob_sha = blob_response.json()["sha"]
            
            # Create a new tree with the file
            tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees"
            tree_payload = {
                "base_tree": base_tree_sha,
                "tree": [
                    {
                        "path": file_path,
                        "mode": "100644",  # regular file
                        "type": "blob",
                        "sha": blob_sha
                    }
                ]
            }
            tree_response = requests.post(tree_url, headers=self.headers, json=tree_payload)
            
            if tree_response.status_code != 201:
                error(f"Failed to create tree: {tree_response.status_code} - {tree_response.text}", "GitHubService")
                return False
            
            new_tree_sha = tree_response.json()["sha"]
            
            # Create a new commit
            commit_create_url = f"https://api.github.com/repos/{owner}/{repo}/git/commits"
            commit_payload = {
                "message": commit_message,
                "tree": new_tree_sha,
                "parents": [latest_commit_sha]
            }
            commit_create_response = requests.post(commit_create_url, headers=self.headers, json=commit_payload)
            
            if commit_create_response.status_code != 201:
                error(f"Failed to create commit: {commit_create_response.status_code}", "GitHubService")
                return False
            
            new_commit_sha = commit_create_response.json()["sha"]
            
            # Update the branch reference
            ref_update_payload = {
                "sha": new_commit_sha,
                "force": False
            }
            ref_update_response = requests.patch(ref_url, headers=self.headers, json=ref_update_payload)
            
            if ref_update_response.status_code == 200:
                info(f"Successfully committed {file_path} to {owner}/{repo}:{branch} using tree API")
                return True
            else:
                error(f"Failed to update ref: {ref_update_response.status_code}", "GitHubService")
                return False
                
        except Exception as e:
            error(f"Error in tree API commit: {e}", "GitHubService")
            return False
    
    def list_files(self, owner: str, repo: str, branch: str) -> List[str]:
        """
        List all files in the repository for a given branch recursively.
        """
        try:
            url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                tree = response.json().get("tree", [])
                files = [item["path"] for item in tree if item["type"] == "blob"]
                return files
            else:
                warning(f"Failed to list files: {response.status_code}")
                return []
        except Exception as e:
            error(f"Error listing files: {e}", "GitHubService")
            return []

    def create_pull_request(self, owner: str, repo: str, head_branch: str, 
                           base_branch: str, title: str, body: str = "") -> Optional[str]:
        """
        Create a pull request.
        
        Args:
            owner: Repository owner
            repo: Repository name
            head_branch: Branch containing changes
            base_branch: Branch to merge into
            title: PR title
            body: PR description
        
        Returns:
            PR URL if successful, None otherwise
        """
        try:
            url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
            payload = {
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
                "maintainer_can_modify": True
            }
            
            response = requests.post(url, headers=self.headers, json=payload)
            
            if response.status_code == 201:
                pr_data = response.json()
                pr_url = pr_data.get("html_url")
                info(f"Successfully created PR: {pr_url}")
                return pr_url
            else:
                error(f"Failed to create PR: {response.status_code} - {response.text}", "GitHubService")
                return None
                
        except Exception as e:
            error(f"Error creating pull request: {e}", "GitHubService")
            return None
