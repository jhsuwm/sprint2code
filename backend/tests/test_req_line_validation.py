"""
Tests for the fixes to the auto-fix pipeline:
1. _is_invalid_req_line() detecting @decorator() lines in requirements.txt
2. _extract_runtime_fixable_errors() extracting pip Invalid requirement errors
"""
import re
import sys
import os

# ---------------------------------------------------------------------------
# 1. Test _is_invalid_req_line() — copied from deployment_validator.py
#    (both deployment_validator.py and deployment_fixer.py share the same logic)
# ---------------------------------------------------------------------------

def _is_invalid_req_line(s: str) -> bool:
    """Return True for any line that is not a valid pip requirement spec."""
    if not s or s.startswith('#'):
        return False
    if s.startswith('```') or s.startswith('FILE_PATH:'):
        return True
    if re.match(r'^-{3,}$', s) or re.match(r'^={3,}$', s):
        return True
    if re.match(r'^(from|import|class|def|return|if|else|elif|for|while|try|except|with|raise|assert|pass)\s', s):
        return True
    # NEW: Python decorators
    if re.match(r'^@\w+', s):
        return True
    if ' ' in s and not s.startswith('-') and '://' not in s:
        return True
    return False


class TestIsInvalidReqLine:
    """Test _is_invalid_req_line() detects invalid lines and accepts valid ones."""

    # --- Should be INVALID (return True) ---
    def test_decorator_lru_cache(self):
        assert _is_invalid_req_line("@lru_cache()") is True

    def test_decorator_app_route(self):
        assert _is_invalid_req_line("@app.route(\"/\")") is True

    def test_decorator_dataclass(self):
        assert _is_invalid_req_line("@dataclass") is True

    def test_decorator_property(self):
        assert _is_invalid_req_line("@property") is True

    def test_decorator_staticmethod(self):
        assert _is_invalid_req_line("@staticmethod") is True

    def test_code_block_marker(self):
        assert _is_invalid_req_line("```python") is True

    def test_separator_dashes(self):
        assert _is_invalid_req_line("---") is True

    def test_separator_equals(self):
        assert _is_invalid_req_line("===") is True

    def test_python_from_import(self):
        assert _is_invalid_req_line("from functools import lru_cache") is True

    def test_python_import(self):
        assert _is_invalid_req_line("import os") is True

    def test_python_class(self):
        assert _is_invalid_req_line("class Settings(BaseSettings):") is True

    def test_python_def(self):
        assert _is_invalid_req_line("def get_settings():") is True

    def test_file_path_marker(self):
        assert _is_invalid_req_line("FILE_PATH: requirements.txt") is True

    def test_line_with_spaces(self):
        assert _is_invalid_req_line("this is not a package") is True

    # --- Should be VALID (return False) ---
    def test_valid_pinned_package(self):
        assert _is_invalid_req_line("fastapi==0.109.0") is False

    def test_valid_package_with_extras(self):
        assert _is_invalid_req_line("pydantic[email]>=2.0") is False

    def test_valid_package_no_version(self):
        assert _is_invalid_req_line("uvicorn") is False

    def test_valid_package_with_bracket_extras(self):
        assert _is_invalid_req_line("passlib[bcrypt]==1.7.4") is False

    def test_valid_comment(self):
        assert _is_invalid_req_line("# This is a comment") is False

    def test_empty_line(self):
        assert _is_invalid_req_line("") is False

    def test_valid_pip_option_r(self):
        assert _is_invalid_req_line("-r base.txt") is False

    def test_valid_pip_option_e(self):
        assert _is_invalid_req_line("-e git+https://github.com/foo/bar.git#egg=bar") is False

    def test_valid_url_dep(self):
        assert _is_invalid_req_line("https://example.com/package.tar.gz") is False


# ---------------------------------------------------------------------------
# 2. Test _extract_runtime_fixable_errors() — extracted logic
# ---------------------------------------------------------------------------

