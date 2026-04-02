import os
import requests
import json
import time
import mimetypes
import re
from requests.auth import HTTPBasicAuth
from typing import List, Dict, Any, Optional, Tuple
from log_config import logger, error, info, warning

class JiraService:
    def __init__(self):
        self.base_url = os.getenv("JIRA_URL", "https://your-domain.atlassian.net").rstrip('/')
        # Support both JIRA_USERNAME and JIRA_EMAIL (common for Cloud)
        self.username = os.getenv("JIRA_USERNAME") or os.getenv("JIRA_EMAIL", "")
        # Support both JIRA_API_TOKEN and JIRA_API_KEY
        self.api_token = os.getenv("JIRA_API_TOKEN") or os.getenv("JIRA_API_KEY", "")
        
        # Mock mode for demonstration if no credentials provided
        self.mock_mode = not (self.username and self.api_token)
        
        if self.mock_mode:
            warning("JiraService started in MOCK MODE. Set JIRA_URL, JIRA_EMAIL/USERNAME, and JIRA_API_TOKEN to use real API.")
        else:
            info(f"JiraService initialized in REAL MODE connecting to {self.base_url}")

    def _get_headers(self):
        return {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def _get_auth(self):
        return HTTPBasicAuth(self.username, self.api_token)

    def get_jira_structure(self) -> List[Dict[str, Any]]:
        """
        Get all projects (Spaces) and their Epics.
        Returns a hierarchical list of projects with their associated epics.
        """
        if self.mock_mode:
            # Return mock data with hierarchical structure
            return [
                {
                    "id": "proj-1",
                    "name": "Project Apollo",
                    "key": "APO",
                    "epics": [
                        {
                            "id": "20001",
                            "key": "APO-201",
                            "fields": {
                                "summary": "Core Infrastructure",
                                "status": {"name": "To Do"}
                            }
                        },
                        {
                            "id": "20002",
                            "key": "APO-202",
                            "fields": {
                                "summary": "User Management",
                                "status": {"name": "In Progress"}
                            }
                        }
                    ]
                },
                {
                    "id": "proj-2",
                    "name": "Project Artemis",
                    "key": "ART",
                    "epics": [
                        {
                            "id": "20003",
                            "key": "ART-301",
                            "fields": {
                                "summary": "Mobile App Development",
                                "status": {"name": "To Do"}
                            }
                        }
                    ]
                }
            ]

        # 1. Fetch all projects
        url_projects = f"{self.base_url}/rest/api/3/project"
        try:
            resp_projects = requests.get(url_projects, headers=self._get_headers(), auth=self._get_auth())
            resp_projects.raise_for_status()
            projects_data = resp_projects.json()
            
            result = []
            for p in projects_data:
                project_key = p["key"]
                # 2. For each project, fetch Epics (excluding Done status)
                # Filter to show only: To Do, In Progress, In Review
                jql = f'project = "{project_key}" AND issuetype = Epic AND statusCategory != Done ORDER BY created DESC'
                url_search = f"{self.base_url}/rest/api/3/search/jql"
                
                payload = {
                    "jql": jql,
                    "maxResults": 100,
                    "fields": ["summary", "status"]
                }
                
                resp_search = requests.post(url_search, headers=self._get_headers(), auth=self._get_auth(), data=json.dumps(payload))
                if resp_search.ok:
                    epics = resp_search.json().get("issues", [])
                    if epics:
                        result.append({
                            "id": p["id"],
                            "name": p["name"],
                            "key": p.get("key"),
                            "epics": epics
                        })
            
            return result
        except Exception as e:
            error(f"Failed to fetch JIRA structure: {e}")
            return []

    def get_todo_stories(self, user_email: str = None) -> List[Dict[str, Any]]:
        """
        Backward compatibility wrapper or simplified story fetch.
        """
        # For now, let's keep the old logic or redirect to the new structure if needed.
        # But since the UI will now call /structure, this might be less used.
        return self.get_jira_structure()

    def get_story_details(self, story_id: str) -> Dict[str, Any]:
        """
        Get story description and subtasks.
        """
        if self.mock_mode:
            return {
                "id": story_id,
                "key": "PROJ-101",
                "fields": {
                    "summary": "Implement Login Page",
                    "description": "Create a responsive login page with email and password fields.",
                    "subtasks": [
                        {
                            "id": "10011",
                            "key": "PROJ-101-1",
                            "fields": {
                                "summary": "Create UI Component",
                                "description": "Build the React component for login form.",
                                "status": {"name": "To Do"}
                            }
                        },
                        {
                            "id": "10012",
                            "key": "PROJ-101-2",
                            "fields": {
                                "summary": "Implement Form Validation",
                                "description": "Add client-side validation for email format and password length.",
                                "status": {"name": "To Do"}
                            }
                        }
                    ]
                }
            }

        url = f"{self.base_url}/rest/api/3/issue/{story_id}"
        try:
            response = requests.get(
                url,
                headers=self._get_headers(),
                auth=self._get_auth(),
                params={"fields": "summary,description,subtasks,status,parent"}
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            error(f"Failed to fetch JIRA story details: {e}")
            return {}
    
    def get_issue_attachments(self, issue_id: str) -> List[Dict[str, Any]]:
        """
        Get all attachments for a JIRA issue.
        
        Returns:
            List of attachment metadata dicts with keys: id, filename, mimeType, size, content (url)
        """
        if self.mock_mode:
            info(f"[Mock] Getting attachments for issue {issue_id}")
            return []
        
        url = f"{self.base_url}/rest/api/3/issue/{issue_id}"
        try:
            response = requests.get(
                url,
                headers=self._get_headers(),
                auth=self._get_auth(),
                params={"fields": "attachment"}
            )
            response.raise_for_status()
            issue_data = response.json()
            attachments = issue_data.get("fields", {}).get("attachment", [])
            
            info(f"Found {len(attachments)} attachment(s) for issue {issue_id}")
            return attachments
        except Exception as e:
            error(f"Failed to fetch attachments for issue {issue_id}: {e}")
            return []
    
    def identify_attachment_type(self, attachment: Dict[str, Any]) -> str:
        """
        Identify the type of attachment based on MIME type and filename.
        
        Args:
            attachment: Attachment metadata dict with 'mimeType' and 'filename' keys
        
        Returns:
            One of: 'text', 'pdf', 'image', 'other'
        """
        mime_type = attachment.get("mimeType", "").lower()
        filename = attachment.get("filename", "").lower()
        
        # Check for text files
        text_mimes = [
            "text/plain", "text/markdown", "text/csv", "text/html",
            "application/json", "application/xml", "text/xml"
        ]
        text_extensions = [".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log"]
        
        if mime_type in text_mimes or any(filename.endswith(ext) for ext in text_extensions):
            return "text"
        
        # Check for PDF files
        if mime_type == "application/pdf" or filename.endswith(".pdf"):
            return "pdf"
        
        # Check for image files
        image_mimes = ["image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp"]
        image_extensions = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]
        
        if mime_type in image_mimes or any(filename.endswith(ext) for ext in image_extensions):
            return "image"
        
        return "other"
    
    def download_attachment_content(self, attachment: Dict[str, Any]) -> Optional[bytes]:
        """
        Download the content of a JIRA attachment.
        
        Args:
            attachment: Attachment metadata dict with 'content' key (URL to download)
        
        Returns:
            Attachment content as bytes, or None if download fails
        """
        if self.mock_mode:
            info(f"[Mock] Downloading attachment: {attachment.get('filename', 'unknown')}")
            return b"Mock attachment content"
        
        content_url = attachment.get("content")
        if not content_url:
            error("Attachment has no content URL")
            return None
        
        try:
            response = requests.get(
                content_url,
                auth=self._get_auth(),
                timeout=60  # Longer timeout for large files
            )
            response.raise_for_status()
            
            filename = attachment.get("filename", "unknown")
            size = len(response.content)
            info(f"Downloaded attachment '{filename}' ({size} bytes)")
            
            return response.content
        except Exception as e:
            error(f"Failed to download attachment: {e}")
            return None

    def update_issue_status(self, issue_id: str, status_name: str) -> bool:
        """
        Update status of an issue (story or subtask).
        Note: Transition IDs depend on the workflow. This is a simplified implementation.
        """
        if self.mock_mode:
            info(f"[Mock] Updated issue {issue_id} to status: {status_name}")
            return True

        # First, find the transition ID for the target status
        transitions_url = f"{self.base_url}/rest/api/3/issue/{issue_id}/transitions"
        try:
            response = requests.get(transitions_url, headers=self._get_headers(), auth=self._get_auth())
            response.raise_for_status()
            transitions = response.json().get("transitions", [])
            
            transition_id = next((t["id"] for t in transitions if t["to"]["name"].lower() == status_name.lower()), None)
            
            if not transition_id:
                # Fallback: Try strict matching or handle 'In Review' mapping
                # Mapping common names: "In Progress" -> 31, "Done" -> 41, etc.
                # Here we just log error if not found
                error(f"Transition to '{status_name}' not found for issue {issue_id}")
                return False

            # Perform transition
            payload = {"transition": {"id": transition_id}}
            resp = requests.post(
                transitions_url, 
                headers=self._get_headers(), 
                auth=self._get_auth(),
                data=json.dumps(payload)
            )
            resp.raise_for_status()
            return True

        except Exception as e:
            error(f"Failed to update issue status: {e}")
            return False
    
    def create_story(self, summary: str, description: str = "", project_key: str = None, epic_key: str = None) -> Dict[str, Any]:
        """
        Create a new JIRA story, optionally linked to an Epic.
        """
        if self.mock_mode:
            mock_story = {
                "id": "mock-story-id",
                "key": f"{project_key or 'MOCK'}-1",
                "fields": {
                    "summary": summary,
                    "description": description
                }
            }
            info(f"[Mock] Created story: {summary} under Epic {epic_key}")
            return mock_story

        # Use provided project_key or fallback to env
        final_project_key = project_key or os.getenv("JIRA_PROJECT_KEY", "PROJ")
        
        description_content = self._format_description_to_adf(description)
        url = f"{self.base_url}/rest/api/3/issue"
        
        fields = {
            "project": {
                "key": final_project_key
            },
            "summary": summary,
            "description": description_content,
            "issuetype": {
                "name": "Story"
            }
        }

        # Link to Epic if provided
        if epic_key:
            # For JIRA Cloud, the field is usually 'parent'
            fields["parent"] = {"key": epic_key}
        
        payload = {"fields": fields}
        
        try:
            response = requests.post(
                url,
                headers=self._get_headers(),
                auth=self._get_auth(),
                data=json.dumps(payload),
                timeout=30
            )
            if not response.ok:
                error(f"JIRA Error Body: {response.text}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            error(f"Failed to create story: {e}")
            return {}

    def update_story_description(self, issue_id: str, description: str) -> bool:
        """
        Update the description of a JIRA issue (story or epic).
        
        Args:
            issue_id: Issue ID or key
            description: New description text
        
        Returns:
            True if successful, False otherwise
        """
        if self.mock_mode:
            info(f"[Mock] Updated description for {issue_id}")
            return True
        
        url = f"{self.base_url}/rest/api/3/issue/{issue_id}"
        
        # Convert description to ADF format
        description_content = self._format_description_to_adf(description)
        
        payload = {
            "fields": {
                "description": description_content
            }
        }
        
        try:
            response = requests.put(
                url,
                headers=self._get_headers(),
                auth=self._get_auth(),
                data=json.dumps(payload),
                timeout=30
            )
            response.raise_for_status()
            info(f"Successfully updated description for {issue_id}")
            return True
        except Exception as e:
            error(f"Failed to update story description: {e}")
            return False
    
    def add_attachment(self, issue_id: str, filename: str, content: bytes, mime_type: str = None) -> bool:
        """
        Add an attachment to a JIRA issue.
        """
        if self.mock_mode:
            info(f"[Mock] Added attachment {filename} to {issue_id}")
            return True

        url = f"{self.base_url}/rest/api/3/issue/{issue_id}/attachments"
        headers = {
            "X-Atlassian-Token": "no-check"
        }
        
        files = {
            "file": (filename, content, mime_type or "application/octet-stream")
        }
        
        try:
            response = requests.post(
                url,
                headers=headers,
                auth=self._get_auth(),
                files=files,
                timeout=60
            )
            response.raise_for_status()
            return True
        except Exception as e:
            error(f"Failed to add attachment: {e}")
            return False

    def create_subtask(self, parent_issue_id: str, summary: str, description: str = "") -> Dict[str, Any]:
        """
        Create a subtask under a parent story with retry logic.
        Returns the created subtask data or empty dict on failure.
        """
        if self.mock_mode:
            mock_subtask = {
                "id": f"mock-{parent_issue_id}-subtask",
                "key": f"MOCK-{parent_issue_id}",
                "fields": {
                    "summary": summary,
                    "description": description
                }
            }
            info(f"[Mock] Created subtask: {summary}")
            return mock_subtask
        
        # Retry configuration
        max_retries = 3
        retry_delay = 2  # seconds
        
        for attempt in range(max_retries):
            try:
                # First, get parent issue to extract project key and issue type
                parent_url = f"{self.base_url}/rest/api/3/issue/{parent_issue_id}"
                parent_response = requests.get(
                    parent_url,
                    headers=self._get_headers(),
                    auth=self._get_auth(),
                    timeout=30  # Add timeout to prevent hanging
                )
                parent_response.raise_for_status()
                parent_data = parent_response.json()
                
                project_key = parent_data["fields"]["project"]["key"]
                
                # Format description with bullet points if it contains sentences
                # Split by '. ' to create bullet points for better readability
                description_content = self._format_description_to_adf(description)
                
                # Create subtask
                url = f"{self.base_url}/rest/api/3/issue"
                
                payload = {
                    "fields": {
                        "project": {
                            "key": project_key
                        },
                        "summary": summary,
                        "description": description_content,
                        "issuetype": {
                            "name": "Subtask"
                        },
                        "parent": {
                            "key": parent_data["key"]
                        }
                    }
                }
                
                response = requests.post(
                    url,
                    headers=self._get_headers(),
                    auth=self._get_auth(),
                    data=json.dumps(payload),
                    timeout=30  # Add timeout to prevent hanging
                )
                response.raise_for_status()
                created_issue = response.json()
                info(f"Created subtask {created_issue.get('key')}: {summary}")
                return created_issue
                
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                # Network-related errors that are worth retrying
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)  # Exponential backoff
                    warning(f"Network error creating subtask (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    error(f"Failed to create subtask after {max_retries} attempts: {e}")
                    return {}
            except Exception as e:
                # Other errors (e.g., authentication, validation) - don't retry
                error(f"Failed to create subtask: {e}")
                return {}
        
        # Should not reach here, but just in case
        return {}
    
    def add_comment(self, issue_id: str, comment_content) -> bool:
        """
        Add a comment to a JIRA issue with timeout to prevent hanging.
        """
        if self.mock_mode:
            preview = str(comment_content)[:100] if isinstance(comment_content, str) else "ADF document"
            info(f"[Mock] Added comment to {issue_id}: {preview}...")
            return True
        
        url = f"{self.base_url}/rest/api/3/issue/{issue_id}/comment"
        
        try:
            if isinstance(comment_content, dict):
                payload = {"body": comment_content}
            else:
                payload = {
                    "body": {
                        "type": "doc", "version": 1,
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": str(comment_content)}]}]
                    }
                }
            
            # CRITICAL: Added timeout of 30s to prevent backend hanging if JIRA is slow
            response = requests.post(
                url,
                headers=self._get_headers(),
                auth=self._get_auth(),
                data=json.dumps(payload),
                timeout=30 
            )
            
            if response.status_code == 201:
                info(f"Successfully added comment to {issue_id}")
                return True
            else:
                error(f"Failed to add comment: {response.status_code} - {response.text[:500]}")
                return False
        except requests.exceptions.Timeout:
            error(f"JIRA add_comment timed out for {issue_id}")
            return False
        except Exception as e:
            error(f"Error adding comment to {issue_id}: {e}")
            return False
    
    def _format_description_to_adf(self, text: str) -> Dict[str, Any]:
        """
        Convert simple markdown text to JIRA ADF (Atlassian Document Format).
        Handles headings, bold text, and bullet lists.
        """
        if not text:
            return {"type": "doc", "version": 1, "content": []}

        content = []
        lines = text.split('\n')
        
        in_list = False
        list_items = []

        def flush_list():
            nonlocal in_list, list_items
            if in_list and list_items:
                content.append({
                    "type": "bulletList",
                    "content": list_items
                })
                list_items = []
                in_list = False

        for line in lines:
            line = line.strip()
            if not line:
                flush_list()
                continue

            # Headings
            if line.startswith('#'):
                flush_list()
                level = len(line.split()[0])
                clean_text = line.lstrip('#').strip()
                content.append({
                    "type": "heading",
                    "attrs": {"level": min(level, 6)},
                    "content": [{"type": "text", "text": clean_text}]
                })
            # Bullet Lists
            elif line.startswith(('* ', '- ', '• ')):
                in_list = True
                clean_text = line[2:].strip()
                list_items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": self._parse_inline_adf(clean_text)}]
                })
            # Regular Paragraphs
            else:
                flush_list()
                content.append({
                    "type": "paragraph",
                    "content": self._parse_inline_adf(line)
                })

        flush_list()
        return {"type": "doc", "version": 1, "content": content}

    def _parse_inline_adf(self, text: str) -> List[Dict[str, Any]]:
        """
        Parse bold and simple inline markdown into ADF text nodes.
        """
        # Simple regex for bold **text**
        parts = re.split(r'(\*\*.*?\*\*)', text)
        nodes = []
        for p in parts:
            if p.startswith('**') and p.endswith('**'):
                nodes.append({
                    "type": "text",
                    "text": p[2:-2],
                    "marks": [{"type": "strong"}]
                })
            elif p:
                nodes.append({"type": "text", "text": p})
        return nodes if nodes else [{"type": "text", "text": text}]
