#!/usr/bin/env python3
"""Operator script: Facebook controlled publishing canary (Exswaping Page only).

Does not enable Instagram or general live publishing.
Never prints access tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load env files without printing secrets
for env_path in ("/etc/autostory/social-agent-meta.env", "/opt/autostory/.env"):
    p = Path(env_path)
    if not p.exists():
        continue
    for line in p.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Prefer release on PYTHONPATH when provided
if len(sys.argv) > 1 and sys.argv[1].startswith("--release="):
    rel = sys.argv[1].split("=", 1)[1]
    sys.path.insert(0, rel)

from src.core.database import get_db_context  # noqa: E402
from src.social_agent.publishing.execution import CANARY_FACEBOOK_PAGE_ID, CANARY_MESSAGE  # noqa: E402
from src.social_agent.publishing import publish_service as canary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", default="canary-operator")
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--execute", action="store_true", help="Mint+execute after preflight (requires pages_manage_posts)")
    args, _ = parser.parse_known_args()

    evidence = {}
    with get_db_context() as db:
        dry = canary.prepare_facebook_canary_dry_run(db, actor=args.actor, workspace_id=args.workspace)
        evidence["dry_run"] = {
            "ok": dry.get("ok"),
            "dry_run_id": dry.get("dry_run_id"),
            "payload_hash": dry.get("payload_hash"),
            "provider_http_posts": dry.get("provider_http_posts"),
            "message": CANARY_MESSAGE,
            "page_id": CANARY_FACEBOOK_PAGE_ID,
        }
        pre = canary.preflight_facebook_canary(
            db,
            actor=args.actor,
            workspace_id=args.workspace,
            dry_run_id=int(dry["dry_run_id"]),
            payload_hash=str(dry["payload_hash"]),
        )
        evidence["preflight"] = {
            "ok": pre.get("ok"),
            "permission_reconnect_required": pre.get("permission_reconnect_required"),
            "failed_checks": [c for c in (pre.get("checks") or []) if not c.get("ok")],
            "provider_http_posts": pre.get("provider_http_posts"),
        }
        if not args.execute:
            db.commit()
            print(json.dumps(evidence, indent=2))
            return 0 if pre.get("ok") else 2

        if not pre.get("ok"):
            db.commit()
            print(json.dumps(evidence, indent=2))
            return 3

        minted = canary.approve_and_mint_canary(
            db,
            actor=args.actor,
            workspace_id=args.workspace,
            dry_run_id=int(dry["dry_run_id"]),
            payload_hash=str(dry["payload_hash"]),
            page_id=CANARY_FACEBOOK_PAGE_ID,
            explicit_approval="CONFIRM",
        )
        evidence["authorization"] = {
            "ok": minted.get("ok"),
            "authorization_id": minted.get("authorization_id"),
            "idempotency_key": minted.get("idempotency_key"),
            "expires_at": minted.get("expires_at"),
            # secret intentionally omitted from evidence JSON written to disk by default
        }
        secret = minted.get("authorization_secret")
        executed = canary.execute_facebook_canary(
            db,
            actor=args.actor,
            workspace_id=args.workspace,
            authorization_id=int(minted["authorization_id"]),
            authorization_secret=str(secret),
            dry_run_id=int(dry["dry_run_id"]),
            payload_hash=str(dry["payload_hash"]),
            page_id=CANARY_FACEBOOK_PAGE_ID,
            idempotency_key=str(minted["idempotency_key"]),
        )
        evidence["execute"] = {k: executed.get(k) for k in executed if k != "public_url"}
        evidence["execute"]["public_url_present"] = bool(executed.get("public_url"))
        evidence["timestamp_utc"] = datetime.now(timezone.utc).isoformat()

        # Duplicate proofs
        reuse = canary.execute_facebook_canary(
            db,
            actor=args.actor,
            workspace_id=args.workspace,
            authorization_id=int(minted["authorization_id"]),
            authorization_secret=str(secret),
            dry_run_id=int(dry["dry_run_id"]),
            payload_hash=str(dry["payload_hash"]),
            page_id=CANARY_FACEBOOK_PAGE_ID,
            idempotency_key=str(minted["idempotency_key"]) + "-reuse",
        )
        evidence["reuse_denied"] = {"ok": reuse.get("ok"), "error": reuse.get("error"), "provider_http_posts": reuse.get("provider_http_posts")}
        dup = canary.execute_facebook_canary(
            db,
            actor=args.actor,
            workspace_id=args.workspace,
            authorization_id=int(minted["authorization_id"]),
            authorization_secret=str(secret),
            dry_run_id=int(dry["dry_run_id"]),
            payload_hash=str(dry["payload_hash"]),
            page_id=CANARY_FACEBOOK_PAGE_ID,
            idempotency_key=str(minted["idempotency_key"]),
        )
        evidence["duplicate_idempotency_denied"] = {
            "ok": dup.get("ok"),
            "error": dup.get("error"),
            "provider_http_posts": dup.get("provider_http_posts"),
        }
        db.commit()

    if args.evidence:
        Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    return 0 if evidence.get("execute", {}).get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
