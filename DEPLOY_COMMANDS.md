# Deploy to server 207.180.212.142 (ex.armcoincrypto.am)

Use **server IP** for upload (domain may not resolve from your Mac).

---

## 1) On your Mac — upload code (use IP)

```bash
cd /Users/gev/Autostory/autostory
rsync -avz --exclude 'venv' --exclude '__pycache__' --exclude '.git' --exclude '*.pyc' --exclude 'data' . root@207.180.212.142:/tmp/autostory-upload/
```

---

## 2) On the server — SSH then run these

```bash
ssh root@207.180.212.142
```

```bash
# Clean and prepare
sudo systemctl stop autostory-web autostory-scheduler 2>/dev/null || true
sudo systemctl disable autostory-web autostory-scheduler 2>/dev/null || true
sudo rm -rf /opt/autostory
sudo mkdir -p /opt/autostory
sudo chown $USER:$USER /opt/autostory
mv /tmp/autostory-upload/* /opt/autostory/
mv /tmp/autostory-upload/.[!.]* /opt/autostory/ 2>/dev/null || true
rmdir /tmp/autostory-upload 2>/dev/null || true
ls /opt/autostory
# Must show: main.py, requirements.txt, deploy/, src/
```

```bash
# Venv and deps
cd /opt/autostory
python3 -m venv venv
source venv/bin/activate
pip install -U pip wheel && pip install -r requirements.txt gunicorn
```

```bash
# .env and DB
cd /opt/autostory
cat > .env << 'EOF'
DATABASE_URL=sqlite:////opt/autostory/data/app.db
FLASK_SECRET_KEY=change-me-to-a-long-random-string
SECRET_KEY=change-me-to-a-long-random-string
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
EOF
mkdir -p data && chmod 755 data
source venv/bin/activate && python main.py init
```

```bash
# Systemd
cd /opt/autostory
sudo cp deploy/autostory-web.service /etc/systemd/system/
sudo cp deploy/autostory-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable autostory-web autostory-scheduler
sudo systemctl start autostory-web autostory-scheduler
sudo systemctl status autostory-web autostory-scheduler --no-pager
```

```bash
# Nginx (works by IP now; add domain when DNS is ready)
sudo tee /etc/nginx/sites-available/autostory << 'NGINX'
server {
    listen 80 default_server;
    server_name ex.armcoincrypto.am 207.180.212.142 _;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 120s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
    }
}
NGINX
sudo ln -sf /etc/nginx/sites-available/autostory /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Test in browser: **http://207.180.212.142**

```bash
# HTTPS only after DNS: ex.armcoincrypto.am A record → 207.180.212.142
sudo certbot --nginx -d ex.armcoincrypto.am
```

---

## DNS

- Add **A record**: `ex.armcoincrypto.am` → `207.180.212.142`
- Wait 5–30 min, then run certbot again.

---

## After updating code (e.g. TDATA import / discovery)

**Important:** `mv` does not overwrite existing directories, so new code under `src/`, `config/`, etc. was not applied. Use **rsync** to copy over new files, then restart:

**On the server** (after you already uploaded with rsync from Mac):

```bash
# Copy new code over existing (overwrites files; keeps venv and data)
rsync -av --exclude venv --exclude data --exclude .env /tmp/autostory-upload/ /opt/autostory/

# Restart so the new code runs
sudo systemctl restart autostory-web autostory-scheduler
sudo systemctl status autostory-web autostory-scheduler --no-pager
```

Or from your Mac, run the update script (it now uses rsync on the server):

```bash
./deploy/update-server.sh
```

---

## Summary

| From Mac | `rsync ... root@207.180.212.142:/tmp/autostory-upload/` |
| Server IP | 207.180.212.142 |
| Test before DNS | http://207.180.212.142 |
| After DNS + certbot | https://ex.armcoincrypto.am |
