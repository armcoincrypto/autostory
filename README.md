# STORYFLEET 🚀

**Telegram User-Account Orchestration Platform**

A powerful, scalable platform for managing multiple Telegram user accounts, publishing stories with mentions, and discovering users from public channels.

## 📋 Features

- **Multi-Account Management**: Orchestrate 5+ concurrent Telegram user sessions
- **Story Publishing**: Publish stories with mentions to drive engagement
- **User Discovery**: Scan public channels to discover potential audience
- **Rate Limiting**: Built-in anti-detection with intelligent rate limiting
- **Centralized Dashboard**: Web-based Flask dashboard for control
- **Bot Interface**: Alternative Telegram bot control interface
- **Task Queue**: Celery-powered background task processing
- **Docker Ready**: Full Docker Compose setup for easy deployment

## 🏗️ Architecture

```
STORYFLEET
├── src/
│   ├── core/           # Database models and base components
│   ├── clients/        # Telegram client management
│   ├── stories/        # Story publishing logic
│   ├── discovery/      # User discovery from channels
│   ├── dashboard/      # Flask web dashboard
│   ├── bot/            # Telegram bot interface
│   ├── queue/          # Celery task queue
│   └── utils/          # Helper utilities
├── config/             # Configuration management
├── tests/              # Test suite
├── data/               # Session and media storage
└── docker-compose.yml  # Docker configuration
```

## 🚀 Quick Start

### Prerequisites

- Python 3.9+
- Redis (for task queue)
- PostgreSQL (recommended) or SQLite
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)

### Installation

1. **Clone and setup**:
```bash
git clone <repository>
cd autostory
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
```

2. **Configure environment**:
```bash
cp .env.example .env
# Edit .env with your Telegram API credentials
```

3. **Initialize database**:
```bash
python main.py init
```

4. **Add your first account**:
```bash
python main.py add-account
```

5. **Start the dashboard**:
```bash
python main.py dashboard
```

Visit `http://localhost:5000` to access the dashboard.

### Local development (no deploy)

Run the dashboard on your Mac for quick UI iteration—edit code, save, browser auto-refreshes:

```bash
./run_local.sh
```

Then open **http://127.0.0.1:5001/** (5001 avoids macOS AirPlay on 5000).

To use real data (accounts, targets, deliveries) from the server:

```bash
./sync_db_from_server.sh              # Sync DB only
./sync_db_from_server.sh --env         # Sync DB + .env (so Add Account & Test now work)
./run_local.sh
```

### Docker Deployment

```bash
# Set environment variables
export TELEGRAM_API_ID=your_api_id
export TELEGRAM_API_HASH=your_api_hash
export DASHBOARD_SECRET_KEY=your_secret_key

# Start all services
docker-compose up -d

# With bot and monitoring
docker-compose --profile with-bot --profile monitoring up -d
```

## 📖 Usage

### Command Line Interface

```bash
# Start web dashboard
python main.py dashboard

# Start Telegram bot
python main.py bot

# Start Celery worker
python main.py worker

# Start Celery beat scheduler
python main.py beat

# Initialize database
python main.py init

# Add account interactively
python main.py add-account

# Show system status
python main.py status
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check |
| `/api/stats` | GET | System statistics |
| `/api/accounts` | GET | List accounts |
| `/api/accounts/auth/start` | POST | Start phone auth |
| `/api/accounts/auth/complete` | POST | Complete auth |
| `/api/stories` | GET | List stories |
| `/api/stories/publish` | POST | Publish story |
| `/api/stories/batch` | POST | Batch publish |
| `/api/discovery/users` | GET | List discovered users |
| `/api/discovery/scan` | POST | Scan channel |
| `/api/campaigns` | GET/POST | Manage campaigns |

### Publishing a Story

```python
from src.clients.manager import client_manager
from src.stories.publisher import story_publisher

# Initialize and connect
await client_manager.initialize()
client = await client_manager.get_client(account_id)

# Publish story
result = await story_publisher.publish_story(
    client_wrapper=client,
    media_path="/path/to/image.jpg",
    caption="Check this out!",
    mentions=[123456789, 987654321]
)
```

### Discovering Users

```python
from src.discovery.scanner import user_discovery

# Discover from channels
result = await user_discovery.discover_from_channels(
    channel_usernames=["@channelname"],
    limit_per_channel=500
)

print(f"Found {result['total_new_users']} new users")
```

## ⚙️ Configuration

Key configuration options in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_API_ID` | Telegram API ID | Required |
| `TELEGRAM_API_HASH` | Telegram API Hash | Required |
| `DATABASE_URL` | Database connection URL | SQLite |
| `REDIS_HOST` | Redis host | localhost |
| `DASHBOARD_PORT` | Dashboard port | 5000 |
| `TELEGRAM_MIN_DELAY_BETWEEN_ACTIONS` | Min delay (seconds) | 2.0 |
| `TELEGRAM_MAX_STORIES_PER_HOUR` | Max stories/hour | 10 |

## 🛡️ Anti-Detection Features

- **Randomized delays** between actions
- **Per-account rate limiting** with flood wait handling
- **Daily action limits** to prevent spam detection
- **Error backoff** with exponential delays
- **Typing simulation** for natural behavior
- **Session encryption** for secure storage

## 🧪 Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src

# Run specific test file
pytest tests/test_models.py
```

## 📁 Project Structure

```
src/
├── core/
│   ├── database.py      # Database setup
│   └── models.py        # SQLAlchemy models
├── clients/
│   ├── manager.py       # Multi-client orchestration
│   ├── session.py       # Session management
│   └── rate_limiter.py  # Rate limiting
├── stories/
│   ├── publisher.py     # Story publishing
│   └── templates.py     # Caption templates
├── discovery/
│   └── scanner.py       # Channel scanning
├── dashboard/
│   ├── app.py          # Flask app factory
│   ├── routes.py       # API endpoints
│   └── templates/      # HTML templates
├── bot/
│   └── bot.py          # Telegram bot
└── queue/
    ├── celery_app.py   # Celery configuration
    └── tasks.py        # Background tasks
```

## 🔐 Security Notes

- Never commit your `.env` file
- Use strong `DASHBOARD_SECRET_KEY` in production
- Session data is encrypted by default
- Restrict `BOT_ADMIN_IDS` to trusted users only
- Use HTTPS in production deployments

## 📝 License

MIT License - see LICENSE file for details.

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests
5. Submit a pull request

## ⚠️ Disclaimer

This tool is intended for legitimate marketing and engagement purposes. Users are responsible for complying with Telegram's Terms of Service and applicable laws regarding automated messaging and data collection.
