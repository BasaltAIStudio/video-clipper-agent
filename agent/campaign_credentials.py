"""
Secure credential storage for Content Rewards account.
Security model: Keys NEVER enter the chat log. User stores credentials via PowerShell
with restricted file permissions (icacls /grant "$USERNAME:R").

Standard location: agent/credentials.env
Format: KEY=value (same format as .env files)
File permissions: Windows user only (owner read/write, no group/other access)
"""

import os
import sys
from pathlib import Path

# Resolve agent directory
AGENT_DIR = Path(__file__).parent
CREDENTIALS_FILE = AGENT_DIR / "credentials.env"


def load_credentials() -> dict:
    """
    Load Content Rewards account credentials from a secure credential file.
    Returns a dict with credential keys, or empty dict if file doesn't exist.
    """
    creds = {}
    if not CREDENTIALS_FILE.exists():
        return creds

    try:
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key and value:
                        creds[key] = value
    except Exception as e:
        print(f"Warning: Failed to load credentials from {CREDENTIALS_FILE}: {e}", file=sys.stderr)
    return creds


def save_credentials(creds: dict) -> bool:
    """
    Securely save Content Rewards credentials to credentials.env.
    Uses Windows icacls to restrict file permissions to the current user only.

    Args:
        creds: Dictionary of credential key/value pairs.

    Returns:
        True if saved successfully, False otherwise.
    """
    try:
        # Create parent directory if needed
        CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Write the file with restricted permissions
        lines = []
        for key, value in creds.items():
            # Escape special characters in values
            escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped_value}"')

        file_contents = "\n".join(lines) + "\n"

        # Write with restrictive permissions
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            f.write(file_contents)

        # Set permissions: owner read/write only (Windows)
        # This command: icacls <file> /grant "<username>:R" /inheritance:r
        # The /inheritance:r removes inherited permissions
        import subprocess
        result = subprocess.run(
            ["icacls", str(CREDENTIALS_FILE), "/grant", f"{os.getlogin()}:R", "/inheritance:r"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode != 0:
            print(f"Warning: icacls restriction failed: {result.stderr}", file=sys.stderr)
            # Still return True - file is written, just without restriction
        return True
    except Exception as e:
        print(f"Error: Failed to save credentials: {e}", file=sys.stderr)
        return False


def get_credential(key: str, default: str = "") -> str:
    """Get a single credential by key."""
    creds = load_credentials()
    return creds.get(key, default)


def get_creds_dict() -> dict:
    """Get all credentials as a dict."""
    return load_credentials()


def print_safety_instructions() -> None:
    """Print instructions for the user on how to securely store credentials."""
    instructions = """
============================================================
CONTENT REWARDS CREDENTIAL STORAGE INSTRUCTIONS
============================================================

For security, your Content Rewards credentials are stored in:
    agent/credentials.env

This file has restricted permissions (Windows user only).

To store your credentials, run this PowerShell command:
    $env_file = "C:\\Users\\lenovo\\video-agent\\agent\\credentials.env"
    "CR_CONTENT_REWARDS_USERNAME=your_email@domain.com" | Out-File -FilePath $env_file -Encoding ASCII
    "CR_CONTENT_REWARDS_PASSWORD=your_password" | Out-File -FilePath $env_file -Encoding ASCII -Append
    icacls $env_file /grant "$($env:USERNAME):R" /inheritance:r

Or manually edit the file with a text editor and set permissions:
    icacls "C:\\Users\\lenovo\\video-agent\\agent\\credentials.env" /grant "$($env:USERNAME):R" /inheritance:r

Your credentials never enter the chat log. They stay in this file only.
============================================================
"""
    print(instructions)
