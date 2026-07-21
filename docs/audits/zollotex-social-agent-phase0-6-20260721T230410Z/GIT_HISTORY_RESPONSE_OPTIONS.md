# Git History Response Options

## Option A — no rewrite

Rotate every exposed credential, remove active-branch files, retain historical objects. Lowest
coordination risk; historical material remains reachable.

## Option B — targeted rewrite (recommended after rotations)

Use `git filter-repo` to remove the three exact paths from all approved refs; preserve server-side
backups; coordinate collaborators; force-push branches/tags; require fresh clones; verify remote
object reachability and secret fingerprints afterward. Old clones remain incident artifacts.

## Option C — repository replacement

Use only if later scanning proves exposure is too broad for a targeted rewrite.

Recommendation: Option B after all active credentials are rotated and owners approve a coordinated
window. No history rewrite, force push, tag change, or object deletion occurred in Phase 0.6.
