"""
Unified AI Service supporting multiple vendors (Gemini, Anthropic, OpenAI)
Uses native async SDKs per vendor for proper multimodal support.
Vendor selection via AI_VENDOR environment variable (default: "gemini")
"""

import os
import re
import asyncio
import base64
import time
from typing import List, Dict, Any, Optional
from log_config import logger, error


class AIService:
    """
    Unified AI Service supporting Gemini, Anthropic, OpenAI, and other vendors.
    Uses native async SDKs for each vendor to handle multimodal content properly.
    """
    
    # Common AI instructions reused across all methods and vendors
    REPO_STRUCTURE_INSTRUCTION = """
    🚨 CRITICAL REPOSITORY STRUCTURE - READ THIS FIRST! 🚨
    
    ==================================================================================
    SEPARATE GIT REPOSITORIES - DO NOT USE "backend/" OR "frontend/" PREFIXES!
    ==================================================================================
    
    ⚠️ FRONTEND and BACKEND are in SEPARATE git repositories!
    ⚠️ DO NOT include "backend/" or "frontend/" prefixes in your file paths!
    
    ✅ CORRECT file paths (separate repos):
       - Backend repo: models/user.py, auth/jwt_utils.py, services/auth_service.py, routes/auth_routes.py, main.py
       - Frontend repo: src/types/user.ts, src/api/auth.ts, src/components/Login.tsx, app/page.tsx
    
    ❌ WRONG file paths (will cause errors):
       - backend/models/user.py ← WRONG! Backend is its own repo root
       - frontend/src/types/user.ts ← WRONG! Frontend is its own repo root
    
    🎯 REMEMBER: Each repo is standalone - the directory IS the Python/TypeScript root!
    """
    
    SUBTASK_ORDERING_INSTRUCTION = """
    🚨 MANDATORY SUBTASK ORDERING - PREVENT ALL IMPORT ERRORS:
    
    Backend Python Projects - MANDATORY ORDER (NO "backend/" prefix):
    1. "Setup Backend Dependencies" → requirements.txt, .env.example, __init__.py
    2. "Create Data Models" → models/*.py (ALL model files)
    3. "Setup Authentication Utilities" → auth/*.py
    4. "Setup Database Client" → database/*.py
    5. "Create Service Layer" → services/*.py
    6. "Create API Routes" → routes/*.py
    7. "Create Main Application" → main.py
    8. "Add Tests" → tests/**/*.py
    
    Frontend TypeScript/React Projects - MANDATORY ORDER (NO "frontend/" prefix):
    1. "Setup Frontend Configuration" → package.json, next.config.js, tsconfig.json, etc.
    2. "Create Type Definitions" → src/types/*.ts
    3. "Create API Client Layer" → src/api/*.ts
    4. "Create State Management" → src/store/*.ts
    5. "Create Reusable Components" → src/components/**/*.tsx
    6. "Create Page Components" → app/**/*.tsx
    7. "Create Root Layout & Globals" → app/layout.tsx, page.tsx, globals.css
    8. "Add Tests" → __tests__/**/*.test.tsx
    
    🚨 WHY THIS ORDER IS MANDATORY:
    - Models MUST be created BEFORE services (services import models)
    - Services MUST be created BEFORE routes (routes import services)
    - Types MUST be created BEFORE API client (API client imports types)
    - API client MUST be created BEFORE pages (pages import API functions)
    
    🎯 GOLDEN RULE FOR SUBTASK ORDER:
    "Dependencies BEFORE Dependents. Always."
    If file A imports from file B, generate B FIRST (or in same subtask)!
    """
    
    CODE_GENERATION_INSTRUCTION = """
    🚨 CRITICAL: GENERATE ALL DEPENDENT FILES TOGETHER - ZERO TOLERANCE!
    
    ⛔ DEPLOYMENT WILL FAIL if you reference a file that doesn't exist!
    
    **GOLDEN RULE: If file A imports from file B, generate BOTH files in THIS response!**
    
    ❌ NEVER DO THIS (causes ImportError - deployment FAILS):
    Task: "Create auth service"
    Response:
      FILE_PATH: services/auth_service.py
      ---
      from models.user import User  ← WRONG! models/user.py doesn't exist yet!
      ---
    
    ✅ ALWAYS DO THIS (complete, working code):
    Task: "Create auth service"
    Response:
      FILE_PATH: models/user.py  ← Generate dependency FIRST
      ---
      from pydantic import BaseModel
      class User(BaseModel):
          id: str
          email: str
      ---
      
      FILE_PATH: services/auth_service.py  ← Then generate file that uses it
      ---
      from models.user import User  ← NOW this works!
      ---
    
    🔴 CRITICAL EXAMPLES - WHAT YOU MUST DO:
    1. If you generate `routes/auth_routes.py` that imports from `services.auth_service`,
       YOU MUST ALSO generate `services/auth_service.py` in the SAME response!
    2. If you generate `services/ticket_service.py` that imports from `models.ticket`,
       YOU MUST ALSO generate `models/ticket.py` in the SAME response!
    3. If you generate a component that imports from `src/types/user.ts`,
       YOU MUST ALSO generate `src/types/user.ts` in the SAME response!
    4. If you generate code that imports `from database.firestore_client import get_client`,
       YOU MUST ALSO generate `database/firestore_client.py` in the SAME response!
    
    ⚠️ ZERO EXCEPTIONS: Every single file you reference MUST be generated together!
    
    MANDATORY RESPONSE FORMAT:
    For each file you generate, use this EXACT structure:
    
    FILE_PATH: [path/to/file.ext]
    ---
    [file content here]
    ---
    
    Example:
    FILE_PATH: models/user.py
    ---
    from pydantic import BaseModel
    
    class User(BaseModel):
        id: str
        email: str
    ---
    
    🚨 CRITICAL: Use exact markers. Do NOT use markdown code blocks.
    🚨 CRITICAL: Ensure every file is wrapped in '---' separators.
    🚨 CRITICAL: DO NOT add "backend/" or "frontend/" prefixes!
    🚨 CRITICAL: DO NOT use "from backend.X" or "from frontend.X" imports!
    """
    
    PACKAGES_VS_FILES_INSTRUCTION = """
    🚨 CRITICAL: THIRD-PARTY PACKAGES vs LOCAL FILES - KNOW THE DIFFERENCE! 🚨
    
    ⛔ NEVER try to create files for third-party packages - they go in requirements.txt!
    
    **COMMON MISTAKE THAT CAUSES DEPLOYMENT FAILURES:**
    
    ❌ WRONG - Creating files for external packages:
    FILE_PATH: pydantic_settings.py  ← WRONG! This is a PyPI package!
    FILE_PATH: firebase_admin.py  ← WRONG! This is a PyPI package!
    FILE_PATH: google/cloud/firestore.py  ← WRONG! This is a PyPI package!
    
    ✅ CORRECT - Add to requirements.txt:
    FILE_PATH: requirements.txt
    ---
    pydantic-settings==2.0.0
    firebase-admin==6.2.0
    google-cloud-firestore==2.11.0
    ---
    
    🔴 EXTERNAL PACKAGES (add to requirements.txt, DON'T create files):
    - pydantic, pydantic-settings, email-validator
    - fastapi, uvicorn, python-jose, passlib, python-multipart
    - firebase-admin, google-cloud-firestore, google-cloud-storage
    - requests, httpx, aiohttp
    - python-dotenv, PyYAML
    - Any package you import that starts with: google, firebase, pydantic, fastapi, etc.
    
    🟢 LOCAL FILES (create these files):
    - models/user.py, auth/jwt_utils.py, services/auth_service.py
    - database/firestore_client.py, config/settings.py
    - routes/auth_routes.py, main.py
    - Any file you create yourself for YOUR application
    
    💡 GOLDEN RULE:
    - If it's on PyPI (pip install X) → Add to requirements.txt
    - If it's YOUR code → Generate the file
    """
    
    TOKEN_LIMIT_INSTRUCTION = """
    🚨 CRITICAL: TOKEN LIMIT - WITH 64K TOKENS - GROUP RELATED FUNCTIONALITY:
    
    ⚠️ Code generation has large token output limit - allows comprehensive generation
    ⚠️ Each subtask should generate 5-10 files MAX to avoid truncation
    ⚠️ If you try to generate 50+ files in one subtask, it might be truncated
    ⚠️ Truncated code = incomplete files = broken application
    
    GUIDELINES FOR FOCUSED SUBTASKS:
    1. Create subtasks that group related functionality (5-15 files per subtask)
    2. Grouping related files together is ENCOURAGED (e.g., model + service + routes)
    3. Generate complete features in one go when possible
    4. Split extremely large features (50+ files) into multiple subtasks
    5. Ensure all dependencies are created before they are imported
    
    EXAMPLE TASK GRANULARITY:
    ✅ "Setup Next.js Frontend Base" → package.json, next.config.js, app/layout.tsx, app/page.tsx, app/globals.css, tsconfig.json (6 files OK!)
    ✅ "Create Authentication Pages" → app/login/page.tsx, app/register/page.tsx, app/reset-password/page.tsx (3 files OK!)
    ✅ "Create Backend Auth System" → auth_routes.py, auth_service.py, jwt_utils.py, auth_utils.py, dependencies.py (5 files OK!)
    
    🎯 GOLDEN RULE: Group related functionality together - generate 5-15 files per subtask!
    """
    
    def __init__(self):
        self.vendor = os.getenv("AI_VENDOR", "gemini").lower()
        self.api_key = None
        self.client = None
        self.async_client = None
        self.model_name = None
        self.model = None
        self.last_api_call_time = 0
        self.min_call_interval = 0.5
        
        self._init_vendor()
    
    def _init_vendor(self):
        """Initialize vendor-specific configuration and clients."""
        if self.vendor == "anthropic":
            self._init_anthropic()
        elif self.vendor == "openai":
            self._init_openai()
        elif self.vendor == "openrouter":
            self._init_openrouter()
        elif self.vendor == "gemini":
            self._init_gemini()
        else:
            error(f"Unknown AI_VENDOR: {self.vendor}. Supported: gemini, anthropic, openai, openrouter", "AIService")
            logger.warning("Defaulting to gemini")
            self.vendor = "gemini"
            self._init_gemini()

    def _normalize_ai_text(self, value: Any) -> str:
        """Normalize vendor response payload into a safe string."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return str(value)
        except Exception:
            return ""
    
    def _init_gemini(self):
        """Initialize Google Gemini API client."""
        try:
            from google import genai
        except ImportError:
            error("google-genai package not installed. Install with: pip install google-genai", "AIService")
            return
        
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.model = self.model_name
        
        if self.api_key:
            try:
                # Configure using the new google-genai client.
                api_url = os.getenv("GEMINI_API_URL")
                if api_url:
                    # Keep behavior explicit: custom URL support is not wired for google-genai here.
                    logger.warning(
                        "GEMINI_API_URL is set but custom Gemini base URL is not supported in this build; using default Google endpoint."
                    )
                self.client = genai.Client(api_key=self.api_key)
                logger.info(f"AIService (Gemini) initialized with model: {self.model_name}")
            except Exception as e:
                error(f"Failed to initialize Gemini client: {e}", "AIService")
        else:
            logger.warning("GEMINI_API_KEY not found. AIService will mock Gemini responses.")
    
    def _init_anthropic(self):
        """Initialize Anthropic Claude API client."""
        try:
            from anthropic import Anthropic, AsyncAnthropic
            self.Anthropic = Anthropic
            self.AsyncAnthropic = AsyncAnthropic
        except ImportError:
            error("anthropic package not installed. Install with: pip install anthropic", "AIService")
            return
        
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.model_name = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
        self.model = self.model_name
        
        if self.api_key:
            try:
                # Support optional custom API URL
                api_url = os.getenv("ANTHROPIC_API_URL")
                if api_url:
                    self.async_client = self.AsyncAnthropic(api_key=self.api_key, base_url=api_url)
                    logger.info(f"AIService (Anthropic) initialized with custom API URL: {api_url}")
                else:
                    self.async_client = self.AsyncAnthropic(api_key=self.api_key)
                logger.info(f"AIService (Anthropic) initialized with model: {self.model_name}")
            except Exception as e:
                error(f"Failed to initialize Anthropic client: {e}", "AIService")
        else:
            logger.warning("ANTHROPIC_API_KEY not found. AIService will mock Anthropic responses.")
    
    def _init_openai(self):
        """Initialize OpenAI API client."""
        try:
            from openai import AsyncOpenAI
            self.AsyncOpenAI = AsyncOpenAI
        except ImportError:
            error("openai package not installed. Install with: pip install openai", "AIService")
            return
        
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.model = self.model_name
        
        if self.api_key:
            try:
                # Support optional custom API URL
                api_url = os.getenv("OPENAI_API_URL")
                if api_url:
                    self.async_client = self.AsyncOpenAI(api_key=self.api_key, base_url=api_url)
                    logger.info(f"AIService (OpenAI) initialized with custom API URL: {api_url}")
                else:
                    self.async_client = self.AsyncOpenAI(api_key=self.api_key)
                logger.info(f"AIService (OpenAI) initialized with model: {self.model_name}")
            except Exception as e:
                error(f"Failed to initialize OpenAI client: {e}", "AIService")
        else:
            logger.warning("OPENAI_API_KEY not found. AIService will mock OpenAI responses.")
    
    def _init_openrouter(self):
        """Initialize OpenRouter API client (uses OpenAI SDK with custom base URL)."""
        try:
            from openai import AsyncOpenAI
            self.AsyncOpenAI = AsyncOpenAI
        except ImportError:
            error("openai package not installed. Install with: pip install openai", "AIService")
            return
        
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        self.model_name = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-haiku")
        self.model = self.model_name
        
        if self.api_key:
            try:
                # OpenRouter uses OpenAI SDK with custom base URL
                api_url = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1")
                self.async_client = self.AsyncOpenAI(api_key=self.api_key, base_url=api_url)
                logger.info(f"AIService (OpenRouter) initialized with model: {self.model_name}")
                logger.info(f"OpenRouter API URL: {api_url}")
            except Exception as e:
                error(f"Failed to initialize OpenRouter client: {e}", "AIService")
        else:
            logger.warning("OPENROUTER_API_KEY not found. AIService will mock OpenRouter responses.")
    
    async def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        current_time = time.time()
        time_since_last_call = current_time - self.last_api_call_time
        
        if time_since_last_call < self.min_call_interval:
            sleep_time = self.min_call_interval - time_since_last_call
            logger.debug(f"Rate limiting: waiting {sleep_time:.2f}s before next API call")
            await asyncio.sleep(sleep_time)
        
        self.last_api_call_time = time.time()
    
    async def _call_with_retry(self, call_func, max_retries=3, timeout=300.0):
        """
        Execute async function with exponential backoff retry logic.
        
        Args:
            call_func: Async callable that makes the API call
            max_retries: Maximum number of retry attempts
            timeout: Timeout for the call
        """
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                await self._rate_limit()
                response = await asyncio.wait_for(call_func(), timeout=timeout)
                return response
                
            except asyncio.TimeoutError:
                wait_time = 2 ** attempt
                if attempt < max_retries - 1:
                    logger.warning(
                        f"⚠️ AI API call timed out ({timeout}s) - "
                        f"Attempt {attempt + 1}/{max_retries}. Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    error_msg = (
                        f"🚫 AI API TIMEOUT: The AI service did not respond within {timeout} seconds. "
                        f"All {max_retries} retry attempts have been exhausted. "
                        f"Please try again later."
                    )
                    logger.error(error_msg)
                    error(error_msg, "AIService")
                    raise Exception(error_msg)
                    
            except Exception as e:
                last_exception = e
                error_str = str(e)
                
                if "429" in error_str or "rate_limit" in error_str.lower():
                    wait_time = 2 ** attempt
                    
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"⚠️ AI model temporarily unavailable (rate limited) - "
                            f"Attempt {attempt + 1}/{max_retries}. Retrying in {wait_time}s..."
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        error_msg = (
                            f"🚫 REMOTE AI MODEL UNAVAILABLE: The AI service is currently overloaded or rate-limited. "
                            f"All {max_retries} retry attempts have been exhausted. "
                            f"Please wait a few minutes and try again later."
                        )
                        logger.error(error_msg)
                        error(error_msg, "AIService")
                        raise Exception(error_msg) from e
                else:
                    logger.error(f"API call failed with non-retryable error: {error_str}")
                    raise
        
        raise last_exception
    
    async def generate_work_plan(self, story_description: str, subtasks: List[Dict[str, Any]] = None) -> str:
        """Generate a structured work plan based on story description."""
        if not self.api_key:
            return """# Work Plan
