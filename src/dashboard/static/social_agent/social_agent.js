(function () {
  const API = "/api/v1/social-agent";

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  async function saFetch(path, options) {
    const opts = options || {};
    const headers = new Headers(opts.headers || {});
    if (!headers.has("Content-Type") && opts.body && !(opts.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
    const res = await fetch(API + path, { ...opts, headers, credentials: "same-origin" });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = { ok: false, error: "invalid_json" };
    }
    if (!res.ok) {
      const err = new Error((data && (data.error || data.message)) || ("http_" + res.status));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function loadingHtml(message) {
    return (
      '<div class="sa-state sa-state-loading" role="status">' +
      '<span class="sa-spinner" aria-hidden="true"></span>' +
      "<span>" +
      escapeHtml(message || "Loading…") +
      "</span></div>"
    );
  }

  function emptyHtml(title, detail) {
    return (
      '<div class="sa-empty" role="status">' +
      "<strong>" +
      escapeHtml(title || "Nothing here yet") +
      "</strong>" +
      (detail ? '<p class="mb-0 mt-2">' + escapeHtml(detail) + "</p>" : "") +
      "</div>"
    );
  }

  function errorHtml(message) {
    return (
      '<div class="sa-state sa-state-error" role="alert">' +
      "<strong>Something went wrong</strong>" +
      '<p class="mb-0 mt-2">' +
      escapeHtml(message || "Request failed") +
      "</p></div>"
    );
  }

  function toast(message, kind) {
    const region = document.getElementById("sa-toast-region");
    if (!region) return;
    const el = document.createElement("div");
    el.className = "sa-toast" + (kind ? " sa-toast-" + kind : "");
    el.textContent = message;
    region.appendChild(el);
    setTimeout(() => el.classList.add("show"), 10);
    setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => el.remove(), 200);
    }, 3200);
  }

  function setBusy(el, busy) {
    if (!el) return;
    el.setAttribute("aria-busy", busy ? "true" : "false");
    if ("disabled" in el) el.disabled = !!busy;
  }

  const COMMANDS = [
    { id: "overview", label: "Go to Overview", href: "/social-agent", keywords: "home dashboard" },
    { id: "assistant", label: "Open AI Copilot", href: "/social-agent/assistant", keywords: "chat ai" },
    { id: "content", label: "Open Content Studio", href: "/social-agent/content", keywords: "draft editor" },
    { id: "publishing", label: "Open Publishing Preview", href: "/social-agent/publishing", keywords: "dry-run" },
    { id: "calendar", label: "Open Calendar", href: "/social-agent/calendar", keywords: "schedule queue" },
    { id: "media", label: "Open Media Library", href: "/social-agent/media", keywords: "upload assets" },
    { id: "accounts", label: "Open Social Accounts", href: "/social-agent/accounts", keywords: "meta telegram" },
    { id: "analytics", label: "Open Analytics", href: "/social-agent/analytics", keywords: "metrics" },
    { id: "comments", label: "Open Comments", href: "/social-agent/comments", keywords: "" },
    { id: "messages", label: "Open Messages", href: "/social-agent/messages", keywords: "inbox" },
    { id: "brand", label: "Open Brand Knowledge", href: "/social-agent/brand", keywords: "tone hashtags" },
    { id: "automations", label: "Open Automations (disabled)", href: "/social-agent/automations", keywords: "workflow" },
    { id: "settings", label: "Open Settings", href: "/social-agent/settings", keywords: "health" },
    { id: "search", label: "Search workspace", action: "search", keywords: "find" },
    { id: "theme", label: "Toggle theme", action: "theme", keywords: "dark light" },
  ];

  function openModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.hidden = false;
    document.body.classList.add("sa-modal-open");
    const input = modal.querySelector(".sa-modal-input");
    if (input) {
      input.value = "";
      setTimeout(() => input.focus(), 0);
    }
  }

  function closeModal(id) {
    const modal = id ? document.getElementById(id) : null;
    if (modal) modal.hidden = true;
    else {
      document.querySelectorAll(".sa-modal").forEach((m) => {
        m.hidden = true;
      });
    }
    document.body.classList.remove("sa-modal-open");
  }

  function renderCommandList(filter) {
    const list = document.getElementById("sa-command-list");
    if (!list) return;
    const q = (filter || "").trim().lower();
    const items = COMMANDS.filter((c) => {
      if (!q) return true;
      return (c.label + " " + (c.keywords || "") + " " + c.id).toLowerCase().includes(q);
    });
    if (!items.length) {
      list.innerHTML = '<li class="sa-modal-empty">No matching commands.</li>';
      return;
    }
    list.innerHTML = items
      .map(
        (c, i) =>
          '<li role="option" class="sa-modal-item' +
          (i === 0 ? " active" : "") +
          '" data-cmd="' +
          escapeHtml(c.id) +
          '"><span>' +
          escapeHtml(c.label) +
          "</span></li>"
      )
      .join("");
    list.querySelectorAll("[data-cmd]").forEach((el) =>
      el.addEventListener("click", () => runCommand(el.getAttribute("data-cmd")))
    );
  }

  function runCommand(id) {
    const cmd = COMMANDS.find((c) => c.id === id);
    closeModal();
    if (!cmd) return;
    if (cmd.action === "search") {
      openModal("sa-search-modal");
      return;
    }
    if (cmd.action === "theme") {
      toggleTheme();
      return;
    }
    if (cmd.href) window.location.href = cmd.href;
  }

  let searchTimer = null;
  async function runSearch(q) {
    const list = document.getElementById("sa-search-list");
    const status = document.getElementById("sa-search-status");
    if (!list || !status) return;
    if (!(q || "").trim()) {
      status.textContent = "Type to search.";
      list.innerHTML = "";
      return;
    }
    status.innerHTML = loadingHtml("Searching…");
    list.innerHTML = "";
    try {
      const data = await saFetch("/search?q=" + encodeURIComponent(q) + "&limit=25");
      const rows = data.results || [];
      status.textContent = rows.length ? rows.length + " result(s)" : "No matches.";
      if (!rows.length) {
        list.innerHTML = '<li class="sa-modal-empty">No matches for “' + escapeHtml(q) + '”.</li>';
        return;
      }
      list.innerHTML = rows
        .map(
          (r) =>
            '<li class="sa-modal-item" role="option">' +
            '<a href="' +
            escapeHtml(r.href || "#") +
            '"><strong>' +
            escapeHtml(r.title) +
            '</strong><span class="sa-muted small d-block">' +
            escapeHtml((r.type || "") + (r.subtitle ? " · " + r.subtitle : "")) +
            "</span></a></li>"
        )
        .join("");
    } catch (e) {
      status.innerHTML = errorHtml(e.message);
    }
  }

  function toggleTheme() {
    const root = document.documentElement;
    const next = root.getAttribute("data-sa-theme") === "light" ? "dark" : "light";
    if (next === "light") root.setAttribute("data-sa-theme", "light");
    else root.removeAttribute("data-sa-theme");
    try {
      localStorage.setItem("sa-theme", next === "light" ? "light" : "dark");
    } catch (_) {}
    toast(next === "light" ? "Light theme" : "Dark theme", "ok");
  }

  async function refreshHealth() {
    const pill = document.getElementById("sa-health-pill");
    if (!pill) return;
    try {
      const data = await saFetch("/health");
      const meta = data.integrations && data.integrations.meta;
      const tg = data.integrations && data.integrations.telegram;
      const warn = meta && meta.status === "CREDENTIALS_MISSING";
      pill.textContent = warn ? "Healthy · Meta credentials missing" : "Healthy";
      pill.classList.toggle("ok", !warn);
      pill.classList.toggle("warn", !!warn);
      pill.title = "Telegram: " + ((tg && tg.status) || "unknown");
    } catch (e) {
      pill.textContent = "Health unavailable";
      pill.classList.add("warn");
    }
  }

  function wireChrome() {
    const nav = document.getElementById("sa-nav");
    const toggle = document.getElementById("sa-nav-toggle");
    if (toggle && nav) {
      toggle.addEventListener("click", () => {
        const open = nav.classList.toggle("sa-nav-open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }

    document.getElementById("sa-open-command")?.addEventListener("click", () => {
      openModal("sa-command-modal");
      renderCommandList("");
    });
    document.getElementById("sa-open-search")?.addEventListener("click", () => openModal("sa-search-modal"));
    document.getElementById("sa-theme-toggle")?.addEventListener("click", toggleTheme);

    document.querySelectorAll(".sa-modal [data-sa-close]").forEach((el) =>
      el.addEventListener("click", () => closeModal())
    );

    document.getElementById("sa-command-input")?.addEventListener("input", (e) => {
      renderCommandList(e.target.value);
    });
    document.getElementById("sa-command-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const active = document.querySelector("#sa-command-list .sa-modal-item.active");
        if (active) runCommand(active.getAttribute("data-cmd"));
      } else if (e.key === "Escape") {
        closeModal("sa-command-modal");
      }
    });

    document.getElementById("sa-search-input")?.addEventListener("input", (e) => {
      clearTimeout(searchTimer);
      const q = e.target.value;
      searchTimer = setTimeout(() => runSearch(q), 180);
    });
    document.getElementById("sa-search-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeModal("sa-search-modal");
    });

    document.addEventListener("keydown", (e) => {
      const tag = (e.target && e.target.tagName) || "";
      const typing = tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openModal("sa-command-modal");
        renderCommandList("");
        return;
      }
      if (!typing && e.key === "/") {
        e.preventDefault();
        openModal("sa-search-modal");
        return;
      }
      if (e.key === "Escape") closeModal();
    });
  }

  window.SocialAgent = {
    api: API,
    fetch: saFetch,
    escapeHtml,
    ui: { loadingHtml, emptyHtml, errorHtml, toast, setBusy, openModal, closeModal },
  };

  try {
    const theme = localStorage.getItem("sa-theme");
    if (theme === "light") document.documentElement.setAttribute("data-sa-theme", "light");
  } catch (_) {}

  function boot() {
    wireChrome();
    refreshHealth();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
