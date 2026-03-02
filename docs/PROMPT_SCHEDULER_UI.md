# Professional Prompt: Add Scheduler UI to STORYFLEET Dashboard

**Copy the prompt below and paste it into ChatGPT (or another AI assistant) to implement the Scheduler UI.**

---

## Prompt for ChatGPT

---

You are a senior full-stack developer. Add a **Scheduler** section to an existing Flask dashboard so users can comfortably configure an Auto Message Scheduler: chat targets, message templates, account–target bindings, schedule profiles, schedule rules, and delivery logs.

### Project Context

- **App**: STORYFLEET – Telegram orchestration dashboard
- **Stack**: Flask, Jinja2 templates, Bootstrap 5, vanilla JavaScript (fetch)
- **Location**: `src/dashboard/` – routes, templates, static
- **Base template**: `templates/base.html` – dark theme (`--primary-color: #6366f1`, `--card-bg: #16213e`), sidebar nav, Bootstrap cards

### Existing API Endpoints (Base: `http://example.com/api/v1/`)

All endpoints return JSON. CSRF is exempt. Use `fetch()` with `Content-Type: application/json` for POST/PUT.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/targets` | List chat targets |
| POST | `/api/v1/targets` | Create target. Body: `{username?, invite_link?, tg_id?, chat_type}` (chat_type: "channel"\|"group") |
| GET | `/api/v1/bindings?account_id=N` | List bindings (optionally by account_id) |
| POST | `/api/v1/bindings` | Create binding. Body: `{account_id, target_id, can_post?, allowed_types?, daily_cap?}` |
| GET | `/api/v1/templates?type=PROMO&scope=GLOBAL` | List templates (optional filters) |
| POST | `/api/v1/templates` | Create template. Body: `{type, body, scope?, account_id?, target_id?, binding_id?, name?, is_active?, weight?}`. type: PROMO|INFO, scope: GLOBAL|ACCOUNT|TARGET|BINDING |
| POST | `/api/v1/templates/preview` | Preview rendered text. Body: `{body, account_name?, chat_title?}` |
| GET | `/api/v1/schedule/profile/<account_id>` | Get schedule profile for account |
| PUT | `/api/v1/schedule/profile/<account_id>` | Update profile. Body: `{is_enabled, timezone?, min_interval_sec?, daily_cap_total?, daily_cap_promo?, daily_cap_info?, jitter_sec?, quiet_hours_json?}` |
| GET | `/api/v1/schedule/rules/<account_id>` | List schedule rules for account |
| POST | `/api/v1/schedule/rules/<account_id>` | Create rule. Body: `{type, times_json?, target_mode?, selected_target_ids?, is_enabled?}`. type: PROMO|INFO, times_json: ["10:00","18:00"], target_mode: ALL_BOUND|ONLY_SELECTED |
| GET | `/api/v1/deliveries?account_id=N&limit=100` | List message deliveries (sent/failed) |

You also need **accounts** from `GET /api/accounts` (list of `{id, phone_number, username, first_name, status}`).

### Template Variables (for user education & preview)

Templates support: `{account_name}`, `{chat_title}`, `{date}`, `{time}`, `{random_emoji}`, `{cta_link}`.

---

### UI Requirements

1. **Navigation**  
   Add a sidebar link: "Scheduler" (e.g. icon `bi-calendar-week`) pointing to `/scheduler`.

2. **Scheduler Page** (`/scheduler`)  
   Single page with clear sections (cards/tabs). Layout should be **comfortable to use**: good spacing, readable labels, logical flow. Use Bootstrap tabs or collapsible sections if needed.

3. **Sections to implement**  

   - **Targets**  
     - Table: id, username, invite_link, tg_id, chat_type, is_verified  
     - Button "Add Target" → modal: username OR invite_link, chat_type (channel/group)  
     - Create via `POST /api/v1/targets`

   - **Bindings**  
     - Table: id, account_id (show phone/username), target_id (show username/title), can_post, allowed_types, daily_cap  
     - Filters: by account (dropdown)  
     - Button "Add Binding" → modal: select account (dropdown), select target (dropdown), can_post checkbox, allowed_types (e.g. "PROMO,INFO"), daily_cap (optional number)  
     - Create via `POST /api/v1/bindings`

   - **Templates**  
     - Table: id, type (PROMO/INFO), scope, name, body (truncated), is_active, weight  
     - Button "Add Template" → modal: type, scope, name, body (textarea), is_active, weight  
     - **Live preview**: show `POST /api/v1/templates/preview` result below the body textarea (debounced, e.g. 300ms)  
     - Create via `POST /api/v1/templates`

   - **Schedule Profile (per account)**  
     - Dropdown to select account  
     - Form: is_enabled (toggle), timezone (default "Asia/Yerevan"), min_interval_sec, daily_cap_total, daily_cap_promo, daily_cap_info, jitter_sec, quiet_hours (JSON or structured inputs)  
     - Save via `PUT /api/v1/schedule/profile/<account_id>`

   - **Schedule Rules (per account)**  
     - Same account selector as profile  
     - Table: id, type (PROMO/INFO), times_json (e.g. "10:00, 18:00"), target_mode, is_enabled  
     - Button "Add Rule" → modal: type, times (multi input or comma-separated), target_mode (ALL_BOUND / ONLY_SELECTED), if ONLY_SELECTED show target multi-select  
     - Create via `POST /api/v1/schedule/rules/<account_id>`

   - **Deliveries**  
     - Table: id, account_id, target_id, type, status, sent_at, error_message  
     - Optional filter by account_id, limit 100  
     - Read-only, from `GET /api/v1/deliveries`

4. **UX Guidelines**  
   - Use Bootstrap 5 components (cards, tables, modals, forms, badges)  
   - Match existing dark theme (card-bg, primary color)  
   - Show loading states ("Loading...") while fetching  
   - Show success/error toasts or alerts for create operations  
   - Validate required fields before submit  
   - Use clear labels and placeholders

5. **Technical constraints**  
   - No new backend routes: use existing API only  
   - Add a single Flask route: `@app.route('/scheduler')` that renders `scheduler.html`  
   - All data loading via `fetch()` in JavaScript  
   - No npm/build step; inline scripts or `<script>` in template
   - Register the route in the same place other pages (e.g. `/accounts`, `/stories`) are registered

---

### Deliverables

1. Add "Scheduler" link to `base.html` sidebar  
2. Create `templates/scheduler.html` with all sections above  
3. Add the `/scheduler` route to the Flask app (in `routes.py` or equivalent)  

Match the structure and style of existing pages (e.g. `accounts.html`, `campaigns.html`) for consistency.

---
