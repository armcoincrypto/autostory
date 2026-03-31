# STORYFLEET — operations stabilization notes

Production-oriented checklist. Does not change application semantics.

---

## 1. Bot unit (`storyfleet-bot.service`) — `217/USER` and `User=` / `Group=`

### What went wrong

`systemd` exits with **`status=217/USER`** when the unit’s **`User=`** or **`Group=`** cannot be resolved (account does not exist on the host, or NSS failure). This is **not** a Python bug.

### Deploy drift: missing `deploy/host-preflight.sh`

The **repo** ships **`deploy/host-preflight.sh`** and **`deploy/update-server.sh`** expects it **on disk under `/opt/autostory/deploy/`** after every sync. If the host never received that file (old checkout, partial copy, wrong branch), operators see **`bash: deploy/host-preflight.sh: No such file or directory`**. That is a **deploy packaging / git alignment** problem, not an application defect.

Without **`storyfleet`** and **`chown`** on **`data/`** and **`/var/log/storyfleet`**, the **repo** bot unit (**`User=storyfleet`**) causes **`217/USER`** or runtime permission failures. An **emergency** workaround is to drop **`User=`** and run the bot as **root** — it “fixes” the symptom but **hides** the missing user and writable paths.

**`deploy/update-server.sh`** aborts after **`cp`** if **`deploy/host-preflight.sh`** is still missing, so a deploy **cannot** complete a bot restart while **`deploy/`** is incomplete — avoiding a **silent** return to **`217/USER`** when someone later restores the official unit from git.

### Safe patterns

**Preferred (least privilege):**

1. Prefer **`sudo bash /opt/autostory/deploy/host-preflight.sh`** (same user/flags as below). Or manually, equivalent to preflight:

   ```bash
   sudo useradd --system --home-dir /opt/autostory --no-create-home --shell /usr/sbin/nologin storyfleet 2>/dev/null || true
   sudo mkdir -p /opt/autostory/data /var/log/storyfleet
   sudo chown -R storyfleet:storyfleet /opt/autostory/data /var/log/storyfleet
   ```

2. Keep **`User=storyfleet`** and **`Group=storyfleet`** in **`deploy/storyfleet-bot.service`** (repo default).

3. Install and reload:

   ```bash
   sudo cp /opt/autostory/deploy/storyfleet-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl restart storyfleet-bot.service
   sudo systemctl status storyfleet-bot.service --no-pager
   ```

**Before every deploy:** confirm the user exists:

```bash
id storyfleet
```

If `id` fails, either create the user (commands above) or **do not** install a unit that references `User=storyfleet` until you do.

**Emergency / legacy host:** running without **`User=`** avoids `217/USER` but runs the bot as **`root`** — worse for security; only use with a deliberate ops decision and tight filesystem permissions elsewhere.

### Prevent “broken” `User=` from coming back

- **CI / review:** Treat changes under **`deploy/*.service`** as sensitive; verify **`User=`** matches an account present on target servers.
- **Fresh server:** run **`id storyfleet`** before **`systemctl enable storyfleet-bot`**.
- **Deploy script:** **`deploy/update-server.sh`** runs **`deploy/host-preflight.sh`** after syncing code (creates **`storyfleet`** if missing; **`chown`** on **`/opt/autostory/data`** and **`/var/log/storyfleet`** only). Run preflight manually after any non-`update-server` deploy: **`sudo bash /opt/autostory/deploy/host-preflight.sh`**.
- **If the host never received `deploy/host-preflight.sh`:** sync the repo (or copy **`deploy/host-preflight.sh`** from git), then run **`sudo bash /opt/autostory/deploy/host-preflight.sh`** before reinstalling **`User=storyfleet`** **`storyfleet-bot.service`**.

### One-shot: align host after an emergency unit (no `User=`)

**Risk:** switching from root-run bot to **`storyfleet`** requires **data** and **logs** writable by that user; run preflight first, then install the repo unit.

```bash
cd /opt/autostory
sudo bash deploy/host-preflight.sh   # creates storyfleet + chown data/ + /var/log/storyfleet only
sudo cp -f deploy/storyfleet-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart storyfleet-bot.service
getent passwd storyfleet
ls -ld /opt/autostory/data /var/log/storyfleet
```

---

## 2. Dashboard story precheck — Gunicorn vs in-memory pool

### Behavior

- **Scheduler / bot / Celery** may call **`client_manager.initialize()`**, which fills **`ClientManager._clients`**.
- **Gunicorn web workers** usually **do not** call **`initialize()`** on startup, so **`_clients`** is empty for normal dashboard traffic.

**Story precheck** (`POST /api/accounts/story-precheck`) uses **`get_fresh_client_for_story_publish()`**:

1. If an in-memory client exists for the account, it is used (**`User=` / pool path**).
2. Otherwise it builds a **one-off Telethon client** from the **canonical session file** (`account_<id>.session` or **`session_path`** on disk), runs the check, and disconnects (**ephemeral path**). Ephemeral wrappers set **`_precheck_disconnect_after`** so pooled clients are **not** disconnected after precheck.

**`no_client`** means: no pool entry **and** no resolvable on-disk session file for that account (or DB row missing), **not** “Telethon says stories are blocked.”

### Operational notes

- Avoid two concurrent heavy prechecks on the same `.session` from many workers if SQLite session locking becomes an issue; typical admin volume is fine.
- Session audit and canonical paths remain the source of truth for “file exists” vs DB.

---

## 3. Log rotation (`/var/log/storyfleet/`)

Bot and other processes may append to **`bot.log`** / **`bot-error.log`**. Without rotation, disks fill up.

**Example:** install **`deploy/logrotate.d-storyfleet`** (see that file) as **`/etc/logrotate.d/storyfleet`**, then test:

```bash
sudo logrotate -d /etc/logrotate.d/storyfleet
sudo logrotate -f /etc/logrotate.d/storyfleet
```

Adjust paths in the file if logs live elsewhere.

---

## 4. Low-risk operational hardening (optional)

| Item | Command / note |
|------|----------------|
| Web unit env | **`autostory-web.service`** uses **`EnvironmentFile=-/opt/autostory/.env`**; keep **`-`** so a missing file does not fail the unit on dev boxes. |
| DB schema on deploy | Gunicorn **`create_app()`** calls **`init_db()`**; ensures additive SQLite columns. After git pull, **`systemctl restart autostory-web`** so workers pick up code **and** run init. |
| `After=` in bot unit | **`postgresql.service` / `redis.service`** are harmless if unused (SQLite-only); optional cleanup later — not required for stability. |
| Secrets | Restrict **`.env`**: `chmod 600 /opt/autostory/.env`. |

---

## Rollback

- **Logrotate:** `sudo rm /etc/logrotate.d/storyfleet`
- **systemd:** restore previous unit from backup or `git checkout` the previous `deploy/storyfleet-bot.service`, then `sudo systemctl daemon-reload && sudo systemctl restart storyfleet-bot.service`
- **Docs only:** revert commits touching `docs/` and `deploy/` comments

---

## Quick verification (post-change)

```bash
sudo systemctl is-active storyfleet-bot.service autostory-web.service
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login
journalctl -u storyfleet-bot.service -n 30 --no-pager
```