SUBTASK: Setup Project Structure
Desc: Initialize project directories
---
SUBTASK: Implement Core Features
Desc: Build main functionality
---
SUBTASK: Add Tests
Desc: Create test coverage
---"""
        
        prompt = f"""You are an expert Autonomous Developer Agent.

Your task is to create a detailed technical work plan for implementing the following requirements.

REQUIREMENTS:
{story_description}

CRITICAL PLANNING RULES:
- The PRD section is the product source of truth. Decompose all required features into implementation subtasks.
- The "Technical Requirements (Selected Skills)" section is mandatory engineering guidance.
- Every selected skill must be represented by one or more subtasks.
- If both backend and frontend requirements are present, include subtasks for BOTH domains and their integration.
- Do not skip testing/validation/quality tasks when required by skills.

Format your response with SUBTASK markers exactly like this:

SUBTASK: 1. [Short summary title]
Desc: [Detailed description of what needs to be implemented]
---

SUBTASK: 2. [Next task title]
Desc: [Detailed description]
---

🎯 GOLDEN RULE: Group related functionality together. Dependencies BEFORE Dependents.
🚨 CRITICAL OUTPUT REQUIREMENT:
- Produce at least 8 subtasks for full-stack requirements; at least 5 for single-stack requirements.
- Do not collapse the plan into a couple of broad subtasks.
"""
        
        try:
            if self.vendor == "gemini":
                return await self._generate_work_plan_gemini(prompt)
            elif self.vendor == "anthropic":
                return await self._generate_work_plan_anthropic(prompt)
            elif self.vendor == "openai":
                return await self._generate_work_plan_openai(prompt)
            elif self.vendor == "openrouter":
                return await self._generate_work_plan_openrouter(prompt)
        except Exception as e:
            error(f"Work plan generation failed: {e}", "AIService")
            if "REMOTE AI MODEL UNAVAILABLE" in str(e) or "AI API TIMEOUT" in str(e):
                raise
            return "Error generating work plan due to AI service failure."
    
    async def _generate_work_plan_gemini(self, prompt: str) -> str:
        """Generate work plan using Gemini API."""
        # Restore full detailed prompt with all AI instructions
        full_prompt = prompt + """
        
        🚨 CRITICAL REPOSITORY STRUCTURE - READ THIS FIRST! 🚨
        
        ==================================================================================
        SEPARATE GIT REPOSITORIES - DO NOT USE "backend/" OR "frontend/" PREFIXES!
        ==================================================================================
        
        ⚠️ FRONTEND and BACKEND are in SEPARATE git repositories!
        ⚠️ DO NOT include "backend/" or "frontend/" prefixes in your file paths!
        
        ✅ CORRECT file paths examples (separate repos):
           - Backend repo: models/user.py, auth/jwt_utils.py, services/auth_service.py, routes/auth_routes.py, main.py
           - Frontend repo: src/types/user.ts, src/api/auth.ts, src/components/Login.tsx, app/page.tsx
        
        ❌ WRONG file paths (will cause FAILURE):
           - backend/models/user.py ← WRONG! Backend is its own repo root
           - frontend/src/types/user.ts ← WRONG! Frontend is its own repo root
        
        🎯 REMEMBER: Each repo is standalone - no "backend/" or "frontend/" folder prefixes!
        
        ==================================================================================
        
        Format your response with SUBTASK markers exactly like this:
        
        SUBTASK: 1. [Short summary title]
        Desc: [Detailed description of what needs to be implemented]
        ---
        
        SUBTASK: 2. [Next task title]
        Desc: [Detailed description]
        ---
        
        🚨 CRITICAL: TOKEN LIMIT - WITH 64K TOKENS - GROUP RELATED FUNCTIONALITY:
        ⚠️ Each subtask should generate 5-10 files MAX to avoid truncation
        ⚠️ Group related files together (e.g., model + service + routes)
        
        🚨 MANDATORY SUBTASK ORDERING - PREVENT ALL IMPORT ERRORS:
        
        Backend Python Projects - MANDATORY ORDER (NO "backend/" prefix):
        1. "Setup Backend Dependencies" → requirements.txt, .env.example, __init__.py
        2. "Create Data Models" → models/*.py (ALL model files)
        3. "Setup Authentication Utilities" → auth/*.py (auth_utils.py, jwt_utils.py, dependencies.py)
        4. "Setup Database Client" → database/*.py (firestore_client.py)
        5. "Create Service Layer" → services/*.py (ALL service files)
        6. "Create API Routes" → routes/*.py (ALL route files)
        7. "Create Main Application" → main.py
        8. "Add Tests" → tests/**/*.py
        
        Frontend TypeScript/React Projects - MANDATORY ORDER (NO "frontend/" prefix):
        1. "Setup Frontend Configuration" → package.json, next.config.js, tsconfig.json, etc.
        2. "Create Type Definitions" → src/types/*.ts (ALL type files)
        3. "Create API Client Layer" → src/api/*.ts (ALL API functions)
        4. "Create State Management" → src/store/*.ts
        5. "Create Reusable Components" → src/components/**/*.tsx
        6. "Create Page Components" → app/**/*.tsx
        7. "Create Root Layout & Globals" → app/layout.tsx, page.tsx, globals.css
        8. "Add Tests" → __tests__/**/*.test.tsx
        
        🚨 WHY THIS ORDER IS MANDATORY:
        - Models MUST be created BEFORE services (services import models)
        - Services MUST be created BEFORE routes (routes import services)
        - Types MUST be created BEFORE API client (API client imports types)
        - API client MUST be created BEFORE pages (pages import API functions)
        
        ⚠️ If a file imports from another file, BOTH files must be in SAME subtask OR created earlier
        ⚠️ NEVER create a file that imports from a file that doesn't exist yet
        
        🎯 GOLDEN RULE FOR SUBTASK ORDER:
        "Dependencies BEFORE Dependents. Always."
        
        🚨 CRITICAL OUTPUT REQUIREMENT:
        - Produce at least 8 subtasks for full-stack requirements; at least 5 for single-stack requirements.
        - Do not collapse the plan into a couple of broad subtasks.
        """
        
        async def call_gemini():
            return await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model,
                contents=full_prompt,
                config={"temperature": 0.7, "max_output_tokens": 4096}
            )
        
        response = await self._call_with_retry(call_gemini, timeout=300.0)
        return response.text
    
    async def _generate_work_plan_anthropic(self, prompt: str) -> str:
        """Generate work plan using Anthropic API."""
        async def call_anthropic():
            return await self.async_client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}]
            )
        
        response = await self._call_with_retry(call_anthropic, timeout=300.0)
        return response.content[0].text
    
    async def _generate_work_plan_openai(self, prompt: str) -> str:
        """Generate work plan using OpenAI API."""
        async def call_openai():
            return await self.async_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=4096
            )
        
        response = await self._call_with_retry(call_openai, timeout=300.0)
        return response.choices[0].message.content
    
    async def _generate_work_plan_openrouter(self, prompt: str) -> str:
        """Generate work plan using OpenRouter API (compatible with OpenAI SDK)."""
        async def call_openrouter():
            return await self.async_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=4096
            )
        
        response = await self._call_with_retry(call_openrouter, timeout=300.0)
        return response.choices[0].message.content
    
    def parse_work_plan(self, work_plan: str) -> List[Dict[str, str]]:
        """Parse work plan to extract subtasks from strict and common variant formats."""
        subtasks: List[Dict[str, str]] = []
        if not work_plan:
            return subtasks

        def add_subtask(summary: str, description: str) -> None:
            summary = re.sub(r'^\d+[\.:\s]+', '', (summary or "").strip()).strip()
            description = (description or "").strip()
            if not summary or not description:
                return
            if any(s["summary"] == summary and s["description"] == description for s in subtasks):
                return
            subtasks.append({"summary": summary, "description": description})
            logger.info(f"Parsed subtask: {summary}")

        # Primary format:
        # SUBTASK: <title>
        # Desc|Description: <details>
        # ---
        primary_pattern = re.compile(
            r"SUBTASK:\s*(?P<title>[^\n]+)\n(?:(?:Desc|Description)\s*:\s*)(?P<desc>.*?)(?=\n---|\nSUBTASK:|\Z)",
            re.DOTALL | re.IGNORECASE,
        )
        for match in primary_pattern.finditer(work_plan):
            add_subtask(match.group("title"), match.group("desc"))

        # Variant format:
        # Optional heading emphasis plus "Subtask" marker and free-form body.
        # Example:
        # ### Subtask 1: Setup backend
        # Description: ...
        # (or body text without explicit "Description:" label)
        subtask_block_pattern = re.compile(
            r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*SUBTASK(?:\s*#?\s*\d+)?\s*[:\-]\s*(?P<title>[^\n]+?)\s*(?:\*\*)?\s*\n(?P<body>.*?)(?=^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*SUBTASK(?:\s*#?\s*\d+)?\s*[:\-]|\Z)",
            re.DOTALL | re.IGNORECASE | re.MULTILINE,
        )
        for match in subtask_block_pattern.finditer(work_plan):
            body = match.group("body").strip()
            desc_match = re.search(
                r"(?im)^\s*(?:Desc|Description|Details|Implementation)\s*:\s*(?P<desc>.*)",
                body,
                re.DOTALL,
            )
            description = desc_match.group("desc").strip() if desc_match else body
            add_subtask(match.group("title"), description)

        # Fallback format (common model drift):
        # 1. <title>
        # Desc|Description: <details>
        fallback_pattern = re.compile(
            r"^\s*\d+[.)]\s*(?P<title>[^\n]+)\n(?:(?:Desc|Description)\s*:\s*)(?P<desc>.*?)(?=\n\s*\d+[.)]\s+|\Z)",
            re.DOTALL | re.IGNORECASE | re.MULTILINE,
        )
        for match in fallback_pattern.finditer(work_plan):
            add_subtask(match.group("title"), match.group("desc"))

        # Fallback for inline numbered items:
        # 1) <title>: <description>
        inline_numbered_pattern = re.compile(
            r"^\s*\d+[.)]\s*(?P<title>[^:\n]+?)\s*[:\-]\s*(?P<desc>[^\n]+)\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        for match in inline_numbered_pattern.finditer(work_plan):
            add_subtask(match.group("title"), match.group("desc"))

        return subtasks
    
    async def generate_prd(self, prompt: str, attachments: List[Dict[str, Any]]) -> str:
        """Generate a PRD based on user prompt and attachments."""
        if not self.api_key:
            return "Error: AI service not initialized. Please check API keys."
        
        system_prompt = """You are an expert Product Manager. Your task is to generate a comprehensive Product Requirement Document (PRD) 
