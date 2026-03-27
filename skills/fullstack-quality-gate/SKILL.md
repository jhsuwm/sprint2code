---
name: fullstack-quality-gate
description: Enforce static analysis quality gates and automatic repair until frontend and backend start without unresolved errors.
type: full
---

# Fullstack Quality Gate Skill

## Goal
- Resolve all static-analysis errors before local startup.
- Do not require manual user edits for unresolved code errors.

## Validation Requirements
- Backend import validation passes.
- Frontend TypeScript validation passes.
- Dependency files (`requirements.txt`, `package.json`) are valid.

## Auto-Fix Policy
1. Prefer programmatic fixes for deterministic issues.
2. Regenerate/fix affected files using AI when programmatic fix is insufficient.
3. Re-run validation after every fix cycle.
4. Block startup until static-analysis errors are zero.

