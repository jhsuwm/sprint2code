# Orion Dev Orchestrator 🚀

An open-source autonomous development agent that transforms product ideas into working code through AI-powered code generation, static analysis, and intelligent auto-fixing.

## 🌟 Features

- **💬 Natural Language to Code**: Describe your product idea in plain English, and watch it transform into production-ready code
- **🤖 AI-Powered Development**: Integrates with Gemini AI to generate complete full-stack applications
- **🔄 GitHub Integration**: Automatically creates branches, commits code, and opens pull requests
- **📋 JIRA Integration**: Creates comprehensive PRDs and manages development workflow with subtasks
- **🔍 Static Analysis**: Validates TypeScript types, Python imports, and dependency integrity before deployment
- **🛠️ Intelligent Auto-Fix**: Detects and automatically fixes code errors through iterative AI refinement
- **🚀 Local Deployment**: Validates code and provides scripts for local development environment setup
- **📊 Real-time Monitoring**: Track code generation, deployment, and app health in real-time

## 🏗️ Architecture

### Backend (Python/FastAPI)
- **Autonomous Dev Agent**: Orchestrates the entire development pipeline
- **Requirements Manager**: Analyzes JIRA stories and manages GitHub repositories
- **Code Execution Manager**: Generates code for each subtask using AI
- **Deployment Manager**: Handles static analysis, auto-fix, and validation
- **Service Integrations**: Gemini AI, GitHub, JIRA, Google Cloud

### Frontend (Next.js/React)
- **Dashboard**: Real-time view of agent execution logs and app health
- **Chat Interface**: Natural language input for product ideas with file attachments
- **Skills Management**: Store and manage technical specifications with modular `SKILL.md` files

## 🚀 Getting Started

### Prerequisites

- Python 3.11+
- Node.js 18+
- GitHub account with Personal Access Token
- JIRA account with API access
- Google Gemini AI API key

### Quick Start (Recommended)

Use the startup script to automatically set up and run both backend and frontend:

```bash
./start.sh
```

The script will:
- ✅ Create Python virtual environment for backend
- ✅ Install all dependencies (backend and frontend)
- ✅ Start backend on http://localhost:8000
- ✅ Start frontend on http://localhost:3000
- ✅ Provide access URLs and next steps
- ✅ Allow easy shutdown with Ctrl+C

### Manual Setup

If you prefer to set up services manually:

**Backend:**

1. Navigate to backend directory:
```bash
cd backend
```

2. Install dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Configure environment variables:
```bash
# Copy the example file and edit with your credentials
cp .env.example .env
# Then edit .env with your actual values
```

