import os
import sys
import re
from typing import List, Dict, Any

# Mocking parts of DeploymentFixer and DeploymentManager for testing
def _is_invalid_req_line(s: str) -> bool:
    """The improved logic from deployment_fixer.py"""
    if not s or s.startswith('#'):
        return False
    # Explicit code-block / code-gen format markers
    if s.startswith('```') or s.startswith('FILE_PATH:'):
        return True
    # Pure separator lines: ---, ===, etc.
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

def test_requirements_cleanup():
    bad_lines = [
        "```python",
        "FILE_PATH: backend/requirements.txt",
        "---",
        "===",
        "from fastapi import FastAPI",
        "import os",
        "def main():",
        "@app.route('/')",
        "print('hello')",
        "python",
        "invalid requirement with spaces",
        "fastapi==0.104.1", # GOOD
        "uvicorn[standard]==0.24.0", # GOOD
        "# Comment", # GOOD
        "-e .", # GOOD
        "https://github.com/some/repo@branch", # GOOD
    ]
    
    print("Testing requirements cleanup...")
    for line in bad_lines:
        invalid = _is_invalid_req_line(line)
        expected = "GOOD" if line in ["fastapi==0.104.1", "uvicorn[standard]==0.24.0", "# Comment", "-e .", "https://github.com/some/repo@branch"] else "BAD"
        result = "BAD" if invalid else "GOOD"
        print(f"[{result}] {line[:30]:30} | Expected: {expected}")
        if result != expected:
            print(f"  FAILED: {line}")

def test_pip_exception_parsing():
    combined = """
[notice] A new release of pip is available: 24.3.1 -> 26.0.1
ERROR: Exception:
Traceback (most recent call last):
  File "/venv/lib/python3.12/site-packages/pip/_vendor/packaging/requirements.py", line 36, in __init__
    parsed = _parse_requirement(requirement_string)
  File "/venv/lib/python3.12/site-packages/pip/_vendor/packaging/_parser.py", line 62, in parse_requirement
    return _parse_requirement(Tokenizer(source, rules=DEFAULT_RULES))
```python
fastapi==0.104.1
"""
    
    print("\nTesting pip exception parsing...")
    errors = []
    
    # Logic from deployment_manager.py
    invalid_req = re.search(r"ERROR:\s*Invalid requirement:\s*'([^']+)'", combined)
    if not invalid_req:
        if "Traceback" in combined and "packaging/requirements.py" in combined:
            for line in combined.splitlines():
                s = line.strip()
                if s.startswith('```') or s.startswith('FILE_PATH:') or re.match(r'^-{3,}$', s):
                    invalid_req = re.match(r'(.*)', s)
                    break
    
    if invalid_req:
        bad_line = invalid_req.group(1)
        print(f"Extracted bad line: {bad_line}")
        if bad_line == "```python":
            print("SUCCESS: Correctly extracted malformed line.")
        else:
            print(f"FAILED: Extracted {bad_line} instead of ```python")
    else:
        print("FAILED: No invalid requirement extracted.")

if __name__ == "__main__":
    test_requirements_cleanup()
    test_pip_exception_parsing()