based on the user's ideas and any attached files (images of UI designs or PDF docs).

The PRD MUST include:
1. Product Summary
2. Functional Requirements
3. UI/UX Requirements (referencing attached designs if provided)
4. Technical Constraints

🚫 DO NOT include a "User Stories" section as it is redundant.

Format the output clearly in markdown."""
        
        try:
            if self.vendor == "gemini":
                return await self._generate_prd_gemini(system_prompt, prompt, attachments)
            elif self.vendor == "anthropic":
                return await self._generate_prd_anthropic(system_prompt, prompt, attachments)
            elif self.vendor == "openai":
                return await self._generate_prd_openai(system_prompt, prompt, attachments)
            elif self.vendor == "openrouter":
                return await self._generate_prd_openrouter(system_prompt, prompt, attachments)
        except Exception as e:
            error(f"PRD generation failed: {e}", "AIService")
            if "REMOTE AI MODEL UNAVAILABLE" in str(e) or "AI API TIMEOUT" in str(e):
                raise
            return f"Error generating PRD: {str(e)}"
    
    async def _generate_prd_gemini(self, system_prompt: str, prompt: str, attachments: List[Dict[str, Any]]) -> str:
        """Generate PRD using Gemini API."""
        async def call_gemini():
            return await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model,
                contents=f"{system_prompt}\n\nUser Product Thought: {prompt}",
                config={"temperature": 0.7, "max_output_tokens": 4096}
            )
        
        response = await self._call_with_retry(call_gemini, timeout=300.0)
        return response.text
    
    async def _generate_prd_anthropic(self, system_prompt: str, prompt: str, attachments: List[Dict[str, Any]]) -> str:
        """Generate PRD using Anthropic API."""
        async def call_anthropic():
            return await self.async_client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": f"{system_prompt}\n\nUser Product Thought: {prompt}"}]
            )
        
        response = await self._call_with_retry(call_anthropic, timeout=300.0)
        return response.content[0].text
    
    async def _generate_prd_openai(self, system_prompt: str, prompt: str, attachments: List[Dict[str, Any]]) -> str:
        """Generate PRD using OpenAI API."""
        async def call_openai():
            return await self.async_client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=4096
            )
        
        response = await self._call_with_retry(call_openai, timeout=300.0)
        return response.choices[0].message.content
    
    async def _generate_prd_openrouter(self, system_prompt: str, prompt: str, attachments: List[Dict[str, Any]]) -> str:
        """Generate PRD using OpenRouter API (compatible with OpenAI SDK)."""
        async def call_openrouter():
            return await self.async_client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=4096
            )
        
        response = await self._call_with_retry(call_openrouter, timeout=300.0)
        return response.choices[0].message.content
    
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
    ) -> tuple:
        """Generate code for a specific task. Returns tuple of (code_text, finish_reason)."""
        logger.info(f"🚀 [AIService] Entering generate_code for task: {task_description[:50]}...")
        
        if not self.api_key:
            return (f"""FILE_PATH: src/generated_code.py
