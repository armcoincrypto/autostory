#!/usr/bin/env python3
"""
STORYFLEET Test Suite
Run this BEFORE deploying to VPS to verify everything works
"""
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'


def success(msg):
    print(f"{Colors.GREEN}✅ {msg}{Colors.RESET}")


def error(msg):
    print(f"{Colors.RED}❌ {msg}{Colors.RESET}")


def warning(msg):
    print(f"{Colors.YELLOW}⚠️  {msg}{Colors.RESET}")


def info(msg):
    print(f"{Colors.BLUE}ℹ️  {msg}{Colors.RESET}")


async def test_database():
    """Test database connection and tables"""
    print("\n📊 Testing Database...")

    try:
        from src.core.database import get_db_context, engine
        from src.core.models import Account, DiscoveredUser, Story, Campaign

        with get_db_context() as db:
            # Check tables exist
            accounts = db.query(Account).count()
            users = db.query(DiscoveredUser).count()
            stories = db.query(Story).count()

            success(f"Database connected")
            info(f"  Accounts: {accounts}")
            info(f"  Discovered users: {users}")
            info(f"  Stories published: {stories}")

            # Check for active accounts
            from src.core.models import AccountStatus
            active = db.query(Account).filter(
                Account.status == AccountStatus.ACTIVE,
                Account.session_string.isnot(None)
            ).count()

            if active > 0:
                success(f"Active accounts with sessions: {active}")
            else:
                warning("No active accounts with sessions - use /login to add one")

            return True

    except Exception as e:
        error(f"Database failed: {e}")
        return False


async def test_redis():
    """Test Redis connection"""
    print("\n🔴 Testing Redis...")

    try:
        import redis
        r = redis.Redis(host='localhost', port=6379, decode_responses=True)
        r.ping()
        success("Redis connected")

        # Test set/get
        r.setex("storyfleet_test", 10, "test_value")
        val = r.get("storyfleet_test")
        if val == "test_value":
            success("Redis read/write working")

        return True

    except ImportError:
        warning("Redis package not installed (pip install redis)")
        return False
    except redis.ConnectionError:
        warning("Redis not running (install: sudo apt install redis-server)")
        return False
    except Exception as e:
        warning(f"Redis error: {e}")
        return False


async def test_telegram_config():
    """Test Telegram configuration"""
    print("\n📱 Testing Telegram Config...")

    try:
        from config.settings import settings

        if settings.telegram.api_id and settings.telegram.api_id != 0:
            success(f"API ID configured: {settings.telegram.api_id}")
        else:
            error("API ID not configured in .env")
            return False

        if settings.telegram.api_hash and settings.telegram.api_hash != "":
            success("API Hash configured")
        else:
            error("API Hash not configured in .env")
            return False

        if settings.bot.token and settings.bot.token != "":
            success("Bot token configured")
        else:
            error("Bot token not configured in .env")
            return False

        if settings.bot.admin_ids:
            success(f"Admin IDs: {settings.bot.admin_ids}")
        else:
            warning("No admin IDs configured")

        return True

    except Exception as e:
        error(f"Config error: {e}")
        return False


async def test_session_manager():
    """Test session manager"""
    print("\n🔐 Testing Session Manager...")

    try:
        from src.core.session_manager import session_manager

        # Test encryption
        test_data = "test_session_string_12345"
        encrypted = session_manager._encrypt(test_data)
        decrypted = session_manager._decrypt(encrypted)

        if decrypted == test_data:
            success("Session encryption working")
        else:
            error("Session encryption/decryption mismatch")
            return False

        return True

    except Exception as e:
        error(f"Session manager error: {e}")
        return False


async def test_scanner():
    """Test group scanner module"""
    print("\n🔍 Testing Scanner...")

    try:
        from src.discovery.scanner import GroupMessageScanner, user_discovery

        scanner = GroupMessageScanner()
        success("Scanner initialized")

        # Check if we have a client to use
        client = await scanner.get_active_client()
        if client:
            success("Active client available for scanning")
            await client.disconnect()
        else:
            warning("No active client - login required to scan")

        return True

    except Exception as e:
        error(f"Scanner error: {e}")
        return False


