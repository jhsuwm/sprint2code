"""
Backend-specific programmatic fix helpers for DeploymentFixer.
"""

import os
import re
import json
import subprocess
import asyncio
from typing import TYPE_CHECKING, List, Dict

if TYPE_CHECKING:
    from agents.deployment_fixer import DeploymentFixer

class BackendFixer:
    def __init__(self, fixer: "DeploymentFixer"):
        self.fixer = fixer

    async def _fix_requirements(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        packages = file_info.get('packages', set())
        local = os.path.join(repo_dir, file_path)
        original_content = file_info.get('content', '')
        content = original_content
        is_model_file = '/models/' in file_path.replace("\\", "/")

        def _is_invalid_req_line(s: str) -> bool:
            if not s or s.startswith('#'):
                return False
            if s.startswith('```') or s.startswith('FILE_PATH:'):
                return True
            if re.match(r'^-{3,}$', s) or re.match(r'^={3,}$', s):
                return True
            patterns = [
                r'^(from|import|class|def|return|if|else|elif|for|while|try|except|with|raise|assert|pass)\b',
                r'^print\(',
                r'^#.*(FILE_PATH|```)',
            ]
            if any(re.search(p, s) for p in patterns):
                return True
            if re.match(r'^@\w+', s):
                return True
            if s in ('python', 'bash', 'yaml', 'json', 'typescript', 'javascript', 'txt'):
                return True
            if ' ' in s and not s.startswith('-') and '://' not in s:
                return True
            return False

        cleaned_lines = []
        content_was_dirty = False
        removed_invalid_runtime_pkg = False
        for line in content.splitlines():
            stripped = line.strip()
            if _is_invalid_req_line(stripped):
                content_was_dirty = True
                continue
            if re.fullmatch(r"azure(\s*(==|>=|<=|~=).*)?", stripped, flags=re.IGNORECASE):
                removed_invalid_runtime_pkg = True
                content_was_dirty = True
                self.fixer._safe_log(job_id, "🧹 Removed deprecated 'azure' meta-package from requirements.txt", "Requirements Cleanup")
                continue
            cleaned_lines.append(line)
        if content_was_dirty:
            content = '\n'.join(cleaned_lines)
            self.fixer._safe_log(job_id, f"🧹 Stripped malformed separator lines from {file_path}", "Requirements Cleanup")

        missing_entries = [str(m) for m in file_info.get('missing', [])]

        def _version_key(v: str):
            parts = re.split(r'[^0-9]+', v)
            nums = [int(p) for p in parts if p.isdigit()]
            return nums or [0]

        def _best_version(versions: List[str]) -> str:
            versions = [v.strip() for v in versions if v and v.strip()]
            if not versions:
                return ""
            return max(versions, key=_version_key)

        def _extract_req_name(req: str) -> str:
            base = re.split(r'[<>=!~]', req, maxsplit=1)[0].strip()
            return base

        def _apply_version_fix(req_name: str, new_version: str) -> bool:
            nonlocal content
            lines = content.splitlines()
            updated = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                name_part = re.split(r'[<>=!~]', stripped, maxsplit=1)[0].strip()
                base_name = name_part.split('[', 1)[0].strip()
                if base_name.lower() == req_name.lower():
                    if new_version:
                        lines[i] = f"{name_part}=={new_version}"
                    else:
                        lines[i] = name_part
                    updated = True
            if not updated:
                if new_version:
                    lines.append(f"{req_name}=={new_version}")
                else:
                    lines.append(req_name)
                updated = True
            content = "\n".join(lines)
            return updated

        version_fixed = False
        for entry in missing_entries:
            vm = re.search(
                r"Invalid version: '([^']+)' not available(?:\. Available versions: (.+))?",
                entry
            )
            if not vm:
                continue
            bad_req = vm.group(1).strip()
            available_raw = (vm.group(2) or "").strip()
            req_name = _extract_req_name(bad_req)
            if available_raw:
                versions = [v.strip() for v in available_raw.split(',')]
                best = _best_version(versions)
                if best and _apply_version_fix(req_name, best):
                    version_fixed = True
                    self.fixer._safe_log(job_id, f"🧹 Downgraded {req_name} to available version {best}", "Requirements Cleanup")
            else:
                if _apply_version_fix(req_name, ""):
                    version_fixed = True
                    self.fixer._safe_log(job_id, f"🧹 Unpinned {req_name} to allow pip to resolve a valid version", "Requirements Cleanup")

        if not packages and not content_was_dirty and not version_fixed:
            return False

        pkg_map = {
            'jwt_utils': 'PyJWT',
            'jose': 'python-jose[cryptography]',
            'jwt': 'PyJWT',
            'pyjwt': 'PyJWT',
            'bcrypt': 'bcrypt',
            'passlib': 'passlib[bcrypt]',
            'passlib.context': 'passlib[bcrypt]',
            'cryptography': 'cryptography',
            'dotenv': 'python-dotenv',
            'pydantic': 'pydantic',
            'pydantic_settings': 'pydantic-settings',
            'pydantic-settings': 'pydantic-settings',
            'email-validator': 'email-validator',
            'email_validator': 'email-validator',
            'validators': 'validators',
            'fastapi': 'fastapi',
            'uvicorn': 'uvicorn[standard]',
            'starlette': 'starlette',
            'flask': 'Flask',
            'django': 'Django',
            'sqlalchemy': 'SQLAlchemy',
            'alembic': 'alembic',
            'psycopg2': 'psycopg2-binary',
            'pymongo': 'pymongo',
            'bson': 'pymongo',
            'redis': 'redis',
            'google.cloud.firestore': 'google-cloud-firestore',
            'google.cloud.storage': 'google-cloud-storage',
            'google.cloud.secretmanager': 'google-cloud-secret-manager',
            'firebase_admin': 'firebase-admin',
            'azure.storage.blob': 'azure-storage-blob',
            'azure': 'azure-storage-blob',
            'requests': 'requests',
            'httpx': 'httpx',
            'aiohttp': 'aiohttp',
            'pandas': 'pandas',
            'numpy': 'numpy',
            'PIL': 'Pillow',
            'pillow': 'Pillow',
            'python_magic': 'python-magic',
            'magic': 'python-magic',
            'dateutil': 'python-dateutil',
            'yaml': 'PyYAML',
            'pyyaml': 'PyYAML',
            'toml': 'toml',
            'click': 'click',
            'tqdm': 'tqdm',
            'pytest': 'pytest'
        }
        pkg_map = {k.lower(): v for k, v in pkg_map.items()}

        invalid_packages = {
            'ticket', 'user', 'auth', 'models', 'services',
            'routes', 'utils', 'config', 'main', 'app', 'backend', 'frontend'
        }

        for pkg in packages:
            if not pkg:
                continue
            pkg_normalized = pkg.strip()
            pkg_lower = pkg_normalized.lower()
            if pkg_lower in invalid_packages:
                self.fixer._safe_log(job_id, f"⏭️ Skipping invalid package: {pkg_normalized} (likely a model/module name)", "Package Filter")
                continue
            real_pkg = pkg_map.get(pkg_lower, pkg_normalized)
            if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]*(\[[A-Za-z0-9_,.-]+\])?$', real_pkg):
                self.fixer._safe_log(job_id, f"⏭️ Skipping invalid package token: {pkg_normalized}", "Package Filter")
                continue
            if not re.search(rf'^{re.escape(real_pkg)}(\[.*\])?([<>=!].*)?$', content, re.MULTILINE):
                content += f"\n{real_pkg}"

        if removed_invalid_runtime_pkg and not any(
            re.search(r'^azure-storage-blob([<>=!].*)?$', ln.strip(), re.IGNORECASE)
            for ln in content.splitlines()
        ):
            content += "\nazure-storage-blob"

        if content == original_content:
            return False

        with open(local, 'w') as f:
            f.write(content)

        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_backend_model(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        missing: List[str] = []
        for item in file_info.get('missing', []):
            if not isinstance(item, str):
                continue
            if item.startswith('Property'):
                continue
            export_match = re.search(r"Missing export:\s*([A-Za-z_][A-Za-z0-9_]*)", item)
            if export_match:
                name = export_match.group(1).strip()
                if name not in missing:
                    missing.append(name)
                continue
            candidate = item.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
                if candidate not in missing:
                    missing.append(candidate)
        if not missing:
            return False

        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content.strip():
            content = "from pydantic import BaseModel\n\n"

        if "from pydantic import" not in content and "import pydantic" not in content:
            content = "from pydantic import BaseModel\n" + content
        elif "from pydantic import" in content and "BaseModel" not in content:
            content = re.sub(
                r"from pydantic import ([^\n]+)",
                lambda m: (
                    f"from pydantic import {m.group(1)}, BaseModel"
                    if "BaseModel" not in m.group(1) else m.group(0)
                ),
                content,
                count=1
            )

        for item in missing:
            if not re.search(rf"class\s+{item.strip()}\b", content):
                if not content.endswith("\n\n"):
                    content += "\n" if content.endswith("\n") else "\n\n"
                content += f"class {item.strip()}(BaseModel):\n    pass\n"

        try:
            compile(content, local, 'exec')
        except SyntaxError as e:
            self.fixer._safe_log(
                job_id,
                f"❌ Rejected invalid model fix for {file_path}: syntax error at line {e.lineno} ({e.msg})",
                "Programmatic Export Fix",
                level="WARNING"
            )
            return False

        with open(local, 'w') as f:
            f.write(content)

        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_main_relative_imports(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original = content
        content = re.sub(r'^\s*from\s+\.(\w+)\s+import\s+', r'from \1 import ', content, flags=re.MULTILINE)
        content = re.sub(r'^\s*import\s+\.(\w+)\b', r'import \1', content, flags=re.MULTILINE)

        if content == original:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)
        self.fixer._safe_log(job_id, f"🧹 Orchestrator: Removed relative imports in entrypoint {file_path}", "Programmatic Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_pip_dependency_conflict(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Resolve pip dependency conflicts by unpinning conflicting packages.

        When pip reports ResolutionImpossible conflicts (e.g. google-cloud-firestore==2.16.0
        conflicts with firebase-admin), the fix is to remove the version pin so pip can
        find a compatible set of versions automatically.
        """
        if not file_path.endswith("requirements.txt"):
            return False

        local = os.path.join(repo_dir, file_path)
        original_content = file_info.get('content', '')
        if not original_content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                original_content = f.read()
        if not original_content:
            return False

        # Extract package names from conflict errors
        conflict_packages = set()
        for item in file_info.get('missing', []):
            item_s = str(item)
            # Match: "Pip dependency conflict in 'requirements.txt': 'google-cloud-firestore==2.16.0' conflicts..."
            m = re.search(r"'([a-zA-Z0-9_-]+)==([0-9][0-9.]*)'", item_s)
            if m:
                conflict_packages.add(m.group(1).lower())
            # Match: "unpin or downgrade 'google-cloud-firestore' to resolve"
            m2 = re.search(r"unpin or downgrade '([a-zA-Z0-9_-]+)'", item_s)
            if m2:
                conflict_packages.add(m2.group(1).lower())

        if not conflict_packages:
            return False

        content = original_content
        modified = False
        unpinned = []

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            pkg_base = re.split(r'[<>=!]', stripped, maxsplit=1)[0].strip().lower()
            if pkg_base in conflict_packages:
                # Replace pinned version with unpinned name
                clean_line = pkg_base
                content = content.replace(line, clean_line, 1)
                unpinned.append(stripped)
                modified = True

        if not modified:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(
            job_id,
            f"🧹 Unpinned conflicting packages to resolve pip dependency conflicts: {unpinned}",
            "Pip Conflict Resolution"
        )
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_python_unterminated_triple_quote(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Fix unterminated triple-quoted string/f-string literals.

        AI frequently generates multi-line f-strings (e.g. email templates) that
        are missing the closing triple-quote. This method finds the unclosed
        opening delimiter and appends the matching closing delimiter.
        """
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original = content
        lines = content.splitlines()

        # Scan for unterminated triple-quoted strings by tracking open/close state
        for quote_type in ('"""', "'''"):
            f_prefixes = ('f"""', "f'''", 'F"""', "F'''")
            in_triple = False
            is_fstring = False

            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue

                if not in_triple:
                    for fp in f_prefixes:
                        if fp in line:
                            in_triple = True
                            is_fstring = True
                            break
                    if not in_triple:
                        idx = line.find(quote_type)
                        if idx != -1:
                            rest = line[idx + 3:]
                            if quote_type not in rest:
                                in_triple = True
                                is_fstring = False
                else:
                    if quote_type in line:
                        in_triple = False

            if in_triple:
                last_line = lines[-1]
                if not last_line.rstrip().endswith(quote_type):
                    lines[-1] = last_line.rstrip() + '\n' + quote_type
                    content = '\n'.join(lines)

        if content == original:
            try:
                compile(content, local, 'exec')
            except SyntaxError as e:
                error_msg = str(e.msg) if e.msg else ''
                if 'unterminated triple-quoted' in error_msg.lower() and e.lineno:
                    lines = content.splitlines()
                    last_line = lines[-1] if lines else ''
                    if not last_line.rstrip().endswith(('"""', "'''")):
                        lines.append('"""')
                        content = '\n'.join(lines)

        if content == original:
            return False

        try:
            compile(content, local, 'exec')
        except SyntaxError:
            self.fixer._safe_log(
                job_id,
                f"❌ Triple-quote fix didn't resolve syntax error in {file_path}",
                "Programmatic Fix",
                level="WARNING"
            )
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(
            job_id,
            f"🧹 Closed unterminated triple-quoted string in {file_path}",
            "Programmatic Fix"
        )
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_python_syntax_artifacts(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original = content
        fenced = re.search(r"```(?:python)?\n(.*?)\n```", content, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            content = fenced.group(1).strip() + "\n"
        else:
            cleaned_lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith(("ROOT_CAUSE:", "COMPLETE_FIX:", "VERIFICATION:", "OUTPUT FORMAT:", "SUCCESS =")):
                    continue
                if stripped.startswith(("###", "NOTE:", "IMPORTANT:", "INSTRUCTIONS:")):
                    continue
                if stripped.startswith("FILE_PATH:"):
                    continue
                if stripped in ("---", "```", "python"):
                    continue
                cleaned_lines.append(line)
            content = "\n".join(cleaned_lines).strip() + "\n"

        def _looks_like_python_start(s: str) -> bool:
            return bool(re.match(r"^\s*(from|import|class|def|async\s+def|if __name__|@|#|\"\"\"|'''|$)", s))

        def _is_artifact_line(s: str) -> bool:
            s = s.strip()
            if not s:
                return True
            if s.startswith(("ROOT_CAUSE:", "COMPLETE_FIX:", "VERIFICATION:", "OUTPUT FORMAT:", "SUCCESS =")):
                return True
            if s.startswith(("###", "NOTE:", "IMPORTANT:", "INSTRUCTIONS:")):
                return True
            if s.startswith("FILE_PATH:"):
                return True
            if s in ("---", "```", "python"):
                return True
            return False

        for _ in range(40):
            try:
                compile(content, local, 'exec')
                break
            except SyntaxError as e:
                lines = content.splitlines()
                if not lines:
                    return False
                idx = max(0, (e.lineno or 1) - 1)
                if idx < len(lines) and _is_artifact_line(lines[idx]):
                    del lines[idx]
                    content = "\n".join(lines).lstrip("\n")
                    continue
                if e.lineno == 1 and not _looks_like_python_start(lines[0]):
                    content = "\n".join(lines[1:]).lstrip("\n")
                    continue
                return False
        else:
            return False

        if content == original:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)
        self.fixer._safe_log(job_id, f"🧹 Cleaned Python syntax artifacts in {file_path}", "Programmatic Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_python_missing_exports(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        is_model_file = '/models/' in file_path.replace("\\", "/")

        if isinstance(content, bytes):
            try:
                content = content.decode('utf-8')
            except Exception:
                return False

        missing_names = set()
        for err in file_info.get('missing', []):
            err_str = str(err)
            for match in re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", err_str):
                missing_names.add(match)
            # Handle unquoted missing export markers (e.g., "Missing export: decode_jwt")
            m = re.search(r"Missing export:\s*([A-Za-z_][A-Za-z0-9_]*)", err_str)
            if m:
                missing_names.add(m.group(1))

        if not missing_names:
            return False

        # Remove duplicate messages that are not symbol names
        missing_names = {name for name in missing_names if not name.startswith(('http', 'https'))}

        def _already_defined(name: str, src: str) -> bool:
            # Match assignments, type-annotated attributes (Pydantic/dataclass), class/def definitions
            return bool(re.search(rf'^(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b|^{re.escape(name)}\s*[=:]', src, re.MULTILINE))

        KNOWN_STUBS: Dict[str, str] = {
            'get_current_user': (
                '\nasync def get_current_user():\n'
                '    """Auto-generated stub: replace with real JWT verification."""\n'
                '    return None\n'
            ),
            'get_current_active_user': (
                '\nasync def get_current_active_user():\n'
                '    """Auto-generated stub: replace with real active-user check."""\n'
                '    return None\n'
            ),
            'create_access_token': (
                '\ndef create_access_token(data: dict, expires_delta=None) -> str:\n'
                '    """Auto-generated stub: replace with real JWT creation."""\n'
                '    return ""\n'
            ),
            'verify_token': (
                '\ndef verify_token(token: str):\n'
                '    """Auto-generated stub: replace with real JWT verification."""\n'
                '    return None\n'
            ),
            'decode_token': (
                '\ndef decode_token(token: str):\n'
                '    """Auto-generated stub: replace with real JWT decoding."""\n'
                '    return {}\n'
            ),
            'decode_jwt': (
                '\ndef decode_jwt(token: str):\n'
                '    """Auto-generated shim: keep compatibility with decode_token."""\n'
                '    try:\n'
                '        return decode_token(token)\n'
                '    except Exception:\n'
                '        return {}\n'
            ),
            'get_db': (
                '\nasync def get_db():\n'
                '    """Auto-generated stub: replace with real DB session/client."""\n'
                '    yield None\n'
            ),
            'get_database': (
                '\nasync def get_database():\n'
                '    """Auto-generated stub: replace with real database client."""\n'
                '    return None\n'
            ),
            'get_collection': (
                '\nasync def get_collection(name: str):\n'
                '    """Auto-generated stub: replace with real collection lookup."""\n'
                '    return None\n'
            ),
            'ObjectId': (
                '\nclass ObjectId(str):\n'
                '    """Auto-generated BSON ObjectId stub (string-compatible)."""\n'
                '    @classmethod\n'
                '    def is_valid(cls, id_val) -> bool:\n'
                '        return bool(id_val)\n'
            ),
            'hash_password': (
                '\ndef hash_password(password: str) -> str:\n'
                '    """Auto-generated stub: replace with real password hashing."""\n'
                '    return password\n'
            ),
            'verify_password': (
                '\ndef verify_password(plain: str, hashed: str) -> bool:\n'
                '    """Auto-generated stub: replace with real password verification."""\n'
                '    return plain == hashed\n'
            ),
            'get_user_by_id': (''), # Handled by _generate_generic_db_stub
            'get_user_by_email': (''), # Handled by _generate_generic_db_stub
            'create_user': (''), # Handled by _generate_generic_db_stub
            'update_user': (''), # Handled by _generate_generic_db_stub
            'get_user_by_password_reset_token': (''), # Handled by _generate_generic_db_stub
        }

        # Database-related function names that should be generated in database.py or database/__init__.py
        database_functions = {'get_user_by_id', 'get_user_by_email', 'create_user', 'update_user', 'get_user_by_password_reset_token'}
        
        stubs_added = []
        database_stubs_added = []
        
        for name in missing_names:
            if _already_defined(name, content):
                continue

            # Handle database functions separately - add them to database.py or database/__init__.py
            if name in database_functions:
                backend_prefix = self.fixer._backend_prefix(repo_dir)
                db_file_path = os.path.join(repo_dir, backend_prefix, 'database.py') if backend_prefix else os.path.join(repo_dir, 'database.py')
                db_init_path = os.path.join(repo_dir, backend_prefix, 'database', '__init__.py') if backend_prefix else os.path.join(repo_dir, 'database', '__init__.py')
                
                target_file = db_file_path
                if not os.path.exists(target_file):
                    target_file = db_init_path
                    if not os.path.exists(target_file):
                        os.makedirs(os.path.dirname(target_file), exist_ok=True)
                        initial_content = '"""Database interaction stubs (auto-generated by Sprint2Code fixer)"""\n\n'
                        with open(target_file, 'w', encoding='utf-8') as f:
                            f.write(initial_content)
                        await self.fixer._commit_programmatic_fix(job_id, os.path.relpath(target_file, repo_dir), initial_content, github_repo, github_branch)

                with open(target_file, 'r', encoding='utf-8') as f:
                    db_content = f.read()

                # Check if function already exists in database file
                if not re.search(rf"(?:async\s+)?def\s+{re.escape(name)}\b", db_content):
                    stub_signature = f"async def {name}(*args, **kwargs):"
                    if name in ('create_user', 'update_user'):
                        stub_body = f"    \"\"\"Auto-generated stub: AI should implement proper {name} logic.\"\"\"\n    print(f\"WARNING: {name} not implemented. Returning empty dict.\")\n    return {{}}"
                    else:
                        stub_body = f"    \"\"\"Auto-generated stub: AI should implement proper {name} logic.\"\"\"\n    print(f\"WARNING: {name} not implemented. Returning None.\")\n    return None"
                    
                    db_stub = f"\n{stub_signature}\n{stub_body}\n"
                    
                    if not db_content.endswith('\n'):
                        db_content += '\n'
                    db_content += db_stub
                    
                    with open(target_file, 'w', encoding='utf-8') as f:
                        f.write(db_content)
                    self.fixer._safe_log(job_id, f"✅ Added generic database stub for {name} to {os.path.relpath(target_file, repo_dir)}", "Programmatic DB Fix")
                    await self.fixer._commit_programmatic_fix(job_id, os.path.relpath(target_file, repo_dir), db_content, github_repo, github_branch)
                    database_stubs_added.append(name)
                continue

            if name in KNOWN_STUBS:
                stub = KNOWN_STUBS[name]
            elif is_model_file:
                if "from pydantic import" not in content and "import pydantic" not in content:
                    content = ("from pydantic import BaseModel\n\n" + content).lstrip()
                elif "from pydantic import" in content and "BaseModel" not in content:
                    content = re.sub(
                        r"from pydantic import ([^\n]+)",
                        lambda m: (
                            f"from pydantic import {m.group(1)}, BaseModel"
                            if "BaseModel" not in m.group(1) else m.group(0)
                        ),
                        content,
                        count=1
                    )
                stub = (
                    f"\nclass {name}(BaseModel):\n"
                    f"    \"\"\"Auto-generated model stub for: {name}\"\"\"\n"
                    f"    pass\n"
                )
            else:
                stub = (
                    f'\nasync def {name}(*args, **kwargs):\n'
                    f'    """Auto-generated stub for: {name}"""\n'
                    f'    return None\n'
                )

            if not content.endswith("\n"):
                content += "\n"
            content += stub
            stubs_added.append(name)

        if not stubs_added:
            return False

        os.makedirs(os.path.dirname(local), exist_ok=True)
        try:
            compile(content, local, 'exec')
        except SyntaxError as e:
            self.fixer._safe_log(
                job_id,
                f"❌ Rejected invalid Python export fix for {file_path}: line {e.lineno} ({e.msg})",
                "Programmatic Export Fix",
                level="WARNING"
            )
            return False
        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(
            job_id,
            f"✅ Added missing Python exports {stubs_added} to {file_path}",
            "Programmatic Export Fix"
        )
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)


    async def _fix_pydantic_settings_missing(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        missing_fields = set()
        for err in file_info.get('missing', []):
            err_str = str(err)
            m = re.search(r"Missing settings:\s*(.+)", err_str)
            if m:
                fields = [f.strip() for f in m.group(1).split(',') if f.strip()]
                missing_fields.update(fields)
        if not missing_fields:
            return False

        def _default_for(name: str, type_hint: str) -> str:
            name_l = name.lower()
            hint_l = (type_hint or "").lower()
            if "optional" in hint_l or "none" in hint_l:
                return "None"
            if "bool" in hint_l:
                return "False"
            if "int" in hint_l:
                return "0"
            if "float" in hint_l:
                return "0.0"
            if "secret" in name_l or "token" in name_l or "key" in name_l:
                return "\"dev-secret\""
            if "project" in name_l and "id" in name_l:
                return "\"local-project\""
            if "url" in name_l:
                return "\"http://localhost\""
            return f"\"{name_l}-default\""

        def _find_type_hint(name: str, src: str) -> str:
            m = re.search(rf"^\s*{re.escape(name)}\s*:\s*([^=\n]+)", src, re.MULTILINE)
            return m.group(1).strip() if m else ""

        original = content
        for name in sorted(missing_fields):
            type_hint = _find_type_hint(name, content)
            default_value = _default_for(name, type_hint)

            # Replace Field(... ) with Field(default, ...)
            field_pattern = re.compile(
                rf"({re.escape(name)}\s*:\s*[^=\n]+=\s*Field\()\s*\.{{3}}\s*([,)])",
                re.MULTILINE
            )
            content, field_subs = field_pattern.subn(
                rf"\\1{default_value}\\2",
                content,
                count=1
            )
            if field_subs > 0:
                continue

            # If Field(...) exists but uses '...' on next line, try a DOTALL fallback.
            field_pattern_multiline = re.compile(
                rf"({re.escape(name)}\s*:\s*[^=\n]+=\s*Field\()\s*\.{{3}}\s*([,)])",
                re.MULTILINE | re.DOTALL
            )
            content, field_subs = field_pattern_multiline.subn(
                rf"\\1{default_value}\\2",
                content,
                count=1
            )
            if field_subs > 0:
                continue

            # No Field(...) usage; add default to annotation-only line.
            annot_pattern = re.compile(rf"^(\s*{re.escape(name)}\s*:\s*[^=\n]+)$", re.MULTILINE)
            content, annot_subs = annot_pattern.subn(rf"\\1 = {default_value}", content, count=1)
            if annot_subs > 0:
                continue

        if content == original:
            return False

        try:
            compile(content, local, 'exec')
        except SyntaxError as e:
            self.fixer._safe_log(
                job_id,
                f"❌ Rejected settings default fix for {file_path}: line {e.lineno} ({e.msg})",
                "Pydantic Settings Fix",
                level="WARNING"
            )
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(
            job_id,
            f"✅ Added defaults for missing settings {sorted(missing_fields)} in {file_path}",
            "Pydantic Settings Fix"
        )
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_pydantic_settings_reexport(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Programmatically add module-level re-exports for Pydantic BaseSettings attributes.

        When config.py defines attrs inside a Settings(BaseSettings) class and other files do
        `from config import SECRET_KEY`, we add top-level aliases like `SECRET_KEY = settings.SECRET_KEY`
        so they are importable without changing how Settings works.
        """
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        # Only apply to files that have a BaseSettings class and a settings instance
        if 'BaseSettings' not in content:
            return False

        # Extract missing names from errors (e.g. "'SECRET_KEY' not found in 'config.py'")
        missing_names = set()
        for err in file_info.get('missing', []):
            for m in re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", str(err)):
                missing_names.add(m)
        if not missing_names:
            return False

        # Find the settings instance variable name (e.g., `settings = Settings()`)
        instance_match = re.search(
            r'^([a-z_][a-zA-Z0-9_]*)\s*=\s*(?:[A-Z][a-zA-Z0-9_]*)\(\)',
            content, re.MULTILINE
        )
        settings_var = instance_match.group(1) if instance_match else 'settings'

        # Find all attributes defined in BaseSettings subclass bodies
        # Pattern: indented `ATTR_NAME: ...` or `ATTR_NAME = ...`
        settings_attrs = set(re.findall(
            r'^\s{4,}([A-Z_][A-Z0-9_]*)\s*[=:]',
            content, re.MULTILINE
        ))

        # Determine which missing names are actually defined in the settings class
        names_to_reexport = missing_names & settings_attrs
        if not names_to_reexport:
            # Fallback: also check lowercase
            names_to_reexport = {n for n in missing_names if re.search(
                rf'^\s+{re.escape(n)}\s*[=:]', content, re.MULTILINE
            )}

        if not names_to_reexport:
            return False

        original = content
        # Add module-level re-exports at the end (before any trailing newlines)
        additions = []
        for name in sorted(names_to_reexport):
            # Skip if already exported at module level
            if re.search(rf'^{re.escape(name)}\s*[=:]', content, re.MULTILINE):
                continue
            additions.append(f'{name} = {settings_var}.{name}')

        if not additions:
            return False

        if not content.endswith('\n'):
            content += '\n'
        content += '\n# Module-level re-exports for direct import compatibility\n'
        content += '\n'.join(additions) + '\n'

        # Validate syntax
        try:
            compile(content, local, 'exec')
        except SyntaxError as e:
            self.fixer._safe_log(
                job_id,
                f"❌ Rejected Pydantic re-export fix for {file_path}: syntax error at line {e.lineno} ({e.msg})",
                "Pydantic Reexport Fix",
                level="WARNING"
            )
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(
            job_id,
            f"✅ Added module-level re-exports {sorted(names_to_reexport)} to {file_path} (Pydantic Settings compatibility)",
            "Pydantic Reexport Fix"
        )
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_python_parameter_order(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original = content

        def _has_real_default(param_str: str) -> bool:
            depth = 0
            for ch in param_str:
                if ch in '([{':
                    depth += 1
                elif ch in ')]}':
                    depth = max(0, depth - 1)
                elif ch == '=' and depth == 0:
                    return True
            return False

        def _strip_inline_comments(text: str) -> str:
            result = []
            for line in text.split('\n'):
                stripped_line = line
                in_str = None
                for i, ch in enumerate(line):
                    if ch in ('"', "'") and in_str is None:
                        in_str = ch
                    elif ch == in_str:
                        in_str = None
                    elif ch == '#' and in_str is None:
                        stripped_line = line[:i]
                        break
                result.append(stripped_line)
            return '\n'.join(result)

        def _split_params(params_str: str) -> List[str]:
            params_str = _strip_inline_comments(params_str)
            parts: List[str] = []
            cur: List[str] = []
            depth = 0
            for ch in params_str:
                if ch in '([{':
                    depth += 1
                elif ch in ')]}':
                    depth = max(0, depth - 1)
                if ch == ',' and depth == 0:
                    parts.append(''.join(cur).strip())
                    cur = []
                else:
                    cur.append(ch)
            if cur:
                parts.append(''.join(cur).strip())
            return [p for p in parts if p]

        def _find_matching_close_paren(src: str, open_pos: int) -> int:
            depth = 0
            i = open_pos
            while i < len(src):
                if src[i] == '(':
                    depth += 1
                elif src[i] == ')':
                    depth -= 1
                    if depth == 0:
                        return i
                i += 1
            return -1

        try:
            import ast
            tree = ast.parse(content)
        except SyntaxError:
            tree = None

        lines_plain = content.splitlines()
        func_sig_ranges: List[tuple] = []

        if tree:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                func_start_0 = node.lineno - 1
                char_offset = sum(len(l) + 1 for l in lines_plain[:func_start_0])
                src_tail = content[char_offset:]
                open_rel = src_tail.find('(')
                if open_rel == -1:
                    continue
                open_abs = char_offset + open_rel
                close_abs = _find_matching_close_paren(content, open_abs)
                if close_abs == -1:
                    continue
                cumulative = 0
                close_line_0 = func_start_0
                for idx in range(func_start_0, len(lines_plain)):
                    cumulative += len(lines_plain[idx]) + 1
                    if cumulative > close_abs - char_offset + open_rel:
                        close_line_0 = idx
                        break
                    close_line_0 = idx
                func_sig_ranges.append((func_start_0, close_line_0, open_abs, close_abs))
        else:
            for m in re.finditer(r'(?m)^[ \t]*(?:async\s+)?def\s+\w+\s*\(', content):
                open_abs = m.end() - 1
                close_abs = _find_matching_close_paren(content, open_abs)
                if close_abs == -1:
                    continue
                func_start_0 = content[:m.start()].count('\n')
                close_line_0 = content[:close_abs].count('\n')
                func_sig_ranges.append((func_start_0, close_line_0, open_abs, close_abs))

        if not func_sig_ranges:
            return False

        changed_any = False
        for func_start_0, sig_end_0, open_abs, close_abs in reversed(func_sig_ranges):
            params_text = content[open_abs + 1:close_abs]
            params = _split_params(params_text)
            if not params:
                continue

            pre_star: List[str] = []
            star_param: List[str] = []
            post_star: List[str] = []
            dstar_param: List[str] = []

            for p in params:
                stripped = p.strip()
                if stripped.startswith('**'):
                    dstar_param.append(p)
                elif stripped.startswith('*'):
                    star_param.append(p)
                elif star_param:
                    post_star.append(p)
                else:
                    pre_star.append(p)

            needs_fix = False
            seen_default = False
            for p in pre_star:
                if _has_real_default(p.strip()):
                    seen_default = True
                elif seen_default:
                    needs_fix = True
                    break

            if not needs_fix:
                continue

            required = [p for p in pre_star if not _has_real_default(p.strip())]
            optional = [p for p in pre_star if _has_real_default(p.strip())]
            new_params = required + optional + star_param + post_star + dstar_param

            if '\n' in params_text:
                indent_str = '    '
                for ln in params_text.split('\n')[1:]:
                    stripped_ln = ln.lstrip()
                    if stripped_ln:
                        indent_str = ln[: len(ln) - len(stripped_ln)]
                        break
                new_params_text = (',\n' + indent_str).join(p.strip() for p in new_params)
                new_params_text = '\n' + indent_str + new_params_text + ',\n'
            else:
                new_params_text = ', '.join(p.strip() for p in new_params)

            content = content[:open_abs + 1] + new_params_text + content[close_abs:]
            lines_plain = content.splitlines()
            changed_any = True

        if not changed_any:
            return False

        try:
            compile(content, local, 'exec')
        except SyntaxError:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)
        self.fixer._safe_log(job_id, f"🧹 Fixed Python parameter order in {file_path}", "Programmatic Syntax Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_backend_prefix(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        local = os.path.join(repo_dir, file_path)
        if not os.path.exists(local):
            return False

        with open(local, 'r') as f:
            original_content = f.read()

        content = re.sub(r'from backend\.(\w+)', r'from \1', original_content)
        content = re.sub(r'import backend\.(\w+)', r'import \1', content)

        if content == original_content:
            return False

        with open(local, 'w') as f:
            f.write(content)

        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _create_missing_backend_module(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        backend_prefix = self.fixer._backend_prefix(repo_dir)
        package_root = os.path.join(repo_dir, backend_prefix) if backend_prefix else repo_dir
        if file_path.endswith('.py'):
            module_name = os.path.basename(file_path)[:-3]
            package_dir = os.path.join(package_root, module_name)
            if os.path.isdir(package_dir):
                init_path = self.fixer._apply_prefix(backend_prefix, f"{module_name}/__init__.py")
                local_init = os.path.join(repo_dir, init_path)
                if not os.path.exists(local_init):
                    os.makedirs(os.path.dirname(local_init), exist_ok=True)
                    content = (
                        '"""Auto-generated package initializer for unresolved package import."""\n'
                        f"# Package: {module_name}\n"
                        '\n'
                    )
                    with open(local_init, 'w', encoding='utf-8') as f:
                        f.write(content)
                    self.fixer._safe_log(job_id, f"✅ Created package __init__.py shim: {init_path}", "Programmatic Create")
                    return await self.fixer._commit_programmatic_fix(job_id, init_path, content, github_repo, github_branch)
                return False

        local = os.path.join(repo_dir, file_path)
        if os.path.exists(local):
            return False

        os.makedirs(os.path.dirname(local), exist_ok=True)
        module_name = os.path.basename(file_path).replace('.py', '')
        content = (
            '"""Auto-generated shim module for unresolved import path."""\n'
            f"# Module: {module_name}\n"
            '\n'
        )

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(job_id, f"✅ Created missing backend module shim: {file_path}", "Programmatic Create")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)
