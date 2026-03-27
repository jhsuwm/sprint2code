"""
Local database setup script for Mac development.
This script automates the local PostgreSQL setup process.
"""

import os
import sys
import subprocess
import getpass
from pathlib import Path

def run_command(command, check=True, capture_output=False):
    """Run a shell command and return the result."""
    try:
        if capture_output:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, check=check)
            return result.stdout.strip()
        else:
            result = subprocess.run(command, shell=True, check=check)
            return result.returncode == 0
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {command}")
        print(f"Error: {e}")
        return False

def check_homebrew():
    """Check if Homebrew is installed."""
    return run_command("which brew", capture_output=True) != ""

def install_homebrew():
    """Install Homebrew."""
    print("Installing Homebrew...")
    install_cmd = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    return run_command(install_cmd)

def check_postgresql():
    """Check if PostgreSQL is installed."""
    return run_command("which psql", capture_output=True) != ""

def install_postgresql():
    """Install PostgreSQL using Homebrew."""
    print("Installing PostgreSQL...")
    if not run_command("brew install postgresql@15"):
        return False
    
    print("Starting PostgreSQL service...")
    if not run_command("brew services start postgresql@15"):
        return False
    
    # Add to PATH
    shell_config = os.path.expanduser("~/.zshrc")
    path_export = 'export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"'
    
    try:
        with open(shell_config, "r") as f:
            content = f.read()
        
        if path_export not in content:
            with open(shell_config, "a") as f:
                f.write(f"\n# PostgreSQL PATH\n{path_export}\n")
            print(f"Added PostgreSQL to PATH in {shell_config}")
    except Exception as e:
        print(f"Warning: Could not update PATH in {shell_config}: {e}")
    
    return True

def setup_database():
    """Set up the vacation planner database and user."""
    print("Setting up database and user...")
    
    # Wait a moment for PostgreSQL to fully start
    import time
    time.sleep(2)
    
    commands = [
        "CREATE DATABASE rooster;",
        "CREATE USER vacation_user WITH PASSWORD 'vacation_password';",
        "GRANT ALL PRIVILEGES ON DATABASE rooster TO vacation_user;",
        "ALTER USER vacation_user CREATEDB;"
    ]
    
    for cmd in commands:
        full_cmd = f'psql postgres -c "{cmd}"'
        if not run_command(full_cmd):
            print(f"Warning: Command may have failed: {cmd}")
            # Continue anyway, as some commands might fail if already executed
    
    return True

def create_env_file():
    """Create .env file with database configuration."""
    api_dir = Path(__file__).parent.parent
    env_file = api_dir / ".env"
    
    env_content = """# Database configuration for local development
DATABASE_URL=postgresql://vacation_user:vacation_password@localhost:5432/rooster
SQL_DEBUG=true

# JWT configuration (you'll need to generate this later)
# JWT_SECRET_KEY=your_jwt_secret_here
# JWT_ALGORITHM=HS256
# JWT_EXPIRE_MINUTES=30
"""
    
    try:
        with open(env_file, "w") as f:
            f.write(env_content)
        print(f"Created .env file at {env_file}")
        return True
    except Exception as e:
        print(f"Error creating .env file: {e}")
        return False

def install_python_dependencies():
    """Install Python dependencies."""
    print("Installing Python dependencies...")
    api_dir = Path(__file__).parent.parent
    requirements_file = api_dir / "requirements.txt"
    
    if requirements_file.exists():
        return run_command(f"pip install -r {requirements_file}")
    else:
        print(f"Requirements file not found: {requirements_file}")
        return False

def test_database_connection():
    """Test the database connection."""
    print("Testing database connection...")
    
    try:
        # Add the parent directory to Python path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        
        from dotenv import load_dotenv
        load_dotenv()
        
        from database.config import check_database_connection
        
        if check_database_connection():
            print("✅ Database connection successful!")
            return True
        else:
            print("❌ Database connection failed!")
            return False
    except Exception as e:
        print(f"❌ Error testing database connection: {e}")
        return False

def initialize_database():
    """Initialize the database schema."""
    print("Initializing database schema...")
    
    init_script = Path(__file__).parent / "init_db.py"
    return run_command(f"python {init_script} --action init")

def main():
    """Main setup function."""
    print("🐘 PostgreSQL Local Setup for Vacation Planner")
    print("=" * 50)
    
    # Check if running on macOS
    if sys.platform != "darwin":
        print("This script is designed for macOS. Please follow manual installation instructions.")
        sys.exit(1)
    
    # Step 1: Check/Install Homebrew
    if not check_homebrew():
        print("Homebrew not found. Installing...")
        if not install_homebrew():
            print("❌ Failed to install Homebrew")
            sys.exit(1)
        print("✅ Homebrew installed successfully")
    else:
        print("✅ Homebrew already installed")
    
    # Step 2: Check/Install PostgreSQL
    if not check_postgresql():
        print("PostgreSQL not found. Installing...")
        if not install_postgresql():
            print("❌ Failed to install PostgreSQL")
            sys.exit(1)
        print("✅ PostgreSQL installed successfully")
    else:
        print("✅ PostgreSQL already installed")
        # Make sure it's running
        run_command("brew services start postgresql@15")
    
    # Step 3: Setup database
    if not setup_database():
        print("❌ Failed to setup database")
        sys.exit(1)
    print("✅ Database and user created successfully")
    
    # Step 4: Create .env file
    if not create_env_file():
        print("❌ Failed to create .env file")
        sys.exit(1)
    print("✅ Environment file created successfully")
    
    # Step 5: Install Python dependencies
    if not install_python_dependencies():
        print("❌ Failed to install Python dependencies")
        print("Please run: pip install -r requirements.txt")
    else:
        print("✅ Python dependencies installed successfully")
    
    # Step 6: Test connection
    if not test_database_connection():
        print("❌ Database connection test failed")
        print("Please check your PostgreSQL installation and try again")
        sys.exit(1)
    
    # Step 7: Initialize database
    if not initialize_database():
        print("❌ Failed to initialize database schema")
        sys.exit(1)
    print("✅ Database schema initialized successfully")
    
    print("\n🎉 Setup completed successfully!")
    print("\nNext steps:")
    print("1. Restart your terminal or run: source ~/.zshrc")
    print("2. Test the setup: python database/utils.py --action health")
    print("3. Generate sample data: python database/utils.py --action sample")
    print("4. Start building your FastAPI application!")
    
    print(f"\nDatabase URL: postgresql://vacation_user:vacation_password@localhost:5432/rooster")
    print("Environment file created at: api/.env")

if __name__ == "__main__":
    main()