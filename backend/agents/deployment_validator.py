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

    def validate_all(self, repo_dir: str) -> List[str]:
        """Perform all validations on the repository - PRODUCTION CODE ONLY"""
        import_errors = self._validate_python_imports(repo_dir)
        dependency_errors = self._validate_frontend_dependencies(repo_dir)
        typescript_errors = self._validate_typescript_types(repo_dir)
        property_errors = self._validate_typescript_properties(repo_dir)
        
        # CRITICAL: Filter out test file errors - they don't affect production deployment
        all_errors = import_errors + dependency_errors + typescript_errors + property_errors
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

    def _validate_typescript_properties(self, repo_dir: str) -> List[str]:
        """
        Verify property access against defined TypeScript interfaces/types.
        Uses type-flow analysis to catch property naming errors.
        """
        errors = []
        frontend_dir = os.path.join(repo_dir, 'frontend')
        if not os.path.exists(frontend_dir):
            return []

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
        frontend_dir = os.path.join(repo_dir, 'frontend')
        if not os.path.exists(frontend_dir):
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

        package_json_path = os.path.join(frontend_dir, 'package.json')
        if not os.path.exists(package_json_path):
            return errors
        
        all_dependencies = set()
        try:
            with open(package_json_path, 'r') as f:
                package_data = json.load(f)
                all_dependencies = set(package_data.get('dependencies', {}).keys()).union(set(package_data.get('devDependencies', {}).keys()))
                scripts = package_data.get('scripts', {})
                if 'build' not in scripts: errors.append("Missing script: 'build' in package.json")
        except Exception:
            return errors
        
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
        function_sigs = {}

        for rel_path, full_path in file_map.items():
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
            if not os.path.isfile(full_path): continue
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    matches = re.findall(package_import_pattern, content)
                    for pkg in matches:
                        package_name = f"{pkg.split('/')[0]}/{pkg.split('/')[1]}" if pkg.startswith('@') and len(pkg.split('/')) >= 2 else pkg.split('/')[0]
                        if package_name and not package_name.startswith(('node:', '.', '@/')):
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
        frontend_dir = os.path.join(repo_dir, 'frontend')
        if not os.path.exists(frontend_dir) or not os.path.exists(os.path.join(frontend_dir, 'tsconfig.json')):
            return []
        
        if not os.path.exists(os.path.join(frontend_dir, 'node_modules')):
            logger.warning(f"node_modules not found in {frontend_dir}, skipping TypeScript validation")
            return []
        
        try:
            result = subprocess.run(['npx', 'tsc', '--noEmit', '--pretty', 'false'], cwd=frontend_dir, capture_output=True, text=True, timeout=120)
            if result.returncode != 0 and result.stdout:
                lines = result.stdout.split('\n')
                for line in lines:
                    line = line.strip()
                    if not line or 'Found' in line: continue
                    if '):' in line and 'error TS' in line:
                        try:
                            file_and_pos = line.split('):')[0]
                            file_path = file_and_pos.split('(')[0]
                            position = file_and_pos.split('(')[1] if '(' in file_and_pos else 'unknown'
                            error_msg = line.split('error TS')[1] if 'error TS' in line else line
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
        if not os.path.exists(backend_dir): return []
        
        installed_packages = set()
        req_path = os.path.join(backend_dir, 'requirements.txt')
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

        std_lib = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else set()
        
        # Common mappings from import name to package name
        import_mapping = {
            'dotenv': 'python-dotenv',
            'jose': 'python-jose',
            'jwt': 'PyJWT',
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

        module_to_file = {}
        ignore_dirs = {'venv', '.venv', 'env', '.env', 'node_modules', '.git', '__pycache__', 'site-packages'}
        for root, dirs, files in os.walk(backend_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                if file.endswith('.py'):
                    rel_path = os.path.relpath(os.path.join(root, file), backend_dir)
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
                            errors.append(f"ImportError in '{module_name}.py': Using 'from backend.{imp}' - MUST use 'from {imp}' instead (relative import). The 'backend.' prefix will fail at runtime in container.")
                    
                    # CRITICAL: Check for relative imports (starting with .) in main.py
                    # main.py is the entry point and cannot use relative imports
                    if module_name == 'main' or file_path.endswith('/main.py'):
                        relative_imports = re.findall(r'from\s+(\.\w+|\.\.\w+)', content)
                        if relative_imports:
                            for imp in set(relative_imports):
                                errors.append(f"ImportError in 'main.py': Using 'from {imp}' - main.py is the entry point and CANNOT use relative imports. MUST use 'from config' instead of 'from .config'.")
                    
                    if 'EmailStr' in content and not ('email-validator' in installed_packages or 'pydantic[email]' in installed_packages):
                        errors.append("Missing dependency: 'email-validator' is required when using pydantic.EmailStr. Add 'email-validator' to requirements.txt")

                    tree = ast.parse(content)
                    for node in ast.walk(tree):
                        imported_modules = []
                        if isinstance(node, ast.Import): imported_modules = [n.name.split('.')[0] for n in node.names]
                        elif isinstance(node, ast.ImportFrom) and node.module: imported_modules = [node.module.split('.')[0]]
                        
                        for imp in imported_modules:
                            if imp in std_lib: continue
                            
                            # CRITICAL: Check if this is a local module
                            # First check if it actually exists in the codebase
                            is_local = imp in module_to_file or any(m.startswith(f"{imp}.") for m in module_to_file)
                            
                            # If not found but looks like a common local module name, check subdirectories
                            if not is_local and imp in ['jwt_utils', 'auth_utils', 'settings', 'config']:
                                # These are commonly local utility modules that might be in subdirectories
                                # Check if any module path contains this name
                                is_local = any(imp in m for m in module_to_file)
                            
                            if is_local: continue
                            
                            # Map import name to package name if known
                            pkg_name = import_mapping.get(imp, imp)
                            
                            # Check multiple naming conventions
                            is_installed = any(p in installed_packages for p in [
                                pkg_name.lower(),
                                pkg_name.replace('_', '-').lower(),
                                pkg_name.replace('-', '_').lower(),
                                f"python-{pkg_name.lower()}",
                                pkg_name.lower().split('[')[0] # handle jose[cryptography]
                            ])
                            
                            if not is_installed:
                                errors.append(f"Missing dependency: '{pkg_name}' is required for '{imp}' import in '{module_name}.py'. Add '{pkg_name}' to requirements.txt")

                        # CRITICAL FIX: Check if imported module file exists
                        if isinstance(node, ast.ImportFrom) and node.module:
                            # Check if this looks like a local import (not stdlib or third-party)
                            top_level_module = node.module.split('.')[0]
                            is_third_party = (
                                top_level_module in std_lib or
                                top_level_module in installed_packages or
                                top_level_module in import_mapping
                            )
                            
                            if not is_third_party:
                                # This should be a local import - check if file exists
                                if node.module in module_to_file:
                                    # File exists - check if imported names exist in it
                                    target_file = module_to_file[node.module]
                                    with open(target_file, 'r') as tf:
                                        target_content = tf.read()
                                        for alias in node.names:
                                            if alias.name == '*' or f"{node.module}.{alias.name}" in module_to_file: continue
                                            if not re.search(rf"(class|def)\s+{alias.name}\b|{alias.name}\s*=", target_content):
                                                errors.append(f"ImportError in '{module_name}.py': '{alias.name}' not found in '{node.module}.py'")
                                else:
                                    # CRITICAL: File doesn't exist but is being imported!
                                    expected_file_path = node.module.replace('.', '/') + '.py'
                                    errors.append(f"ImportError in '{module_name}.py': Cannot import from '{node.module}' - file '{expected_file_path}' does not exist")
            except Exception: continue
        return errors
