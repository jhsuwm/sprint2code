"""
Local App Service - Handles starting AI-generated apps locally on user's desktop
"""
import os
import subprocess
import asyncio
import signal
import socket
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from typing import Dict, Any, Optional, List, Tuple
from log_config import logger, error, info


class LocalAppService:
    def __init__(self):
        self.backend_process = None
        self.frontend_process = None
        self.backend_port = 8100
        self.frontend_port = 3100
        self.last_startup_diagnostics: Dict[str, Any] = {}
        # Reserve Sprint2Code's own default ports so generated apps never collide.
        self._reserved_ports = {8000, 3000}
        # Startup logs captured during service boot (stdout is drained by then, so preserve here)
        self._backend_startup_logs: List[str] = []
        self._frontend_startup_logs: List[str] = []
    
    def _resolve_service_dirs(self, repo_dir: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Resolve backend/frontend directories for both monorepo and single-repo layouts.

        Supported layouts:
        - Monorepo: <repo>/backend and/or <repo>/frontend
        - Backend-only repo: backend files at <repo> root (e.g., main.py)
        - Frontend-only repo: frontend files at <repo> root (e.g., package.json)
        """
        backend_dir = None
        frontend_dir = None

        monorepo_backend = os.path.join(repo_dir, 'backend')
        monorepo_frontend = os.path.join(repo_dir, 'frontend')

        if os.path.isdir(monorepo_backend):
            backend_dir = monorepo_backend
        if os.path.isdir(monorepo_frontend):
            frontend_dir = monorepo_frontend

        # Fallback: backend-only repository rooted at repo_dir
        if backend_dir is None:
            if any(os.path.exists(os.path.join(repo_dir, marker)) for marker in ('main.py', 'requirements.txt')):
                backend_dir = repo_dir

        # Fallback: frontend-only repository rooted at repo_dir
        if frontend_dir is None:
            if os.path.exists(os.path.join(repo_dir, 'package.json')):
                frontend_dir = repo_dir

        return backend_dir, frontend_dir

    def _can_bind_port(self, host: str, port: int) -> bool:
        """Check whether host:port can be bound right now."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return True
            except OSError:
                return False

    def _find_available_port(self, preferred_port: int, reserved_ports: Optional[set] = None) -> int:
        """
        Return an available port for servers that bind on 0.0.0.0.
        Avoid reserved ports (e.g., Sprint2Code's own 8000/3000).
        """
        reserved = reserved_ports or set()
        host = "0.0.0.0"

        if preferred_port not in reserved and self._can_bind_port(host, preferred_port):
            return preferred_port

        # Scan forward deterministically first to keep ports stable across retries.
        for candidate in range(preferred_port + 1, preferred_port + 200):
            if candidate in reserved:
                continue
            if self._can_bind_port(host, candidate):
                return candidate

        # Fall back to OS-assigned ephemeral port; retry if it lands in reserved set.
        while True:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, 0))
                candidate = int(sock.getsockname()[1])
            if candidate not in reserved:
                return candidate

    async def _wait_for_http_ready(self, urls: List[str], timeout_seconds: int = 20) -> bool:
        """Wait until any probe URL returns an HTTP response (2xx-5xx means process is serving)."""
        async def _probe_once(url: str) -> bool:
            def _sync_probe() -> bool:
                try:
                    with urlopen(url, timeout=1.5) as resp:
                        return 200 <= getattr(resp, "status", 200) < 600
                except HTTPError as e:
                    return 200 <= e.code < 600
                except URLError:
                    return False
                except Exception:
                    return False
            return await asyncio.to_thread(_sync_probe)

        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            for url in urls:
                if await _probe_once(url):
                    return True
            await asyncio.sleep(0.5)
        return False
    
    async def start_app_locally(self, 
                                 repo_dir: str,
                                 job_id: str) -> Tuple[bool, str, Dict[str, str]]:
        """
        Start the AI-generated app locally (backend and frontend).
        
        Args:
            repo_dir: Path to the cloned repository
            job_id: Unique job ID for tracking
            
        Returns:
            Tuple of (success, message, urls_dict)
        """
        info(f"Starting app locally from {repo_dir}")
        self.last_startup_diagnostics = {}
        self._backend_startup_logs = []
        self._frontend_startup_logs = []
        
        try:
            backend_dir, frontend_dir = self._resolve_service_dirs(repo_dir)
            if not backend_dir and not frontend_dir:
                return False, "No backend or frontend project directory found", {}

            # Allocate ports dynamically to avoid conflicts with existing local services.
            self.backend_port = self._find_available_port(8100, reserved_ports=self._reserved_ports)
            frontend_reserved = set(self._reserved_ports)
            frontend_reserved.add(self.backend_port)
            self.frontend_port = self._find_available_port(3100, reserved_ports=frontend_reserved)
            info(f"Selected local ports: backend={self.backend_port}, frontend={self.frontend_port}")

            urls: Dict[str, str] = {}

            # Start backend when present
            if backend_dir:
                backend_started, backend_msg = await self._start_backend(backend_dir, job_id)
                if not backend_started:
                    self.last_startup_diagnostics['phase'] = 'backend_startup'
                    self.last_startup_diagnostics['backend_error'] = backend_msg
                    return False, f"Backend failed to start: {backend_msg}", {}
                urls['backend_url'] = f"http://localhost:{self.backend_port}"

            # Start frontend when present
            if frontend_dir:
                frontend_started, frontend_msg = await self._start_frontend(frontend_dir, job_id)
                if not frontend_started:
                    # Stop backend if frontend fails in mixed deployments.
                    await self._stop_backend(job_id)
                    self.last_startup_diagnostics['phase'] = 'frontend_startup'
                    self.last_startup_diagnostics['frontend_error'] = frontend_msg
                    return False, f"Frontend failed to start: {frontend_msg}", {}
                urls['frontend_url'] = f"http://localhost:{self.frontend_port}"

            return True, "App started successfully", urls
        
        except Exception as e:
            error_msg = f"Error starting app locally: {str(e)}"
            error(error_msg)
            # Clean up any processes
            await self._stop_all(job_id)
            self.last_startup_diagnostics['phase'] = 'startup_exception'
            self.last_startup_diagnostics['exception'] = error_msg
            return False, error_msg, {}
    
    async def _start_backend(self, backend_dir: str, job_id: str) -> Tuple[bool, str]:
        """Start the backend FastAPI server locally in an isolated virtual environment."""
        # Check if backend directory exists
        if not os.path.exists(backend_dir):
            return False, "Backend directory not found"
        
        # CRITICAL: Create virtual environment for isolation
        venv_dir = os.path.join(backend_dir, '.venv')
        info(f"Creating isolated virtual environment at {venv_dir}")
        
        try:
            venv_result = subprocess.run(
                ['python3', '-m', 'venv', '.venv'],
                cwd=backend_dir,
                capture_output=True,
                text=True,
                timeout=60
            )
            if venv_result.returncode != 0:
                return False, f"Failed to create venv: {venv_result.stderr[:500]}"
        except Exception as e:
            return False, f"Failed to create venv: {str(e)}"
        
        # Determine venv paths
        if os.name == 'nt':  # Windows
            venv_python = os.path.join(venv_dir, 'Scripts', 'python.exe')
            venv_pip = os.path.join(venv_dir, 'Scripts', 'pip.exe')
            venv_uvicorn = os.path.join(venv_dir, 'Scripts', 'uvicorn.exe')
        else:  # Unix/Mac
            venv_python = os.path.join(venv_dir, 'bin', 'python')
            venv_pip = os.path.join(venv_dir, 'bin', 'pip')
            venv_uvicorn = os.path.join(venv_dir, 'bin', 'uvicorn')
        
        # Check if requirements.txt exists and install dependencies IN VENV
        requirements_path = os.path.join(backend_dir, 'requirements.txt')
        if os.path.exists(requirements_path):
            info(f"Installing backend dependencies in isolated venv")
            install_result = subprocess.run(
                [venv_pip, 'install', '-r', 'requirements.txt'],
                cwd=backend_dir,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout for pip install
            )
            if install_result.returncode != 0:
                error_msg = f"Failed to install dependencies: {install_result.stderr[:1000]}"
                error(f"pip install failed: {install_result.stderr}")
                error(f"pip stdout: {install_result.stdout}")
                return False, error_msg
            
            # Install uvicorn in venv if not already present
            info(f"Ensuring uvicorn is installed in venv")
            subprocess.run(
                [venv_pip, 'install', 'uvicorn[standard]'],
                cwd=backend_dir,
                capture_output=True,
                text=True,
                timeout=60
            )
        
        # Start uvicorn server using venv's uvicorn
        info(f"Starting backend on port {self.backend_port} (using venv)")
        try:
            # Start process in background using venv's Python
            self.backend_process = await asyncio.create_subprocess_exec(
                venv_python, '-m', 'uvicorn', 'main:app',
                '--host', '0.0.0.0',
                '--port', str(self.backend_port),
                cwd=backend_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy()
            )
            
            # Wait a few seconds and check if it's still running
            await asyncio.sleep(3)
            
            if self.backend_process.returncode is not None:
                # Process exited
                stdout = await self.backend_process.stdout.read()
                stderr = await self.backend_process.stderr.read()
                crash_output = stderr.decode(errors='replace') or stdout.decode(errors='replace')
                return False, f"Backend crashed immediately: {crash_output[:1200]}"
            
            # Try to read some startup logs
            startup_logs = []
            try:
                for _ in range(10):  # Try to read 10 lines
                    line = await asyncio.wait_for(
                        self.backend_process.stdout.readline(),
                        timeout=0.5
                    )
                    if line:
                        startup_logs.append(line.decode().strip())
                    err_line = await asyncio.wait_for(
                        self.backend_process.stderr.readline(),
                        timeout=0.2
                    )
                    if err_line:
                        startup_logs.append(err_line.decode().strip())
            except asyncio.TimeoutError:
                pass  # No more output
            
            # Check for obvious startup errors in startup stream.
            for log_line in startup_logs:
                if 'error' in log_line.lower() or 'traceback' in log_line.lower():
                    return False, f"Backend startup error: {log_line}"

            # Process can stay alive while app is not importable in some launcher modes.
            # Require an actual HTTP response before reporting success.
            backend_ready = await self._wait_for_http_ready(
                [
                    f"http://127.0.0.1:{self.backend_port}/health",
                    f"http://127.0.0.1:{self.backend_port}/",
                ],
                timeout_seconds=25,
            )
            if not backend_ready:
                try:
                    stderr_tail = await asyncio.wait_for(self.backend_process.stderr.read(), timeout=1.0)
                except Exception:
                    stderr_tail = b""
                tail_msg = stderr_tail.decode(errors='replace').strip()
                if tail_msg:
                    return False, f"Backend did not become HTTP-ready. stderr: {tail_msg[:1200]}"
                return False, "Backend did not become HTTP-ready in time"
            
            info(f"Backend started successfully on port {self.backend_port}")
            info(f"Backend startup logs: {startup_logs}")
            self._backend_startup_logs = startup_logs
            return True, "Backend started"
        
        except Exception as e:
            return False, f"Failed to start backend: {str(e)}"
    
    async def _start_frontend(self, frontend_dir: str, job_id: str) -> Tuple[bool, str]:
        """Start the frontend Next.js server locally."""
        # Check if frontend directory exists
        if not os.path.exists(frontend_dir):
            return False, "Frontend directory not found"
        
        # Check if package.json exists and install dependencies
        package_json_path = os.path.join(frontend_dir, 'package.json')
        if not os.path.exists(package_json_path):
            return False, "package.json not found in frontend directory"
        
        info(f"Installing frontend dependencies from package.json")
        
        # Use npm ci if package-lock.json exists, otherwise npm install
        npm_cmd = ['npm', 'install']
        
        install_result = subprocess.run(
            npm_cmd,
            cwd=frontend_dir,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        if install_result.returncode != 0:
            return False, f"Failed to install dependencies: {install_result.stderr[:500]}"
        
        # Set environment variable for backend URL
        env = os.environ.copy()
        env['NEXT_PUBLIC_BACKEND_URL'] = f"http://localhost:{self.backend_port}"
        env['PORT'] = str(self.frontend_port)
        
        # Start Next.js dev server
        info(f"Starting frontend on port {self.frontend_port}")
        try:
            # Start process in background
            self.frontend_process = await asyncio.create_subprocess_exec(
                'npm', 'run', 'dev',
                cwd=frontend_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            
            # Wait for frontend to be ready (Next.js takes longer to start)
            max_wait = 90  # Next.js cold start can be slow
            ready = False
            startup_logs = []
            
            for _ in range(max_wait):
                # Check if process is still running
                if self.frontend_process.returncode is not None:
                    stderr = await self.frontend_process.stderr.read()
                    return False, f"Frontend crashed: {stderr.decode()[:500]}"
                
                # Try to read output
                try:
                    line = await asyncio.wait_for(
                        self.frontend_process.stdout.readline(),
                        timeout=1.0
                    )
                    if line:
                        log_line = line.decode().strip()
                        startup_logs.append(log_line)
                        info(f"Frontend: {log_line}")
                        
                        # Check for ready signal
                        line_lower = log_line.lower()
                        if (
                            'ready started server on' in line_lower
                            or 'compiled successfully' in line_lower
                            or 'ready in ' in line_lower
                            or 'local:' in line_lower
                        ):
                            ready = True
                            # DRAIN A BIT MORE: Capture trailing logs (like ports) that often follow "Ready"
                            info("Found ready signal, draining final startup logs...")
                            for _ in range(5):
                                try:
                                    extra_line = await asyncio.wait_for(
                                        self.frontend_process.stdout.readline(),
                                        timeout=1.0
                                    )
                                    if extra_line:
                                        log_line = extra_line.decode().strip()
                                        startup_logs.append(log_line)
                                        info(f"Frontend (drain): {log_line}")
                                except asyncio.TimeoutError:
                                    break
                            break
                        
                        # Check for errors
                        if 'error' in log_line.lower() and 'warn' not in log_line.lower():
                            return False, f"Frontend error: {log_line}"
                except asyncio.TimeoutError:
                    pass  # No output yet, keep waiting

                try:
                    err_line = await asyncio.wait_for(
                        self.frontend_process.stderr.readline(),
                        timeout=0.2
                    )
                    if err_line:
                        log_line = err_line.decode().strip()
                        startup_logs.append(log_line)
                        info(f"Frontend(stderr): {log_line}")

                        line_lower = log_line.lower()
                        if (
                            'ready started server on' in line_lower
                            or 'compiled successfully' in line_lower
                            or 'ready in ' in line_lower
                            or 'local:' in line_lower
                        ):
                            ready = True
                            break
                        if 'error' in line_lower and 'warn' not in line_lower:
                            return False, f"Frontend error: {log_line}"
                except asyncio.TimeoutError:
                    pass
                
                await asyncio.sleep(1)
            
            if ready:
                info(f"Frontend started successfully on port {self.frontend_port}")
                info(f"Frontend startup logs: {startup_logs[-5:]}")  # Last 5 lines
                self._frontend_startup_logs = startup_logs
                return True, "Frontend started"
            else:
                try:
                    err_tail = await asyncio.wait_for(self.frontend_process.stderr.read(), timeout=1.0)
                except Exception:
                    err_tail = b""
                stderr_msg = err_tail.decode(errors='replace').strip()
                if stderr_msg:
                    return False, f"Frontend did not become ready within {max_wait}s. stderr: {stderr_msg[:1200]}"
                return False, f"Frontend did not become ready within {max_wait}s"
        
        except Exception as e:
            return False, f"Failed to start frontend: {str(e)}"
    
    async def _stop_backend(self, job_id: str):
        """Stop the backend process."""
        if self.backend_process and self.backend_process.returncode is None:
            info(f"Stopping backend process for job {job_id}")
            try:
                self.backend_process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self.backend_process.wait(), timeout=10)
            except asyncio.TimeoutError:
                warning("Backend did not stop gracefully, killing...")
                self.backend_process.kill()
            except Exception as e:
                error(f"Error stopping backend: {e}")
    
    async def _stop_frontend(self, job_id: str):
        """Stop the frontend process."""
        if self.frontend_process and self.frontend_process.returncode is None:
            info(f"Stopping frontend process for job {job_id}")
            try:
                self.frontend_process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self.frontend_process.wait(), timeout=10)
            except asyncio.TimeoutError:
                warning("Frontend did not stop gracefully, killing...")
                self.frontend_process.kill()
            except Exception as e:
                error(f"Error stopping frontend: {e}")
    
    async def _stop_all(self, job_id: str):
        """Stop both backend and frontend processes."""
        await self._stop_backend(job_id)
        await self._stop_frontend(job_id)
    
    async def get_app_logs(self, service_type: str, limit: int = 100) -> List[str]:
        """
        Get logs from running app processes.
        Falls back to startup logs captured during boot when the live buffer is empty
        (frontend stdout is fully drained during the ready-signal wait loop).
        """
        process = self.backend_process if service_type == 'backend' else self.frontend_process

        if not process:
            return [f"{service_type.title()} process not found"]

        logs = []
        try:
            # Read available output from stdout and stderr
            for stream_name in ['stdout', 'stderr']:
                stream = getattr(process, stream_name)
                if not stream:
                    continue

                while len(logs) < limit:
                    try:
                        # Use a very short timeout to drain existing buffer
                        line = await asyncio.wait_for(stream.readline(), timeout=0.01)
                        if line:
                            logs.append(f"[{stream_name.upper()}] {line.decode().strip()}")
                        else:
                            break
                    except asyncio.TimeoutError:
                        break
        except Exception as e:
            logs.append(f"Error reading logs: {str(e)}")

        # Fall back to startup logs if the live buffer was already drained during startup
        if not logs:
            startup_logs = (
                self._backend_startup_logs if service_type == 'backend'
                else self._frontend_startup_logs
            )
            logs = [f"[STARTUP] {line}" for line in startup_logs if line]

        return logs[-limit:]

    def get_startup_diagnostics(self) -> Dict[str, Any]:
        """Return last startup diagnostics captured by this service."""
        return dict(self.last_startup_diagnostics)
    
    async def check_health(self, job_id: str) -> Tuple[bool, bool]:
        """
        Check if backend and frontend are still running.
        
        Args:
            job_id: Job identifier
            
        Returns:
            Tuple of (backend_healthy, frontend_healthy)
        """
        backend_healthy = (
            self.backend_process is not None and
            self.backend_process.returncode is None and
            await self._wait_for_http_ready(
                [
                    f"http://127.0.0.1:{self.backend_port}/health",
                    f"http://127.0.0.1:{self.backend_port}/",
                ],
                timeout_seconds=2,
            )
        )

        frontend_healthy = (
            self.frontend_process is not None and
            self.frontend_process.returncode is None and
            await self._wait_for_http_ready(
                [f"http://127.0.0.1:{self.frontend_port}/"],
                timeout_seconds=2,
            )
        )
        
        return backend_healthy, frontend_healthy