---
# Mock Code Generation for: {task_description[:30]}...
# API key not configured.

def implementation():
    # TODO: Implement {task_description}
    pass
""", 'ERROR')
        
        # Build full prompt with all instructions (preserved from original)
        prompt = f"""You are an expert Autonomous Developer Agent. Generate code for the following task.

TASK: {task_description}
CONTEXT: {context}
PRD: {story_context}

🚨 CRITICAL REPOSITORY STRUCTURE - READ THIS FIRST! 🚨

SEPARATE GIT REPOSITORIES - DO NOT USE "backend/" OR "frontend/" PREFIXES!

⚠️ FRONTEND and BACKEND are in SEPARATE git repositories!
⚠️ DO NOT include "backend/" or "frontend/" prefixes in your file paths!
⚠️ DO NOT use "from backend.X" or "from frontend.X" in imports!

✅ CORRECT file paths (separate repos):
   - Backend repo: models/user.py, auth/jwt_utils.py, services/auth_service.py, routes/auth_routes.py, main.py
   - Frontend repo: src/types/user.ts, src/api/auth.ts, src/components/Login.tsx, app/page.tsx

❌ WRONG file paths (will cause errors):
   - backend/models/user.py ← WRONG! Backend is its own repo root
   - frontend/src/types/user.ts ← WRONG! Frontend is its own repo root

