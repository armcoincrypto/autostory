# Git Secret Removal Plan

History rewriting is **not authorized** and was not performed.

## Affected objects

Three environment backups were introduced together in commit `99b04be8152419e24bfbea4e570f584f95a2e6c7`. They are reachable from the remote production branch, local development/Phase branches, ten AI-build branches, and tag `p14-2-auto-coding-ready-20260609`.

## Reviewed sequence

1. Preserve restricted metadata evidence and repository object IDs; do not create another plaintext copy.
2. Complete the credential consumer map and rotate every definitely/potentially exposed active credential.
3. Add ignore/secret-scan controls and remove the three files from the current canonical branch through a reviewed commit.
4. Notify all collaborators that historical clones remain contaminated.
5. Inventory protected branches, open PRs, worktrees, tags, mirrors, CI caches, deployment caches, and archived release bundles.
6. Decide explicitly between:
   - retaining immutable history after rotation, with the incident documented; or
   - coordinated `git filter-repo` rewriting across all affected refs.
7. If rewriting is approved, freeze pushes, capture ref mappings, rewrite all affected branches/tags, run a bare-repository secret scan, force-push non-protected refs under owner control, recreate tags as approved, and invalidate stale PR branches.
8. Require collaborators to reclone or purge old objects; normal pull is insufficient.
9. Expire remote/server reflogs and garbage-collect only under repository-owner policy.
10. Remove contaminated build/deployment caches and archived releases after retention approval.
11. Re-scan the remote default branch, all protected refs, release artifacts, and a fresh clone.

## Force-push implications

A history rewrite changes every descendant commit, invalidates signatures and review links, disrupts open worktrees/PRs, and requires coordinated force pushes. It must not be attempted by this remediation branch. Credential rotation is required regardless because Git object deletion cannot prove all clones were purged.

## Active branch action

The Phase 0.5 branch adds ignore rules for `.env.*` backups while retaining approved examples. It does not delete the tracked backups because evidence preservation, credential rotation, and canonical-branch ownership are unresolved.
