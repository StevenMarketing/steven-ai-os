/* Steven AI OS — Google Reviews Manager : app shell + UI logic (Group 7 / T18/T19).
 *
 * Plain JavaScript (fetch + DOM). No framework, no build step, no CDN.
 * Talks only to the local JSON API served by src/gbp/api/server.py.
 *
 * HARD RULE: nothing here publishes a reply. There is no publish call in the
 * API and this UI never claims a reply was published. The only publish-related
 * endpoint is a dry-run guard, surfaced as an explicit "PUBLISH BLOCKED" notice.
 *
 * CSP note: script-src is 'self' — this file is the whole script surface, there
 * is no inline <script> and no eval. All user-facing text comes from STRINGS.
 * Charts are inline SVG built with createElementNS + attributes (never a
 * style="" attribute), so no CSSOM/style-attribute relaxation is needed.
 */
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";

  // ------------------------------------------------------------------ //
  // i18n scaffold
  // ------------------------------------------------------------------ //
  // A single map of every user-facing string. English is the default; the
  // structure is locale-keyed so a `vi: { ... }` map can be added later with
  // no call-site changes. `t()` always falls back to en, then to the key.
  var DEFAULT_LOCALE = "en";

  var STRINGS = {
    en: {
      shell: {
        appTitle: "Steven AI OS",
        module: "Google Reviews Manager",
        version: "v2.0.0",
        toggleNav: "Toggle navigation",
        actor: "Actor"
      },
      nav: {
        overview: "Overview",
        locations: "Locations",
        reviewInbox: "Review Inbox",
        aiDrafts: "AI Drafts",
        needsApproval: "Needs Approval",
        approved: "Approved",
        rejected: "Rejected",
        auditHistory: "Audit History",
        systemStatus: "System Status",
        settings: "Settings",
        googleConnection: "Google Connection"
      },
      state: {
        loading: "Loading…",
        empty: "No data available.",
        notTracked: "Not tracked",
        error: "Request failed.",
        notPermitted: "Your role does not permit this action.",
        noLocations: "No locations."
      },
      status: {
        checking: "Checking status…",
        googleConnected: "Google: connected",
        googleNotConnected: "Google: not connected",
        apiOk: "API: ok",
        apiError: "API: error",
        demo: "Demo data",
        degraded: "Sync degraded — {n} location(s) need attention",
        unknown: "Status unknown"
      },
      panel: {
        syncStatus: "Sync Status",
        usage: "AI Usage / Cost",
        systemStatusIntro: "Live sync health and per-location counters, read from /api/locations/status and /api/dashboard.",
        settingsEmpty: "No configurable options yet."
      },
      google: {
        // The readiness line is a plain-text sentence: a clear "not ready"
        // message is required, never a fake "connected".
        notReady: "Google is not connected. No live Google call is made from this page — the preview below is FIXTURE DATA only.",
        ready: "Google is connected. Live reads stay disabled in this build; the preview below is still FIXTURE DATA only.",
        fixtureLabel: "FIXTURE DATA — sample values, not read from Google.",
        locationsTitle: "Preview — up to five locations",
        reviewsTitle: "Read-only reviews for the selected location",
        selectLocation: "Select a location above to see its fixture reviews.",
        noReviews: "No fixture reviews for this location.",
        colName: "Business name",
        colAddress: "Address",
        colZip: "ZIP",
        colId: "ID",
        colRating: "Rating",
        colComment: "Comment",
        view: "Preview reviews"
      },
      dashboard: {
        summary: "Total drafts tracked: {n}. DRAFT MODE — nothing is published to Google.",
        drafts: "drafts",
        aiDrafts: "AI drafts",
        needsApproval: "needs approval",
        syncErrors: "SYNC ERRORS",
        aiCalls: "AI calls",
        inputTokens: "input tokens",
        outputTokens: "output tokens",
        estimatedCost: "estimated cost"
      },
      overview: {
        totalLocations: "Locations",
        totalReviews: "Total reviews",
        unanswered: "Unanswered reviews",
        needsApproval: "Needs approval",
        approved: "Approved replies",
        published: "Published replies",
        syncErrors: "Sync errors",
        estCost: "Estimated AI cost",
        ratingTitle: "Rating distribution",
        noDistribution: "No rating data.",
        trendTitle: "Review trend",
        trendUnavailable: "Not tracked — no timestamp data",
        trendLabel: "Reviews per day",
        attentionTitle: "Locations needing attention",
        noAttention: "No location needs attention.",
        recentTitle: "Recent reviews",
        noRecent: "No reviews.",
        openLocation: "Open in Locations",
        // Unit sub-captions: every counter states WHAT it counts. Reviews and
        // reply drafts are different units (a review can have 0..1 draft), so
        // the two must never be read as the same number.
        unitsCaption: "Units differ: a review can have 0..1 reply draft. " +
          "\"Reviews\" counts review records; \"drafts\" counts reply drafts.",
        unitLocations: "business locations",
        unitReviews: "review records",
        unitUnanswered: "no reply yet",
        unitNeedsApproval: "reply drafts awaiting review",
        unitApproved: "approved reply drafts",
        unitPublished: "published reply drafts (never in DRAFT MODE)",
        unitSyncErrors: "locations with a failed last sync",
        unitCost: "sum of api_usage (USD)"
      },
      sync: {
        synced: "Synced",
        syncing: "Syncing",
        failed: "Failed",
        partial: "Partial",
        notTracked: "Not tracked",
        never: "Never"
      },
      locations: {
        businessName: "Business Name",
        cityState: "City / State",
        zipCode: "ZIP Code",
        reviews: "Total reviews",
        unanswered: "Unanswered",
        needsApproval: "Needs Approval",
        syncStatus: "Sync Status",
        lastSync: "Last Sync",
        brandProfile: "Brand Profile",
        autoPublish: "Auto-publish policy",
        actions: "Actions",
        view: "View",
        openDetails: "Open details",
        syncHistory: "Sync history",
        noSyncRuns: "No sync runs recorded.",
        search: "Search",
        reset: "Reset",
        perPage: "Per page",
        pageInfo: "Page {page} of {pages} — {total} locations",
        empty: "No locations match these filters.",
        configured: "configured",
        notConfigured: "not configured",
        detailTitle: "Location detail",
        reviewsTitle: "Recent reviews for this location",
        latestError: "Last error"
      },
      queue: {
        empty: "No drafts match these filters.",
        pageInfo: "Page {page} of {pages} — {total} total",
        selected: "{n} selected",
        original: "Original review",
        aiDraft: "AI draft",
        noComment: "(no comment)",
        emptyDraft: "(empty)",
        edit: "Edit",
        approve: "Approve",
        reject: "Reject",
        save: "Save"
      },
      inbox: {
        listTitle: "Reviews",
        detailTitle: "Review detail",
        count: "{n} review(s)",
        noDraft: "Reply not generated",
        manual: "Manual approval required — cannot be bulk-approved",
        back: "Back",
        fullComment: "Comment",
        draft: "AI draft reply",
        validator: "Validator reason",
        audit: "Audit history",
        noAudit: "No audit entries yet.",
        date: "Date",
        language: "Language",
        status: "Status",
        editReply: "Edit Reply",
        approve: "Approve",
        reject: "Reject",
        save: "Save",
        empty: "No reviews match these filters.",
        pageInfo: "Page {page} of {pages} — {total} total",
        selected: "{n} selected",
        placeholder: "Select a review to see its details.",
        confirmApprove: "Approve this reply? Nothing is published to Google (DRAFT MODE). Continue?",
        confirmReject: "Reject this reply? Continue?",
        confirmBulk: "Approve {n} selected draft(s)? Low-star / sensitive reviews are skipped. Nothing is published (DRAFT MODE). Continue?"
      },
      audit: {
        title: "Audit History",
        colTimestamp: "Timestamp",
        colActor: "Actor",
        colAction: "Action",
        colLocation: "Location",
        colReviewDraft: "Review/Draft",
        colResult: "Result",
        colDetails: "Details",
        search: "Search actor or action",
        filterAction: "Action",
        filterSince: "Since",
        filterUntil: "Until",
        any: "Any",
        apply: "Apply",
        reset: "Reset",
        pageInfo: "Page {page} of {pages} — {total} entries",
        empty: "No activity yet — approve or reject a reply to create audit entries.",
        noMatch: "No audit entries match these filters."
      }
    }
  };

  function t(key, vars) {
    var parts = String(key).split(".");
    var cur = STRINGS[DEFAULT_LOCALE];
    for (var i = 0; i < parts.length && cur != null; i++) cur = cur[parts[i]];
    if (typeof cur !== "string") return String(key);
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        cur = cur.replace("{" + k + "}", String(vars[k]));
      });
    }
    return cur;
  }

  // ------------------------------------------------------------------ //
  // markers / constants
  // ------------------------------------------------------------------ //
  // Reasons a queue row may NOT be bulk-approved. Kept as stable marker
  // strings so tests can assert them; they mirror the server-side reasons
  // from ApprovalService._auto_publish_eligible.
  var BULK_BLOCK = {
    LOW_STARS: "bulk_block_low_stars_1_2",
    SENSITIVE: "bulk_block_sensitive"
  };

  var BULK_BLOCK_LABEL = {
    "bulk_block_low_stars_1_2": "1-2 star review — manual approval required",
    "bulk_block_sensitive": "sensitive review — manual approval required"
  };

  // Canonical areas (the 8 the API/dashboard exposes) + 2 shell-only panels.
  var AREAS = {
    dashboard:   { section: "dashboard" },
    locations:   { section: "locations" },
    inbox:       { section: "queue", title: "Review Inbox",  status: "" },
    drafts:      { section: "queue", title: "AI Drafts",     status: "draft" },
    needs_review:{ section: "queue", title: "Needs Approval", status: "needs_review" },
    approved:    { section: "queue", title: "Approved",      status: "approved" },
    rejected:    { section: "queue", title: "Rejected",      status: "rejected" },
    audit:       { section: "audit" },
    status:      { section: "status" },     // System Status shell panel
    settings:    { section: "settings" },   // Settings shell panel
    google:      { section: "google" }      // Google Connection (offline fixture preview)
  };

  // NAV -> AREA MAPPING. The old `?area=` values keep working and the new nav
  // labels get aliases onto the same 8 areas:
  //   Overview       -> dashboard     (overview, dashboard)
  //   Locations      -> locations     (locations)
  //   Review Inbox   -> inbox         (inbox, reviews, review-inbox)
  //   AI Drafts      -> drafts        (drafts, ai-drafts, aidrafts)
  //   Needs Approval -> needs_review  (needs_review, needs-approval, needs-review)
  //   Approved       -> approved      (approved)
  //   Rejected       -> rejected      (rejected)
  //   Audit History  -> audit         (audit, audit-history)
  //   System Status  -> status        (status, system-status)
  //   Settings       -> settings      (settings)
  var AREA_ALIASES = {
    overview: "dashboard",
    reviews: "inbox",
    "review-inbox": "inbox",
    "ai-drafts": "drafts",
    aidrafts: "drafts",
    "needs-approval": "needs_review",
    "needs-review": "needs_review",
    "audit-history": "audit",
    "system-status": "status",
    "google-connection": "google",
    connection: "google"
  };

  //: Primary nav order (the 8 areas), used to keep active-state in sync.
  var AREA_ORDER = ["dashboard", "locations", "inbox", "drafts",
                    "needs_review", "approved", "rejected", "audit"];

  var FILTER_KEYS = ["location_id", "stars", "status", "language", "since", "q"];

  //: Server-driven pagination (the server clamps page_size to 100).
  var PAGE_SIZE = 25;

  function resolveArea(raw) {
    if (!raw) return null;
    if (AREAS[raw]) return raw;
    var alias = AREA_ALIASES[raw];
    return (alias && AREAS[alias]) ? alias : null;
  }

  // ------------------------------------------------------------------ //
  // tiny helpers
  // ------------------------------------------------------------------ //
  function $(id) { return document.getElementById(id); }

  function el(tag, opts, children) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.text != null) node.textContent = String(opts.text);
    if (opts.cls) node.className = opts.cls;
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (k) {
        node.setAttribute(k, opts.attrs[k]);
      });
    }
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  function svgEl(tag, attrs, children) {
    var node = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) {
      node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  function stars(rating) {
    var n = Number(rating) || 0;
    if (n < 1) return "\u2606\u2606\u2606\u2606\u2606";
    return "\u2605".repeat(n) + "\u2606".repeat(5 - n);
  }

  // ------------------------------------------------------------------ //
  // auth state (Group 8A / T22) -- populated from GET /api/me
  // ------------------------------------------------------------------ //
  // When the server enforces auth (opt-in `--auth`) the actor recorded on
  // mutations is the LOGGED-IN user and a CSRF header is sent; in mock mode
  // (auth=None) everything stays exactly as before (free-text box, no CSRF).
  var auth = { enabled: false, username: null, csrf: null,
               permissions: [], isAdmin: false };

  //: Whether the current session may perform ``action`` (view/edit/approve/
  //: reject/admin). Mock mode (no auth) => unrestricted, exactly as before.
  //: The backend stays authoritative; this only hides actions that would 403.
  function can(action) {
    if (!auth.enabled) return true;
    if (auth.isAdmin) return true;
    return auth.permissions.indexOf(action) !== -1;
  }

  function actor() {
    if (auth.enabled && auth.username) return auth.username;
    var v = $("actor-input") && $("actor-input").value;
    return (v && v.trim()) || "manager";
  }

  function fmtMoney(v) { return "$" + Number(v || 0).toFixed(4); }

  function clearNode(node) { if (node) node.textContent = ""; }

  // ------------------------------------------------------------------ //
  // standard panel states (loading / empty / error / not-tracked)
  // ------------------------------------------------------------------ //
  function renderLoading(container) {
    if (!container) return;
    clearNode(container);
    var wrap = el("div", { cls: "skeleton", attrs: { "aria-busy": "true" } }, [
      el("div", { cls: "spinner", attrs: { role: "progressbar", "aria-label": t("state.loading") } }),
      el("div", { cls: "skeleton-row w-80" }),
      el("div", { cls: "skeleton-row w-60" })
    ]);
    container.appendChild(wrap);
  }

  function renderEmpty(container, message) {
    if (!container) return;
    clearNode(container);
    container.appendChild(el("p", { cls: "state state-empty", text: message || t("state.empty") }));
  }

  function renderErrorState(container, err) {
    if (!container) return;
    clearNode(container);
    var msg = (err && err.message) ? err.message : String(err);
    container.appendChild(el("div", { cls: "state state-error", attrs: { role: "alert" } }, [
      el("span", { text: t("state.error") }),
      el("span", { cls: "state-detail", text: msg })
    ]));
  }

  function notTrackedOr(value, render) {
    // Never invent numbers: a null/undefined API value is shown as "Not tracked".
    if (value == null) return el("div", { cls: "k", text: t("state.notTracked") });
    return render(value);
  }

  // ------------------------------------------------------------------ //
  // HTTP with explicit 409 / 423 handling
  // ------------------------------------------------------------------ //
  function ApiError(status, body) {
    this.status = status;
    this.body = body || {};
    this.message = describe(status, this.body);
    this.name = "ApiError";
  }
  ApiError.prototype = Object.create(Error.prototype);

  function describe(status, body) {
    var detail = body && (body.detail || body.reason || body.error);
    if (status === 409) {
      return "Conflict (409): this draft was changed by someone else since " +
        "you loaded it. Reload the queue and try again." +
        (detail ? " [" + detail + "]" : "");
    }
    if (status === 423) {
      return "Publish blocked (423): " + (body && body.reason ? body.reason : detail) +
        ". This reply must not be published.";
    }
    if (status === 404) {
      return "Not found (404): " + (detail || "the item no longer exists.");
    }
    if (status === 400) {
      return "Invalid request (400): " + (detail || "bad input.");
    }
    if (status === 403) {
      // Clear authorization message: the action is not permitted for this
      // user/role/location (or the CSRF token was rejected).
      var kind = body && body.error;
      if (kind === "csrf_failed") {
        return "Forbidden (403): your session's security token was rejected — reload the page and sign in again.";
      }
      if (kind === "inactive") {
        return "Forbidden (403): this account is inactive.";
      }
      return "Not permitted (403): your role or location scope does not allow this action.";
    }
    return "Request failed (" + status + "): " + (detail || "unknown error");
  }

  function api(method, path, body, opts) {
    opts = opts || {};
    var mutating = method !== "GET" && method !== "HEAD";
    var req = { method: method, headers: {}, credentials: "same-origin" };
    // CSRF: every state-changing request carries the per-session token.
    if (mutating && auth.csrf) req.headers["X-CSRF-Token"] = auth.csrf;
    if (body != null) {
      req.headers["Content-Type"] = "application/json";
      req.body = JSON.stringify(body);
    }
    return fetch(path, req).then(function (resp) {
      return resp.text().then(function (txt) {
        var data = null;
        if (txt) { try { data = JSON.parse(txt); } catch (e) { data = null; } }
        // A 401 means the session expired/revoked: return to the login view.
        if (resp.status === 401 && !opts.skipAuthRedirect) handleUnauthorized();
        if (!resp.ok) throw new ApiError(resp.status, data);
        return data;
      });
    });
  }

  // ------------------------------------------------------------------ //
  // notices
  // ------------------------------------------------------------------ //
  function notify(message, kind, targetId) {
    var box = $(targetId || "global-notice");
    if (!box) return;
    box.textContent = message;
    box.className = "notice" + (kind ? " " + kind : "");
    box.hidden = false;
  }
  function clearNotice(targetId) {
    var box = $(targetId || "global-notice");
    if (box) { box.hidden = true; box.textContent = ""; }
  }
  function reportError(err) {
    var msg = (err && err.message) ? err.message : String(err);
    notify(msg, "error");
  }

  // ------------------------------------------------------------------ //
  // URL <-> state (filters must be shareable/refreshable)
  // ------------------------------------------------------------------ //
  var state = { area: "dashboard", filters: {}, page: 1, pages: 1, total: 0,
                selectedReview: null };

  function readUrl() {
    var q = new URLSearchParams(window.location.search);
    var area = q.get("area");
    state.area = resolveArea(area) || "dashboard";
    state.filters = {};
    FILTER_KEYS.forEach(function (k) {
      var v = q.get(k);
      if (v) state.filters[k] = v;
    });
    var p = parseInt(q.get("page"), 10);
    state.page = (p && p > 0) ? p : 1;
  }

  function writeUrl() {
    var q = new URLSearchParams();
    q.set("area", state.area);
    FILTER_KEYS.forEach(function (k) {
      if (state.filters[k]) q.set(k, state.filters[k]);
    });
    if (state.page && state.page > 1) q.set("page", String(state.page));
    var url = window.location.pathname + "?" + q.toString();
    window.history.replaceState(null, "", url);
  }

  // ------------------------------------------------------------------ //
  // app shell: mobile drawer + active nav
  // ------------------------------------------------------------------ //
  function navItems() {
    return Array.prototype.slice.call(document.querySelectorAll(".sidebar [data-area]"));
  }

  function setNavActive(areaKey) {
    navItems().forEach(function (item) {
      var on = item.getAttribute("data-area") === areaKey;
      item.classList.toggle("active", on);
      if (on) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
  }

  function openDrawer() {
    var sb = $("sidebar");
    var ov = $("nav-overlay");
    var btn = $("nav-toggle");
    if (sb) sb.classList.add("open");
    if (ov) ov.hidden = false;
    if (btn) btn.setAttribute("aria-expanded", "true");
  }
  function closeDrawer() {
    var sb = $("sidebar");
    var ov = $("nav-overlay");
    var btn = $("nav-toggle");
    if (sb) sb.classList.remove("open");
    if (ov) ov.hidden = true;
    if (btn) btn.setAttribute("aria-expanded", "false");
  }
  function drawerIsOpen() {
    var sb = $("sidebar");
    return !!(sb && sb.classList.contains("open"));
  }

  function bindShell() {
    var btn = $("nav-toggle");
    var ov = $("nav-overlay");
    if (btn) {
      btn.addEventListener("click", function () {
        if (drawerIsOpen()) closeDrawer(); else openDrawer();
      });
    }
    if (ov) ov.addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && drawerIsOpen()) {
        closeDrawer();
        var b = $("nav-toggle");
        if (b) b.focus();
      }
    });
    closeDrawer();
  }

  // ------------------------------------------------------------------ //
  // rendering
  // ------------------------------------------------------------------ //
  function showArea(areaKey) {
    var cfg = AREAS[areaKey] || AREAS.dashboard;
    var prevArea = state.area;
    if (areaKey !== state.area) state.area = areaKey;
    // Switching to a queue area from elsewhere clears the detail selection.
    if (cfg.section === "queue" && prevArea !== areaKey) state.selectedReview = null;

    setNavActive(areaKey);

    // sections
    $("area-dashboard").hidden = cfg.section !== "dashboard";
    $("area-locations").hidden = cfg.section !== "locations";
    $("area-queue").hidden = cfg.section !== "queue";
    $("area-audit").hidden = cfg.section !== "audit";
    $("area-status").hidden = cfg.section !== "status";
    $("area-settings").hidden = cfg.section !== "settings";
    $("area-google").hidden = cfg.section !== "google";

    if (cfg.section === "dashboard") renderDashboard();
    else if (cfg.section === "locations") renderLocations();
    else if (cfg.section === "audit") renderAudit();
    else if (cfg.section === "status") renderSystemStatus();
    else if (cfg.section === "settings") renderSettings();
    else if (cfg.section === "google") renderGoogleConnection();
    else renderQueue(cfg);

    closeDrawer();
  }

  // --- topbar status pill ------------------------------------------- //
  function setStatusPill(tone, text) {
    var pill = $("status-pill");
    var lbl = $("status-pill-text");
    var dot = $("nav-status-dot");
    if (pill) pill.className = "status-pill" + (tone ? " " + tone : "");
    if (dot) dot.className = "dot" + (tone ? " " + tone : "");
    if (lbl) lbl.textContent = text;
  }

  function refreshStatusPill() {
    // THREE DISTINCT STATES, never a false "healthy":
    //   1. Google connection   -> real signal only (google_connected)
    //   2. Internal API health  -> "ok" because THIS fetch answered
    //   3. Demo/mock mode       -> mock_mode flag
    // A failed /api/dashboard fetch is "API: error" -- it can NEVER be
    // rendered as healthy.
    api("GET", "/api/dashboard").then(function (data) {
      data = data || {};
      var connected = data.google_connected === true;
      var mock = data.mock_mode === true;
      var parts = [
        connected ? t("status.googleConnected") : t("status.googleNotConnected"),
        t("status.apiOk")
      ];
      if (mock) parts.push(t("status.demo"));
      setStatusPill(connected ? "ok" : "", parts.join(" \u00b7 "));
    }).catch(function (err) {
      setStatusPill(
        "danger",
        t("status.apiError") + " \u00b7 " + t("status.googleNotConnected")
      );
      reportError(err);
    });
  }

  // ------------------------------------------------------------------ //
  // auth view: login / logout (Group 8A / T22). Hidden in mock mode.
  // ------------------------------------------------------------------ //
  function applyActor() {
    var input = $("actor-input");
    var name = $("actor-name");
    var label = $("actor-label");
    if (!input || !name) return;
    if (auth.enabled && auth.username) {
      // Logged in: show the username instead of the free-text Actor box.
      input.hidden = true;
      if (label) label.hidden = true;
      name.hidden = false;
      name.textContent = auth.username;
    } else {
      input.hidden = false;
      if (label) label.hidden = false;
      name.hidden = true;
      name.textContent = "";
    }
  }

  function showLogin() {
    var v = $("login-view");
    if (v) v.hidden = false;
    var u = $("login-username");
    if (u) u.focus();
  }

  function hideLogin() {
    var v = $("login-view");
    if (v) v.hidden = true;
    var err = $("login-error");
    if (err) { err.hidden = true; err.textContent = ""; }
  }

  function handleUnauthorized() {
    // The session is gone: drop identity and return to the login view.
    auth.enabled = true;
    auth.username = null;
    auth.csrf = null;
    auth.permissions = [];
    auth.isAdmin = false;
    applyActor();
    showLogin();
  }

  function refreshAuth() {
    return api("GET", "/api/me", null, { skipAuthRedirect: true })
      .then(function (me) {
        me = me || {};
        if (me.authenticated) {
          auth.enabled = true;
          auth.username = me.username;
          auth.csrf = me.csrf_token;
          auth.permissions = me.permissions || [];
          auth.isAdmin = me.is_admin === true;
          hideLogin();
        } else if (me.mock_mode) {
          // Mock mode: no auth, no CSRF -- behaves exactly as before.
          auth.enabled = false;
          auth.username = null;
          auth.csrf = null;
          auth.permissions = [];
          auth.isAdmin = false;
          hideLogin();
        }
        applyActor();
      })
      .catch(function (err) {
        if (err && err.status === 401) handleUnauthorized();
      });
  }

  function submitLogin(ev) {
    if (ev) ev.preventDefault();
    var u = $("login-username");
    var p = $("login-password");
    var err = $("login-error");
    api("POST", "/api/login",
        { username: u ? u.value : "", password: p ? p.value : "" },
        { skipAuthRedirect: true })
      .then(function (me) {
        auth.enabled = true;
        auth.username = (me && me.username) || null;
        auth.csrf = (me && me.csrf_token) || null;
        auth.permissions = (me && me.permissions) || [];
        auth.isAdmin = !!(me && me.is_admin);
        if (p) p.value = "";
        hideLogin();
        applyActor();
        showArea(state.area);
        refreshStatusPill();
      })
      .catch(function (e) {
        if (err) {
          err.textContent = (e && e.status === 401)
            ? "Invalid username or password."
            : ((e && e.message) || "Sign-in failed.");
          err.hidden = false;
        }
      });
  }

  // --- Overview / dashboard ----------------------------------------- //
  function kpiCard(value, label, tone, caption) {
    var card = el("div", { cls: "kpi" + (tone ? " " + tone : "") }, [
      el("div", { cls: "kpi-n", text: value }),
      el("div", { cls: "kpi-k", text: label })
    ]);
    // Sub-caption states the UNIT this counter counts (reviews vs reply
    // drafts are different units and must never be read as the same number).
    if (caption) card.appendChild(el("div", { cls: "kpi-c", text: caption }));
    return card;
  }

  function renderDashboard() {
    clearNotice();
    var grid = $("kpi-grid");
    var dist = $("rating-distribution");
    var trend = $("review-trend");
    var attention = $("attention-locations");
    var recent = $("recent-reviews");
    var syncPanel = $("sync-status-panel");
    [grid, dist, trend, attention, recent, syncPanel].forEach(renderLoading);

    api("GET", "/api/dashboard").then(function (data) {
      data = data || {};
      var k = data.kpis || {};
      clearNode(grid);
      // EXACTLY the 8 KPIs from /api/dashboard — no duplicate ("Drafts" vs
      // "AI drafts") cards any more.
      var defs = [
        ["total_locations", t("overview.totalLocations"), "", t("overview.unitLocations")],
        ["total_reviews", t("overview.totalReviews"), "", t("overview.unitReviews")],
        ["unanswered_reviews", t("overview.unanswered"), "", t("overview.unitUnanswered")],
        ["needs_approval", t("overview.needsApproval"), "", t("overview.unitNeedsApproval")],
        ["approved_replies", t("overview.approved"), "", t("overview.unitApproved")],
        ["published_replies", t("overview.published"), "", t("overview.unitPublished")],
        ["sync_errors", t("overview.syncErrors"), "danger", t("overview.unitSyncErrors")]
      ];
      defs.forEach(function (d) {
        var v = k[d[0]];
        grid.appendChild(kpiCard(v == null ? t("state.notTracked") : v, d[1],
          (d[2] && v) ? d[2] : "", d[3]));
      });
      grid.appendChild(kpiCard(
        k.estimated_ai_cost_usd == null ? t("state.notTracked")
                                        : fmtMoney(k.estimated_ai_cost_usd),
        t("overview.estCost"), "", t("overview.unitCost")));

      // ONE caption explaining the unit difference (reviews vs reply drafts).
      var units = $("kpi-units-caption");
      if (units) units.textContent = t("overview.unitsCaption");

      renderRatingDistribution(dist, data);
      renderReviewTrend(trend, data);
      renderAttention(attention, data.attention_locations || []);
      renderRecentReviews(recent, data.recent_reviews || []);

      var summary = $("dashboard-unanswered");
      if (summary) summary.textContent = t("dashboard.summary", { n: (data.total || 0) });
    }).catch(function (err) {
      renderErrorState(grid, err);
      reportError(err);
    });

    api("GET", "/api/locations/status").then(function (locs) {
      renderSyncStatus(syncPanel, locs || []);
    }).catch(function (err) { renderErrorState(syncPanel, err); });

    renderUsage();
  }

  // 1-5 star distribution: inline SVG bars (no style="" attributes).
  function renderRatingDistribution(wrap, data) {
    if (!wrap) return;
    clearNode(wrap);
    var rows = data.rating_distribution;
    if (!rows || !rows.length) { renderEmpty(wrap, t("overview.noDistribution")); return; }
    var total = rows.reduce(function (a, r) { return a + (Number(r.count) || 0); }, 0);
    if (!total) { renderEmpty(wrap, t("overview.noDistribution")); return; }
    rows.slice().sort(function (a, b) { return Number(b.stars) - Number(a.stars); })
      .forEach(function (r) {
        var count = Number(r.count) || 0;
        var pct = Math.round((count / total) * 100);
        var bar = svgEl("svg", {
          "class": "bar", viewBox: "0 0 100 10", preserveAspectRatio: "none",
          role: "img", "aria-label": count + " reviews at " + r.stars + " stars"
        }, [
          svgEl("rect", {
            x: "0", y: "0", height: "10", rx: "2",
            width: String(count ? Math.max(pct, 1) : 0)
          })
        ]);
        wrap.appendChild(el("div", { cls: "dist-row" }, [
          el("span", { cls: "dist-stars", text: stars(r.stars) }),
          bar,
          el("span", { cls: "dist-count", text: count + " (" + pct + "%)" })
        ]));
      });
  }

  // Review-trend chart, ONLY when the API says the data is available.
  function renderReviewTrend(wrap, data) {
    if (!wrap) return;
    clearNode(wrap);
    var available = data.review_trend_available || data.reviews_trend_available;
    var series = data.review_trend || [];
    if (!available || !series.length) {
      wrap.appendChild(el("p", {
        cls: "state state-empty", text: t("overview.trendUnavailable")
      }));
      return;
    }
    var max = series.reduce(function (a, p) { return Math.max(a, Number(p.count) || 0); }, 0) || 1;
    var total = series.reduce(function (a, p) { return a + (Number(p.count) || 0); }, 0);
    var svg = svgEl("svg", {
      "class": "trend-svg", viewBox: "0 0 " + (series.length * 12) + " 100",
      preserveAspectRatio: "none", role: "img",
      "aria-label": t("overview.trendLabel")
    });
    series.forEach(function (p, i) {
      var h = Math.max(((Number(p.count) || 0) / max) * 100, 1);
      svg.appendChild(svgEl("rect", {
        x: String(i * 12 + 2), width: "8", y: String(100 - h),
        height: String(h), rx: "1.5"
      }, [svgEl("title", {})]));
      svg.lastChild.firstChild.textContent = p.date + ": " + p.count;
    });
    wrap.appendChild(svg);
    wrap.appendChild(el("p", {
      cls: "muted",
      text: series[0].date + " \u2192 " + series[series.length - 1].date +
            " (" + total + " reviews)"
    }));
  }

  // ZIP / postal code is display-only here. It is stored as TEXT so leading
  // zeros survive, and it is NEVER fabricated: with no source data the cell
  // shows an em dash. Location name and ZIP are NOT identifiers --
  // location_resource_id remains the single unique id.
  function zipLabel(value) {
    var v = (value == null) ? "" : String(value).trim();
    return v ? v : "\u2014";
  }

  // --- Sync status / last-sync display ------------------------------ //
  // Only the states the backend actually records (ok / partial / failed) are
  // mapped. Anything unknown is shown RAW so the UI can never claim "Synced"
  // without evidence. No run recorded at all -> "Not tracked".
  var SYNC_STATUS_LABELS = {
    ok: "sync.synced",
    success: "sync.synced",
    partial: "sync.partial",
    failed: "sync.failed",
    error: "sync.failed",
    running: "sync.syncing",
    syncing: "sync.syncing",
    in_progress: "sync.syncing"
  };

  function syncLabel(status) {
    var raw = (status == null) ? "" : String(status).trim();
    if (!raw) return t("sync.notTracked");
    var key = SYNC_STATUS_LABELS[raw.toLowerCase()];
    return key ? t(key) : raw;
  }

  function fullTime(value) {
    return (value == null || value === "") ? "" : String(value);
  }

  // Relative time ("5m ago"). The stored timestamps are naive UTC, so a value
  // without an explicit offset is treated as UTC. Unparseable input is echoed
  // verbatim -- a timestamp is never invented.
  function relTime(value, absolute) {
    if (value == null || value === "") {
      return t("sync.never");
    }
    var raw = String(value);
    if (absolute) return raw;
    var iso = /[zZ]|[+-]\d\d:?\d\d$/.test(raw)
      ? raw : raw.replace(" ", "T") + "Z";
    var ms = Date.parse(iso);
    if (isNaN(ms)) return raw;
    var secs = Math.max(0, Math.floor((Date.now() - ms) / 1000));
    if (secs < 60) return "just now";
    var mins = Math.floor(secs / 60);
    if (mins < 60) return mins + "m ago";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + "h ago";
    return Math.floor(hours / 24) + "d ago";
  }

  function renderAttention(wrap, rows) {
    if (!wrap) return;
    clearNode(wrap);
    if (!rows.length) { renderEmpty(wrap, t("overview.noAttention")); return; }
    var table = el("table", { cls: "grid" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: t("locations.businessName") }),
      el("th", { text: t("locations.cityState") }),
      el("th", { text: t("locations.zipCode") }),
      el("th", { text: t("locations.unanswered") }),
      el("th", { text: t("locations.syncStatus") }),
      el("th", { text: t("locations.latestError") })
    ])]));
    var tbody = el("tbody");
    rows.forEach(function (r) {
      tbody.appendChild(el("tr", { cls: r.needs_alert ? "alert-row" : "" }, [
        el("td", { text: (r.needs_alert ? "! " : "") +
          (r.business_name || r.location_name || "—"),
          attrs: { "data-label": t("locations.businessName") } }),
        el("td", { text: [r.city, r.state].filter(Boolean).join(", "),
          attrs: { "data-label": t("locations.cityState") } }),
        el("td", { text: zipLabel(r.postal_code),
          attrs: { "data-label": t("locations.zipCode") } }),
        el("td", { text: r.unanswered_reviews != null ? r.unanswered_reviews : t("state.notTracked"),
          attrs: { "data-label": t("locations.unanswered") } }),
        el("td", { text: syncLabel(r.last_sync_status),
          attrs: { "data-label": t("locations.syncStatus") } }),
        el("td", { text: r.last_error || "—", attrs: { "data-label": t("locations.latestError") } })
      ]));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  function renderRecentReviews(wrap, rows) {
    if (!wrap) return;
    clearNode(wrap);
    if (!rows.length) { renderEmpty(wrap, t("overview.noRecent")); return; }
    var table = el("table", { cls: "grid" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: t("locations.businessName") }),
      el("th", { text: t("locations.zipCode") }),
      el("th", { text: "Rating" }),
      el("th", { text: "Date" }),
      el("th", { text: "Status" }),
      el("th", { text: "Comment" })
    ])]));
    var tbody = el("tbody");
    rows.forEach(function (r) {
      tbody.appendChild(el("tr", {}, [
        el("td", { text: r.business_name || "—", attrs: { "data-label": t("locations.businessName") } }),
        el("td", { text: zipLabel(r.postal_code), attrs: { "data-label": t("locations.zipCode") } }),
        el("td", { text: stars(r.stars) + " (" + (r.stars == null ? "?" : r.stars) + ")",
          attrs: { "data-label": "Rating" } }),
        el("td", { text: r.date || t("state.notTracked"), attrs: { "data-label": "Date" } }),
        el("td", { text: r.status || t("state.notTracked"), attrs: { "data-label": "Status" } }),
        el("td", { text: r.comment || t("queue.noComment"), attrs: { "data-label": "Comment" } })
      ]));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  function syncTable(rows) {
    var table = el("table", { cls: "grid" });
    var thead = el("thead", {}, [el("tr", {}, [
      el("th", { text: "Shop" }),
      el("th", { text: t("locations.zipCode") }),
      el("th", { text: "Last sync" }),
      el("th", { text: "Status" }),
      el("th", { text: "Last error" }),
      el("th", { text: "Failures" }),
      el("th", { text: "Unanswered" }),
      el("th", { text: "Pending" }),
      el("th", { text: "Approved" })
    ])]);
    var tbody = el("tbody");
    rows.forEach(function (l) {
      tbody.appendChild(el("tr", { cls: l.needs_alert ? "alert-row" : "" }, [
        el("td", { text: (l.needs_alert ? "! " : "") + (l.business_name || l.location_name || "—"), attrs: { "data-label": "Shop" } }),
        el("td", { text: zipLabel(l.postal_code), attrs: { "data-label": t("locations.zipCode") } }),
        el("td", { text: relTime(l.last_sync_at), attrs: { "data-label": "Last sync",
          title: fullTime(l.last_sync_at) } }),
        el("td", { text: syncLabel(l.last_sync_status), attrs: { "data-label": "Status" } }),
        el("td", { text: l.last_error || "—", attrs: { "data-label": "Last error" } }),
        el("td", { text: l.consecutive_failures != null ? l.consecutive_failures : t("state.notTracked"), attrs: { "data-label": "Failures" } }),
        el("td", { text: l.unanswered_reviews != null ? l.unanswered_reviews : t("state.notTracked"), attrs: { "data-label": "Unanswered" } }),
        el("td", { text: l.drafts_pending_review != null ? l.drafts_pending_review : t("state.notTracked"), attrs: { "data-label": "Pending" } }),
        el("td", { text: l.drafts_approved != null ? l.drafts_approved : t("state.notTracked"), attrs: { "data-label": "Approved" } })
      ]));
    });
    table.appendChild(thead);
    table.appendChild(tbody);
    return table;
  }

  function renderSyncStatus(wrap, locs) {
    if (!wrap) return;
    clearNode(wrap);
    var rows = locs || [];
    if (!rows.length) {
      wrap.appendChild(el("p", { cls: "muted", text: t("state.noLocations") }));
      return;
    }
    wrap.appendChild(syncTable(rows));
  }

  function renderUsage() {
    var wrap = $("usage-panel");
    if (!wrap) return;
    renderLoading(wrap);
    api("GET", "/api/usage").then(function (u) {
      clearNode(wrap);
      u = u || {};
      wrap.appendChild(kpiCard(u.total_calls != null ? u.total_calls : t("state.notTracked"), t("dashboard.aiCalls")));
      wrap.appendChild(kpiCard(u.input_tokens != null ? u.input_tokens : t("state.notTracked"), t("dashboard.inputTokens")));
      wrap.appendChild(kpiCard(u.output_tokens != null ? u.output_tokens : t("state.notTracked"), t("dashboard.outputTokens")));
      wrap.appendChild(kpiCard(u.estimated_cost_usd != null ? fmtMoney(u.estimated_cost_usd) : t("state.notTracked"), t("dashboard.estimatedCost")));
    }).catch(function (err) {
      renderErrorState(wrap, err);
    });
  }

  // --- Locations ---------------------------------------------------- //
  var locState = {
    page: 1, pages: 1, total: 0, page_size: 25,
    sort: "business_name", order: "asc", q: "", state: "", sync_status: ""
  };
  var locReviews = { location_id: null, page: 1, pages: 1, total: 0 };

  function locQuery() {
    var q = new URLSearchParams();
    q.set("page", String(locState.page));
    q.set("page_size", String(locState.page_size));
    q.set("sort", locState.sort);
    q.set("order", locState.order);
    if (locState.q) q.set("q", locState.q);
    if (locState.state) q.set("state", locState.state);
    if (locState.sync_status) q.set("sync_status", locState.sync_status);
    return q;
  }

  function renderLocations() {
    clearNotice();
    var wrap = $("locations-table");
    renderLoading(wrap);
    api("GET", "/api/locations?" + locQuery().toString()).then(function (res) {
      clearNode(wrap);
      var rows = (res && res.items) || [];
      locState.page = (res && res.page) || locState.page;
      locState.pages = (res && res.pages) || 0;
      locState.total = (res && res.total) || 0;
      updateLocPager();
      if (!rows.length) { renderEmpty(wrap, t("locations.empty")); return; }
      wrap.appendChild(locationsTable(rows));
    }).catch(function (err) {
      renderErrorState(wrap, err);
      reportError(err);
    });
  }

  function updateLocPager() {
    var info = $("loc-page-info");
    var pages = Math.max(1, locState.pages);
    if (info) {
      info.textContent = t("locations.pageInfo", {
        page: locState.page, pages: pages, total: locState.total
      });
    }
    if ($("loc-prev")) $("loc-prev").disabled = locState.page <= 1;
    if ($("loc-next")) $("loc-next").disabled = locState.page >= pages;
  }

  function sortableTh(label, col) {
    var btn = el("button", { cls: "th-sort", attrs: { type: "button" },
      text: label + (locState.sort === col ? (locState.order === "asc" ? " \u25B2" : " \u25BC") : "") });
    btn.addEventListener("click", function () {
      if (locState.sort === col) {
        locState.order = locState.order === "asc" ? "desc" : "asc";
      } else {
        locState.sort = col;
        locState.order = "asc";
      }
      locState.page = 1;
      renderLocations();
    });
    return el("th", {}, [btn]);
  }

  function cell(label, value) {
    return el("td", {
      text: (value == null || value === "") ? t("state.notTracked") : String(value),
      attrs: { "data-label": label }
    });
  }

  function locationsTable(rows) {
    // Desktop table keeps only the six columns that matter for managing ~500
    // shops; everything else (reviews, needs approval, brand profile,
    // auto-publish policy) stays visible in the per-shop detail panel. The
    // backend keeps returning every field.
    var table = el("table", { cls: "grid locations-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      sortableTh(t("locations.businessName"), "business_name"),
      sortableTh(t("locations.cityState"), "city"),
      sortableTh(t("locations.zipCode"), "postal_code"),
      sortableTh(t("locations.unanswered"), "unanswered"),
      el("th", { text: t("locations.syncStatus") }),
      sortableTh(t("locations.lastSync"), "last_sync")
    ])]));
    var tbody = el("tbody");
    rows.forEach(function (loc) {
      var nameBtn = el("button", {
        cls: "link",
        text: loc.business_name || loc.location_name || ("Location " + loc.id),
        attrs: { type: "button", title: t("locations.openDetails") } });
      nameBtn.addEventListener("click", function () { openLocation(loc.id); });
      tbody.appendChild(el("tr", { cls: loc.needs_alert ? "alert-row" : "" }, [
        el("td", { attrs: { "data-label": t("locations.businessName") } }, [nameBtn]),
        cell(t("locations.cityState"), [loc.city, loc.state].filter(Boolean).join(", ")),
        cell(t("locations.zipCode"), zipLabel(loc.postal_code)),
        cell(t("locations.unanswered"), loc.unanswered),
        cell(t("locations.syncStatus"), syncLabel(loc.sync_status)),
        el("td", { text: relTime(loc.last_sync), attrs: {
          "data-label": t("locations.lastSync"), title: fullTime(loc.last_sync) } })
      ]));
    });
    table.appendChild(tbody);
    return table;
  }

  function openLocation(id) {
    var panel = $("location-detail");
    if (!panel) return;
    panel.hidden = false;
    renderLoading(panel);
    api("GET", "/api/locations/" + encodeURIComponent(id)).then(function (loc) {
      clearNode(panel);
      var bp = loc.brand_profile || {};
      panel.appendChild(el("h3", { cls: "panel-title",
        text: loc.business_name || loc.location_name || ("Location " + id) }));
      var dl = el("div", { cls: "detail-grid" });
      [["City", loc.city], ["State", loc.state],
       [t("locations.zipCode"), loc.postal_code || "\u2014"],
       ["Language", loc.language],
       ["Reviews", loc.total_reviews], ["Unanswered", loc.unanswered],
       ["Needs approval", loc.needs_approval],
       ["Sync status", syncLabel(loc.sync_status)],
       ["Last sync", relTime(loc.last_sync, true)],
       [t("locations.latestError"), loc.last_error || "—"],
       ["Brand profile", loc.brand_profile_configured ? t("locations.configured") : t("locations.notConfigured")],
       ["Auto-publish policy", loc.auto_publish_policy]].forEach(function (p) {
        dl.appendChild(el("div", { cls: "detail-item" }, [
          el("span", { cls: "k", text: p[0] }),
          el("span", { cls: "v", text: (p[1] == null || p[1] === "") ? t("state.notTracked") : String(p[1]) })
        ]));
      });
      if (bp.configured) {
        dl.appendChild(el("div", { cls: "detail-item" }, [
          el("span", { cls: "k", text: "Brand business name" }),
          el("span", { cls: "v", text: bp.business_name || "—" })
        ]));
      }
      panel.appendChild(dl);

      // Sync history: straight from the run ledger. An empty ledger shows an
      // explicit empty state -- no fabricated runs.
      var histSection = el("div", { cls: "panel-sub" });
      histSection.appendChild(el("div", { cls: "label", text: t("locations.syncHistory") }));
      var histWrap = el("div", { cls: "sync-history" });
      renderLoading(histWrap);
      histSection.appendChild(histWrap);
      panel.appendChild(histSection);
      api("GET", "/api/locations/" + encodeURIComponent(id) + "/sync-runs?limit=20")
        .then(function (d) {
          clearNode(histWrap);
          var runs = (d && d.runs) || [];
          if (!runs.length) { renderEmpty(histWrap, t("locations.noSyncRuns")); return; }
          var ht = el("table", { cls: "grid" });
          ht.appendChild(el("thead", {}, [el("tr", {}, [
            el("th", { text: "When" }),
            el("th", { text: t("locations.syncStatus") }),
            el("th", { text: "Fetched" }),
            el("th", { text: "Upserted" }),
            el("th", { text: "Errors" })
          ])]));
          var hb = el("tbody");
          runs.forEach(function (r) {
            var when = r.ended_at || r.started_at;
            hb.appendChild(el("tr", {}, [
              el("td", { text: relTime(when), attrs: { title: fullTime(when) } }),
              el("td", { text: syncLabel(r.status) }),
              el("td", { text: r.fetched != null ? r.fetched : "\u2014" }),
              el("td", { text: r.upserted != null ? r.upserted : "\u2014" }),
              el("td", { text: r.errors != null ? r.errors : "\u2014" })
            ]));
          });
          ht.appendChild(hb);
          histWrap.appendChild(ht);
        })
        .catch(function (err) { renderErrorState(histWrap, err); });

      locReviews.location_id = id;
      locReviews.page = 1;
      renderLocationReviews();
    }).catch(function (err) { renderErrorState(panel, err); });
  }

  // Fetch ONLY this location's current page of reviews.
  function renderLocationReviews() {
    var panel = $("location-reviews-panel");
    var wrap = $("location-reviews");
    if (!panel || !wrap || locReviews.location_id == null) return;
    panel.hidden = false;
    $("location-reviews-title").textContent = t("locations.reviewsTitle");
    renderLoading(wrap);
    var q = new URLSearchParams();
    q.set("location_id", String(locReviews.location_id));
    q.set("page", String(locReviews.page));
    q.set("page_size", "5");
    api("GET", "/api/queue?" + q.toString()).then(function (res) {
      clearNode(wrap);
      locReviews.pages = (res && res.pages) || 0;
      locReviews.total = (res && res.total) || 0;
      $("lrev-info").textContent = t("queue.pageInfo", {
        page: (res && res.page) || 1, pages: Math.max(1, locReviews.pages),
        total: locReviews.total
      });
      $("lrev-prev").disabled = locReviews.page <= 1;
      $("lrev-next").disabled = locReviews.page >= Math.max(1, locReviews.pages);
      var rows = (res && res.items) || [];
      if (!rows.length) { renderEmpty(wrap, t("state.empty")); return; }
      wrap.appendChild(locationReviewsTable(rows));
    }).catch(function (err) { renderErrorState(wrap, err); });
  }

  function locationReviewsTable(rows) {
    var table = el("table", { cls: "grid" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: t("locations.businessName") }),
      el("th", { text: "Rating" }),
      el("th", { text: "Review" }),
      el("th", { text: t("queue.aiDraft") }),
      el("th", { text: "Status" })
    ])]));
    var tbody = el("tbody");
    rows.forEach(function (row) {
      tbody.appendChild(el("tr", {}, [
        cell(t("locations.businessName"), row.location_name || row.title),
        cell("Rating", stars(row.rating) + " (" + (row.rating == null ? "?" : row.rating) + ")"),
        cell("Review", row.comment || t("queue.noComment")),
        cell(t("queue.aiDraft"), row.draft_text || t("queue.emptyDraft")),
        cell("Status", row.status)
      ]));
    });
    table.appendChild(tbody);
    return table;
  }

  function populateStateFilter() {
    var sel = $("loc-state");
    if (!sel) return;
    api("GET", "/api/locations?page_size=100&sort=state&order=asc").then(function (res) {
      var rows = (res && res.items) || [];
      var seen = {};
      rows.forEach(function (r) {
        var st = r.state;
        if (!st || seen[st]) return;
        seen[st] = true;
        sel.appendChild(el("option", { text: st, attrs: { value: st } }));
      });
    }).catch(function () { /* best-effort */ });
  }

  // --- Audit (V2) --------------------------------------------------- //
  // Server payload from GET /api/audit: {id, ts, actor, action, entity,
  // entity_id, detail, detail_json}. Filter/search/paging happen client-side
  // over the fetched rows (offline, read-only). Audit records are NEVER
  // modified or deleted here.
  var auditAllRows = [];
  var auditFilters = { q: "", action: "", since: "", until: "" };
  var auditPage = 1;
  var AUDIT_PAGE_SIZE = 25;

  function auditResult(r) {
    var a = String(r.action || "");
    if (r.result) return String(r.result);
    if (/approve/.test(a)) return "approved";
    if (/reject/.test(a)) return "rejected";
    if (/edit/.test(a)) return "edited";
    if (/sync/.test(a)) return "synced";
    if (/created|draft/.test(a)) return a;
    return a || "\u2014";
  }

  function applyAuditFilters() {
    var q = auditFilters.q.toLowerCase();
    return auditAllRows.filter(function (r) {
      if (q) {
        var hay = ((r.actor || "") + " " + (r.action || "")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      if (auditFilters.action && String(r.action || "") !== auditFilters.action) {
        return false;
      }
      var day = String(r.ts || "").slice(0, 10);
      if (auditFilters.since && day && day < auditFilters.since) return false;
      if (auditFilters.until && day && day > auditFilters.until) return false;
      return true;
    });
  }

  function renderAuditTable(wrap, pager) {
    clearNode(wrap);
    if (!auditAllRows.length) {
      renderEmpty(wrap, t("audit.empty"));
      if (pager) pager.textContent = "";
      return;
    }
    var filtered = applyAuditFilters();
    if (!filtered.length) {
      renderEmpty(wrap, t("audit.noMatch"));
      if (pager) pager.textContent = "";
      return;
    }
    var total = filtered.length;
    var pages = Math.max(1, Math.ceil(total / AUDIT_PAGE_SIZE));
    if (auditPage > pages) auditPage = pages;
    if (auditPage < 1) auditPage = 1;
    var slice = filtered.slice((auditPage - 1) * AUDIT_PAGE_SIZE,
                               auditPage * AUDIT_PAGE_SIZE);

    var cols = [
      t("audit.colTimestamp"), t("audit.colActor"), t("audit.colAction"),
      t("audit.colLocation"), t("audit.colReviewDraft"),
      t("audit.colResult"), t("audit.colDetails")
    ];
    var table = el("table", { cls: "grid" });
    table.appendChild(el("thead", {}, [el("tr", {}, cols.map(function (c) {
      return el("th", { text: c });
    }))]));
    var tbody = el("tbody");
    slice.forEach(function (r) {
      var reviewDraft = r.entity
        ? (r.entity + (r.entity_id ? " #" + r.entity_id : ""))
        : (r.entity_id ? String(r.entity_id) : "\u2014");
      var cells = [
        r.ts || "",
        r.actor || "\u2014",
        r.action || "\u2014",
        r.location || r.business_name || "\u2014",
        reviewDraft,
        auditResult(r),
        r.detail || r.detail_json || ""
      ];
      tbody.appendChild(el("tr", {}, cells.map(function (val, i) {
        return el("td", { text: val, attrs: { "data-label": cols[i] } });
      })));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);

    if (pager) {
      pager.textContent = t("audit.pageInfo",
        { page: auditPage, pages: pages, total: total });
    }
    var prev = $("audit-prev");
    var next = $("audit-next");
    if (prev) prev.disabled = auditPage <= 1;
    if (next) next.disabled = auditPage >= pages;
  }

  function renderAudit() {
    clearNotice();
    var wrap = $("audit-table");
    renderLoading(wrap);
    api("GET", "/api/audit").then(function (rows) {
      auditAllRows = rows || [];
      // Populate the action filter from what we actually have (read-only).
      var sel = $("audit-action");
      if (sel) {
        var seen = {};
        clearNode(sel);
        sel.appendChild(el("option", { text: t("audit.any"), attrs: { value: "" } }));
        auditAllRows.forEach(function (r) {
          var a = String(r.action || "");
          if (!a || seen[a]) return;
          seen[a] = true;
          sel.appendChild(el("option", { text: a, attrs: { value: a } }));
        });
        sel.value = auditFilters.action || "";
      }
      renderAuditTable(wrap, $("audit-page-info"));
    }).catch(function (err) {
      renderErrorState(wrap, err);
      reportError(err);
    });
  }

  function bindAuditControls() {
    var search = $("audit-search");
    var action = $("audit-action");
    var since = $("audit-since");
    var until = $("audit-until");
    function read(reset) {
      auditFilters.q = search ? search.value : "";
      auditFilters.action = action ? action.value : "";
      auditFilters.since = since ? since.value : "";
      auditFilters.until = until ? until.value : "";
      auditPage = 1;
      if (reset) renderAudit();
      else renderAuditTable($("audit-table"), $("audit-page-info"));
    }
    if ($("audit-apply")) $("audit-apply").addEventListener("click", function () { read(false); });
    if (search) search.addEventListener("input", function () { read(false); });
    if (action) action.addEventListener("change", function () { read(false); });
    if (since) since.addEventListener("change", function () { read(false); });
    if (until) until.addEventListener("change", function () { read(false); });
    if ($("audit-reset")) {
      $("audit-reset").addEventListener("click", function () {
        if (search) search.value = "";
        if (action) action.value = "";
        if (since) since.value = "";
        if (until) until.value = "";
        read(false);
      });
    }
    if ($("audit-prev")) {
      $("audit-prev").addEventListener("click", function () {
        if (auditPage > 1) {
          auditPage -= 1;
          renderAuditTable($("audit-table"), $("audit-page-info"));
        }
      });
    }
    if ($("audit-next")) {
      $("audit-next").addEventListener("click", function () {
        auditPage += 1;
        renderAuditTable($("audit-table"), $("audit-page-info"));
      });
    }
  }

  // --- System Status (shell panel) ---------------------------------- //
  function renderSystemStatus() {
    clearNotice();
    var cards = $("system-status-cards");
    var panel = $("system-status-panel");
    renderLoading(cards);
    renderLoading(panel);
    Promise.all([
      api("GET", "/api/dashboard"),
      api("GET", "/api/locations/status")
    ]).then(function (res) {
      var counts = res[0] || {};
      var locs = res[1] || [];
      clearNode(cards);
      var alerts = locs.filter(function (l) { return l.needs_alert; }).length;
      cards.appendChild(kpiCard(locs.length, "locations", "", t("overview.unitLocations")));
      cards.appendChild(kpiCard(alerts, t("dashboard.syncErrors"), alerts ? "danger" : "", t("overview.unitSyncErrors")));
      cards.appendChild(kpiCard(counts.needs_review != null ? counts.needs_review : t("state.notTracked"), t("dashboard.needsApproval"), "", t("overview.unitNeedsApproval")));
      cards.appendChild(kpiCard(counts.total != null ? counts.total : t("state.notTracked"), "drafts tracked", "", "reply draft records"));
      if (!locs.length) { renderEmpty(panel, t("state.noLocations")); }
      else { clearNode(panel); panel.appendChild(syncTable(locs)); }
    }).catch(function (err) {
      renderErrorState(cards, err);
      renderErrorState(panel, err);
      reportError(err);
    });
  }

  // --- Settings (honest panel, no fake toggles) --------------------- //
  function renderSettings() {
    clearNotice();
    var wrap = $("settings-panel");
    if (!wrap) return;
    // Static, honest content lives in index.html; nothing to fetch. This only
    // makes sure the empty-state string is the i18n one.
    var empty = $("settings-empty");
    if (empty) empty.textContent = t("panel.settingsEmpty");
  }

  // --- Google Connection (offline readiness + FIXTURE preview) ------ //
  // EVERY row below is hard-coded FIXTURE DATA -- this module never calls
  // Google, never opens a connect flow, and there is no publish endpoint. The
  // readiness line only reflects the existing /api/dashboard google_connected
  // flag, so session / CSRF behaviour is unchanged (a plain read).
  var GOOGLE_PREVIEW_FIXTURE = {
    label: "FIXTURE DATA",
    locations: [
      { id: "locations/fx-001", name: "Fixture Nails — Garden Grove",
        address: "14001 Brookhurst St, Garden Grove, CA", zip: "92843" },
      { id: "locations/fx-002", name: "Fixture Nails — Westminster",
        address: "9200 Bolsa Ave, Westminster, CA", zip: "92683" },
      { id: "locations/fx-003", name: "Fixture Nails — Costa Mesa",
        address: "3033 Bristol St, Costa Mesa, CA", zip: "92626" },
      { id: "locations/fx-004", name: "Fixture Nails — Anaheim",
        address: "1211 S Euclid St, Anaheim, CA", zip: "92804" },
      { id: "locations/fx-005", name: "Fixture Nails — Fountain Valley",
        address: "18000 Euclid St, Fountain Valley, CA", zip: "92708" }
    ],
    reviews: {
      "locations/fx-001": [
        { rating: 5, comment: "Great gel manicure, very gentle.", author: "Fixture A" },
        { rating: 4, comment: "Nice staff, a little wait.", author: "Fixture B" }
      ],
      "locations/fx-002": [
        { rating: 5, comment: "Love my nails every time!", author: "Fixture C" }
      ],
      "locations/fx-003": [
        { rating: 2, comment: "Rushed pedicure, uneven polish.", author: "Fixture D" }
      ],
      "locations/fx-004": [],
      "locations/fx-005": [
        { rating: 5, comment: "Clean shop and friendly team.", author: "Fixture E" }
      ]
    }
  };

  //: The preview never shows more than five locations (the fixture is the cap).
  var GOOGLE_PREVIEW_MAX = 5;

  function googleFixtureLocations() {
    return (GOOGLE_PREVIEW_FIXTURE.locations || []).slice(0, GOOGLE_PREVIEW_MAX);
  }

  function renderGoogleFixtureReviews(locationId) {
    var wrap = $("google-preview-reviews");
    if (!wrap) return;
    clearNode(wrap);
    if (!locationId) { renderEmpty(wrap, t("google.selectLocation")); return; }
    var rows = (GOOGLE_PREVIEW_FIXTURE.reviews || {})[locationId] || [];
    if (!rows.length) { renderEmpty(wrap, t("google.noReviews")); return; }
    var table = el("table", { cls: "grid google-reviews" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: t("google.colRating") }),
      el("th", { text: t("google.colComment") })
    ])]));
    var body = el("tbody");
    rows.forEach(function (r) {
      body.appendChild(el("tr", {}, [
        el("td", { text: stars(r.rating) }),
        el("td", { text: r.comment })
      ]));
    });
    table.appendChild(body);
    wrap.appendChild(table);
  }

  function renderGooglePreviewLocations() {
    var wrap = $("google-preview-locations");
    if (!wrap) return;
    clearNode(wrap);
    var table = el("table", { cls: "grid google-locations" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: t("google.colName") }),
      el("th", { text: t("google.colAddress") }),
      el("th", { text: t("google.colZip") }),
      el("th", { text: t("google.colId") }),
      el("th", { text: "" })
    ])]));
    var body = el("tbody");
    googleFixtureLocations().forEach(function (loc) {
      var btn = el("button", {
        cls: "link-btn", text: t("google.view"),
        attrs: { type: "button", "data-google-loc": loc.id }
      });
      btn.addEventListener("click", function () { renderGoogleFixtureReviews(loc.id); });
      body.appendChild(el("tr", {}, [
        el("td", { text: loc.name }),
        el("td", { text: loc.address }),
        el("td", { text: loc.zip }),
        el("td", { text: loc.id }),
        el("td", {}, [btn])
      ]));
    });
    table.appendChild(body);
    wrap.appendChild(table);
  }

  function renderGoogleConnection() {
    clearNotice();
    var box = $("google-readiness");
    var label = $("google-preview-label");
    if (label) label.textContent = t("google.fixtureLabel");
    renderGooglePreviewLocations();
    renderGoogleFixtureReviews(null);
    if (!box) return;
    // Read-only: the same authenticated GET the shell already uses.
    api("GET", "/api/dashboard").then(function (data) {
      var connected = !!(data && data.google_connected === true);
      box.textContent = connected ? t("google.ready") : t("google.notReady");
      box.setAttribute("data-google-state", connected ? "connected" : "not_ready");
    }).catch(function (err) {
      box.textContent = t("google.notReady");
      box.setAttribute("data-google-state", "not_ready");
      reportError(err);
    });
  }

  // ------------------------------------------------------------------ //
  // queue
  // ------------------------------------------------------------------ //
  var selected = {};

  function bulkBlockReason(row) {
    var rating = Number(row.stars != null ? row.stars : row.rating);
    if (rating === 1 || rating === 2) return BULK_BLOCK.LOW_STARS;
    if (row.is_sensitive) return BULK_BLOCK.SENSITIVE;
    return null;
  }

  // The Review Inbox reads the review-centric GET /api/reviews (every item
  // carries a human-readable business_name + the REAL stored draft text).
  // Selecting a row loads its detail with a read-only GET -- no AI call.
  function renderQueue(cfg) {
    clearNotice("bulk-result");
    $("queue-title").textContent = cfg.title || "Queue";

    // defaults for status-scoped views unless the URL supplied one
    if (cfg.status && !state.filters.status) {
      state.filters.status = cfg.status;
      $("f-status").value = cfg.status;
    }

    var q = new URLSearchParams();
    FILTER_KEYS.forEach(function (k) {
      if (state.filters[k]) q.set(k, state.filters[k]);
    });
    q.set("page", String(state.page));
    q.set("page_size", String(PAGE_SIZE));
    q.set("_", String(Date.now())); // defeat any stale caching

    var list = $("queue-list");
    renderLoading(list);
    api("GET", "/api/reviews?" + q.toString()).then(function (res) {
      clearNode(list);
      selected = {};
      updateBulkCount();

      // Server-side pagination: {items, total, page, page_size, pages}.
      var rows = (res && res.items) || [];
      state.page = (res && res.page) || state.page;
      state.pages = (res && res.pages) || 0;
      state.total = (res && res.total) || 0;
      updatePager();

      var count = $("queue-count");
      if (count) count.textContent = t("inbox.count", { n: state.total });

      if (!rows.length) {
        renderEmpty(list, t("inbox.empty"));
        renderDetailPlaceholder();
        return;
      }
      rows.forEach(function (row) { list.appendChild(renderRow(row)); });
      // Refresh the detail pane from the server for the current selection.
      if (state.selectedReview != null) openDetail(state.selectedReview);
      else renderDetailPlaceholder();
    }).catch(function (err) {
      renderErrorState(list, err);
      renderErrorState($("queue-detail"), err);
      reportError(err);
    });
  }

  function updatePager() {
    var info = $("queue-page-info");
    var pages = Math.max(1, state.pages);
    if (info) {
      info.textContent = t("queue.pageInfo", { page: state.page, pages: pages, total: state.total });
    }
    var prev = $("page-prev");
    var next = $("page-next");
    if (prev) prev.disabled = state.page <= 1;
    if (next) next.disabled = state.page >= pages;
  }

  function gotoPage(delta) {
    var next = Math.max(1, state.page + delta);
    if (state.pages && next > state.pages) return;
    state.page = next;
    writeUrl();
    refreshQueue();
  }

  // --- list row (left pane) ----------------------------------------- //
  function renderRow(row) {
    var card = el("div", { cls: "inbox-row", attrs: {
      "data-review-id": row.review_id, "data-draft-id": row.draft_id,
      "data-status": row.status || "", role: "button", tabindex: "0",
      "aria-label": (row.business_name || "Review") + " — " + (row.review_date || "")
    } });

    var blockReason = bulkBlockReason(row);
    var cb = el("input", { attrs: { type: "checkbox",
      "aria-label": "Select draft " + row.draft_id } });
    cb.setAttribute("data-bulk-disabled",
      (blockReason || row.draft_id == null) ? "true" : "false");
    if (!can("approve")) {
      cb.disabled = true;
      cb.title = t("state.notPermitted");
    } else if (row.draft_id == null) {
      cb.disabled = true;
      cb.title = t("inbox.noDraft");
    } else if (blockReason) {
      cb.disabled = true;
      cb.title = BULK_BLOCK_LABEL[blockReason] || blockReason;
      cb.setAttribute("data-bulk-block", blockReason);
    } else {
      cb.addEventListener("change", function () {
        if (cb.checked) selected[row.draft_id] = true;
        else delete selected[row.draft_id];
        updateBulkCount();
      });
    }
    var select = el("span", { cls: "rowselect" }, [cb]);
    select.addEventListener("click", function (e) { e.stopPropagation(); });

    var head = el("div", { cls: "inbox-head" }, [
      select,
      el("span", { cls: "shop", text: row.business_name || t("state.notTracked") }),
      el("span", { cls: "zip", text: zipLabel(row.postal_code) }),
      el("span", { cls: "stars",
        text: stars(row.stars) + " (" + (row.stars == null ? "?" : row.stars) + ")" }),
      el("span", { cls: "status " + (row.status || ""), text: row.status || "unknown" })
    ]);
    card.appendChild(head);
    card.appendChild(el("div", { cls: "excerpt",
      text: row.comment || t("queue.noComment") }));
    card.appendChild(el("div", { cls: "inbox-meta" }, [
      el("span", { text: t("inbox.date") + ": " + (row.review_date || t("state.notTracked")) }),
      el("span", { text: t("inbox.language") + ": " + (row.language || t("state.notTracked")) })
    ]));
    if (blockReason || row.requires_manual_approval) {
      card.appendChild(el("div", { cls: "blocked",
        text: BULK_BLOCK_LABEL[blockReason] || t("inbox.manual") }));
    }

    card.addEventListener("click", function () { selectReview(row.review_id); });
    card.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectReview(row.review_id);
      }
    });
    if (state.selectedReview === row.review_id) card.classList.add("selected");
    return card;
  }

  // --- detail pane (right pane / full-screen on mobile) ------------- //
  function markSelectedRow(reviewId) {
    Array.prototype.slice.call(
      document.querySelectorAll("#queue-list .inbox-row")
    ).forEach(function (n) {
      n.classList.toggle("selected",
        Number(n.getAttribute("data-review-id")) === Number(reviewId));
    });
  }

  function selectReview(reviewId) {
    state.selectedReview = reviewId;
    markSelectedRow(reviewId);
    var split = $("queue-split");
    if (split) split.classList.add("detail-open");   // mobile: full-screen detail
    var back = $("queue-detail-back");
    if (back) back.hidden = false;
    openDetail(reviewId);
  }

  function closeDetail() {
    state.selectedReview = null;
    markSelectedRow(-1);
    var split = $("queue-split");
    if (split) split.classList.remove("detail-open");
  }

  function renderDetailPlaceholder() {
    var wrap = $("queue-detail");
    if (!wrap) return;
    clearNode(wrap);
    wrap.appendChild(el("p", { cls: "muted", text: t("inbox.placeholder") }));
    var back = $("queue-detail-back");
    if (back) back.hidden = true;
  }

  function openDetail(reviewId) {
    var wrap = $("queue-detail");
    var title = $("queue-detail-title");
    if (!wrap) return;
    if (title) title.textContent = t("inbox.detailTitle");
    renderLoading(wrap);
    // READ-ONLY GET: the review + its drafts + audit. No AI call, no reload.
    api("GET", "/api/reviews/" + encodeURIComponent(reviewId)).then(function (d) {
      renderDetail(d || {});
    }).catch(function (err) {
      renderErrorState(wrap, err);
      reportError(err);
    });
  }

  function detailRow(dl, label, value) {
    dl.appendChild(el("div", { cls: "detail-item" }, [
      el("span", { cls: "k", text: label }),
      el("span", { cls: "v",
        text: (value == null || value === "") ? t("state.notTracked") : String(value) })
    ]));
  }

  function renderDetail(d) {
    var wrap = $("queue-detail");
    if (!wrap) return;
    clearNode(wrap);
    var title = $("queue-detail-title");
    if (title) title.textContent = d.business_name || t("inbox.detailTitle");

    var dl = el("div", { cls: "detail-grid" });
    detailRow(dl, t("locations.businessName"), d.business_name || "—");
    detailRow(dl, t("locations.zipCode"), d.postal_code || "—");
    detailRow(dl, "Stars", stars(d.stars) + " (" + (d.stars == null ? "?" : d.stars) + ")");
    detailRow(dl, t("inbox.date"), d.review_date);
    detailRow(dl, t("inbox.language"), d.language);
    detailRow(dl, t("inbox.status"), d.status);
    wrap.appendChild(dl);

    wrap.appendChild(el("div", { cls: "label", text: t("inbox.fullComment") }));
    wrap.appendChild(el("div", { cls: "detail-comment",
      text: d.comment || t("queue.noComment") }));

    // Draft display rule: NO stored draft text -> explicit "Reply not generated".
    var draftText = (d.draft_text != null && String(d.draft_text).trim())
      ? String(d.draft_text) : null;
    wrap.appendChild(el("div", { cls: "label", text: t("inbox.draft") }));
    if (draftText) {
      wrap.appendChild(el("div", { cls: "draft-text", text: draftText }));
    } else {
      wrap.appendChild(el("div", { cls: "state state-empty", text: t("inbox.noDraft") }));
    }

    if (d.validator_reason) {
      wrap.appendChild(el("div", { cls: "reasons",
        text: t("inbox.validator") + ": " + d.validator_reason }));
    }
    if (d.requires_manual_approval) {
      wrap.appendChild(el("div", { cls: "blocked", text: t("inbox.manual") }));
    }

    var draftId = d.draft_id;
    var version = d.draft_version;
    var actions = el("div", { cls: "actions" });
    var editBtn = el("button", { text: t("inbox.editReply") });
    var approveBtn = el("button", { cls: "approve", text: t("inbox.approve") });
    var rejectBtn = el("button", { cls: "reject", text: t("inbox.reject") });
    actions.appendChild(editBtn);
    actions.appendChild(approveBtn);
    actions.appendChild(rejectBtn);
    // Hide/disable actions the current user cannot perform (the backend
    // remains authoritative: a hidden button never means the check is gone).
    if (!can("edit")) editBtn.disabled = true;
    if (!can("approve")) approveBtn.disabled = true;
    if (!can("reject")) rejectBtn.disabled = true;
    if (draftId == null) {
      editBtn.disabled = true;
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
    }
    if (editBtn.disabled && !can("edit")) editBtn.title = t("state.notPermitted");
    if (approveBtn.disabled && !can("approve")) approveBtn.title = t("state.notPermitted");
    if (rejectBtn.disabled && !can("reject")) rejectBtn.title = t("state.notPermitted");
    wrap.appendChild(actions);

    var editor = el("div", { cls: "editor", attrs: { hidden: "hidden" } });
    var ta = el("textarea", { attrs: { "aria-label": t("inbox.draft") } });
    ta.value = draftText || "";
    editor.appendChild(ta);
    var save = el("button", { cls: "primary", text: t("inbox.save") });
    save.addEventListener("click", function () {
      api("POST", "/api/drafts/" + draftId + "/edit", {
        text: ta.value, actor: actor(), expected_version: version
      }).then(function () {
        notify("Draft " + draftId + " saved (not published — DRAFT MODE).");
        refreshQueue();
      }).catch(reportError);
    });
    editor.appendChild(save);
    wrap.appendChild(editor);

    editBtn.addEventListener("click", function () {
      if (editor.getAttribute("hidden") !== null) editor.removeAttribute("hidden");
      else editor.setAttribute("hidden", "hidden");
    });

    approveBtn.addEventListener("click", function () {
      if (draftId == null) return;
      if (!window.confirm(t("inbox.confirmApprove"))) return;
      api("POST", "/api/drafts/" + draftId + "/approve", {
        actor: actor(), expected_version: version
      }).then(function (res) {
        notify("Draft " + draftId + " approved (status: " + res.status +
          "). No reply is published — DRAFT MODE.");
        refreshQueue();   // RE-FETCH rows + counters from the server
      }).catch(reportError);
    });

    rejectBtn.addEventListener("click", function () {
      if (draftId == null) return;
      if (!window.confirm(t("inbox.confirmReject"))) return;
      var note = window.prompt("Reject note (optional):", "") || "";
      api("POST", "/api/drafts/" + draftId + "/reject", {
        actor: actor(), note: note, expected_version: version
      }).then(function () {
        notify("Draft " + draftId + " rejected (not published — DRAFT MODE).");
        refreshQueue();
      }).catch(reportError);
    });

    // Audit history (recent first).
    wrap.appendChild(el("div", { cls: "label", text: t("inbox.audit") }));
    var audit = d.audit || [];
    if (!audit.length) {
      wrap.appendChild(el("p", { cls: "muted", text: t("inbox.noAudit") }));
    } else {
      var table = el("table", { cls: "grid" });
      table.appendChild(el("thead", {}, [el("tr", {}, [
        el("th", { text: "When" }), el("th", { text: "Actor" }),
        el("th", { text: "Action" }), el("th", { text: "Detail" })
      ])]));
      var tbody = el("tbody");
      audit.forEach(function (a) {
        tbody.appendChild(el("tr", {}, [
          el("td", { text: a.ts || "", attrs: { "data-label": "When" } }),
          el("td", { text: a.actor || "", attrs: { "data-label": "Actor" } }),
          el("td", { text: a.action || "", attrs: { "data-label": "Action" } }),
          el("td", { text: a.detail_json || "", attrs: { "data-label": "Detail" } })
        ]));
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
    }
  }

  function currentAreaKey() {
    var cfg = AREAS[state.area];
    return cfg ? state.area : "inbox";
  }

  function refreshQueue() {
    showArea(currentAreaKey());
  }

  function updateBulkCount() {
    var n = Object.keys(selected).length;
    $("bulk-count").textContent = t("queue.selected", { n: n });
    $("bulk-approve-btn").disabled = n === 0 || !can("approve");
  }

  function bulkApprove() {
    var ids = Object.keys(selected).map(Number);
    if (!ids.length) return;
    if (!window.confirm(t("inbox.confirmBulk", { n: ids.length }))) return;
    api("POST", "/api/drafts/bulk-approve", { ids: ids, actor: actor() }).then(function (res) {
      var approved = (res && res.approved) || [];
      var skipped = (res && res.skipped) || [];
      var lines = ["Bulk approve: " + approved.length + " approved (nothing published — DRAFT MODE)."];
      if (skipped.length) {
        lines.push("Skipped " + skipped.length + " item(s):");
        skipped.forEach(function (s) {
          lines.push("  - draft " + s.id + ": " + s.reason);
        });
      }
      notify(lines.join("\n"), skipped.length ? "warn" : "", "bulk-result");
      refreshQueue();
    }).catch(reportError);
  }

  // ------------------------------------------------------------------ //
  // filters
  // ------------------------------------------------------------------ //
  function asRows(res) {
    if (Array.isArray(res)) return res;
    return (res && res.items) || [];
  }

  function populateLocations() {
    api("GET", "/api/locations?page_size=100").then(function (res) {
      var sel = $("f-location");
      asRows(res).forEach(function (loc) {
        var label = loc.business_name || loc.title || loc.location_name;
        var opt = el("option", { text: label, attrs: { value: loc.location_name } });
        sel.appendChild(opt);
      });
      if (state.filters.location_id) sel.value = state.filters.location_id;
    }).catch(function () { /* location list is best-effort */ });
  }

  function applyFiltersFromDom() {
    state.filters = {};
    [["location_id", "f-location"], ["stars", "f-stars"], ["status", "f-status"],
     ["language", "f-language"], ["since", "f-since"], ["q", "f-q"]].forEach(function (pair) {
      var v = $(pair[1]).value;
      if (v) state.filters[pair[0]] = v;
    });
    state.page = 1;
  }

  function resetFilters() {
    state.filters = {};
    ["f-location", "f-stars", "f-status", "f-language", "f-since", "f-q"].forEach(function (id) {
      $(id).value = "";
    });
    state.page = 1;
  }

  function syncFiltersToDom() {
    if (state.filters.location_id) $("f-location").value = state.filters.location_id;
    if (state.filters.stars) $("f-stars").value = state.filters.stars;
    if (state.filters.status) $("f-status").value = state.filters.status;
    if (state.filters.language) $("f-language").value = state.filters.language;
    if (state.filters.since) $("f-since").value = state.filters.since;
    if (state.filters.q) $("f-q").value = state.filters.q;
  }

  function applyLocalizedShell() {
    document.documentElement.lang = DEFAULT_LOCALE;
    var v = $("app-version");
    if (v) v.textContent = t("shell.version");
    var btn = $("nav-toggle");
    if (btn) btn.setAttribute("aria-label", t("shell.toggleNav"));
  }

  // ------------------------------------------------------------------ //
  // boot
  // ------------------------------------------------------------------ //
  function init() {
    applyLocalizedShell();
    bindShell();
    readUrl();

    navItems().forEach(function (item) {
      item.addEventListener("click", function () {
        var area = item.getAttribute("data-area");
        var cfg = AREAS[area];
        if (cfg && cfg.status) state.filters.status = cfg.status;
        writeUrl();
        showArea(area);
      });
    });

    $("f-apply").addEventListener("click", function () {
      applyFiltersFromDom();
      writeUrl();
      showArea(state.area);
    });
    $("f-search").addEventListener("click", function () {
      applyFiltersFromDom();
      writeUrl();
      showArea(state.area);
    });
    $("f-reset").addEventListener("click", function () {
      resetFilters();
      writeUrl();
      showArea(state.area);
    });
    $("page-prev").addEventListener("click", function () { gotoPage(-1); });
    $("page-next").addEventListener("click", function () { gotoPage(1); });
    $("bulk-approve-btn").addEventListener("click", bulkApprove);
    if ($("queue-detail-back")) {
      $("queue-detail-back").addEventListener("click", closeDetail);
    }

    // Locations page controls (server-side search / filter / sort / paging).
    if ($("loc-search")) {
      $("loc-search").addEventListener("click", function () {
        locState.q = ($("loc-q").value || "").trim();
        locState.state = $("loc-state").value || "";
        locState.sync_status = $("loc-sync").value || "";
        locState.page = 1;
        renderLocations();
      });
    }
    if ($("loc-reset")) {
      $("loc-reset").addEventListener("click", function () {
        locState.q = ""; locState.state = ""; locState.sync_status = "";
        locState.sort = "business_name"; locState.order = "asc"; locState.page = 1;
        if ($("loc-q")) $("loc-q").value = "";
        if ($("loc-state")) $("loc-state").value = "";
        if ($("loc-sync")) $("loc-sync").value = "";
        renderLocations();
      });
    }
    if ($("loc-page-size")) {
      $("loc-page-size").addEventListener("change", function () {
        locState.page_size = parseInt($("loc-page-size").value, 10) || 25;
        locState.page = 1;
        renderLocations();
      });
    }
    if ($("loc-prev")) {
      $("loc-prev").addEventListener("click", function () {
        if (locState.page > 1) { locState.page -= 1; renderLocations(); }
      });
    }
    if ($("loc-next")) {
      $("loc-next").addEventListener("click", function () {
        if (locState.page < Math.max(1, locState.pages)) { locState.page += 1; renderLocations(); }
      });
    }
    if ($("lrev-prev")) {
      $("lrev-prev").addEventListener("click", function () {
        if (locReviews.page > 1) { locReviews.page -= 1; renderLocationReviews(); }
      });
    }
    if ($("lrev-next")) {
      $("lrev-next").addEventListener("click", function () {
        if (locReviews.page < Math.max(1, locReviews.pages)) { locReviews.page += 1; renderLocationReviews(); }
      });
    }

    populateLocations();
    populateStateFilter();
    syncFiltersToDom();
    writeUrl();
    showArea(state.area);
    bindAuditControls();
    if ($("login-form")) {
      $("login-form").addEventListener("submit", submitLogin);
    }
    // Resolve the auth state first; in mock mode this is a no-op. A 401 here
    // (auth enabled, no session) flips the app to the login view.
    refreshAuth().then(refreshStatusPill, refreshStatusPill);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
