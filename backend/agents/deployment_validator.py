import os
import subprocess
import re
import ast
import sys
from typing import List, Dict, Any
from log_config import logger

class DeploymentValidator:
    def __init__(self):
        pass

    def _detect_frontend_root(self, repo_dir: str) -> str | None:
        """Find frontend root directory (frontend/ subdir or repo root)."""
        frontend_dir = os.path.join(repo_dir, 'frontend')
        if os.path.exists(frontend_dir):
            return frontend_dir
        if os.path.exists(os.path.join(repo_dir, 'package.json')):
            return repo_dir
        if (
            os.path.exists(os.path.join(repo_dir, 'app'))
            or os.path.exists(os.path.join(repo_dir, 'pages'))
            or os.path.exists(os.path.join(repo_dir, 'src', 'app'))
            or os.path.exists(os.path.join(repo_dir, 'src', 'pages'))
        ):
            return repo_dir
        return None

    def validate_all(self, repo_dir: str) -> List[str]:
        """Perform all validations on the repository - PRODUCTION CODE ONLY"""
        import_errors = self._validate_python_imports(repo_dir)
        dependency_errors = self._validate_frontend_dependencies(repo_dir)
        typescript_errors = self._validate_typescript_types(repo_dir)
        property_errors = self._validate_typescript_properties(repo_dir)
        concat_errors = self._validate_concatenated_files(repo_dir)
        
        # CRITICAL: Filter out test file errors - they don't affect production deployment
        all_errors = import_errors + dependency_errors + typescript_errors + property_errors + concat_errors
        production_errors = [e for e in all_errors if not self._is_test_file_error(e)]
        
        filtered_count = len(all_errors) - len(production_errors)
        if filtered_count > 0:
            logger.info(f"Filtered out {filtered_count} test file errors - focusing on production code")
        
        return production_errors
    
    def _is_test_file_error(self, error: str) -> bool:
        """Check if error is from a test file"""
        test_patterns = [
            '__tests__/',
            '__mocks__/',
            '.test.ts',
            '.test.tsx',
            '.test.js',
            '.test.jsx',
            '.spec.ts',
            '.spec.tsx',
            '/tests/',
            'tests.',  # Catches tests.conftest.py, tests.test_*.py
            'conftest.py',  # pytest configuration file
            'test_',
            '_test.',
            'jest.config',
            'jest.setup',
            'playwright.config',
            'vitest.config'
        ]
        return any(pattern in error for pattern in test_patterns)

    def _validate_concatenated_files(self, repo_dir: str) -> List[str]:
        """Detect source files that contain AI multi-file output markers like --- and FILE_PATH:"""
        errors = []
        for root, dirs, files in os.walk(repo_dir):
            if any(p in root for p in ["node_modules", "venv", ".git", ".next"]):
                continue
            for file in files:
                if not file.endswith(('.ts', '.tsx', '.js', '.jsx', '.py')):
                    continue
                    
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                        
                    for i, line in enumerate(lines):
                        stripped = line.strip()
                        # Allow normal docstrings/comments but catch AI markers that dictate file paths
                        if stripped.startswith('FILE_PATH:') or (stripped == '---' and i > 0 and any('FILE_PATH:' in pre_line for pre_line in lines[max(0, i-5):i+5])):
                            rel_path = os.path.relpath(file_path, repo_dir)
                            errors.append(f"AI Concatenation Error in '{rel_path}' at line {i+1}: File contains embedded 'FILE_PATH:' or '---' markers and needs to be split.")
                            break
                except Exception:
                    pass
        return errors

    def _validate_typescript_properties(self, repo_dir: str) -> List[str]:
        """
        Verify property access against defined TypeScript interfaces/types.
        Uses type-flow analysis to catch property naming errors.
        """
        errors = []
        frontend_dir = self._detect_frontend_root(repo_dir)
        if not frontend_dir:
            return []

        if not os.path.exists(os.path.join(frontend_dir, 'node_modules')):
            return ["ENVIRONMENT ERROR: TypeScript property validation skipped because 'node_modules' is missing. Run 'npm install'."]

        types_map = {} # { 'TypeName': { 'prop1', 'prop2', ... } }
        function_return_types = {} # { 'functionName': 'ReturnType' }
        function_param_types = {} # { 'functionName': { 'paramName': 'TypeName' } }

        # 1. Parse type/interface definitions AND function return types AND parameter types
        ignore_dirs = {'node_modules', '.next', '.git', 'out', 'build', 'dist', 'coverage'}
        for root, dirs, files in os.walk(frontend_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                if file.endswith(('.ts', '.tsx')):
                    try:
                        with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                            content = f.read()
                            
                            # Parse type definitions
                            matches = re.finditer(r'(?:interface|type)\s+(?P<name>\w+)\s*=?\s*\{(?P<body>[^}]+)\}', content)
                            for match in matches:
                                name = match.group('name')
                                body = match.group('body')
                                props = re.findall(r'(\w+)\s*[:?]', body)
                                if name not in types_map: types_map[name] = set()
                                types_map[name].update(props)
                            
                            # Parse function return types
                            func_matches = re.finditer(r'(?:export\s+)?(?:async\s+)?(?:function|const)\s+(\w+)[^:]*:\s*Promise<(\w+)>', content)
                            for func_match in func_matches:
                                func_name = func_match.group(1)
                                return_type = func_match.group(2)
                                function_return_types[func_name] = return_type
                            
                            # Parse function parameter types
                            param_matches = re.finditer(r'(?:export\s+)?(?:async\s+)?(?:function|const)\s+(\w+)\s*\(([^)]+)\)', content)
                            for param_match in param_matches:
                                func_name = param_match.group(1)
                                params_str = param_match.group(2)
                                param_list = re.findall(r'(\w+)\s*:\s*(\w+)', params_str)
                                if param_list:
                                    if func_name not in function_param_types:
                                        function_param_types[func_name] = {}
                                    for param_name, type_name in param_list:
                                        function_param_types[func_name][param_name] = type_name
                    except Exception: continue

        # 2. Check property accesses with type-flow analysis
        for root, dirs, files in os.walk(frontend_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                if file.endswith(('.tsx', '.ts')):
                    rel_path = os.path.relpath(os.path.join(root, file), frontend_dir).replace(os.path.sep, '/')
                    try:
                        with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                            content = f.read()
                            lines = content.split('\n')
                            
                            var_types = {}
                            for line in lines:
                                assign_match = re.search(r'(?:const|let|var)\s+(\w+)\s*=\s*(?:await\s+)?(\w+)\(', line)
                                if assign_match:
                                    var_name = assign_match.group(1)
                                    func_name = assign_match.group(2)
                                    if func_name in function_return_types:
                                        var_types[var_name] = function_return_types[func_name]
                                
                                type_annot_match = re.search(r'(\w+)\s*:\s*(\w+)(?:\[\])?', line)
                                if type_annot_match:
                                    var_name = type_annot_match.group(1)
                                    type_name = type_annot_match.group(2)
                                    if type_name in types_map:
                                        var_types[var_name] = type_name
                            
                            property_accesses = re.findall(r'(\w+)\.(\w+)', content)
                            for var_name, prop_name in property_accesses:
                                if prop_name in ['map', 'forEach', 'filter', 'find', 'length', 'includes', 'push', 'pop', 'shift', 'unshift', 'slice', 'splice', 'concat', 'join', 'toString', 'toLowerCase', 'toUpperCase', 'trim', 'split', 'replace', 'match', 'search', 'indexOf', 'lastIndexOf', 'startsWith', 'endsWith', 'substring', 'get', 'set', 'has', 'data', 'status', 'then', 'catch']: 
                                    continue
                                
                                if var_name in var_types:
                                    var_type = var_types[var_name]
                                    if var_type in types_map:
                                        known_props = types_map[var_type]
                                        if prop_name not in known_props:
                                            normalized_prop = prop_name.lower().replace('_', '')
                                            for known_prop in known_props:
                                                normalized_known = known_prop.lower().replace('_', '')
                                                if normalized_prop == normalized_known and prop_name != known_prop:
                                                    errors.append(f"PropertyError in '{rel_path}': '{prop_name}' does not exist on type '{var_type}'. Did you mean '{known_prop}'?")
                                                    break
                            
                            func_calls_with_objects = re.finditer(r'(\w+)\s*\(\s*\{([^}]*)\}\s*\)', content)
                            for func_call_match in func_calls_with_objects:
                                func_name = func_call_match.group(1)
                                obj_props_str = func_call_match.group(2)
                                obj_props = set(re.findall(r'(\w+)\s*:', obj_props_str))
                                
                                if func_name in function_param_types:
                                    param_types = function_param_types[func_name]
                                    if param_types:
                                        first_param_type = list(param_types.values())[0]
                                        if first_param_type in types_map:
                                            expected_props = types_map[first_param_type]
                                            missing_props = expected_props - obj_props
                                            if missing_props:
                                                errors.append(f"PropertyError in '{rel_path}': Object passed to '{func_name}' is missing properties: {', '.join(missing_props)}. Expected type '{first_param_type}' requires: {', '.join(expected_props)}")
                                            for obj_prop in obj_props:
                                                if obj_prop not in expected_props:
                                                    normalized_obj = obj_prop.lower().replace('_', '')
                                                    for expected_prop in expected_props:
                                                        normalized_exp = expected_prop.lower().replace('_', '')
                                                        if normalized_obj == normalized_exp and obj_prop != expected_prop:
                                                            errors.append(f"PropertyError in '{rel_path}': Object passed to '{func_name}' uses '{obj_prop}' but type '{first_param_type}' expects '{expected_prop}'")
                                                            break
                    except Exception: continue
        return list(set(errors))

    def _validate_frontend_dependencies(self, repo_dir: str) -> List[str]:
        """Verify that all frontend imports have dependencies and members exist in source files"""
        import json
        logger.info("Starting Frontend dependency validation...")
        errors = []
        frontend_dir = self._detect_frontend_root(repo_dir)
        if not frontend_dir:
            return []
        
        mandatory_files = {
            'package.json': "Build instructions",
            'next.config.js': "Next.js configuration",
            'tsconfig.json': "TypeScript configuration"
        }
        
        for m_file, m_desc in mandatory_files.items():
            if not os.path.exists(os.path.join(frontend_dir, m_file)):
                if m_file == 'tsconfig.json' and os.path.exists(os.path.join(frontend_dir, 'jsconfig.json')):
                    continue
                errors.append(f"Missing mandatory file: '{m_file}' ({m_desc}) is required")

        # NEW: Check for App Router (app/) or Pages Router (pages/)
        has_app = os.path.exists(os.path.join(frontend_dir, 'app')) or os.path.exists(os.path.join(frontend_dir, 'src', 'app'))
        has_pages = os.path.exists(os.path.join(frontend_dir, 'pages')) or os.path.exists(os.path.join(frontend_dir, 'src', 'pages'))
        if not (has_app or has_pages):
            errors.append("ENVIRONMENT ERROR: Missing mandatory directory: Frontend must have either an 'app' directory (App Router) or 'pages' directory (Pages Router). This is required for Next.js to start.")

        package_json_path = os.path.join(frontend_dir, 'package.json')
        if not os.path.exists(package_json_path):
            return errors
        
        try:
            with open(package_json_path, 'r') as f:
                package_data = json.load(f)
                all_dependencies = set(package_data.get('dependencies', {}).keys()).union(set(package_data.get('devDependencies', {}).keys()))
                scripts = package_data.get('scripts', {})
                if 'build' not in scripts: errors.append("Missing script: 'build' in package.json")
        except Exception as e:
            errors.append(f"Invalid package.json: JSON parsing failed ({e}). Fix the syntax errors.")
            return errors # Cannot proceed without valid package.json
        
        # CRITICAL: If package.json exists, node_modules should be present for validation to be meaningful.
        node_modules_path = os.path.join(frontend_dir, 'node_modules')
        if not os.path.exists(node_modules_path) and all_dependencies:
            errors.append("Missing 'node_modules': Dependencies are defined in package.json but not installed. Run 'npm install'.")
        elif os.path.exists(node_modules_path) and all_dependencies:
            # Check for a critical package to ensure it's not an empty node_modules
            critical_pkg = 'next' if 'next' in all_dependencies else (list(all_dependencies)[0] if all_dependencies else None)
            if critical_pkg and not os.path.exists(os.path.join(node_modules_path, critical_pkg)):
                 errors.append(f"Incomplete 'node_modules': Package '{critical_pkg}' not found. Dependency installation may have failed.")
        
        file_map = {}
        ignore_dirs = {'node_modules', '.next', '.git', 'out', 'build', 'dist', 'coverage'}
        for root, dirs, files in os.walk(frontend_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                if file.endswith(('.ts', '.tsx', '.js', '.jsx')):
                    rel_path = os.path.relpath(os.path.join(root, file), frontend_dir).replace(os.path.sep, '/')
                    file_map[rel_path] = os.path.join(root, file)
                    file_map[rel_path.rsplit('.', 1)[0]] = os.path.join(root, file)

        package_import_pattern = r'import\s+.*\s+from\s+["\']([^./][^"\']+)["\']'
        internal_import_pattern = r'import\s+{(?P<members>[^}]+)}\s+from\s+["\'](?P<path>[^"\']+)["\']'
        def _is_local_alias_import(spec: str) -> bool:
            """True when an import spec is a project alias/path, not an npm package."""
            s = (spec or "").strip()
            if not s:
                return False
            if s.startswith(('@/','~/','#/','./','../','/')) or s in ('@', '~', '#'):
                return True
            # Sprint2Code projects commonly use these as TS path aliases.
            if s == '@types' or s.startswith('@types/') or s.startswith('@api/'):
                return True
            return False

        node_builtin_modules = {
            'assert', 'buffer', 'child_process', 'cluster', 'console', 'constants', 'crypto',
            'dgram', 'dns', 'domain', 'events', 'fs', 'http', 'https', 'module', 'net',
            'os', 'path', 'process', 'punycode', 'querystring', 'readline', 'repl', 'stream',
            'string_decoder', 'timers', 'tls', 'tty', 'url', 'util', 'v8', 'vm', 'worker_threads', 'zlib'
        }
        function_sigs = {}

        for rel_path, full_path in file_map.items():
            # Skip extension-less duplicate keys (added for import resolution only).
            if not rel_path.endswith(('.ts', '.tsx', '.js', '.jsx')): continue
            if not os.path.isfile(full_path): continue
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    arrow_matches = re.finditer(r'export\s+const\s+(?P<name>\w+)\s*=\s*(?:async\s+)?\((?P<params>[^)]*)\)\s*=>', content)
                    for m in arrow_matches:
                        name = m.group('name')
                        params = m.group('params').split(',')
                        min_params = len([p for p in params if p.strip() and '=' not in p and '?' not in p])
                        function_sigs[name] = min_params

                    func_matches = re.finditer(r'export\s+(?:async\s+)?function\s+(?P<name>\w+)\s*\((?P<params>[^)]*)\)', content)
                    for m in func_matches:
                        name = m.group('name')
                        params = m.group('params').split(',')
                        min_params = len([p for p in params if p.strip() and '=' not in p and '?' not in p])
                        function_sigs[name] = min_params
            except Exception: continue

        for rel_path, full_path in file_map.items():
            # Skip extension-less duplicate keys (added for import resolution only).
            if not rel_path.endswith(('.ts', '.tsx', '.js', '.jsx')): continue
            if not os.path.isfile(full_path): continue
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    matches = re.findall(package_import_pattern, content)
                    for pkg in matches:
                        if _is_local_alias_import(pkg):
                            continue
                        # Ignore path aliases that are not npm packages (e.g. "~/", "~", "#/").
                        if pkg.startswith(('~/', '#/')) or pkg in ('~', '#'):
                            continue
                        package_name = f"{pkg.split('/')[0]}/{pkg.split('/')[1]}" if pkg.startswith('@') and len(pkg.split('/')) >= 2 else pkg.split('/')[0]
                        if _is_local_alias_import(package_name):
                            continue
                        if package_name in node_builtin_modules:
                            continue
                        if package_name and not package_name.startswith(('node:', '.', '@/', '~/', '#/')) and package_name not in ('~', '#'):
                            if package_name not in all_dependencies:
                                errors.append(f"Missing dependency: '{package_name}' is imported but not in package.json")
                    
                    matches = re.finditer(internal_import_pattern, content)
                    for match in matches:
                        path = match.group('path')
                        members_raw = match.group('members')
                        target_file = None
                        if path.startswith(('.', '..')):
                            target_file = file_map.get(os.path.normpath(os.path.join(os.path.dirname(rel_path), path)).replace(os.path.sep, '/'))
                        elif path.startswith('@/'):
                            p = path[2:]
                            target_file = file_map.get(p) or file_map.get(f"src/{p}")
                        
                        if target_file and os.path.exists(target_file):
                            with open(target_file, 'r', encoding='utf-8') as tf:
                                target_content = tf.read()
                                members = [m.strip().split(' as ')[0].strip() for m in members_raw.split(',')]
                                for member in members:
                                    if member and member != '*' and not re.search(rf"export\s+(type|interface|const|let|var|function|class|enum)\s+{member}\b|export\s+{{[^}}]*\b{member}\b[^}}]*}}", target_content):
                                        errors.append(f"ImportError in '{rel_path}': '{member}' not found in '{path}'")
                    
                    for func_name, min_params in function_sigs.items():
                        calls = re.finditer(rf'\b{func_name}\s*\((?P<args>[^)]*)\)', content)
                        for call in calls:
                            args_raw = call.group('args')
                            args_count = 0 if not args_raw.strip() else args_raw.count(',') + 1
                            if args_count < min_params:
                                errors.append(f"TypeError in '{rel_path}': Function '{func_name}' expects at least {min_params} arguments, but found {args_count}.")
            except Exception: continue
        
        return errors

    def _validate_typescript_types(self, repo_dir: str) -> List[str]:
        """Run TypeScript compiler to check for type errors"""
        errors = []
        frontend_dir = self._detect_frontend_root(repo_dir)
        if not frontend_dir or not os.path.exists(os.path.join(frontend_dir, 'tsconfig.json')):
            return []
        
        node_modules_path = os.path.join(frontend_dir, 'node_modules')
        if not os.path.exists(node_modules_path):
            logger.warning(f"node_modules not found in {frontend_dir}, skipping TypeScript validation")
            return ["ENVIRONMENT ERROR: TypeScript validation skipped because 'node_modules' is missing. Run 'npm install'."]
        
        try:
            result = subprocess.run(['npx', 'tsc', '--noEmit', '--pretty', 'false'], cwd=frontend_dir, capture_output=True, text=True, timeout=120)
            if result.returncode != 0 and result.stdout:
                lines = result.stdout.split('\n')
                for line in lines:
                    line = line.strip()
                    if not line or 'Found' in line: continue
                    if '):' in line and 'error TS' in line:
                        try:
                            # Robust parse for paths that legitimately contain parentheses,
                            # e.g. Next.js route groups: app/(dashboard)/page.tsx
                            m = re.match(
                                r"^(?P<file>.+)\((?P<line>\d+),(?P<col>\d+)\):\s*error\s*TS(?P<code>\d+):\s*(?P<msg>.+)$",
                                line
                            )
                            if m:
                                file_path = m.group('file').strip()
                                normalized = file_path.replace("\\", "/")
                                if "/node_modules/" in normalized or normalized.startswith("node_modules/"):
                                    continue
                                position = f"{m.group('line')},{m.group('col')}"
                                error_msg = f"{m.group('code')}: {m.group('msg').strip()}"
                                errors.append(f"TypeScript error in '{file_path}' at {position}: {error_msg}")
                            else:
                                # Fallback for unexpected tsc formats
                                file_and_pos = line.split('):')[0]
                                file_path = file_and_pos.rsplit('(', 1)[0]
                                normalized = file_path.replace("\\", "/")
                                if "/node_modules/" in normalized or normalized.startswith("node_modules/"):
                                    continue
                                position = file_and_pos.rsplit('(', 1)[1] if '(' in file_and_pos else 'unknown'
                                error_msg = line.split('error TS', 1)[1] if 'error TS' in line else line
                                errors.append(f"TypeScript error in '{file_path}' at {position}: {error_msg.strip()}")
                        except Exception:
                            errors.append(f"TypeScript error: {line}")
            
            if errors: logger.info(f"TypeScript validation found {len(errors)} type error(s)")
            else: logger.info("TypeScript validation passed with no errors")
        except Exception as e:
            logger.error(f"Error running TypeScript validation: {e}")
        
        return errors

    def _validate_python_imports(self, repo_dir: str) -> List[str]:
        """Verify Python imports and third-party dependencies"""
        errors = []
        logger.info("Starting Python import validation...")
        backend_dir = os.path.join(repo_dir, 'backend')
        backend_root = None
        if os.path.exists(backend_dir):
            backend_root = backend_dir
        else:
            # Backend-only repos often keep Python sources at repo root.
            # Detect by presence of any .py file outside common ignore dirs.
            ignore_dirs = {'.git', 'node_modules', '.venv', 'venv', 'env', '.env', '__pycache__', 'site-packages', '.next'}
            for root, dirs, files in os.walk(repo_dir):
                dirs[:] = [d for d in dirs if d not in ignore_dirs]
                if any(f.endswith('.py') for f in files):
                    backend_root = repo_dir
                    logger.info("Backend root fallback: using repo root for Python import validation")
                    break
        if backend_root is None:
            return []
        
        installed_packages = set()
        req_path = os.path.join(backend_root, 'requirements.txt')
        if not os.path.exists(req_path): req_path = os.path.join(repo_dir, 'requirements.txt')
        if os.path.exists(req_path):
            try:
                with open(req_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'): continue
                        pkg = line.split('#egg=')[-1] if '#egg=' in line else re.split(r'[<>=!]', line)[0].split('[')[0].strip()
                        installed_packages.add(pkg.lower())
            except Exception: pass
            
        if any(p.startswith('google-') for p in installed_packages):
            installed_packages.add('google')

        # CRITICAL: Check for pip-incompatible lines that would cause startup failure.
        # AI-generated requirements.txt files sometimes include code-block separator lines
        # like '---', '===', '```', or AI output format markers like 'FILE_PATH: ...'
        # which cause: ERROR: Invalid requirement: ---
        def _is_invalid_req_line(s: str) -> bool:
            """Return True for any line that is not a valid pip requirement spec."""
            if not s or s.startswith('#'):
                return False
            # Explicit code-block / code-gen format markers
            if s.startswith('```') or s.startswith('FILE_PATH:'):
                return True
            # Pure separator lines: ---, ===, etc.
            if re.match(r'^-{3,}$', s) or re.match(r'^={3,}$', s):
                return True
            # Python keywords that start code statements
            # (from X import Y  /  import X  /  class X  /  def X ...)
            if re.match(r'^(from|import|class|def|return|if|else|elif|for|while|try|except|with|raise|assert|pass)\s', s):
                return True
            # Python decorators (e.g., @lru_cache(), @app.route("/"), @dataclass)
            # Valid pip "@ URL" specs always have a package name BEFORE the @
            if re.match(r'^@\w+', s):
                return True
            # Any line containing whitespace that is NOT a pip option (-r, -e, -i, --...)
            # or a URL (contains ://). Valid pip specifiers never have bare spaces.
            if ' ' in s and not s.startswith('-') and '://' not in s:
                return True
            return False

        try:
            with open(req_path, 'r') as f:
                for line in f:
                    stripped = line.strip()
                    if _is_invalid_req_line(stripped):
                        errors.append(
                            f"Invalid pip requirement in 'requirements.txt': "
                            f"'{stripped}' is not a valid pip package spec - "
                            f"remove this line (it is a code-block marker, not a package)"
                        )
        except Exception:
            pass

        std_lib = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else set()
        
        # Common mappings from import name to package name
        import_mapping = {
            'dotenv': 'python-dotenv',
            'jose': 'python-jose',
            'jwt': 'PyJWT',
            'pydantic_settings': 'pydantic-settings',
            'firebase_admin': 'firebase-admin',
            'email_validator': 'email-validator',
            'google.cloud.firestore': 'google-cloud-firestore',
            'google.cloud.storage': 'google-cloud-storage',
            'google.cloud.secretmanager': 'google-cloud-secret-manager',
            'PIL': 'Pillow',
            'magic': 'python-magic',
            'dateutil': 'python-dateutil',
            'yaml': 'PyYAML',
            'cv2': 'opencv-python',
            'sklearn': 'scikit-learn',
            'bs4': 'beautifulsoup4'
        }
        dotted_import_prefix_mapping = {
            'google.cloud.firestore': 'google-cloud-firestore',
            'google.cloud.storage': 'google-cloud-storage',
            'google.cloud.secretmanager': 'google-cloud-secret-manager',
            'google.api_core': 'google-api-core',
        }

        def _map_import_to_package(import_name: str) -> str:
            """Resolve full import path to the most specific package name."""
            if import_name in import_mapping:
                return import_mapping[import_name]
            for prefix, package in dotted_import_prefix_mapping.items():
                if import_name == prefix or import_name.startswith(prefix + '.'):
                    return package
            top_level = import_name.split('.')[0]
            return import_mapping.get(top_level, top_level)

        def _is_package_installed(package_name: str) -> bool:
            """Check package name against common naming variants in requirements."""
            normalized = package_name.lower()
            candidates = {
                normalized,
                normalized.replace('_', '-'),
                normalized.replace('-', '_'),
                f"python-{normalized}",
                normalized.split('[')[0],  # handle extras like package[extra]
            }
            return any(candidate in installed_packages for candidate in candidates)

        module_to_file = {}
        ignore_dirs = {'venv', '.venv', 'env', '.env', 'node_modules', '.git', '__pycache__', 'site-packages'}
        ignore_dirs = {'venv', '.venv', 'env', '.env', 'node_modules', '.git', '__pycache__', 'site-packages'}
        if backend_root == repo_dir:
            ignore_dirs.add('frontend')
        for root, dirs, files in os.walk(backend_root):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                if file.endswith('.py'):
                    rel_path = os.path.relpath(os.path.join(root, file), backend_root)
                    module_name = ".".join(rel_path.split(os.path.sep))[:-3]
                    if module_name.endswith('.__init__'): module_name = module_name[:-9]
                    module_to_file[module_name] = os.path.join(root, file)

        for module_name, file_path in module_to_file.items():
            try:
                with open(file_path, 'r') as f:
                    content = f.read()
                    
                    # CRITICAL: Check for incorrect absolute imports using 'backend.' prefix
                    # These will fail at runtime in Docker container where backend/ is the root
                    incorrect_imports = re.findall(r'from\s+backend\.(\w+)', content)
                    if incorrect_imports:
                        for imp in set(incorrect_imports):
                            errors.append(f"ImportError in '{module_name}.py': WRONG - 'from backend.{imp}' | CORRECT - 'from {imp}' (remove 'backend.' prefix - the backend directory IS the Python root, not a package)")
                    
                    # CRITICAL: Check for relative imports (starting with .) in main.py
                    # main.py is the entry point and cannot use relative imports
                    if module_name == 'main' or file_path.endswith('/main.py'):
                        relative_imports = re.findall(r'from\s+(\.\w+|\.\.\w+)', content)
                        if relative_imports:
                            for imp in set(relative_imports):
                                errors.append(f"ImportError in 'main.py': Using 'from {imp}' - main.py is the entry point and CANNOT use relative imports. MUST use 'from config' instead of 'from .config'.")
                    
                    if 'EmailStr' in content and not ('email-validator' in installed_packages or 'pydantic[email]' in installed_packages):
                        errors.append("Missing dependency: 'email-validator' is required when using pydantic.EmailStr. Add 'email-validator' to requirements.txt")

                    try:
                        tree = ast.parse(content)
                    except SyntaxError as syn_err:
                        errors.append(
                            f"SyntaxError in '{module_name}.py' at line {syn_err.lineno}: {syn_err.msg}"
                        )
                        continue

                    for node in ast.walk(tree):
                        imported_modules = []
                        if isinstance(node, ast.Import):
                            imported_modules = [n.name for n in node.names]
                        elif isinstance(node, ast.ImportFrom) and node.module and getattr(node, 'level', 0) == 0:
                            imported_modules = [node.module]
                        
                        for imp in imported_modules:
                            top_level_imp = imp.split('.')[0]
                            if top_level_imp in std_lib:
                                continue
                            
                            # CRITICAL: Check if this is a local module
                            # First check if it actually exists in the codebase
                            is_local = top_level_imp in module_to_file or any(m.startswith(f"{top_level_imp}.") for m in module_to_file)
                            
                            # If not found but looks like a common local module name, check subdirectories
                            if not is_local and top_level_imp in ['jwt_utils', 'auth_utils', 'settings', 'config']:
                                # These are commonly local utility modules that might be in subdirectories
                                # Check if any module path contains this name
                                is_local = any(top_level_imp in m for m in module_to_file)
                            
                            if is_local:
                                continue
                            
                            # Map import name to package name if known
                            pkg_name = _map_import_to_package(imp)
                            is_installed = _is_package_installed(pkg_name)
                            
                            if not is_installed:
                                errors.append(f"Missing dependency: '{pkg_name}' is required for '{top_level_imp}' import in '{module_name}.py'. Add '{pkg_name}' to requirements.txt")

                        # CRITICAL FIX: Check if imported module file exists
                        if isinstance(node, ast.ImportFrom) and node.module:
                            # Check if this looks like a local import (not stdlib or third-party)
                            top_level_module = node.module.split('.')[0]
                            mapped_pkg = _map_import_to_package(node.module)
                            is_third_party = (
                                top_level_module in std_lib or
                                top_level_module in installed_packages or
                                node.module in import_mapping or
                                top_level_module in import_mapping or
                                _is_package_installed(mapped_pkg)
                            )
                            
                            if not is_third_party:
                                # This should be a local import - check if file/module or package exists.
                                module_is_package = any(m.startswith(f"{node.module}.") for m in module_to_file)
                                if node.module in module_to_file:
                                    # File exists - check if imported names exist in it
                                    target_file = module_to_file[node.module]
                                    with open(target_file, 'r') as tf:
                                        target_content = tf.read()
                                        for alias in node.names:
                                            if alias.name == '*' or f"{node.module}.{alias.name}" in module_to_file: continue
                                            if not re.search(rf"(class|def)\s+{alias.name}\b|{alias.name}\s*=", target_content):
                                                errors.append(f"ImportError in '{module_name}.py': '{alias.name}' not found in '{node.module}.py'")
                                elif module_is_package:
                                    # Package import (e.g. "from routes import auth_routes"):
                                    # accept members that are existing submodules under that package.
                                    for alias in node.names:
                                        if alias.name == '*':
                                            continue
                                        if f"{node.module}.{alias.name}" in module_to_file:
                                            continue
                                        errors.append(
                                            f"ImportError in '{module_name}.py': '{alias.name}' not found in package '{node.module}'"
                                        )
                                else:
                                    # CRITICAL: File doesn't exist but is being imported!
                                    expected_file_path = node.module.replace('.', '/') + '.py'
                                    errors.append(f"ImportError in '{module_name}.py': Cannot import from '{node.module}' - file '{expected_file_path}' does not exist")
            except Exception:
                continue

        return errors
