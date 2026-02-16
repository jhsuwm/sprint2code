"""
Google Cloud Service - Handles deployment to Google Cloud Run and log retrieval
"""
import os
import subprocess
import tempfile
import yaml
import json
from typing import Dict, Any, Optional, List, Tuple
from log_config import logger, error


class GoogleCloudService:
    def __init__(self):
        # Use liquid-terra-450614-b6 as default project ID if not set
        self.project_id = os.getenv('GOOGLE_CLOUD_PROJECT_ID') or 'liquid-terra-450614-b6'
        self.region = os.getenv('GOOGLE_CLOUD_REGION', 'us-central1')
        
        # Set up Google Cloud authentication for subprocess
        # Check if service account key exists
        service_account_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
        if not service_account_path or not os.path.exists(service_account_path):
            logger.warning("GOOGLE_APPLICATION_CREDENTIALS not set or file not found. Using default credentials.")
    
    def generate_deployment_files(self, 
                                   client_name: str,
                                   project_path: str,
                                   subdomain: str,
                                   epic_key: str,
                                   job_id: str = "") -> Dict[str, str]:
        """
        Generate deployment files (Dockerfiles, deploy script, Cloud Run YAML) for client app.
        
        Args:
            client_name: Name of the client
            project_path: Path to the generated code
            subdomain: Subdomain for the app
            epic_key: JIRA epic key for naming resources
            job_id: Unique job ID for log filtering
            
        Returns:
            Dictionary with file paths and contents
        """
        logger.info(f"Generating deployment files for {client_name}")
        
        # Unique SaaS naming convention to avoid conflicts between clients and epics
        # Backend: develop-backend-<company_name>-<jira_epic_key>
        # Frontend: develop-frontend-<company_name>-<jira_epic_key>
        safe_company = client_name.lower().replace('_', '-')
        safe_epic = epic_key.lower().replace('_', '-') if epic_key else "unknown"
        
        backend_service_name = f"develop-backend-{safe_company}-{safe_epic}"
        frontend_service_name = f"develop-frontend-{safe_company}-{safe_epic}"
        
        files = {}
        
        # Backend Dockerfile
        files['Dockerfile.backend'] = f"""# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \\
    gcc \\
    g++ \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY backend/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create a non-root user
RUN useradd -m -u 1000 appuser

# Copy backend code - handle both monorepo and flat repo structures
COPY . .
# If it's a monorepo structure, move backend files to root if they exist
RUN if [ -d "backend" ]; then cp -r backend/* . && rm -rf backend; fi

# Set ownership
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port 8000 (FastAPI standard)
# Cloud Run will set PORT env variable to match containerPort in YAML
EXPOSE 8000

# Start the application
# Cloud Run automatically sets PORT env variable, default to 8000 for local testing
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${{PORT:-8000}} --workers 1 --log-level info"]
"""

        # Frontend Dockerfile (Next.js Multi-stage)
        files['Dockerfile.frontend'] = f"""FROM node:18-alpine AS base

# Install dependencies only when needed
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app

# Install dependencies - handle both monorepo and flat repo structures
COPY . ./
# If it's a monorepo structure, move frontend files to current dir
RUN if [ -d "frontend" ]; then \
      cp frontend/package.json frontend/package-lock.json* ./ 2>/dev/null || cp frontend/package.json ./; \
    fi

RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# Rebuild the source code only when needed
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN if [ -d "frontend" ]; then cp -r frontend/* . && rm -rf frontend; fi

# Set environment variables for build
ARG BACKEND_URL
ENV NEXT_PUBLIC_BACKEND_URL=$BACKEND_URL
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# Production image, copy all the files and run next
FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copy built assets
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
"""

        # Cloud Run YAML for backend
        # Note: PORT env variable is automatically set by Cloud Run based on containerPort
        # Note: No health probes specified - Cloud Run will use default TCP checks
        files['cloud-run-backend.yaml'] = f"""apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: {backend_service_name}
  labels:
    cloud.googleapis.com/location: {self.region}
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: '1'
        autoscaling.knative.dev/maxScale: '10'
    spec:
      containerConcurrency: 80
      timeoutSeconds: 300
      containers:
      - image: gcr.io/{self.project_id}/{backend_service_name}:latest
        ports:
        - name: http1
          containerPort: 8000
        env:
        - name: GOOGLE_CLOUD_PROJECT_ID
          value: '{self.project_id}'
        - name: FIRESTORE_DATABASE_ID
          value: '(default)'
        - name: JWT_SECRET_KEY
          value: 'dummy-jwt-secret-key-for-testing'
        - name: JWT_ALGORITHM
          value: 'HS256'
        - name: CORS_ORIGINS
          value: 'https://{subdomain}'
        - name: ROOSTER_JOB_ID
          value: '{job_id}'
        resources:
          limits:
            cpu: '1000m'
            memory: '512Mi'
"""

        # Cloud Run YAML for frontend
        # Note: PORT env variable is automatically set by Cloud Run and should not be specified
        backend_url = f"https://{backend_service_name}-{self._get_cloud_run_hash()}.a.run.app"
        files['cloud-run-frontend.yaml'] = f"""apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: {frontend_service_name}
  labels:
    cloud.googleapis.com/location: {self.region}
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: '1'
        autoscaling.knative.dev/maxScale: '10'
    spec:
      containerConcurrency: 80
      timeoutSeconds: 60
      containers:
      - image: gcr.io/{self.project_id}/{frontend_service_name}:latest
        ports:
        - name: http1
          containerPort: 3000
        env:
        - name: NEXT_PUBLIC_BACKEND_URL
          value: '{backend_url}'
        - name: ROOSTER_JOB_ID
          value: '{job_id}'
        resources:
          limits:
            cpu: '1000m'
            memory: '512Mi'
"""

        # Deployment script
        files['deploy.sh'] = f"""#!/bin/bash
# {client_name.title()} - Google Cloud Run Deployment Script
# Generated by Autonomous Dev Agent

set -e  # Exit on error

# Colors for output
RED='\\033[0;31m'
GREEN='\\033[0;32m'
YELLOW='\\033[1;33m'
BLUE='\\033[0;34m'
NC='\\033[0m'

print_status() {{
    echo -e "${{BLUE}}[INFO]${{NC}} $1"
}}

print_success() {{
    echo -e "${{GREEN}}[SUCCESS]${{NC}} $1"
}}

print_error() {{
    echo -e "${{RED}}[ERROR]${{NC}} $1"
}}

print_warning() {{
    echo -e "${{YELLOW}}[WARNING]${{NC}} $1"
}}

PROJECT_ID="{self.project_id}"
REGION="{self.region}"
BACKEND_SERVICE="{backend_service_name}"
FRONTEND_SERVICE="{frontend_service_name}"
SUBDOMAIN="{subdomain}"

print_status "Starting deployment for {client_name.title()}..."
print_status "Project: $PROJECT_ID"
print_status "Region: $REGION"
print_status "Subdomain: $SUBDOMAIN"

# Build and push backend
print_status "Building backend Docker image..."
cp Dockerfile.backend Dockerfile
BUILD_OUTPUT=$(gcloud builds submit --tag gcr.io/$PROJECT_ID/$BACKEND_SERVICE:latest --project=$PROJECT_ID --async . 2>&1)
BUILD_ID=$(echo "$BUILD_OUTPUT" | grep -o 'builds/[a-f0-9-]*' | head -1 | cut -d'/' -f2)
rm -f Dockerfile

if [ -z "$BUILD_ID" ]; then
    print_error "Failed to extract build ID from gcloud output"
    print_error "Output was: $BUILD_OUTPUT"
    exit 1
fi

print_status "Backend build submitted: $BUILD_ID"

# Poll build status
print_status "Waiting for backend build to complete..."
while true; do
    STATUS=$(gcloud builds describe $BUILD_ID --project=$PROJECT_ID --format="value(status)" 2>/dev/null || echo "UNKNOWN")
    print_status "Backend build status: $STATUS"
    if [ "$STATUS" = "SUCCESS" ]; then
        print_success "Backend build completed successfully"
        break
    elif [ "$STATUS" = "FAILURE" ] || [ "$STATUS" = "CANCELLED" ] || [ "$STATUS" = "TIMEOUT" ]; then
        echo "--- START OF BACKEND BUILD LOGS ---"
        print_error "Backend build failed with status: $STATUS"
        print_status "Extracting failure details..."
        gcloud builds describe $BUILD_ID --project=$PROJECT_ID --format="value(failureInfo.detail)" || true
        gcloud builds log $BUILD_ID --project=$PROJECT_ID || {{
            print_status "Fallback: Retrieving logs via gcloud logging..."
            gcloud logging read "resource.type=\"build\" AND resource.labels.build_id=\"$BUILD_ID\"" --project=$PROJECT_ID --format="value(textPayload)" --limit=500 || true
        }}
        echo "--- END OF BACKEND BUILD LOGS ---"
        exit 1
    fi
    sleep 20
done
print_success "Backend image built and pushed"

# Deploy backend with force to ensure latest image is used
print_status "Deploying backend to Cloud Run..."
gcloud run services replace cloud-run-backend.yaml \\
    --region=$REGION \\
    --project=$PROJECT_ID

# Force update to latest image by deleting old revisions (keep only latest 3)
print_status "Cleaning up old backend revisions..."
REVISIONS=$(gcloud run revisions list \\
    --service=$BACKEND_SERVICE \\
    --region=$REGION \\
    --project=$PROJECT_ID \\
    --format="value(name)" \\
    --sort-by="~metadata.creationTimestamp" \\
    --limit=100)

# Keep first 3, delete the rest
REVISION_COUNT=0
for REV in $REVISIONS; do
    REVISION_COUNT=$((REVISION_COUNT + 1))
    if [ $REVISION_COUNT -gt 3 ]; then
        print_status "Deleting old revision: $REV"
        gcloud run revisions delete $REV \\
            --region=$REGION \\
            --project=$PROJECT_ID \\
            --quiet || true
    fi
done

gcloud run services add-iam-policy-binding $BACKEND_SERVICE \\
    --member="allUsers" \\
    --role="roles/run.invoker" \\
    --region=$REGION \\
    --project=$PROJECT_ID
BACKEND_URL=$(gcloud run services describe $BACKEND_SERVICE \\
    --region=$REGION \\
    --project=$PROJECT_ID \\
    --format="value(status.url)")
print_success "Backend deployed at: $BACKEND_URL"

# Build and push frontend with build args
print_status "Building frontend Docker image..."
# Use gcloud builds submit with build-config to pass build args
cat > cloudbuild-frontend.yaml << 'CLOUDBUILD_EOF'
steps:
- name: 'gcr.io/cloud-builders/docker'
  args:
  - 'build'
  - '-t'
  - 'gcr.io/$PROJECT_ID/$FRONTEND_SERVICE:latest'
  - '--build-arg'
  - 'BACKEND_URL=$BACKEND_URL'
  - '-f'
  - 'Dockerfile.frontend'
  - '.'
images:
- 'gcr.io/$PROJECT_ID/$FRONTEND_SERVICE:latest'
CLOUDBUILD_EOF

# Replace placeholders in cloudbuild config
sed -i.bak "s|\$PROJECT_ID|$PROJECT_ID|g" cloudbuild-frontend.yaml
sed -i.bak "s|\$FRONTEND_SERVICE|$FRONTEND_SERVICE|g" cloudbuild-frontend.yaml  
sed -i.bak "s|\$BACKEND_URL|$BACKEND_URL|g" cloudbuild-frontend.yaml
rm -f cloudbuild-frontend.yaml.bak

BUILD_OUTPUT=$(gcloud builds submit --config=cloudbuild-frontend.yaml --project=$PROJECT_ID --async . 2>&1)
BUILD_ID=$(echo "$BUILD_OUTPUT" | grep -o 'builds/[a-f0-9-]*' | head -1 | cut -d'/' -f2)

if [ -z "$BUILD_ID" ]; then
    print_error "Failed to extract build ID from gcloud output"
    print_error "Output was: $BUILD_OUTPUT"
    exit 1
fi

print_status "Frontend build submitted: $BUILD_ID"

# Poll build status
print_status "Waiting for frontend build to complete..."
while true; do
    STATUS=$(gcloud builds describe $BUILD_ID --project=$PROJECT_ID --format="value(status)" 2>/dev/null || echo "UNKNOWN")
    print_status "Frontend build status: $STATUS"
    if [ "$STATUS" = "SUCCESS" ]; then
        print_success "Frontend build completed successfully"
        break
    elif [ "$STATUS" = "FAILURE" ] || [ "$STATUS" = "CANCELLED" ] || [ "$STATUS" = "TIMEOUT" ]; then
        echo "--- START OF FRONTEND BUILD LOGS ---"
        print_error "Frontend build failed with status: $STATUS"
        print_status "Extracting failure details..."
        gcloud builds describe $BUILD_ID --project=$PROJECT_ID --format="value(failureInfo.detail)" || true
        gcloud builds log $BUILD_ID --project=$PROJECT_ID || {{
            print_status "Fallback: Retrieving logs via gcloud logging..."
            gcloud logging read "resource.type=\"build\" AND resource.labels.build_id=\"$BUILD_ID\"" --project=$PROJECT_ID --format="value(textPayload)" --limit=500 || true
        }}
        echo "--- END OF FRONTEND BUILD LOGS ---"
        rm -f cloudbuild-frontend.yaml
        exit 1
    fi
    sleep 20
done
rm -f cloudbuild-frontend.yaml
print_success "Frontend image built and pushed"

# Update frontend YAML with actual backend URL
# Use a more robust sed that works on both macOS and Linux
if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s|value: 'https://.*\.run\.app'|value: '$BACKEND_URL'|g" cloud-run-frontend.yaml
else
    sed -i "s|value: 'https://.*\.run\.app'|value: '$BACKEND_URL'|g" cloud-run-frontend.yaml
fi

# Deploy frontend with force to ensure latest image is used
print_status "Deploying frontend to Cloud Run..."
gcloud run services replace cloud-run-frontend.yaml \\
    --region=$REGION \\
    --project=$PROJECT_ID

# Force update to latest image by deleting old revisions (keep only latest 3)
print_status "Cleaning up old frontend revisions..."
REVISIONS=$(gcloud run revisions list \\
    --service=$FRONTEND_SERVICE \\
    --region=$REGION \\
    --project=$PROJECT_ID \\
    --format="value(name)" \\
    --sort-by="~metadata.creationTimestamp" \\
    --limit=100)

# Keep first 3, delete the rest
REVISION_COUNT=0
for REV in $REVISIONS; do
    REVISION_COUNT=$((REVISION_COUNT + 1))
    if [ $REVISION_COUNT -gt 3 ]; then
        print_status "Deleting old revision: $REV"
        gcloud run revisions delete $REV \\
            --region=$REGION \\
            --project=$PROJECT_ID \\
            --quiet || true
    fi
done

gcloud run services add-iam-policy-binding $FRONTEND_SERVICE \\
    --member="allUsers" \\
    --role="roles/run.invoker" \\
    --region=$REGION \\
    --project=$PROJECT_ID
FRONTEND_URL=$(gcloud run services describe $FRONTEND_SERVICE \\
    --region=$REGION \\
    --project=$PROJECT_ID \\
    --format="value(status.url)")
print_success "Frontend deployed at: $FRONTEND_URL"

# Map custom domain
print_status "Mapping custom domain $SUBDOMAIN to $FRONTEND_SERVICE..."
# Check if mapping already exists
if gcloud beta run domain-mappings describe --domain=$SUBDOMAIN --region=$REGION --project=$PROJECT_ID &>/dev/null; then
    print_status "Domain mapping for $SUBDOMAIN already exists, checking if it points to $FRONTEND_SERVICE..."
    CURRENT_SERVICE=$(gcloud beta run domain-mappings describe --domain=$SUBDOMAIN --region=$REGION --project=$PROJECT_ID --format="value(spec.routeSequence[0].routeName)" 2>/dev/null || gcloud beta run domain-mappings describe --domain=$SUBDOMAIN --region=$REGION --project=$PROJECT_ID --format="value(metadata.name)" | cut -d'.' -f1)
    
    if [[ "$CURRENT_SERVICE" != "$FRONTEND_SERVICE" ]]; then
        print_warning "Domain $SUBDOMAIN is mapped to $CURRENT_SERVICE, not $FRONTEND_SERVICE. Re-mapping..."
        gcloud beta run domain-mappings delete --domain=$SUBDOMAIN --region=$REGION --project=$PROJECT_ID --quiet || true
        gcloud beta run domain-mappings create --service=$FRONTEND_SERVICE --domain=$SUBDOMAIN --region=$REGION --project=$PROJECT_ID
    else
        print_success "Domain $SUBDOMAIN is already correctly mapped to $FRONTEND_SERVICE"
    fi
else
    print_status "Creating new domain mapping for $SUBDOMAIN..."
    gcloud beta run domain-mappings create --service=$FRONTEND_SERVICE --domain=$SUBDOMAIN --region=$REGION --project=$PROJECT_ID || print_warning "Failed to create domain mapping (might need manual intervention)"
fi

# Update DNS record if managed zone exists
ZONE_NAME=$(gcloud dns managed-zones list --filter="dnsName:roosterjourney.com." --format="value(name)" --project=$PROJECT_ID 2>/dev/null)
if [ ! -z "$ZONE_NAME" ]; then
    print_status "Updating DNS record for $SUBDOMAIN in zone $ZONE_NAME..."
    
    # Check if record exists
    EXISTING_RECORD=$(gcloud dns record-sets list --zone=$ZONE_NAME --name="$SUBDOMAIN." --format="csv[no-heading](type,ttl,rrdatas[0])" --project=$PROJECT_ID 2>/dev/null)
    
    gcloud dns record-sets transaction start --zone=$ZONE_NAME --project=$PROJECT_ID
    
    if [ ! -z "$EXISTING_RECORD" ]; then
        print_status "Removing existing record for $SUBDOMAIN..."
        # Correctly parse CSV fields even if they contain spaces
        TYPE=$(echo "$EXISTING_RECORD" | cut -d',' -f1)
        TTL=$(echo "$EXISTING_RECORD" | cut -d',' -f2)
        DATA=$(echo "$EXISTING_RECORD" | cut -d',' -f3)
        gcloud dns record-sets transaction remove --name="$SUBDOMAIN." --type="$TYPE" --ttl="$TTL" "$DATA" --zone="$ZONE_NAME" --project="$PROJECT_ID"
    fi
    
    print_status "Adding new CNAME record for $SUBDOMAIN -> ghs.googlehosted.com."
    gcloud dns record-sets transaction add --name="$SUBDOMAIN." --type=CNAME --ttl=300 "ghs.googlehosted.com." --zone="$ZONE_NAME" --project="$PROJECT_ID"
    
    if ! gcloud dns record-sets transaction execute --zone="$ZONE_NAME" --project="$PROJECT_ID"; then
        print_warning "Failed to execute DNS transaction, aborting..."
        gcloud dns record-sets transaction abort --zone="$ZONE_NAME" --project="$PROJECT_ID" || true
    else
        print_success "DNS record updated successfully for $SUBDOMAIN"
    fi
fi

print_success "🎉 Deployment and domain mapping completed successfully!"
echo ""
echo -e "${{GREEN}}Application URLs:${{NC}}"
echo -e "${{BLUE}}Frontend:${{NC}} $FRONTEND_URL"
echo -e "${{BLUE}}Backend:${{NC}} $BACKEND_URL"
echo ""
echo -e "${{YELLOW}}Note:${{NC}} Map custom domain $SUBDOMAIN to $FRONTEND_URL in Cloud DNS"
"""

        return files
    
    def _get_cloud_run_hash(self) -> str:
        """Generate a deterministic hash for Cloud Run URL"""
        # This is a placeholder - actual URL hash is generated by GCP
        import hashlib
        hash_obj = hashlib.md5(self.project_id.encode())
        return hash_obj.hexdigest()[:10]
    
    def deploy_to_cloud_run(self, 
                            project_path: str,
                            deployment_files: Dict[str, str],
                            client_name: str,
                            epic_key: str) -> Tuple[bool, str, Dict[str, str]]:
        """
        Deploy client app to Google Cloud Run.
        
        Args:
            project_path: Path to the project code
            deployment_files: Dictionary of deployment file contents
            client_name: Name of the client
            epic_key: JIRA epic key
            
        Returns:
            Tuple of (success, message, urls_dict)
        """
        logger.info(f"Deploying {client_name} to Cloud Run...")
        
        try:
            # Create temporary directory for deployment
            with tempfile.TemporaryDirectory() as temp_dir:
                # Write deployment files
                for filename, content in deployment_files.items():
                    file_path = os.path.join(temp_dir, filename)
                    with open(file_path, 'w') as f:
                        f.write(content)
                    logger.info(f"Written {filename}")
                
                # Make deploy.sh executable
                deploy_script = os.path.join(temp_dir, 'deploy.sh')
                os.chmod(deploy_script, 0o755)
                
                # Execute deployment script
                logger.info("Executing deployment script...")
                result = subprocess.run(
                    ['bash', deploy_script],
                    cwd=temp_dir,
                    capture_output=True,
                    text=True,
                    timeout=1800  # 30 minute timeout
                )
                
                if result.returncode == 0:
                    logger.info("Deployment successful")
                    # Parse URLs from output
                    output = result.stdout
                    backend_url = self._extract_url(output, 'Backend')
                    frontend_url = self._extract_url(output, 'Frontend')
                    
                    return True, "Deployment successful", {
                        'backend_url': backend_url,
                        'frontend_url': frontend_url
                    }
                else:
                    error_msg = f"Deployment failed: {result.stderr}"
                    error(error_msg, "GoogleCloudService")
                    return False, error_msg, {}
        
        except subprocess.TimeoutExpired:
            error_msg = "Deployment timed out after 30 minutes"
            error(error_msg, "GoogleCloudService")
            return False, error_msg, {}
        except Exception as e:
            error_msg = f"Deployment error: {str(e)}"
            error(error_msg, "GoogleCloudService")
            return False, error_msg, {}
    
    def _extract_url(self, output: str, service_type: str) -> str:
        """Extract URL from deployment output"""
        import re
        pattern = rf"{service_type}:\s*(https://[^\s]+)"
        match = re.search(pattern, output)
        return match.group(1) if match else ""
    
    def get_latest_revision(self, service_name: str) -> Optional[str]:
        """
        Get the latest revision name for a Cloud Run service.
        This is CRITICAL for retrieving logs from failed deployments.
        
        Args:
            service_name: Name of the Cloud Run service
            
        Returns:
            Revision name (e.g., "service-name-00001-abc") or None
        """
        try:
            result = subprocess.run([
                'gcloud', 'run', 'services', 'describe', service_name,
                '--region', self.region,
                '--project', self.project_id,
                '--format', 'value(status.latestCreatedRevisionName)'
            ], capture_output=True, text=True, timeout=15)
            
            if result.returncode == 0 and result.stdout.strip():
                revision = result.stdout.strip()
                logger.info(f"Latest revision for {service_name}: {revision}")
                return revision
            else:
                logger.warning(f"Could not get latest revision for {service_name}")
                return None
        except Exception as e:
            error(f"Error getting latest revision: {str(e)}", "GoogleCloudService")
            return None
    
    def get_cloud_run_logs(self, 
                           service_name: str,
                           limit: int = 100,
                           severity: Optional[str] = None,
                           start_time: Optional[str] = None,
                           job_id: Optional[str] = None) -> List[Dict[str, Any]]:
        # Debug logging for troubleshooting log retrieval
        logger.info(f"DEBUG: get_cloud_run_logs(service={service_name}, limit={limit}, severity={severity}, start_time={start_time}, job_id={job_id})")
        """
        Retrieve logs from Cloud Run service with full metadata.
        CRITICAL: For failed deployments, this retrieves logs from the specific failed revision.
        
        Args:
            service_name: Name of the Cloud Run service
            limit: Number of log entries to retrieve
            severity: Filter by severity (ERROR, WARNING, INFO, etc.)
            start_time: ISO timestamp to filter logs after (e.g., '2025-12-25T10:00:00Z')
            
            
        Returns:
            List of log entry dicts with timestamp, severity, and message
        """
        try:
            logger.info(f"Fetching logs for {service_name} (severity={severity}, start_time={start_time}, job_id={job_id})")
            
            # CRITICAL: Use SIMPLEST filter possible to get ALL logs
            # Complex filters often miss important startup error logs
            # We filter the content in Python after retrieving ALL logs
            # We include both service_name and job_name labels to cover all cases
            filter_str = f'resource.labels.service_name="{service_name}" OR resource.labels.job_name="{service_name}"'
            
            # DON'T filter by execution_environment - it often excludes startup errors
            # DON'T filter by severity here - we want ALL logs including INFO that might contain errors
            # We'll filter in Python after retrieval
            
            # Use a more relaxed start_time (back 5 minutes) to account for processing delays and clock skew
            if start_time:
                try:
                    from datetime import datetime, timedelta
                    dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    safe_dt = dt - timedelta(minutes=5)
                    filter_str += f' AND timestamp>="{safe_dt.isoformat()}"'
                except Exception:
                    filter_str += f' AND timestamp>="{start_time}"'
            
            # Use gcloud logging to get more detailed logs with metadata
            cmd = [
                'gcloud', 'logging', 'read',
                filter_str,
                '--project', self.project_id,
                '--limit', str(limit),
                '--format', 'json',
                '--freshness', '1h'  # Keep within 1h freshness
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            if result.returncode == 0:
                try:
                    logs_data = json.loads(result.stdout) if result.stdout.strip() else []
                    logger.info(f"DEBUG: Raw logs count: {len(logs_data)}")
                    if len(logs_data) > 0:
                        logger.info(f"DEBUG: First raw log entry: {json.dumps(logs_data[0])[:500]}")
                    
                    # Parse and format logs
                    formatted_logs = []
                    for entry in logs_data:
                        timestamp = entry.get('timestamp', 'N/A')
                        severity_level = entry.get('severity', 'INFO')

                        # Filter by severity if specified
                        if severity and severity_level not in ['ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY']:
                            if severity == "WARNING" and severity_level != "WARNING":
                                continue
                            if severity == "ERROR": # Only want actual errors
                                continue
                        
                        # Extract message from textPayload or jsonPayload
                        message = entry.get('textPayload', '').strip()
                        
                        # If no textPayload, try jsonPayload or protoPayload
                        if not message:
                            json_payload = entry.get('jsonPayload', {})
                            proto_payload = entry.get('protoPayload', {})
                            
                            if json_payload:
                                # Skip empty json payloads like {}
                                if json_payload != {}:
                                    # Try common message fields
                                    message = (json_payload.get('message') or 
                                              json_payload.get('msg') or 
                                              json_payload.get('textPayload') or
                                              json_payload.get('error'))
                                    
                                    # If still no message, convert full JSON to string (but skip if it's just metadata)
                                    if not message:
                                        # Only include if it has content
                                        if len(json_payload) > 0:
                                            message = json.dumps(json_payload)
                            
                            if not message and proto_payload:
                                # Handle audit logs and other proto payloads
                                if '@type' in proto_payload and 'AuditLog' in proto_payload['@type']:
                                    method = proto_payload.get('methodName', 'Unknown Method')
                                    resource = proto_payload.get('resourceName', 'Unknown Resource')
                                    status = proto_payload.get('status', {})
                                    msg = status.get('message', '')
                                    message = f"Audit: {method} on {resource}"
                                    if msg:
                                        message += f" - Status: {msg}"
                                else:
                                    message = json.dumps(proto_payload)
                        
                        # Only add if we have actual content
                        if message and message.strip() and message.strip() != '{}':
                            formatted_logs.append({
                                'timestamp': timestamp,
                                'severity': severity_level,
                                'message': message
                            })
                    
                    logger.info(f"DEBUG: Formatted {len(formatted_logs)} non-empty logs for {service_name}")
                    
                    # If we got no logs, try a simpler filter without execution environment
                    if len(formatted_logs) == 0:
                        logger.warning(f"DEBUG: No logs found, trying simple filter...")
                        return self._get_logs_simple_filter(service_name, limit, severity, start_time, job_id)
                    
                    # Sort logs by timestamp ascending (oldest first)
                    formatted_logs.sort(key=lambda x: x.get('timestamp', ''))
                    return formatted_logs
                except json.JSONDecodeError as e:
                    error(f"Failed to parse log JSON: {e}", "GoogleCloudService")
                    return []
            else:
                logger.warning(f"gcloud logging read failed: {result.stderr}")
                # Try fallback method
                return self._get_logs_simple_filter(service_name, limit, severity, start_time, job_id)
        
        except Exception as e:
            error(f"Error fetching logs: {str(e)}", "GoogleCloudService")
            return []
    
    def _get_logs_simple_filter(self,
                                service_name: str,
                                limit: int = 100,
                                severity: Optional[str] = None,
                                start_time: Optional[str] = None,
                                job_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fallback method to get logs with simpler filter.
        """
        try:
            logger.info(f"Using simple filter for {service_name} (job_id={job_id})")
            
            # Simplest possible filter - just get any logs from this service
            filter_str = f'resource.labels.service_name="{service_name}"'
            
            if start_time:
                try:
                    from datetime import datetime, timedelta
                    dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    safe_dt = dt - timedelta(minutes=5)
                    filter_str += f' AND timestamp>="{safe_dt.isoformat()}"'
                except Exception:
                    filter_str += f' AND timestamp>="{start_time}"'
            
            cmd = [
                'gcloud', 'logging', 'read',
                filter_str,
                '--project', self.project_id,
                '--limit', str(limit),
                '--format', 'json'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            if result.returncode == 0:
                logs_data = json.loads(result.stdout) if result.stdout.strip() else []
                logger.info(f"Simple filter retrieved {len(logs_data)} raw entries")
                
                formatted_logs = []
                for entry in logs_data:
                    timestamp = entry.get('timestamp', 'N/A')
                    severity_level = entry.get('severity', 'INFO')

                    # Filter by severity if specified
                    if severity and severity_level not in ['ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY']:
                        if severity == "WARNING" and severity_level != "WARNING":
                            continue
                        if severity == "ERROR":
                            continue
                    
                    # Get any available message
                    message = entry.get('textPayload', '').strip()
                    if not message:
                        json_payload = entry.get('jsonPayload', {})
                        proto_payload = entry.get('protoPayload', {})
                        if json_payload and json_payload != {}:
                            message = str(json_payload)
                        elif proto_payload and proto_payload != {}:
                            if '@type' in proto_payload and 'AuditLog' in proto_payload['@type']:
                                method = proto_payload.get('methodName', 'Unknown Method')
                                message = f"Audit: {method}"
                                if 'status' in proto_payload and 'message' in proto_payload['status']:
                                    message += f" - {proto_payload['status']['message']}"
                            else:
                                message = str(proto_payload)
                    
                    # Filter by severity if specified
                    if severity and severity_level not in ['ERROR', 'CRITICAL', 'WARNING']:
                        continue
                    
                    if message and message.strip() != '{}':
                        formatted_logs.append({
                            'timestamp': timestamp,
                            'severity': severity_level,
                            'message': message
                        })
                
                logger.info(f"Simple filter returned {len(formatted_logs)} formatted logs")
                # Sort logs by timestamp ascending (oldest first)
                formatted_logs.sort(key=lambda x: x.get('timestamp', ''))
                return formatted_logs
            
            return []
        except Exception as e:
            error(f"Simple filter failed: {str(e)}", "GoogleCloudService")
            return []
    
    async def get_startup_logs(self, service_name: str, job_id: Optional[str] = None, start_time: Optional[str] = None) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        Get startup logs and determine if service started successfully.
        
        Args:
            service_name: Name of the Cloud Run service
            job_id: Unique job ID for log filtering
            start_time: ISO timestamp to filter logs after
            
        Returns:
            Tuple of (is_healthy, logs_list)
        """
        try:
            logger.info(f"Checking startup health for {service_name} (job_id={job_id}, start_time={start_time})")
            
            # Get recent logs, focusing on errors
            all_logs = self.get_cloud_run_logs(service_name, limit=200, job_id=job_id, start_time=start_time)
            
            # Check for startup failure indicators
            startup_failed = False
            error_logs = []
            
            for log_entry in all_logs:
                message = log_entry.get('message', '')
                severity = log_entry.get('severity', 'INFO')
                
                # Check for common startup failure patterns
                failure_patterns = [
                    'SyntaxError',
                    'ImportError',
                    'ModuleNotFoundError',
                    'failed to start',
                    'container failed',
                    'startup probe',
                    'Traceback',
                    'ERROR'
                ]
                
                if severity in ['ERROR', 'CRITICAL'] or any(pattern in message for pattern in failure_patterns):
                    startup_failed = True
                    error_logs.append(log_entry)
            
            # Get service status to confirm
            status = await self.get_service_status(service_name)
            is_healthy = status == 'SUCCESSFUL' and not startup_failed
            
            logger.info(f"Service {service_name} health status: {'HEALTHY' if is_healthy else 'UNHEALTHY'}")
            
            # Return error logs if unhealthy, otherwise return up to 100 latest logs
            # If unhealthy but no specific error patterns found, return all recent logs
            if not is_healthy and not error_logs:
                return is_healthy, all_logs[-100:]
            return is_healthy, error_logs if not is_healthy else all_logs[-100:]
        
        except Exception as e:
            error(f"Error checking startup health: {str(e)}", "GoogleCloudService")
            return False, []
    
    async def get_service_status(self, service_name: str) -> Optional[str]:
        """
        Get the status of a Cloud Run service.
        
        Args:
            service_name: Name of the Cloud Run service
            
        Returns:
            Service status or None if failed
        """
        try:
            # Get full service details
            result = subprocess.run([
                'gcloud', 'run', 'services', 'describe', service_name,
                '--region', self.region,
                '--project', self.project_id,
                '--format', 'json'
            ], capture_output=True, text=True, timeout=15)
            
            if result.returncode == 0:
                try:
                    service_data = json.loads(result.stdout)
                    conditions = service_data.get('status', {}).get('conditions', [])
                    
                    # Check Ready condition
                    for condition in conditions:
                        if condition.get('type') == 'Ready':
                            status = condition.get('status')
                            return 'SUCCESSFUL' if status == 'True' else 'FAILED'
                    
                    return 'UNKNOWN'
                except json.JSONDecodeError:
                    return 'FAILED'
            else:
                logger.warning(f"Failed to get status for {service_name}: {result.stderr}")
                return 'FAILED'
        
        except Exception as e:
            error(f"Error getting service status: {str(e)}", "GoogleCloudService")
            return 'FAILED'
    
    async def wait_for_service_ready(self, service_name: str, timeout: int = 300) -> Tuple[bool, str]:
        """
        Wait for Cloud Run service to become ready or fail.
        With --async builds, we need to wait for the service to be created first.
        
        Args:
            service_name: Name of the Cloud Run service
            timeout: Maximum time to wait in seconds
            
        Returns:
            Tuple of (is_ready, status_message)
        """
        import time
        import asyncio
        start_time = time.time()
        
        logger.info(f"Waiting for {service_name} to be ready (timeout: {timeout}s)")
        
        # First, wait for service to exist (it may not exist yet with --async builds)
        service_exists = False
        while time.time() - start_time < timeout:
            status = await self.get_service_status(service_name)
            
            if status and status != 'FAILED':
                service_exists = True
                logger.info(f"{service_name} service exists, checking readiness...")
                break
            
            # Service doesn't exist yet, wait and retry
            logger.info(f"Waiting for {service_name} to be created (async build may still be running)...")
            await asyncio.sleep(15)
        
        if not service_exists:
            logger.error(f"{service_name} was not created within {timeout}s")
            return False, f"Service was not created within {timeout}s (async build may have failed)"
        
        # Now wait for service to become ready
        while time.time() - start_time < timeout:
            status = await self.get_service_status(service_name)
            
            if status == 'SUCCESSFUL':
                logger.info(f"{service_name} is ready!")
                return True, "Service started successfully"
            elif status == 'FAILED':
                logger.error(f"{service_name} failed to start")
                return False, "Service failed to start"
            
            # Wait before checking again
            await asyncio.sleep(10)
        
        logger.error(f"{service_name} did not become ready within {timeout}s")
        return False, f"Service did not become ready within {timeout}s"
    
    def format_logs_for_display(self, logs: List[Dict[str, Any]]) -> str:
        """
        Format logs for display in JIRA or UI.
        
        Args:
            logs: List of log entry dicts
            
        Returns:
            Formatted log string
        """
        if not logs:
            return "No logs available"
        
        formatted = []
        for entry in logs:
            timestamp = entry.get('timestamp', 'N/A')
            severity = entry.get('severity', 'INFO')
            message = entry.get('message', '').strip()
            
            # Format timestamp to be more readable
            if timestamp != 'N/A' and 'T' in timestamp:
                timestamp = timestamp.split('.')[0].replace('T', ' ')
            
            formatted.append(f"[{timestamp}] [{severity}] {message}")
        
        return '\n'.join(formatted)
