# ISP RADIUS Panel — Audit & Design System

Status: **draft, awaiting approval.** No application code has been changed for the redesign yet.

This document has two parts:

- **Part A — Codebase audit.** What exists today and must keep working.
- **Part B — Design system.** The rules the redesigned UI follows.

Part C lists where the brief and the codebase disagree, and the decision taken for each.

---

## Part A — Codebase audit

### A1. Architecture as it actually is

The brief mentions "Flask blueprints" and "SQLAlchemy models". **Neither exists in this codebase.** The real structure:

| Layer | Implementation |
|---|---|
| Web framework | Flask, single `app` object in `panel/app.py` |
| Routing | Custom registry: `@core.route(rule, module=..., need=...)` in feature modules, attached by `core.attach(app)`. 22 routes use `@app.route` directly in `app.py`. **No blueprints.** |
| Database access | Raw SQL through PyMySQL (`db.connect()`, `core.q / x / insert / commit`). Per-request connection in `flask.g`. **No ORM, no models.** |
| Schema | `sql/*.sql` (617 lines): `CREATE TABLE IF NOT EXISTS` + `ALTER ... ADD COLUMN IF NOT EXISTS`, re-applied by `install.sh` on every upgrade. No migration versioning. |
| Templates | Jinja2, 94 files in `panel/templates`, server-rendered. `base.html` (staff), `portal_base.html` (customers), `bare.html`, `login.html`. |
| Frontend assets | `static/css/app.css` (84 KB, hand-written, light/dark), `static/js/app.js` (UI behaviours), `charts.js` (own SVG charts), `activation.js`. **No framework, no build step, no CDN.** Inter is self-hosted (`static/fonts`). |
| Server | gunicorn (4 workers × 8 threads) behind nginx on port 80. systemd units: `isp-panel`, `isp-panel-sender`, `isp-panel-maint.timer`, `isp-panel-backup.timer`. |
| OS / install | Ubuntu 24.04, `install.sh` installs and configures MariaDB, FreeRADIUS, nginx, the panel, sudoers rule for `systemctl restart freeradius`. |

**Decision:** the redesign keeps this architecture. It does not introduce SQLAlchemy, Alembic or blueprints. Converting 219 routes and every raw query to an ORM would touch all business logic for no user-visible gain, and is the single biggest way to break things. (See C1.)

Size: 37 Python files, 11,167 lines; 94 templates, 4,662 lines; 219 routes.

### A2. Feature inventory (all routes, grouped)

Every item below is built and wired to a route. Counts are routes per module.

| Area | Module (routes) | What works today |
|---|---|---|
| Auth & shell | `app.py` (22) | Login with IP rate-limit (8 fails / 10 min), logout, CSRF on every POST, role/permission gate on every request, dashboard, `/api/live` (online count JSON), error pages |
| Subscribers | `app.py`, `mod_subs.py` (10), `mod_subview.py` (10) | List with ~20 status filters (online, stale, expired-online, MAC-bound, grace, auto-renewed…), add / edit / view, renew with payment, suspend / activate, kick (Disconnect-Request), delete, bulk actions, CSV import (with sample) / export, activation by username, grace period, MAC bind / reset, migrate plan, balance, service switches, RADIUS sync, password, invoice, photo, extra service charges |
| Online sessions | `app.py` | Live sessions with search / NAS filter, one-click disconnect |
| Packages | `mod_plans.py` (4), `mod_policy.py` (9) | Packages (speed, price, validity, pool, devices, quota, bandwidth), RADIUS policy editor (group check/reply attributes), allocation (who may sell which plan), tax / extra fees, package report |
| Accounting | `mod_accounting.py` (11) | Payments + CSV, printable receipts, receive payment, expenses, income vs expense (12 months), dues, delete payment |
| Users & roles | `mod_people.py` (39) | Admins, staff, resellers, sub-resellers, retailers; roles + permission matrix (module level) and function-level permissions (`permcatalog.py`); custom roles; balances & transfers; per-user package prices; switch-into-user and switch back; profile, photo, invoice settings; departments, zones, areas, area report; "My users" for sellers |
| Seller self-service | `mod_people.py`, `mod_reports.py`, `mod_topup.py` (3) | My packages, my users, my profit (+CSV), add balance via bKash Tokenized Checkout (server-verified, credited once) |
| Network | `mod_nas.py` (18), `mod_network.py` (8), `mod_olt.py` (9), `oltsync.py`, `snmplib.py` | NAS (routers) with ping / ping-all, NAS groups, IP pools, MAC vendor DB import, devices with ping, OLTs with SNMP config / test / walk / sync / sync-all, ONUs (list, view, map to customer, signal metrics), traffic graph, topology diagram (SVG) |
| Tools | `app.py`, `mod_diag.py` (1) | Test a RADIUS login, restart FreeRADIUS, "why was this customer refused" diagnosis |
| Messaging | `mod_messages.py` (9), `msglib.py`, `sendlib.py`, `sender.py` | SMS / email compose (one, group, typed numbers), templates, outbox log with retry / cancel, automatic welcome / payment / expiry messages through a rate-limited queue |
| Tickets | `mod_tickets.py` (6) | Tickets with category, priority, department, assignee, replies, internal notes |
| Inventory | `mod_inventory.py` (5) | Items, min stock, in / out / assign / return / count, stock log |
| Notes, notices, documents | `mod_notes.py` (4), `mod_notices.py` (3), `mod_documents.py` (4) | Private / shared / pinned notes linked to customers; dashboard notices; document upload stored outside web root |
| Reports & logs | `mod_reports.py` (7), `app.py` | Traffic, collection (+CSV), customers, profit CSV, plan report, RADIUS session log, login attempts, panel activity log |
| Settings | `mod_settings.py` (17), `mod_backup.py` (13) | General, branding (logo / favicon), email (SMTP + test), SMS gateway (+ test), payment gateways, automatic messages, Telegram alerts, subscriber / user rules, partner tier names, modules on/off, log engine, software; backup / restore / download / upload, Google Drive copy |
| Customer portal | `mod_portal.py` (7) | Separate login, usage & invoices, change password, pay through bKash |
| Background jobs | `maintenance.py`, `alertlib.py`, `backuplib.py` | Every 5 min: online snapshot, server stats, Telegram alerts, auto MAC-lock / auto-renew, expiry & quota state, OLT auto-poll, stale-session close; daily: old auth log purge, accounting archive; scheduled backups |

