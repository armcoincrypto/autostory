# Clean deploy: replace all autostory code on server

Use this to deploy your **latest code**, remove all old files, and fix port/nginx.

---

## Step 1 — On your Mac: upload latest code

```bash
cd /Users/gev/Autostory/autostory
rsync -avz --exclude 'venv' --exclude '__pycache__' --exclude '.git' --exclude '*.pyc' --exclude 'data' . root@207.180.212.142:/tmp/autostory-upload/
```

---

## Step 2 — On the server: SSH in

```bash
ssh root@207.180.212.142
```

---

## Step 3 — Stop services and free port 8000

```bash
sudo systemctl stop autostory-web autostory-scheduler 2>/dev/null || true
sudo fuser -k 8000/tcp 2>/dev/null || true
sleep 2
```

---

## Step 4 — Remove old app and unpack new code

```bash
sudo rm -rf /opt/autostory
sudo mkdir -p /opt/autostory
sudo chown $USER:$USER /opt/autostory
mv /tmp/autostory-upload/* /opt/autostory/
mv /tmp/autostory-upload/.[!.]* /opt/autostory/ 2>/dev/null || true
rmdir /tmp/autostory-upload 2>/dev/null || true
ls /opt/autostory
# Must show: main.py, requirements.txt, deploy/, src/
```

---

## Step 5 — Python venv and dependencies

```bash
cd /opt/autostory
python3 -m venv venv
source venv/bin/activate
pip install -U pip wheel && pip install -r requirements.txt gunicorn
```

---

## Step 6 — .env and database

**Edit .env with your real values** (TELEGRAM_API_ID must be a number, TELEGRAM_API_HASH from my.telegram.org):

```bash
cd /opt/autostory
nano .env
```

Set at least:

- `DATABASE_URL=sqlite:////opt/autostory/data/app.db`
- `FLASK_SECRET_KEY` and `SECRET_KEY` (long random strings)
- `TELEGRAM_API_ID=<your_integer>`
- `TELEGRAM_API_HASH=<your_hash>`

Then:

```bash
mkdir -p data && chmod 755 data
source venv/bin/activate && python main.py init
```

---

## Step 7 — Systemd

```bash
cd /opt/autostory
sudo cp deploy/autostory-web.service /etc/systemd/system/
sudo cp deploy/autostory-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable autostory-web autostory-scheduler
sudo systemctl start autostory-web autostory-scheduler
sudo systemctl status autostory-web autostory-scheduler --no-pager
```

---

## Step 8 — Nginx (port 8000) and SSL

Use the project’s config so Nginx proxies to **port 8000** (not 8011):

```bash
cd /opt/autostory
sudo cp deploy/nginx-armcoincrypto.conf /etc/nginx/sites-available/autostory
sudo ln -sf /etc/nginx/sites-available/autostory /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

If you already have a certificate for ex.armcoincrypto.am, re-apply SSL (Certbot will add the 443 block):

```bash
sudo certbot --nginx -d ex.armcoincrypto.am
```

---

## Step 9 — Check

- **Site:** https://ex.armcoincrypto.am
- **Logs:**  
  `sudo journalctl -u autostory-web -n 30 --no-pager`  
  `sudo journalctl -u autostory-scheduler -n 30 --no-pager`

---

## If autostory-web fails: "Address already in use" (port 8000)

Run on the server (exact service name is **autostory-web** with a hyphen):

```bash
sudo systemctl stop autostory-web autostory-scheduler
sudo fuser -k 8000/tcp
sleep 2
sudo systemctl start autostory-web autostory-scheduler
sudo systemctl status autostory-web autostory-scheduler --no-pager
```

If you see "Unit autostorweb.service not found", the service name was mistyped; use **autostory-web** (with hyphen).

## One-shot server script (after rsync from Mac)

Run this **on the server** after Step 1. It does not create `.env`; edit `.env` before `python main.py init` if needed.

```bash
# Run from server as root, after you’ve rsync’d code to /tmp/autostory-upload/
set -e
systemctl stop autostory-web autostory-scheduler 2>/dev/null || true
fuser -k 8000/tcp 2>/dev/null || true
sleep 2
rm -rf /opt/autostory
mkdir -p /opt/autostory
mv /tmp/autostory-upload/* /opt/autostory/
mv /tmp/autostory-upload/.[!.]* /opt/autostory/ 2>/dev/null || true
rmdir /tmp/autostory-upload 2>/dev/null || true
cd /opt/autostory
python3 -m venv venv
./venv/bin/pip install -U pip wheel
./venv/bin/pip install -r requirements.txt gunicorn
mkdir -p data && chmod 755 data
# Edit .env here if needed, then:
# ./venv/bin/python main.py init
cp deploy/autostory-web.service /etc/systemd/system/
cp deploy/autostory-scheduler.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable autostory-web autostory-scheduler
cp deploy/nginx-armcoincrypto.conf /etc/nginx/sites-available/autostory
ln -sf /etc/nginx/sites-available/autostory /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
# After .env is set and init run:
# systemctl start autostory-web autostory-scheduler
```

Then create/edit `.env`, run `./venv/bin/python main.py init`, and start services:

```bash
systemctl start autostory-web autostory-scheduler
sudo certbot --nginx -d ex.armcoincrypto.am
```
