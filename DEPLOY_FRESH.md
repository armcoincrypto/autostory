# Full fresh deploy on server (ex.armcoincrypto.am)

Your **latest clean code** is in this repo on your Mac:
- Path: `~/Autostory/autostory` or `/Users/gev/Autostory/autostory`

You will: (1) get the code onto the server, (2) run these commands on the server.

---

## Step 1 — Get code to the server

**Option A — Git (recommended)**  
On your Mac, push to GitHub/GitLab (if not already), then on the server you’ll clone.

```bash
# On your Mac (in project folder)
cd /Users/gev/Autostory/autostory
git add -A && git commit -m "Deploy ex.armcoincrypto.am" && git push origin main
```

**Option B — Rsync from Mac to server**  
Replace `user` and `ex.armcoincrypto.am` (or your server IP) with your SSH user and host:

```bash
# On your Mac
cd /Users/gev/Autostory/autostory
rsync -avz --exclude 'venv' --exclude '__pycache__' --exclude '.git' --exclude '*.pyc' --exclude 'data' . user@ex.armcoincrypto.am:/opt/autostory/
```

---

## Step 2 — On the server: remove old app and deploy fresh

SSH into your server, then run these in order.

```bash
# 1) SSH to server (use your user and IP/hostname if different)
ssh root@ex.armcoincrypto.am
# or: ssh youruser@YOUR_SERVER_IP

# 2) Stop and disable old services (if they exist)
sudo systemctl stop autostory-web.service autostory-scheduler.service 2>/dev/null || true
sudo systemctl disable autostory-web.service autostory-scheduler.service 2>/dev/null || true

# 3) Remove old project (full clean)
sudo rm -rf /opt/autostory

# 4) Create directory and set owner
sudo mkdir -p /opt/autostory
sudo chown $USER:$USER /opt/autostory
cd /opt/autostory
```

---

## Step 3 — Put code on server

**If using Git:**

```bash
cd /opt/autostory
git clone https://github.com/YOUR_USER/autostory.git .
# or: git clone YOUR_REPO_URL .
```

**If using rsync:**  
You already ran the rsync from Mac in Step 1; skip this and stay in `/opt/autostory`.

---

## Step 4 — Python, venv, dependencies

```bash
cd /opt/autostory
sudo apt update
sudo apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx

python3 -m venv venv
source venv/bin/activate
pip install -U pip wheel
pip install -r requirements.txt
pip install gunicorn
```

---

## Step 5 — Environment and data

```bash
cd /opt/autostory
nano .env
```

Paste (edit values):

```
DATABASE_URL=sqlite:////opt/autostory/data/app.db
FLASK_SECRET_KEY=your-long-random-secret-key-here
SECRET_KEY=your-long-random-secret-key-here
TELEGRAM_API_ID=your_telegram_api_id
TELEGRAM_API_HASH=your_telegram_api_hash
```

Save (Ctrl+O, Enter, Ctrl+X). Then:

```bash
mkdir -p /opt/autostory/data
chmod 755 /opt/autostory/data
source venv/bin/activate
python main.py init
```

---

## Step 6 — Systemd services

```bash
cd /opt/autostory
sudo cp deploy/autostory-web.service /etc/systemd/system/
sudo cp deploy/autostory-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable autostory-web.service autostory-scheduler.service
sudo systemctl start autostory-web.service autostory-scheduler.service
sudo systemctl status autostory-web.service autostory-scheduler.service --no-pager
```

---

## Step 7 — Nginx for ex.armcoincrypto.am

```bash
cd /opt/autostory
sudo cp deploy/nginx-armcoincrypto.conf /etc/nginx/sites-available/autostory
sudo ln -sf /etc/nginx/sites-available/autostory /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

---

## Step 8 — HTTPS (Let’s Encrypt)

```bash
sudo certbot --nginx -d ex.armcoincrypto.am
```

Use the email it asks for and agree to terms. After this, the site will be at **https://ex.armcoincrypto.am**.

---

## Step 9 — Check

- Open **https://ex.armcoincrypto.am** in the browser.
- You should see Dashboard, Accounts, Stories, Discovery, Campaigns, Scheduler.

Logs if something fails:

```bash
sudo journalctl -u autostory-web.service -n 50 --no-pager
sudo journalctl -u autostory-scheduler.service -n 50 --no-pager
```

---

## One-line reference

**Where is my latest clean code?**  
- **On your Mac:** `/Users/gev/Autostory/autostory`  
- **On the server after deploy:** `/opt/autostory`

**Domain:** ex.armcoincrypto.am (DNS A record must point to this server’s IP).