### A3. Database tables (must all be preserved)

**FreeRADIUS standard tables (read and written by the panel):**
`radcheck`, `radreply`, `radgroupcheck`, `radgroupreply`, `radusergroup`, `radacct`, `radpostauth`, `nas`.
Plus `radacct_archive` (created by the panel, same layout as `radacct`).

**Panel tables (41):**
`panel_admins`, `plans`, `subscribers`, `payments`, `audit_log`, `stats_snapshots`, `server_stats`, `settings`, `custom_roles`, `role_funcs`, `role_perms`, `user_plans`, `departments`, `zones`, `areas`, `expenses`, `inventory_items`, `inventory_moves`, `net_devices`, `ip_pools`, `tickets`, `ticket_replies`, `outbox`, `message_templates`, `notes`, `documents`, `fees`, `sub_charges`, `coa_log`, `sub_ledger`, `plan_fees`, `nas_groups`, `mac_vendors`, `onus`, `olt_snmp`, `onu_metrics`, `notices`, `user_ledger`, `topups`, `portal_payments`, `radacct_archive`.

### A4. Invariants the redesign must not break

These are the things a UI rewrite can silently break. Each one gets checked after every phase.

1. **FreeRADIUS contract.** `radcheck` keeps `Cleartext-Password` (needed for CHAP / MS-CHAPv2). Plan → `radusergroup` / `radgroupreply` mapping in `radius.py` is untouched.
2. **Permission gate.** `gate()` in `app.py` checks module level (`core.ENDPOINT_MODULE`) and function level (`permcatalog.ENDPOINT_FN`) for every endpoint. Endpoint names stay the same, so these maps stay valid. The sidebar (`nav.py`) and the new bottom nav are both built from the same filtered list, never hard-coded.
3. **Seller scoping.** Resellers / sub-resellers / retailers see only their downline (`core.my_downline`, `scope_sql`, `olt_scope`). No new page or widget may query subscribers without these.
4. **CSRF.** Every `<form method="post">` keeps `{{ csrf_input() }}`; every `fetch` POST sends `X-CSRF-Token`.
5. **Endpoint names and URLs.** Bookmarks, receipts, bKash callback URLs (`/bkash/callback/<id>/<key>`, `/portal/bkash/...`) and CSV export URLs keep working.
6. **No external network dependency at runtime.** Fonts and scripts are self-hosted. Many ISP servers sit on restricted networks.
7. **Branding.** Uploaded logo / favicon and the renamed partner tiers keep showing.
8. **Print.** Receipts and invoices print in light, black-on-white, regardless of theme.
9. **Upgrade path.** `sudo ./install.sh` over an existing install keeps data and passwords.

