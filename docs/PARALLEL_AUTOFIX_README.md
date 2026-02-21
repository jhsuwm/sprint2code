# Parallel Auto-Fix Architecture

## Overview

This document describes the parallel auto-fix architecture implemented to speed up the AI-generated code error resolution pipeline.

## Problem Statement

### Issues Identified
1. **Sequential Processing**: Auto-fix was processing files one-by-one, taking 35+ minutes for large codebases
2. **Pipeline Timeouts**: Users had to abort due to long wait times
3. **No Parallelization**: Despite having multiple errors across different files, only one fix was running at a time

### From Logs
- Initial error count: 109 errors
- Auto-fix cycle 2 took 35+ minutes (14:24 → 14:59)
- User had to abort the process due to excessive time

## Solution: Orchestrator Pattern with Worker Agents

### Architecture

```
┌─────────────────────────────────────┐
│   DeploymentManager (Main)          │
│   - Runs static analysis            │
│   - Detects errors                  │
└────────────┬────────────────────────┘
             │
             ▼
┌─────────────────────────────────────┐
│   AutoFixOrchestrator                │
│   - Parses errors into tasks        │
│   - Prioritizes files (deps first)  │
│   - Distributes to workers          │
│   - Monitors progress                │
└────────────┬────────────────────────┘
             │
      ┌──────┴──────┐
      ▼             ▼
┌──────────┐  ┌──────────┐
│ Worker 1 │  │ Worker 2 │
│          │  │          │
│ Fix file │  │ Fix file │
│ Validate │  │ Validate │
│ Commit   │  │ Commit   │
└──────────┘  └──────────┘
```

### Key Components

#### 1. **AutoFixOrchestrator** (`auto_fix_orchestrator.py`)
- Main coordinator for parallel auto-fix operations
- Creates worker pool (default: 2 workers)
- Distributes tasks via asyncio Queue
- Tracks metrics and progress
- **CRITICAL**: Manages file-to-worker assignments to prevent race conditions
  - Same file always assigned to same worker (consistent hashing)
  - Workers only process their assigned files
  - Prevents concurrent modifications to the same file

#### 2. **AutoFixWorker** (`auto_fix_worker.py`)
- Individual worker agent that processes one file at a time
- Each worker has its own DeploymentFixer instance
- Performs: fix generation → validation → commit
- Returns success/failure status

#### 3. **DeploymentManager** (updated)
- Integrated orchestrator for parallel processing
- Configurable via `PARALLEL_AUTOFIX` environment variable
- Falls back to sequential mode if needed

## Benefits

### Speed Improvements
- **2x faster** with 2 workers (can scale to more)
- Files fixed in parallel instead of sequentially
- Independent files don't block each other

### Better Resource Utilization
- Multiple AI fix requests in parallel
- Workers idle less during AI generation
- More efficient use of GitHub API rate limits

### Improved UX
- Faster feedback to users
- Less likely to timeout
- Progress visible across multiple workers

## Configuration

### Environment Variables

```bash
# Enable parallel auto-fix (default)
export PARALLEL_AUTOFIX=true

# Disable for sequential mode (legacy)
export PARALLEL_AUTOFIX=false

# Number of workers (in code, default=2)
# Can be adjusted in deployment_manager.py initialization
```

### Worker Count
```python
# In deployment_manager.py __init__
self.orchestrator = AutoFixOrchestrator(
    job_manager, gemini_service, github_service, 
    gcloud_service, jira_service, 
    num_workers=2  # Adjust this number
)
```

## File Prioritization

The orchestrator prioritizes files in this order:

1. **Dependencies** (requirements.txt, package.json) - Must be fixed first
2. **Types/Models** - Needed by other files
3. **Implementation** - Can be fixed in parallel

This ensures dependencies are resolved before dependent files are processed.

## Logging & Monitoring

### Worker Logs
```
🤖 Worker 1: Fixing backend/models/user.py...
✅ Worker 1: Programmatic fix succeeded for backend/models/user.py
🤖 Worker 2: Fixing frontend/src/api/index.ts...
✅ Worker 2: Successfully fixed and committed frontend/src/api/index.ts
```

