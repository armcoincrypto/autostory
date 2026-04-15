"""
Story Rotation Engine
=====================
Picks the next eligible account from a pool and publishes one story.
Designed to be driven by the scheduler worker (async context).

Key design decisions:
- Account eligibility is checked fresh on every tick (health, precheck, cooldown).
- Round-robin ordering: account with the oldest last_active publishes next.
- Continuous runs reschedule themselves via next_tick_at after each step.
- Once-mode runs complete when stories_ok + stories_failed >= max_stories (or pool exhausted).
- All DB mutations happen inside get_db_context so they're atomic.
"""
import asyncio
import sys
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

import structlog

sys.path.insert(0, '/home/user/autostory')
from src.core.models import (
    Account, AccountStatus, DiscoveredUser,
    StoryPool, StoryPoolMember, StoryRun, StoryRunStep,
)
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)

# How long to wait before reusing the same account (seconds).
# Telegram allows ~1 story/hour per account safely.
ACCOUNT_STORY_COOLDOWN_SEC = 3600


class StoryRotationEngine:
    """
    Drives story rotation runs.  Call tick() from the scheduler worker loop.
    """

    # ── Account eligibility ────────────────────────────────────────────────

    def _is_eligible(self, account: Account) -> Tuple[bool, str]:
        """
        Return (eligible, reason).
        Reason is 'ok' on success or a short code on failure.
        """
        st = (account.status.value if hasattr(account.status, 'value')
              else (account.status or '')).lower()
        hs = (account.health_status or '').strip().lower()

        if st == 'banned':
            return False, 'banned'
        if st == 'flood_wait':
            if account.flood_wait_until and datetime.utcnow() < account.flood_wait_until:
                return False, 'flood_wait'
        if st in ('auth_required', 'inactive'):
            return False, st
        if hs in ('banned', 'deleted', 'frozen', 'restricted'):
            return False, f'health:{hs}'
        if (account.story_precheck_status or '').lower() != 'allowed':
            return False, 'story_not_ready'

        # Per-account cooldown (don't spam one account)
        if account.last_active:
            elapsed = (datetime.utcnow() - account.last_active).total_seconds()
            if elapsed < ACCOUNT_STORY_COOLDOWN_SEC:
                remaining = int(ACCOUNT_STORY_COOLDOWN_SEC - elapsed)
                return False, f'cooldown:{remaining}s'

        return True, 'ok'

    # ── Account selection ──────────────────────────────────────────────────

    def _pick_account(self, pool_id: Optional[int], db,
                      purpose_filter: Optional[str] = None) -> Optional[Account]:
        """
        Round-robin: pick the eligible account with the oldest last_active.
        Falls back to all story-ready active accounts when pool_id is None.
        purpose_filter: 'autostory' | 'both' | None (None = exclude messaging-only)
        """
        if pool_id:
            members = db.query(StoryPoolMember).filter(
                StoryPoolMember.pool_id == pool_id,
                StoryPoolMember.is_enabled == True,
            ).all()
            account_ids = [m.account_id for m in members]
            if not account_ids:
                return None
            candidates = db.query(Account).filter(
                Account.id.in_(account_ids)
            ).order_by(Account.last_active.asc().nullsfirst()).all()
        else:
            from sqlalchemy import or_
            q = db.query(Account).filter(
                Account.status == AccountStatus.ACTIVE,
                Account.story_precheck_status == 'allowed',
            )
            if purpose_filter in ('autostory', 'both'):
                # exact match
                q = q.filter(Account.purpose == purpose_filter)
            else:
                # default: exclude messaging-only accounts
                q = q.filter(
                    or_(
                        Account.purpose.in_(['autostory', 'both']),
                        Account.purpose.is_(None),
                    )
                )
            candidates = q.order_by(Account.last_active.asc().nullsfirst()).all()

        for acc in candidates:
            eligible, reason = self._is_eligible(acc)
            if eligible:
                return acc
            logger.debug('Account skipped in rotation',
                         account_id=acc.id, reason=reason)
        return None

    # ── Mention selection ──────────────────────────────────────────────────

    def _pick_mention_users(
        self, count: int, db, source_chat_id: Optional[int] = None
    ) -> List[int]:
        """
        FIFO: oldest-discovered, never-mentioned, not blocked, has username.
        When source_chat_id is set, only users discovered from that Telegram group.
        """
        q = db.query(DiscoveredUser).filter(
            DiscoveredUser.is_blocked == False,
            DiscoveredUser.times_mentioned == 0,
            DiscoveredUser.username.isnot(None),
        )
        if source_chat_id is not None:
            q = q.filter(DiscoveredUser.source_chat_id == source_chat_id)
        users = q.order_by(DiscoveredUser.discovered_at.asc()).limit(count).all()
        return [u.user_id for u in users]

    # ── Single rotation step ───────────────────────────────────────────────

    async def run_step(self, run_id: int) -> Dict[str, Any]:
        """
        Execute one rotation step for a given run:
        1. Mark run as running (if still pending).
        2. Pick next eligible account.
        3. Publish a story.
        4. Record the StoryRunStep.
        5. Update run counters + next_tick_at.
        """
        from src.stories.publisher import story_publisher
        from src.clients.manager import client_manager

        # ── Read run config ────────────────────────────────────────────────
        with get_db_context() as db:
            run = db.get(StoryRun, run_id)
            if not run:
                return {'success': False, 'error': f'run {run_id} not found'}
            if run.status not in ('pending', 'running'):
                return {'success': False, 'error': f'run {run_id} is {run.status}'}

            if run.status == 'pending':
                run.status = 'running'
                run.started_at = datetime.utcnow()

            pool_id = run.pool_id
            purpose_filter = getattr(run, 'purpose_filter', None)
            caption = run.caption
            media_path = run.media_path
            mentions_per_story = run.mentions_per_story or 5
            mode = run.mode
            interval_minutes = run.interval_minutes or 60
            max_stories = run.max_stories
            mention_source_chat_id = getattr(run, 'mention_source_chat_id', None)

        if not media_path:
            await self._record_step(run_id, None, 'failed', 'no_media_path', mode, interval_minutes)
            return {'success': False, 'error': 'no_media_path'}

        # ── Pick account ───────────────────────────────────────────────────
        with get_db_context() as db:
            account = self._pick_account(pool_id, db, purpose_filter=purpose_filter)
            if not account:
                logger.warning('No eligible account for rotation', run_id=run_id)
                # Reschedule but don't count as failure
                with get_db_context() as db2:
                    run2 = db2.get(StoryRun, run_id)
                    if run2 and run2.status == 'running':
                        run2.last_tick_at = datetime.utcnow()
                        if mode == 'continuous':
                            run2.next_tick_at = datetime.utcnow() + timedelta(minutes=interval_minutes)
                return {'success': False, 'error': 'no_eligible_account'}
            account_id = account.id
            mention_ids = self._pick_mention_users(
                mentions_per_story, db, mention_source_chat_id
            )

        # ── Get or reconnect client ────────────────────────────────────────
        client_wrapper = await client_manager.get_client(account_id)
        if not client_wrapper:
            try:
                await client_manager.connect_account(account_id)
                client_wrapper = await client_manager.get_client(account_id)
            except Exception as e:
                logger.warning('Could not connect account', account_id=account_id, error=str(e))

        if not client_wrapper:
            await self._record_step(run_id, account_id, 'failed', 'client_unavailable',
                                    mode, interval_minutes, count_failure=True)
            return {'success': False, 'error': 'client_unavailable', 'account_id': account_id}

        # ── Publish ────────────────────────────────────────────────────────
        result = await story_publisher.publish_story(
            client_wrapper=client_wrapper,
            media_path=media_path,
            caption=caption,
            mentions=mention_ids,
        )

        # ── Record step + update run ───────────────────────────────────────
        step_status = 'ok' if result['success'] else 'failed'
        step_error = result.get('error') if not result['success'] else None
        db_id = result.get('db_id')

        with get_db_context() as db:
            step = StoryRunStep(
                run_id=run_id,
                account_id=account_id,
                story_id=db_id,
                status=step_status,
                error=step_error,
                executed_at=datetime.utcnow(),
            )
            db.add(step)

            run = db.get(StoryRun, run_id)
            if run:
                if result['success']:
                    run.stories_ok += 1
                else:
                    run.stories_failed += 1
                run.last_tick_at = datetime.utcnow()

                if mode == 'continuous':
                    run.next_tick_at = datetime.utcnow() + timedelta(minutes=interval_minutes)
                else:
                    # once-mode: complete when we've hit max_stories
                    total = run.stories_ok + run.stories_failed
                    if max_stories and total >= max_stories:
                        run.status = 'completed'
                        run.completed_at = datetime.utcnow()

        logger.info(
            'Rotation step complete',
            run_id=run_id,
            account_id=account_id,
            success=result['success'],
        )
        return {**result, 'account_id': account_id}

    async def _record_step(self, run_id, account_id, status, error,
                           mode, interval_minutes, count_failure=False):
        """Helper: write a StoryRunStep and reschedule the run."""
        with get_db_context() as db:
            if account_id:
                step = StoryRunStep(
                    run_id=run_id,
                    account_id=account_id,
                    status=status,
                    error=error,
                    executed_at=datetime.utcnow(),
                )
                db.add(step)
            run = db.get(StoryRun, run_id)
            if run:
                if count_failure:
                    run.stories_failed += 1
                run.last_tick_at = datetime.utcnow()
                if mode == 'continuous':
                    run.next_tick_at = datetime.utcnow() + timedelta(minutes=interval_minutes)

    # ── Worker tick ────────────────────────────────────────────────────────

    async def tick(self) -> None:
        """
        Called by the scheduler worker every loop iteration.
        Finds all runs that are due and executes one step for each.
        """
        now = datetime.utcnow()
        with get_db_context() as db:
            runs = db.query(StoryRun).filter(
                StoryRun.status.in_(['pending', 'running'])
            ).all()

            due = []
            for run in runs:
                if run.status == 'pending':
                    due.append(run.id)
                elif run.status == 'running' and run.mode == 'continuous':
                    if run.next_tick_at is None or now >= run.next_tick_at:
                        due.append(run.id)

        for run_id in due:
            try:
                await self.run_step(run_id)
            except Exception as e:
                logger.error('Rotation tick error', run_id=run_id, error=str(e))
            # Small gap between runs to avoid hammering Telegram
            await asyncio.sleep(3)


story_rotation_engine = StoryRotationEngine()
