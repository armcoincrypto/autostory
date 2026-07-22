# Key Rotation Design

1. Generate new key id (e.g. `v2`) into key ring file; keep `v1`.
2. Set active key to `v2`; restart services in `transition` mode.
3. Run `migrate_telegram_session_encryption.py reencrypt --target-key-id v2` on authorized target.
4. Verify decrypt for all rows.
5. Observe; only then remove `v1` from ring after retention window.

Safe diagnostics only: `active_key_id`, `configured_key_ids`, `key_material_present`.
