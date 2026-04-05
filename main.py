#!/usr/bin/env python3
"""
STORYFLEET - Main Entry Point
Telegram User-Account Orchestration Platform
"""
import sys
import asyncio
import argparse
import structlog

# Configure logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(colors=True)
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


def run_dashboard():
    """Run the Flask dashboard"""
    from src.dashboard.app import create_app
    from src.core.database import init_db
    from config.settings import settings

    # Initialize database
    init_db()

    # Create and run Flask app
    app = create_app()
    logger.info(
        "Starting dashboard",
        host=settings.dashboard.host,
        port=settings.dashboard.port
    )

    app.run(
        host=settings.dashboard.host,
        port=settings.dashboard.port,
        debug=settings.dashboard.debug
    )


def run_bot():
    """Run the Telegram bot"""
    from src.bot.bot import run_bot as start_bot
    from src.core.database import init_db

    # Initialize database
    init_db()

    logger.info("Starting Telegram bot")
    asyncio.run(start_bot())


def run_worker():
    """Run Celery worker"""
    from src.queue.celery_app import celery_app

    logger.info("Starting Celery worker")
    celery_app.worker_main(["worker", "--loglevel=info"])


def run_beat():
    """Run Celery beat scheduler"""
    from src.queue.celery_app import celery_app

    logger.info("Starting Celery beat")
    celery_app.worker_main(["beat", "--loglevel=info"])


def run_scheduler():
    """Run Auto Message Scheduler worker"""
    from src.scheduler.worker import main as scheduler_main

    logger.info("Starting Auto Message Scheduler")
    scheduler_main()


def init_database():
    """Initialize the database"""
    from src.core.database import init_db

    logger.info("Initializing database")
    init_db()
    logger.info("Database initialized successfully")


def add_account_interactive():
    """Interactive account addition"""
    from src.clients.manager import client_manager
    from src.core.database import init_db

    init_db()

    async def add():
        phone = input("Enter phone number (with country code, e.g., +1234567890): ").strip()

        result = await client_manager.start_phone_auth(phone)

        if not result["success"]:
            print(f"Error: {result['error']}")
            return

        print(f"Code sent to {phone}")
        code = input("Enter verification code: ").strip()

        password = None
        result = await client_manager.complete_phone_auth(
            phone_number=phone,
            code=code,
            phone_code_hash=result["phone_code_hash"],
            session_string=result["session_string"],
            password=password
        )

        if result.get("needs_password"):
            password = input("Enter 2FA password: ").strip()
            result = await client_manager.complete_phone_auth(
                phone_number=phone,
                code=code,
                phone_code_hash=result["phone_code_hash"],
                session_string=result["session_string"],
                password=password
            )

        if result["success"]:
            print(f"✅ Account added successfully!")
            print(f"   User ID: {result['user_id']}")
            print(f"   Username: {result.get('username', 'N/A')}")
        else:
            print(f"❌ Error: {result['error']}")

    asyncio.run(add())


def show_status():
    """Show system status"""
    from src.core.database import get_db_context, init_db
    from src.core.models import Account, Story, DiscoveredUser, Campaign, AccountStatus

    init_db()

    with get_db_context() as db:
        accounts = db.query(Account).count()
        active = db.query(Account).filter(Account.status == AccountStatus.ACTIVE).count()
        stories = db.query(Story).count()
        users = db.query(DiscoveredUser).count()
        campaigns = db.query(Campaign).filter(Campaign.is_active == True).count()

    print("\n" + "="*50)
    print("         STORYFLEET Status")
    print("="*50)
    print(f"\n  📱 Accounts:     {active}/{accounts} active")
    print(f"  📸 Stories:      {stories} published")
    print(f"  👥 Users:        {users} discovered")
    print(f"  🎯 Campaigns:    {campaigns} active")
    print("\n" + "="*50 + "\n")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="STORYFLEET - Telegram User-Account Orchestration Platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py dashboard    # Start web dashboard
  python main.py bot          # Start Telegram bot
  python main.py worker       # Start Celery worker
  python main.py init         # Initialize database
  python main.py add-account  # Add new account interactively
  python main.py status       # Show system status
        """
    )

    parser.add_argument(
        "command",
        choices=["dashboard", "bot", "worker", "beat", "scheduler", "init", "add-account", "status"],
        help="Command to run"
    )

    args = parser.parse_args()

    commands = {
        "dashboard": run_dashboard,
        "bot": run_bot,
        "worker": run_worker,
        "beat": run_beat,
        "scheduler": run_scheduler,
        "init": init_database,
        "add-account": add_account_interactive,
        "status": show_status,
    }

    try:
        commands[args.command]()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        sys.exit(0)
    except Exception as e:
        logger.error("Fatal error", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