### Orchestrator Logs
```
🎯 Orchestrator: Starting parallel fix cycle 1
🔧 Orchestrator: Trying programmatic fix for backend/requirements.txt
🔄 Orchestrator: Distributing 23 files to 2 workers
✅ Orchestrator: Cycle 1 complete - fixes applied
```

## Race Condition Prevention

### The Problem
When multiple workers process the same file concurrently, they can create race conditions:
- Worker 1 fixes error A in file.py, commits to GitHub
- Worker 2 simultaneously fixes error B in file.py (using old version)
- Worker 2's commit overwrites Worker 1's fix
- Result: Error A reappears, infinite loop

### The Solution
**Consistent File-to-Worker Assignment**:

1. **Hash-based Assignment**: Each file is assigned to a worker using `hash(file_path) % num_workers`
2. **Persistent Mapping**: Assignment is stored in `_file_assignments` dictionary
3. **Sticky Assignment**: Same file always goes to same worker across all cycles
4. **Worker Filtering**: Workers only process tasks assigned to their ID

### Example
```python
# Cycle 1: File assignments
backend/auth.py → Worker 1 (hash = 12345)
frontend/api.ts → Worker 2 (hash = 67890)

# Cycle 2: Same file has new errors
backend/auth.py → Worker 1 (consistent!)
# Worker 2 will skip this task, put it back in queue
```

### Benefits
✅ No concurrent modifications to same file
✅ Each worker sees latest committed changes
✅ Eliminates race condition infinite loops
✅ Maintains parallelism for different files

## Known Limitations

1. **Worker Affinity**: Files "stick" to workers, may cause imbalance if one file has many errors
2. **API Rate Limits**: Parallel workers may hit Gemini API limits faster
3. **Memory**: Each worker needs its own context/state
4. **Cross-File Dependencies**: Workers don't coordinate on interdependent files (handled by prioritization)

## Future Enhancements

### Planned
- [ ] Dynamic worker scaling based on error count
- [ ] Smart dependency detection to avoid conflicts
- [ ] Worker health monitoring and restart
- [ ] Metrics dashboard for orchestrator performance

### Possible
- [ ] Distributed workers across machines
- [ ] Redis-based task queue for persistence
- [ ] Priority queue for critical files
- [ ] A/B testing sequential vs parallel performance

## Migration Guide

### For Existing Code
No changes needed! The system automatically uses parallel mode by default.

### To Disable (Rollback)
```bash
export PARALLEL_AUTOFIX=false
```

### To Adjust Workers
Edit `deployment_manager.py` line ~29:
```python
num_workers=2  # Change to desired number (1-5 recommended)
```

## Testing

### Manual Test
1. Generate code with many errors (100+)
2. Watch logs for parallel worker activity
3. Compare time vs sequential mode

### Performance Benchmark
```bash
# Sequential (baseline)
PARALLEL_AUTOFIX=false python -m backend.main

# Parallel (2 workers)
PARALLEL_AUTOFIX=true python -m backend.main

# Expected: 40-50% time reduction for 100+ errors
```

## Troubleshooting

### Workers Not Starting
- Check `PARALLEL_AUTOFIX` environment variable
- Verify orchestrator initialization in logs

### Slower Than Sequential
- Possible with <10 errors (overhead)
- Check worker count (reduce if too many)
- Verify API rate limits not hit

### Git Conflicts
- Orchestrator handles by pulling latest before each commit
- If persistent, reduce worker count to 1

## Credits

Implemented to address performance issues observed in:
- `agent_execution.log` from 2026-02-16
- 35+ minute auto-fix cycles
- 109 initial errors taking excessive time

## References

- `auto_fix_orchestrator.py` - Main orchestrator
- `auto_fix_worker.py` - Worker implementation  
- `deployment_manager.py` - Integration point
- `deployment_fixer.py` - Shared fix logic
