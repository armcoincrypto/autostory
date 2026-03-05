# STORYFLEET Deployment — Autostory + Messaging (Scheduler)

One dashboard: **Stories**, **Discovery**, **Campaigns**, **Accounts**, **Scheduler** (messaging).

Runs on the server as:
- **Web**: Flask (gunicorn) on port 8000
- **Scheduler worker**: `python main.py scheduler`

---

## 1. Subdomain at name.am

1. Log in to **name.am** (your domain registrar).
2. Open DNS management for your domain (e.g. `name.am`).
3. Add a **subdomain** (e.g. `app`, `autostory`, `dashboard`):
   - **Type**: `A`
   - **Name**: `app` (you will use `app.name.am`) or `autostory` → `autostory.name.am`
   - **Value**: your **server’s public IP**
   - TTL: 300–3600
4. Save and wait a few minutes for DNS to update. Check:
   ```bash
   ping app.name.am   # replace with your subdomain
   ```

Use that subdomain (e.g. `https://app.name.am`) for the rest of the steps.

---

## 2. Server setup

On your server (Ubuntu/Debian):

```bash
# Install Python 3.10+, Nginx, certbot
sudo apt update
sudo apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx

# Project directory
sudo mkdir -p /opt/autostory
sudo chown $USER:$USER /opt/autostory
cd /opt/autostory

# Clone or upload your project here (e.g. git clone ...)

# Virtualenv and dependencies
python3 -m venv venv
source venv/bin/activate
pip install -U pip wheel
pip install -r requirements.txt
pip install gunicorn
```

---

## 3. Environment and data

```bash
cd /opt/autostory
nano .env
```

Add (replace values and subdomain):

```bash
# Database
DATABASE_URL=sqlite:////opt/autostory/data/app.db

# Flask
FLASK_SECRET_KEY=generate-a-long-random-string-here
SECRET_KEY=generate-a-long-random-string-here

# Telegram (for accounts + Load my Telegram groups + stories)
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash

# Optional: if dashboard runs elsewhere and proxies to this server
# DASHBOARD_RUN_NOW_PROXY_URL=http://this-server:5000
```

Create data directory and init DB:

```bash
mkdir -p /opt/autostory/data
chmod 755 /opt/autostory/data
source venv/bin/activate
python main.py init
```

---

## 4. Systemd services

```bash
sudo cp deploy/autostory-web.service /etc/systemd/system/
sudo cp deploy/autostory-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable autostory-web.service autostory-scheduler.service
sudo systemctl start autostory-web.service autostory-scheduler.service
sudo systemctl status autostory-web.service autostory-scheduler.service --no-pager
```

Gunicorn will listen on `127.0.0.1:8000`; Nginx will expose it on your subdomain.

---

## 5. Nginx + subdomain (name.am)

Replace `app.name.am` with your actual subdomain (e.g. `autostory.name.am`).

Create site config:

```bash
sudo nano /etc/nginx/sites-available/autostory
```

Paste:

```nginx
server {
    listen 80;
    server_name app.name.am;
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
```

Enable and test:

```bash
sudo ln -s /etc/nginx/sites-available/autostory /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Open `http://app.name.am` in the browser — you should see the dashboard.

---

## 6. HTTPS (Let’s Encrypt) for app.name.am

```bash
sudo certbot --nginx -d app.name.am
```

Follow prompts (email, agree to terms). Certbot will adjust the Nginx config and add SSL. After that, use **https://app.name.am**.

Renewal is automatic; check with:

```bash
sudo certbot renew --dry-run
```

---

## 7. Logs

```bash
# Web app
sudo journalctl -u autostory-web.service -n 100 -f

# Scheduler worker
sudo journalctl -u autostory-scheduler.service -n 100 -f
```

---

## 8. Verification checklist

- [ ] **https://app.name.am** loads (Dashboard, Accounts, Stories, Discovery, Campaigns, Scheduler in sidebar).
- [ ] **Accounts**: add/login Telegram account; set Purpose to “Messaging” or “Both” for Scheduler.
- [ ] **Scheduler**: open Scheduler → choose user → “Load my Telegram groups” → add groups → set message and schedule → Save.
- [ ] **Stories**: Publish Story / Scan Channel from dashboard or Stories page (if you use them).
- [ ] Scheduler worker is running: `sudo systemctl status autostory-scheduler.service`.

---

## 9. Quick reference

| Item | Value |
|------|--------|
| App URL | `https://app.name.am` (use your subdomain) |
| Gunicorn | `127.0.0.1:8000` |
| Nginx config | `/etc/nginx/sites-available/autostory` |
| Project path | `/opt/autostory` |
| Data/DB | `/opt/autostory/data` |
| Services | `autostory-web`, `autostory-scheduler` |

Replace **app.name.am** everywhere with the subdomain you created at name.am (e.g. `autostory.name.am`).
