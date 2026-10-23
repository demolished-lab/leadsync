"""
Lightweight authentication for the Sales Follow-Up Agent.

Provides:
  - Password hashing (SHA-256 + salt, no external deps)
  - User management from YAML config
  - Session token generation/verification
  - Role-based access control (admin, rep, viewer)
  - API key authentication for programmatic access

User config file (auth_config.yaml):
    users:
      john:
        name: John Doe
        email: john@company.com
        password: <sha256-hashed>
        role: rep
        team: enterprise
      admin:
        name: Admin User
        email: admin@company.com
        password: <sha256-hashed>
        role: admin

Run `python -m core.auth create-user` to add users interactively.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .totp import TOTPManager, BackupCodes, get_totp_manager, get_backup_codes

logger = logging.getLogger("Auth")

# ── Password Hashing ───────────────────────────────────────────

_SALT_PREFIX = "sfa:"


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """
    Hash a password with bcrypt (preferred); falls back to SHA-256+salt only
    when bcrypt is unavailable (a loud warning is logged — install passlib).
    Returns bcrypt hash or "salt:sha256" for legacy.
    """
    try:
        import bcrypt
        # bcrypt handles its own salt; ignore provided salt for bcrypt path
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    except Exception as e:
        logger.warning(
            f"bcrypt unavailable ({e}) — falling back to SHA-256+salt password "
            "hashing, which is NOT recommended in production. "
            "Install with: pip install \"passlib[bcrypt]\""
        )
        if salt is None:
            salt = secrets.token_hex(16)
        hashed = hashlib.sha256(f"{_SALT_PREFIX}{salt}:{password}".encode()).hexdigest()
        return f"{salt}:{hashed}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify against bcrypt or legacy SHA-256."""
    if not stored_hash:
        return False
    # bcrypt hashes start with $2b$ / $2a$
    if stored_hash.startswith("$2"):
        try:
            import bcrypt
            return bcrypt.checkpw(password.encode(), stored_hash.encode())
        except Exception:
            return False
    try:
        salt, expected_hash = stored_hash.split(":", 1)
        actual_hash = hashlib.sha256(f"{_SALT_PREFIX}{salt}:{password}".encode()).hexdigest()
        return hmac.compare_digest(actual_hash, expected_hash)
    except (ValueError, AttributeError):
        return False


# ── User Config ────────────────────────────────────────────────

_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "auth_config.yaml",
)


def _load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load auth config from YAML file."""
    path = config_path or os.environ.get("AUTH_CONFIG_PATH", _DEFAULT_CONFIG_PATH)

    if not os.path.exists(path):
        default_password = os.environ.get("ADMIN_PASSWORD")
        if not default_password:
            # Generate a random password; do NOT default to admin123 in prod.
            default_password = secrets.token_urlsafe(16)
            logger.warning(
                f"No ADMIN_PASSWORD set — generated a random admin password for {path}. "
                "Set ADMIN_PASSWORD env var for deterministic bootstrap."
            )
        default_config = {
            "users": {
                "admin": {
                    "name": "Administrator",
                    "email": "admin@company.com",
                    "password": hash_password(default_password),
                    "role": "admin",
                    "team": "management",
                },
            },
            "session_ttl_hours": 24,
            "max_login_attempts": 5,
            "lockout_minutes": 15,
        }
        save_config(default_config, path)
        if os.environ.get("ADMIN_PASSWORD"):
            logger.warning(f"Created default auth config at {path} — change the admin password!")
        else:
            # One-time bootstrap password goes to a protected file next to the
            # config, NOT into (potentially aggregated/shipped) server logs.
            secret_file = path.rsplit(".", 1)[0] + "_bootstrap_password.txt"
            try:
                with open(secret_file, "w", encoding="utf-8") as sf:
                    sf.write(default_password + "\n")
                try:
                    os.chmod(secret_file, 0o600)
                except Exception:
                    pass
            except Exception:
                secret_file = None
            if secret_file:
                logger.warning(
                    f"Admin bootstrap password (one-time) written to {secret_file} "
                    "(mode 0600) — delete it after first login."
                )
            else:
                logger.warning("Admin bootstrap password generated but could not be written to disk")

    try:
        import yaml
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # Fallback to JSON if yaml not installed
        json_path = path.replace(".yaml", ".json").replace(".yml", ".json")
        if os.path.exists(json_path):
            with open(json_path, "r") as f:
                return json.load(f)
        # Last resort: read as JSON from the yaml path
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}


def save_config(config: Dict[str, Any], config_path: Optional[str] = None) -> None:
    """Save auth config to YAML file."""
    path = config_path or os.environ.get("AUTH_CONFIG_PATH", _DEFAULT_CONFIG_PATH)
    try:
        import yaml
        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    except ImportError:
        # Fallback to JSON
        json_path = path.replace(".yaml", ".json").replace(".yml", ".json")
        with open(json_path, "w") as f:
            json.dump(config, f, indent=2)


def get_user(username: str, config_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get a user by username."""
    config = _load_config(config_path)
    return config.get("users", {}).get(username)


