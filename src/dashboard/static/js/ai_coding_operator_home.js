/**
 * Simple Operator Home — /ai-coding current task panel (client-side only).
 */
(function (global) {
  'use strict';

  const OM = () => global.AiCodingOperatorMode;
  const CACHE_KEY = 'ai-coding-operator-last-build';
  const FETCH_TIMEOUT_MS = 12000;
  const LIST_TIMEOUT_MS = 12000;
  const DETAIL_TIMEOUT_MS = 12000;
  const UNAVAILABLE_AFTER_FAILURES = 2;
  const ACTIVE_BUILD = new Set([
    'draft',
    'planning',
    'plan_ready',
    'executing',
    'reviewing',
    'validating',
    'fixing',
    'ready_for_manual_test',
    'operator_declined',
  ]);
  const REFRESH_STATUS_MSG = 'Refreshing status…';
  const DEFAULT_FORBIDDEN =
    'spam, scraping users, hidden sending, auto-send, send-all without approval, bypassing limits, production deploy, secret exposure';

  const EMPTY_HOME_LINE1 = 'Create Your First Build';
  const EMPTY_HOME_LINE2 = 'Describe what you want AI to build.';
  const STEP3_NOTE = 'AI builds. You test. Then approve or send feedback.';
  const UNAVAILABLE_MSG =
    'AI build service is not available yet. You can still use Advanced Task Planner, or retry.';
  const START_BUILD_UNAVAILABLE_MSG =
    'AI build service is unavailable. The task was not started. Try again or use Advanced Task Planner.';
  const SAFETY_CHIPS = [
    'No auto-send',
    'No deploy',
    'No approval bypass',
    'Manual test required',
  ];

  function formatError(err) {
    const om = OM();
    if (om && om.friendlyOperatorError) return om.friendlyOperatorError(err);
    if (!err) return 'Request failed';
    if (typeof err === 'string') return err;
    return err.message || 'Request failed';
  }

  function mergeContinueResponse(run, resp) {
    const om = OM();
    return om && om.mergeOperatorContinue ? om.mergeOperatorContinue(run, resp) : (resp && resp.run) || run;
  }

  function isNotFoundError(err) {
    if (!err) return false;
    const status = err.status;
    const msg = String(err.message || '').toLowerCase();
    return status === 404 || msg === 'not found' || msg === 'not_found';
  }

  function isBuildRunsRouteMissing(err) {
    return !!(err && err.buildRunsRouteMissing);
  }

  function isUnavailableError(err) {
    if (!err) return false;
    const status = err.status;
    if (status === 502 || status === 503 || status === 504) return true;
    const msg = String(err.message || '').toLowerCase();
    return (
      err.name === 'AbortError'
      || /unavailable|upstream|timeout|timed out|network|fetch failed|aborted/i.test(msg)
    );
  }

  function friendlyStartBuildError(err) {
    if (isNotFoundError(err) || isUnavailableError(err)) {
      return START_BUILD_UNAVAILABLE_MSG;
    }
    const raw = formatError(err);
    if (/[\[{]|stack|trace|json|detail:/i.test(raw) || raw.length > 120) {
      return START_BUILD_UNAVAILABLE_MSG;
    }
    return raw;
  }

  async function fetchJson(api, path, options) {
    return global.dashboardFetchJson(`${api}${path}`, options);
  }

  async function fetchJsonTimed(api, path, options, timeoutMs) {
    const ms = timeoutMs || FETCH_TIMEOUT_MS;
    if (typeof AbortController === 'undefined') {
      return Promise.race([
        fetchJson(api, path, options),
        new Promise((_, reject) => {
          global.setTimeout(() => {
            const err = new Error('Request timed out');
            err.status = 504;
            reject(err);
          }, ms);
        }),
      ]);
    }
    const controller = new AbortController();
    const timer = global.setTimeout(() => controller.abort(), ms);
    try {
      const opts = Object.assign({}, options || {}, { signal: controller.signal });
      return await fetchJson(api, path, opts);
    } catch (err) {
      if (err && err.name === 'AbortError') {
        const timeoutErr = new Error('Request timed out');
        timeoutErr.status = 504;
        throw timeoutErr;
      }
      throw err;
    } finally {
      global.clearTimeout(timer);
    }
  }

  function normalizeBuildRunsList(data) {
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.items)) return data.items;
    if (data && Array.isArray(data.runs)) return data.runs;
    return [];
  }

  async function fetchBuildRuns(api) {
    try {
      const data = await fetchJsonTimed(api, '/build-runs?limit=30', null, LIST_TIMEOUT_MS);
      return normalizeBuildRunsList(data);
    } catch (err) {
      if (isNotFoundError(err)) {
        const routeErr = new Error('Build runs API not found');
        routeErr.status = 404;
        routeErr.buildRunsRouteMissing = true;
        throw routeErr;
      }
      throw err;
    }
  }

  function loadCachedBuild() {
    try {
      const raw = global.localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || !parsed.run || !parsed.run.id) return null;
      return parsed;
    } catch (_) {
      return null;
    }
  }

  function saveCachedBuild(run, extras) {
    if (!run || !run.id) return;
    const payload = {
      run,
      extras: extras || { journal: [], program: null },
      savedAt: Date.now(),
    };
    try {
      global.localStorage.setItem(CACHE_KEY, JSON.stringify(payload));
    } catch (_) {}
    return payload;
  }

  function runToSummary(run) {
    return {
      id: run.id,
      status: run.status,
      task_title: run.task_title,
      updated_at: run.updated_at || run.created_at,
      project_id: run.project_id,
      execution_program_id: run.execution_program_id,
    };
  }

  async function checkBuildServiceHealth(api) {
    try {
      const health = await fetchJsonTimed(api, '/health', null, 5000);
      return !!(health && health.connected !== false);
    } catch (_) {
      return false;
    }
  }

  function setRefreshStatusNote(panel, message) {
    if (!panel) return;
    const mount = panel.querySelector('[data-operator-mode="true"]') || panel;
    const existing = mount.querySelector('#hub-refresh-status');
    if (!message) {
      existing?.remove();
      return;
    }
    let note = existing;
    if (!note) {
      note = document.createElement('div');
      note.id = 'hub-refresh-status';
      note.className = 'alert alert-secondary py-1 px-2 small mb-2';
      mount.insertBefore(note, mount.firstChild);
    }
    note.textContent = message;
  }

  function normalizeProjectsList(data) {
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.items)) return data.items;
    return [];
  }

  async function fetchProjectsSafe(api) {
    try {
      const data = await fetchJsonTimed(api, '/projects/summary?limit=50');
      return normalizeProjectsList(data);
    } catch (_) {
      return [];
    }
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

  function hintProjectIdFromRows(rows) {
    if (!rows || !rows.length) return '';
    const sorted = [...rows].sort(
      (a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0)
    );
    for (const row of sorted) {
      if (row && row.project_id) return String(row.project_id).trim();
    }
    return '';
  }

  function resolveProjectSelection(projects, hintProjectId) {
    const list = (projects || []).filter((p) => projectIdOf(p));
    let selectedId = '';
    if (list.length === 1) {
      selectedId = projectIdOf(list[0]);
    } else if (hintProjectId && list.some((p) => projectIdOf(p) === hintProjectId)) {
      selectedId = hintProjectId;
    } else if (list.length > 0) {
      selectedId = projectIdOf(list[0]);
    }
    return {
      list,
      selectedId,
      showVisibleProject: list.length !== 1,
    };
  }

  function getSelectedProjectId() {
    const main = document.getElementById('hub-project-main');
    if (main) return main.value.trim();
    const hidden = document.getElementById('hub-project');
    return hidden ? String(hidden.value || '').trim() : '';
  }

  function openDevAdvancedPanel() {
    if (typeof global.openAiCodingDevAdvanced === 'function') {
      global.openAiCodingDevAdvanced();
      return;
    }
    const details = document.getElementById('ai-dev-advanced-panel');
    if (!details) return;
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    details.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function collapseDevAdvancedPanel() {
    const details = document.getElementById('ai-dev-advanced-panel');
    if (!details) return;
    details.removeAttribute('open');
    details.open = false;
    const hash = global.location.hash;
    if (hash === '#ai-dev-advanced' || hash === '#ai-dev-advanced-panel') {
      global.history.replaceState(null, '', global.location.pathname + global.location.search);
    }
  }

  function wireAdvancedToolsLink(root) {
    root?.querySelectorAll('[data-open-dev-advanced]').forEach((link) => {
      link.addEventListener('click', (ev) => {
        ev.preventDefault();
        openDevAdvancedPanel();
      });
    });
  }

  function pickCurrentRun(rows, cachedRun) {
    if (rows && rows.length) {
      const active = rows.filter((r) => ACTIVE_BUILD.has(r.status));
      if (active.length) {
        return active.sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at))[0];
      }
    }
    if (cachedRun && cachedRun.id) {
      return runToSummary(cachedRun);
    }
    return null;
  }

  async function enrichRun(api, run) {
    const extras = { journal: [], program: null };
    try {
      if (run.id) {
        extras.journal = await fetchJsonTimed(api, `/build-runs/${run.id}/journal`, null, 5000);
      }
    } catch (_) {}
    try {
      if (run.execution_program_id) {
        extras.program = await fetchJsonTimed(api, `/execution-programs/${run.execution_program_id}`, null, 5000);
        if (extras.program && run.manual_test_report) {
          run.manual_test_report.completed_phases = extras.program.completed_phases;
          run.manual_test_report.total_phases = extras.program.total_phases;
        }
      }
    } catch (_) {}
    return extras;
  }

  function sortBuildRows(rows) {
    return [...(rows || [])].sort(
      (a, b) => new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0)
    );
  }

  function renderHomeSection(title, iconClass, bodyHtml, emptyText, esc) {
    return `<section class="op-home-section">
      <div class="op-home-section-title"><i class="bi ${iconClass}"></i> ${esc(title)}</div>
      ${bodyHtml || `<p class="op-home-section-empty">${esc(emptyText)}</p>`}
    </section>`;
  }

  function renderBlockedMiniCard(run, om) {
    const esc = om.esc;
    const card = om.renderOperatorBuildMiniCard(run);
    const reason = om.blockedReasonLabel(run);
    return card.replace('</a>', `<div class="small mt-1" style="color:#fca5a5">${esc(reason)}</div></a>`);
  }

  function renderOperatorDashboard(container, ctx, hooks) {
    const om = OM();
    if (!om || !om.renderOperatorHomeHero) return false;
    om.ensureOperatorStyles();
    const esc = om.esc;
    const rows = sortBuildRows(ctx.allRows || []);
    const current = ctx.currentRun || null;
    const currentId = current && current.id ? String(current.id) : '';

    const readyForTest = rows.filter(
      (r) => r.status === 'ready_for_manual_test' && String(r.id) !== currentId
    );
    const blocked = rows.filter(
      (r) => om.isRealOperatorBlocker(r) && String(r.id) !== currentId
    );
    const recent = rows.slice(0, 5);

    const miniGrid = (list, mapper) => {
      if (!list.length) return '';
      const cards = list.map((r) => (mapper ? mapper(r) : om.renderOperatorBuildMiniCard(r))).join('');
      return `<div class="op-home-build-grid">${cards}</div>`;
    };

    let html = '<div class="op-home-dashboard" data-operator-home="dashboard">';
    if (current) {
      html += om.renderOperatorHomeHero(current);
    }
    html += renderHomeSection(
      'Ready For Test',
      'bi-clipboard2-check',
      miniGrid(readyForTest),
      'No builds waiting for your test.',
      esc
    );
    html += renderHomeSection(
      'Blocked',
      'bi-exclamation-octagon',
      miniGrid(blocked, (r) => renderBlockedMiniCard(r, om)),
      'Nothing blocked right now.',
      esc
    );
    html += renderHomeSection(
      'Recent Builds',
      'bi-clock-history',
      miniGrid(recent),
      'No builds yet.',
      esc
    );
    html += '</div>';
    html += '<p class="text-center mt-2 mb-0"><a href="#" class="small op-mode-muted" data-open-dev-advanced>View advanced / developer tools</a></p>';

    container.innerHTML = html;
    wireAdvancedToolsLink(container);
    return true;
  }

  function renderStartCard(container, projects, hooks, options) {
    const om = OM();
    const esc = om.esc;
    const showEmptyNotice = options && options.showEmptyNotice;
    const hintProjectId = (options && options.hintProjectId) || '';
    const { list, selectedId, showVisibleProject } = resolveProjectSelection(projects, hintProjectId);
    const projectOptions = list
      .map((p) => `<option value="${esc(projectIdOf(p))}">${projectLabelOf(p, esc)}</option>`)
      .join('');
    const safetyChips = SAFETY_CHIPS.map((label) => `<span class="hub-safety-chip">${esc(label)}</span>`).join('');
    const projectStepBlock = showVisibleProject
      ? `<div class="mb-2" id="hub-project-main-wrap">
          <label class="form-label small mb-1">Project</label>
          <select class="form-select form-select-sm" id="hub-project-main">
            <option value="">Select a project…</option>
            ${projectOptions}
          </select>
        </div>`
      : (selectedId
        ? `<p class="op-mode-muted small mb-2">Project selected automatically.</p>
           <input type="hidden" id="hub-project" value="${esc(selectedId)}" />`
        : `<div class="mb-2" id="hub-project-main-wrap">
          <label class="form-label small mb-1">Project</label>
          <select class="form-select form-select-sm" id="hub-project-main" disabled>
            <option value="">No projects available</option>
          </select>
        </div>`);
    const emptyBlock = showEmptyNotice
      ? `<div class="hub-empty-lines">
          <p class="line1">${esc(EMPTY_HOME_LINE1)}</p>
          <p class="line2">${esc(EMPTY_HOME_LINE2)}</p>
        </div>`
      : '';
    container.innerHTML = `
      <div class="op-mode-panel hub-flow-card" data-operator-home="start">
        <div class="op-mode-kicker">Simple Operator Mode</div>
        ${emptyBlock}
        <div class="hub-step">
          <div class="hub-step-label">1. Describe the task</div>
          ${projectStepBlock}
          <div class="mb-2">
            <label class="form-label small mb-1">Task title</label>
            <input class="form-control" id="hub-task-title" placeholder="Example: Improve AI Coding empty state" />
          </div>
          <div class="mb-0">
            <label class="form-label small mb-1">What should AI build?</label>
            <textarea class="form-control" id="hub-task-goal" rows="3" placeholder="Example: Make the empty state clearer for non-technical operators. Do not change backend or workflow logic."></textarea>
            <p class="op-mode-muted small mt-1 mb-0">One or two sentences is enough.</p>
          </div>
        </div>
        <div class="hub-step">
          <div class="hub-step-label">2. Safety limits</div>
          <div class="hub-safety-chips">${safetyChips}</div>
        </div>
        <div class="hub-step">
          <div class="hub-step-label">3. Start</div>
          <button type="button" class="btn btn-primary btn-lg w-100" id="hub-start-build">Start Building</button>
          <p class="op-mode-muted small mt-2 mb-0 text-center">${esc(STEP3_NOTE)}</p>
        </div>
        <details class="op-mode-advanced mt-3">
          <summary>Advanced options</summary>
          <div class="mt-2 row g-2">
            <div class="col-12">
              <label class="form-label small">Forbidden scope</label>
              <input class="form-control form-control-sm" id="hub-forbidden" value="${esc(DEFAULT_FORBIDDEN)}" />
            </div>
            <div class="col-md-6">
              <label class="form-label small">Operator notes</label>
              <input class="form-control form-control-sm" id="hub-notes" />
            </div>
            <div class="col-md-6">
              <label class="form-label small">Max fix attempts</label>
              <input type="number" class="form-control form-control-sm" id="hub-max-attempts" value="3" min="1" max="10" />
            </div>
          </div>
        </details>
      </div>
      <p class="text-center mt-2"><a href="#" class="small op-mode-muted" data-open-dev-advanced>View advanced / developer tools</a></p>`;
    const mainSel = document.getElementById('hub-project-main');
    if (mainSel && selectedId) mainSel.value = selectedId;
    mainSel?.addEventListener('change', () => {
      const hidden = document.getElementById('hub-project');
      if (hidden) hidden.value = mainSel.value;
    });
    document.getElementById('hub-start-build')?.addEventListener('click', () => hooks.startBuild());
    wireAdvancedToolsLink(container);
  }

  function renderUnavailablePanel(container, hooks) {
    const om = OM();
    const esc = om.esc;
    container.innerHTML = `
      <div class="op-mode-panel" data-operator-home="unavailable">
        <div class="op-mode-kicker"><i class="bi bi-person-workspace"></i> Simple Operator Mode</div>
        <p class="text-warning small mb-3">${esc(UNAVAILABLE_MSG)}</p>
        <div class="d-flex flex-wrap gap-2">
          <button type="button" class="btn btn-outline-light btn-sm" id="hub-retry">Retry</button>
          <a class="btn btn-outline-info btn-sm" href="/ai-coding/planner">Advanced Task Planner</a>
        </div>
      </div>
      <p class="text-center mt-2"><a href="#" class="small op-mode-muted" data-open-dev-advanced>View advanced / developer tools</a></p>`;
    document.getElementById('hub-retry')?.addEventListener('click', () => hooks.refresh());
    wireAdvancedToolsLink(container);
  }

  function renderTaskDetail(panel, run, hooks, extras) {
    const om = OM();
    if (!om || !om.renderOperatorSimplePanel) {
      return false;
    }
    om.renderOperatorSimplePanel(panel, run, extras || {});
    panel.querySelector('.op-mode-panel')?.insertAdjacentHTML(
      'beforeend',
      '<div class="mt-2 text-center"><a href="#" class="small op-mode-muted" id="hub-open-builds">Open full task view</a></div>'
    );
    const advLink = document.createElement('p');
    advLink.className = 'text-center mt-2 mb-0';
    advLink.innerHTML = '<a href="#" class="small op-mode-muted" data-open-dev-advanced>View advanced / developer tools</a>';
    panel.appendChild(advLink);
    wireAdvancedToolsLink(panel);
    renameHomeDetailIds(panel);
    bindHomeActions(run, hooks, panel);
    if (om.bindReleaseActions) om.bindReleaseActions(panel, run, hooks);
    return true;
  }

  function renameHomeDetailIds(panel) {
    const map = [
      ['sb-primary', 'hub-primary'],
      ['sb-refresh', 'hub-refresh'],
      ['sb-problems', 'hub-problems'],
      ['sb-decline-submit', 'hub-decline-submit'],
      ['sb-feedback-wrap', 'hub-feedback-wrap'],
      ['sb-decline-feedback', 'hub-decline-feedback'],
    ];
    map.forEach(([from, to]) => {
      const el = panel.querySelector(`#${from}`);
      if (el) el.id = to;
    });
  }

  function bindHomeActions(run, hooks, panelEl) {
    const om = OM();
    const scope = panelEl || document;
    const action = om.primaryAction(run);
    const panel = scope.querySelector('[data-operator-mode="true"]');

    function showFeedbackForm() {
      const wrap = scope.querySelector('#hub-feedback-wrap, #sb-feedback-wrap');
      if (wrap) {
        wrap.style.display = 'block';
        wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }

    scope.querySelector('#hub-primary, #sb-primary')?.addEventListener('click', () => {
      if (action.id === 'approve') hooks.approve(run);
      else if (action.id === 'advance') hooks.advance(run);
      else if (action.id === 'problems') showFeedbackForm();
      else if (action.id === 'retry') hooks.refresh();
      else if (action.id === 'release_prepare') hooks.prepareRelease(run);
      else if (action.id === 'release_approve') hooks.approveRelease(run, scope);
      else if (action.id === 'release_run') hooks.runRelease(run);
      else if (action.id === 'release_rollback') hooks.rollbackRelease(run);
    });
    panel?.querySelectorAll('[data-action="send-feedback"]').forEach((link) => {
      link.addEventListener('click', (ev) => {
        ev.preventDefault();
        showFeedbackForm();
      });
    });
    scope.querySelector('#hub-decline-submit, #sb-decline-submit')?.addEventListener('click', () => hooks.submitFeedback(run));
    if (om && om.bindManualTestPackageActions) {
      om.bindManualTestPackageActions(scope, run, hooks);
    }
    scope.querySelector('#hub-refresh, #sb-refresh')?.addEventListener('click', () => hooks.refresh());
    scope.querySelector('#hub-open-builds')?.addEventListener('click', (e) => {
      e.preventDefault();
      global.location.href = `/ai-coding/builds/${run.id}`;
    });
  }

  function init(options) {
    const api = options.api;
    const panel = document.getElementById(options.panelId || 'ai-operator-home');
    if (!panel || !OM()) return null;

    let currentRun = null;
    let lastGoodRun = null;
    let lastGoodExtras = { journal: [], program: null };
    let refreshSeq = 0;
    let refreshFailures = 0;
    let lastBuildRows = [];

    function rememberGoodRun(run, extras) {
      if (!run || !run.id) return;
      currentRun = run;
      lastGoodRun = run;
      lastGoodExtras = extras || { journal: [], program: null };
      saveCachedBuild(run, lastGoodExtras);
    }

    function restoreLastKnownPanel() {
      const cached = loadCachedBuild();
      const run = (cached && cached.run) || lastGoodRun;
      const extras = (cached && cached.extras) || lastGoodExtras;
      if (!run || !run.id) return false;
      rememberGoodRun(run, extras);
      if (!renderOperatorDashboard(panel, {
        currentRun: run,
        allRows: lastBuildRows.length ? lastBuildRows : [runToSummary(run)],
        extras,
      }, hooks)) return false;
      return true;
    }

    async function showEmptyStart(hintProjectId, buildRows) {
      const projects = await fetchProjectsSafe(api);
      const hint = hintProjectId || hintProjectIdFromRows(buildRows || lastBuildRows);
      renderStartCard(panel, projects, hooks, { showEmptyNotice: true, hintProjectId: hint });
      currentRun = null;
      setRefreshStatusNote(panel, null);
    }

    async function attachReleaseStatus(run, extras) {
      const om = OM();
      if (!om || !om.fetchReleaseStatus) return extras;
      const releaseStatus = await om.fetchReleaseStatus(api, run);
      if (releaseStatus) {
        run.release_status = releaseStatus;
      }
      return Object.assign({}, extras || {}, { releaseStatus });
    }

    async function enrichRunInBackground(run, seq) {
      try {
        const extras = await enrichRun(api, run);
        const withRelease = await attachReleaseStatus(run, extras);
        if (seq !== refreshSeq) return;
        rememberGoodRun(run, withRelease);
        if (panel.querySelector('[data-operator-home="dashboard"]')) {
          renderOperatorDashboard(panel, {
            currentRun: run,
            allRows: lastBuildRows.length ? lastBuildRows : [runToSummary(run)],
            extras: withRelease,
          }, hooks);
          setRefreshStatusNote(panel, null);
        }
      } catch (_) {
        /* extras are optional; keep last good panel */
      }
    }

    async function loadCurrentTask(ctx) {
      const seq = ctx && ctx.refreshSeq != null ? ctx.refreshSeq : refreshSeq;
      const cachedPayload = loadCachedBuild();
      const cachedRun = cachedPayload && cachedPayload.run;

      let rows = [];
      let listErr = null;
      try {
        rows = await fetchBuildRuns(api);
        lastBuildRows = rows;
      } catch (err) {
        listErr = err;
        if (isBuildRunsRouteMissing(err)) throw err;
        if (lastBuildRows.length) {
          rows = lastBuildRows;
        }
      }

      let summary = pickCurrentRun(rows, cachedRun);
      if (!summary && (!rows || !rows.length)) {
        if (listErr && !cachedRun) throw listErr;
        await showEmptyStart(null, rows);
        return;
      }

      let run = null;
      if (summary) {
        try {
          run = await fetchJsonTimed(api, `/build-runs/${summary.id}`, null, DETAIL_TIMEOUT_MS);
        } catch (detailErr) {
          if (cachedRun && String(cachedRun.id) === String(summary.id)) {
            run = cachedRun;
          } else if (isNotFoundError(detailErr)) {
            run = summary;
          } else {
            throw detailErr;
          }
        }
        rememberGoodRun(run, lastGoodExtras);
      }

      let extras = { journal: [], program: null };
      if (run) {
        extras = await attachReleaseStatus(run, extras);
      }
      if (!renderOperatorDashboard(panel, {
        currentRun: run,
        allRows: rows,
        extras,
      }, hooks)) {
        if (run && run.id) {
          global.location.href = `/ai-coding/builds/${run.id}`;
        }
        return;
      }
      setRefreshStatusNote(panel, null);
      if (run) enrichRunInBackground(run, seq);
    }

    async function handleRefreshFailure(err, seq) {
      if (seq !== refreshSeq) return;

      const hasCached = restoreLastKnownPanel();
      if (hasCached) {
        setRefreshStatusNote(panel, REFRESH_STATUS_MSG);
        refreshFailures += 1;
        return;
      }

      refreshFailures += 1;

      if (isBuildRunsRouteMissing(err)) {
        renderUnavailablePanel(panel, hooks);
        currentRun = null;
        return;
      }

      if (isNotFoundError(err) && !lastGoodRun) {
        await showEmptyStart();
        return;
      }

      const healthy = await checkBuildServiceHealth(api);
      if (
        refreshFailures >= UNAVAILABLE_AFTER_FAILURES
        && !healthy
        && !lastGoodRun
      ) {
        renderUnavailablePanel(panel, hooks);
        currentRun = null;
        return;
      }

      if (lastGoodRun && restoreLastKnownPanel()) {
        setRefreshStatusNote(panel, REFRESH_STATUS_MSG);
        return;
      }

      if (isUnavailableError(err) || isNotFoundError(err)) {
        await showEmptyStart();
        return;
      }

      renderUnavailablePanel(panel, hooks);
      currentRun = null;
    }

    const hooks = {
      showMsg: (msg, tone) => options.alertFn && options.alertFn(msg, tone),
      async refresh() {
        const seq = ++refreshSeq;
        const hasPanel = !!panel.querySelector('[data-operator-home="dashboard"], [data-operator-home="start"]');
        if (!hasPanel && !loadCachedBuild() && !lastGoodRun) {
          panel.innerHTML =
            '<div class="op-mode-muted py-4 text-center"><span class="spinner-border spinner-border-sm"></span> Loading current task…</div>';
        } else {
          setRefreshStatusNote(panel, REFRESH_STATUS_MSG);
        }
        try {
          await loadCurrentTask({ refreshSeq: seq });
          if (seq !== refreshSeq) return;
          refreshFailures = 0;
          setRefreshStatusNote(panel, null);
        } catch (err) {
          await handleRefreshFailure(err, seq);
        }
      },
      async startBuild() {
        const title = document.getElementById('hub-task-title')?.value?.trim();
        const goal = document.getElementById('hub-task-goal')?.value?.trim();
        const projectId = getSelectedProjectId();
        if (!title || !goal) {
          hooks.showMsg('Task title and description are required.', 'warning');
          return;
        }
        if (!projectId) {
          hooks.showMsg('Choose a project for this supervised build.', 'warning');
          return;
        }
        if (!global.confirm('Start building? AI will prepare the work. You test manually before anything goes live.')) return;
        const forbidden = (document.getElementById('hub-forbidden')?.value || DEFAULT_FORBIDDEN)
          .split(',').map((s) => s.trim()).filter(Boolean);
        try {
          const created = await fetchJson(api, '/build-runs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              project_id: projectId,
              task_title: title,
              task_goal: goal,
              forbidden_scope: forbidden,
              operator_notes: document.getElementById('hub-notes')?.value || null,
              max_attempts: Number(document.getElementById('hub-max-attempts')?.value || 3),
            }),
          });
          global.location.href = `/ai-coding/builds/${created.id}`;
        } catch (err) {
          hooks.showMsg(friendlyStartBuildError(err), 'danger');
        }
      },
      async advance(run) {
        try {
          const resp = await fetchJson(api, `/build-runs/${run.id}/operator-continue`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actor: 'operator' }),
          });
          await hooks.refresh();
          const msg = resp.friendly_message
            || (resp.advanced ? 'AI work continued.' : 'AI is waiting for your input.');
          hooks.showMsg(msg, resp.advanced ? 'success' : 'info');
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
          await hooks.refresh();
        }
      },
      async approve(run) {
        if (!global.confirm('You manually tested and approve this build?')) return;
        try {
          await fetchJson(api, `/build-runs/${run.id}/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ comment: 'Manual test passed' }),
          });
          await hooks.refresh();
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
        const fieldId = isManualTest ? 'hub-decline-feedback' : 'hub-decline-feedback';
        const message = (
          document.getElementById('hub-decline-feedback')
          || document.getElementById('sb-decline-feedback')
        )?.value?.trim();
        if (!message) {
          hooks.showMsg(isManualTest ? 'Describe what failed manual testing.' : 'Enter notes for AI.', 'warning');
          return;
        }
        try {
          if (isManualTest) {
            await fetchJson(api, `/build-runs/${run.id}/decline`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ feedback: message }),
            });
            hooks.showMsg('Feedback sent.', 'info');
          } else {
            await fetchJson(api, `/build-runs/${run.id}/feedback`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                message,
                source: 'operator_pre_manual_test',
              }),
            });
            hooks.showMsg('Notes sent to AI.', 'info');
          }
          await hooks.refresh();
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
          await hooks.refresh();
          hooks.showMsg('Release plan prepared. Review before approving.', 'success');
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
      async approveRelease(run, scopeEl) {
        const scope = scopeEl || panel;
        const confirmed = scope.querySelector('#sb-release-confirm, #hub-release-confirm')?.checked;
        if (!confirmed) {
          hooks.showMsg('Check the box to confirm production release approval.', 'warning');
          return;
        }
        try {
          const resp = await fetchJson(api, `/build-runs/${run.id}/approve-release`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              actor: 'operator',
              operator_confirmed: true,
              confirmation_text: 'I approve this production release.',
            }),
          });
          run.release_status = resp;
          await hooks.refresh();
          hooks.showMsg('Production release approved. Dry run required before any live action.', 'success');
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
      async runRelease(run) {
        if (!global.confirm('Run approved release as dry-run? No real merge, deploy, or send will occur.')) return;
        try {
          const key = `dry-run-${run.id}-${Date.now()}`;
          const resp = await fetchJson(api, `/build-runs/${run.id}/run-release`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actor: 'operator', idempotency_key: key }),
          });
          run.release_status = resp;
          await hooks.refresh();
          hooks.showMsg('Dry-run release completed. See audit log in release card.', 'success');
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
          const resp = await fetchJson(api, `/build-runs/${run.id}/rollback-release`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actor: 'operator', reason: reason.trim() }),
          });
          run.release_status = resp;
          await hooks.refresh();
          hooks.showMsg('Rollback recorded (dry-run).', 'info');
        } catch (err) {
          hooks.showMsg(formatError(err), 'danger');
        }
      },
    };

    collapseDevAdvancedPanel();

    if (restoreLastKnownPanel()) {
      setRefreshStatusNote(panel, REFRESH_STATUS_MSG);
    }
    hooks.refresh();
    if (options.autoRefreshMs > 0) {
      global.setInterval(() => hooks.refresh().catch(() => {}), options.autoRefreshMs);
    }
    return hooks;
  }

  global.AiCodingOperatorHome = {
    init,
    pickCurrentRun,
    runToSummary,
    loadCachedBuild,
    saveCachedBuild,
    normalizeProjectsList,
    resolveProjectSelection,
    projectIdOf,
    projectLabelOf,
    hintProjectIdFromRows,
    renderOperatorDashboard,
    sortBuildRows,
    CACHE_KEY,
    ACTIVE_BUILD,
    FETCH_TIMEOUT_MS,
    LIST_TIMEOUT_MS,
    DETAIL_TIMEOUT_MS,
    UNAVAILABLE_AFTER_FAILURES,
    REFRESH_STATUS_MSG,
    EMPTY_HOME_LINE1,
    EMPTY_HOME_LINE2,
    STEP3_NOTE,
    SAFETY_CHIPS,
    UNAVAILABLE_MSG,
    START_BUILD_UNAVAILABLE_MSG,
  };
})(window);