### A5. Known issues found during the audit (not part of the redesign unless approved)

| # | Issue | Severity |
|---|---|---|
| 1 | Panel served over **plain HTTP** only; cookie `Secure` flag off | High |
| 2 | **No automated tests** at all | High (for a redesign) |
| 3 | No schema migration versioning | Medium |
| 4 | Panel DB user has `GRANT ALL` on the radius database | Medium |
| 5 | SMTP / bKash / Telegram secrets stored in plain text in `settings` | Medium |
| 6 | 196 inline `style=""` attributes and 15 inline `<script>` blocks in templates (blocks a Content-Security-Policy, makes re-skinning harder) | Medium |
| 7 | `app.py` (1,364 lines) and `mod_people.py` (1,099) are large; leftover blank blocks in `app.py` | Low |
| 8 | `ping` called without `--` before host; bKash amount check skipped if response has no amount | Low |

### A6. Frontend inventory (what the redesign re-skins)

The templates already share a consistent class vocabulary. This is what makes a low-risk redesign possible: **restyle the classes, keep the markup.**

| Class | Uses | Becomes |
|---|---|---|
| `.btn` (`.primary`, `.danger`, `.sm`, `.icon`) | 404 | Button |
| `.field`, `.lbl`, `.help`, `.req` | ~1,000 | Form field |
| `.panel`, `.panel-head`, `.panel-body` | 184 | Card |
| `.tbl`, `.tbl-wrap`, `.xtable` | 75 tables in 49 templates | Data table → mobile cards |
| `.kpi`, `.kpis` | 76 | Stat tile |
| `.pill`, `.badge`, `.chip` | 72 | Status badge |
| `.modal` (`-head`, `-body`, `-foot`) | 51 | Dialog |
| `.page-head` | 60 | Page header |
| `.num`, `.mono` | 306 | Monospace data |
| `.empty` | 71 | Empty state |
| `.toast` | base | Toast |

Current sidebar: 244 px, dark, collapsible on desktop (`localStorage["isp-side"]`), drawer on mobile. Theme: `data-theme` on `<html>`, `localStorage["isp-theme"]`, follows OS when unset. Ten different mobile breakpoints are in use today (560–1100 px).

---

## Part B — Design system

### B1. Principles

1. **Data first.** Operators scan thousands of rows. Density, alignment and legibility beat decoration.
2. **One accent.** Electric Blue means "action / selected". Status colours mean status only, never decoration.
3. **Quiet surfaces.** Thin borders (1 px), almost no shadow, no gradients on working screens.
4. **Monospace for machine values.** IPs, MACs, usernames in tables, speeds, amounts, timestamps in logs.
5. **Same markup, new skin.** Existing class names stay. New components are added only where nothing exists (bottom nav, mobile card view).

### B2. Typography

| Role | Family | Use |
|---|---|---|
| Display | **Plus Jakarta Sans** 600 / 700 | Page titles, card titles, KPI numbers, login |
| UI | **Inter** 400 / 500 / 600 | Everything else |
| Data | **JetBrains Mono** 400 / 500 | IP, MAC, speeds (`10M/10M`), money, usernames in tables, logs, OIDs |

All three are **self-hosted** as `woff2` in `static/fonts/` (see C2). Fallback stacks:

```css
--font-display: "Plus Jakarta Sans", "Inter", system-ui, sans-serif;
--font-ui:      "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
--font-mono:    "JetBrains Mono", ui-monospace, "Cascadia Mono", Menlo, Consolas, monospace;
```

Monospace numbers use `font-variant-numeric: tabular-nums` so columns line up.

**Type scale** (rem, base 14 px on desktop and 15 px on mobile):

| Token | Size / line-height | Weight | Use |
|---|---|---|---|
| `--text-xs` | 11 / 16 | 500 | Table headers (uppercase, +0.04em), badges |
| `--text-sm` | 13 / 20 | 400 | Table cells, help text |
| `--text-base` | 14 / 22 | 400 | Body, inputs |
| `--text-lg` | 16 / 24 | 600 | Card titles |
| `--text-xl` | 20 / 28 | 700 display | Page titles |
| `--text-2xl` | 28 / 34 | 700 display | KPI numbers |

### B3. Colour tokens

