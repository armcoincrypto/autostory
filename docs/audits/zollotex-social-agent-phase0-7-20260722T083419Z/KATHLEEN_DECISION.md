# Kathleen Decision

```text
BLOCKED_OWNER_SOURCE_REQUIRED
```

## Evidence

- No authoritative `.py` lineage in Git history/tags for `src/bot/kathleen/` package.
- Production tree contains bytecode loaders (`_pyc_loader` + `_bytecode_archive`) and a listener marked reconstructed from bytecode.
- Recovery lab holds decompiled `.pycdc.py` / `.decompiled.py` candidates only — rejected as authoritative source (Phase 0.6 + this phase; no decompile promotion).
- Emergency backup trees contain `.pyc` only for Kathleen.

## Runtime policy

- `kathleen-account-listener.service` remains inactive.
- Candidate does not ship Kathleen package.
- Dexpert audit registers safe fallback HTML when Kathleen is absent.
- Web/scheduler/readiness promotion is not blocked by Kathleen absence (no hard import in those entry points).