🎯 REMEMBER: Each repo is standalone - the directory IS the Python/TypeScript root!

🚨 CRITICAL: GENERATE ALL DEPENDENT FILES TOGETHER!

⛔ DEPLOYMENT WILL FAIL if you reference a file that doesn't exist!

**GOLDEN RULE: If file A imports from file B, generate BOTH files in THIS response!**

MANDATORY RESPONSE FORMAT:
For each file you generate, use this EXACT structure:

FILE_PATH: [path/to/file.ext]
---
[file content here]
---

Example:
FILE_PATH: models/user.py
---
from pydantic import BaseModel

class User(BaseModel):
    id: str
    email: str
---

🚨 CRITICAL: Use exact markers. Do NOT use markdown code blocks.
🚨 CRITICAL: Ensure every file is wrapped in '---' separators.
🚨 CRITICAL: DO NOT add "backend/" or "frontend/" prefixes!
🚨 CRITICAL: DO NOT use "from backend.X" or "from frontend.X" imports!
"""
        
        try:
            if self.vendor == "gemini":
                text, reason = await self._generate_code_gemini(prompt, max_output_tokens, temperature, timeout)
            elif self.vendor == "anthropic":
                text, reason = await self._generate_code_anthropic(prompt, max_output_tokens, temperature, timeout)
            elif self.vendor == "openai":
                text, reason = await self._generate_code_openai(prompt, max_output_tokens, temperature, timeout)
            elif self.vendor == "openrouter":
                text, reason = await self._generate_code_openrouter(prompt, max_output_tokens, temperature, timeout)

            text = self._normalize_ai_text(text)
            if not text.strip():
                logger.warning("⚠️ [AIService] AI returned empty code response; returning controlled fallback")
                return ("FILE_PATH: error.txt\n---\n# Error: Empty AI code generation response\n---", 'EMPTY')

            logger.info(f"🤖 [AIService] AI Response received. Length: {len(text)} characters, Finish Reason: {reason}")
            return (text, reason)
        except Exception as e:
            error(f"Code generation failed: {e}", "AIService")
            if "REMOTE AI MODEL UNAVAILABLE" in str(e) or "AI API TIMEOUT" in str(e):
                raise
            return (f"FILE_PATH: error.txt\n---\n# Error: {str(e)}\n---", 'ERROR')
    
    async def _generate_code_gemini(self, prompt: str, max_tokens: int, temp: float, timeout: float) -> tuple:
        """Generate code using Gemini API."""
        async def call_gemini():
            return await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model,
                contents=prompt,
                config={"temperature": temp, "max_output_tokens": max_tokens}
            )
        
        response = await self._call_with_retry(call_gemini, timeout=timeout)
        finish_reason = 'STOP'
        if hasattr(response, 'candidates') and response.candidates:
            finish_reason = str(response.candidates[0].finish_reason)
        text = self._normalize_ai_text(getattr(response, 'text', None))
        return (text, finish_reason)
    
    async def _generate_code_anthropic(self, prompt: str, max_tokens: int, temp: float, timeout: float) -> tuple:
        """Generate code using Anthropic API."""
        async def call_anthropic():
            return await self.async_client.messages.create(
                model=self.model,
                max_tokens=min(max_tokens, 4096),
                temperature=temp,
                messages=[{"role": "user", "content": prompt}]
            )
        
        response = await self._call_with_retry(call_anthropic, timeout=timeout)
        text = ""
        try:
            if getattr(response, 'content', None):
                text_parts = []
                for block in response.content:
                    block_text = self._normalize_ai_text(getattr(block, 'text', None))
                    if block_text:
                        text_parts.append(block_text)
                text = "\n".join(text_parts).strip()
        except Exception:
            text = ""
        return (text, str(getattr(response, 'stop_reason', 'UNKNOWN')))
    
    async def _generate_code_openai(self, prompt: str, max_tokens: int, temp: float, timeout: float) -> tuple:
        """Generate code using OpenAI API."""
        async def call_openai():
            return await self.async_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temp,
                max_tokens=min(max_tokens, 4096)
            )
        
        response = await self._call_with_retry(call_openai, timeout=timeout)
        choice = response.choices[0] if getattr(response, 'choices', None) else None
        msg = choice.message if choice else None
        text = self._normalize_ai_text(getattr(msg, 'content', None))
        finish_reason = getattr(choice, 'finish_reason', 'UNKNOWN')
        return (text, finish_reason)
    
    async def _generate_code_openrouter(self, prompt: str, max_tokens: int, temp: float, timeout: float) -> tuple:
        """Generate code using OpenRouter API (compatible with OpenAI SDK)."""
        async def call_openrouter():
            return await self.async_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temp,
                max_tokens=min(max_tokens, 4096)
            )
        
        response = await self._call_with_retry(call_openrouter, timeout=timeout)
        choice = response.choices[0] if getattr(response, 'choices', None) else None
        msg = choice.message if choice else None
        text = self._normalize_ai_text(getattr(msg, 'content', None))
        finish_reason = getattr(choice, 'finish_reason', 'UNKNOWN')
        return (text, finish_reason)
    
    def parse_generated_code(self, response: str) -> List[Dict[str, str]]:
        """Extract file paths and content from generated code response."""
        if response is None:
            logger.warning("⚠️ [AIService] Parsing skipped: response is None")
            return []
        logger.info(f"🔍 [AIService] Parsing code generation response (length: {len(response)})")
        if not response or len(response) < 10:
            logger.warning("⚠️ [AIService] AI response is empty or too short!")
            return []
        
        files = []
        block_pattern = re.compile(
            r"(?:^|(?:\n| ))(?:FILE_PATH:|FILE:)\s*(?P<path>[^\n]+)\n---\n(?P<content>.*?)(?=(?:\n| )(?:---(?:\n| )(?:FILE_PATH:|FILE:)|-{3,}(?:\n| )|FILE_PATH:|FILE:)|(?:\n---(?=\n(?:FILE_PATH:|FILE:)|\n*$))|\n\Z)",
            re.DOTALL
        )
        matches = list(block_pattern.finditer(response))
        logger.info(f"🔍 [AIService] Parsed {len(matches)} FILE block(s)")
        
        for m in matches:
            file_path = m.group('path').strip()
            content = m.group('content').strip()
            
            file_path = re.sub(r'[`*]', '', file_path).strip()
            content = re.sub(r'^```(?:\w+)?\n', '', content)
            content = re.sub(r'\n```$', '', content)
            content = re.sub(r'^-{3,}\n', '', content)
            content = re.sub(r'\n-{3,}$', '', content)
            content = content.strip()
            
            if file_path and content:
                files.append({'file_path': file_path, 'content': content})
                logger.info(f"✅ [AIService] Parsed file: {file_path} ({len(content)} chars)")
        
        if not files:
            logger.error("❌ [AIService] Failed to extract any valid files from response!")
        
        return files
