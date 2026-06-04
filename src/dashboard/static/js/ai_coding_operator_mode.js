/**
 * Operator Mode — production readiness summaries for AI Coding.
 */
(function (global) {
  'use strict';

  const PRODUCTION_FIELDS = [
    'status_label',
    'current_step',
    'primary_action_label',
    'needs_operator_input',
    'manual_test_required',
    'friendly_message',
    'safe_to_continue',
    'production_status_label',
    'completed_summary',
    'current_work_summary',
    'next_step_summary',
    'why_waiting_summary',
    'next_after_click_summary',
    'production_release_summary',
    'production_readiness_percent',
    'production_checklist',
    'manual_test_checklist',
    'missing_evidence_reason',
    'stop_type',
    'what_ai_needs',
    'operator_status',
    'progress_stage',
    'progress_percent',
    'last_action',
    'operator_next_action',
    'blocker_reason',
    'primary_action',
    'can_continue',
    'show_manual_test_package',
    'show_release_action',
    'show_advanced_details',
  ];

  const PROGRESS_BY_STATUS = {
    draft: 8,
    planning: 8,
    plan_ready: 20,
    executing: 35,
    reviewing: 65,
    validating: 70,
    fixing: 42,
    ready_for_manual_test: 80,
    operator_approved: 90,
    ready_for_release_approval: 92,
    release_approved: 92,
    releasing: 95,
    released: 100,
    operator_declined: 42,
    failed: 0,
    cancelled: 0,
  };

  const TECHNICAL_REPLACEMENTS = [
    [/Next action:\s*/gi, ''],
    [/Approve execution review/gi, 'Continue AI work'],
    [/Execution review pending/gi, ''],
    [/approve review on Executions page/gi, ''],
    [/Approve review on Executions page/gi, ''],
    [/review evidence/gi, ''],
    [/validation evidence/gi, ''],
    [/review gate/gi, ''],
    [/validation gate/gi, ''],
    [/confirmation required/gi, ''],
    [/review pending/gi, 'AI is checking the work'],
    [/validation pending/gi, 'AI is testing the result'],
    [/review_pending/gi, ''],
    [/validation_pending/gi, ''],
    [/operator_approval/gi, ''],
    [/AI Workflows/gi, ''],
    [/Task Planner/gi, ''],
    [/Executions page/gi, ''],
    [/Confirm validation/gi, 'Continue AI work'],
    [/Advance build/gi, 'Continue AI work'],
    [/Send notes to AI/gi, 'Answer question'],
    [/Needs your feedback/gi, 'Blocked'],
    [/AI needs clarification/gi, 'Blocked'],
    [/test proof required/gi, 'Preparing manual test package'],
    [/test proof/gi, 'Preparing manual test package'],
    [/AI validating/gi, 'AI Working'],
    [/AI fixing/gi, 'AI Working'],
    [/Planning/gi, 'AI Working'],
    [/AI is working/gi, 'AI Working'],
    [/\s{2,}/g, ' '],
  ];

  function esc(v) {
    return String(v ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;');
  }

  function sanitizeOperatorText(value) {
    let text = String(value ?? '').trim();
    if (!text) return '';
    TECHNICAL_REPLACEMENTS.forEach(([pattern, replacement]) => {
      text = text.replace(pattern, replacement);
    });
    return text.trim();
  }

  function mergeOperatorContinue(run, resp) {
    if (!resp || !resp.run) return run;
    const merged = Object.assign({}, resp.run);
    PRODUCTION_FIELDS.forEach((key) => {
      if (resp[key] !== undefined && resp[key] !== null) merged[key] = resp[key];
    });
    return merged;
  }

  function friendlyOperatorError(err) {
    if (!err) return 'The service is unavailable. Try again.';
    const status = err.status;
    if (status === 502 || status === 503 || status === 504) {
      return 'The service is unavailable. Try again.';
    }
    const raw = sanitizeOperatorText(typeof err === 'string' ? err : err.message || '');
    if (/validation_passed|validation.*required|review_pending|validation_pending/i.test(raw)) {
      return 'AI could not continue safely. Open details or send feedback.';
    }
    if (/confirmation|test proof|pending/i.test(raw.toLowerCase())) {
      return 'AI is working on the next safe step.';
    }
    if (/[\[{]|stack|trace|json|detail:/i.test(raw) || raw.length > 140) {
      return 'AI could not continue safely. Open details or send feedback.';
    }
    if (/forbidden|blocked|terminal|maximum.*attempt/i.test(raw.toLowerCase())) {
      return 'AI could not continue safely. Open details or send feedback.';
    }
    if (raw) return raw;
    return 'AI could not continue safely. Open details or send feedback.';
  }

  function field(run, key, fallback) {
    const raw = run[key];
    if (raw != null && String(raw).trim()) {
      return sanitizeOperatorText(raw);
    }
    return fallback;
  }

  function productionStatus(run) {
    return field(run, 'operator_status', field(run, 'production_status_label', 'AI Working'));
  }

  function progressStage(run) {
    return field(run, 'progress_stage', '');
  }

  function progressPercent(run) {
    if (run.progress_percent != null && !Number.isNaN(Number(run.progress_percent))) {
      return Math.max(0, Math.min(100, Number(run.progress_percent)));
    }
    if (run.production_readiness_percent != null && !Number.isNaN(Number(run.production_readiness_percent))) {
      return Math.max(0, Math.min(100, Number(run.production_readiness_percent)));
    }
    return computeProgressFallback(run);
  }

  function computeProgressFallback(run) {
    let pct = PROGRESS_BY_STATUS[run.status] ?? 40;
    const report = run.manual_test_report || {};
    if (report.completed_phases != null && report.total_phases) {
      const total = Number(report.total_phases) || 1;
      const done = Number(report.completed_phases) || 0;
      if (run.status === 'executing' || run.status === 'fixing') {
        pct = Math.min(85, 30 + Math.round((done / total) * 50));
      }
    }
    return Math.max(0, Math.min(100, pct));
  }

  function operatorStatusTone(label) {
    const normalized = sanitizeOperatorText(label);
    if (/planning/i.test(normalized)) return 'planning';
    if (/fixing/i.test(normalized)) return 'fixing';
    if (/validating/i.test(normalized)) return 'validating';
    if (/ready for manual test/i.test(normalized)) return 'ready';
    if (/approved/i.test(normalized) && !/release/i.test(normalized)) return 'success';
    if (/ready for release/i.test(normalized)) return 'release';
    if (/released/i.test(normalized)) return 'success';
    if (/blocked/i.test(normalized)) return 'blocked';
    return 'working';
  }

  function renderOperatorStatusBadge(run) {
    const label = productionStatus(run);
    const tone = operatorStatusTone(label);
    return `<div class="op-status-badge op-status-${tone}">${esc(label)}</div>`;
  }

  function showBlockedCard(run) {
    const stopType = run?.stop_type || null;
    const status = String(run?.status || '').toLowerCase();
    if (stopType === 'STOP_TYPE_MANUAL_TEST' || status === 'ready_for_manual_test') return false;
    if (stopType === 'STOP_TYPE_RELEASE') return false;
    if (stopType === 'STOP_TYPE_REQUIREMENTS') return true;
    if (TERMINAL_FAILURE.has(status)) return true;
    return run?.safe_to_continue === false && !!stopType;
  }

  function renderBlockedCard(run, action) {
    if (!showBlockedCard(run)) return '';
    const message = sanitizeOperatorText(run.blocker_reason)
      || action?.blockedReason
      || sanitizeOperatorText(run.what_ai_needs)
      || sanitizeOperatorText(run.friendly_message)
      || blockedContinueReason(run);
    if (!message) return '';
    return `<div class="op-blocked-card">
      <div class="op-blocked-title"><i class="bi bi-exclamation-octagon"></i> Blocked — your action needed</div>
      <p class="op-blocked-body mb-0">${esc(message)}</p>
    </div>`;
  }

  function renderCurrentStepCard(run) {
    const step = field(run, 'current_step', currentWorkSummary(run));
    const stage = progressStage(run);
    const next = field(run, 'operator_next_action', field(run, 'next_after_click_summary', nextAfterClick(run)));
    return `<div class="op-step-card">
      <div class="op-step-kicker">${stage ? esc(stage) : 'Current step'}</div>
      <div class="op-step-body">${esc(step)}</div>
      <div class="op-step-next"><span class="op-step-next-label">Next</span> ${esc(next)}</div>
    </div>`;
  }

  function renderProgressBarBlock(run, pct) {
    const stage = progressStage(run);
    const label = stage || 'Build progress';
    return `<div class="op-progress-wrap">
      <div class="d-flex justify-content-between align-items-center mb-1 gap-2 flex-wrap">
        <span class="op-progress-label">${esc(label)}</span>
        <span class="op-progress-value">${pct}%</span>
      </div>
      <div class="op-mode-progress"><div class="bar op-progress-bar-${operatorStatusTone(productionStatus(run))}" style="width:${pct}%"></div></div>
    </div>`;
  }

  function renderCurrentBuildCard(run, action) {
    ensureOperatorStyles();
    const pct = progressPercent(run);
    return `<div class="op-current-build-card" data-current-build="true">
      <div class="op-current-build-title">${esc(run.task_title || 'Build task')}</div>
      ${renderOperatorStatusBadge(run)}
      ${renderProgressBarBlock(run, pct)}
      ${renderCurrentStepCard(run)}
      ${renderBlockedCard(run, action)}
    </div>`;
  }

  function completedSummary(run) {
    return field(run, 'completed_summary', 'Work has not started yet');
  }

  function currentWorkSummary(run) {
    return field(run, 'current_work_summary', 'AI is working on the next build step.');
  }

  function whyWaiting(run) {
    return field(run, 'why_waiting_summary', 'Not waiting');
  }

  function nextAfterClick(run) {
    return field(run, 'next_after_click_summary', 'AI will run the next safe build step.');
  }

  function productionRelease(run) {
    return field(run, 'production_release_summary', 'Not released');
  }

  function productionReadinessPercent(run) {
    return progressPercent(run);
  }

  function computeProgress(run) {
    return progressPercent(run);
  }

  function checklistStatusClass(status) {
    const normalized = sanitizeOperatorText(status);
    if (normalized === 'Done') return 'done';
    if (normalized === 'In progress') return 'active';
    if (normalized === 'Ready') return 'ready';
    if (normalized === 'Not released') return 'muted';
    return 'waiting';
  }

  function renderProductionChecklistPanel(run) {
    const items = Array.isArray(run.production_checklist) ? run.production_checklist : [];
    const pct = productionReadinessPercent(run);
    const rows = items.length
      ? items
        .map((item) => {
          const label = sanitizeOperatorText(item.label || item.key || 'Step');
          const status = sanitizeOperatorText(item.status || 'Waiting');
          return `<div class="op-trust-row">
            <span class="op-trust-label">${esc(label)}</span>
            <span class="op-trust-status op-trust-${checklistStatusClass(status)}">${esc(status)}</span>
          </div>`;
        })
        .join('')
      : `<p class="op-mode-muted small mb-0">Checklist will appear as AI work progresses.</p>`;
    return `<div class="op-trust-card">
      <div class="op-readiness-title">Production checklist</div>
      <div class="op-trust-percent mb-2">Checklist status only — build progress uses the bar above.</div>
      <div class="op-trust-bar mb-2"><div class="bar" style="width:${pct}%"></div></div>
      ${rows}
    </div>`;
  }

  const TERMINAL_APPROVED = new Set(['approved', 'released', 'rolled_back']);
  const RELEASE_FLOW_STATUSES = new Set([
    'operator_approved',
    'ready_for_release_approval',
    'release_approved',
    'releasing',
    'released',
    'release_failed',
    'rollback_available',
    'rolled_back',
  ]);
  const TERMINAL_FAILURE = new Set(['failed', 'cancelled']);
  const ACTIVE_WORK_STATUSES = new Set([
    'draft',
    'planning',
    'plan_ready',
    'executing',
    'reviewing',
    'validating',
    'fixing',
    'operator_declined',
  ]);

  function isTerminalApprovedStatus(status) {
    return TERMINAL_APPROVED.has(String(status || '').toLowerCase());
  }

  function isActiveNonTerminalStatus(status) {
    const normalized = String(status || '').toLowerCase();
    if (!normalized || isTerminalApprovedStatus(normalized) || TERMINAL_FAILURE.has(normalized)) {
      return false;
    }
    return ACTIVE_WORK_STATUSES.has(normalized) || normalized === 'ready_for_manual_test';
  }

  function mapPrimaryLabelToAction(label) {
    const normalized = sanitizeOperatorText(label);
    if (!normalized || normalized === 'Done') return null;
    if (/approve after test/i.test(normalized)) {
      return { id: 'approve', label: normalized, tone: 'success' };
    }
    if (/approve production release/i.test(normalized)) {
      return { id: 'release_approve', label: normalized, tone: 'success' };
    }
    if (/answer question/i.test(normalized)) {
      return { id: 'problems', label: normalized, tone: 'warning' };
    }
    if (/send feedback/i.test(normalized)) {
      return { id: 'problems', label: normalized, tone: 'warning' };
    }
    if (/^retry$/i.test(normalized)) {
      return { id: 'retry', label: normalized, tone: 'warning' };
    }
    if (/continue ai work/i.test(normalized)) {
      return { id: 'advance', label: normalized, tone: 'primary' };
    }
    return { id: 'advance', label: normalized, tone: 'primary' };
  }

  function blockedContinueReason(run) {
    const friendly = field(run, 'friendly_message', '');
    if (friendly) return friendly;
    return field(run, 'why_waiting_summary', 'AI cannot continue safely yet.');
  }

  function primaryAction(run) {
    const status = String(run?.status || '').toLowerCase();
    const stopType = run?.stop_type || null;
    const release = run.release_status || null;
    if (RELEASE_FLOW_STATUSES.has(status) || stopType === 'STOP_TYPE_RELEASE') {
      const label = release && release.primary_release_action;
      if (label === 'Prepare release plan') {
        return { id: 'release_prepare', label, tone: 'primary', show: true };
      }
      if (label === 'Approve production release') {
        return { id: 'release_approve', label, tone: 'success', show: true };
      }
      if (label === 'Run approved release') {
        return { id: 'release_run', label, tone: 'primary', show: true };
      }
      if (label === 'Rollback release') {
        return { id: 'release_rollback', label, tone: 'warning', show: true };
      }
      return { id: 'none', label: 'Done', tone: 'success', show: false };
    }
    const apiLabel = sanitizeOperatorText(run?.primary_action_label || '');
    const apiMapped = mapPrimaryLabelToAction(apiLabel);
    const blocked = run?.safe_to_continue === false;
    const needsInput = !!run?.needs_operator_input;

    if (isTerminalApprovedStatus(status)) {
      return { id: 'none', label: apiLabel || 'Done', tone: 'success', show: false };
    }

    if (status === 'ready_for_manual_test' || stopType === 'STOP_TYPE_MANUAL_TEST') {
      const label = (apiMapped && apiMapped.id === 'approve')
        ? apiMapped.label
        : (apiLabel || 'Approve after test');
      return { id: 'approve', label, tone: 'success', show: true };
    }

    if (stopType === 'STOP_TYPE_REQUIREMENTS' || (blocked && stopType !== 'STOP_TYPE_MANUAL_TEST')) {
      return {
        id: 'problems',
        label: apiLabel || 'Answer question',
        tone: 'warning',
        show: true,
        blockedReason: blockedContinueReason(run),
      };
    }

    if (TERMINAL_FAILURE.has(status)) {
      const preferRetry = !blocked && !needsInput && apiMapped && apiMapped.id === 'retry';
      if (preferRetry) {
        return { id: 'retry', label: apiMapped.label, tone: 'warning', show: true };
      }
      const label = (apiMapped && apiMapped.id === 'problems')
        ? apiMapped.label
        : (apiLabel || 'Send feedback');
      return { id: 'problems', label, tone: 'warning', show: true };
    }

    if (blocked || (needsInput && apiMapped && apiMapped.id === 'problems' && stopType)) {
      return {
        id: 'problems',
        label: apiLabel || 'Answer question',
        tone: 'warning',
        show: true,
        blockedReason: blockedContinueReason(run),
      };
    }

    if (apiMapped && apiLabel !== 'Done') {
      return { id: apiMapped.id, label: apiMapped.label, tone: apiMapped.tone, show: true };
    }

    if (isActiveNonTerminalStatus(status) || status) {
      return { id: 'advance', label: 'Continue AI work', tone: 'primary', show: true };
    }

    return { id: 'advance', label: 'Continue AI work', tone: 'primary', show: true };
  }

  function renderReadinessRow(label, value) {
    if (!value) return '';
    return `<div class="op-readiness-row">
      <div class="op-readiness-label">${esc(label)}</div>
      <div class="op-readiness-value">${esc(value)}</div>
    </div>`;
  }

  function renderProductionReadinessCard(run) {
    return renderCurrentBuildCard(run, primaryAction(run));
  }

  function renderProgressBar(pct, run) {
    const r = run || {};
    return renderProgressBarBlock(r, pct);
  }

  function renderChecklistBullets(items) {
    if (!items || !items.length) return '';
    return `<ul class="small mb-0 ps-3">${items.map((item) => `<li>${esc(sanitizeOperatorText(item))}</li>`).join('')}</ul>`;
  }

  function renderChecklistSection(title, items) {
    if (!items || !items.length) return '';
    return `<div class="op-check-section">
      <div class="op-check-heading">${esc(title)}</div>
      <ul class="small mb-0 ps-3">${items.map((item) => `<li>${esc(sanitizeOperatorText(item))}</li>`).join('')}</ul>
    </div>`;
  }

  function normalizeBlockers(run) {
    let blockers = run && run.blockers;
    if (typeof blockers === 'string') {
      try {
        blockers = JSON.parse(blockers);
      } catch (_) {
        blockers = [blockers];
      }
    }
    if (!Array.isArray(blockers)) {
      blockers = blockers ? [blockers] : [];
    }
    return blockers.map((b) => sanitizeOperatorText(b)).filter(Boolean);
  }

  function ensureOperatorStyles() {
    if (document.getElementById('ai-operator-mode-styles')) return;
    const el = document.createElement('style');
    el.id = 'ai-operator-mode-styles';
    el.textContent = `
      .op-mode-panel { background: linear-gradient(135deg, rgba(15,23,42,0.96), rgba(30,41,59,0.85)); border: 1px solid rgba(56,189,248,0.35); border-radius: 16px; padding: 1.25rem 1.35rem; }
      .op-mode-kicker { font-size: 0.68rem; letter-spacing: 0.12em; text-transform: uppercase; color: #7dd3fc; }
      .op-mode-title { font-size: 1.35rem; font-weight: 700; color: #f8fafc; line-height: 1.25; }
      .op-mode-status { font-size: 1rem; font-weight: 600; color: #e2e8f0; }
      .op-mode-muted { color: #94a3b8; font-size: 0.88rem; }
      .op-mode-progress { height: 10px; background: rgba(15,23,42,0.8); border-radius: 999px; overflow: hidden; border: 1px solid rgba(148,163,184,0.2); }
      .op-mode-progress .bar { height: 100%; background: linear-gradient(90deg, #0ea5e9, #22c55e); transition: width 0.35s ease; }
      .op-mode-advanced { margin-top: 1rem; border: 1px solid rgba(148,163,184,0.2); border-radius: 10px; padding: 0.5rem 0.75rem; background: rgba(2,6,23,0.35); }
      .op-mode-advanced summary { cursor: pointer; color: #94a3b8; font-size: 0.82rem; }
      .op-mode-advanced .tech { font-family: ui-monospace, monospace; font-size: 0.72rem; color: #64748b; word-break: break-all; }
      .op-mode-list a { color: #e2e8f0; text-decoration: none; }
      .op-mode-list a:hover { color: #7dd3fc; }
      .op-mode-list .badge { font-size: 0.65rem; }
      .op-readiness-card, .op-checklist-card { background: rgba(2,6,23,0.45); border: 1px solid rgba(148,163,184,0.22); border-radius: 12px; padding: 1rem 1.1rem; margin-bottom: 0.85rem; }
      .op-readiness-title { font-size: 1.05rem; font-weight: 700; color: #e2e8f0; margin-bottom: 0.65rem; }
      .op-readiness-row { display: grid; grid-template-columns: minmax(7.5rem, 34%) 1fr; gap: 0.25rem 0.75rem; padding: 0.35rem 0; border-bottom: 1px solid rgba(148,163,184,0.12); }
      .op-readiness-row:last-child { border-bottom: none; }
      .op-readiness-label { font-size: 0.78rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.04em; }
      .op-readiness-value { font-size: 0.92rem; color: #f1f5f9; line-height: 1.35; }
      .op-check-section { margin-top: 0.65rem; }
      .op-check-heading { font-size: 0.78rem; font-weight: 600; color: #cbd5e1; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 0.25rem; }
      .op-trust-card { background: rgba(2,6,23,0.5); border: 1px solid rgba(56,189,248,0.22); border-radius: 12px; padding: 0.9rem 1rem; margin-bottom: 0.85rem; }
      .op-trust-percent { font-size: 0.9rem; color: #e2e8f0; }
      .op-trust-bar { height: 8px; background: rgba(15,23,42,0.85); border-radius: 999px; overflow: hidden; border: 1px solid rgba(148,163,184,0.18); }
      .op-trust-bar .bar { height: 100%; background: linear-gradient(90deg, #0ea5e9, #22c55e); transition: width 0.35s ease; }
      .op-trust-row { display: flex; justify-content: space-between; gap: 0.75rem; padding: 0.3rem 0; border-bottom: 1px solid rgba(148,163,184,0.1); font-size: 0.88rem; }
      .op-trust-row:last-child { border-bottom: none; }
      .op-trust-label { color: #cbd5e1; }
      .op-trust-status { font-weight: 600; white-space: nowrap; }
      .op-trust-done { color: #6ee7b7; }
      .op-trust-active { color: #7dd3fc; }
      .op-trust-ready { color: #fcd34d; }
      .op-trust-waiting { color: #94a3b8; }
      .op-trust-muted { color: #64748b; }
      .op-manual-test-package { background: linear-gradient(145deg, rgba(15,23,42,0.98), rgba(6,78,59,0.18)); border: 1px solid rgba(52,211,153,0.35); border-radius: 14px; padding: 1.1rem 1.15rem; margin-bottom: 0.85rem; box-shadow: 0 8px 28px rgba(2,6,23,0.35); }
      .op-mtp-header { display: flex; align-items: center; gap: 0.5rem; font-size: 1.08rem; font-weight: 700; color: #ecfdf5; margin-bottom: 0.35rem; }
      .op-mtp-header i { color: #6ee7b7; }
      .op-mtp-sub { color: #94a3b8; font-size: 0.86rem; margin-bottom: 0.85rem; }
      .op-mtp-section { margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px solid rgba(148,163,184,0.14); }
      .op-mtp-section:first-of-type { border-top: none; padding-top: 0; margin-top: 0; }
      .op-mtp-heading { font-size: 0.74rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: #a7f3d0; margin-bottom: 0.35rem; }
      .op-mtp-note { color: #fcd34d; font-size: 0.82rem; margin-bottom: 0.35rem; }
      .op-mtp-steps { margin: 0; padding-left: 1.15rem; }
      .op-mtp-steps li { margin-bottom: 0.28rem; color: #e2e8f0; font-size: 0.88rem; }
      .op-mtp-chips { display: flex; flex-wrap: wrap; gap: 0.35rem; }
      .op-mtp-chip { display: inline-block; padding: 0.18rem 0.55rem; border-radius: 999px; font-size: 0.72rem; font-weight: 600; color: #fecaca; background: rgba(127,29,29,0.35); border: 1px solid rgba(248,113,113,0.35); }
      .op-release-card { margin-top: 1rem; border: 1px solid rgba(34,197,94,0.35); border-radius: 12px; padding: 1rem 1.1rem; background: rgba(15,23,42,0.55); }
      .op-release-card.not-ready { border-color: rgba(148,163,184,0.25); }
      .op-release-title { font-weight: 700; color: #bbf7d0; font-size: 1rem; }
      .op-release-card.not-ready .op-release-title { color: #94a3b8; }
      .op-release-kicker { font-size: 0.68rem; letter-spacing: 0.1em; text-transform: uppercase; color: #86efac; }
      .op-release-section { margin-top: 0.75rem; }
      .op-release-heading { font-size: 0.76rem; font-weight: 600; text-transform: uppercase; color: #7dd3fc; margin-bottom: 0.35rem; }
      .op-release-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.85rem; }
      .op-release-progress { font-size: 0.82rem; color: #cbd5e1; }
      .op-mtp-locations code { color: #7dd3fc; font-size: 0.82rem; background: rgba(2,6,23,0.55); padding: 0.1rem 0.35rem; border-radius: 4px; }
      .op-mtp-decision { margin-top: 0.85rem; padding-top: 0.75rem; border-top: 1px solid rgba(148,163,184,0.14); }
      .op-mtp-decision p { color: #cbd5e1; font-size: 0.86rem; margin-bottom: 0.35rem; }
      .op-mtp-feedback-link { color: #fcd34d; text-decoration: none; font-size: 0.86rem; font-weight: 600; }
      .op-mtp-feedback-link:hover { color: #fde68a; text-decoration: underline; }
      .op-manual-test-approved { border-color: rgba(52,211,153,0.55); background: linear-gradient(145deg, rgba(15,23,42,0.98), rgba(6,95,70,0.22)); }
      .op-mtp-approved-banner { background: rgba(6,95,70,0.28); border: 1px solid rgba(110,231,183,0.35); border-radius: 10px; padding: 0.75rem 0.85rem; margin-bottom: 0.85rem; }
      .op-mtp-approved-title { color: #ecfdf5; font-weight: 700; font-size: 0.95rem; display: flex; align-items: center; gap: 0.4rem; }
      .op-mtp-approved-title i { color: #6ee7b7; }
      .op-mtp-approved-time { color: #a7f3d0; font-size: 0.82rem; margin-top: 0.25rem; }
      .op-mtp-approved-note { color: #cbd5e1; font-size: 0.84rem; margin-top: 0.35rem; }
      .op-mtp-final { border-top-color: rgba(110,231,183,0.25); }
      .op-mtp-final p { color: #d1fae5; }
      .op-status-badge { display: inline-flex; align-items: center; font-size: 1.05rem; font-weight: 700; padding: 0.35rem 0.85rem; border-radius: 999px; margin: 0.35rem 0 0.65rem; letter-spacing: 0.01em; }
      .op-status-planning { color: #c4b5fd; background: rgba(139,92,246,0.16); border: 1px solid rgba(167,139,250,0.45); }
      .op-status-working { color: #bae6fd; background: rgba(14,165,233,0.18); border: 1px solid rgba(56,189,248,0.45); }
      .op-status-fixing { color: #fde68a; background: rgba(245,158,11,0.14); border: 1px solid rgba(251,191,36,0.4); }
      .op-status-validating { color: #a5f3fc; background: rgba(6,182,212,0.16); border: 1px solid rgba(34,211,238,0.45); }
      .op-status-ready { color: #fde68a; background: rgba(245,158,11,0.16); border: 1px solid rgba(251,191,36,0.45); }
      .op-status-success { color: #bbf7d0; background: rgba(34,197,94,0.16); border: 1px solid rgba(74,222,128,0.45); }
      .op-status-release { color: #a5f3fc; background: rgba(6,182,212,0.16); border: 1px solid rgba(34,211,238,0.45); }
      .op-status-blocked { color: #fecaca; background: rgba(239,68,68,0.16); border: 1px solid rgba(248,113,113,0.45); }
      .op-current-build-card { background: rgba(2,6,23,0.5); border: 1px solid rgba(56,189,248,0.28); border-radius: 14px; padding: 1rem 1.1rem; margin-bottom: 0.85rem; box-shadow: 0 8px 28px rgba(2,6,23,0.28); }
      .op-current-build-title { font-size: 1.12rem; font-weight: 700; color: #f8fafc; line-height: 1.3; margin-bottom: 0.15rem; }
      .op-progress-wrap { margin: 0.35rem 0 0.85rem; }
      .op-progress-label { font-size: 0.78rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }
      .op-progress-value { font-size: 0.88rem; font-weight: 700; color: #e2e8f0; }
      .op-progress-bar-working, .op-progress-bar-planning, .op-progress-bar-validating, .op-progress-bar-fixing { background: linear-gradient(90deg, #0ea5e9, #22d3ee); }
      .op-progress-bar-ready, .op-progress-bar-fixing { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
      .op-progress-bar-success, .op-progress-bar-release { background: linear-gradient(90deg, #22c55e, #6ee7b7); }
      .op-progress-bar-blocked { background: linear-gradient(90deg, #ef4444, #f87171); }
      .op-step-card { background: rgba(15,23,42,0.55); border: 1px solid rgba(148,163,184,0.18); border-radius: 12px; padding: 0.85rem 0.95rem; }
      .op-step-kicker { font-size: 0.72rem; letter-spacing: 0.06em; text-transform: uppercase; color: #7dd3fc; margin-bottom: 0.35rem; font-weight: 600; }
      .op-step-body { color: #f1f5f9; font-size: 0.94rem; line-height: 1.45; }
      .op-step-next { margin-top: 0.65rem; padding-top: 0.65rem; border-top: 1px solid rgba(148,163,184,0.12); color: #cbd5e1; font-size: 0.84rem; line-height: 1.4; }
      .op-step-next-label { display: inline-block; font-size: 0.68rem; letter-spacing: 0.08em; text-transform: uppercase; color: #64748b; margin-right: 0.35rem; font-weight: 700; }
      .op-blocked-card { background: rgba(127,29,29,0.22); border: 1px solid rgba(248,113,113,0.45); border-radius: 12px; padding: 0.85rem 1rem; margin-bottom: 0.85rem; }
      .op-blocked-title { color: #fecaca; font-weight: 700; font-size: 0.95rem; margin-bottom: 0.35rem; display: flex; align-items: center; gap: 0.4rem; }
      .op-blocked-body { color: #fca5a5; font-size: 0.88rem; line-height: 1.4; }
    `;
    document.head.appendChild(el);
  }

  function renderProgressBarLegacy(pct) {
    return `<div class="op-mode-progress mt-2 mb-1"><div class="bar" style="width:${pct}%"></div></div>
      <div class="op-mode-muted small">Build progress: ${pct}%</div>`;
  }

  function renderAdvancedDetails(run, extras) {
    const journal = (extras && extras.journal) || [];
    const program = (extras && extras.program) || null;
    const journalHtml = journal.length
      ? journal
        .slice(-15)
        .map(
          (j) =>
            `<div class="border-bottom border-secondary py-1"><span class="tech">${esc(j.entry_type)}</span> · ${esc(j.created_at)}<br>${esc(JSON.stringify(j.content || {})).slice(0, 200)}</div>`
        )
        .join('')
      : '<span class="op-mode-muted">No journal entries.</span>';

    const phasesHtml = program
      ? (program.phases || [])
        .map(
          (ph) =>
            `<div class="tech py-1">Phase ${ph.phase_number}: ${esc(ph.title)} — ${esc(humanPhaseStatus(ph.status))} (review ${esc(ph.review_status || '—')}, validation ${esc(ph.validation_status)})</div>`
        )
        .join('')
      : '';

    const blockers = normalizeBlockers(run);
    const blockersHtml = blockers.length
      ? `<div class="text-warning mt-1">${blockers.map((b) => esc(b)).join('; ')}</div>`
      : '';

    return `
      <details class="op-mode-advanced" id="op-advanced-details">
        <summary>Advanced Details</summary>
        <div class="mt-2 small">
          <div class="tech">Build run: ${esc(run.id)}</div>
          ${run.task_plan_id ? `<div class="tech">Plan: ${esc(run.task_plan_id)}</div>` : ''}
          ${run.execution_program_id ? `<div class="tech">Program: ${esc(run.execution_program_id)}</div>` : ''}
          ${run.current_phase_id ? `<div class="tech">Phase: ${esc(run.current_phase_id)}</div>` : ''}
          ${run.latest_execution_id ? `<div class="tech">Execution: ${esc(run.latest_execution_id)}</div>` : ''}
          <div class="tech mt-1">Raw status: ${esc(run.status)} · plan ${esc(run.plan_status || '—')} · program ${esc(run.program_status || '—')} · phase ${esc(run.phase_status || '—')}</div>
          <div class="tech">Review: ${esc(run.review_status || '—')} · Validation: ${esc(run.validation_status || '—')}</div>
          ${blockersHtml}
          ${phasesHtml ? `<div class="mt-2"><strong class="text-secondary">Phases</strong>${phasesHtml}</div>` : ''}
          <div class="mt-2"><strong class="text-secondary">Journal</strong>${journalHtml}</div>
          <div class="mt-2 d-flex flex-wrap gap-2">
            ${run.execution_program_id ? `<a href="/ai-coding/executions/${esc(run.execution_program_id)}?mode=advanced" class="btn btn-outline-secondary btn-sm">Technical executions view</a>` : ''}
            ${run.task_plan_id ? `<a href="/ai-coding/planner/${esc(run.task_plan_id)}" class="btn btn-outline-secondary btn-sm">Task planner</a>` : ''}
          </div>
        </div>
      </details>`;
  }

  function renderReleaseActionList(actions) {
    if (!actions || !actions.length) {
      return '<p class="op-mode-muted small mb-0">No release actions planned yet.</p>';
    }
    return `<ul class="small mb-0 ps-3">${actions
      .map((item) => {
        const dry = item.dry_run_only !== false ? ' (dry-run)' : '';
        return `<li>${esc(sanitizeOperatorText(item.description || item.type || 'Action'))}${esc(dry)}</li>`;
      })
      .join('')}</ul>`;
  }

  function renderProductionReleaseCard(run, extras) {
    const release = (extras && extras.releaseStatus) || run.release_status || null;
    const status = String(run.status || '').toLowerCase();
    const showCard = RELEASE_FLOW_STATUSES.has(status) || (release && release.manual_test_approved);
    if (!showCard) return '';

    const enabled = release && release.release_agent_enabled;
    const label = (release && release.release_ready_label) || 'Not ready for release';
    const plan = release && release.plan;
    const notReady = label === 'Not ready for release' || !enabled;
    const cardClass = notReady ? 'op-release-card not-ready' : 'op-release-card';

    let body = '';
    if (status === 'operator_approved' || status === 'ready_for_release_approval' || status === 'release_approved') {
      body += `<p class="op-mode-muted small mb-2">AI has finished the build. You approved manual testing. Production release is not automatic.</p>`;
    }
    if (!enabled) {
      body += `<p class="op-mode-muted small mb-0">${esc(release?.blocked_reason || 'Release agent is disabled on the server.')}</p>`;
    } else if (plan) {
      body += `<div class="op-release-section"><div class="op-release-heading">What will be released</div><p class="small mb-0">${esc(plan.what_will_be_released || '')}</p></div>`;
      body += `<div class="op-release-section"><div class="op-release-heading">Target environment</div><p class="small mb-0">${esc(plan.target_environment || '—')}</p></div>`;
      body += `<div class="op-release-section"><div class="op-release-heading">Branch / build</div><p class="small mb-0">${esc(plan.branch_name || '—')} · ${esc(plan.build_reference || run.id || '')}</p></div>`;
      body += `<div class="op-release-section"><div class="op-release-heading">Actions AI will run</div>${renderReleaseActionList(plan.planned_actions)}</div>`;
      if (plan.rollback_plan && plan.rollback_plan.notes) {
        body += `<div class="op-release-section"><div class="op-release-heading">Rollback plan</div><p class="small mb-0">${esc(plan.rollback_plan.notes)}</p></div>`;
      }
      if (plan.risk_summary && plan.risk_summary.length) {
        body += `<div class="op-release-section"><div class="op-release-heading">Risk summary</div><ul class="small mb-0 ps-3">${plan.risk_summary.map((r) => `<li>${esc(sanitizeOperatorText(r))}</li>`).join('')}</ul></div>`;
      }
      if (release.dry_run_required) {
        body += `<p class="small text-info mb-0 mt-2"><i class="bi bi-shield-check"></i> Dry run required — no real merge, deploy, or send in Phase 1.</p>`;
      }
    } else if (release && release.manual_test_approved) {
      body += `<p class="op-mode-muted small mb-0">Review the release plan before allowing AI to run it.</p>`;
    }

    const latest = release && release.latest_execution;
    if (latest) {
      body += `<div class="op-release-progress mt-2">Latest execution: ${esc(latest.execution_mode)} · ${esc(latest.status)}</div>`;
    }

    const approveBlock = plan && plan.status === 'ready_for_approval'
      ? `<div class="form-check mt-2" id="sb-release-confirm-wrap">
          <input class="form-check-input" type="checkbox" id="sb-release-confirm" />
          <label class="form-check-label small" for="sb-release-confirm">I approve this production release.</label>
        </div>`
      : '';

    const buttons = [];
    if (enabled && release && release.primary_release_action === 'Prepare release plan') {
      buttons.push('<button type="button" class="btn btn-primary btn-sm" data-release-action="prepare">Prepare release plan</button>');
    }
    if (enabled && release && release.primary_release_action === 'Approve production release') {
      buttons.push('<button type="button" class="btn btn-success btn-sm" data-release-action="approve">Approve production release</button>');
    }
    if (enabled && release && release.primary_release_action === 'Run approved release') {
      buttons.push('<button type="button" class="btn btn-primary btn-sm" data-release-action="run">Run approved release</button>');
    }
    if (enabled && release && release.primary_release_action === 'Rollback release') {
      buttons.push('<button type="button" class="btn btn-outline-warning btn-sm" data-release-action="rollback">Rollback release</button>');
    }

    return `<div class="${cardClass}" id="op-production-release-panel">
      <div class="op-release-kicker">Production release</div>
      <div class="op-release-title mt-1">${esc(label)}</div>
      ${body}
      ${approveBlock}
      <div class="op-release-actions">${buttons.join('')}</div>
    </div>`;
  }

  function bindReleaseActions(scope, run, hooks) {
    if (!scope || !hooks) return;
    scope.querySelector('[data-release-action="prepare"]')?.addEventListener('click', () => {
      if (hooks.prepareRelease) hooks.prepareRelease(run);
    });
    scope.querySelector('[data-release-action="approve"]')?.addEventListener('click', () => {
      if (hooks.approveRelease) hooks.approveRelease(run, scope);
    });
    scope.querySelector('[data-release-action="run"]')?.addEventListener('click', () => {
      if (hooks.runRelease) hooks.runRelease(run);
    });
    scope.querySelector('[data-release-action="rollback"]')?.addEventListener('click', () => {
      if (hooks.rollbackRelease) hooks.rollbackRelease(run);
    });
  }

  function shouldFetchReleaseStatus(run) {
    if (!run || !run.id) return false;
    return RELEASE_FLOW_STATUSES.has(String(run.status || '').toLowerCase());
  }

  async function fetchReleaseStatus(api, run) {
    if (!shouldFetchReleaseStatus(run)) return null;
    try {
      return await global.dashboardFetchJson(`${api}/build-runs/${run.id}/release-status`);
    } catch (_) {
      return null;
    }
  }

  function isManualTestFeedbackStatus(run) {
    return String(run?.status || '').toLowerCase() === 'ready_for_manual_test';
  }

  function feedbackUiLabels(run) {
    const stopType = run?.stop_type || null;
    if (isManualTestFeedbackStatus(run) || stopType === 'STOP_TYPE_MANUAL_TEST') {
      return {
        link: 'Send feedback after test',
        submit: 'Send feedback after test',
        placeholder: 'What failed manual testing? What should AI fix?',
        label: 'Describe what failed manual testing',
      };
    }
    if (stopType === 'STOP_TYPE_REQUIREMENTS') {
      return {
        link: 'Answer question',
        submit: 'Submit answer',
        placeholder: 'Answer the question so AI can continue safely.',
        label: run.what_ai_needs ? `What AI needs: ${run.what_ai_needs}` : 'What AI needs',
      };
    }
    return {
      link: 'Answer question',
      submit: 'Submit answer',
      placeholder: 'What should AI fix or clarify on the next safe pass?',
      label: 'Notes for AI',
    };
  }

  function renderOperatorSimplePanel(container, run, extras) {
    ensureOperatorStyles();
    const pct = computeProgress(run);
    const action = primaryAction(run);
    const isReady = run.status === 'ready_for_manual_test';
    const showPrimary = action.show !== false;
    const fb = feedbackUiLabels(run);
    const showFeedbackLink = isReady
      || action.id === 'problems'
      || run.stop_type === 'STOP_TYPE_REQUIREMENTS';

    const clarificationAlert = '';

    const primaryBtn = showPrimary
      ? `<button type="button" class="btn btn-${action.tone} btn-lg op-mode-primary w-100 w-md-auto" id="sb-primary">${esc(action.label || 'Continue AI work')}</button>`
      : '';
    const refreshBtn = isReady
      ? ''
      : '<button type="button" class="btn btn-outline-secondary btn-sm" id="sb-refresh">Refresh</button>';

    const blockedAlert = '';
    const friendlyAlert = !showBlockedCard(run) && run.friendly_message
      ? `<div class="alert alert-info py-2 small mt-2 mb-0">${esc(sanitizeOperatorText(run.friendly_message))}</div>`
      : '';

    const feedbackWrap = `
      <div class="mt-3" id="sb-feedback-wrap" style="display:none">
        <label class="form-label small">${esc(fb.label)}</label>
        <textarea class="form-control form-control-sm" id="sb-decline-feedback" rows="4" placeholder="${esc(fb.placeholder)}"></textarea>
        <button type="button" class="btn btn-warning btn-sm mt-2" id="sb-decline-submit">${esc(fb.submit)}</button>
      </div>`;

    container.innerHTML = `
      <div class="op-mode-panel" data-operator-mode="true">
        <div class="op-mode-kicker"><i class="bi bi-person-workspace"></i> Simple Operator Mode</div>
        ${renderCurrentBuildCard(run, action)}
        ${run.show_manual_test_package !== false ? renderWhatToTestCard(run, extras || {}) : ''}
        ${run.show_release_action ? renderProductionReleaseCard(run, extras || {}) : ''}
        ${friendlyAlert}
        <details class="op-mode-advanced mt-3" id="op-more-details">
          <summary>More details</summary>
          <div class="mt-2">
            ${renderProductionChecklistPanel(run)}
            ${renderProgressBarLegacy(pct)}
            ${renderAdvancedDetails(run, extras || {})}
          </div>
        </details>
        <div class="d-flex flex-wrap gap-2 align-items-center mt-3 op-mtp-actions">
          ${primaryBtn}
          ${showFeedbackLink ? `<a href="#" class="op-mtp-feedback-link ms-md-2" data-action="send-feedback">${esc(fb.link)}</a>` : ''}
          ${refreshBtn}
        </div>
        ${feedbackWrap}
      </div>`;
  }

  function renderMustNotHappenChips(items) {
    if (!items || !items.length) return '';
    return `<div class="op-mtp-chips">${items.map((item) => `<span class="op-mtp-chip">${esc(sanitizeOperatorText(item))}</span>`).join('')}</div>`;
  }

  function renderWhereToCheck(checklist) {
    const locations = Array.isArray(checklist.where_to_check) ? checklist.where_to_check : [];
    const note = sanitizeOperatorText(checklist.where_to_check_note);
    const parts = [];
    if (locations.length) {
      parts.push(
        `<div class="op-mtp-locations small">${locations
          .map((loc) => `<div class="mb-1"><code>${esc(sanitizeOperatorText(loc))}</code></div>`)
          .join('')}</div>`
      );
    }
    if (note) {
      parts.push(`<p class="op-mtp-note mb-0">${esc(note)}</p>`);
    }
    if (!parts.length) {
      parts.push(`<p class="op-mtp-note mb-0">${esc('AI did not provide an exact screen or URL. Open the related page for this task and verify the behavior manually.')}</p>`);
    }
    return parts.join('');
  }

  function formatApprovalTime(iso) {
    if (!iso) return null;
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return sanitizeOperatorText(String(iso));
      return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    } catch (_) {
      return sanitizeOperatorText(String(iso));
    }
  }

  function manualTestPackageSections(checklist) {
    const testSteps = checklist.test_steps && checklist.test_steps.length
      ? checklist.test_steps
      : checklist.where_to_check;
    const expected = checklist.expected_result && checklist.expected_result.length
      ? checklist.expected_result
      : checklist.confirm_results;
    const validationNote = sanitizeOperatorText(checklist.validation_evidence);
    const sections = [
      `<div class="op-mtp-section"><div class="op-mtp-heading">What changed</div>${renderChecklistBullets(checklist.what_changed)}</div>`,
      `<div class="op-mtp-section"><div class="op-mtp-heading">Where to check</div>${renderWhereToCheck(checklist)}</div>`,
      `<div class="op-mtp-section"><div class="op-mtp-heading">Test steps</div><ol class="op-mtp-steps">${(testSteps || []).map((s) => `<li>${esc(sanitizeOperatorText(s))}</li>`).join('')}</ol></div>`,
      `<div class="op-mtp-section"><div class="op-mtp-heading">Expected result</div>${renderChecklistBullets(expected)}</div>`,
      `<div class="op-mtp-section"><div class="op-mtp-heading">Must not happen</div>${renderMustNotHappenChips(checklist.must_not_happen)}</div>`,
      `<div class="op-mtp-section op-mtp-decision-section"><div class="op-mtp-heading">Pass / fail decision</div><ol class="op-mtp-steps">${(checklist.pass_fail_decision || [
        'Walk through every test step above.',
        'If everything matches Expected result and Must not happen, choose Approve after test.',
        'If something failed, send feedback so AI can fix it.',
      ]).map((s) => `<li>${esc(sanitizeOperatorText(s))}</li>`).join('')}</ol></div>`,
    ];
    if (validationNote && validationNote.includes('not attached')) {
      sections.push(`<div class="op-mtp-section"><p class="op-mtp-note mb-0">${esc(validationNote)}</p></div>`);
    }
    if (checklist.rollback_note) {
      sections.push(
        `<div class="op-mtp-section"><div class="op-mtp-heading">Rollback note</div><p class="small mb-0 op-mode-muted">${esc(sanitizeOperatorText(checklist.rollback_note))}</p></div>`
      );
    }
    return sections;
  }

  function renderApprovedSummaryBanner(run, checklist) {
    const approvedAt = formatApprovalTime(
      checklist.approval_recorded_at || run.updated_at || run.created_at
    );
    const releaseNote = sanitizeOperatorText(
      checklist.production_release_note
      || run.production_release_summary
      || 'Not released automatically — approval is recorded only.'
    );
    const comment = sanitizeOperatorText(checklist.approval_comment);
    return `<div class="op-mtp-approved-banner">
      <div class="op-mtp-approved-title"><i class="bi bi-patch-check-fill"></i> ${esc(checklist.final_status || 'Approved after manual test')}</div>
      ${approvedAt ? `<div class="op-mtp-approved-time">Approved ${esc(approvedAt)}</div>` : ''}
      ${comment ? `<div class="op-mtp-approved-time">Note: ${esc(comment)}</div>` : ''}
      <p class="op-mtp-approved-note mb-0">${esc(releaseNote)}</p>
    </div>`;
  }

  function renderWhatToTestCard(run, extras) {
    const isApproved = run.status === 'operator_approved';
    const isReady = run.status === 'ready_for_manual_test' || run.manual_test_required;
    if (!isReady && !isApproved) {
      return `<div class="op-checklist-card">
        <div class="op-readiness-title">What to test</div>
        <p class="op-mode-muted small mb-0">AI will show test steps here when the work is ready.</p>
      </div>`;
    }

    const checklist = run.manual_test_checklist || {};
    const missing = sanitizeOperatorText(run.missing_evidence_reason);

    if (isApproved) {
      if (!checklist.what_changed || !checklist.what_changed.length) {
        return `<div class="op-manual-test-package op-manual-test-approved" id="op-manual-test-panel">
          <div class="op-mtp-header"><i class="bi bi-clipboard2-check"></i> Manual test package</div>
          ${renderApprovedSummaryBanner(run, checklist)}
          <p class="op-mode-muted small mb-0">Approval is recorded. Production release was not automatic.</p>
        </div>`;
      }
      return `<div class="op-manual-test-package op-manual-test-approved" id="op-manual-test-panel">
        <div class="op-mtp-header"><i class="bi bi-clipboard2-check"></i> Manual test package</div>
        <p class="op-mtp-sub">Record of what you tested before approval. Nothing was deployed or sent automatically.</p>
        ${renderApprovedSummaryBanner(run, checklist)}
        ${manualTestPackageSections(checklist).join('')}
        <div class="op-mtp-decision op-mtp-final">
          <p class="mb-0"><strong>Final status:</strong> Manual test approved. Production release remains manual — this build was not auto-deployed, auto-sent, or auto-merged.</p>
        </div>
      </div>`;
    }

    if (missing && (!checklist.what_changed || !checklist.what_changed.length)) {
      return `<div class="op-manual-test-package" id="op-manual-test-panel">
        <div class="op-mtp-header"><i class="bi bi-clipboard2-check"></i> Manual test package</div>
        <p class="op-mtp-sub">Test in your environment before approval. Nothing is deployed or sent automatically.</p>
        <p class="op-mtp-note">${esc(missing)}</p>
        <div class="op-mtp-decision">
          <p class="mb-1">After testing, approve if everything looks good.</p>
          <a href="#" class="op-mtp-feedback-link" data-action="send-feedback">Send feedback after test</a>
        </div>
      </div>`;
    }

    const validationNote = sanitizeOperatorText(checklist.validation_evidence);
    const sections = manualTestPackageSections(checklist);

    if (validationNote && validationNote.includes('not attached') && !sections.some((s) => s.includes(validationNote))) {
      sections.push(`<div class="op-mtp-section"><p class="op-mtp-note mb-0">${esc(validationNote)}</p></div>`);
    }

    return `<div class="op-manual-test-package" id="op-manual-test-panel">
      <div class="op-mtp-header"><i class="bi bi-clipboard2-check"></i> Manual test package</div>
      <p class="op-mtp-sub">Everything you need to test manually before approval. Nothing is deployed or sent automatically.</p>
      ${sections.join('')}
      ${missing ? `<p class="op-mtp-note mb-0">${esc(missing)}</p>` : ''}
      <div class="op-mtp-decision">
        <p class="mb-1"><strong>Decision:</strong> Use the Pass / fail checklist above, then choose <strong>Approve after test</strong> or send feedback.</p>
        <a href="#" class="op-mtp-feedback-link" data-action="send-feedback">Send feedback after test</a> if something needs fixing.
      </div>
    </div>`;
  }

  /** @deprecated Advanced details only */
  function humanBuildStatus(status) {
    const map = {
      draft: 'Planning',
      planning: 'Planning',
      plan_ready: 'AI Working',
      executing: 'AI Working',
      reviewing: 'AI Validating',
      validating: 'AI Validating',
      fixing: 'AI Fixing',
      ready_for_manual_test: 'Ready For Manual Test',
      operator_approved: 'Approved',
      operator_declined: 'AI Fixing',
      failed: 'Blocked',
      cancelled: 'Blocked',
    };
    return map[status] || 'AI Working';
  }

  /** Advanced details only — human-readable phase status */
  function humanPhaseStatus(status) {
    const map = {
      review_pending: 'Internal review',
      validation_pending: 'Internal validation',
      operator_approval: 'Awaiting approval',
      running: 'Running',
      ready: 'Ready',
      waiting: 'Waiting',
      completed: 'Completed',
      blocked: 'Blocked',
      failed: 'Failed',
      cancelled: 'Cancelled',
    };
    return map[String(status || '').toLowerCase()] || status || '—';
  }

  /** @deprecated Advanced details only */
  function gateSummary(run) {
    const review = (run.review_status || '').toLowerCase();
    const validation = (run.validation_status || '').toLowerCase();
    return {
      tests: validation === 'passed' ? 'passed' : 'pending',
      review: review === 'approved' || review === 'not_required' ? 'passed' : 'pending',
      validation: validation === 'passed' ? 'passed' : 'pending',
    };
  }

  global.AiCodingOperatorMode = {
    esc,
    sanitizeOperatorText,
    normalizeBlockers,
    ensureOperatorStyles,
    renderCurrentBuildCard,
    renderProgressBarBlock,
    progressPercent,
    progressStage,
    productionStatus,
    completedSummary,
    currentWorkSummary,
    whyWaiting,
    nextAfterClick,
    productionRelease,
    computeProgress,
    productionReadinessPercent,
    renderProductionChecklistPanel,
    primaryAction,
    isActiveNonTerminalStatus,
    isTerminalApprovedStatus,
    bindReleaseActions,
    fetchReleaseStatus,
    shouldFetchReleaseStatus,
    renderProductionReleaseCard,
    isManualTestFeedbackStatus,
    feedbackUiLabels,
    RELEASE_FLOW_STATUSES,
    mergeOperatorContinue,
    friendlyOperatorError,
    renderProductionReadinessCard,
    renderWhatToTestCard,
    renderManualTestChecklistCard: renderWhatToTestCard,
    renderOperatorSimplePanel,
    renderAdvancedDetails,
    humanBuildStatus,
    humanPhaseStatus,
    gateSummary,
  };
})(window);
