(function () {
  const API = "/api/v1/social-agent";

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

  window.SocialAgent = { api: API, fetch: saFetch };

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

  try {
    const theme = localStorage.getItem("sa-theme");
    if (theme === "light") document.documentElement.setAttribute("data-sa-theme", "light");
  } catch (_) {}

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refreshHealth);
  } else {
    refreshHealth();
  }
})();
