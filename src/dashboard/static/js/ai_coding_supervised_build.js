/**
 * Supervised Build Loop — Operator Mode UI (client-side only).
 */
(function (global) {
  'use strict';

  const OM = () => global.AiCodingOperatorMode;

  function formatError(err) {
    const om = OM();
    return om && om.friendlyOperatorError ? om.friendlyOperatorError(err) : 'Request failed';
  }

  function mergeContinueResponse(run, resp) {
    const om = OM();
    return om && om.mergeOperatorContinue ? om.mergeOperatorContinue(run, resp) : (resp && resp.run) || run;
  }

  async function fetchJson(api, path, options) {
    return global.dashboardFetchJson(`${api}${path}`, options);
  }

  function normalizeProjectsList(data) {
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.items)) return data.items;
    return [];
  }

  function projectIdOf(project) {
    return String((project && (project.project_id || project.id)) || '').trim();
  }

  function projectLabelOf(project, esc) {
    const name = String((project && (project.project_name || project.name)) || '').trim();
    if (name) return esc(name);
    const id = projectIdOf(project);
    return id ? esc(id) : esc('Project');
  }

  function ensureStyles() {
    const om = OM();
    if (om && om.ensureOperatorStyles) {
      om.ensureOperatorStyles();
      return;
    }
  }

  function renderProgress(pct) {
    return `<div class="op-mode-progress mt-2 mb-1"><div class="bar" style="width:${pct}%"></div></div>
      <div class="op-mode-muted small">Build progress: ${pct}%</div>`;
  }

  function renderGateRow(gates) {
    const icon = (s) => (s === 'passed' ? 'ok' : s === 'failed' ? 'bad' : 'pending');
    const label = (s) => (s === 'passed' ? 'Passed' : s === 'failed' ? 'Failed' : 'Pending');
    return `<div class="d-flex flex-wrap gap-2 mt-1">
      <span class="op-mode-gate ${icon(gates.review)}"><i class="bi bi-eye"></i> Review: ${label(gates.review)}</span>
      <span class="op-mode-gate ${icon(gates.validation)}"><i class="bi bi-check2-square"></i> Validation: ${label(gates.validation)}</span>
      <span class="op-mode-gate ${icon(gates.tests)}"><i class="bi bi-bug"></i> Tests: ${label(gates.tests)}</span>
    </div>`;
  }

  function renderReadyScreen(run, hooks) {
    const om = OM();
    const esc = om.esc;
    const report = run.manual_test_report || {};
    const changed = om.whatChangedBullets(run);
    const steps = om.howToTestSteps(report);
    const rollback = report.rollback_note || run.operator_notes || 'Follow plan rollback strategy before any production change.';

    return `
      <div class="op-mode-ready" id="op-manual-test-panel">
        <h4><i class="bi bi-check2-circle"></i> Ready for manual test</h4>
        <p class="op-mode-muted mb-0">Test in your environment, then approve or send feedback. Nothing is deployed or sent automatically.</p>
      </div>
      <div class="op-mode-section">
        <h6>What changed</h6>
        <ul class="small mb-0">${changed.map((c) => `<li>${esc(c)}</li>`).join('')}</ul>
      </div>
      <div class="op-mode-section">
        <h6>How to test</h6>
        <ol class="small mb-0">${steps.map((s) => `<li>${esc(s)}</li>`).join('')}</ol>
      </div>
      <div class="op-mode-section">
        <h6>Rollback</h6>
        <p class="small op-mode-muted mb-0">${esc(rollback)}</p>
      </div>
      <div class="mt-3" id="sb-feedback-wrap" style="display:none">
        <label class="form-label small">Describe what is wrong or what to improve</label>
        <textarea class="form-control form-control-sm" id="sb-decline-feedback" rows="4" placeholder="e.g. Campaign list does not load on mobile…"></textarea>
        <button type="button" class="btn btn-warning btn-sm mt-2" id="sb-decline-submit">Send feedback to AI</button>
      </div>`;
  }

  function renderAdvancedDetails(run, extras) {
    const om = OM();
    return om && om.renderAdvancedDetails ? om.renderAdvancedDetails(run, extras) : '';
  }

  function bindPrimaryActions(run, hooks) {
    const om = OM();
    const action = om.primaryAction(run);
    const panel = document.querySelector('[data-operator-mode="true"]');

    function showFeedbackForm() {
      const wrap = document.getElementById('sb-feedback-wrap');
      if (wrap) {
        wrap.style.display = 'block';
        wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }

    document.getElementById('sb-primary')?.addEventListener('click', () => {
      if (action.id === 'approve') hooks.approve(run);
      else if (action.id === 'advance') hooks.advance(run);
      else if (action.id === 'problems') showFeedbackForm();
      else if (action.id === 'retry') hooks.refresh();
      else if (action.id === 'release_prepare') hooks.prepareRelease(run);
      else if (action.id === 'release_approve') hooks.approveRelease(run, panel);
      else if (action.id === 'release_run') hooks.runRelease(run);
      else if (action.id === 'release_rollback') hooks.rollbackRelease(run);
    });

    panel?.querySelectorAll('[data-action="send-feedback"]').forEach((link) => {
      link.addEventListener('click', (ev) => {
        ev.preventDefault();
        showFeedbackForm();
      });
    });

    document.getElementById('sb-decline-submit')?.addEventListener('click', () => hooks.submitFeedback(run));

    document.getElementById('sb-refresh')?.addEventListener('click', () => hooks.loadRun(run.id));
  }

  async function enrichRun(api, run) {
    const extras = { journal: [], program: null };
    try {
      if (run.id) extras.journal = await fetchJson(api, `/build-runs/${run.id}/journal`);
    } catch (_) {}
    try {
      if (run.execution_program_id) {
        extras.program = await fetchJson(api, `/execution-programs/${run.execution_program_id}`);
        if (extras.program && run.manual_test_report) {
          run.manual_test_report.completed_phases = extras.program.completed_phases;
          run.manual_test_report.total_phases = extras.program.total_phases;
        }
      }
    } catch (_) {}
    const om = OM();
    if (om && om.fetchReleaseStatus) {
      extras.releaseStatus = await om.fetchReleaseStatus(api, run);
      if (extras.releaseStatus) run.release_status = extras.releaseStatus;
    }
    return extras;
  }

  const DEFAULT_FORBIDDEN =
    'spam, scraping users, hidden sending, auto-send, send-all without approval, bypassing limits, production deploy, secret exposure';

  function renderDetail(container, run, hooks, extras, options) {
    const om = OM();
    if (!om || !om.renderOperatorSimplePanel) {
      container.innerHTML = '<p class="op-mode-muted small py-3">Operator UI unavailable. Hard refresh the page.</p>';
      return;
    }
    om.renderOperatorSimplePanel(container, run, extras || {});
    bindPrimaryActions(run, hooks);
    if (om.bindReleaseActions) om.bindReleaseActions(container, run, hooks);
  }

  function renderNewTaskForm(container, projects, hooks) {
    ensureStyles();
    const om = OM();
    const esc = om.esc;
    const list = normalizeProjectsList(projects);
    const opts = list.length
      ? list
        .map((p) => `<option value="${esc(projectIdOf(p))}">${projectLabelOf(p, esc)}</option>`)
        .join('')
      : '<option value="">Project</option>';
    const defaultProject = list[0] ? projectIdOf(list[0]) : '';

    container.innerHTML = `
      <div class="op-mode-panel">
        <div class="op-mode-kicker"><i class="bi bi-person-workspace"></i> Simple Operator Mode</div>
        <div class="op-mode-title mt-1">New build task</div>
        <div class="mb-3">
          <label class="form-label">Task title</label>
          <input class="form-control" id="sb-title" placeholder="e.g. Telegram Broadcast Platform" />
        </div>
        <div class="mb-3">
          <label class="form-label">What should AI build?</label>
          <textarea class="form-control" id="sb-goal" rows="4" placeholder="Describe what you need…"></textarea>
        </div>
        <details class="op-mode-advanced">
          <summary>Advanced options</summary>
          <div class="row g-2 mt-2">
            <div class="col-12">
              <label class="form-label small">Project</label>
              <select class="form-select form-select-sm" id="sb-project">${opts}</select>
            </div>
            <div class="col-12">
              <label class="form-label small">Forbidden scope</label>
              <input class="form-control form-control-sm" id="sb-forbidden" value="${esc(DEFAULT_FORBIDDEN)}" />
            </div>
            <div class="col-md-6">
              <label class="form-label small">Operator notes</label>
              <input class="form-control form-control-sm" id="sb-notes" />
            </div>
            <div class="col-md-6">
              <label class="form-label small">Max fix attempts</label>
              <input type="number" class="form-control form-control-sm" id="sb-max-attempts" value="3" min="1" max="10" />
            </div>
          </div>
        </details>
        <button type="button" class="btn btn-primary btn-lg mt-2" id="sb-start">Start build</button>
      </div>`;
    const sel = document.getElementById('sb-project');
    if (sel && defaultProject) sel.value = defaultProject;
    document.getElementById('sb-start')?.addEventListener('click', () => hooks.startBuild());
  }

  function renderRunList(rows) {
    const om = OM();
    const esc = om.esc;
    if (!rows.length) return '<div class="op-mode-muted small py-2">No tasks yet.</div>';
    return rows
      .map(
        (r) => `
      <a href="/ai-coding/builds/${esc(r.id)}" class="d-block border-bottom border-secondary py-2 text-decoration-none">
        <strong class="text-light">${esc(r.task_title)}</strong>
        <span class="badge bg-dark border border-secondary ms-1">${esc(om.humanBuildStatus(r.status))}</span>
        <div class="op-mode-muted small">${esc(om.currentWorkSummary ? om.currentWorkSummary(r) : (r.current_work_summary || ''))}</div>
      </a>`
      )
      .join('');
  }

  function init(options) {
    const api = options.api;
    const listEl = document.getElementById(options.listPanelId || 'ai-sb-list');
    const detailEl = document.getElementById(options.detailPanelId || 'ai-sb-detail');
    const initialRunId = options.runId || null;
    const wizardEl = document.getElementById('ai-code-wizard-panel');

    if (wizardEl && options.hideWizard !== false) {
      wizardEl.innerHTML = `<details class="op-mode-advanced"><summary>Guided Autopilot (v1)</summary>
        <div id="ai-wizard-nested" class="mt-2 small op-mode-muted">Loading…</div></details>`;
      if (global.AiCodingOperatorWizard) {
        AiCodingOperatorWizard.init({
          ...options,
          panelId: 'ai-wizard-nested',
          autoRefreshMs: 0,
        });
      }
    }

    const hooks = {
      showMsg: (msg, tone) => {
        if (options.alertFn) options.alertFn(msg, tone);
      },
      async loadProjects() {
        try {
          const data = await fetchJson(api, '/projects/summary?limit=50');
          return normalizeProjectsList(data);
        } catch (_) {
          return [];
        }
      },
      async loadRuns() {
        const rows = await fetchJson(api, '/build-runs?limit=30');
        if (listEl) {
          listEl.className = 'op-mode-list small';
          listEl.innerHTML = renderRunList(rows);
        }
      },
      async loadRun(id) {
        try {
          const run = await fetchJson(api, `/build-runs/${id}`);
          const extras = await enrichRun(api, run);
          if (detailEl) renderDetail(detailEl, run, hooks, extras);
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
      async startBuild() {
        const projectId = document.getElementById('sb-project')?.value;
        const title = document.getElementById('sb-title')?.value?.trim();
        const goal = document.getElementById('sb-goal')?.value?.trim();
        if (!title || !goal) {
          hooks.showMsg('Task title and description are required.', 'warning');
          return;
        }
        if (!projectId) {
          hooks.showMsg('Select a project under Advanced options.', 'warning');
          return;
        }
        if (!global.confirm('Start supervised build? AI will plan and build. You approve before anything goes live.')) return;
        const forbidden = (document.getElementById('sb-forbidden')?.value || DEFAULT_FORBIDDEN)
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean);
        try {
          const created = await fetchJson(api, '/build-runs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              project_id: projectId,
              task_title: title,
              task_goal: goal,
              forbidden_scope: forbidden,
              operator_notes: document.getElementById('sb-notes')?.value || null,
              max_attempts: Number(document.getElementById('sb-max-attempts')?.value || 3),
            }),
          });
          hooks.showMsg('Build started.', 'success');
          global.location.href = `/ai-coding/builds/${created.id}`;
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
      async advance(run) {
        try {
          const resp = await fetchJson(api, `/build-runs/${run.id}/operator-continue`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actor: 'operator' }),
          });
          const updated = mergeContinueResponse(run, resp);
          const extras = await enrichRun(api, updated);
          renderDetail(detailEl, updated, hooks, extras);
          const msg = resp.friendly_message
            || (resp.advanced ? 'AI work continued.' : 'AI is waiting for your input.');
          hooks.showMsg(msg, resp.advanced ? 'success' : 'info');
          await hooks.loadRuns();
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
          await hooks.loadRun(run.id);
        }
      },
      async approve(run) {
        if (!global.confirm('Confirm you manually tested and approve this build?')) return;
        try {
          const updated = await fetchJson(api, `/build-runs/${run.id}/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ comment: 'Manual test passed' }),
          });
          const extras = await enrichRun(api, updated);
          renderDetail(detailEl, updated, hooks, extras);
          hooks.showMsg('Approved.', 'success');
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
      async submitFeedback(run) {
        const om = OM();
        const isManualTest = om && om.isManualTestFeedbackStatus
          ? om.isManualTestFeedbackStatus(run)
          : run.status === 'ready_for_manual_test';
        const message = document.getElementById('sb-decline-feedback')?.value?.trim();
        if (!message) {
          hooks.showMsg(
            isManualTest ? 'Describe what failed manual testing.' : 'Enter notes for AI.',
            'warning',
          );
          return;
        }
        try {
          let updated;
          if (isManualTest) {
            updated = await fetchJson(api, `/build-runs/${run.id}/decline`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ feedback: message }),
            });
            hooks.showMsg('Feedback sent.', 'info');
          } else {
            updated = await fetchJson(api, `/build-runs/${run.id}/feedback`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                message,
                source: 'operator_pre_manual_test',
              }),
            });
            hooks.showMsg('Notes sent to AI.', 'info');
          }
          const extras = await enrichRun(api, updated);
          renderDetail(detailEl, updated, hooks, extras);
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
      async decline(run) {
        await hooks.submitFeedback(run);
      },
      async prepareRelease(run) {
        try {
          const resp = await fetchJson(api, `/build-runs/${run.id}/prepare-release`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actor: 'operator', target_environment: 'staging' }),
          });
          run.release_status = resp;
          const updated = await fetchJson(api, `/build-runs/${run.id}`);
          const extras = await enrichRun(api, updated);
          renderDetail(detailEl, updated, hooks, extras);
          hooks.showMsg('Release plan prepared.', 'success');
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
      async approveRelease(run, scopeEl) {
        const scope = scopeEl || detailEl;
        const confirmed = scope?.querySelector('#sb-release-confirm')?.checked;
        if (!confirmed) {
          hooks.showMsg('Check the box to confirm production release approval.', 'warning');
          return;
        }
        try {
          await fetchJson(api, `/build-runs/${run.id}/approve-release`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              actor: 'operator',
              operator_confirmed: true,
              confirmation_text: 'I approve this production release.',
            }),
          });
          const updated = await fetchJson(api, `/build-runs/${run.id}`);
          const extras = await enrichRun(api, updated);
          renderDetail(detailEl, updated, hooks, extras);
          hooks.showMsg('Production release approved.', 'success');
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
      async runRelease(run) {
        if (!global.confirm('Run approved release as dry-run? No real merge, deploy, or send.')) return;
        try {
          await fetchJson(api, `/build-runs/${run.id}/run-release`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              actor: 'operator',
              idempotency_key: `dry-run-${run.id}-${Date.now()}`,
            }),
          });
          const updated = await fetchJson(api, `/build-runs/${run.id}`);
          const extras = await enrichRun(api, updated);
          renderDetail(detailEl, updated, hooks, extras);
          hooks.showMsg('Dry-run release completed.', 'success');
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
      async rollbackRelease(run) {
        const reason = global.prompt('Why roll back this release?') || '';
        if (!reason.trim()) {
          hooks.showMsg('Rollback reason is required.', 'warning');
          return;
        }
        try {
          await fetchJson(api, `/build-runs/${run.id}/rollback-release`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actor: 'operator', reason: reason.trim() }),
          });
          const updated = await fetchJson(api, `/build-runs/${run.id}`);
          const extras = await enrichRun(api, updated);
          renderDetail(detailEl, updated, hooks, extras);
          hooks.showMsg('Rollback recorded (dry-run).', 'info');
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
    };

    (async () => {
      try {
        if (initialRunId) await hooks.loadRun(initialRunId);
        else if (detailEl) {
          const projects = await hooks.loadProjects();
          try {
            renderNewTaskForm(detailEl, projects, hooks);
          } catch (formErr) {
            detailEl.innerHTML = '<p class="op-mode-muted small py-3">Unable to load the build task form. Refresh or return to <a href="/ai-coding">AI Coding Home</a>.</p>';
            hooks.showMsg('Could not load the build form. Try again from AI Coding Home.', 'warning');
          }
        }
        await hooks.loadRuns();
      } catch (err) {
        hooks.showMsg(formatError(err), 'danger');
      }
    })();

    if (options.autoRefreshMs > 0 && initialRunId) {
      global.setInterval(() => hooks.loadRun(initialRunId).catch(() => {}), options.autoRefreshMs);
    }

    return hooks;
  }

  global.AiCodingSupervisedBuild = {
    init,
    renderDetail,
    normalizeProjectsList,
    projectIdOf,
    projectLabelOf,
  };
})(window);
