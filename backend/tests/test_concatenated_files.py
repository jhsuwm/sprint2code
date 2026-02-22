import os
import pytest
from unittest.mock import MagicMock, AsyncMock

from agents.deployment_validator import DeploymentValidator
from agents.deployment_fixer import DeploymentFixer

@pytest.fixture
def mock_fixer():
    job_manager = MagicMock()
    gemini_service = MagicMock()
    github_service = MagicMock()
    jira_service = MagicMock()
    fixer = DeploymentFixer(job_manager, gemini_service, github_service, jira_service)
    fixer._commit_programmatic_fix = AsyncMock(return_value=True)
    fixer._safe_log = MagicMock()
    return fixer

def test_validate_concatenated_files(tmp_path):
    validator = DeploymentValidator()
    
    # Create a clean TS file
    clean_file = tmp_path / "clean.ts"
    clean_file.write_text("export const A = 1;\nexport const B = 2;")
    
    # Create a concatenated TS file
    concat_file = tmp_path / "concat.ts"
    concat_content = (
        "export const A = 1;\n"
        "---\n"
        "FILE_PATH: frontend/src/B.ts\n"
        "export const B = 2;\n"
    )
    concat_file.write_text(concat_content)
    
    # Create an intentionally excluded file (e.g. node_modules)
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    ignore_file = node_modules / "concat.ts"
    ignore_file.write_text(concat_content)
    
    errors = validator._validate_concatenated_files(str(tmp_path))
    
    assert len(errors) == 1
    assert "concat.ts" in errors[0]
    assert "AI Concatenation Error" in errors[0]
    # Check node_modules was ignored
    assert "node_modules" not in errors[0]

@pytest.mark.asyncio
async def test_split_concatenated_files(tmp_path, mock_fixer):
    repo_dir = str(tmp_path)
    
    file_path = "frontend/src/common.ts"
    content = (
        "export interface User { id: string; }\n"
        "---\n"
        "FILE_PATH: frontend/src/auth.ts\n"
        "```ts\n"
        "export const login = () => {};\n"
        "```\n"
        "---\n"
        "FILE_PATH: frontend/src/ticket.ts\n"
        "export const getTicket = () => {};\n"
    )
    
    file_info = {'content': content, 'missing': ['AI Concatenation Error in frontend/src/common.ts at line 2']}
    
    success = await mock_fixer._split_concatenated_files(
        file_path=file_path,
        file_info=file_info,
        github_repo="test/repo",
        github_branch="main",
        repo_dir=repo_dir,
        job_id="test_job_id"
    )
    
    assert success is True
    
    # Should have committed 3 times
    assert mock_fixer._commit_programmatic_fix.call_count == 3
    
    calls = mock_fixer._commit_programmatic_fix.call_args_list
    
    # Path assertions
    paths = [call.args[1] for call in calls]
    assert "frontend/src/common.ts" in paths
    assert "frontend/src/auth.ts" in paths
    assert "frontend/src/ticket.ts" in paths
    
    # Content assertions
    contents = {call.args[1]: call.args[2] for call in calls}
    assert contents["frontend/src/common.ts"] == "export interface User { id: string; }"
    assert contents["frontend/src/auth.ts"] == "export const login = () => {};"
    assert contents["frontend/src/ticket.ts"] == "export const getTicket = () => {};"

@pytest.mark.asyncio
async def test_split_concatenated_files_no_action(tmp_path, mock_fixer):
    repo_dir = str(tmp_path)
    file_path = "frontend/src/clean.ts"
    content = "export const A = 1;\nexport const B = 2;"
    file_info = {'content': content}
    
    success = await mock_fixer._split_concatenated_files(
        file_path, file_info, "repo", "branch", repo_dir, "job_id"
    )
    
    assert success is False
    assert mock_fixer._commit_programmatic_fix.call_count == 0
