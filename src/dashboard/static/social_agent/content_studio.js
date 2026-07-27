(function () {
  const state = {
    contentId: null,
    variantId: null,
    platform: "facebook",
    previews: [],
  };

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function meter() {
    const el = document.getElementById("sa-brief");
    const m = document.getElementById("sa-char-meter");
    if (el && m) m.textContent = el.value.length + " chars";
  }

  async function loadDrafts() {
    const listEl = document.getElementById("sa-draft-list");
    const data = await SocialAgent.fetch("/content");
    const items = data.items || [];
    if (!items.length) {
      listEl.innerHTML = "<div class='sa-empty'>No drafts yet. Generate variants from a brief.</div>";
      return;
    }
    listEl.innerHTML = items
      .map(
        (it) =>
          "<button type='button' class='sa-draft-row' data-open='" +
          it.id +
          "'>" +
          "<span class='sa-status'>" +
          escapeHtml(it.status) +
          "</span>" +
          "<strong>#" +
          it.id +
          " · " +
          escapeHtml(it.title || "Untitled") +
          "</strong>" +
          "</button>"
      )
      .join("");
    listEl.querySelectorAll("[data-open]").forEach((btn) =>
      btn.addEventListener("click", () => openDraft(Number(btn.getAttribute("data-open"))))
    );
  }

  function renderTabs() {
    const tabs = document.getElementById("sa-platform-tabs");
    tabs.innerHTML = (state.previews || [])
      .map((p) => {
        const active = p.platform === state.platform ? " active" : "";
        return (
          "<button type='button' class='sa-tab" +
          active +
          "' data-platform='" +
          escapeHtml(p.platform) +
          "'>" +
          escapeHtml(p.label) +
          "</button>"
        );
      })
      .join("");
    tabs.querySelectorAll("[data-platform]").forEach((btn) =>
      btn.addEventListener("click", () => {
        state.platform = btn.getAttribute("data-platform");
        renderTabs();
        renderPreview();
      })
    );
  }

  function renderPreview() {
    const frame = document.getElementById("sa-preview-frame");
    const p = (state.previews || []).find((x) => x.platform === state.platform);
    if (!p) {
      frame.innerHTML = "<div class='sa-empty'>No preview.</div>";
      return;
    }
    state.variantId = p.variant_id;
    const bodyEl = document.getElementById("sa-variant-body");
    if (bodyEl && document.activeElement !== bodyEl) bodyEl.value = p.body || "";
    const ok = p.validation && p.validation.ok;
    frame.innerHTML =
      "<div class='sa-preview-card' data-platform='" +
      escapeHtml(p.platform) +
      "'>" +
      "<div class='sa-preview-meta'>" +
      escapeHtml(p.label) +
      " · " +
      p.char_count +
      "/" +
      p.char_limit +
      " · " +
      (ok ? "valid" : "needs edit") +
      "</div>" +
      "<div class='sa-preview-body'>" +
      escapeHtml(p.body || "") +
      "</div>" +
      "<pre class='sa-preview-json'>" +
      escapeHtml(JSON.stringify(p.preview || {}, null, 2)) +
      "</pre>" +
      "</div>";
  }

  function renderStatusActions(status) {
    const wrap = document.getElementById("sa-status-actions");
    const transitions = {
      DRAFT: ["NEEDS_REVIEW", "ARCHIVED"],
      NEEDS_REVIEW: ["APPROVED", "REJECTED", "DRAFT"],
      APPROVED: ["NEEDS_REVIEW", "ARCHIVED"],
      REJECTED: ["DRAFT", "NEEDS_REVIEW"],
      ARCHIVED: ["DRAFT"],
    };
    const next = transitions[status] || [];
    wrap.innerHTML = next
      .map(
        (s) =>
          "<button type='button' class='btn btn-outline-light btn-sm' data-status='" +
          s +
          "'>" +
          s.replace("_", " ") +
          "</button>"
      )
      .join("");
    wrap.querySelectorAll("[data-status]").forEach((btn) =>
      btn.addEventListener("click", () => transition(btn.getAttribute("data-status")))
    );
  }

  async function openDraft(id) {
    state.contentId = id;
    document.getElementById("sa-active-editor").hidden = false;
    const data = await SocialAgent.fetch("/content/" + id);
    document.getElementById("sa-active-title").textContent =
      "Draft #" + data.item.id + " · " + (data.item.status || "");
    renderStatusActions(data.item.status || "DRAFT");
    const preview = await SocialAgent.fetch("/content/" + id + "/studio-preview");
    state.previews = preview.previews || [];
    if (!state.previews.find((p) => p.platform === state.platform) && state.previews[0]) {
      state.platform = state.previews[0].platform;
    }
    renderTabs();
    renderPreview();
    const events = await SocialAgent.fetch("/content/" + id + "/status-events");
    document.getElementById("sa-audit-trail").textContent = (events.events || [])
      .map((e) => (e.from_status || "∅") + " → " + e.to_status + (e.actor ? " · " + e.actor : ""))
      .join(" · ");
  }

  async function transition(status) {
    if (!state.contentId) return;
    await SocialAgent.fetch("/content/" + state.contentId + "/status", {
      method: "POST",
      body: JSON.stringify({ status }),
    });
    await openDraft(state.contentId);
    await loadDrafts();
  }

  async function saveVariant() {
    if (!state.contentId || !state.variantId) return;
    const body = document.getElementById("sa-variant-body").value;
    await SocialAgent.fetch("/content/" + state.contentId + "/variants/" + state.variantId, {
      method: "PATCH",
      body: JSON.stringify({ body }),
    });
    await openDraft(state.contentId);
  }

  async function dryRun() {
    if (!state.contentId) return;
    const data = await SocialAgent.fetch("/publishing/preview", {
      method: "POST",
      body: JSON.stringify({
        content_id: state.contentId,
        destinations: ["facebook_page", "instagram_feed"],
      }),
    });
    document.getElementById("sa-preview-frame").innerHTML =
      "<pre class='sa-preview-json'>" + escapeHtml(JSON.stringify(data, null, 2)) + "</pre>";
  }

  async function generate() {
    const brief = document.getElementById("sa-brief").value.trim();
    if (!brief) return;
    const langs = Array.from(document.getElementById("sa-langs").selectedOptions).map((o) => o.value);
    const btn = document.getElementById("sa-gen");
    btn.disabled = true;
    try {
      const data = await SocialAgent.fetch("/tools/execute", {
        method: "POST",
        body: JSON.stringify({
          tool: "content.generate_variants",
          dry_run: false,
          arguments: {
            brief,
            platforms: [
              "facebook",
              "instagram",
              "telegram",
              "x",
              "linkedin",
              "tiktok",
              "youtube_community",
              "discord",
            ],
            languages: langs.length ? langs : ["EN"],
            persist: true,
          },
        }),
      });
      if (data.content_id) await openDraft(data.content_id);
      await loadDrafts();
    } finally {
      btn.disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("sa-brief").addEventListener("input", meter);
    meter();
    document.getElementById("sa-refresh-drafts").addEventListener("click", loadDrafts);
    document.getElementById("sa-gen").addEventListener("click", generate);
    document.getElementById("sa-save-variant").addEventListener("click", saveVariant);
    document.getElementById("sa-dry-run").addEventListener("click", dryRun);
    loadDrafts().catch((e) => {
      document.getElementById("sa-draft-list").textContent = String(e.message || e);
    });
  });
})();
