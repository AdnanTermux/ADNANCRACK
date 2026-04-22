"""
🔧 BOT CONFIGURATION MODULE
Centralized configuration for all bot settings - everything is now configurable!
"""

from typing import Dict, List, Any
import json
from pathlib import Path

# File paths
CONFIG_FILE = Path("config.json")

# ═══════════════════════════════════════════════════════════════
#  UI/UX CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# Button Styles (all buttons now consistent)
BUTTON_STYLES = {
    "success": "success",      # Green buttons for positive actions
    "danger": "danger",         # Red buttons for delete/stop actions  
    "primary": "primary",       # Blue buttons for navigation/info
    "neutral": "secondary",     # Gray buttons for neutral actions (fallback: primary)
}

# Button Text Customization
BUTTON_TEXT = {
    "back": "🔙  Back",
    "cancel": "❌  Cancel",
    "confirm": "✅  Confirm",
    "delete": "🗑  Delete",
    "add": "➕  Add",
    "edit": "✏️  Edit",
    "next": "Next ▶",
    "prev": "◀ Prev",
    "refresh": "🔁  Refresh",
    "close": "❌  Close",
    "submit": "✅  Submit",
    "approve": "✅  Approve",
    "reject": "❌  Reject",
}

# Menu Configuration
MENU_EMOJI = {
    "settings": "⚙️",
    "help": "📖",
    "profile": "👤",
    "numbers": "📱",
    "panels": "🔌",
    "admin": "👮",
    "analytics": "📊",
    "logs": "📋",
}

# ═══════════════════════════════════════════════════════════════
#  PERFORMANCE CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# Rate Limiting
RATE_LIMITS = {
    "number_request_cooldown": 5,      # Seconds between number requests
    "otp_fetch_interval": 1,            # Seconds between OTP fetches
    "max_requests_per_minute": 60,      # Global rate limit
    "admin_broadcast_delay": 0.5,       # Delay between broadcasts (prevents spam)
}

# Timeout Values
TIMEOUTS = {
    "message_edit_timeout": 3,          # Seconds to wait for message edit
    "database_timeout": 10,             # Seconds for database operations
    "api_call_timeout": 15,             # Seconds for API calls
    "webhook_timeout": 30,              # Seconds for webhook calls
    "request_expiry": 3600,             # Seconds until auto-delete old requests (1 hour)
}

# Cache Configuration
CACHE_CONFIG = {
    "user_cache_ttl": 300,              # Cache user data for 5 minutes
    "panel_status_cache_ttl": 60,       # Cache panel status for 1 minute
    "country_cache_ttl": 3600,          # Cache countries for 1 hour
    "admin_perms_cache_ttl": 180,       # Cache admin perms for 3 minutes
}

# ═══════════════════════════════════════════════════════════════
#  FEATURE TOGGLES
# ═══════════════════════════════════════════════════════════════

FEATURES = {
    "enable_analytics": True,           # Show analytics dashboard
    "enable_broadcast": True,           # Enable broadcast feature
    "enable_logging": True,             # Enable OTP logging
    "enable_webhook": True,             # Enable webhook notifications
    "enable_auto_cleanup": True,        # Auto-delete old OTP entries
    "enable_request_history": True,     # Keep bot request history
    "enable_child_bots": True,          # Allow child bot creation
    "enable_premium_tiers": True,       # Premium tier system
    "enable_animated_emoji": True,      # Premium animated emojis
    "enable_test_mode": False,          # Test mode (verbose logging)
}

# ═══════════════════════════════════════════════════════════════
#  LIMITS & QUOTAS
# ═══════════════════════════════════════════════════════════════

LIMITS = {
    "default_number_limit": 3,          # Default numbers per user
    "max_number_limit": 10,             # Maximum numbers a user can hold
    "free_tier_limit": 1,               # Free tier number limit
    "pro_tier_limit": 5,                # Pro tier number limit
    "enterprise_tier_limit": 20,        # Enterprise tier limit
    "max_panels": 50,                   # Maximum panels allowed
    "max_admin_users": 100,             # Maximum admins allowed
    "max_log_chats": 1000,              # Maximum log chats
    "max_broadcast_size": 10000,        # Max users for one broadcast
    "otp_store_max_size": 10000,        # Max OTPs to keep in store
}

# ═══════════════════════════════════════════════════════════════
#  MESSAGE CONFIGURATION
# ═══════════════════════════════════════════════════════════════

