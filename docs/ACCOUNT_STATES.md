# Account States (Operator Reference)

This document clarifies the distinction between **general health**, **session readiness**, and **story readiness**. These are independent; do not assume `alive` = story-ready.

---

## General Healthy

- **Meaning:** The account passes a basic Telegram API healthcheck (connect, is_user_authorized, get_me).
- **Indicators:** `health_status=alive`
- **Does NOT imply:** Session file exists, story-capable, or usable for publishing.
- **Note:** Old/stale DB rows can show `alive` even when they have no session file; such accounts need re-import.

---

## Session Ready

- **Meaning:** A valid Telethon session file exists on disk and can be used to create a client.
- **Indicators:** `account_<id>.session` file exists in `/opt/autostory/data/sessions/`, or `session_path` points to an existing file.
- **UI:** Badge "Canonical OK" in the Session column.
- **Required for:** Any Telegram client operations (stories, scheduler, discovery).

---

## Story Ready

- **Meaning:** Account is eligible for story batch publishing.
- **Requires ALL of:**
  1. `status=active`
  2. `health_status` in (alive, null)
  3. Session file exists on disk (session-ready)
  4. `story_status` in (ok, unknown) — not frozen, not restricted
  5. `story_blocked_until` is null or expired
  6. purpose allows autostory
- **UI:** Badge "Story Ready" in summary; "OK" in Story column when eligible.
- **Note:** Story Ready ⊂ Session Ready ⊂ General Healthy (when healthy). An account can be alive but not session-ready; it can be session-ready but not story-ready (e.g. frozen).

---

## Needs Re-import

- **Meaning:** No session file on disk; account cannot be used until re-imported.
- **Indicators:** `session_path` empty and no `account_<id>.session` file.
- **UI:** Badge "Needs re-import" in Session column.
- **Action:** Re-import via tdata (zip or session string) from the Add Account modal.

---

## Summary

| State           | health_status | Session file | story_status | Can publish stories? |
|----------------|---------------|--------------|--------------|----------------------|
| General healthy| alive         | maybe not    | any          | No                   |
| Session ready  | any           | yes          | any          | Maybe (if other OK)   |
| Story ready    | alive/null    | yes          | ok/unknown   | Yes                  |
| Needs re-import| any           | no           | any          | No                   |
