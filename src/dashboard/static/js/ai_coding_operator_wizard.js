/**
 * Operator Autopilot v1 — premium guided operations (client-side only).
 * Uses existing AI Coding proxy APIs. No auto-approve, deploy, merge, send, or release.
 */
(function (global) {
  'use strict';

  const ROADMAP = [
    { id: 'plan', label: 'Plan' },
    { id: 'execution', label: 'Execution' },
    { id: 'review', label: 'Review' },
    { id: 'validation', label: 'Validation' },
    { id: 'approval', label: 'Approval' },
    { id: 'governance', label: 'Governance' },
  ];

  const ACTION_CATALOG = {
    generate_plan: {
      label: 'Generate the plan',
      why: 'The system cannot continue until the plan is generated from your goal and project context.',
      safety: 'safe_dry_run',
      safetyLabel: 'Safe dry-run',
      onClick: 'Creates phased breakdown, risks, and validation notes. Nothing is deployed, merged, or sent.',
      roadmap: 'plan',
      needsChecklist: false,
      confirm: null,
    },
    fix_plan_policy: {
      label: 'Resolve plan blockers',
      why: 'Policy checks found issues that must be fixed before the plan can be approved.',
      safety: 'blocked',
      safetyLabel: 'Action blocked',
      onClick: 'Open the planner and address policy blockers manually.',
      roadmap: 'plan',
      needsChecklist: false,
      readonly: true,
    },
    approve_plan: {
      label: 'Approve this plan',
      why: 'The system cannot create an execution program until an operator approves the generated plan.',
      safety: 'operator_approval',
      safetyLabel: 'Operator approval required',
      onClick: 'Records plan approval only. Does not deploy, merge, release, or send messages.',
      roadmap: 'plan',
      needsChecklist: true,
      confirm: 'Approve this technical plan? This does not deploy or send anything.',
    },
    create_execution: {
      label: 'Create execution program',
      why: 'An approved plan needs a supervised execution program before any phase work can begin.',
      safety: 'safe_dry_run',
      safetyLabel: 'Safe dry-run',
      onClick: 'Creates a multi-phase execution shell. No code is deployed automatically.',
      roadmap: 'execution',
      needsChecklist: false,
      confirm: 'Create an execution program from this approved plan?',
    },
    start_program: {
      label: 'Start supervised run',
      why: 'The execution program is ready but has not been started yet.',
      safety: 'safe_dry_run',
      safetyLabel: 'Safe dry-run',
      onClick: 'Marks the program as running and prepares the first phase gate.',
      roadmap: 'execution',
      needsChecklist: false,
      confirm: 'Start this execution program? No deploy or merge will occur.',
    },
    continue_ready: {
      label: 'Start safe discovery',
      why: 'This phase is ready to begin. Discovery-style phases prepare prompts without auto-implementation.',
      safety: 'safe_dry_run',
      safetyLabel: 'Safe dry-run',
      onClick: 'Generates the phase prompt and links an execution record. Operator still reviews before later gates.',
      roadmap: 'execution',
      needsChecklist: false,
      confirm: 'Start work on this phase? No deploy, merge, or send occurs.',
    },
    continue_ready_impl: {
      label: 'Start next phase work',
      why: 'This phase is ready. Implementation phases may change code — review gates still apply afterward.',
      safety: 'needs_review',
      safetyLabel: 'Needs review',
      onClick: 'Starts phase work and generates a prompt. Review and validation gates remain mandatory.',
      roadmap: 'execution',
      needsChecklist: false,
      confirm: 'Start implementation work on this phase? You must still pass review and validation.',
    },
    continue_running: {
      label: 'Submit work for review',
      why: 'Phase work was started and should be submitted when ready for operator review.',
      safety: 'needs_review',
      safetyLabel: 'Needs review',
      onClick: 'Moves the phase to review. Does not auto-approve the review.',
      roadmap: 'review',
      needsChecklist: false,
      confirm: null,
    },
    approve_review: {
      label: 'Review completed work',
      why: 'This phase produced work that requires an execution review before validation can continue.',
      safety: 'needs_review',
      safetyLabel: 'Needs review',
      onClick: 'Records your review decision on the linked execution. Validation is still required afterward.',
      roadmap: 'review',
      needsChecklist: true,
      confirm: 'Mark the execution review as approved? This does not skip validation or phase approval.',
    },
    sync_review: {
      label: 'Continue after review',
      why: 'The execution review is decided. The review gate must be synced before validation.',
      safety: 'needs_validation',
      safetyLabel: 'Needs validation',
      onClick: 'Syncs the review gate and advances toward validation. Does not auto-pass validation.',
      roadmap: 'validation',
      needsChecklist: false,
      confirm: null,
    },
    continue_validation: {
      label: 'Confirm validation',
      why: 'Validation must be confirmed by the operator before final phase approval.',
      safety: 'needs_validation',
      safetyLabel: 'Needs validation',
      onClick: 'Records whether validation passed. Does not auto-approve the phase.',
      roadmap: 'validation',
      needsChecklist: true,
      confirm: null,
    },
    approve_phase: {
      label: 'Approve this phase',
      why: 'Validation passed, but final operator approval is required before moving to the next phase.',
      safety: 'operator_approval',
      safetyLabel: 'Operator approval required',
      onClick: 'Completes this phase in the program. Does not deploy, merge, or release.',
      roadmap: 'approval',
      needsChecklist: true,
      confirm: 'Approve completion of this phase? No deployment is performed.',
    },
    create_governance: {
      label: 'Create deployment governance plan',
      why: 'When execution is complete, deployment governance documents readiness before any real deploy decision.',
      safety: 'safe_readonly',
      safetyLabel: 'Safe read-only',
      onClick: 'Opens deployment governance. Assessment and approval are separate manual steps.',
      roadmap: 'governance',
      needsChecklist: false,
      readonly: true,
    },
    archive_program: {
      label: 'Archive completed test run',
      why: 'Terminal programs can be archived to keep the dashboard focused on active work.',
      safety: 'safe_dry_run',
      safetyLabel: 'Safe dry-run',
      onClick: 'Hides the program from default lists. Does not delete data.',
      roadmap: 'governance',
      needsChecklist: false,
      confirm: 'Archive this execution program?',
    },
    open_program: {
      label: 'Open active run',
      why: 'An execution program needs attention. Open it to see the precise next step.',
      safety: 'safe_readonly',
      safetyLabel: 'Safe read-only',
      onClick: 'Opens execution detail. No changes are made until you click an action.',
      roadmap: 'execution',
      needsChecklist: false,
      readonly: true,
    },
    idle: {
      label: 'Start a new mission',
      why: 'No pending guided steps were detected. Create or open a plan to begin.',
      safety: 'safe_readonly',
      safetyLabel: 'Safe read-only',
      onClick: 'Opens the task planner. No automatic changes occur.',
      roadmap: 'plan',
      needsChecklist: false,
      readonly: true,
    },
    gov_generate: {
      label: 'Generate readiness assessment',
      why: 'Governance plan needs an assessment before operator approval.',
      safety: 'safe_dry_run',
      safetyLabel: 'Safe dry-run',
      onClick: 'Scores readiness and builds checklists. Does not deploy.',
      roadmap: 'governance',
      needsChecklist: false,
      confirm: null,
    },
    gov_approve: {
      label: 'Approve governance plan',
      why: 'Deployment governance requires explicit operator approval. This is not a deploy action.',
      safety: 'operator_approval',
      safetyLabel: 'Operator approval required',
      onClick: 'Records governance approval only. No merge, release, or live send.',
      roadmap: 'governance',
      needsChecklist: true,
      confirm: 'Approve this governance plan? This does NOT deploy anything.',
    },
  };

  function esc(v) {
    return String(v ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;');
  }

  function ensureStyles() {
    if (document.getElementById('ai-autopilot-styles')) return;
    const el = document.createElement('style');
    el.id = 'ai-autopilot-styles';
    el.textContent = `
      .ai-autopilot-panel { background: linear-gradient(135deg, rgba(15,23,42,0.92), rgba(30,41,59,0.75)); border: 1px solid rgba(56,189,248,0.35); border-radius: 14px; padding: 1rem 1.1rem; box-shadow: 0 12px 40px rgba(0,0,0,0.25); }
      .ai-autopilot-kicker { font-size: 0.68rem; letter-spacing: 0.1em; text-transform: uppercase; color: #7dd3fc; }
      .ai-autopilot-mission-title { font-size: 1.05rem; font-weight: 600; color: #f8fafc; }
      .ai-autopilot-badge { display: inline-flex; border-radius: 999px; padding: 0.15rem 0.55rem; font-size: 0.68rem; font-weight: 600; border: 1px solid transparent; }
      .ai-autopilot-badge.status { background: rgba(56,189,248,0.15); color: #7dd3fc; border-color: rgba(56,189,248,0.3); }
      .ai-autopilot-badge.safety-readonly { background: rgba(148,163,184,0.15); color: #cbd5e1; border-color: rgba(148,163,184,0.25); }
      .ai-autopilot-badge.safety-dryrun { background: rgba(56,189,248,0.12); color: #7dd3fc; border-color: rgba(56,189,248,0.25); }
      .ai-autopilot-badge.safety-review { background: rgba(245,158,11,0.12); color: #fcd34d; border-color: rgba(245,158,11,0.25); }
      .ai-autopilot-badge.safety-validation { background: rgba(167,139,250,0.12); color: #c4b5fd; border-color: rgba(167,139,250,0.25); }
      .ai-autopilot-badge.safety-approval { background: rgba(250,204,21,0.12); color: #fde047; border-color: rgba(250,204,21,0.25); }
      .ai-autopilot-badge.safety-blocked { background: rgba(239,68,68,0.12); color: #fca5a5; border-color: rgba(239,68,68,0.25); }
      .ai-autopilot-next { font-size: 1.15rem; font-weight: 700; color: #f8fafc; margin: 0.35rem 0; }
      .ai-autopilot-muted { color: #94a3b8; font-size: 0.82rem; }
      .ai-autopilot-roadmap { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.75rem; }
      .ai-autopilot-roadmap .step { padding: 0.25rem 0.55rem; border-radius: 8px; font-size: 0.68rem; border: 1px solid rgba(148,163,184,0.2); color: #64748b; }
      .ai-autopilot-roadmap .step.done { color: #6ee7b7; border-color: rgba(16,185,129,0.35); }
      .ai-autopilot-roadmap .step.current { color: #7dd3fc; border-color: rgba(56,189,248,0.45); background: rgba(56,189,248,0.1); font-weight: 600; }
      .ai-autopilot-checklist { margin-top: 0.75rem; padding: 0.65rem 0.75rem; border-radius: 10px; background: rgba(2,6,23,0.45); border: 1px solid rgba(148,163,184,0.15); }
      .ai-autopilot-checklist li { margin: 0.2rem 0; font-size: 0.78rem; }
      .ai-autopilot-checklist li.ok { color: #6ee7b7; }
      .ai-autopilot-checklist li.bad { color: #fca5a5; }
      .ai-autopilot-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-top: 0.85rem; }
      .ai-autopilot-primary { min-width: 10rem; }
    `;
    document.head.appendChild(el);
  }

  function currentPhase(program) {
    if (!program?.phases?.length) return null;
    if (program.current_phase_id) {
      return program.phases.find((p) => p.id === program.current_phase_id) || program.phases[0];
    }
    return program.phases[0];
  }

  function reviewPending(ph) {
    const st = (ph?.review_status || 'pending').toLowerCase();
    return !st || st === 'pending';
  }

  function isDiscoveryPhase(ph) {
    if (!ph) return false;
    const t = `${ph.title || ''} ${ph.objective || ''}`.toLowerCase();
    return t.includes('discovery') || t.includes('scope confirmation') || t.includes('read-only');
  }

  function mergeCatalog(base) {
    const cat = ACTION_CATALOG[base.key] || {};
    return {
      ...cat,
      ...base,
      label: base.label || cat.label || 'Continue',
      why: base.why || cat.why || base.detail || '',
      safety: base.safety || cat.safety || 'safe_readonly',
      safetyLabel: base.safetyLabel || cat.safetyLabel || 'Safe read-only',
      onClick: base.onClick || cat.onClick || '',
      roadmap: base.roadmap || cat.roadmap || 'plan',
      needsChecklist: base.needsChecklist ?? cat.needsChecklist ?? false,
      confirm: base.confirm !== undefined ? base.confirm : cat.confirm,
      readonly: base.readonly ?? cat.readonly ?? false,
    };
  }

  function planStep(plan) {
    if (!plan || plan.status === 'archived') return null;
    if (plan.status === 'draft') {
      return mergeCatalog({ key: 'generate_plan', planId: plan.id, href: `/ai-coding/planner/${plan.id}`, priority: 30 });
    }
    if (['generated', 'reviewed'].includes(plan.status)) {
      const policy = plan.policy_result || {};
      if (!policy.allowed_to_approve) {
        return mergeCatalog({
          key: 'fix_plan_policy',
          planId: plan.id,
          href: `/ai-coding/planner/${plan.id}`,
          priority: 25,
          detail: (policy.blocking_reasons || []).join('; '),
        });
      }
      return mergeCatalog({ key: 'approve_plan', planId: plan.id, href: `/ai-coding/planner/${plan.id}`, priority: 40 });
    }
    if (plan.status === 'approved') {
      return mergeCatalog({ key: 'create_execution', planId: plan.id, href: `/ai-coding/planner/${plan.id}`, priority: 50 });
    }
    return null;
  }

  function programStep(program) {
    if (!program || program.status === 'archived') return null;
    const ph = currentPhase(program);
    if (['draft', 'ready'].includes(program.status)) {
      return mergeCatalog({ key: 'start_program', programId: program.id, href: `/ai-coding/executions/${program.id}`, priority: 60 });
    }
    if (!ph) return null;
    if (ph.status === 'ready') {
      const key = isDiscoveryPhase(ph) ? 'continue_ready' : 'continue_ready_impl';
      return mergeCatalog({ key, programId: program.id, phaseId: ph.id, href: `/ai-coding/executions/${program.id}`, priority: 70 });
    }
    if (ph.status === 'running') {
      return mergeCatalog({ key: 'continue_running', programId: program.id, phaseId: ph.id, href: `/ai-coding/executions/${program.id}`, priority: 75 });
    }
    if (['review_pending', 'blocked'].includes(ph.status) && reviewPending(ph)) {
      return mergeCatalog({
        key: 'approve_review',
        programId: program.id,
        phaseId: ph.id,
        executionId: ph.execution_id,
        href: `/ai-coding/executions/${program.id}`,
        priority: 90,
      });
    }
    if (ph.status === 'review_pending' || (ph.status === 'blocked' && !reviewPending(ph))) {
      return mergeCatalog({ key: 'sync_review', programId: program.id, phaseId: ph.id, href: `/ai-coding/executions/${program.id}`, priority: 85 });
    }
    if (ph.status === 'validation_pending') {
      return mergeCatalog({ key: 'continue_validation', programId: program.id, phaseId: ph.id, href: `/ai-coding/executions/${program.id}`, priority: 80 });
    }
    if (ph.status === 'operator_approval') {
      return mergeCatalog({ key: 'approve_phase', programId: program.id, phaseId: ph.id, href: `/ai-coding/executions/${program.id}`, priority: 88 });
    }
    if (program.status === 'completed') {
      return mergeCatalog({ key: 'create_governance', programId: program.id, href: '/ai-coding/deployments', priority: 20 });
    }
    if (['completed', 'cancelled', 'failed'].includes(program.status)) {
      return mergeCatalog({ key: 'archive_program', programId: program.id, href: `/ai-coding/executions/${program.id}`, priority: 10 });
    }
    return null;
  }

  function governanceStep(gov) {
    if (!gov) return null;
    if (gov.status === 'draft' || gov.status === 'generated') {
      return mergeCatalog({ key: 'gov_generate', govPlanId: gov.id, href: `/ai-coding/deployments/${gov.id}`, priority: 55 });
    }
    if (gov.can_approve || gov.status === 'operator_approval_required') {
      return mergeCatalog({ key: 'gov_approve', govPlanId: gov.id, href: `/ai-coding/deployments/${gov.id}`, priority: 56 });
    }
    return null;
  }

  function pickBest(steps) {
    const valid = steps.filter(Boolean);
    if (!valid.length) {
      return mergeCatalog({ key: 'idle', href: '/ai-coding/planner', priority: 0 });
    }
    return valid.sort((a, b) => b.priority - a.priority)[0];
  }

  function resolveNextAction(state, pageContext) {
    const steps = [];
    const ctx = pageContext || {};
    if (ctx.governance) steps.push(governanceStep(ctx.governance));
    if (ctx.program) steps.push(programStep(ctx.program));
    if (ctx.plan) steps.push(planStep(ctx.plan));
    if (!ctx.program && state.programs?.length) {
      for (const summary of state.programs) {
        if (['running', 'review_pending', 'validation_pending', 'operator_approval', 'blocked', 'ready', 'draft'].includes(summary.status)) {
          const full = state.programDetails?.[summary.id];
          steps.push(full ? programStep(full) : mergeCatalog({ key: 'open_program', programId: summary.id, href: `/ai-coding/executions/${summary.id}`, priority: 65 }));
        }
      }
    }
    if (!ctx.plan && state.plans?.length) {
      const approved = state.plans.find((p) => p.status === 'approved');
      const generated = state.plans.find((p) => ['generated', 'reviewed'].includes(p.status));
      const draft = state.plans.find((p) => p.status === 'draft');
      if (approved) steps.push(planStep(approved));
      else if (generated) steps.push(planStep(generated));
      else if (draft) steps.push(planStep(draft));
    }
    if (ctx.programId && state.programDetails?.[ctx.programId]) {
      steps.unshift(programStep(state.programDetails[ctx.programId]));
    }
    if (ctx.planId && state.planDetails?.[ctx.planId]) {
      steps.unshift(planStep(state.planDetails[ctx.planId]));
    }
    return pickBest(steps);
  }

  function humanStatus(plan, program) {
    if (program) {
      const map = {
        draft: 'Preparing run',
        ready: 'Ready to start',
        running: 'In progress',
        review_pending: 'Awaiting review',
        validation_pending: 'Awaiting validation',
        operator_approval: 'Awaiting your approval',
        blocked: 'Blocked — needs attention',
        completed: 'Completed',
        cancelled: 'Cancelled',
        failed: 'Failed',
      };
      return map[program.status] || program.status;
    }
    if (plan) {
      const map = { draft: 'Draft plan', generated: 'Generated — needs approval', reviewed: 'Reviewed', approved: 'Approved', rejected: 'Rejected' };
      return map[plan.status] || plan.status;
    }
    return 'Idle';
  }

  function buildMission(state, ctx, action) {
    const plan = ctx.plan || (ctx.planId && state.planDetails?.[ctx.planId]) || null;
    const program = ctx.program || (ctx.programId && state.programDetails?.[ctx.programId]) || null;
    const ph = program ? currentPhase(program) : null;
    return {
      projectName: program?.plan_title?.split(':')[0]?.trim() || plan?.title || 'Your project',
      planTitle: program?.plan_title || plan?.title || 'No plan selected',
      goal: (program?.plan_goal || plan?.goal || '').slice(0, 280) || 'Define a goal in the task planner.',
      phaseProgress: program ? `Phase completion: ${program.completed_phases || 0}/${program.total_phases || 0}` : 'Planning stage',
      phaseLabel: ph ? `Step ${ph.phase_number}: ${ph.title}` : null,
      statusLabel: humanStatus(plan, program),
    };
  }

  function buildRoadmap(action) {
    const current = action.roadmap || 'plan';
    const order = ROADMAP.map((r) => r.id);
    const idx = order.indexOf(current);
    return ROADMAP.map((step, i) => ({
      ...step,
      state: i < idx ? 'done' : i === idx ? 'current' : '',
    }));
  }

  function buildChecklist(action, plan, program) {
    if (!action.needsChecklist) return null;
    const ph = program ? currentPhase(program) : null;
    const forbidden = plan?.forbidden_scope || [];
    const constraints = plan?.constraints || [];
    const validation = (plan?.validation_strategy || program?.plan_goal || '').trim();
    const rollback = (plan?.rollback_strategy || '').trim();
    const hasPhases = (plan?.phases?.length || program?.phases?.length || 0) > 0;
    const items = [
      {
        ok: forbidden.length > 0 || constraints.length > 0,
        label: 'Forbidden scope and constraints documented',
      },
      {
        ok: true,
        label: 'No auto-send, scraping, or hidden sending in this workflow',
      },
      {
        ok: true,
        label: 'No deploy / merge / release control in this action',
      },
      {
        ok: action.key !== 'continue_validation' || ph?.validation_status === 'pending' || ph?.validation_status === 'passed',
        label: 'Validation outcome available or pending confirmation',
      },
      {
        ok: Boolean(validation),
        label: 'Validation strategy documented',
      },
      {
        ok: Boolean(rollback),
        label: 'Rollback note present',
      },
      {
        ok: hasPhases,
        label: 'Phase breakdown exists',
      },
    ];
    const allOk = items.every((i) => i.ok);
    return { items, allOk, warning: allOk ? null : 'Missing evidence — operator should not approve yet.' };
  }

  function buildViewModel(state, ctx) {
    const action = resolveNextAction(state, ctx);
    const plan = ctx.plan || (ctx.planId && state.planDetails?.[ctx.planId]) || null;
    const program = ctx.program || (ctx.programId && state.programDetails?.[ctx.programId]) || null;
    return {
      action,
      mission: buildMission(state, ctx, action),
      roadmap: buildRoadmap(action),
      checklist: buildChecklist(action, plan, program),
    };
  }

  async function loadState(api, pageContext) {
    const state = { plans: [], programs: [], governance: [], programDetails: {}, planDetails: {}, health: null };
    try {
      state.health = await global.dashboardFetchJson(`${api}/health`);
    } catch (_) {
      state.health = null;
    }
    try {
      state.plans = await global.dashboardFetchJson(`${api}/task-plans?limit=50`);
    } catch (_) {
      state.plans = [];
    }
    try {
      state.programs = await global.dashboardFetchJson(`${api}/execution-programs?limit=50`);
    } catch (_) {
      state.programs = [];
    }
    try {
      state.governance = await global.dashboardFetchJson(`${api}/deployment-governance/plans?limit=50`);
    } catch (_) {
      state.governance = [];
    }
    const detailIds = new Set();
    if (pageContext.programId) detailIds.add(pageContext.programId);
    if (pageContext.program?.id) detailIds.add(pageContext.program.id);
    for (const p of state.programs) {
      if (['running', 'review_pending', 'validation_pending', 'operator_approval', 'blocked', 'ready', 'draft'].includes(p.status)) {
        detailIds.add(p.id);
      }
    }
    for (const id of [...detailIds].slice(0, 8)) {
      try {
        state.programDetails[id] = await global.dashboardFetchJson(`${api}/execution-programs/${id}`);
      } catch (_) { /* skip */ }
    }
    if (pageContext.planId) {
      try {
        state.planDetails[pageContext.planId] = await global.dashboardFetchJson(`${api}/task-plans/${pageContext.planId}`);
      } catch (_) { /* skip */ }
    }
    if (pageContext.govPlanId) {
      try {
        state.governanceDetail = await global.dashboardFetchJson(`${api}/deployment-governance/plans/${pageContext.govPlanId}`);
      } catch (_) { /* skip */ }
    }
    return state;
  }

  function safetyClass(safety) {
    return {
      safe_readonly: 'safety-readonly',
      safe_dry_run: 'safety-dryrun',
      needs_review: 'safety-review',
      needs_validation: 'safety-validation',
      operator_approval: 'safety-approval',
      blocked: 'safety-blocked',
    }[safety] || 'safety-readonly';
  }

  function renderPanel(panelEl, view) {
    if (!panelEl) return;
    ensureStyles();
    const { action, mission, roadmap, checklist } = view;
    const readonly = action.readonly;
    const safetyCls = safetyClass(action.safety);
    const checklistHtml = checklist
      ? `<div class="ai-autopilot-checklist">
          <div class="ai-autopilot-muted mb-1"><strong>Before you approve</strong></div>
          <ul class="mb-0 ps-3">${checklist.items.map((i) => `<li class="${i.ok ? 'ok' : 'bad'}">${i.ok ? '✓' : '✗'} ${esc(i.label)}</li>`).join('')}</ul>
          ${checklist.warning ? `<div class="text-warning mt-2 small">${esc(checklist.warning)}</div>` : ''}
        </div>`
      : '';
    const roadmapHtml = roadmap
      .map((s) => `<span class="step ${s.state}">${esc(s.label)}</span>`)
      .join('<span class="ai-autopilot-muted">→</span>');

    panelEl.className = 'ai-autopilot-panel mb-3';
    panelEl.innerHTML = `
      <div class="d-flex flex-wrap justify-content-between gap-2 mb-2">
        <div>
          <div class="ai-autopilot-kicker"><i class="bi bi-stars"></i> Operator Autopilot v1</div>
          <div class="ai-autopilot-mission-title mt-1">${esc(mission.projectName)}</div>
          <div class="ai-autopilot-muted">${esc(mission.planTitle)}</div>
        </div>
        <div class="text-end">
          <span class="ai-autopilot-badge status">${esc(mission.statusLabel)}</span>
          <span class="ai-autopilot-badge ${safetyCls}">${esc(action.safetyLabel)}</span>
        </div>
      </div>
      <div class="row g-2 small">
        <div class="col-md-8">
          <div class="ai-autopilot-muted">Mission</div>
          <div>${esc(mission.goal)}</div>
        </div>
        <div class="col-md-4">
          <div class="ai-autopilot-muted">Phase completion</div>
          <div>${esc(mission.phaseProgress)}</div>
          ${mission.phaseLabel ? `<div class="ai-autopilot-muted mt-1">${esc(mission.phaseLabel)}</div>` : ''}
        </div>
      </div>
      <div class="ai-autopilot-roadmap">${roadmapHtml}</div>
      <hr class="border-secondary opacity-25 my-3" />
      <div class="ai-autopilot-muted">Next best action</div>
      <div class="ai-autopilot-next">${esc(action.label)}</div>
      <div class="ai-autopilot-muted mt-2"><strong>Why:</strong> ${esc(action.why)}</div>
      <div class="ai-autopilot-muted mt-1"><strong>If you click:</strong> ${esc(action.onClick)}</div>
      ${checklistHtml}
      <div class="ai-autopilot-actions">
        <button type="button" class="btn btn-primary ai-autopilot-primary" id="ai-wizard-go" ${readonly ? 'disabled' : ''}>${readonly ? 'Open details' : esc(action.label)}</button>
        <a class="btn btn-outline-light btn-sm" id="ai-wizard-open" href="${esc(action.href || '#')}">Open details</a>
        <button type="button" class="btn btn-outline-secondary btn-sm" id="ai-wizard-refresh"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
      </div>
      <div id="ai-wizard-msg" class="mt-2"></div>`;
  }

  async function runAction(api, action, hooks) {
    const showMsg = hooks.showMsg || (() => {});
    const onDone = hooks.onDone || (() => {});

    if (action.readonly && action.href) {
      global.location.href = action.href;
      return;
    }
    if (action.confirm && !global.confirm(action.confirm)) return;

    try {
      switch (action.key) {
        case 'generate_plan':
          await global.dashboardFetchJson(`${api}/task-plans/${action.planId}/generate`, { method: 'POST' });
          showMsg('Plan generated successfully. Refreshing guidance…', 'success');
          break;
        case 'approve_plan':
          await global.dashboardFetchJson(`${api}/task-plans/${action.planId}/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
          });
          showMsg('Plan approved. Next: create an execution program when ready.', 'success');
          break;
        case 'create_execution': {
          const resp = await global.dashboardFetchJson(`${api}/task-plans/${action.planId}/create-execution`, { method: 'POST' });
          showMsg('Execution program created. Opening supervised run…', 'success');
          global.location.href = `/ai-coding/executions/${resp.execution_program_id}`;
          return;
        }
        case 'start_program':
          await global.dashboardFetchJson(`${api}/execution-programs/${action.programId}/start`, { method: 'POST' });
          showMsg('Supervised run started.', 'success');
          break;
        case 'continue_ready':
        case 'continue_ready_impl':
        case 'continue_running':
        case 'sync_review': {
          const body = { actor: 'operator' };
          if (action.key === 'continue_running') body.note = global.prompt('Optional note for reviewers:') || '';
          await global.dashboardFetchJson(
            `${api}/execution-programs/${action.programId}/phases/${action.phaseId}/continue`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
          );
          showMsg('Step recorded. Guidance will refresh with the new gate.', 'success');
          break;
        }
        case 'approve_review':
          await global.dashboardFetchJson(`${api}/executions/${action.executionId}/operator-review`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              decision: 'approved',
              summary: global.prompt('Review summary (optional):') || 'Operator approved',
            }),
          });
          showMsg('Review recorded. Next: click the primary button again to sync the review gate.', 'success');
          break;
        case 'continue_validation': {
          const passed = global.confirm('Did validation pass for this phase?');
          const note = global.prompt('Validation note (optional):') || '';
          await global.dashboardFetchJson(
            `${api}/execution-programs/${action.programId}/phases/${action.phaseId}/continue`,
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ actor: 'operator', validation_passed: passed, note }),
            }
          );
          showMsg(passed ? 'Validation marked passed. Approve the phase when ready.' : 'Validation failed — phase may block.', passed ? 'success' : 'warning');
          break;
        }
        case 'approve_phase':
          await global.dashboardFetchJson(
            `${api}/execution-programs/${action.programId}/phases/${action.phaseId}/approve`,
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ comment: 'Operator approved phase completion' }),
            }
          );
          showMsg('Phase approved. Continue with the next phase when it becomes ready.', 'success');
          break;
        case 'archive_program':
          await global.dashboardFetchJson(`${api}/execution-programs/${action.programId}/archive`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: global.prompt('Archive reason (optional):') || '' }),
          });
          showMsg('Test run archived.', 'success');
          break;
        case 'gov_generate':
          await global.dashboardFetchJson(`${api}/deployment-governance/plans/${action.govPlanId}/generate`, { method: 'POST' });
          showMsg('Governance assessment generated.', 'success');
          break;
        case 'gov_approve':
          await global.dashboardFetchJson(`${api}/deployment-governance/plans/${action.govPlanId}/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ comment: 'Operator approved deployment readiness' }),
          });
          showMsg('Governance plan approved. No deployment was performed.', 'success');
          break;
        case 'open_program':
        case 'create_governance':
        case 'fix_plan_policy':
        case 'idle':
          if (action.href) global.location.href = action.href;
          return;
        default:
          if (action.href) {
            global.location.href = action.href;
            return;
          }
          showMsg('No automated action configured. Use Open details.', 'warning');
          return;
      }
      await onDone(action);
    } catch (err) {
      const msg = hooks.formatError ? hooks.formatError(err) : (err && err.message) || 'Action failed';
      showMsg(`${msg} — try Refresh or Open details.`, 'danger');
    }
  }

  function init(options) {
    const api = options.api;
    const panelId = options.panelId || 'ai-code-wizard-panel';
    const panelEl = document.getElementById(panelId);
    if (!panelEl || !api) return null;

    const hooks = {
      showMsg: (msg, tone) => {
        const el = document.getElementById('ai-wizard-msg');
        if (el) el.innerHTML = `<div class="alert alert-${tone} py-2 mb-0">${esc(msg)}</div>`;
        if (options.alertFn) options.alertFn(msg, tone);
      },
      formatError: options.formatError,
      onDone: options.onRefresh || (() => {}),
    };

    let timer = null;
    let lastView = null;

    async function refresh() {
      panelEl.innerHTML = '<div class="ai-autopilot-muted py-2"><span class="spinner-border spinner-border-sm me-2"></span>Updating autopilot…</div>';
      const ctx = options.getContext ? await options.getContext() : {};
      const state = await loadState(api, ctx);
      if (ctx.govPlanId && state.governanceDetail) ctx.governance = state.governanceDetail;
      lastView = buildViewModel(state, ctx);
      renderPanel(panelEl, lastView);
      const go = document.getElementById('ai-wizard-go');
      if (go) {
        go.addEventListener('click', () => {
          if (lastView.checklist && !lastView.checklist.allOk && lastView.action.needsChecklist) {
            if (!global.confirm('Evidence checklist is incomplete. Proceed anyway?')) return;
          }
          runAction(api, lastView.action, hooks);
        });
      }
      document.getElementById('ai-wizard-refresh')?.addEventListener('click', () => refresh().catch((e) => hooks.showMsg((e && e.message) || 'Refresh failed', 'danger')));
    }

    refresh().catch((err) => hooks.showMsg((err && err.message) || 'Autopilot failed to load', 'danger'));

    if (options.autoRefreshMs > 0) {
      timer = global.setInterval(() => refresh().catch(() => {}), options.autoRefreshMs);
    }

    return { refresh, stop: () => { if (timer) global.clearInterval(timer); } };
  }

  global.AiCodingOperatorWizard = {
    init,
    resolveNextAction,
    buildViewModel,
    buildChecklist,
    currentPhase,
    ACTION_CATALOG,
    ROADMAP,
  };
})(window);
