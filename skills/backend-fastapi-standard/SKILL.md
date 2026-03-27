---
name: backend-fastapi-standard
description: Backend implementation standards for FastAPI services, auth, models, and testing.
type: backend
github_repository: https://github.com/jhsuwm/lunarxpress-customer-service-backend.git
---

# Backend FastAPI Skill

## Configuration
Set `github_repository` in the frontmatter above to your actual backend GitHub repository URL.
Example: `github_repository: https://github.com/my-org/my-backend-service`

## Stack
- Python `3.11+`
- FastAPI
- Pydantic models
- Firestore repository pattern
- JWT-based auth

## Required File Order
1. `requirements.txt` and environment/config files
2. `models/`
3. `auth/`
4. `database/`
5. `services/`
6. `routes/`
7. `main.py`
8. `tests/`

## Rules
- Do not leave unresolved imports.
- Keep route handlers thin; business logic belongs in services.
- Use typed request/response models.
- Add/update tests for changed service and route behavior.
