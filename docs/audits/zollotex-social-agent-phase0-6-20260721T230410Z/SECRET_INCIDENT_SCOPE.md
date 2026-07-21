# Secret Incident Scope

Phase 0.5 identified 22 real secret-bearing artifacts and three Git-tracked backups. Phase 0.6
confirmed the active dashboard operator token in 17 local files and two historical Git blobs.
The Swaperex admin token was local/operator-output exposed but not found in scanned Git, frontend,
or logs. Secret values are retained only in active protected locations and restricted evidence.

Active-branch containment removes all three tracked backups. Historical objects remain reachable
until an explicitly approved history response. Untracked environment backups remain mode `0600`
and are not deleted because they are forensic/local artifacts requiring retention-owner approval.