Names are semantic. Components use tokens only, never hex values.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#F8FAFC` | `#090D16` | Page background |
| `--surface` | `#FFFFFF` | `#111827` | Cards, tables, dialogs |
| `--surface-2` | `#F1F5F9` | `#1A2233` | Table header, hover row, inputs |
| `--border` | `#E2E8F0` | `#1F2937` | Default 1 px lines |
| `--border-strong` | `#CBD5E1` | `#334155` | Inputs, focused areas |
| `--text` | `#0F172A` | `#F1F5F9` | Primary text |
| `--text-muted` | `#64748B` | `#94A3B8` | Secondary text |
| `--accent` | `#2563EB` | `#3B82F6` | Primary buttons, links, selected nav, focus ring |
| `--accent-hover` | `#1D4ED8` | `#60A5FA` | |
| `--accent-soft` | `#EFF6FF` | `rgba(59,130,246,.14)` | Selected row, active nav background |
| `--online` | `#10B981` | `#10B981` | Online dot, fills, chart series |
| `--online-text` | `#047857` | `#34D399` | Online label text |
| `--online-soft` | `#ECFDF5` | `rgba(16,185,129,.14)` | Online badge background |
| `--offline` | `#EF4444` | `#EF4444` | Offline / expired dot, fills |
| `--offline-text` | `#B91C1C` | `#F87171` | Offline / expired label text |
| `--offline-soft` | `#FEF2F2` | `rgba(239,68,68,.14)` | |
| `--expiring` | `#F59E0B` | `#F59E0B` | Expiring-soon dot, fills |
| `--expiring-text` | `#B45309` | `#FBBF24` | Expiring label text |
| `--expiring-soft` | `#FFFBEB` | `rgba(245,158,11,.14)` | |
| `--neutral-text` | `#475569` | `#CBD5E1` | Suspended / disabled |
| `--neutral-soft` | `#F1F5F9` | `rgba(148,163,184,.14)` | |

Why the `-text` variants: the brief's Emerald, Rose and Amber are too light to read as small text on white (about 2.5:1, 3.8:1 and 2.1:1 contrast). They stay as dots, fills and chart colours. Text uses the darker shade so it passes WCAG AA (4.5:1). See C4.

**Chart series** (in order): `--accent`, `--online`, `--expiring`, `#8B5CF6`, `#EC4899`, `--text-muted`. `charts.js` already reads CSS variables, so it switches theme automatically.

### B4. Subscriber status → colour (one mapping, used everywhere)

| Status (existing filter key) | Badge | Dot |
|---|---|---|
| online | Online · `--online-*` | green, pulsing |
| offline (active, not connected) | Offline · `--neutral-*` | grey |
| expired, expired_online | Expired · `--offline-*` | red |
| exp1 / exp3 / exp7 / exp14 / exp28 | Expires in N d · `--expiring-*` | amber |
| suspended, profile/internet disabled | Suspended · `--neutral-*`, strike icon | grey |
| grace | Grace · `--expiring-*` outline | amber ring |
| stale | Stale · `--expiring-*` | amber, hollow |

Note: "Offline" for an active customer who is simply not connected is **grey, not red**. Red is reserved for "needs money / action" (expired). Otherwise most of the list turns red at night.

### B5. Spacing, radius, elevation

- Spacing scale (px): 2, 4, 8, 12, 16, 20, 24, 32, 40, 48. Tokens `--s-1` … `--s-10`.
- Radius: `--r-sm` 6 px (inputs, badges), `--r-md` 8 px (buttons, cards), `--r-lg` 12 px (dialogs, mobile cards). Badges are full pill.
- Elevation: cards have a border only. `--shadow-pop` (`0 8px 24px -8px rgb(15 23 42 / .18)`) only for menus, dialogs, toasts.
- Focus: `0 0 0 3px` of `--accent` at 35 % opacity, on every interactive element, visible in both themes.

### B6. Layout and breakpoints

One set of breakpoints replaces today's ten:

| Name | Width | Layout |
|---|---|---|
| `sm` | < 768 px | Phone: no sidebar, top bar + **bottom nav**, tables become cards, single column forms |
| `md` | 768–1023 px | Tablet: sidebar collapsed to icon rail (64 px) by default |
| `lg` | 1024–1279 px | Desktop: sidebar expanded (248 px), collapsible |
| `xl` | ≥ 1280 px | Content max-width 1440 px, centred |

**Desktop sidebar**
- Expanded 248 px: icon + label, groups as collapsible sections (same groups as `nav.py`).
- Collapsed 64 px icon rail. Group children open in a flyout on hover/focus. Tooltip with label.
- Toggle button at the bottom of the sidebar and shortcut `[`. State stays in `localStorage["isp-side"]` (existing key).
- Sidebar uses `--surface` with a right border (light) / `--bg` (dark). It is no longer a fixed dark block in light mode.

