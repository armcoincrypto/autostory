# Swaperex Administrator Token Incident

- Variable: `ADMIN_API_TOKEN`; header: `X-Admin-Token`.
- Server consumer: isolated Swaperex admin FastAPI app.
- Client consumers: operator SPA session/manual token entry; no build-time token found.
- Before rotation: token was inline in a root-owned but mode-`0644` systemd drop-in.
- Exact fingerprint scan: one file match; zero matches in 4,133 Swaperex Git blobs, deployed
  frontend assets, searched logs, shell history, or local transcript artifacts.
- Severity: exposed to local readers and Phase 0.5 operator output; treated as compromised.

Containment:

1. Replacement generated with a cryptographically secure generator.
2. Stored at `/etc/swaperex/swaperex-admin.env`, root-owned mode `0600`; parent mode `0700`.
3. Drop-in changed to `EnvironmentFile`; inline value removed.
4. Only `swaperex-admin.service` restarted.
5. New token returned 200; old and missing tokens returned 401.
6. Neither old nor new token appeared in the service journal.

The old drop-in is retained only in the restricted evidence directory. Restoring it would
reactivate a compromised token and is emergency-only, not a normal rollback.