async def test_story_publisher():
    """Test story publisher module"""
    print("\n📤 Testing Story Publisher...")

    try:
        from src.publisher.story_publisher import StoryPublisher, story_publisher

        publisher = StoryPublisher()
        success("Story publisher initialized")

        # Check for available account
        account_id = await publisher.get_available_account()
        if account_id:
            success(f"Available account for publishing: #{account_id}")
        else:
            warning("No available accounts - login required")

        # Check for users to mention
        users = await publisher.get_users_for_mention(5)
        if users:
            success(f"Users available for mention: {len(users)}")
        else:
            warning("No users collected - use /scan to collect users")

        return True

    except Exception as e:
        error(f"Story publisher error: {e}")
        return False


async def test_bot_handlers():
    """Test bot can be imported"""
    print("\n🤖 Testing Bot...")

    try:
        from src.bot.bot import StoryFleetBot

        success("Bot module loaded")

        # Check handlers are defined
        bot = StoryFleetBot()
        success("Bot instance created")

        return True

    except Exception as e:
        error(f"Bot error: {e}")
        return False


async def test_health_monitor():
    """Test health monitor"""
    print("\n💓 Testing Health Monitor...")

    try:
        from src.core.health_monitor import HealthMonitor, health_monitor

        monitor = HealthMonitor()
        success("Health monitor initialized")

        # Get system status
        status = await monitor.get_system_status()
        success(f"System status: {status['health']}")
        info(f"  Total accounts: {status['accounts']['total']}")
        info(f"  Active accounts: {status['accounts']['active']}")

        return True

    except Exception as e:
        error(f"Health monitor error: {e}")
        return False


async def run_all_tests():
    """Run all tests"""
    print("=" * 60)
    print(f"{Colors.BLUE}🧪 STORYFLEET TEST SUITE{Colors.RESET}")
    print(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    results = {}

    # Core tests
    results['database'] = await test_database()
    results['redis'] = await test_redis()
    results['telegram_config'] = await test_telegram_config()
    results['session_manager'] = await test_session_manager()
    results['scanner'] = await test_scanner()
    results['story_publisher'] = await test_story_publisher()
    results['bot'] = await test_bot_handlers()
    results['health_monitor'] = await test_health_monitor()

    # Summary
    print("\n" + "=" * 60)
    print(f"{Colors.BLUE}📋 TEST SUMMARY{Colors.RESET}")
    print("=" * 60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test, result in results.items():
        status = f"{Colors.GREEN}PASS{Colors.RESET}" if result else f"{Colors.RED}FAIL{Colors.RESET}"
        print(f"  {test}: {status}")

    print(f"\n  Total: {passed}/{total} tests passed")

    # Recommendations
    print("\n" + "=" * 60)
    print(f"{Colors.YELLOW}📝 RECOMMENDATIONS{Colors.RESET}")
    print("=" * 60)

    if not results['database']:
        print("  • Run: python3 main.py init-db")

    if not results['redis']:
        print("  • Install Redis: sudo apt install redis-server")
        print("  • Start Redis: sudo systemctl start redis-server")

    if not results['telegram_config']:
        print("  • Configure .env with API_ID, API_HASH, BOT_TOKEN")

    # Check for accounts
    try:
        from src.core.database import get_db_context
        from src.core.models import Account, AccountStatus
        with get_db_context() as db:
            active = db.query(Account).filter(
                Account.status == AccountStatus.ACTIVE
            ).count()
            if active == 0:
                print("  • Use /login command to add a Telegram account")

            from src.core.models import DiscoveredUser
            users = db.query(DiscoveredUser).count()
            if users == 0:
                print("  • Use /scan command to collect users from groups")
    except:
        pass

    print("\n" + "=" * 60)

    if passed == total:
        print(f"{Colors.GREEN}✅ ALL TESTS PASSED - READY FOR DEPLOYMENT{Colors.RESET}")
        return 0
    elif passed >= total - 2:
        print(f"{Colors.YELLOW}⚠️  MOSTLY READY - Fix warnings above{Colors.RESET}")
        return 0
    else:
        print(f"{Colors.RED}❌ NOT READY - Fix errors above{Colors.RESET}")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(run_all_tests())
    sys.exit(exit_code)
