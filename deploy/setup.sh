#!/bin/bash
# STORYFLEET Production Setup Script
# Run as root on Ubuntu 22.04

set -e

echo "🚀 STORYFLEET Production Setup"
echo "=============================="

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Check root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root${NC}"
    exit 1
fi

# Variables
APP_DIR="/opt/autostory"
APP_USER="storyfleet"
LOG_DIR="/var/log/storyfleet"

echo ""
echo -e "${YELLOW}Step 1: System Updates${NC}"
apt update && apt upgrade -y

echo ""
echo -e "${YELLOW}Step 2: Installing Dependencies${NC}"
apt install -y \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    postgresql \
    postgresql-contrib \
    redis-server \
    nginx \
    supervisor \
    git \
    curl \
    ufw \
    fail2ban

echo ""
echo -e "${YELLOW}Step 3: Creating Application User${NC}"
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -s /bin/false -d $APP_DIR $APP_USER
    echo -e "${GREEN}User $APP_USER created${NC}"
else
    echo "User $APP_USER already exists"
fi

echo ""
echo -e "${YELLOW}Step 4: Setting up directories${NC}"
mkdir -p $APP_DIR/data
mkdir -p $LOG_DIR
chown -R $APP_USER:$APP_USER $APP_DIR
chown -R $APP_USER:$APP_USER $LOG_DIR

echo ""
echo -e "${YELLOW}Step 5: Setting up PostgreSQL${NC}"
sudo -u postgres psql -c "CREATE DATABASE storyfleet;" 2>/dev/null || echo "Database may already exist"
sudo -u postgres psql -c "CREATE USER storyfleet WITH PASSWORD 'storyfleet_secure_password';" 2>/dev/null || echo "User may already exist"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE storyfleet TO storyfleet;"

echo ""
echo -e "${YELLOW}Step 6: Enabling Services${NC}"
systemctl enable postgresql
systemctl enable redis-server
systemctl start postgresql
systemctl start redis-server

echo ""
echo -e "${YELLOW}Step 7: Setting up Python Virtual Environment${NC}"
cd $APP_DIR
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo -e "${YELLOW}Step 8: Installing Systemd Service${NC}"
cp deploy/storyfleet-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable storyfleet-bot

echo ""
echo -e "${YELLOW}Step 9: Setting up Firewall${NC}"
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
echo "y" | ufw enable

echo ""
echo -e "${YELLOW}Step 10: Setting up Log Rotation${NC}"
cat > /etc/logrotate.d/storyfleet << EOF
/var/log/storyfleet/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 storyfleet storyfleet
    sharedscripts
    postrotate
        systemctl reload storyfleet-bot > /dev/null 2>&1 || true
    endscript
}
EOF

echo ""
echo -e "${GREEN}=============================="
echo "✅ STORYFLEET Setup Complete!"
echo "==============================${NC}"
echo ""
echo "Next steps:"
echo "1. Edit /opt/autostory/.env with your credentials"
echo "2. Initialize database: cd /opt/autostory && source venv/bin/activate && python main.py init-db"
echo "3. Start the bot: systemctl start storyfleet-bot"
echo "4. Check status: systemctl status storyfleet-bot"
echo "5. View logs: tail -f /var/log/storyfleet/bot.log"
echo ""
echo -e "${YELLOW}Database connection string for .env:${NC}"
echo "DATABASE_URL=postgresql://storyfleet:storyfleet_secure_password@localhost/storyfleet"