Required environment variables in `.env`:
- `GEMINI_API_KEY` - Your Google Gemini AI API key ([Get one here](https://makersuite.google.com/app/apikey))
- `GITHUB_TOKEN` - GitHub Personal Access Token with repo permissions
- `JIRA_URL` - Your JIRA instance URL (e.g., https://yourcompany.atlassian.net)
- `JIRA_EMAIL` - Your JIRA account email
- `JIRA_API_TOKEN` - JIRA API token
- `JWT_SECRET_KEY` - Secret key for JWT token generation (use a strong random string)

**Note:** Google Cloud/Firestore is optional and not required for core functionality. Skills are stored locally in the `/skills` folder.

4. Start the backend:
```bash
python main.py
```

Backend will run on `http://localhost:8000`

**Frontend:**

1. Navigate to frontend directory:
```bash
cd frontend
```

2. Install dependencies:
```bash
npm install
```

3. Configure environment variables (optional - defaults to localhost:8000):
```bash
# Copy the example file if you need custom backend URL
cp .env.local.example .env.local
# Edit .env.local if your backend runs on a different URL
```

4. Start the development server:
```bash
npm run dev
```

Frontend will run on `http://localhost:3000`

## 📝 Usage

### 1. Configure Agent Skills

Create skill folders under `/skills`, each with a `SKILL.md` file that declares technical standards and workflow constraints.

**Use the provided examples:**
```bash
ls skills
```

**Example skill header:**
```md
---
name: frontend-nextjs-standard
description: Frontend implementation standards for Next.js TypeScript apps
type: frontend
---
```

**In the dashboard:** Click **Agent Skills** and select one or more local skills. Selected skills are injected into planning, generation, and auto-fix context.

### 2. Select JIRA Epic

Click "JIRA Space/Epic" to choose where stories will be created.

### 3. Describe Your Product

Use the chat interface to describe your product idea:
- Attach UI designs (PNG, JPG)
- Attach requirement documents (PDF)
- Attach error logs for debugging (TXT, LOG)

### 4. Watch the Magic

The agent will:
1. Generate a comprehensive PRD
2. Create JIRA story and subtasks
3. Generate code for each subtask
4. Run static analysis and auto-fix errors
5. Create pull request for review
6. Provide setup scripts for local testing

## 🛠️ How It Works

### Pipeline Flow

```
Product Idea → PRD Generation → JIRA Story Creation → Work Plan → 
Code Generation → Static Analysis → Auto-Fix → GitHub PR → 
Validated & Ready for Local Testing
```

### Static Analysis

Before deployment, the system validates:
- **Python Imports**: Ensures all imports exist and dependencies are installed
- **TypeScript Types**: Validates interfaces, type safety, and property access
- **Frontend Dependencies**: Checks package.json completeness
- **Property Naming**: Detects inconsistent naming across type definitions

### Auto-Fix System

When errors are detected:
1. **Programmatic Fixes**: Automatically adds missing dependencies
2. **AI-Powered Fixes**: Sends errors back to AI for code regeneration
3. **Iterative Refinement**: Continues until all errors are resolved
4. **Best Result Selection**: Accepts partial fixes if progress plateaus

### Code Validation

The system ensures code quality before PR creation:
- All static analysis errors are resolved
- Dependencies are properly declared
- Code follows the technical specifications
- Ready for local development and testing

## 🧪 Testing Generated Apps Locally

Once the autonomous agent completes code generation and creates a pull request, you can test the generated application locally:

### Quick Start Script

Use the provided shell script to automatically start the generated app:

```bash
./start-app-locally.sh <path-to-generated-repo>
```

**Example:**
```bash
# Clone the generated app from the PR branch
git clone -b feature/ai-generated-story-123 https://github.com/username/my-app.git

# Start the app locally
./start-app-locally.sh ~/projects/my-app
```

The script will:
- ✅ Detect backend (Python/FastAPI) and frontend (Next.js/React)
- ✅ Install dependencies automatically
- ✅ Start both services in the background
- ✅ Provide access URLs and log file locations
- ✅ Allow easy shutdown with Ctrl+C

### Manual Start

Alternatively, start services manually:

**Backend:**
```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

### Stopping Services

If using the script, press `Ctrl+C` or use the PIDs saved in `.orion-pids`:
```bash
kill $(cat .orion-pids | grep BACKEND_PID | cut -d= -f2)
kill $(cat .orion-pids | grep FRONTEND_PID | cut -d= -f2)
```

## 📚 Documentation

Detailed documentation available in `/docs`:
- `AUTONOMOUS_DEV_WORKFLOW.md` - Complete workflow guide
- `AUTONOMOUS_DEV_GITHUB_INTEGRATION.md` - GitHub setup
- `agent_skills_specification.md` - Agent skills implementation specification
- `JIRA_SETUP.md` - JIRA integration guide

## 🔧 Configuration Examples

Sample skills are available in `/skills`:
- `backend-fastapi-standard/SKILL.md` - Backend technical standards
- `frontend-nextjs-standard/SKILL.md` - Frontend technical standards
- `fullstack-quality-gate/SKILL.md` - Static-analysis and auto-fix quality gate

## 🤝 Contributing

Contributions are welcome! Please read our contributing guidelines and submit pull requests.

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🙏 Acknowledgments

- Built with love for the open-source community
- Powered by Google's Gemini AI
- Inspired by the vision of autonomous software development

## 💬 Support

For questions and support, please open an issue on GitHub.

---

**Made with ❤️ by the Orion Dev Team**
