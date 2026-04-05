#!/usr/bin/env python3
"""
Audit canonical session files vs DB accounts.
Classifies every account: session_ready, missing_canonical_session, needs_reimport,
auth_required, frozen_story, story_rate_limited, restricted, other.
Run from project root or /opt/autostory.

Usage:
  python -m scripts.audit_sessions
  python -m scripts.audit_sessions --db /opt/autostory/data/storyfleet.db --sessions-dir /opt/autostory/data/sessions
  python -m scripts.audit_sessions --json
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Ensure project root in path
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def main():
    parser = argparse.ArgumentParser(description="Audit canonical session files vs DB accounts")
    parser.add_argument("--db", default=None, help="Database path (default: from DATABASE_URL or ./data/storyfleet.db)")
    parser.add_argument("--sessions-dir", default=None, help="Sessions directory (default: from settings)")
    parser.add_argument("--json", action="store_true", help="Output JSON (details per account)")
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db).expanduser().resolve()
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    if args.sessions_dir:
        os.environ["STORAGE_SESSIONS_DIR"] = str(Path(args.sessions_dir).expanduser().resolve())

    from sqlalchemy import text
    from src.core.database import init_db, get_db_context
    from src.core.session_paths import get_canonical_session_path, get_sessions_dir, classify_account_readiness, recommended_action

    init_db()
    sessions_dir = get_sessions_dir()

    with get_db_context() as db:
        rows = db.execute(text("""
            SELECT id, phone_number, status, health_status, session_path, story_status, story_blocked_until
            FROM accounts
            ORDER BY id
        """)).fetchall()

    details = []
    by_classification = {}
    for r in rows:
        a = {
            "id": r[0],
            "phone_number": r[1],
            "status": r[2],
            "health_status": r[3],
            "session_path": r[4],
            "story_status": r[5],
            "story_blocked_until": r[6],
        }
        aid = a["id"]
        canonical = get_canonical_session_path(aid)
        exists = canonical.is_file()

        class _Acc:
            pass

        acc = _Acc()
        acc.id = aid
        acc.session_path = a.get("session_path")
        acc.health_status = a.get("health_status")
        acc.status = type("S", (), {"value": a.get("status")})() if a.get("status") else None
        acc.story_status = a.get("story_status")
        acc.story_blocked_until = a.get("story_blocked_until")

        cl = classify_account_readiness(acc)
        act = recommended_action(cl)
        by_classification[cl] = by_classification.get(cl, 0) + 1
        details.append({
            "id": aid,
            "phone": a.get("phone_number"),
            "health_status": a.get("health_status"),
            "session_path": a.get("session_path"),
            "canonical_exists": exists,
            "story_status": a.get("story_status"),
            "classification": cl,
            "action": act,
        })

    report = {
        "sessions_dir": str(sessions_dir),
        "total_audited": len(rows),
        "by_classification": by_classification,
        "details": details,
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return

    # Human-readable
    print("=" * 80)
    print("Account session audit")
    print("=" * 80)
    print(f"Sessions dir: {sessions_dir}")
    print(f"Total accounts: {len(rows)}")
    print()
    for k, v in sorted(by_classification.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print()
    print(f"{'ID':<5} {'Phone':<16} {'Health':<12} {'Session':<8} {'Story':<12} {'Class':<22} {'Action'}")
    print("-" * 80)
    for d in details:
        sid = "Y" if d["canonical_exists"] else "N"
        print(f"{d['id']:<5} {str(d['phone'] or '-')[:15]:<16} {(d['health_status'] or '-')[:11]:<12} {sid:<8} {(str(d['story_status']) or '-')[:11]:<12} {d['classification']:<22} {d['action']}")
    print("=" * 80)

    # Summary by action
    reimport = [d for d in details if d["action"] == "reimport"]
    wait = [d for d in details if d["action"] == "wait"]
    ok = [d for d in details if d["action"] == "ok"]
    if reimport:
        print(f"\nReimport needed ({len(reimport)}): {[d['id'] for d in reimport]}")
    if wait:
        print(f"Wait ({len(wait)}): {[d['id'] for d in wait]}")
    if ok:
        print(f"OK ({len(ok)}): {[d['id'] for d in ok]}")


if __name__ == "__main__":
    main()