**Top bar** (56 px): page title / breadcrumb, global search (subscriber username, phone, IP: existing `/subscribers?q=`), theme toggle, user menu. Live online counter (from `/api/live`) as a green pill.

**Mobile bottom navigation** (`sm` only)
- 5 slots, 64 px tall plus `env(safe-area-inset-bottom)`, icons 24 px with 11 px labels, 48 px minimum hit area.
- Items are chosen from what the user may open (from `nav.build()`), in this priority:
  - Staff / admin: **Home · Subscribers · Online · Payments · More**
  - Sellers: **Home · Subscribers · Activate · Balance · More**
  - A slot the user has no permission for is skipped and the next one is used.
- **More** opens a full-height sheet with the complete sidebar menu (same filtered list).
- The bottom nav hides on scroll down and shows on scroll up, and always shows on page load.

### B7. Components

All are restyles of existing classes unless marked **new**.

- **Button** `.btn`: 36 px (32 px `.sm`, 44 px on phones). Variants: default (surface + border), `.primary` (accent), `.danger` (red text, red fill on confirm), `.ghost` **new**, `.icon` (square).
- **Field** `.field`: label above, 36 px input, `--surface-2` fill, border on focus. Errors in `--offline-text` under the field. Monospace inputs for IP, MAC, password, rate strings.
- **Card** `.panel`: border, `--r-md`, header 48 px with title + actions on the right.
- **Stat tile** `.kpi`: label (xs, muted), value (display 28 px, mono for money), delta / sub-text, optional sparkline. Clickable tiles show an arrow and hover border.
- **Status badge** `.pill`: dot + label, 20 px tall, colours from B4.
- **Data table** `.tbl`: sticky header (`--surface-2`, xs uppercase), 44 px rows (36 px with "compact" view, existing setting), hover row, selected row `--accent-soft`, numeric columns right-aligned and mono, row actions in a trailing menu.
- **Mobile card view** **new** (see B8).
- **Dialog** `.modal`: max 560 px, full-screen sheet on phones (slides up).
- **Toast** `.toast`: bottom-right on desktop, above the bottom nav on phones; auto-dismiss 5 s except errors.
- **Tabs**: underline style, scrollable on phones.
- **Empty state** `.empty`: icon, one sentence, primary action.
- **Filters bar** `.filters`: chips for active filters; on phones collapses into a "Filters" button that opens a sheet.

### B8. Tables → mobile cards (< 768 px)

Done in one place, so the 75 tables do not each need rewriting:

1. `app.js` reads each `table.tbl` header text and writes it to every cell as `data-label` (only if the template has not set one).
2. Under 768 px, CSS turns each row into a card: the **first cell** is the card title, a cell marked `data-role="status"` shows as a badge in the top-right corner, other cells show as `label: value` rows, and the **actions cell** becomes a full-width button row at the bottom (48 px targets).
3. Templates can tune a table with attributes, without restructuring:
   - `data-primary` on the column to use as the title (default: first column)
   - `data-hide-mobile` on a column to drop it from cards
   - `data-cards="off"` on the table to keep horizontal scrolling. This is for matrix tables where cards make no sense: role permission matrix, cashflow, allocation.
4. The bulk-select checkbox stays in the card corner. Selecting still drives the existing bulk bar, which docks above the bottom nav.

### B9. Data formatting

- **Money**: the currency comes from the existing `currency` setting (default `BDT`). Show it as `৳ 1,250` only when the currency is BDT; otherwise use the setting's code. Mono, tabular, right-aligned. (See C3 about the ৳ glyph.)
- **Speed**: `10M / 10M` mono; units `K/M/G` as stored in the plan rate string.
- **Bytes**: existing `|bytes` filter, mono.
- **IP / MAC**: mono. MAC upper-case with colons. Copy-on-click (existing behaviour in `app.js`).
- **Dates**: `19 Nov 2026`; relative ("3h ago") for sessions and logs with the full timestamp in the tooltip.

### B10. Motion and accessibility

- Durations 120 ms (hover), 200 ms (sheets, dialogs). Respect `prefers-reduced-motion`.
- WCAG AA contrast for all text. Status is never shown by colour alone: it always has a label or icon.
- Keyboard: visible focus everywhere, `Esc` closes dialogs and sheets, `/` focuses search, `[` toggles the sidebar.
- Touch targets ≥ 44 px on phones.

### B11. Customer portal and print

