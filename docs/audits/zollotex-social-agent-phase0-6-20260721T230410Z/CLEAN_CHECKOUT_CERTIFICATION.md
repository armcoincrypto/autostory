# Clean Checkout Certification

- Certified source commit: `e5c6b332e72ef07b3bddedb4dada4f26055b8579`
- Fresh checkout: `/opt/autostory-phase0-6-certification`
- Dependencies: fresh Python 3.12 virtualenv installed from committed `requirements.txt`
- Compilation: pass for tracked Python; shell syntax pass
- Collection: 683 tests collected, zero collection errors
- Focused security/lineage: 112 passed, 1 skipped
- Full regression: 56 failed, 609 passed, 18 skipped
- Isolated startup: pass on `127.0.0.1:18080` with temporary SQLite and all mutation flags false
- Public health: 200 with minimal `{"status":"healthy"}`
- Clean shutdown: pass
- Secret scan: 469 tracked files, zero findings
- Worktree clean: yes
- Runtime source from Git: yes for the candidate; virtualenv reproduced from manifest

```text
CLEAN_CHECKOUT_IMPORT=PASS
TEST_COLLECTION=PASS
FULL_REGRESSION=56_FAILED_609_PASSED_18_SKIPPED
ISOLATED_STARTUP=PASS
WORKTREE_CLEAN=YES
RUNTIME_FILES_FROM_GIT=YES
```

Source collection lineage is restored, but immutable source lineage and runtime reproducibility
gates do not pass because the functional regression is red and active scheduler/readiness
processes still load the dirty production tree.
