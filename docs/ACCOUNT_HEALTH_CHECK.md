# Account health check (“alive” vs deleted)

## What it does

The “Check if accounts are alive” feature connects each account via Telethon and runs several API calls. Only if **all** of them succeed and the user has no `deleted`/`restricted` flags do we mark the account as **alive**.

## Run from the correct directory

All commands must be run from the **project root** — the directory that contains `main.py`, `src/`, and `scripts/`. On the VPS, that is your autostory deploy directory (e.g. `/root/autostory` or wherever you deployed). If you see `ModuleNotFoundError: No module named 'scripts'` or `No module named 'datafeed'`, you are in the wrong directory or a different project.

Use a **real account ID number** (e.g. `1`), not the literal text `<ID>`.

## Classification rules

| Status           | When we use it |
|------------------|----------------|
| **alive**        | Only if: connect OK, authorized, `get_dialogs(1)` OK, `get_me()` OK, `User.deleted`/`restricted` false, `GetFullUser` OK, `GetAccountTTL` OK, `GetState` OK. `reason_code`: `all_checks_passed`. |
| **deleted**      | `User.deleted=True`, or `UserDeactivatedError`, or `UserDeactivatedBanError`, or RPCError code 401 / message “deactivated”. |
| **banned**       | `UserDeactivatedBanError`. |
| **restricted**   | `User.restricted=True`, or `UserRestrictedError`, or RPCError “restricted”. |
| **auth_required** | No session, not authorized, or `SessionRevokedError` / `AuthKeyUnregisteredError` / `AuthKeyError`. |
| **flood_wait**   | `FloodWaitError`. |
| **error**        | Any other exception (we log exception type as `reason_code`). |

Every result includes a **reason_code** so you can see why we chose that status (e.g. `UserDeactivatedError`, `all_checks_passed`, `RPCError_code_401`).

## How to reproduce a false “alive”

1. Pick an account that you **know** is deleted or unusable (e.g. you deleted it in Telegram, or it shows as deleted in the app).
2. **Go to the project root** (directory with `main.py` and `src/`).
3. Run the check for that account only with **verbose** logging (use the real account ID, e.g. `1`):

   ```bash
   python scripts/check_account_health.py --account-id 1 --verbose
   ```

   Or:

   ```bash
   python -m scripts.check_account_health --account-id 1 --verbose
   ```
3. In the logs (stderr) you’ll see, in order:
   - `alive_check_start` (account_id, phone_masked, checked_at)
   - For each step: `alive_check_step` with `step` = `connect`, `is_user_authorized`, `get_dialogs(limit=1)`, `get_me`, `GetFullUserRequest`, `GetAccountTTLRequest`, `GetStateRequest`, and `result=ok` or an exception.
   - If an exception occurs: `alive_check_exception` with `exception_type`, `message`, and optionally `code`.
4. If the account is reported **alive** but should be dead, the logs show which steps succeeded. That tells us which call is not failing for that account (e.g. cached `get_me`, or a call we don’t do yet).

## How to verify the fix

1. Run the check again on the same account (and optionally others):
   ```bash
   python -m scripts.check_account_health --account-id YOUR_ACCOUNT_ID --verbose
   ```
2. Confirm the account is now **deleted** (or **error** with a clear reason_code), not **alive**.
3. In the dashboard, run “Check if accounts are alive” and confirm the same account shows as deleted with a reason (e.g. `UserDeactivatedError` or `RPCError_code_401`).

## Running on the VPS

1. **SSH into the server** and **cd to the autostory project directory** (where your Autostory `main.py` and `src/` live). If you see errors like `No module named 'datafeed'`, that usually means you are in another project’s directory (e.g. AIdrugbot); switch to the Autostory deploy folder.

2. From the project root:
   ```bash
   python scripts/check_account_health.py --account-id 1 --verbose
   ```
   Use your real account ID number instead of `1`.

3. Logs go to stderr; the summary (status, reason_code, message per account) goes to stdout.

4. To capture logs to a file:
   ```bash
   python scripts/check_account_health.py --account-id 1 --verbose 2> /tmp/health_check.log
   cat /tmp/health_check.log
   ```

## Code locations

- **Checker**: `src/clients/manager.py` → `check_accounts_health(update_status, account_ids, verbose)`.
- **API**: `src/dashboard/routes.py` → `POST /api/accounts/check` (body: `update_status`, `account_ids`, `verbose`).
- **Dashboard UI**: `src/dashboard/templates/accounts.html` → “Check if accounts are alive” button and results table (status, reason, message, checked_at).
- **CLI**: `main.py` → `check-accounts` (optional `--account-id`, `--verbose`, `--update-status`).
- **Test harness**: `scripts/check_account_health.py` (same options, configures structlog when `--verbose`).