- The **portal** (`portal_base.html`) uses the same tokens and fonts with a simpler shell: top bar + bottom nav (Home · Usage · Invoices · Pay).
- **Receipts / invoices** keep a print stylesheet: light colours always, no navigation, mono amounts.

---

## Part C — Conflicts between the brief and the codebase

| # | Brief says | Reality | Decision |
|---|---|---|---|
| C1 | Audit "Flask blueprints, SQLAlchemy models" | There are no blueprints and no ORM. Routing is a custom registry, data access is raw PyMySQL SQL. | Document what exists (A1). **Do not** add SQLAlchemy or blueprints in the redesign. It would touch every query and is the biggest risk of breaking business logic. |
| C2 | Use Plus Jakarta Sans and JetBrains Mono | Fonts must be self-hosted (A4 #6). | **Done (Phase 1):** `plusjakartasans-600/700.woff2` and `jetbrainsmono-400/500.woff2` (latin subset, `@fontsource` 5.2.8, OFL) are in `static/fonts/`. Their OFL licence text still needs adding next to `LICENSE-Inter.txt`. No Google Fonts CDN. |
| C3 | BDT ৳ amounts in JetBrains Mono | The ৳ sign (U+09F3) is in the Bengali Unicode block. Neither JetBrains Mono nor Inter is expected to include it, so the browser will draw it from a fallback font, slightly mismatched. The currency is also a setting, not always BDT. | Use the `currency` setting. Wrap the sign in its own span with a font that has it (system Bengali font, or a tiny self-hosted subset of Noto Sans Bengali containing only ৳). The digits stay in JetBrains Mono. To be verified when the font files are added. |
| C4 | Emerald / Rose / Amber for status | These colours fail WCAG AA contrast as small text on white. | Keep the exact colours for dots, fills and charts. Add darker `-text` shades for labels in light mode (B3). |
| C5 | Bottom navigation bar | The menu has 18 groups and changes by role and permission. | 4 permission-filtered slots + "More" sheet (B6), built from the same `nav.build()` list so permissions are never bypassed. |
| C6 | Auto-convert wide tables to cards | Some tables are matrices (permissions, cashflow, allocation), where cards would be unreadable. The subscriber table has bulk select and a column picker. | Automatic by default, with `data-cards="off"` for matrix tables. Bulk select kept in cards (B8). |

None of these blocks the redesign. C1 is the one that would cause serious breakage if the brief were followed literally.

---

## Part D — Implementation plan (after approval)

Each phase leaves the panel fully working and is reviewed before the next.

1. **Foundation**: new tokens, fonts, base typography; rewrite `app.css` keeping every existing class name; theme toggle stays.
2. **Shell**: new `base.html` sidebar (expanded / rail / flyout), top bar, mobile bottom nav + "More" sheet.
3. **Tables & cards**: table restyle, mobile card conversion in `app.js`, `data-cards="off"` on matrix tables.
4. **Core screens**: dashboard, subscriber list / view / form, online sessions, payments, activation.
5. **Remaining screens**: network, OLT/ONU, accounting, users & roles, settings, tickets, messaging, inventory, reports, logs; move inline styles into classes as each template is touched.
6. **Portal, login, receipts.**
7. **JSON API** (last, as agreed).

### Progress log

- **Phase 1 (done):** tokens, fonts, foundation layer in `app.css`.
- **Dashboard & subscriber prompt (done, in place of parts of phases 3–4):**
  - Decisions taken with the owner: stay on Flask + Jinja + `charts.js` (no React / Tailwind / Recharts); bandwidth comes from a new 5-minute snapshot job; "not collected" = package value of active customers minus payments (older months estimated).
  - New tables `traffic_snapshots` and `traffic_last` (additive; created by `install.sh`). `maintenance.py` → `record_traffic()` stores each open session's rate (difference between the last two interim updates) and the total. Per-customer Rx / Tx is therefore an average over about 10 minutes, not a live reading.
  - Dashboard (staff view): 4 KPI cards (active + 30-day growth, online with live pulse, collected + due, throughput), bandwidth area chart with 1H / 24H / 7D / 30D (`/api/traffic`), status donut linking to filtered lists, 6-month revenue bars. Money cards need the Accounting permission; bandwidth needs Online.
  - Subscribers: All / Online / Offline / Expired tabs, router filter (`?nas=`), IP / MAC and Rx / Tx columns, Renew (drawer), Disconnect, Edit on each row; Add subscriber drawer. Renew and Disconnect return to the list (`next`).
  - Every `.tbl` becomes a card under 768 px (`cardify()` in `app.js`), with a details toggle; templates can hint with `data-card="title|key|actions|select|hide"`.

- **NOC / backend prompt (done):**
  - Decisions taken with the owner: a systemd service instead of Celery + Redis; 3-second polling instead of Flask-SocketIO + gevent; SNMP instead of the RouterOS API; extend `radpostauth` / `audit_log` instead of SQLAlchemy models. Gunicorn stays on threads (gthread). FreeRADIUS already runs as its own systemd service, independent of the panel, so panel restarts never drop PPPoE sessions.
  - `noc.py` (`isp-panel-noc.service`): every 30 s probes all routers in parallel (2 s limit each: ping + SNMP v2c CPU / RAM / uptime / model), PPPoE count from radacct, writes `nas_health`, Telegram on offline (3 rounds) / CPU over limit (2 rounds) and on recovery; sends Disconnect-Requests for `coa_queue` (renewal of an expired or suspended customer) and for sessions that outlived the package (setting `auto_kick_expired`, on by default), with a 30-minute back-off per failed session. While it runs, the older 5-minute router-down alert in `alertlib.py` stands down.
  - FreeRADIUS post-auth query (install.sh step 2b) now also stores `reason` (Module-Failure-Message / Reply-Message), `nas_ip` and `mac`; `mod_noc.explain()` turns that plus the customer record into Wrong password / Expired / MAC mismatch / Device limit / Suspended / Unknown user.
  - Logs & System Health page (`/noc`, Logs menu, staff only): router cards, live RADIUS logins with reasons, staff actions, admin settings (default SNMP community, CPU limit, alerts, auto-disconnect). Per-router SNMP community on the Routers form.

- **bKash prompt (Tokenized Checkout v1.2.0-beta):** bKash checkout already existed in two places (portal renewals, reseller Add Balance). Per the owner's decision it was improved, not duplicated. No Redis, Celery, Blueprint or `/api/v1` routes; customers still sign in to pay; renewals use the package's own days.
  - `panel/bkash.py` is the single bKash service: token cache (50 min) with `refresh_token`, create / execute / status, and `confirm()`, which asks bKash for the status when execute times out or is refused. Credit happens only through a `pending → completed` UPDATE (database row lock = idempotent), with the renewal or balance change in the same transaction.
  - New `gateway_logs` table: every request and answer, secrets and tokens removed, kept 400 days.
  - Lost callbacks: `maintenance.py` runs `bkash.sweep()` every 5 minutes for up to 3 days. It settles paid payments, closes payments that never started, and marks payments the customer did not approve within 60 minutes as failed.
  - Portal: new checkout page (`/portal/pay`, GET) and receipt or status page (`/portal/payment/<id>/<key>`). The callback no longer needs the portal session. The pending page refreshes every 10 s and checks bKash at most once every 15 s.
  - Settings › bKash shows payments still waiting for bKash and the last 30 gateway calls.
  - CoA after a portal renewal goes through `do_renew` → `coa_queue` → NOC service (about 30 s).
  - Tested with a fake bKash server: 19 money-path checks. Not tested against the real bKash sandbox.
- **Installer prompt (wizard + HTTPS):** per the owner's decisions it stays on systemd (no Docker, no Celery), Caddy replaces nginx, RADIUS is opened only for the routers' IPs, and the second subdomain is the same panel used as the bKash callback address.
  - `install.sh` asks everything first (company, super admin, public/LAN IP, subdomains, first MikroTik, optional bKash), shows a summary and waits for confirmation. Answers without passwords go to `/etc/isp-panel/install.conf` (mode 600) and are the defaults next time. Without a terminal it reads environment variables, so scripted re-runs still work. An existing install keeps its admin password unless a new one is typed.
  - DNS pre-flight with `dig` against 1.1.1.1 and 8.8.8.8. It retries every 20 s for up to 15 minutes, also checks for a stray AAAA record, and prints the Hostinger steps. If DNS is not ready it falls back to HTTP, so Let's Encrypt is never asked in vain.
  - Caddy (`deploy/Caddyfile.tmpl`) handles HTTPS with automatic renewal, HSTS, an HTTP→HTTPS redirect (also for the bare IP), and the 4 GB limit only on the backup upload. TLS 1.2 + 1.3 by default; `TLS13_ONLY=1` allows 1.3 only. The generated file is validated before it replaces the old one, and nginx is stopped and disabled.
  - UFW: the SSH port is detected first, then 80/443. The old open-to-all 1812/1813 rules are removed. `deploy/isp-panel-fwsync` plus a 1-minute timer open RADIUS only for IPs in the `nas` table; saving a router triggers it at once.
  - `manage.py seed-install` writes branding, the super admin (with invoice name/address/phone), the first router and the bKash settings. Secrets are passed through the environment, never the command line.
  - The installer verifies HTTPS with `curl` (no `-k`) and prints the certificate issuer and expiry. It then prints the MikroTik `/radius`, `/ppp aaa` and, when the server has a private LAN IP, `/ip dns static` split-DNS commands.
  - Tested: the wizard in WSL Ubuntu 24.04 bash (scripted answers, validation, re-run, no-terminal mode), the firewall-sync diff, and seed-install. Caddy 2.8.4 in WSL: all three generated Caddyfiles (HTTPS, HTTP only, TLS 1.3 only) pass `caddy validate`. A run with an internal CA on high ports showed the HTTP→HTTPS redirects (each domain to itself, the bare IP to the panel), the headers, `X-Forwarded-Proto: https`, static caching and TLS 1.3. Not yet run end to end on a real server with Let's Encrypt.
- **CI/CD prompt (deploy workflow, update.sh, .gitignore):** per the owner's decision, built as the systemd equivalent of the Docker/Celery/Alembic brief, with the update backup skipping session-history rows.
  - `update.sh` runs in this order: stage + compile + import test as the panel user → backup to `/backups` (last 7, gzip-verified, needs 1 GB free) → `panel_schema.sql` (stops on error, nothing swapped) → collation fix → atomic dir swap → gunicorn HUP (new `ExecReload`, `--chdir`, 120 s graceful timeout) → try-restart sender and NOC → `/healthz` must report the new revision, otherwise automatic rollback. It never restarts FreeRADIUS, MariaDB or Caddy, holds an flock, and stops before doing anything when `INSTALLER_VERSION` in the new install.sh is higher than the server's.
  - `/usr/local/sbin/isp-panel-deploy` (root-owned) does a fast-forward-only `git pull` then runs update.sh, so the deploy user's sudo is limited to that one command.
  - `/healthz` (no login): 200 with `{"ok": true, "revision": ...}` or 503 when the database does not answer.
  - `.github/workflows/deploy.yml`: a check job (py_compile, bash -n, app import) → a deploy job (environment `production`, concurrency group, pinned host key, `REVISION` taken from update.sh) → a health check on `PANEL_URL` or on the server.
  - `.gitignore` at the repo root covers env files, install/deploy conf, instance/, pycache, dumps/backups, DB volumes and Caddy/Let's Encrypt data. The 37 tracked `.pyc` files were removed from the index (kept on disk).
  - Tested: update.sh in a stubbed WSL sandbox (22 checks: normal, schema failure, health failure → rollback, installer needed, syntax error, import error, concurrent run), `/healthz` with the database up and down, and the workflow YAML parse. Not yet run on GitHub or a real server.
- **Repository layout:** the project moved from `isp-radius-panel/` to the repository root (`install.sh`, `update.sh`, `panel/`, `sql/`, `deploy/`; git history kept). The old panel README is now `docs/USER-GUIDE.md` and this file is `docs/DESIGN.md`. A new root `README.md` is the Linux install guide. Paths were updated in the workflow, the scripts and the docs. The deploy wrapper looks for `update.sh` at the root and falls back to the old folder. Scripts are marked executable, and `.gitattributes` keeps Unix line endings for shell, Python, SQL and template files.
- **Page test on a real database (`tests/test_pages.py`, CI job `pages`):** GitHub starts MariaDB 10.11 as a service and loads the FreeRADIUS schema from Ubuntu's `freeradius-config` package plus `sql/panel_schema.sql`. The test applies the same collation step as install.sh (reading install.sh's own table list), seeds realistic data (3 packages, 8 customers in every state, admin / reseller / sub-reseller / staff, 2 routers, sessions, logins, payments, bKash rows) and fills every other table with two plain rows. Then it opens every GET page as admin, reseller, staff and portal customer. Processes and outgoing HTTP are blocked; a 500 fails the job with a GitHub annotation on the failing file and line; `deploy` needs `check` and `pages`. A local run on a portable MariaDB 10.11.10 gave 188 pages 200 and no errors. The 23 failures seen with the fake test database were all fake-data gaps; the same pages answer 200 (or 403 where the role has no access). A deliberately broken template was caught with the exact file and line.

Optional, if approved: HTTPS in `install.sh` (A5 #1), and a small smoke-test suite that logs in as each role and loads every GET page (A5 #2). This would catch broken templates during phases 1–6.
