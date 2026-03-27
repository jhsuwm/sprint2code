# Sprint2Code - Setup Guide

## Quick Setup (5 minutes)

### 1. Get Your API Keys

#### Gemini AI API Key
1. Go to [Google AI Studio](https://makersuite.google.com/app/apikey)
2. Click "Create API Key"
3. Copy the key

#### GitHub Personal Access Token
1. Go to GitHub Settings → Developer Settings → Personal Access Tokens → Tokens (classic)
2. Click "Generate new token (classic)"
3. Give it a name (e.g., "Sprint2Code")
4. Select scopes: `repo` (all), `workflow`
5. Click "Generate token" and copy it

#### JIRA API Token
1. Go to [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click "Create API token"
3. Give it a label (e.g., "Sprint2Code")
4. Copy the token

### 2. Configure Environment

```bash
# Navigate to backend
cd backend

# Copy the example environment file
cp .env.example .env

# Edit .env with your actual credentials
nano .env  # or use your preferred editor
```

Fill in your `.env` file:
```env
GEMINI_API_KEY=your_actual_gemini_key
GITHUB_TOKEN=your_actual_github_token
JIRA_URL=https://your-domain.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=your_actual_jira_token
JWT_SECRET_KEY=generate_a_random_32_character_string
```

**Generate JWT Secret:**
```bash
# Use this command to generate a secure random string
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Create Your Config Files

```bash
# Copy example configs
cp config/backend-config-example.yaml config/my-project-backend.yaml
cp config/frontend-config-example.yaml config/my-project-frontend.yaml

# Edit with your GitHub repo URLs and preferences
nano config/my-project-backend.yaml
nano config/my-project-frontend.yaml
```

### 4. Start the Orchestrator

```bash
# From the root directory
./start.sh
```

### 5. Access the Dashboard

1. Open http://localhost:3000 in your browser
2. Log in (create an account on first use)
3. Click "Technical Config" and select your YAML config files
4. Click "JIRA Epic" to select target epic
5. Start describing your product idea!

## Troubleshooting

### Backend won't start
- Check `backend.log` for errors
- Verify all environment variables in `.env`
- Ensure Python 3.11+ is installed: `python3 --version`

### Frontend won't start
- Check `frontend.log` for errors
- Ensure Node.js 18+ is installed: `node --version`
- Try deleting `node_modules` and running `npm install` again

### JIRA connection fails
- Verify your JIRA URL doesn't have trailing slash
- Check your JIRA email and API token are correct
- Ensure you have access to the JIRA project

### GitHub connection fails
- Verify your GitHub token has `repo` and `workflow` permissions
- Check the token hasn't expired

## Next Steps

1. **Test with a simple idea:** "Create a todo list app"
2. **Review the generated code** in the GitHub PR
3. **Test locally** using `./start-app-locally.sh <repo-path>`
4. **Iterate and improve** by describing changes

## Support

For issues or questions:
- Check the main [README.md](README.md)
- Open an issue on GitHub
- Review the documentation in `/docs`