def _extract_runtime_fixable_errors(startup_message: str, startup_diag: dict, repo_dir: str):
    """Simplified version of DeploymentManager._extract_runtime_fixable_errors for testing."""
    errors = []
    combined = "\n".join([
        startup_message or "",
        str(startup_diag.get('backend_error', '')) if startup_diag else "",
        str(startup_diag.get('frontend_error', '')) if startup_diag else "",
        str(startup_diag.get('exception', '')) if startup_diag else "",
    ])

    # Python runtime import failures
    missing_mod = re.search(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", combined)
    if missing_mod:
        errors.append(f"Missing dependency: '{missing_mod.group(1)}' in requirements.txt")

    cannot_import = re.search(
        r"ImportError:\s+cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
        combined
    )
    if cannot_import:
        symbol = cannot_import.group(1)
        module = cannot_import.group(2)
        importer_rel = "backend/main.py"
        errors.append(f"ImportError in '{importer_rel}': '{symbol}' not found in '{module}'")

    # NEW: pip install failures
    invalid_req = re.search(
        r"ERROR:\s*Invalid requirement:\s*'([^']+)'",
        combined
    )
    if invalid_req:
        bad_line = invalid_req.group(1)
        errors.append(
            f"Invalid pip requirement in 'requirements.txt': "
            f"'{bad_line}' is not a valid pip package spec - "
            f"remove this line (it is a code-block marker, not a package)"
        )

    # Frontend runtime build errors
    missing_resolve = re.search(r"Module not found:.*Can't resolve ['\"]([^'\"]+)['\"]", combined, re.IGNORECASE)
    if missing_resolve:
        module_name = missing_resolve.group(1)
        if module_name.startswith(('@/','./','../','~/', '/')):
            errors.append(f"TypeScript error in 'src/app/page.tsx' at 1,1: 2307: Cannot find module '{module_name}'")
        else:
            errors.append(f"Missing dependency: '{module_name}' in package.json")

    return errors


class TestExtractRuntimeFixableErrors:
    """Test _extract_runtime_fixable_errors() extracts pip errors."""

    def test_extract_invalid_requirement_lru_cache(self):
        """The exact error from the log file should be extracted."""
        message = (
            "Backend failed to start: Failed to install dependencies: "
            "[notice] A new release of pip is available: 24.3.1 -> 26.0.1 "
            "[notice] To update, run: python3.12 -m pip install --upgrade pip "
            "ERROR: Invalid requirement: '@lru_cache()': "
            "Expected package name at the start of dependency specifier "
            "@lru_cache() ^ (from line 16 of requirements.txt)"
        )
        errors = _extract_runtime_fixable_errors(message, {}, "/tmp")
        assert len(errors) == 1
        assert "Invalid pip requirement in 'requirements.txt'" in errors[0]
        assert "@lru_cache()" in errors[0]

    def test_extract_invalid_requirement_decorator(self):
        message = "ERROR: Invalid requirement: '@dataclass'"
        errors = _extract_runtime_fixable_errors(message, {}, "/tmp")
        assert len(errors) == 1
        assert "@dataclass" in errors[0]

    def test_extract_module_not_found(self):
        message = "ModuleNotFoundError: No module named 'fastapi'"
        errors = _extract_runtime_fixable_errors(message, {}, "/tmp")
        assert len(errors) == 1
        assert "Missing dependency: 'fastapi' in requirements.txt" in errors[0]

    def test_no_errors_for_normal_message(self):
        message = "Backend started successfully on port 8000"
        errors = _extract_runtime_fixable_errors(message, {}, "/tmp")
        assert len(errors) == 0

    def test_extract_frontend_module_not_found(self):
        message = "Module not found: Error: Can't resolve 'zustand'"
        errors = _extract_runtime_fixable_errors(message, {}, "/tmp")
        assert len(errors) == 1
        assert "Missing dependency: 'zustand' in package.json" in errors[0]

    def test_extract_frontend_local_module_not_found(self):
        message = "Module not found: Error: Can't resolve '@/components/Button'"
        errors = _extract_runtime_fixable_errors(message, {}, "/tmp")
        assert len(errors) == 1
        assert "Cannot find module '@/components/Button'" in errors[0]

    def test_combined_pip_and_module_errors(self):
        """Multiple error types in one message."""
        message = (
            "ERROR: Invalid requirement: '@property'\n"
            "ModuleNotFoundError: No module named 'redis'"
        )
        errors = _extract_runtime_fixable_errors(message, {}, "/tmp")
        assert len(errors) == 2


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
