---
name: frontend-nextjs-standard
description: Frontend implementation standards for Next.js TypeScript apps with API clients and state management.
type: frontend
github_repository: https://github.com/jhsuwm/lunarxpress-customer-service-frontend.git
---

# Frontend Next.js Skill

## Configuration
Set `github_repository` in the frontmatter above to your actual frontend GitHub repository URL.
Example: `github_repository: https://github.com/my-org/my-frontend-app`

## Stack
- Next.js (App Router)
- TypeScript
- Tailwind CSS
- Axios-based API client
- Zustand state stores

## Required File Order
1. `package.json`, `tsconfig.json`, `next.config.js`
2. `src/types/`
3. `src/api/`
4. `src/store/`
5. `src/components/`
6. `app/` pages/layout
7. tests

## Rules
- Declare dependencies before importing them.
- Keep API types and UI props strongly typed.
- Do not leave TypeScript errors unresolved.
- Keep reusable UI in `src/components`.
