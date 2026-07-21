/**
 * P14.1 — Factory Health Dashboard (read-only analytics).
 */
(function (global) {
  'use strict';

  const API = global.AI_CODING_FACTORY_API || '/api/v1/ai-coding';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function pct(n) {
    if (n == null || Number.isNaN(n)) return '—';
    return `${Math.round(Number(n) * 100)}%`;
  }

  function num(n) {
    return n == null ? '—' : String(n);
  }

  function sec(n) {
    if (n == null) return '—';
    const v = Number(n);
    if (v < 60) return `${Math.round(v)}s`;
    return `${Math.round(v / 60)}m`;
  }

  function healthClass(status) {
    if (status === 'healthy') return 'fa-health-healthy';
    if (status === 'warning') return 'fa-health-warning';
    return 'fa-health-critical';
  }

  function barCard(title, value, max, tone) {
    const v = Number(value) || 0;
    const m = Math.max(Number(max) || 1, v, 1);
    const width = Math.min(100, Math.round((v / m) * 100));
    return `<div class="col-6 col-md-4 col-xl-3">
      <div class="fa-glass fa-kpi p-3 h-100">
        <div class="label">${esc(title)}</div>
        <div class="value">${esc(String(v))}</div>
        <div class="fa-bar-track mt-2"><div class="fa-bar-fill fa-bar-${tone}" style="width:${width}%"></div></div>
      </div>
    </div>`;
  }

  function kpiCard(title, value, sub) {
    return `<div class="col-6 col-md-4 col-xl-2">
      <div class="fa-glass fa-kpi p-3 h-100">
        <div class="label">${esc(title)}</div>
        <div class="value">${esc(String(value))}</div>
        ${sub ? `<div class="small fa-muted mt-1">${esc(sub)}</div>` : ''}
      </div>
    </div>`;
  }

  async function fetchJson(path) {
    return global.dashboardFetchJson(`${API}${path}`);
  }

  function showAlert(msg, tone) {
    const el = document.getElementById('fa-alert');
    if (!el) return;
    el.innerHTML = msg
      ? `<div class="alert alert-${tone || 'warning'} py-2 small">${esc(msg)}</div>`
      : '';
  }

  function renderHealthBanner(data) {
    const h = data.factory_health || {};
    const label = document.getElementById('fa-overall-label');
    const summary = document.getElementById('fa-overall-summary');
    const ts = document.getElementById('fa-generated-at');
    const status = h.overall_health || 'unknown';
    const text = status === 'healthy' ? 'Factory Healthy' : status === 'warning' ? 'Factory Warning' : 'Factory Critical';
    if (label) {
      label.className = `fs-4 fw-semibold ${healthClass(status)}`;
      label.textContent = text;
    }
    if (summary) summary.textContent = h.summary || '—';
    if (ts) ts.textContent = data.generated_at ? `Updated ${data.generated_at}` : '—';

    const kpis = document.getElementById('fa-health-kpis');
    if (kpis) {
      kpis.innerHTML = [
        kpiCard('Projects', h.projects_total, `${h.projects_healthy || 0} healthy`),
        kpiCard('Workers', h.workers_healthy || 0, `${h.workers_blocked || 0} blocked`),
        kpiCard('Queued jobs', h.jobs_queued || 0, `${h.jobs_retrying || 0} retrying`),
        kpiCard('Builds running', h.builds_running || 0, `${h.builds_ready_for_test || 0} ready for test`),
        kpiCard('Releases blocked', h.releases_blocked || 0, `${h.releases_executed || 0} executed`),
      ].join('');
    }
  }

  function renderBuilds(b) {
    const el = document.getElementById('fa-build-section');
    if (!el) return;
    const max = Math.max(b.builds_total || 0, b.builds_last_24h || 0, 1);
    el.innerHTML = [
      kpiCard('Total builds', b.builds_total, `${b.builds_last_24h || 0} last 24h`),
      kpiCard('Success rate', pct(b.build_success_rate), `Avg ${sec(b.average_build_duration_sec)}`),
      kpiCard('Completed', b.builds_completed, `${b.builds_last_7d || 0} last 7d`),
      barCard('Failed builds', b.builds_failed, max, 'bad'),
      barCard('Blocked builds', b.builds_blocked, max, 'warn'),
      kpiCard('Auto-fix validated', b.auto_fix_validated, `${b.auto_fix_blocked || 0} blocked`),
    ].join('');
  }

  function renderWorkers(w) {
    const el = document.getElementById('fa-worker-section');
    if (!el) return;
    el.innerHTML = [
      kpiCard('Workers', w.worker_count, `${w.healthy_workers || 0} healthy`),
      barCard('Stale workers', w.stale_workers || 0, Math.max(w.worker_count, 1), 'warn'),
      kpiCard('Jobs completed', w.jobs_completed, `Avg runtime ${sec(w.average_job_runtime_sec)}`),
      kpiCard('Jobs failed', w.jobs_failed, `${w.jobs_retrying || 0} retrying`),
      kpiCard('Oldest running', sec(w.oldest_running_job_age_sec), w.worker_mode || 'daemon'),
      kpiCard('Last heartbeat', w.last_worker_heartbeat ? 'Recent' : 'None', w.inline_mode_enabled ? 'inline mode' : 'daemon mode'),
    ].join('');
  }

  function renderQueue(q) {
    const el = document.getElementById('fa-queue-section');
    if (!el) return;
    const depth = (q.queued_jobs || 0) + (q.running_jobs || 0) + (q.retry_jobs || 0);
    const max = Math.max(depth, q.blocked_jobs || 0, 1);
    el.innerHTML = [
      barCard('Queue depth', depth, max, 'info'),
      barCard('Queued', q.queued_jobs || 0, max, 'info'),
      barCard('Running', q.running_jobs || 0, max, 'ok'),
      barCard('Retrying', q.retry_jobs || 0, max, 'warn'),
      barCard('Blocked', q.blocked_jobs || 0, max, 'bad'),
      kpiCard('Oldest queued', sec(q.oldest_queued_job_age_sec), `Wait avg ${sec(q.average_wait_time_sec)}`),
      kpiCard('Exec avg', sec(q.average_execution_time_sec), `${q.stale_running_jobs || 0} stale running`),
    ].join('');
  }

  function renderProjects(p) {
    const summary = document.getElementById('fa-project-summary');
    const dist = p.health_distribution || {};
    if (summary) {
      summary.innerHTML = [
        barCard('Healthy', dist.healthy || 0, p.projects.length || 1, 'ok'),
        barCard('Warning', dist.warning || 0, p.projects.length || 1, 'warn'),
        barCard('Critical', dist.critical || 0, p.projects.length || 1, 'bad'),
      ].join('');
    }
    const tbody = document.getElementById('fa-project-rows');
    if (!tbody) return;
    const rows = p.projects || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="fa-muted">No projects registered.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map((row) => `<tr>
      <td>${esc(row.project_name)}</td>
      <td class="${healthClass(row.health_status)}">${esc(row.health_status)}</td>
      <td>${esc(num(row.build_count))}</td>
      <td>${esc(pct(row.success_rate))}</td>
      <td>${esc(num(row.failed_builds))}</td>
      <td>${esc(num(row.active_jobs))}</td>
      <td class="small fa-muted">${esc(row.last_activity || '—')}</td>
    </tr>`).join('');
  }

  function renderReleases(r) {
    const el = document.getElementById('fa-release-section');
    if (!el) return;
    el.innerHTML = [
      kpiCard('Dry runs', r.dry_runs_generated, `${r.release_packages_generated || 0} prepared`),
      kpiCard('Approvals granted', r.approvals_granted, `${r.approvals_requested || 0} requested`),
      barCard('Release blocked', r.release_blocked || 0, Math.max(r.release_attempts || 1, 1), 'warn'),
      kpiCard('Release success', r.release_success, `${r.release_attempts || 0} attempts`),
    ].join('');
  }

  async function refresh() {
    showAlert('', '');
    try {
      const data = await fetchJson('/analytics/factory');
      renderHealthBanner(data);
      renderBuilds(data.builds || {});
      renderWorkers(data.workers || {});
      renderQueue(data.queue || {});
      renderProjects(data.projects || {});
      renderReleases(data.releases || {});
    } catch (err) {
      showAlert(err && err.message ? err.message : 'Failed to load factory analytics', 'danger');
    }
  }

  document.getElementById('fa-refresh')?.addEventListener('click', refresh);
  refresh();
})(window);
