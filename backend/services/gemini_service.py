import os
import re
import asyncio
import base64
import time
from google import genai
from google.genai import types
from typing import List, Dict, Any, Optional
from log_config import logger, error

class GeminiService:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.client = None
        self.last_api_call_time = 0
        self.min_call_interval = 1.0  # Minimum 1 second between API calls
        
        # Configure model via environment variable (default: gemini-3-flash-preview for better quality)
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
        self.model = self.model_name # Keep for backward compatibility
        
        if self.api_key:
            try:
                # Initialize the client from google-genai SDK
                self.client = genai.Client(api_key=self.api_key)
                logger.info(f"GeminiService initialized successfully with model: {self.model_name}")
            except Exception as e:
                error(f"Failed to initialize GeminiService: {e}", "GeminiService")
        else:
            logger.warning("GEMINI_API_KEY not found. GeminiService will mock responses.")
    
    async def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        current_time = time.time()
        time_since_last_call = current_time - self.last_api_call_time
        
        if time_since_last_call < self.min_call_interval:
            sleep_time = self.min_call_interval - time_since_last_call
            logger.info(f"Rate limiting: waiting {sleep_time:.2f}s before next API call")
            await asyncio.sleep(sleep_time)
        
        self.last_api_call_time = time.time()
    
    async def _call_with_retry(self, method_name, *args, max_retries=3, timeout=300.0, **kwargs):
        """
        Call an AI API method asynchronously with exponential backoff retry logic.
        
        Args:
            method_name: The name of the method to call (e.g., 'models.generate_content')
            max_retries: Maximum number of retry attempts (default: 3)
            timeout: Timeout for the API call in seconds (default: 300.0)
            *args, **kwargs: Arguments to pass to the function
        """
        last_exception = None
        
        # Split method name to traverse the aio client (e.g., 'models.generate_content')
        parts = method_name.split('.')
        
        for attempt in range(max_retries):
            try:
                # Rate limit before each call
                await self._rate_limit()
                
                # Access the async version of the method via client.aio
                func = self.client.aio
                for part in parts:
                    func = getattr(func, part)
                
                # Make the API call with a timeout to prevent indefinite hangs
                # Native async call allows proper cancellation
                response = await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=timeout
                )
                return response
                
            except asyncio.TimeoutError:
                # Handle timeout - retry with backoff
                wait_time = 2 ** attempt
                if attempt < max_retries - 1:
                    logger.warning(
                        f"⚠️  AI API call timed out (300s) - "
                        f"Attempt {attempt + 1}/{max_retries}. Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    error_msg = (
                        f"🚫 AI API TIMEOUT: The AI service did not respond within 300 seconds. "
                        f"All {max_retries} retry attempts have been exhausted. "
                        f"Please try again later."
                    )
                    logger.error(error_msg)
                    error(error_msg, "GeminiService")
                    raise Exception(error_msg)
                    
            except Exception as e:
                last_exception = e
                error_str = str(e)
                
                # Check if it's a 429 RESOURCE_EXHAUSTED error
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    # Calculate exponential backoff: 2^attempt seconds (1s, 2s, 4s)
                    wait_time = 2 ** attempt
                    
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"⚠️  AI model temporarily unavailable (429 error) - "
                            f"Attempt {attempt + 1}/{max_retries}. Retrying in {wait_time}s..."
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        # All retries exhausted - provide clear user-facing message
                        error_msg = (
                            f"🚫 REMOTE AI MODEL UNAVAILABLE: The AI service is currently overloaded or rate-limited (429 error). "
                            f"All {max_retries} retry attempts have been exhausted. "
                            f"Please wait a few minutes and try again later. "
                            f"This is a temporary issue with the external AI provider."
                        )
                        logger.error(error_msg)
                        error(error_msg, "GeminiService")
                        # Raise with user-friendly message
                        raise Exception(error_msg) from e
                else:
                    # For non-429 errors, raise immediately
                    logger.error(f"API call failed with non-retryable error: {error_str}")
                    raise
        
        # If we get here, all retries were exhausted
        raise last_exception

    async def generate_work_plan(self, story_description: str, subtasks: List[Dict[str, Any]] = None) -> str:
        """
        Generate a structured work plan based on story description.
        Returns a work plan with SUBTASK markers that can be parsed to create JIRA subtasks.
        """
        if not self.client:
            return """# Work Plan

SUBTASK: Setup Project Structure
Desc: Initialize project directories and configuration files
---

SUBTASK: Implement Core Features
Desc: Build the main functionality according to requirements
---

SUBTASK: Add Tests
Desc: Create comprehensive test coverage
---"""
        
        prompt = f"""
        You are an expert Autonomous Developer Agent.
        
        Your task is to create a detailed technical work plan for implementing the following requirements.
        
        REQUIREMENTS:
        {story_description}
        
        Please provide a structured work plan by breaking down the implementation into specific subtasks.
        
        IMPORTANT: Add a sequence number to each SUBTASK summary (e.g., "1. Setup...", "2. Create...").
        
        Format your response with SUBTASK markers like this:
        
        SUBTASK: 1. [Short summary title]
        Desc: [Detailed description of what needs to be implemented]
        ---
        
        SUBTASK: 2. [Next task title]
        Desc: [Detailed description]
        ---
        
        SUBTASK: [Next task title]
        Desc: [Detailed description]
        ---
        
        Example:
        
        SUBTASK: Create package.json
        Desc: Initialize package.json with React 18.2 and core dependencies only
        ---
        
        SUBTASK: Create Next.js Config
        Desc: Create next.config.js with standalone output and API rewrites
        ---
        
        SUBTASK: Create Login Component
        Desc: Build Login.tsx component with form inputs and validation
        ---
        
        🚨 CRITICAL: TOKEN LIMIT - WITH 64K TOKENS - GROUP RELATED FUNCTIONALITY:
        
        ⚠️ Code generation has 64K token output limit - this allows for comprehensive generation
        ⚠️ Each subtask should generate 5-10 files MAX to avoid truncation
        ⚠️ If you try to generate 50+ files in one subtask, it might be truncated
        ⚠️ Truncated code = incomplete files = broken application
        
        GUIDELINES FOR FOCUSED SUBTASKS:
        1. Create subtasks that group related functionality (5-15 files per subtask)
        2. Grouping related files together is ENCOURAGED (e.g., model + service + routes)
        3. Generate complete features in one go when possible
        5. Split extremely large features (50+ files) into multiple subtasks
        7. Split extremely large features (50+ files) into multiple subtasks
        7. Ensure all dependencies are created before they are imported
        
        🚨 MANDATORY SUBTASK ORDERING - PREVENT ALL IMPORT ERRORS:
        
        🔴 CRITICAL: You MUST generate subtasks in this EXACT order to prevent ImportError failures!
        
        Backend Python Projects - MANDATORY ORDER:
        1. SUBTASK 1: "Setup Backend Dependencies" → requirements.txt, .env.example, backend/__init__.py
        2. SUBTASK 2: "Create Data Models" → backend/models/*.py (ALL model files)
        3. SUBTASK 3: "Setup Authentication Utilities" → backend/auth/*.py (auth_utils.py, jwt_utils.py, dependencies.py)
        4. SUBTASK 4: "Setup Database Client" → backend/database/*.py (firestore_client.py with lazy initialization)
        5. SUBTASK 5: "Create Service Layer" → backend/services/*.py (ALL service files)
        6. SUBTASK 6: "Create API Routes" → backend/routes/*.py (ALL route files)
        7. SUBTASK 7: "Create Main Application" → backend/main.py
        8. SUBTASK 8: "Add Tests" → backend/tests/**/*.py (unit & integration tests)
        
        Frontend TypeScript/React Projects - MANDATORY ORDER:
        1. SUBTASK 1: "Setup Frontend Configuration" → package.json (with ALL deps!), next.config.js, tsconfig.json, tailwind.config.ts, postcss.config.js, public/.gitkeep
        2. SUBTASK 2: "Create Type Definitions" → frontend/src/types/*.ts (ALL type files with ALL interfaces/types/enums)
        3. SUBTASK 3: "Create API Client Layer" → frontend/src/api/*.ts (ALL API functions)
        4. SUBTASK 4: "Create State Management" → frontend/src/store/*.ts (Zustand stores)
        5. SUBTASK 5: "Create Reusable Components" → frontend/src/components/**/*.tsx
        6. SUBTASK 6: "Create Page Components" → frontend/src/app/**/*.tsx OR frontend/src/pages/*.tsx
        7. SUBTASK 7: "Create Root Layout & Globals" → frontend/src/app/layout.tsx, page.tsx, globals.css
        8. SUBTASK 8: "Add Tests" → frontend/__tests__/**/*.test.tsx
        
        🚨 WHY THIS ORDER IS MANDATORY:
        - Models MUST be created BEFORE services (services import models)
        - Services MUST be created BEFORE routes (routes import services)
        - Types MUST be created BEFORE API client (API client imports types)
        - API client MUST be created BEFORE pages (pages import API functions)
        - If you violate this order, deployment will FAIL with ImportError!
        
        ⚠️ If a file imports from another file, BOTH files must be in the SAME subtask OR
        ⚠️ The imported file must be created in an EARLIER subtask
        ⚠️ NEVER create a file that imports from a file that doesn't exist yet
        
        🎯 GOLDEN RULE FOR SUBTASK ORDER:
        "Dependencies BEFORE Dependents. Always."
        - If file A imports from file B, generate B first (or in same subtask)
        - Models → Services → Routes for backend
        - Types → API → Pages for frontend
        
        EXAMPLE - CORRECT DEPENDENCY ORDER:
        ✅ Subtask 1: "Create JWT utilities" → auth/jwt_utils.py
        ✅ Subtask 2: "Create Auth Service" → auth/auth_service.py (can import jwt_utils)
        
        EXAMPLE - WRONG (causes import errors):
        ❌ Subtask 1: "Create Auth Service" → auth/auth_service.py (imports jwt_utils)
        ❌ Subtask 2: "Create JWT utilities" → auth/jwt_utils.py (created AFTER it's imported)
        
        EXAMPLE - CORRECT GROUPING:
        ✅ Subtask: "Create Auth utilities and service" → jwt_utils.py + auth_service.py (both together)
        
        🚨 MANDATORY STARTUP FILES FOR NEXT.JS PROJECTS:
        ⚠️ If this is a Next.js frontend project, you MUST include these as separate subtasks:
        1. "Setup Frontend Base" → package.json (MUST include all dependencies like axios, zod!), next.config.js, tsconfig.json, tailwind.config.ts, postcss.config.js
        2. "Create root layout" → app/layout.tsx as root layout with metadata
        3. "Create root page" → app/page.tsx as homepage
        4. "Create global styles" → app/globals.css with Tailwind directives
        5. "Create middleware (if needed)" → middleware.ts for auth/routing
        
        🚨 DEPENDENCY GOLDEN RULE:
        ⚠️ NEVER import 'axios', 'zod', 'react-hook-form', or '@hookform/resolvers' unless they are in package.json!
        ⚠️ ALWAYS include these dependencies in the 'package.json' file you generate!
        
        WITHOUT THESE FILES, THE APP WILL SHOW 404 ERROR OR FAIL TO START!
        
        EXAMPLE TASK GRANULARITY WITH NEW 64K TOKEN LIMIT:
        ✅ "Setup Next.js Frontend Base" → package.json (MUST include axios, zod, react-hook-form, @hookform/resolvers!), next.config.js, app/layout.tsx, app/page.tsx, app/globals.css, tsconfig.json, tailwind.config.ts, postcss.config.js (8 files OK!)
        ✅ "Create Authentication Pages" → app/login/page.tsx, app/register/page.tsx, app/reset-password/page.tsx (examples - adjust file count as needed)
        ✅ "Create Authentication Components" → components/LoginForm.tsx, components/RegisterForm.tsx, components/PasswordResetForm.tsx (examples - adjust file count as needed)
    ✅ "Create Backend Auth System" → auth_routes.py, auth_service.py, jwt_utils.py, auth_utils.py, dependencies.py (5 files OK!)
    ✅ "Create Ticket System Backend" → ticket_routes.py, ticket_service.py, ticket_models.py (examples - adjust file count as needed)
    ✅ "Setup Frontend Base" → package.json (MUST include all dependencies like axios, zod!), next.config.js, tsconfig.json, tailwind.config.ts, postcss.config.js (5 files OK!)
    ✅ "Create Ticket Components" → components/TicketList.tsx, components/TicketItem.tsx, components/TicketForm.tsx, components/TicketDetail.tsx (4 files OK!)
        
        🎯 GOLDEN RULE: Group related functionality together - generate 5-15 files per subtask!
        
        REMEMBER: With 64K tokens, you can generate complete feature modules, but split larger tasks!
        """
        
        try:
            response = await self._call_with_retry(
                'models.generate_content',
                model=self.model,
                contents=prompt,
                config={
                    'temperature': 0.7,
                    'max_output_tokens': 65536
                }
            )
            return response.text
        except Exception as e:
            error(f"Gemini generation failed: {e}", "GeminiService")
            if "REMOTE AI MODEL UNAVAILABLE" in str(e) or "AI API TIMEOUT" in str(e):
                raise
            return "Error generating work plan due to AI service failure."
    
    def parse_work_plan(self, work_plan: str) -> List[Dict[str, str]]:
        """
        Parse work plan to extract subtasks.
        
        Returns:
            List of dicts with 'summary' and 'description' keys
        """
        subtasks = []
        
        # Split by SUBTASK markers
        parts = work_plan.split('SUBTASK:')
        
        for part in parts[1:]:  # Skip first part before first SUBTASK
            if 'Desc:' not in part:
                continue
            
            lines = part.split('\n')
            
            # Extract summary (first line)
            summary = lines[0].strip()
            # Strip leading numbering (e.g. "1. " or "1: ") generated by AI
            summary = re.sub(r'^\d+[\.:\s]+', '', summary).strip()
            
            # Extract description (after Desc:)
            desc_start = part.find('Desc:')
            if desc_start == -1:
                continue
            
            remaining = part[desc_start + 5:]  # Skip "Desc:"
            
            # Find end marker ---
            desc_end = remaining.find('---')
            if desc_end != -1:
                description = remaining[:desc_end].strip()
            else:
                description = remaining.strip()
            
            if summary and description:
                subtasks.append({
                    'summary': summary,
                    'description': description
                })
                logger.info(f"Parsed subtask: {summary}")
        
        return subtasks

    async def generate_prd(self, prompt: str, attachments: List[Dict[str, Any]]) -> str:
        """
        Generate a PRD based on user prompt and attachments.
        """
        system_prompt = """
        You are an expert Product Manager. Your task is to generate a comprehensive Product Requirement Document (PRD) 
        based on the user's ideas and any attached files (images of UI designs or PDF docs).
        
        The PRD MUST include:
        1. Product Summary
        2. Functional Requirements
        3. UI/UX Requirements (referencing attached designs if provided)
        4. Technical Constraints
        
        🚫 DO NOT include a "User Stories" section as it is redundant.
        
        Format the output clearly in markdown.
        """
        
        content_parts = [system_prompt, f"User Product Thought: {prompt}"]
        
        for att in attachments:
            if att["type"] in ["image", "pdf"]:
                content_parts.append(f"\n📎 ATTACHED {att['type'].upper()}: {att['filename']}")
                content_bytes = att["content"]
                # Decode base64 if it's a string
                if isinstance(content_bytes, str):
                    try:
                        content_bytes = base64.b64decode(content_bytes)
                    except Exception as e:
                        error(f"Failed to decode base64 attachment {att['filename']}: {e}", "GeminiService")
                        continue

                content_parts.append(types.Part.from_bytes(
                    data=content_bytes,
                    mime_type=att["mime_type"]
                ))
            elif att["type"] == "text":
                content_parts.append(f"\n📎 ATTACHED TEXT: {att['filename']}\n{att['content']}")
                
        try:
            response = await self._call_with_retry(
                'models.generate_content',
                model=self.model,
                contents=content_parts,
                config={
                    'temperature': 0.7,
                    'max_output_tokens': 65536
                }
            )
            return response.text
        except Exception as e:
            error(f"PRD generation failed: {e}", "GeminiService")
            if "REMOTE AI MODEL UNAVAILABLE" in str(e) or "AI API TIMEOUT" in str(e):
                raise
            return f"Error generating PRD: {str(e)}"

    async def generate_code(
        self,
        task_description: str,
        context: str = "",
        story_context: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        repo_files: Optional[List[str]] = None,
        temperature: float = 0.7,
        timeout: float = 300.0,
        max_output_tokens: int = 65536
    ) -> str:
        """
        Generate code for a specific task with file path information.
        Returns a structured response with file paths and code content.
        """
        # CRITICAL DEBUG: Log entry into generate_code
        logger.info(f"🚀 [GeminiService] Entering generate_code for task: {task_description[:50]}...")
        
        if not self.client:
            return f"""FILE_PATH: src/generated_code.py
---
# Mock Code Generation for: {task_description[:30]}...
# This is a placeholder since GEMINI_API_KEY is not set.

def implementation():
    # TODO: Implement {task_description}
    pass
"""
        
        # Build prompt (using simpler version for brevity in tool call)
        has_yaml_config = "Technical Configuration:" in context or "YAML Config" in context
        is_auto_fix = "STATIC ANALYSIS FIX REQUIRED" in context
        
        prompt = f"""
        You are an expert Autonomous Developer Agent. Generate code for the following task.
        
        TASK: {task_description}
        CONTEXT: {context}
        PRD: {story_context}
        
        MANDATORY RESPONSE FORMAT:
        For each file you generate, use the following EXACT structure:
        
        FILE_PATH: [path/to/file.ext]
        ---
        [file content here]
        ---
        
        Example:
        FILE_PATH: src/app.py
        ---
        print("hello world")
        ---
        
        🚨 CRITICAL: Use the exact markers above. Do NOT use markdown code blocks like ```python.
        🚨 CRITICAL: Ensure every file is wrapped in '---' separators.
        """
        
        # Log generation attempt
        logger.info(f"📝 [GeminiService] Generating code for task: {task_description[:100]}...")
        
        # Multimodal content
        content_parts = [prompt]
        if attachments:
            for att in attachments:
                if att["type"] in ["image", "pdf"]:
                    content_parts.append(types.Part.from_bytes(data=att["content"], mime_type=att["mime_type"]))
        
        try:
            response = await self._call_with_retry(
                'models.generate_content',
                model=self.model,
                contents=content_parts,
                config={'max_output_tokens': max_output_tokens, 'temperature': temperature},
                timeout=timeout
            )
            
            finish_reason = 'STOP'
            if hasattr(response, 'candidates') and response.candidates:
                finish_reason = str(response.candidates[0].finish_reason)
            
            # CRITICAL DEBUG: Log AI response details
            logger.info(f"🤖 [GeminiService] AI Response received for task '{task_description[:50]}...'")
            logger.info(f"   Length: {len(response.text)} characters")
            logger.info(f"   Finish Reason: {finish_reason}")
            
            if len(response.text) < 100:
                logger.warning(f"⚠️ [GeminiService] AI Response is unusually short: {response.text}")
            
            return (response.text, finish_reason)
        except Exception as e:
            error(f"Gemini code generation failed: {e}", "GeminiService")
            if "REMOTE AI MODEL UNAVAILABLE" in str(e) or "AI API TIMEOUT" in str(e):
                raise
            return (f"FILE_PATH: error.txt\n---\n# Error: {str(e)}\n---", 'ERROR')
    
    def parse_generated_code(self, response: str) -> List[Dict[str, str]]:
        """Extract file paths and content."""
        # CRITICAL DEBUG: Log raw response for troubleshooting
        logger.info(f"🔍 [GeminiService] Parsing code generation response (length: {len(response)})")
        if not response or len(response) < 10:
            logger.warning("⚠️ [GeminiService] AI response is empty or too short!")
            return []
            
        # Log a small snippet of the response to see the format
        snippet = response[:200].replace('\n', '\\n')
        logger.info(f"🔍 [GeminiService] Response snippet: {snippet}...")

        files = []
        # Support both 'FILE_PATH:' and 'FILE:' markers just in case
        markers = ['FILE_PATH:', 'FILE:']
        
        # Check which marker exists
        active_marker = None
        for m in markers:
            if m in response:
                active_marker = m
                break
                
        if not active_marker:
            logger.warning(f"❌ [GeminiService] No file markers ('FILE_PATH:' or 'FILE:') found in response!")
            # Last resort: look for any markdown blocks
            if "```" in response:
                logger.info("🔍 [GeminiService] Attempting to extract files from markdown blocks...")
                # Simple extraction for single-file responses without markers
                code_blocks = re.findall(r'```(?:\w+)?\n(.*?)\n```', response, re.DOTALL)
                if code_blocks:
                    logger.info(f"✅ [GeminiService] Found {len(code_blocks)} code blocks via markdown fallback")
                    return [{'file_path': 'generated_code.py', 'content': block} for block in code_blocks]
            return []

        parts = response.split(active_marker)
        logger.info(f"🔍 [GeminiService] Split response into {len(parts)} parts using marker '{active_marker}'")
        
        for part in parts[1:]:
            if '---' not in part: 
                logger.warning("⚠️ [GeminiService] Part missing '---' separator, skipping")
                continue
            lines = part.split('\n')
            file_path = lines[0].strip()
            
            # Clean up path if it contains backticks or other markdown
            file_path = re.sub(r'[`*]', '', file_path).strip()
            
            content_start = part.find('---')
            remaining = part[content_start + 3:]
            
            # Handle both '---' end marker and end of string
            content_end = remaining.find('---')
            content = remaining[:content_end].strip() if content_end != -1 else remaining.strip()
            
            # Remove leading/trailing code block markers if AI included them inside the dashes
            content = re.sub(r'^```(?:\w+)?\n', '', content)
            content = re.sub(r'\n```$', '', content)
            
            if file_path and content:
                files.append({'file_path': file_path, 'content': content})
                logger.info(f"✅ [GeminiService] Successfully parsed file: {file_path} ({len(content)} chars)")
        
        if not files:
            logger.error("❌ [GeminiService] Failed to extract any valid files from response despite finding markers!")
            
        return files