MESSAGES = {
    "unauthorized": "🚫 <b>Access Denied</b>\n<i>Admin privileges required</i>",
    "invalid_input": "❌ <b>Invalid Input</b>\n<i>Please check and try again</i>",
    "success_action": "✅ <b>Success</b>\n<i>Action completed successfully</i>",
    "error_occurred": "❌ <b>Error</b>\n<i>An error occurred. Please try again.</i>",
    "not_found": "❌ <b>Not Found</b>\n<i>The requested item was not found</i>",
    "timeout": "⏱️ <b>Timeout</b>\n<i>Request took too long. Try again.</i>",
    "no_permission": "🚫 <b>No Permission</b>\n<i>You don't have permission for this action</i>",
}

# ═══════════════════════════════════════════════════════════════
#  LOGGING CONFIGURATION
# ═══════════════════════════════════════════════════════════════

LOGGING = {
    "log_level": "INFO",                # DEBUG, INFO, WARNING, ERROR, CRITICAL
    "log_to_file": True,                # Write logs to file
    "log_file": "bot.log",              # Log file name
    "log_max_size_mb": 100,             # Max log file size before rotation
    "log_backup_count": 5,              # Number of backup log files
    "include_timestamps": True,         # Include timestamps in logs
    "include_caller_info": True,        # Include file/line info in logs
    "emoji_enhanced": True,             # Use emoji in logs
}

# ═══════════════════════════════════════════════════════════════
#  DATABASE CONFIGURATION
# ═══════════════════════════════════════════════════════════════

DATABASE = {
    "type": "sqlite",                   # sqlite, postgresql, mysql
    "path": "database.db",              # For SQLite
    "auto_cleanup": True,               # Auto-delete old records
    "cleanup_interval_hours": 24,       # Cleanup frequency
    "retention_days": 30,               # Keep records for N days
    "backup_enabled": True,             # Auto-backup database
    "backup_frequency": "daily",        # daily, weekly, monthly
}

# ═══════════════════════════════════════════════════════════════
#  SECURITY CONFIGURATION
# ═══════════════════════════════════════════════════════════════

SECURITY = {
    "require_2fa": False,               # Require 2FA for admins
    "session_timeout_hours": 24,        # Admin session timeout
    "max_login_attempts": 5,            # Max failed login attempts
    "lockout_duration_minutes": 15,     # Lockout after max attempts
    "encrypt_sensitive_data": True,     # Encrypt tokens/keys
    "audit_log_enabled": True,          # Log all admin actions
    "validate_callback_data": True,     # Validate callback data formats
}

# ═══════════════════════════════════════════════════════════════
#  CALLBACK HANDLER CONFIGURATION
# ═══════════════════════════════════════════════════════════════

CALLBACK_CONFIG = {
    "max_callback_data_length": 64,     # Max length of callback_data (Telegram limit)
    "callback_timeout_seconds": 30,     # Callback execution timeout
    "answer_callbacks": True,           # Send answer to callback queries
    "show_notification_alerts": True,   # Show toast notifications
    "edit_messages_on_callback": True,  # Edit message instead of new message
}

# ═══════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def get_button_style(button_type: str = "primary") -> str:
    """Get button style from configuration"""
    return BUTTON_STYLES.get(button_type, "primary")

def get_button_text(key: str, default: str = "") -> str:
    """Get button text from configuration"""
    return BUTTON_TEXT.get(key, default)

def get_enum_emoji(category: str, key: str) -> str:
    """Get emoji from menu configuration"""
    category_map = MENU_EMOJI if category == "menu" else {}
    return category_map.get(key, "")

def get_limit(limit_type: str) -> int:
    """Get limit value"""
    return LIMITS.get(limit_type, 1)

def is_feature_enabled(feature_name: str) -> bool:
    """Check if feature is enabled"""
    return FEATURES.get(feature_name, False)

def get_timeout(timeout_type: str) -> int:
    """Get timeout value in seconds"""
    return TIMEOUTS.get(timeout_type, 10)

def get_rate_limit(limit_type: str) -> float:
    """Get rate limiting value"""
    return RATE_LIMITS.get(limit_type, 1)

def get_message(msg_key: str) -> str:
    """Get message template"""
    return MESSAGES.get(msg_key, "")

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION OVERRIDE (from config.json)
# ═══════════════════════════════════════════════════════════════

def load_custom_config():
    """Load and override configuration from config.json"""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                custom = json.load(f)
                
            # Override any settings found in config.json
            if "BOT_CONFIG" in custom:
                config = custom["BOT_CONFIG"]
                for section, values in config.items():
                    if section == "LIMITS":
                        LIMITS.update(values)
                    elif section == "TIMEOUTS":
                        TIMEOUTS.update(values)
                    elif section == "FEATURES":
                        FEATURES.update(values)
                    elif section == "RATE_LIMITS":
                        RATE_LIMITS.update(values)
    except Exception as e:
        print(f"Warning: Could not load custom config: {e}")

# Load custom config on import
load_custom_config()
