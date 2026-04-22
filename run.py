#!/usr/bin/env python3
"""
CRACK SMS - Unified Launcher
Automatically starts both bot and API server for production deployment
Works on Railway, Docker, and local systems
"""

import os
import sys
import asyncio
import logging
import threading
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
logger = logging.getLogger(__name__)

def start_api_server():
    """Start FastAPI server in daemon thread."""
    try:
        import uvicorn
        from api_server import app as fastapi_app
        
        port = int(os.environ.get('API_PORT', 8000))
        logger.info(f"🚀 Starting FastAPI API Server on port {port}...")
        
        uvicorn.run(
            fastapi_app,
            host='0.0.0.0',
            port=port,
            log_level='info',
            access_log=True
        )
    except Exception as e:
        logger.error(f"❌ Failed to start API server: {e}", exc_info=True)
        sys.exit(1)

def start_telegram_bot():
    """Start Telegram bot."""
    try:
        import bot
        logger.info("🤖 Starting Telegram Bot...")
        # Bot starts via bot.py __main__ section
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}", exc_info=True)
        sys.exit(1)

def main():
    """Main entry point for production deployment."""
    logger.info("=" * 60)
    logger.info("🚀 CRACK SMS v21 - PRODUCTION LAUNCHER")
    logger.info("=" * 60)
    
    # Check environment
    if not os.environ.get('BOT_TOKEN'):
        logger.error("❌ BOT_TOKEN not set in environment variables!")
        sys.exit(1)
    
    logger.info(f"✅ BOT_TOKEN configured")
    logger.info(f"✅ Database: {os.environ.get('DATABASE_URL', 'SQLite (bot_database.db)')}")
    logger.info(f"✅ Admin IDs: {os.environ.get('SUPER_ADMIN_IDS', 'Not set')}")
    
    # Start API server in background thread
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()
    
    # Small delay to let API start
    import time
    time.sleep(2)
    
    logger.info("=" * 60)
    logger.info("✅ All services starting...")
    logger.info("=" * 60)
    
    # Start bot (this will run polling)
    import subprocess
    result = subprocess.run([sys.executable, 'bot.py'])
    sys.exit(result.returncode)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n⏹️  Shutdown signal received")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)
