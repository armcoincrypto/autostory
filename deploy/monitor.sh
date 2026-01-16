#!/bin/bash
# STORYFLEET VPS Monitoring Script
# Run via cron: */5 * * * * /opt/autostory/deploy/monitor.sh
#
# Setup:
# 1. Edit BOT_TOKEN and CHAT_ID below
# 2. chmod +x /opt/autostory/deploy/monitor.sh
# 3. Add to crontab: crontab -e

# Configuration - EDIT THESE
BOT_TOKEN="${BOT_TOKEN:-}"  # Set in .env or here
CHAT_ID="${CHAT_ID:-}"       # Admin chat ID

# Load environment if available
if [ -f /opt/autostory/.env ]; then
    export $(grep -v '^#' /opt/autostory/.env | xargs)
fi

# Paths
APP_DIR="/opt/autostory"
LOG_DIR="/var/log/storyfleet"
LOG_FILE="$LOG_DIR/monitor.log"

# Create log directory if needed
mkdir -p $LOG_DIR

# Timestamp
NOW=$(date '+%Y-%m-%d %H:%M:%S')

# Function to send Telegram alert
send_alert() {
    local message="$1"
    if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${CHAT_ID}" \
            -d "text=${message}" \
            -d "parse_mode=HTML" > /dev/null 2>&1
    fi
    echo "[$NOW] ALERT: $message" >> $LOG_FILE
}

# Function to log
log() {
    echo "[$NOW] $1" >> $LOG_FILE
}

# Start monitoring
log "=== Monitor Check Started ==="

# Check 1: Bot Process
BOT_PID=$(pgrep -f "main.py bot" | head -1)
if [ -z "$BOT_PID" ]; then
    log "❌ Bot NOT running"
    send_alert "🚨 <b>STORYFLEET Bot Down!</b>%0A%0ABot process not found. Attempting restart..."

    # Attempt restart
    cd $APP_DIR
    source venv/bin/activate 2>/dev/null
    nohup python3 main.py bot > $LOG_DIR/bot.log 2>&1 &
    sleep 3

    NEW_PID=$(pgrep -f "main.py bot" | head -1)
    if [ -n "$NEW_PID" ]; then
        send_alert "✅ Bot restarted successfully (PID: $NEW_PID)"
        log "✅ Bot restarted (PID: $NEW_PID)"
    else
        send_alert "❌ Bot restart FAILED! Manual intervention required."
        log "❌ Bot restart failed"
    fi
else
    log "✅ Bot running (PID: $BOT_PID)"

    # Check CPU/Memory usage
    CPU=$(ps -p $BOT_PID -o %cpu | tail -1 | tr -d ' ')
    MEM=$(ps -p $BOT_PID -o %mem | tail -1 | tr -d ' ')
    log "   CPU: ${CPU}% | Memory: ${MEM}%"

    # Alert if high usage
    CPU_INT=${CPU%.*}
    MEM_INT=${MEM%.*}
    if [ "$CPU_INT" -gt 80 ] 2>/dev/null; then
        send_alert "⚠️ High CPU usage: ${CPU}%"
    fi
    if [ "$MEM_INT" -gt 80 ] 2>/dev/null; then
        send_alert "⚠️ High Memory usage: ${MEM}%"
    fi
fi

# Check 2: Redis
if command -v redis-cli &> /dev/null; then
    if redis-cli ping > /dev/null 2>&1; then
        log "✅ Redis running"
    else
        log "❌ Redis not responding"
        send_alert "⚠️ Redis is not responding"

        # Try to restart Redis
        sudo systemctl restart redis-server 2>/dev/null
    fi
else
    log "ℹ️ Redis not installed"
fi

# Check 3: Database File
DB_FILE="$APP_DIR/data/storyfleet.db"
if [ -f "$DB_FILE" ]; then
    DB_SIZE=$(du -h "$DB_FILE" | cut -f1)
    log "✅ Database exists ($DB_SIZE)"
else
    DB_FILE="$APP_DIR/storyfleet.db"
    if [ -f "$DB_FILE" ]; then
        DB_SIZE=$(du -h "$DB_FILE" | cut -f1)
        log "✅ Database exists ($DB_SIZE)"
    else
        log "⚠️ Database file not found"
    fi
fi

# Check 4: Disk Space
DISK_USAGE=$(df -h / | awk 'NR==2 {print $5}' | tr -d '%')
log "💾 Disk usage: ${DISK_USAGE}%"
if [ "$DISK_USAGE" -gt 85 ] 2>/dev/null; then
    send_alert "⚠️ Low disk space: ${DISK_USAGE}% used"
fi

# Check 5: Recent Errors in Log
if [ -f "$LOG_DIR/bot.log" ]; then
    ERROR_COUNT=$(tail -100 "$LOG_DIR/bot.log" | grep -c -i "error\|exception\|failed" 2>/dev/null || echo "0")
    log "📋 Recent errors in log: $ERROR_COUNT"

    if [ "$ERROR_COUNT" -gt 10 ]; then
        send_alert "⚠️ Multiple errors in bot log ($ERROR_COUNT in last 100 lines)"
    fi
fi

# Check 6: Network Connectivity (Telegram API)
if curl -s --connect-timeout 5 https://api.telegram.org > /dev/null; then
    log "✅ Telegram API reachable"
else
    log "❌ Cannot reach Telegram API"
    send_alert "🚨 Cannot reach Telegram API - Network issue?"
fi

log "=== Monitor Check Complete ==="

# Rotate log if too large (> 10MB)
if [ -f "$LOG_FILE" ]; then
    LOG_SIZE=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo "0")
    if [ "$LOG_SIZE" -gt 10485760 ]; then
        mv "$LOG_FILE" "${LOG_FILE}.old"
        log "Log rotated"
    fi
fi

exit 0
