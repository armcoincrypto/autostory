# Tdata Upload: Problems and Implementation Spec

Use this as a prompt or spec to implement or fix tdata zip import correctly.

---

## 1. Expected zip structures

- **Single-account zip**: Root contains a folder with `map.json` (tdata from Telegram Desktop), or a single subfolder that has `map.json` or `tdata/map.json`. One session per zip.
- **Multi-account zip (e.g. "10-us-19.01.zip")**: Root contains **multiple inner zips** (e.g. 10 files: `1.zip`, `2.zip`, …). Each inner zip is one account: either a tdata folder (with `map.json`) or `.session` files / session strings. Expected: **one session per inner zip**, and the importer must process **every** inner zip, not only the first.

---

## 2. Problems we had (and fixes)

### 2.1 Only first account imported from multi-account zip

- **Cause**: Logic found the first folder that had tdata (`map.json`), converted that single folder to one session, and returned. Other nested zips were never processed.
- **Fix**: If there are nested zips (e.g. `nested_0` … `nested_N`), do **not** use "first tdata = single session". For **each** base (root + each `nested_*`), try tdata conversion and/or `find_all_session_strings_in_extracted`, collect all session strings, deduplicate, then import each.

### 2.2 Duplicate nested zip extraction

- **Cause**: The same "extract all nested zips" loop ran twice. The second run overwrote the same `nested_0` … `nested_N` folders.
- **Fix**: Run the nested extraction loop **once** only. Then build `bases = [tmpdir, nested_0, nested_1, …]` and collect sessions from each base.

### 2.3 Request timeout when importing many accounts

- **Cause**: Importing 10 accounts (connect + get_me + GetFullUser + DB per account) can take 1–2 minutes. Browser or proxy could time out.
- **Fix**: Use a long timeout (e.g. 5 minutes) for the import request on the client; show a clear "may take 1–2 min for many accounts" message; handle timeout errors and suggest refreshing or smaller batches.

### 2.4 Some sessions fail with "Session not authorized"

- **Cause**: Not all items in the zip are valid/authorized sessions. Expired, revoked, or incomplete tdata/session strings will fail at `import_session_string` with "Session not authorized" or similar.
- **Fix**: Per-session errors are expected. Return a summary: `imported`, `updated`, `failed`, and a list of errors with index (e.g. "#7: Session not authorized"). Do not treat one failure as a global failure; continue importing the rest.

### 2.5 Username / phone missing for new accounts

- **Cause**: For some sessions `get_me()` returns `username=None` or `phone=None`. Only updating existing accounts by `session_string`/`last_active` and not refreshing `username`/`first_name`/`last_name`/`phone_number` from Telegram.
- **Fix**: When updating an existing account by `user_id`, refresh all profile fields from `get_me()`. When `get_me().phone` is missing, try `GetFullUserRequest` for the current user and use `full_user.phone`; normalize with leading `+`. Store and show username/phone in the UI even when initially missing.

---

## 3. Required behavior (implementation checklist)

- [ ] **Single zip**: If the zip has no inner zips, look for one tdata root (folder with `map.json`). If found, convert that folder to one session and import. If not found, scan the whole extracted tree for `.session` files and session strings and import all found.
- [ ] **Multi zip**: If the zip contains inner zips, extract each inner zip into a distinct folder (`nested_0`, `nested_1`, …). For **each** of these folders (and optionally root): (a) try tdata conversion if `map.json` exists, (b) run `find_all_session_strings_in_extracted` for that folder. Collect all session strings, deduplicate by string value, then import each in order.
- [ ] **No double extraction**: Extract nested zips only once; do not overwrite the same nested folders in a second loop.
- [ ] **Per-account import**: For each collected session string, call `import_session_string`. Count imported vs updated vs failed; return failed indices and error messages. Do not abort the whole batch on first failure.
- [ ] **Profile data**: On import (new or update), set/refresh `phone_number`, `username`, `first_name`, `last_name` from Telegram (`get_me()` and, if needed, `GetFullUserRequest`). Use a fallback like `user_<id>` for phone when Telegram does not return it.
- [ ] **Timeouts**: Server and client should allow long-running import (e.g. 5+ minutes for many accounts). Client shows a clear "processing many accounts" message and handles timeout with a helpful message.

---

## 4. Zip layout examples

- **Example A – Single tdata**:  
  `upload.zip` → extract → `tdata/map.json`, … → one session.

- **Example B – Multi-account (10-us style)**:  
  `10-us-19.01.zip` → extract → `1.zip`, `2.zip`, … `10.zip`. Each `K.zip` extracts to `nested_{K-1}` and contains either tdata (with `map.json`) or `.session` files. Result: 10 sessions, one per inner zip.

- **Example C – Flat session files**:  
  `upload.zip` → extract → `session/12345.session`, `session/67890.session`, … → `find_all_session_strings_in_extracted` returns N strings; import all.

---

## 5. Dependencies and environment

- **opentele**: Required for tdata → session conversion (`TDesktop`, `ToTelethon`). Use `keyFile` options `None` and `"datas"` if the first fails.
- **TELEGRAM_API_ID / TELEGRAM_API_HASH**: Must be set (or use TelegramDesktop API) so that converted sessions work with the same app credentials used by the rest of the app.
- **Passcode**: If Telegram Desktop uses a local passcode, the user must provide it (e.g. in "Passcode or session string"); pass it to `tdata_to_session_string(..., passcode=passcode)`.

---

Use this spec to verify or reimplement the tdata upload flow so that single- and multi-account zips are handled correctly, with no "only first account" or duplicate-extraction bugs, and with clear errors and timeouts for large batches.

---

## 6. How to test

### 6.1 Single-account ZIP

1. Create a zip that contains a single `tdata/` folder (from Telegram Desktop) with `map.json` inside.
2. In Dashboard → Accounts, open "Add account", upload the zip, optionally set passcode, click "Upload and import account".
3. **Expect**: One new account appears in the list. After import, the results modal shows "Discovered 1 candidate(s). 1 imported, 0 updated, 0 failed."
4. **DB**: One new row in `accounts` with `phone_number`, `username`, `first_name`, `last_name`, `user_id` populated (from `get_me()` / GetFullUser after import).

### 6.2 Multi-account ZIP (5+ accounts)

1. Create a zip that contains multiple inner zips (e.g. `1.zip`, `2.zip`, … `5.zip`), each with a tdata folder or `.session` / session string.
2. Upload the zip and run import.
3. **Expect**: Results modal shows "Discovered N candidate(s)" with N ≥ 5; imported + updated + failed sum to N. Each row shows status (OK/FAILED), account/phone/username for OK, and reason/error for FAILED.
4. **DB**: Row count increases by the number of successful imports. Each new/updated account has profile fields set.

### 6.3 One invalid session in the zip

1. Use a multi-account zip where at least one inner zip contains an expired/revoked session or invalid tdata.
2. Run import.
3. **Expect**: That account shows status FAILED with a reason (e.g. "Session not authorized"). Other accounts are still imported.
4. **UI**: Results modal lists each candidate with OK or FAILED and the error message for failures.

### 6.4 Safety and regression

- **Zip-slip**: Zip with paths like `../../etc/passwd` should not extract outside the temp dir.
- **Existing behavior**: Single-account success still returns API shape compatible with previous clients. Paste-session-string import and "Import from pasted string" still work. Account list and health check unchanged.