def list_users(config_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all users (without passwords or secrets)."""
    config = _load_config(config_path)
    users = []
    for username, data in config.get("users", {}).items():
        users.append({
            "username": username,
            "name": data.get("name", username),
            "email": data.get("email", ""),
            "role": data.get("role", "rep"),
            "team": data.get("team", ""),
            "totp_enabled": data.get("totp_enabled", False),
            "backup_codes_remaining": len(data.get("totp_backup_codes", [])),
        })
    return users


def create_user(
    username: str,
    password: str,
    name: str = "",
    email: str = "",
    role: str = "rep",
    team: str = "",
    config_path: Optional[str] = None,
) -> bool:
    """Create a new user."""
    config = _load_config(config_path)
    if "users" not in config:
        config["users"] = {}

    if username in config["users"]:
        logger.warning(f"User '{username}' already exists")
        return False

    config["users"][username] = {
        "name": name or username,
        "email": email,
        "password": hash_password(password),
        "role": role,
        "team": team,
    }
    save_config(config, config_path)
    logger.info(f"User '{username}' created with role '{role}'")
    return True


def update_password(username: str, new_password: str, config_path: Optional[str] = None) -> bool:
    """Update a user's password."""
    config = _load_config(config_path)
    if username not in config.get("users", {}):
        return False

    config["users"][username]["password"] = hash_password(new_password)
    save_config(config, config_path)
    return True


def delete_user(username: str, config_path: Optional[str] = None) -> bool:
    """Delete a user."""
    config = _load_config(config_path)
    if username not in config.get("users", {}):
        return False

    del config["users"][username]
    save_config(config, config_path)
    return True


# ── 2FA Management ────────────────────────────────────────────

def enable_2fa(username: str, config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Enable 2FA for a user. Generates a TOTP secret and backup codes.

    Returns:
        Dict with secret, uri, and backup_codes.
        The secret must be verified with verify_2fa_setup() before 2FA is active.
    """
    config = _load_config(config_path)
    user = config.get("users", {}).get(username)
    if user is None:
        return {"error": "User not found"}

    if user.get("totp_secret"):
        return {"error": "2FA already enabled. Disable first."}

    totp = get_totp_manager()
    bc = get_backup_codes()

    secret = totp.generate_secret()
    backup = bc.generate()
    hashed_backup = bc.hash_codes(backup)

    # Store secret and hashed backup codes (not yet active)
    user["totp_secret"] = secret
    user["totp_backup_codes"] = hashed_backup
    user["totp_enabled"] = False  # Active only after verification
    save_config(config, config_path)

    uri = totp.get_uri(secret, user.get("email", username), "SalesFollowUpAgent")

    logger.info(f"2FA setup initiated for '{username}' — awaiting verification")

    return {
        "secret": secret,
        "uri": uri,
        "backup_codes": backup,
        "message": "Scan the QR code or enter the secret in your authenticator app. "
                    "Then verify with a code to activate 2FA.",
    }


def verify_2fa_setup(username: str, code: str, config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Verify the initial 2FA code to activate 2FA.

    Must be called after enable_2fa() to complete setup.
    """
    config = _load_config(config_path)
    user = config.get("users", {}).get(username)
    if user is None:
        return {"error": "User not found"}

    secret = user.get("totp_secret")
    if not secret:
        return {"error": "2FA not initiated. Call enable_2fa() first."}

    if user.get("totp_enabled"):
        return {"error": "2FA already active."}

    totp = get_totp_manager()
    if totp.verify(secret, code, window=2):
        user["totp_enabled"] = True
        save_config(config, config_path)
        logger.info(f"2FA activated for '{username}'")
        return {"success": True, "message": "2FA activated successfully."}
    else:
        return {"success": False, "message": "Invalid code. Try again."}


def verify_2fa(username: str, code: str, config_path: Optional[str] = None) -> bool:
    """
    Verify a 2FA code during login.

    Returns True if valid.
    """
    user = get_user(username, config_path)
    if user is None:
        return False

    if not user.get("totp_enabled"):
        return True  # 2FA not enabled — auto-pass

    secret = user.get("totp_secret")
    if not secret:
        return False

    totp = get_totp_manager()
    return totp.verify(secret, code)


def use_backup_code(username: str, code: str, config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Use a backup code for 2FA verification.

    Returns:
        Dict with success status and remaining codes count.
    """
    config = _load_config(config_path)
    user = config.get("users", {}).get(username)
    if user is None:
        return {"error": "User not found"}

    backup_hashes = user.get("totp_backup_codes", [])
    if not backup_hashes:
        return {"error": "No backup codes available"}

    bc = get_backup_codes()
    is_valid, index = bc.verify_code(code, backup_hashes)

    if is_valid and index is not None:
        # Remove used code
        user["totp_backup_codes"] = bc.remove_used(backup_hashes, index)
        save_config(config, config_path)
        remaining = len(user["totp_backup_codes"])
        logger.info(f"Backup code used for '{username}' — {remaining} remaining")
        return {"success": True, "remaining": remaining}
    else:
        return {"success": False, "message": "Invalid backup code"}


def disable_2fa(username: str, password: str, config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Disable 2FA for a user. Requires password confirmation.
    """
    config = _load_config(config_path)
    user = config.get("users", {}).get(username)
    if user is None:
        return {"error": "User not found"}

    if not verify_password(password, user.get("password", "")):
        return {"error": "Invalid password"}

    if not user.get("totp_enabled"):
        return {"error": "2FA is not enabled"}

    user.pop("totp_secret", None)
    user.pop("totp_backup_codes", None)
    user["totp_enabled"] = False
    save_config(config, config_path)

    logger.info(f"2FA disabled for '{username}'")
    return {"success": True, "message": "2FA disabled."}


def get_2fa_status(username: str, config_path: Optional[str] = None) -> Dict[str, Any]:
    """Get 2FA status for a user."""
    user = get_user(username, config_path)
    if user is None:
        return {"error": "User not found"}

    backup_count = len(user.get("totp_backup_codes", []))
    return {
        "enabled": user.get("totp_enabled", False),
        "configured": bool(user.get("totp_secret")),
        "backup_codes_remaining": backup_count,
    }


# ── 2FA Enforcement ───────────────────────────────────────────

def is_2fa_required(username: str, config_path: Optional[str] = None) -> bool:
    """
    Check if 2FA is required for a user based on config and role.

    Returns True if:
    - enforce_2fa_admin is True AND user is admin, OR
    - enforce_2fa_rep is True AND user is rep or above
    """
    from ..config import get_settings
    settings = get_settings()

    user = get_user(username, config_path)
    if user is None:
        return False

    role = user.get("role", "rep")

    if role == "admin" and getattr(settings, "enforce_2fa_admin", False):
        return True
    if role in ("admin", "rep") and getattr(settings, "enforce_2fa_rep", False):
        return True

    return False


def check_2fa_compliance(username: str, config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Check if a user complies with 2FA enforcement policy.

    Returns:
        Dict with compliance status and required actions.
    """
    user = get_user(username, config_path)
    if user is None:
        return {"compliant": False, "error": "User not found"}

    required = is_2fa_required(username, config_path)
    if not required:
        return {
            "compliant": True,
            "required": False,
            "message": "2FA not required for this user",
        }

    status = get_2fa_status(username, config_path)
    enabled = status.get("enabled", False)

    if enabled:
        return {
            "compliant": True,
            "required": True,
            "message": "2FA is enabled",
            "backup_codes_remaining": status.get("backup_codes_remaining", 0),
        }
    else:
        return {
            "compliant": False,
            "required": True,
            "message": "2FA is required but not enabled. Please set up 2FA before continuing.",
            "action_required": "enable_2fa",
        }


def get_enforcement_status(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Get 2FA enforcement status for all users.

    Returns overview of compliance across the organization.
    """
    from ..config import get_settings
    settings = get_settings()

    users = list_users(config_path)
    compliance = []
    non_compliant = []

    for u in users:
        status = check_2fa_compliance(u["username"], config_path)
        entry = {
            "username": u["username"],
            "role": u["role"],
            **status,
        }
        compliance.append(entry)
        if not status.get("compliant") and status.get("required"):
            non_compliant.append(u["username"])

    return {
        "enforce_admin": getattr(settings, "enforce_2fa_admin", False),
        "enforce_rep": getattr(settings, "enforce_2fa_rep", False),
        "total_users": len(users),
        "compliant_users": len(users) - len(non_compliant),
        "non_compliant_users": non_compliant,
        "compliance_rate": round(
            (len(users) - len(non_compliant)) / max(1, len(users)) * 100, 1
        ),
        "details": compliance,
    }


def get_non_compliant_users(config_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get list of users who need to set up 2FA."""
    status = get_enforcement_status(config_path)
    return [
        d for d in status["details"]
        if not d.get("compliant") and d.get("required")
    ]


# ── Authentication ─────────────────────────────────────────────

class Authenticator:
    """
    Session-based authenticator with brute-force protection.

    Tracks failed login attempts and locks out after N failures.
    Issues session tokens with configurable TTL.
    """

    def __init__(self, config_path: Optional[str] = None):
        self._config_path = config_path
        self._sessions: Dict[str, Dict[str, Any]] = {}  # token → session data
        self._failed_attempts: Dict[str, List[float]] = {}  # username → [timestamps]
        self._config = None

    @property
    def config(self) -> Dict[str, Any]:
        if self._config is None:
            self._config = _load_config(self._config_path)
        return self._config

    def authenticate(self, username: str, password: str) -> Tuple[bool, str, Optional[Dict]]:
        """Authenticate a user (back-compatible 3-tuple form)."""
        ok, message, user, _token = self.authenticate_token(username, password)
        return ok, message, user

    def authenticate_token(
        self, username: str, password: str
    ) -> Tuple[bool, str, Optional[Dict], Optional[str]]:
        """
        Authenticate a user and return the freshly created session token.

        Returns:
            (success, message, user_data_or_None, session_token_or_None)
        """
        # Check lockout
        if self._is_locked_out(username):
            config = self.config
            lockout_min = config.get("lockout_minutes", 15)
            return False, f"Account locked. Try again in {lockout_min} minutes.", None, None

        user = get_user(username, self._config_path)
        if user is None:
            self._record_failed_attempt(username)
            return False, "Invalid username or password.", None, None

        if not verify_password(password, user["password"]):
            self._record_failed_attempt(username)
            remaining = self.config.get("max_login_attempts", 5) - len(self._failed_attempts.get(username, []))
            return False, f"Invalid username or password. {remaining} attempts remaining.", None, None

        # Success — clear failed attempts
        self._failed_attempts.pop(username, None)

        # Create session
        token = self._create_session(username, user)

        user_data = {
            "username": username,
            "name": user.get("name", username),
            "email": user.get("email", ""),
            "role": user.get("role", "rep"),
            "team": user.get("team", ""),
        }

        return True, "Login successful.", user_data, token

    def verify_session(self, token: str) -> Optional[Dict[str, Any]]:
        """Verify a session token. Returns user data or None if invalid/expired."""
        session = self._sessions.get(token)
        if session is None:
            return None

        # Check expiry
        ttl_hours = self.config.get("session_ttl_hours", 24)
        expires_at = session.get("expires_at", 0)
        if time.time() > expires_at:
            del self._sessions[token]
            return None

        return session.get("user")

    def logout(self, token: str) -> None:
        """Invalidate a session token."""
        self._sessions.pop(token, None)

    def get_session_count(self) -> int:
        """Get number of active sessions."""
        self._cleanup_expired()
        return len(self._sessions)

    def _create_session(self, username: str, user: Dict) -> str:
        """Create a new session token."""
        token = secrets.token_urlsafe(32)
        ttl_hours = self.config.get("session_ttl_hours", 24)

        self._sessions[token] = {
            "username": username,
            "user": {
                "username": username,
                "name": user.get("name", username),
                "email": user.get("email", ""),
                "role": user.get("role", "rep"),
                "team": user.get("team", ""),
            },
            "created_at": time.time(),
            "expires_at": time.time() + (ttl_hours * 3600),
        }
        return token

    def _record_failed_attempt(self, username: str) -> None:
        """Record a failed login attempt."""
        if username not in self._failed_attempts:
            self._failed_attempts[username] = []
        self._failed_attempts[username].append(time.time())

    def _is_locked_out(self, username: str) -> bool:
        """Check if a user is locked out due to too many failed attempts."""
        attempts = self._failed_attempts.get(username, [])
        if not attempts:
            return False

        max_attempts = self.config.get("max_login_attempts", 5)
        lockout_minutes = self.config.get("lockout_minutes", 15)

        # Clean old attempts outside lockout window
        cutoff = time.time() - (lockout_minutes * 60)
        self._failed_attempts[username] = [t for t in attempts if t > cutoff]

        return len(self._failed_attempts[username]) >= max_attempts

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = time.time()
        expired = [t for t, s in self._sessions.items() if now > s.get("expires_at", 0)]
        for t in expired:
            del self._sessions[t]


# ── API Key Authentication ─────────────────────────────────────

class APIKeyManager:
    """
    API key authentication for programmatic access.

    Keys are stored in the auth config under an 'api_keys' section.
    """

    def __init__(self, config_path: Optional[str] = None):
        self._config_path = config_path

    def create_key(self, name: str, role: str = "rep") -> str:
        """Create a new API key. Returns the key string."""
        config = _load_config(self._config_path)
        if "api_keys" not in config:
            config["api_keys"] = {}

        key = f"sfa_{secrets.token_urlsafe(32)}"
        # Use full key hash as index to avoid prefix collision; keep prefix for display only
        key_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        config["api_keys"][key_id] = {
            "prefix": key[:12] + "...",
            "name": name,
            "role": role,
            "created_at": datetime.utcnow().isoformat(),
            "key_hash": hash_password(key),
        }
        save_config(config, self._config_path)
        return key

    def verify_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        """Verify an API key. Returns key metadata or None."""
        config = _load_config(self._config_path)
        for key_data in config.get("api_keys", {}).values():
                if verify_password(api_key, key_data["key_hash"]):
                    return {
                        "name": key_data.get("name", "unknown"),
                        "role": key_data.get("role", "rep"),
                    }
        return None

    def revoke_key(self, api_key: str) -> bool:
        """Revoke an API key."""
        config = _load_config(self._config_path)
        api_keys = config.get("api_keys", {})
        for kid, key_data in list(api_keys.items()):
            if verify_password(api_key, key_data.get("key_hash", "")):
                del api_keys[kid]
                save_config(config, self._config_path)
                return True
            # legacy prefix match fallback
            if kid == (api_key[:12] + "..." if len(api_key) > 12 else api_key):
                del api_keys[kid]
                save_config(config, self._config_path)
                return True
        return False


# ── Role Checking ──────────────────────────────────────────────

def require_role(user: Optional[Dict], *allowed_roles: str) -> bool:
    """Check if a user has one of the allowed roles."""
    if user is None:
        return False
    return user.get("role", "") in allowed_roles


def is_admin(user: Optional[Dict]) -> bool:
    """Check if a user is an admin."""
    return require_role(user, "admin")


def is_rep_or_above(user: Optional[Dict]) -> bool:
    """Check if a user is a rep or admin."""
    return require_role(user, "admin", "rep")


# ── Global Instances ───────────────────────────────────────────
_authenticator: Optional[Authenticator] = None
_api_key_manager: Optional[APIKeyManager] = None


def get_authenticator() -> Authenticator:
    global _authenticator
    if _authenticator is None:
        _authenticator = Authenticator()
    return _authenticator


def get_api_key_manager() -> APIKeyManager:
    global _api_key_manager
    if _api_key_manager is None:
        _api_key_manager = APIKeyManager()
    return _api_key_manager


def _cli() -> None:
    """CLI: python -m core.auth create-user|create-key|list-users|list-keys"""
    import argparse
    parser = argparse.ArgumentParser(description="LeadSync authentication management")
    sub = parser.add_subparsers(dest="command", required=True)

    p_user = sub.add_parser("create-user", help="Create a user")
    p_user.add_argument("username")
    p_user.add_argument("password")
    p_user.add_argument("--name", default="")
    p_user.add_argument("--email", default="")
    p_user.add_argument("--role", default="rep", choices=["admin", "rep", "viewer"])
    p_user.add_argument("--team", default="")

    p_key = sub.add_parser("create-key", help="Create an API key (prints it once)")
    p_key.add_argument("name")
    p_key.add_argument("--role", default="rep", choices=["admin", "rep", "viewer"])

    sub.add_parser("list-users", help="List users (no secrets)")
    sub.add_parser("list-keys", help="List API keys (no secrets)")

    args = parser.parse_args()

    if args.command == "create-user":
        ok = create_user(args.username, args.password, args.name, args.email, args.role, args.team)
        sys.exit(0 if ok else 1)
    elif args.command == "create-key":
        key = get_api_key_manager().create_key(args.name, args.role)
        print(f"API key created for '{args.name}' (role={args.role}).")
        print(f"KEY (shown once, store it safely): {key}")
    elif args.command == "list-users":
        for u in list_users():
            print(u)
    elif args.command == "list-keys":
        config = _load_config()
        for kid, data in config.get("api_keys", {}).items():
            print({"key_id": kid, **{k: data.get(k) for k in ("name", "role", "created_at")}})


if __name__ == "__main__":
    _cli()
