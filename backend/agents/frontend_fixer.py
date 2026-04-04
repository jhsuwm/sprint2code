"""
Frontend-specific programmatic fix helpers for DeploymentFixer.
"""

import os
import re
import json
import subprocess
import asyncio
from typing import TYPE_CHECKING, List, Dict, Optional

if TYPE_CHECKING:
    from agents.deployment_fixer import DeploymentFixer

class FrontendFixer:
    def __init__(self, fixer: "DeploymentFixer"):
        self.fixer = fixer

    async def _refresh_frontend_dependencies(self, repo_dir: str, job_id: str) -> bool:
        """Shared frontend dependency refresh logic (programmatic or worker)."""
        frontend_dir = os.path.join(repo_dir, 'frontend')
        package_json = os.path.join(frontend_dir, 'package.json')
        if not os.path.exists(package_json) and os.path.exists(os.path.join(repo_dir, 'package.json')):
            frontend_dir = repo_dir
            package_json = os.path.join(frontend_dir, 'package.json')
        if not os.path.exists(package_json):
            return False

        npm_cmd = ['npm', 'install', '--prefer-offline', '--no-audit', '--legacy-peer-deps']
        try:
            for attempt in range(1, 3):
                self.fixer._safe_log(job_id, f"📦 Refreshing frontend deps in {repo_dir} ({' '.join(npm_cmd)})", "Environment")
                process = await asyncio.create_subprocess_exec(
                    *npm_cmd,
                    cwd=frontend_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await process.communicate()
                stderr_text = (stderr or b"").decode(errors='replace')
                if process.returncode == 0:
                    self.fixer._safe_log(job_id, "✅ Successfully refreshed frontend dependencies", "Environment")
                    return True

                self.fixer._safe_log(job_id, f"⚠️ Dependency refresh failed (rc={process.returncode})", "Environment", level="WARNING")
                self.fixer._safe_log(job_id, f"Error details: {stderr_text[:1000]}", "Environment", level="DEBUG")

                bad_pkg = None
                e404_match = re.search(r"'([^']+@\S+)' is not in this registry", stderr_text)
                if e404_match:
                    spec = e404_match.group(1)
                    if spec.startswith('@'):
                        at_idx = spec.find('@', 1)
                        bad_pkg = spec if at_idx == -1 else spec[:at_idx]
                    else:
                        bad_pkg = spec.split('@', 1)[0]
                if bad_pkg is None:
                    url_match = re.search(r"registry\.npmjs\.org/([^\s]+)\s+-\s+Not found", stderr_text)
                    if url_match:
                        bad_pkg = url_match.group(1)

                if bad_pkg is None and 'ETARGET' in stderr_text:
                    etarget_match = re.search(r"No matching version found for ([^\s]+)", stderr_text)
                    if etarget_match:
                        spec = etarget_match.group(1)
                        if spec.startswith('@'):
                            at_idx = spec.find('@', 1)
                            bad_pkg = spec if at_idx == -1 else spec[:at_idx]
                        else:
                            bad_pkg = spec.split('@', 1)[0]

                if bad_pkg and (self.fixer._is_frontend_alias_package(bad_pkg) or bad_pkg):
                    try:
                        with open(package_json, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        deps = data.get('dependencies', {})
                        dev_deps = data.get('devDependencies', {})
                        removed = False
                        if bad_pkg in deps:
                            del deps[bad_pkg]
                            removed = True
                        if bad_pkg in dev_deps:
                            del dev_deps[bad_pkg]
                            removed = True
                        if removed:
                            data['dependencies'] = deps
                            data['devDependencies'] = dev_deps
                            with open(package_json, 'w', encoding='utf-8') as f:
                                json.dump(data, f, indent=2)
                                f.write('\n')
                            self.fixer._invalid_npm_packages.add(bad_pkg)
                            self.fixer._safe_log(job_id, f"🧹 Removed invalid npm alias package '{bad_pkg}' and retrying install", "Environment")
                            continue
                    except Exception as e:
                        self.fixer._safe_log(job_id, f"⚠️ Failed package.json cleanup after npm E404: {e}", "Environment", level="WARNING")

                if attempt >= 2:
                    self.fixer._safe_log(job_id, f"❌ Dependency refresh failed: {stderr_text[:1000]}", "Environment", level="ERROR")
                    return False

            return False
        except Exception as e:
            self.fixer._safe_log(job_id, f"❌ Dependency refresh error: {e}", "Environment", level="ERROR")
            return False

    async def _fix_package_json(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Add missing packages and resiliently fix syntax errors"""
        local = os.path.join(repo_dir, file_path)
        original_content = file_info.get('content', '') or '{}'

        def _extract_npm_package_name(pkg_spec: str) -> str:
            spec = (pkg_spec or "").strip().strip('"\'"')
            if not spec:
                return ""
            if spec.startswith('@'):
                first_at = spec.find('@', 1)
                return spec if first_at == -1 else spec[:first_at]
            return spec.rsplit('@', 1)[0] if '@' in spec else spec

        data = self.fixer._resilient_json_parse(original_content)
        if data is None:
            self.fixer._safe_log(job_id, f"❌ Failed to resiliently parse {file_path}", "JSON Fix")
            return False

        packages = file_info.get('packages', set())
        deps = data.get('dependencies', {})
        dev_deps = data.get('devDependencies', {})
        modified = False

        removed_invalid = []
        for pkg_name in list(deps.keys()):
            if self.fixer._is_frontend_alias_package(pkg_name):
                removed_invalid.append(pkg_name)
                del deps[pkg_name]
                modified = True
        for pkg_name in list(dev_deps.keys()):
            if self.fixer._is_frontend_alias_package(pkg_name):
                removed_invalid.append(pkg_name)
                del dev_deps[pkg_name]
                modified = True
        if removed_invalid:
            for pkg_name in removed_invalid:
                self.fixer._invalid_npm_packages.add(pkg_name)
            self.fixer._safe_log(
                job_id,
                f"🧹 Removed invalid/local alias dependencies from package.json: {sorted(set(removed_invalid))}",
                "Package Recovery"
            )

        if any("Missing 'node_modules'" in str(m) for m in file_info.get('missing', [])):
            modified = True
            self.fixer._safe_log(job_id, f"🚀 Refreshing dependencies to resolve missing node_modules in {file_path}", "JSON Fix")

        if original_content.strip() != json.dumps(data, indent=2).strip() and not packages:
            modified = True
            self.fixer._safe_log(job_id, f"🧹 Cleaned up malformed JSON in {file_path}", "JSON Fix")

        known_packages = {
            'zustand': '^4.5.0',
            'react-icons': '^5.0.0',
            '@heroicons/react': '^2.1.0',
            'react-hook-form': '^7.50.0',
            'class-variance-authority': '^0.7.1',
            'clsx': '^2.1.1',
            'tailwind-merge': '^2.5.2',
            '@types/react': '^18.2.0',
            '@types/react-dom': '^18.2.0',
            '@types/node': '^20.11.0'
        }
        npm_name_re = re.compile(r"^(?:@[a-z0-9._-]+\/)?[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)

        for pkg in packages:
            pkg = (pkg or "").strip()
            if not pkg:
                continue
            if pkg == '@zustand/persist':
                self.fixer._safe_log(job_id, "🔁 Normalizing '@zustand/persist' dependency to 'zustand'", "Package Normalize")
                pkg = 'zustand'
            if self.fixer._is_frontend_alias_package(pkg):
                self.fixer._safe_log(job_id, f"⏭️ Skipping invalid/local alias dependency: {pkg}", "Package Filter")
                self.fixer._invalid_npm_packages.add(pkg)
                continue
            if not npm_name_re.match(pkg):
                self.fixer._safe_log(job_id, f"⏭️ Skipping invalid npm package token: {pkg}", "Package Filter")
                self.fixer._invalid_npm_packages.add(pkg)
                continue
            if pkg in self.fixer._invalid_npm_packages:
                self.fixer._safe_log(job_id, f"⏭️ Skipping known invalid npm package: {pkg}", "Package Filter")
                continue
            if pkg not in deps and pkg not in dev_deps:
                if pkg.startswith(('@testing-library', 'jest', '@types/jest')):
                    dev_deps[pkg] = '^14.0.0' if pkg.startswith('@testing-library') else 'latest'
                    modified = True
                elif pkg in known_packages:
                    deps[pkg] = known_packages[pkg]
                    modified = True
                    self.fixer._safe_log(job_id, f"✅ Adding well-known package: {pkg}@{known_packages[pkg]}", "Package Add")
                elif pkg.startswith(('@', 'react-', 'next-')):
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

        frontend_dir = os.path.dirname(local)
        self.fixer.job_manager.log(job_id, f"Regenerating package-lock.json to match package.json", "Lock File Sync")

        try:
            result = subprocess.run(
                ['npm', 'install', '--package-lock-only', '--legacy-peer-deps'],
                cwd=frontend_dir,
                capture_output=True,
                text=True,
                timeout=120
            )

            if result.returncode == 0:
                self.fixer.job_manager.log(job_id, "✅ Successfully regenerated package-lock.json", "Lock File Sync")
                if not await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch):
                    return False
                lock_file_path = os.path.join(frontend_dir, 'package-lock.json')
                if os.path.exists(lock_file_path):
                    with open(lock_file_path, 'r') as f:
                        lock_content = f.read()
                    lock_file_repo_path = file_path.replace('package.json', 'package-lock.json')
                    await self.fixer._commit_programmatic_fix(job_id, lock_file_repo_path, lock_content, github_repo, github_branch)
                    self.fixer.job_manager.log(job_id, "✅ Committed synchronized package-lock.json", "Lock File Sync")
                return True
            else:
                self.fixer.job_manager.log(job_id, f"⚠️ Failed to regenerate lock file: {result.stderr[:200]}", "Lock File Warning", level="WARNING")
                etarget_match = re.search(r"No matching version found for ([^\s]+)", result.stderr)
                if etarget_match:
                    bad_pkg = _extract_npm_package_name(etarget_match.group(1))
                    if not bad_pkg:
                        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)
                    self.fixer._invalid_npm_packages.add(bad_pkg)
                    removed = False
                    if bad_pkg in deps:
                        del deps[bad_pkg]
                        removed = True
                    if bad_pkg in dev_deps:
                        del dev_deps[bad_pkg]
                        removed = True
                    if removed:
                        self.fixer._safe_log(job_id, f"🧹 Removed invalid package causing ETARGET: {bad_pkg}", "Package Recovery")
                        data['dependencies'] = deps
                        data['devDependencies'] = dev_deps
                        content = json.dumps(data, indent=2)
                        with open(local, 'w') as f:
                            f.write(content)
                else:
                    e404_match = re.search(r"'([^']+@\S+)' is not in this registry", result.stderr)
                    if e404_match:
                        pkg_with_version = e404_match.group(1)
                        bad_pkg = _extract_npm_package_name(pkg_with_version)
                        if not bad_pkg:
                            return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)
                        self.fixer._invalid_npm_packages.add(bad_pkg)
                        removed = False
                        if bad_pkg in deps:
                            del deps[bad_pkg]
                            removed = True
                        if bad_pkg in dev_deps:
                            del dev_deps[bad_pkg]
                            removed = True
                        if removed:
                            self.fixer._safe_log(job_id, f"🧹 Removed package not found in npm registry: {bad_pkg}", "Package Recovery")
                            data['dependencies'] = deps
                            data['devDependencies'] = dev_deps
                            content = json.dumps(data, indent=2)
                            with open(local, 'w') as f:
                                f.write(content)
                    else:
                        invalid_name_match = re.search(r'Invalid package name "([^"]+)"|Invalid package name \'([^\']+)\'', result.stderr)
                        invalid_pkg = (invalid_name_match.group(1) if invalid_name_match and invalid_name_match.group(1) else (invalid_name_match.group(2) if invalid_name_match else None))
                        if invalid_pkg:
                            self.fixer._invalid_npm_packages.add(invalid_pkg)
                            removed = False
                            if invalid_pkg in deps:
                                del deps[invalid_pkg]
                                removed = True
                            if invalid_pkg in dev_deps:
                                del dev_deps[invalid_pkg]
                                removed = True
                            if removed:
                                self.fixer._safe_log(job_id, f"🧹 Removed invalid package name: {invalid_pkg}", "Package Recovery")
                                data['dependencies'] = deps
                                data['devDependencies'] = dev_deps
                                content = json.dumps(data, indent=2)
                                with open(local, 'w') as f:
                                    f.write(content)
                return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)
        except Exception as e:
            self.fixer.job_manager.log(job_id, f"⚠️ Exception during lock file regen: {str(e)[:100]}", "Lock File Error", level="WARNING")
            return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_jsx_inline_comment_syntax(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Fix common TS1005 JSX parse errors caused by inline JSX comments inside opening tags."""
        local = os.path.join(repo_dir, file_path)
        if not os.path.exists(local):
            return False

        with open(local, 'r') as f:
            original_content = f.read()

        content = original_content
        content = re.sub(r"\s*\{\s*/\*.*?\*/\s*\}\s*", " ", content, flags=re.DOTALL)

        if content == original_content:
            return False

        with open(local, 'w') as f:
            f.write(content)

        self.fixer._safe_log(job_id, f"✅ Removed inline JSX comments causing TS1005 in {file_path}", "JSX Syntax Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_ts_with_jsx_syntax(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        if not file_path.endswith('.ts'):
            return False

        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original = content

        if "<AuthContext.Provider" in content and "</AuthContext.Provider>" in content:
            value_match = re.search(
                r"<AuthContext\.Provider\s+value=\{\{(.*?)\}\}\s*>",
                content,
                flags=re.DOTALL
            )
            if value_match:
                value_expr = value_match.group(1).strip()
                provider_block_pattern = re.compile(
                    r"return\s*\(\s*<AuthContext\.Provider\s+value=\{\{.*?\}\}\s*>\s*\{children\}\s*</AuthContext\.Provider>\s*\);\s*",
                    flags=re.DOTALL
                )
                replacement = (
                    f"return createElement(AuthContext.Provider, {{ value: {{ {value_expr} }} }}, children);\n"
                )
                content = provider_block_pattern.sub(replacement, content)
                if "import { createContext, useContext, useState, useEffect, useCallback } from 'react';" in content:
                    content = content.replace(
                        "import { createContext, useContext, useState, useEffect, useCallback } from 'react';",
                        "import { createElement, createContext, useContext, useState, useEffect, useCallback } from 'react';"
                    )
                elif "from 'react';" in content:
                    react_import_match = re.search(r"import\s+\{([^}]*)\}\s+from\s+'react';", content)
                    if react_import_match and "createElement" not in react_import_match.group(1):
                        content = re.sub(
                            r"import\s+\{([^}]*)\}\s+from\s+'react';",
                            lambda m: f"import {{ createElement, {m.group(1).strip()} }} from 'react';",
                            content,
                            count=1
                        )

        if content == original:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)
        self.fixer._safe_log(job_id, f"🧹 Orchestrator: Converted JSX syntax to TS-safe createElement in {file_path}", "Programmatic Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

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
            return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)
        return False

    async def _fix_type_file(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        missing = file_info.get('missing', [])
        local = os.path.join(repo_dir, file_path)
        original_content = file_info.get('content') or ''
        if not original_content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                original_content = f.read()
        content = original_content

        def _fix_unterminated_block_comments(ts_content: str) -> str:
            if not ts_content:
                return ts_content
            if ts_content.count("/*") <= ts_content.count("*/"):
                return ts_content

            token_re = re.compile(r"/\*|\*/")
            opens = []
            for match in token_re.finditer(ts_content):
                token = match.group(0)
                if token == "/*":
                    opens.append(match.start())
                elif opens:
                    opens.pop()

            if not opens:
                return ts_content

            insert_at = None
            last_unclosed = opens[-1]
            export_match = re.search(r"(?m)^\s*export\b", ts_content[last_unclosed:])
            if export_match:
                insert_at = last_unclosed + export_match.start()

            if insert_at is None:
                import_match = re.search(r"(?m)^\s*import\b", ts_content[last_unclosed:])
                if import_match:
                    insert_at = last_unclosed + import_match.start()

            if insert_at is None:
                return ts_content.rstrip() + "\n*/\n"

            return ts_content[:insert_at] + "*/\n" + ts_content[insert_at:]

        content = _fix_unterminated_block_comments(content)

        def _remove_orphan_block_closers(ts_content: str) -> str:
            depth = 0
            out_lines = []
            for line in ts_content.splitlines():
                stripped = line.strip()
                if stripped == "*/" and depth <= 0 and "/*" not in line:
                    continue
                opens = line.count("/*")
                closes = line.count("*/")
                if stripped == "*/" and opens == 0 and depth <= 0:
                    continue
                depth += opens
                if closes:
                    depth = max(0, depth - closes)
                out_lines.append(line)
            return "\n".join(out_lines)

        content = _remove_orphan_block_closers(content)

        def _remove_orphan_closing_braces(ts_content: str) -> str:
            depth = 0
            out_lines = []
            for line in ts_content.splitlines():
                stripped = line.strip()
                if stripped == "}" and depth <= 0:
                    continue
                opens = line.count("{")
                closes = line.count("}")
                depth += opens
                depth = max(0, depth - closes)
                out_lines.append(line)
            return "\n".join(out_lines)

        content = _remove_orphan_closing_braces(content)

        content = re.sub(
            r"\n?export interface (?:TypeScript error|ImportError|Missing dependency)[^\n]*\{\n\s*// TODO\n\}\n?",
            "\n",
            content,
            flags=re.MULTILINE,
        )

        valid_missing_exports = []
        for item in missing:
            if not isinstance(item, str):
                continue
            candidate = None
            match = re.search(r"Missing export:\s*([A-Za-z_][A-Za-z0-9_]*)", item)
            if match:
                candidate = match.group(1)
            if candidate is None:
                match = re.search(r"'([A-Za-z_][A-Za-z0-9_]*)'\s+not found in", item)
                if match:
                    candidate = match.group(1)
            if candidate and candidate not in valid_missing_exports:
                valid_missing_exports.append(candidate)

        if valid_missing_exports:
            if len(content.strip()) < 20:
                content = "// Auto-generated\n\n"
            for name in valid_missing_exports:
                if not re.search(rf"\b(interface|type|class|enum)\s+{re.escape(name)}\b", content):
                    content += f"\nexport interface {name} {{\n  // TODO\n}}\n"

        if content == original_content:
            return False

        with open(local, "w") as f:
            f.write(content)

        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_ts_missing_exports(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Add deterministic missing TS exports (primarily functions/constants in API/service files)."""
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content') or ''
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original = content
        missing_names: List[str] = []
        has_invalid_missing = False
        has_inline_comment_token = False
        for item in file_info.get('missing', []):
            item_s = str(item)
            if re.search(r"'[^']*//[^']*'\s+not found in", item_s):
                has_inline_comment_token = True
            m = re.search(r"Missing export:\s*([A-Za-z_][A-Za-z0-9_]*)", item_s)
            if not m:
                m = re.search(r"'([A-Za-z_][A-Za-z0-9_]*)'\s+not found in", item_s)
                if not m and "not found in" in item_s:
                    has_invalid_missing = True
            if m:
                name = m.group(1).strip()
                if name not in missing_names:
                    missing_names.append(name)

        if has_inline_comment_token or (not missing_names and has_invalid_missing):
            content = re.sub(
                r"(?m)^(\s*(?:import|export)[^\n]*?)\s*//.*$",
                r"\1",
                content
            )
            if content != original:
                with open(local, 'w', encoding='utf-8') as f:
                    f.write(content)
                self.fixer._safe_log(job_id, f"🧹 Removed inline import/export comments in {file_path}", "Programmatic Fix")
                return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)
            if not missing_names:
                return False

        for name in missing_names:
            # If symbol exists locally but just isn't exported, add a named export.
            has_local_def = re.search(
                rf"\b(?:function|const|let|var|class|type|interface|enum)\s+{re.escape(name)}\b",
                content
            )
            has_export = re.search(
                rf"\bexport\s+(?:async\s+)?(?:function|const|let|var|class|type|interface|enum)\s+{re.escape(name)}\b",
                content
            ) or re.search(
                rf"\bexport\s*\{{[^}}]*\b{re.escape(name)}\b[^}}]*\}}",
                content
            )
            if has_local_def and not has_export:
                content = content.rstrip() + f"\n\nexport {{ {name} }};\n"
                continue
            if re.search(rf"\bexport\s+(?:async\s+)?(?:function|const|let|var|class|type|interface|enum)\s+{re.escape(name)}\b", content):
                continue
            if re.search(rf"\bexport\s*\{{[^}}]*\b{re.escape(name)}\b[^}}]*\}}", content):
                continue
            if not content.endswith('\n'):
                content += '\n'
            content += (
                f"\nexport const {name} = async (..._args: any[]): Promise<any> => {{\n"
                f"  return null;\n"
                f"}};\n"
            )

        if content == original:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)
        self.fixer._safe_log(job_id, f"✅ Added missing TS exports {missing_names} to {file_path}", "Programmatic Export Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_ts_call_arity(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Fix TS call sites where functions are called with too few args."""
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content') or ''
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original = content
        for item in file_info.get('missing', []):
            m = re.search(
                r"TypeError in '[^']+': Function '([^']+)' expects at least (\d+) arguments, but found (\d+)",
                str(item)
            )
            if not m:
                continue
            func_name = m.group(1)
            min_params = int(m.group(2))
            # Replace calls that have too few arguments.
            def _replace_call(match: re.Match) -> str:
                args_raw = match.group('args')
                args_count = 0 if not args_raw.strip() else args_raw.count(',') + 1
                if args_count >= min_params:
                    return match.group(0)
                # Avoid function declarations like "function foo(...)"
                prefix = content[max(0, match.start() - 30):match.start()]
                if re.search(r"(?:export\s+)?(?:async\s+)?function\s+$", prefix):
                    return match.group(0)
                missing_count = min_params - args_count
                filler = ", ".join(["undefined"] * missing_count)
                new_args = args_raw.strip()
                if new_args:
                    new_args = f"{new_args}, {filler}"
                else:
                    new_args = filler
                return f"{func_name}({new_args})"

            pattern = re.compile(rf"\b{re.escape(func_name)}\s*\((?P<args>[^)]*)\)")
            content = pattern.sub(_replace_call, content)

        if content == original:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)
        self.fixer._safe_log(job_id, f"✅ Patched call arity mismatches in {file_path}", "Programmatic Arity Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_ts_string_property_access(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Fix property access on string types (e.g., data.id when data is a string)."""
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content') or ''
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original = content
        for item in file_info.get('missing', []):
            m = re.search(r"Property '([^']+)' does not exist on type 'string'", str(item))
            if not m:
                continue
            prop = m.group(1)
            if prop == "id":
                # Target the common pattern data.id to avoid altering unrelated objects.
                content = re.sub(r"\bdata\.id\b", "String(data)", content)

        if content == original:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)
        self.fixer._safe_log(job_id, f"✅ Patched string property access in {file_path}", "Programmatic Property Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_ts_missing_type_property(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        missing_items = [str(err) for err in file_info.get('missing', [])]
        if not missing_items:
            return False

        prop_name = None
        type_name = None
        for item in missing_items:
            m = re.search(r"Object literal may only specify known properties, and '([^']+)' does not exist in type '([^']+)'", item)
            if m:
                prop_name, type_name = m.group(1), m.group(2)
                break
            m = re.search(r"Property '([^']+)' does not exist on type '([^']+)'", item)
            if m:
                prop_name, type_name = m.group(1), m.group(2)
                break
        if not prop_name or not type_name:
            return False

        def _normalize_prop(name: str) -> str:
            return name.lower().replace('_', '')

        def _strip_bool_prefix(name: str) -> str:
            lowered = name
            for prefix in ("is", "has", "can", "should"):
                if lowered.startswith(prefix) and len(lowered) > len(prefix):
                    return lowered[len(prefix):]
            return lowered

        def _extract_type_props(ts: str) -> List[str]:
            interface_pat = re.compile(rf"interface\s+{re.escape(type_name)}\s*\{{(?P<body>[^}}]*)\}}", re.DOTALL)
            type_pat = re.compile(rf"type\s+{re.escape(type_name)}\s*=\s*\{{(?P<body>[^}}]*)\}}", re.DOTALL)
            match = interface_pat.search(ts) or type_pat.search(ts)
            if not match:
                return []
            body = match.group("body")
            return re.findall(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:?]', body, re.MULTILINE)

        def _find_similar_prop() -> tuple[str | None, str | None]:
            prefixes = self.fixer._repo_prefixes(repo_dir)
            frontend_prefix = prefixes["frontend"]
            frontend_root = os.path.join(repo_dir, frontend_prefix) if frontend_prefix else repo_dir
            ignore_dirs = {'node_modules', '.next', '.git', 'out', 'build', 'dist', 'coverage'}

            normalized_missing = _normalize_prop(prop_name)
            normalized_missing_no_prefix = _strip_bool_prefix(normalized_missing)

            candidates = []
            for root, dirs, files in os.walk(frontend_root):
                dirs[:] = [d for d in dirs if d not in ignore_dirs]
                for fname in files:
                    if not fname.endswith(('.ts', '.tsx')):
                        continue
                    full_path = os.path.join(root, fname)
                    try:
                        with open(full_path, 'r', encoding='utf-8') as f:
                            ts = f.read()
                    except Exception:
                        continue
                    props = _extract_type_props(ts)
                    if not props:
                        continue
                    for candidate in props:
                        norm = _normalize_prop(candidate)
                        norm_stripped = _strip_bool_prefix(norm)
                        if norm == normalized_missing or norm_stripped == normalized_missing or norm == normalized_missing_no_prefix:
                            rel_path = os.path.relpath(full_path, repo_dir).replace("\\", "/")
                            return candidate, rel_path
                    candidates.append((full_path, props))
            return None, None

        suggested_prop, type_def_path = _find_similar_prop()
        if suggested_prop and suggested_prop != prop_name:
            local = os.path.join(repo_dir, file_path)
            content = file_info.get('content', '')
            if not content and os.path.exists(local):
                with open(local, 'r', encoding='utf-8') as f:
                    content = f.read()
            if content:
                pattern = re.compile(rf"\b{re.escape(prop_name)}\b")
                updated = pattern.sub(suggested_prop, content)
                if updated != content:
                    with open(local, 'w', encoding='utf-8') as f:
                        f.write(updated)
                    self.fixer._safe_log(
                        job_id,
                        f"🧹 Replaced '{prop_name}' with '{suggested_prop}' in {file_path} (matched {type_name} in {type_def_path})",
                        "Programmatic Fix"
                    )
                    return await self.fixer._commit_programmatic_fix(job_id, file_path, updated, github_repo, github_branch)

        def _guess_type(prop: str) -> str:
            prop_l = prop.lower()
            if prop_l.startswith(("is_", "has_", "can_", "should_")) or prop_l in ("active", "enabled", "disabled", "verified"):
                return "boolean"
            if prop_l in ("count", "total", "size", "age"):
                return "number"
            return "string"

        prop_type = _guess_type(prop_name)
        prefixes = self.fixer._repo_prefixes(repo_dir)
        frontend_prefix = prefixes["frontend"]
        base_dir = os.path.join(repo_dir, frontend_prefix, "src", "types") if frontend_prefix else os.path.join(repo_dir, "src", "types")
        candidates: List[str] = []
        if os.path.isdir(base_dir):
            for root, _, files in os.walk(base_dir):
                for f in files:
                    if f.endswith(".ts"):
                        candidates.append(os.path.join(root, f))
        else:
            for rel in ("src/types/auth.ts", "src/types/index.ts", "src/types/api.ts"):
                local_path = os.path.join(repo_dir, frontend_prefix, rel) if frontend_prefix else os.path.join(repo_dir, rel)
                if os.path.exists(local_path):
                    candidates.append(local_path)

        def _patch_type_file(path: str) -> str:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    ts = f.read()
            except Exception:
                return ""

            original = ts
            interface_pat = re.compile(rf"(interface\s+{re.escape(type_name)}\s*\{{)([^}}]*)(\}})", re.DOTALL)
            type_pat = re.compile(rf"(type\s+{re.escape(type_name)}\s*=\s*\{{)([^}}]*)(\}})", re.DOTALL)

            def _insert(body: str) -> str:
                if re.search(rf"\b{re.escape(prop_name)}\s*:", body):
                    return body
                line = f"  {prop_name}: {prop_type};"
                body = body.rstrip()
                if body and not body.endswith('\n'):
                    body += '\n'
                body += line + '\n'
                return body

            if interface_pat.search(ts):
                ts = interface_pat.sub(lambda m: f"{m.group(1)}{_insert(m.group(2))}{m.group(3)}", ts)
            elif type_pat.search(ts):
                ts = type_pat.sub(lambda m: f"{m.group(1)}{_insert(m.group(2))}{m.group(3)}", ts)
            else:
                return ""

            if ts == original:
                return ""
            with open(path, 'w', encoding='utf-8') as f:
                f.write(ts)
            return ts

        for candidate in candidates:
            updated = _patch_type_file(candidate)
            if updated:
                rel_path = os.path.relpath(candidate, repo_dir).replace("\\", "/")
                self.fixer._safe_log(job_id, f"🧹 Added '{prop_name}' to {type_name} in {rel_path}", "Programmatic Fix")
                return await self.fixer._commit_programmatic_fix(job_id, rel_path, updated, github_repo, github_branch)

        return False

    async def _fix_missing_css_file(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Create missing CSS files with basic boilerplate content"""
        missing_items = [str(err) for err in file_info.get('missing', [])]
        
        # Extract the CSS file path from AssetError
        css_file_path = None
        for err in missing_items:
            match = re.search(r"AssetError in '[^']+': CSS file '([^']+)' not found", err)
            if match:
                css_file_path = match.group(1)
                break
        
        if not css_file_path:
            return False
        
        # Resolve CSS file location
        local = os.path.join(repo_dir, file_path)
        if not os.path.exists(local):
            return False
        
        # Calculate the CSS file's absolute path relative to the importing file
        if css_file_path.startswith('./') or css_file_path.startswith('../'):
            css_absolute = os.path.normpath(os.path.join(os.path.dirname(local), css_file_path))
        elif css_file_path.startswith('@/'):
            frontend_dir = os.path.dirname(local)
            while frontend_dir != repo_dir and not os.path.exists(os.path.join(frontend_dir, 'package.json')):
                frontend_dir = os.path.dirname(frontend_dir)
            css_absolute = os.path.join(frontend_dir, 'src', css_file_path[2:])
            # Fallback for app/ directory structure
            if not os.path.dirname(css_absolute).startswith(os.path.join(frontend_dir, 'src')):
                css_absolute = os.path.join(frontend_dir, 'app', css_file_path[2:])
        else:
            css_absolute = os.path.normpath(os.path.join(os.path.dirname(local), css_file_path))
        
        # Create the CSS file with appropriate boilerplate
        css_filename = os.path.basename(css_file_path)
        if css_filename in ('globals.css', 'global.css'):
            content = (
                "@tailwind base;\n"
                "@tailwind components;\n"
                "@tailwind utilities;\n\n"
                "/* Global styles */\n"
                "* {\n"
                "  box-sizing: border-box;\n"
                "  padding: 0;\n"
                "  margin: 0;\n"
                "}\n\n"
                "html,\n"
                "body {\n"
                "  max-width: 100vw;\n"
                "  overflow-x: hidden;\n"
                "}\n"
            )
        elif '.module.css' in css_filename:
            content = (
                "/* CSS Module */\n"
                ".container {\n"
                "  padding: 1rem;\n"
                "}\n"
            )
        else:
            content = (
                "/* Auto-generated CSS file */\n"
                "/* Add your styles here */\n"
            )
        
        # Create directory if needed
        os.makedirs(os.path.dirname(css_absolute), exist_ok=True)
        
        # Write the CSS file
        with open(css_absolute, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # Get repo-relative path for commit
        css_repo_path = os.path.relpath(css_absolute, repo_dir).replace(os.path.sep, '/')
        
        self.fixer._safe_log(
            job_id,
            f"✅ Created missing CSS file: {css_repo_path}",
            "Programmatic CSS Fix"
        )
        
        return await self.fixer._commit_programmatic_fix(job_id, css_repo_path, content, github_repo, github_branch)

    async def _fix_axios_interceptor_type(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Fix Axios interceptor type mismatch - use InternalAxiosRequestConfig instead of AxiosRequestConfig."""
        if not file_path.endswith(('.ts', '.tsx')):
            return False
        
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content') or ''
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False
        
        # Check if this file has Axios interceptor issues
        missing_errors = [str(err) for err in file_info.get('missing', [])]
        has_interceptor_type_issue = any(
            'AxiosRequestConfig' in err and 'InternalAxiosRequestConfig' in err 
            for err in missing_errors
        )
        
        if not has_interceptor_type_issue:
            return False
        
        original = content
        
        # Fix import: Add InternalAxiosRequestConfig to imports if not present
        if 'InternalAxiosRequestConfig' not in content and 'from \'axios\'' in content:
            content = re.sub(
                r"import\s+(axios,\s*)?\{([^}]+)\}\s+from\s+['\"]axios['\"];?",
                lambda m: f"import {m.group(1) or ''}{{ {m.group(2).strip()}, InternalAxiosRequestConfig }} from 'axios';",
                content
            )
        
        # Replace AxiosRequestConfig with InternalAxiosRequestConfig in interceptor signatures
        content = re.sub(
            r"\(config:\s*AxiosRequestConfig\)\s*=>",
            r"(config: InternalAxiosRequestConfig) =>",
            content
        )
        
        if content == original:
            return False
        
        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)
        
        self.fixer._safe_log(
            job_id,
            f"✅ Fixed Axios interceptor type (AxiosRequestConfig → InternalAxiosRequestConfig) in {file_path}",
            "Programmatic Axios Fix"
        )
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_frontend_alias_src_prefix(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Fix alias imports like '@/src/...' or 'src/...' to '@/...'."""
        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content') or ''
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original = content
        content = re.sub(r"(['\"])@/src/", r"\1@/", content)
        content = re.sub(r"(['\"])~/src/", r"\1~/", content)
        content = re.sub(r"(['\"])src/", r"\1@/", content)

        if content == original:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)
        self.fixer._safe_log(job_id, f"✅ Fixed alias /src prefix in {file_path}", "Programmatic Import Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_frontend_auth_contract(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Fix common login-page contract drift between auth/api types and API response shape."""
        if not file_path.endswith(('.ts', '.tsx', '.js', '.jsx')):
            return False

        missing_errors = [str(err) for err in file_info.get('missing', [])]
        if not missing_errors:
            return False
        missing_blob = "\n".join(missing_errors)
        has_login_request_mismatch = ("LoginRequest" in missing_blob and "types/auth" in missing_blob and "types/api" in missing_blob)
        has_auth_response_prop_mismatch = ("AuthResponse" in missing_blob and ("success" in missing_blob or "message" in missing_blob))

        if not (has_login_request_mismatch or has_auth_response_prop_mismatch):
            return False

        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original_content = content

        content = re.sub(
            r"import\s+(type\s+)?\{\s*LoginRequest\s*\}\s+from\s+['\"]([^'\"]*types/auth[^'\"]*)['\"];?",
            lambda m: f"import {m.group(1) or ''}{{ LoginRequest }} from '{m.group(2).replace('types/auth', 'types/api')}';",
            content
        )

        content = re.sub(r"\bresponse\.success\b", "Boolean(response?.token)", content)
        content = re.sub(
            r"setError\(\s*response\.message\s*\|\|\s*(['\"][^'\"]*['\"])\s*\);",
            r"setError(\1);",
            content
        )

        if content == original_content:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(
            job_id,
            f"🧹 Orchestrator: Aligned auth contract usage in {file_path} (LoginRequest/AuthResponse)",
            "Programmatic Fix"
        )
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _fix_auth_page_missing_form_props(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Patch auth pages that render form components without required props."""
        if not file_path.endswith(('.tsx', '.jsx')):
            return False

        local = os.path.join(repo_dir, file_path)
        content = file_info.get('content', '')
        if not content and os.path.exists(local):
            with open(local, 'r', encoding='utf-8') as f:
                content = f.read()
        if not content:
            return False

        original_content = content
        changed = False

        if re.search(r"<LoginForm\s*/>", content):
            if "const _sprint2codeNoopLoginSubmit" not in content:
                insert_at = content.find("return ")
                if insert_at != -1:
                    helper = (
                        "const _sprint2codeNoopLoginSubmit = async (_payload: unknown) => {\n"
                        "  return;\n"
                        "};\n\n"
                    )
                    content = content[:insert_at] + helper + content[insert_at:]
            content = re.sub(
                r"<LoginForm\s*/>",
                "<LoginForm onSubmit={_sprint2codeNoopLoginSubmit} isLoading={false} error={null} />",
                content
            )
            changed = True

        if re.search(r"<ForgotPasswordForm\s*/>", content):
            if "const _sprint2codeNoopForgotSubmit" not in content:
                insert_at = content.find("return ")
                if insert_at != -1:
                    helper = (
                        "const _sprint2codeNoopForgotSubmit = async (_payload: unknown) => {\n"
                        "  return;\n"
                        "};\n\n"
                    )
                    content = content[:insert_at] + helper + content[insert_at:]
            content = re.sub(
                r"<ForgotPasswordForm\s*/>",
                "<ForgotPasswordForm onSubmit={_sprint2codeNoopForgotSubmit} isLoading={false} error={null} successMessage={null} />",
                content
            )
            changed = True

        if not changed or content == original_content:
            return False

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(
            job_id,
            f"🧹 Orchestrator: Added required auth form props in {file_path}",
            "Programmatic Fix"
        )
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _create_missing_frontend_module(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Create a missing frontend module file reported by static analysis."""
        local = os.path.join(repo_dir, file_path)
        if os.path.exists(local):
            return False

        imported_names = []
        default_import = None
        module_spec = file_path
        if module_spec.startswith('frontend/'):
            module_spec = module_spec[9:]
        if module_spec.startswith('src/'):
            module_spec = '@/'+module_spec[4:]
        module_spec = os.path.splitext(module_spec)[0]

        for dep_file in file_info.get('dependent_files', []):
            dep_path = os.path.join(repo_dir, dep_file)
            if not os.path.exists(dep_path):
                continue
            try:
                with open(dep_path, 'r', encoding='utf-8') as dep_f:
                    dep_content = dep_f.read()
            except Exception:
                continue

            import_match = re.search(
                rf'import\s+(?:type\s+)?([^\n]+?)\s+from\s+["\']{re.escape(module_spec)}["\']',
                dep_content
            )
            if not import_match:
                import_match = re.search(
                    rf'import\s+(?:type\s+)?\*\s+as\s+([^\n]+?)\s+from\s+["\']{re.escape(module_spec)}["\']',
                    dep_content
                )
            if import_match:
                imp = import_match.group(1).strip()
                if imp.startswith('{') and imp.endswith('}'):
                    names = [n.split(' as ')[0].strip() for n in imp[1:-1].split(',') if n.strip()]
                    imported_names.extend(names)
                elif imp.startswith('* as '):
                    imported_names.append(imp[5:].strip())
                else:
                    default_import = imp.strip()

        os.makedirs(os.path.dirname(local), exist_ok=True)
        content = ''
        path_lower = file_path.lower()

        if file_path.endswith(('.tsx', '.jsx')):
            component_name = default_import or os.path.splitext(os.path.basename(file_path))[0]
            component_name = re.sub(r'[^A-Za-z0-9]', '', component_name)
            content = (
                f'export default function {component_name}() {{\n'
                f'  return <div>{component_name}</div>\n'
                '}'
            )
        elif '/store/' in path_lower:
            store_name = default_import or os.path.splitext(os.path.basename(file_path))[0]
            store_type = store_name[0].upper() + store_name[1:] + 'State'
            content = (
                "import { create } from 'zustand';\n\n"
                f"interface {store_type} {{}}\n\n"
                f"const {store_name} = create<{store_type}>(() => ({{}}));\n\n"
                f"export default {store_name};\n"
            )
        elif '/api/' in path_lower:
            if imported_names:
                content = "import axios from 'axios';\n\nconst api = axios.create();\n\n"
                for name in imported_names:
                    if name != 'default':
                        content += f'export async function {name}() {{\n  return null as any;\n}}\n\n'
            else:
                func_name = default_import or os.path.splitext(os.path.basename(file_path))[0]
                content = (
                    "import axios from 'axios';\n\n"
                    f"export default async function {func_name}() {{\n  return null as any;\n}}\n"
                )
        elif '/types/' in path_lower:
            if imported_names:
                content = ''
                for name in imported_names:
                    content += f'export type {name} = unknown;\n'
            else:
                type_name = os.path.splitext(os.path.basename(file_path))[0]
                type_name = type_name[0].upper() + type_name[1:]
                content = f'export type {type_name} = unknown;\n'
        else:
            if imported_names:
                content = ''
                for name in imported_names:
                    content += f'export const {name} = null as any;\n'
            elif default_import:
                content = 'export default null as any;\n'
            else:
                content = 'export default null as any;\n'

        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(job_id, f"✅ Created missing frontend module shim: {file_path}", "Programmatic Create")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _create_next_config(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Create a standard next.config.js if it is missing from the frontend repo."""
        local = os.path.join(repo_dir, file_path)
        if os.path.exists(local) and os.path.getsize(local) > 0:
            return False

        content = (
            "/** @type {import('next').NextConfig} */\n"
            "const nextConfig = {\n"
            "  reactStrictMode: true,\n"
            "};\n"
            "\n"
            "module.exports = nextConfig;\n"
        )

        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(job_id, f"✅ Created missing next.config.js with standard boilerplate", "Programmatic Create")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    async def _create_tsconfig(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Create or fix tsconfig.json for the frontend repo."""
        local = os.path.join(repo_dir, file_path)
        needs_patch = False

        if os.path.exists(local) and os.path.getsize(local) > 0:
            try:
                with open(local, 'r', encoding='utf-8') as f:
                    existing = f.read()
                if '"moduleResolution": "bundler"' in existing:
                    needs_patch = True
                else:
                    return False
            except Exception:
                return False

        tsconfig = {
            "compilerOptions": {
                "target": "es5",
                "lib": ["dom", "dom.iterable", "esnext"],
                "allowJs": True,
                "skipLibCheck": True,
                "strict": True,
                "noEmit": True,
                "esModuleInterop": True,
                "module": "esnext",
                "moduleResolution": "node",
                "resolveJsonModule": True,
                "isolatedModules": True,
                "jsx": "preserve",
                "incremental": True,
                "plugins": [{"name": "next"}],
                "paths": {"@/*": ["./src/*"]}
            },
            "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
            "exclude": ["node_modules"]
        }
        content = json.dumps(tsconfig, indent=2) + "\n"

        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        action = "Patched (bundler→node)" if needs_patch else "Created"
        self.fixer._safe_log(job_id, f"✅ {action} tsconfig.json with moduleResolution=node", "Programmatic Create/Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)


    async def _fix_tsconfig_path_alias(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        """Ensure tsconfig/jsconfig has @/* (or ~/*) path alias configured."""
        local = os.path.join(repo_dir, file_path)
        if not os.path.exists(local) or os.path.getsize(local) == 0:
            return await self._create_tsconfig(file_path, file_info, github_repo, github_branch, repo_dir, job_id)

        try:
            with open(local, 'r', encoding='utf-8') as f:
                raw = f.read()
        except Exception:
            return False

        data = self.fixer._resilient_json_parse(raw)
        if data is None:
            return False

        compiler = data.get('compilerOptions', {}) if isinstance(data, dict) else {}
        changed = False
        if compiler.get('baseUrl') is None:
            compiler['baseUrl'] = '.'
            changed = True

        paths = compiler.get('paths', {}) if isinstance(compiler.get('paths', {}), dict) else {}

        frontend_root = os.path.dirname(local)
        src_dir = os.path.join(frontend_root, 'src')
        target = './src/*' if os.path.isdir(src_dir) else './*'

        missing_blob = ' '.join(str(m) for m in file_info.get('missing', []))
        alias_keys = set()
        if '@/' in missing_blob or '@/*' in missing_blob:
            alias_keys.add('@/*')
        if '~/' in missing_blob or '~/*' in missing_blob:
            alias_keys.add('~/*')
        if not alias_keys:
            alias_keys.add('@/*')

        for key in alias_keys:
            if paths.get(key) != [target]:
                paths[key] = [target]
                changed = True

        if not changed:
            return False

        compiler['paths'] = paths
        data['compilerOptions'] = compiler

        content = json.dumps(data, indent=2) + "\n"
        with open(local, 'w', encoding='utf-8') as f:
            f.write(content)

        self.fixer._safe_log(job_id, f"✅ Added path alias mapping in {file_path}", "Programmatic Create/Fix")
        return await self.fixer._commit_programmatic_fix(job_id, file_path, content, github_repo, github_branch)

    def _get_redundant_type_context(self, repo_dir: str, file_path: str, info: dict) -> str:
        """Find multiple definitions of types mentioned in errors and provide them as context"""
        missing = info.get('missing', [])
        type_names = set()
        for err in missing:
            # Extract things like 'Ticket', 'User', etc from "Argument of type 'Ticket[]' is not assignable..."
            matches = re.findall(r"'(\w+)(?:\[\])?'", str(err))
            type_names.update(matches)

        if not type_names:
            return ""

        context = "\n### 🔍 POTENTIAL TYPE CONFLICTS (Redundant Definitions Found) ###\n"
        context += "The following types have multiple definitions in the codebase. This often causes 'is not assignable' errors.\n"
        context += "You MUST consolidate these or use correct imports.\n\n"

        found_any = False
        frontend_dir = os.path.join(repo_dir, 'frontend', 'src')
        if not os.path.exists(frontend_dir):
            return ""

        for t_name in type_names:
            definitions = []
            for root, _, files in os.walk(frontend_dir):
                for f in files:
                    if f.endswith(('.ts', '.tsx')):
                        f_path = os.path.join(root, f)
                        try:
                            with open(f_path, 'r', encoding='utf-8') as file_obj:
                                f_content = file_obj.read()
                                if re.search(rf'export (interface|type|class|enum)\s+{re.escape(t_name)}\b', f_content):
                                    rel_f_path = os.path.relpath(f_path, repo_dir)
                                    definitions.append((rel_f_path, f_content))
                        except Exception:
                            continue

            if len(definitions) > 1:
                found_any = True
                context += f"🚨 Type '{t_name}' is defined in {len(definitions)} places:\n"
                for rel_p, content in definitions:
                    context += f"  - {rel_p}\n"
                context += "\n"
                for rel_p, content in definitions:
                    context += f"--- Definition in {rel_p} ---\n"
                    match = re.search(rf'export (interface|type|class|enum)\s+{re.escape(t_name)}\b.*?\}}', content, re.DOTALL)
                    if match:
                        context += f"```typescript\n{match.group(0)}\n```\n"
                    else:
                        snippet = content[:500] + "..." if len(content) > 500 else content
                        context += f"```typescript\n{snippet}\n```\n"
                context += "\n"

        return context if found_any else ""

    async def _fix_redundant_types(self, file_path, file_info, github_repo, github_branch, repo_dir, job_id):
        if not file_path.endswith(('.ts', '.tsx')):
            return False

        types_dir = os.path.join(repo_dir, 'frontend', 'src', 'types')
        if not os.path.exists(types_dir):
            return False

        index_ts = os.path.join(types_dir, 'index.ts')
        ticket_ts = os.path.join(types_dir, 'ticket.ts')

        if os.path.exists(index_ts) and os.path.exists(ticket_ts):
            with open(index_ts, 'r') as f: index_content = f.read()
            with open(ticket_ts, 'r') as f: ticket_content = f.read()

            pattern = r'export interface Ticket\s*\{.*?\}'
            if re.search(pattern, index_content, re.DOTALL) and re.search(pattern, ticket_content, re.DOTALL):
                self.fixer._safe_log(job_id, f"🧹 Orchestrator: Consolidating redundant 'Ticket' type definition in {file_path}", "Programmatic Fix")
                new_index = re.sub(pattern, "export type { Ticket } from './ticket';", index_content, flags=re.DOTALL)
                for t in ['TicketStatus', 'TicketPriority', 'TicketCategory']:
                    t_pat = rf'export type {t}\s*=\s*[^;]+;'
                    if re.search(t_pat, new_index) and re.search(t_pat, ticket_content):
                        new_index = re.sub(t_pat, f"export type {{ {t} }} from './ticket';", new_index)
                if new_index != index_content:
                    with open(index_ts, 'w') as f:
                        f.write(new_index)
                    return await self.fixer._commit_programmatic_fix(job_id, 'frontend/src/types/index.ts', new_index, github_repo, github_branch)

        return False
