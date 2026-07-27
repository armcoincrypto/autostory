(function () {
  const state = {
    contentId: null,
    variantId: null,
    platform: "facebook",
    previews: [],
    dirty: false,
  };

  function esc(s) {
    return SocialAgent.escapeHtml(s);
  }

  function meter() {
    const el = document.getElementById("sa-brief");
    const m = document.getElementById("sa-char-meter");
    if (el && m) m.textContent = el.value.length + " chars";
  }

  async function loadDrafts() {
    const listEl = document.getElementById("sa-draft-list");
    listEl.innerHTML = SocialAgent.ui.loadingHtml("Loading drafts…");
    try {
      const data = await SocialAgent.fetch("/content");
      const items = data.items || [];
      if (!items.length) {
        listEl.innerHTML = SocialAgent.ui.emptyHtml("No drafts yet", "Generate variants from a brief above.");
        return;
      }
      listEl.innerHTML = items
        .map(
          (it) =>
            "<button type='button' class='sa-draft-row' data-open='" +
            it.id +
            "'>" +
            "<span class='sa-status'>" +
            esc(it.status) +
            "</span>" +
            "<strong>#" +
            it.id +
            " · " +
            esc(it.title || "Untitled") +
            "</strong>" +
            "</button>"
        )
        .join("");
      listEl.querySelectorAll("[data-open]").forEach((btn) =>
        btn.addEventListener("click", () => openDraft(Number(btn.getAttribute("data-open"))))
      );
    } catch (e) {
      listEl.innerHTML = SocialAgent.ui.errorHtml(e.message);
    }
  }

  function renderTabs() {
    const tabs = document.getElementById("sa-platform-tabs");
    tabs.innerHTML = (state.previews || [])
      .map((p) => {
        const active = p.platform === state.platform ? " active" : "";
        const warn = p.validation && !p.validation.ok ? " !" : "";
        return (
          "<button type='button' class='sa-tab" +
          active +
          "' data-platform='" +
          esc(p.platform) +
          "' aria-selected='" +
          (p.platform === state.platform) +
          "'>" +
          esc(p.label) +
          warn +
          "</button>"
        );
      })
      .join("");
    tabs.querySelectorAll("[data-platform]").forEach((btn) =>
      btn.addEventListener("click", () => {
        if (state.dirty && !confirm("Discard unsaved variant edits?")) return;
        state.platform = btn.getAttribute("data-platform");
        state.dirty = false;
        renderTabs();
        renderPreview();
      })
    );
  }

  function renderPreview() {
    const frame = document.getElementById("sa-preview-frame");
    const p = (state.previews || []).find((x) => x.platform === state.platform);
    if (!p) {
      frame.innerHTML = SocialAgent.ui.emptyHtml("No preview", "Select or generate a draft.");
      return;
    }
    state.variantId = p.variant_id;
    const bodyEl = document.getElementById("sa-variant-body");
    if (bodyEl && document.activeElement !== bodyEl) bodyEl.value = p.body || "";
    const ok = p.validation && p.validation.ok;
    frame.innerHTML =
      "<div class='sa-preview-card' data-platform='" +
      esc(p.platform) +
      "'>" +
      "<div class='sa-preview-meta'>" +
      esc(p.label) +
      " · " +
      p.char_count +
      "/" +
      p.char_limit +
      " · " +
      (ok ? "valid" : "needs edit") +
      (p.preview && p.preview.provider_called === false ? " · no provider call" : "") +
      "</div>" +
      "<div class='sa-preview-body'>" +
      esc(p.body || "") +
      "</div>" +
      "<details><summary class='sa-muted small'>Canonical renderer payload</summary>" +
      "<pre class='sa-preview-json'>" +
      esc(JSON.stringify(p.preview || {}, null, 2)) +
      "</pre></details>" +
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
    state.dirty = false;
    document.getElementById("sa-active-editor").hidden = false;
    document.getElementById("sa-preview-frame").innerHTML = SocialAgent.ui.loadingHtml("Rendering previews…");
    try {
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
        .join(" · ") || "No status events yet.";
    } catch (e) {
      document.getElementById("sa-preview-frame").innerHTML = SocialAgent.ui.errorHtml(e.message);
    }
  }

  async function transition(status) {
    if (!state.contentId) return;
    try {
      await SocialAgent.fetch("/content/" + state.contentId + "/status", {
        method: "POST",
        body: JSON.stringify({ status }),
      });
      SocialAgent.ui.toast("Status → " + status, "ok");
      await openDraft(state.contentId);
      await loadDrafts();
    } catch (e) {
      SocialAgent.ui.toast(e.message, "error");
    }
  }

  async function saveVariant() {
    if (!state.contentId || !state.variantId) {
      SocialAgent.ui.toast("No variant selected", "error");
      return;
    }
    const body = document.getElementById("sa-variant-body").value;
    const btn = document.getElementById("sa-save-variant");
    SocialAgent.ui.setBusy(btn, true);
    try {
      await SocialAgent.fetch("/content/" + state.contentId + "/variants/" + state.variantId, {
        method: "PATCH",
        body: JSON.stringify({ body }),
      });
      state.dirty = false;
      SocialAgent.ui.toast("Variant saved", "ok");
      await openDraft(state.contentId);
    } catch (e) {
      SocialAgent.ui.toast(e.message, "error");
    } finally {
      SocialAgent.ui.setBusy(btn, false);
    }
  }

  async function dryRun() {
    if (!state.contentId) return;
    document.getElementById("sa-preview-frame").innerHTML = SocialAgent.ui.loadingHtml("Running Meta dry-run…");
    try {
      const data = await SocialAgent.fetch("/publishing/preview", {
        method: "POST",
        body: JSON.stringify({
          content_id: state.contentId,
          destinations: ["facebook_page", "instagram_feed"],
        }),
      });
      document.getElementById("sa-preview-frame").innerHTML =
        "<pre class='sa-preview-json'>" + esc(JSON.stringify(data, null, 2)) + "</pre>";
      SocialAgent.ui.toast("Dry-run complete · provider_called=" + String(data.provider_called), "ok");
    } catch (e) {
      document.getElementById("sa-preview-frame").innerHTML = SocialAgent.ui.errorHtml(e.message);
    }
  }

  async function generate() {
    const brief = document.getElementById("sa-brief").value.trim();
    if (!brief) {
      SocialAgent.ui.toast("Enter a brief first", "error");
      return;
    }
    const langs = Array.from(document.getElementById("sa-langs").selectedOptions).map((o) => o.value);
    const btn = document.getElementById("sa-gen");
    SocialAgent.ui.setBusy(btn, true);
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
      if (data.content_id) {
        await openDraft(data.content_id);
        SocialAgent.ui.toast("Draft #" + data.content_id + " created", "ok");
      }
      await loadDrafts();
    } catch (e) {
      SocialAgent.ui.toast(e.message, "error");
    } finally {
      SocialAgent.ui.setBusy(btn, false);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("sa-brief").addEventListener("input", meter);
    meter();
    document.getElementById("sa-variant-body")?.addEventListener("input", () => {
      state.dirty = true;
    });
    document.getElementById("sa-refresh-drafts").addEventListener("click", loadDrafts);
    document.getElementById("sa-gen").addEventListener("click", generate);
    document.getElementById("sa-save-variant").addEventListener("click", saveVariant);
    document.getElementById("sa-dry-run").addEventListener("click", dryRun);
    const params = new URLSearchParams(window.location.search);
    const openId = params.get("open");
    loadDrafts().then(() => {
      if (openId) openDraft(Number(openId));
    });
  });
})();
