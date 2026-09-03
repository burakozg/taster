// Tasting Log PWA — single-page, no framework. Served by the relay at the
// same origin as its API, so no separate backend URL is needed — just the
// shared API key. See ARCHITECTURE.md for the relay/worker split.

const LS_KEY = "taster_api_key";

function getApiKey() {
  return localStorage.getItem(LS_KEY);
}

function isConfigured() {
  return Boolean(getApiKey());
}

async function apiFetch(path, options = {}) {
  const headers = { Authorization: `Bearer ${getApiKey()}`, ...(options.headers || {}) };
  const resp = await fetch(path, { ...options, headers });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`${resp.status} ${resp.statusText}: ${body}`);
  }
  return resp.json();
}

// --- setup screen ---

function initSetup() {
  const setupScreen = document.getElementById("setup");
  const appScreen = document.getElementById("app");

  if (isConfigured()) {
    setupScreen.classList.add("hidden");
    appScreen.classList.remove("hidden");
    return;
  }

  document.getElementById("setup-save").addEventListener("click", () => {
    const key = document.getElementById("setup-key").value.trim();
    if (!key) return;
    localStorage.setItem(LS_KEY, key);
    setupScreen.classList.add("hidden");
    appScreen.classList.remove("hidden");
  });
}

// --- tabs ---

function initTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => {
        t.classList.remove("active");
        t.setAttribute("aria-selected", "false");
      });
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      tab.setAttribute("aria-selected", "true");
      document.getElementById(`tab-${tab.dataset.tab}`).classList.add("active");
    });
  });
}

// --- rating slider (1.0-5.0, one decimal) ---
//
// A slider always has a position, so "no rating yet" needs explicit state:
// the value only counts once the slider has been touched, and "clear"
// returns to the untouched state. That matters because a to-try entry must
// NOT carry a rating (the backend rejects it).

function initRatingSlider() {
  const slider = document.getElementById("add-rating");
  const valueEl = document.getElementById("add-rating-value");
  const clearBtn = document.getElementById("add-rating-clear");

  const render = () => {
    const touched = slider.dataset.touched === "1";
    valueEl.textContent = touched ? `${Number(slider.value).toFixed(1)} ★` : "No rating";
    clearBtn.classList.toggle("hidden", !touched);
  };

  slider.addEventListener("input", () => {
    slider.dataset.touched = "1";
    render();
  });
  clearBtn.addEventListener("click", () => {
    slider.dataset.touched = "0";
    slider.value = "3";
    render();
  });
  render();
}

function getRating() {
  const slider = document.getElementById("add-rating");
  return slider.dataset.touched === "1" ? Number(slider.value).toFixed(1) : null;
}

// --- generic async job polling (used by both capture and lookup — the
// relay enqueues a job and the QNAP worker polls/processes/posts the
// result back; this polls the relay for that result) ---

// Poll the relay for a broker job's result until it's done/failed or the
// deadline passes. `timeoutMs` must comfortably exceed the job's real runtime:
// an AI job that web-searches (a manage plan searches a country per record,
// captures/lookups hit the web too) routinely runs past a minute, and if we
// stop polling first the finished result is stranded — the job shows "done" in
// history while the UI sticks on "Still working". So the AI-heavy call sites
// pass minutes, not the default. Elapsed seconds are shown so a slow-but-alive
// job doesn't read as hung.
async function pollJob(path, statusEl, { onDone, workingMessage, timeoutMs = 120000, intervalMs = 2000 }) {
  const started = Date.now();
  statusEl.textContent = workingMessage;
  statusEl.classList.add("working");
  try {
    while (Date.now() - started < timeoutMs) {
      await new Promise((r) => setTimeout(r, intervalMs));
      const result = await apiFetch(path);
      if (result.status === "done") {
        statusEl.classList.remove("working");
        onDone(result);
        return;
      }
      if (result.status === "failed") {
        statusEl.classList.remove("working");
        statusEl.textContent = `Failed: ${result.error ?? "unknown error"}`;
        return;
      }
      // "pending" or "processing" — keep polling, showing it's still alive
      statusEl.textContent = `${workingMessage} (${Math.round((Date.now() - started) / 1000)}s)`;
    }
    statusEl.textContent = "Still working — check the recent jobs in Admin; the result will be there when it finishes.";
  } finally {
    statusEl.classList.remove("working");
  }
}

// A camera + upload picker pair sharing one preview and one file slot. The
// user can take a photo OR pick a saved image (a screenshot of a message, a
// web find); whichever they choose last is what gets submitted. Returns
// { getFile, clear } so callers read a single value regardless of source.
function wirePhotoInputs({ cameraId, uploadId, preview, onChange }) {
  let file = null;
  const set = (f) => {
    file = f || null;
    if (file) {
      preview.src = URL.createObjectURL(file);
      preview.classList.remove("hidden");
    } else {
      preview.classList.add("hidden");
      preview.removeAttribute("src");
    }
    if (onChange) onChange(file);
  };
  for (const id of [cameraId, uploadId]) {
    document.getElementById(id).addEventListener("change", (e) => set(e.target.files[0] || null));
  }
  return {
    getFile: () => file,
    clear: () => {
      document.getElementById(cameraId).value = "";
      document.getElementById(uploadId).value = "";
      set(null);
    },
  };
}

// --- add: merged photo + chat capture ---
//
// One entry point for both capture shapes: if a photo is attached we POST
// /capture/photo (with the optional rating + note); otherwise the free text
// goes to /capture/chat. The rating slider only appears once a photo is
// chosen — chat captures parse their rating from the text itself, and a
// to-try entry must carry no rating at all.

function initAdd() {
  const preview = document.getElementById("add-preview");
  const ratingRow = document.getElementById("add-rating-row");
  const textEl = document.getElementById("add-text");
  const statusEl = document.getElementById("add-status");
  const submitBtn = document.getElementById("add-submit");

  // The rating slider only makes sense with a photo attached (chat captures
  // parse their rating from the text), so it follows the picker's state.
  const photo = wirePhotoInputs({
    cameraId: "add-photo-camera",
    uploadId: "add-photo-upload",
    preview,
    onChange: (file) => ratingRow.classList.toggle("hidden", !file),
  });

  function showSaved(result) {
    // A capture can come back "failed" (graceful degradation) even though the
    // job completed — reflect that honestly instead of a false success card.
    if (result.status === "failed") {
      statusEl.textContent = `Couldn't save: ${result.error ?? "no usable note extracted"}`;
      return;
    }
    const title = [result.note?.producer, result.note?.name].filter(Boolean).join(" — ") || "(unnamed)";
    const bits = [];
    if (result.note?.type) bits.push(result.note.type);
    if (result.note?.country_of_origin) bits.push(result.note.country_of_origin);
    if (result.prior_match) {
      bits.push(`previously tasted ${result.prior_match.date}, rated ${result.prior_match.rating ?? "?"}`);
    }
    statusEl.textContent = "";
    const card = document.createElement("div");
    card.className = "success-card";
    const check = document.createElement("span");
    check.className = "check";
    check.textContent = "✓";
    const body = document.createElement("div");
    const t = document.createElement("div");
    t.className = "sc-title";
    t.textContent = `Saved · ${title}`;
    body.appendChild(t);
    if (bits.length) {
      const sub = document.createElement("div");
      sub.className = "sc-sub";
      sub.textContent = bits.join(" · ");
      body.appendChild(sub);
    }
    card.append(check, body);
    statusEl.appendChild(card);
  }

  submitBtn.addEventListener("click", async () => {
    const file = photo.getFile();
    const text = textEl.value.trim();
    if (!file && !text) {
      statusEl.textContent = "Add a photo or describe the tasting.";
      return;
    }
    submitBtn.disabled = true;
    statusEl.textContent = file ? "Uploading..." : "Sending...";
    try {
      let path;
      const form = new FormData();
      if (file) {
        form.append("image", file);
        const stars = getRating();
        if (stars !== null) form.append("stars", stars);
        if (text) form.append("note", text);
        const { capture_id } = await apiFetch("/capture/photo", { method: "POST", body: form });
        path = `/capture/${capture_id}`;
      } else {
        form.append("text", text);
        const { capture_id } = await apiFetch("/capture/chat", { method: "POST", body: form });
        path = `/capture/${capture_id}`;
      }
      await pollJob(path, statusEl, { workingMessage: "Enriching...", onDone: showSaved, timeoutMs: 180000 });
      textEl.value = "";
      photo.clear();
    } catch (e) {
      statusEl.textContent = `Error: ${e.message}`;
    } finally {
      submitBtn.disabled = false;
    }
  });
}

// --- search: merged lookup + items ---
//
// The text box does double duty: it live-filters the grouped item list below
// (client-side, over the snapshot the worker pushes), and it's the question
// the "Ask AI" button sends to /lookup (with an optional shop-mode photo).
// Filtering the list and asking the AI are independent — a filter narrows
// what's shown; asking adds a conversational answer above it.

let allItems = [];
// Search "In stock" chip state: when true, renderGroups keeps only items with
// stock > 0 (AND-combined with the text filter).
let inStockOnly = false;
// Search "Wishlist" chip: when true, keep only status: to-try items (the
// want-to-try list the data model already captures but had no view for).
let wishlistOnly = false;
// Set by initSearch so the item-detail modal can refresh the list after an
// edit or delete.
let reloadItems = () => {};

// Group order + display names per to-do.txt. Any type not listed still shows,
// appended after these, so nothing in the vault is silently hidden.
// GROUPS / EDIT_FIELDS / FIELD_KIND below are the cold-start FALLBACK — the
// real values come from GET /categories (the worker's registry), applied by
// applyCategories() at startup. This is why they're `let`, not `const`: a new
// category added on the backend shows up here with no PWA change. renderGroups
// still appends any unknown type after these, so nothing is ever hidden.
let GROUPS = [
  ["cigar", "Cigars"],
  ["whisky", "Whiskies"],
  ["coffee", "Coffee Beans"],
  ["beer", "Beers"],
  ["pipe", "Pipe Tobacco"],
  ["chocolate", "Chocolate"],
  ["pairing", "Pairings"],
];

// Title-case a bare type slug ("pipe" -> "Pipe", "cold-brew" -> "Cold Brew") for
// the rare case a group has no registry label yet.
function titleCase(s) {
  return String(s).replace(/(^|[\s-])([a-z])/g, (_, sep, c) => sep + c.toUpperCase());
}

// A small glyph per category — reinforces the colored border stripe so type is
// never signalled by color alone (color-blind case). Unknown types get none.
const GROUP_ICONS = {
  cigar: "🚬", whisky: "🥃", coffee: "☕", beer: "🍺", pipe: "🌿",
  chocolate: "🍫", raki: "🍶", pairing: "🤝",
};

// country (any casing / common aliases) -> flag emoji, for the origin-driven
// collection. No external assets — emoji are the platform's own. Subdivision
// flags (Scotland/England/Wales) render on Apple platforms; elsewhere they
// simply fall back to nothing, which is fine.
const COUNTRY_FLAGS = {
  scotland: "🏴󠁧󠁢󠁳󠁣󠁴󠁿", england: "🏴󠁧󠁢󠁥󠁮󠁧󠁿", wales: "🏴󠁧󠁢󠁷󠁬󠁳󠁿",
  "united kingdom": "🇬🇧", uk: "🇬🇧", ireland: "🇮🇪", france: "🇫🇷", germany: "🇩🇪",
  belgium: "🇧🇪", netherlands: "🇳🇱", "czech republic": "🇨🇿", czechia: "🇨🇿",
  denmark: "🇩🇰", sweden: "🇸🇪", norway: "🇳🇴", italy: "🇮🇹", spain: "🇪🇸",
  poland: "🇵🇱", austria: "🇦🇹",
  "united states": "🇺🇸", usa: "🇺🇸", us: "🇺🇸", america: "🇺🇸", canada: "🇨🇦",
  japan: "🇯🇵", india: "🇮🇳", taiwan: "🇹🇼", australia: "🇦🇺",
  cuba: "🇨🇺", nicaragua: "🇳🇮", "dominican republic": "🇩🇴", honduras: "🇭🇳",
  mexico: "🇲🇽", ecuador: "🇪🇨", brazil: "🇧🇷", "costa rica": "🇨🇷", peru: "🇵🇪",
  colombia: "🇨🇴", panama: "🇵🇦", bolivia: "🇧🇴", "el salvador": "🇸🇻",
  ethiopia: "🇪🇹", kenya: "🇰🇪", guatemala: "🇬🇹", rwanda: "🇷🇼", tanzania: "🇹🇿",
  burundi: "🇧🇮", uganda: "🇺🇬", yemen: "🇾🇪", indonesia: "🇮🇩", vietnam: "🇻🇳",
};

function flagFor(country) {
  if (!country) return "";
  const key = String(country).trim().toLowerCase();
  return COUNTRY_FLAGS[key] || "";
}

function itemMatches(item, needle) {
  if (!needle) return true;
  const hay = [
    item.producer, item.name, item.type, item.region, item.status,
    // country_of_origin is the only place a word like "Cuba" lives for a
    // cigar (cigars have no region), so it must be searchable; origin/category
    // cover coffee's equivalents.
    item.country_of_origin, item.origin, item.category,
    (item.tags || []).join(" "),
    Array.isArray(item.notes) ? item.notes.join(" ") : item.notes,
    Array.isArray(item.common_notes) ? item.common_notes.join(" ") : item.common_notes,
  ].filter(Boolean).join(" ").toLowerCase();
  return hay.includes(needle);
}

function renderSkeleton() {
  const container = document.getElementById("items-groups");
  // Three placeholder cards with a title line + a couple of rows each.
  container.innerHTML = Array.from({ length: 3 }, () =>
    '<li class="skeleton-card">' +
    '<div class="sk-line" style="width:45%"></div>' +
    '<div class="sk-line" style="width:80%"></div>' +
    '<div class="sk-line" style="width:70%"></div>' +
    "</li>"
  ).join("");
}

function renderGroups(filterText) {
  const container = document.getElementById("items-groups");
  const needle = filterText.trim().toLowerCase();
  const matching = allItems.filter(
    (i) => itemMatches(i, needle)
      && (!inStockOnly || (i.stock ?? 0) > 0)
      && (!wishlistOnly || i.status === "to-try")
  );

  container.innerHTML = "";
  if (matching.length === 0) {
    container.innerHTML = `<li class="empty">${allItems.length ? "No items match." : "No items yet."}</li>`;
    return;
  }

  // Known groups first (in to-do order), then any leftover types.
  const known = GROUPS.map(([t]) => t);
  const types = [...known, ...[...new Set(matching.map((i) => i.type))].filter((t) => !known.includes(t))];

  for (const type of types) {
    const group = matching.filter((i) => i.type === type);
    if (group.length === 0) continue;
    // Decreasing star score; unrated (to-try) entries sink to the bottom.
    group.sort((a, b) => (b.rating ?? -1) - (a.rating ?? -1));

    // Prefer the registry label; if a type isn't known (older client, brand-new
    // category), title-case the type rather than showing it raw-lowercase.
    const known = GROUPS.find(([t]) => t === type);
    const label = known ? known[1] : titleCase(type);
    const details = document.createElement("details");
    details.className = "group";
    details.dataset.type = type;
    details.open = false;  // groups start collapsed; tap a header to expand
    const summary = document.createElement("summary");
    if (GROUP_ICONS[type]) {
      const icon = document.createElement("span");
      icon.className = "group-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = GROUP_ICONS[type];
      summary.appendChild(icon);
    }
    summary.append(document.createTextNode(label));
    const count = document.createElement("span");
    count.className = "group-count";
    count.textContent = String(group.length);
    summary.appendChild(count);
    details.appendChild(summary);

    const ul = document.createElement("ul");
    for (const item of group) {
      const li = document.createElement("li");
      li.className = "item-row";

      const rating = document.createElement("span");
      if (item.rating) {
        rating.className = "rating";
        rating.textContent = `★ ${Number(item.rating).toFixed(1)}`;
      } else {
        rating.className = "rating unrated";
        rating.textContent = "☆";
      }

      const name = document.createElement("span");
      name.className = "item-name";
      const title = document.createElement("span");
      title.className = "item-title";
      // Coffee is rated per brew method, so the method rides in the title to
      // distinguish the same bean's espresso vs V60 entries.
      let titleText = [item.producer, item.name].filter(Boolean).join(" — ") || item._id;
      if (item.brew_method) titleText += ` — ${item.brew_method}`;
      title.textContent = titleText;
      name.appendChild(title);
      const metaBits = [item.region || item.country_of_origin, item.date].filter(Boolean);
      if (metaBits.length) {
        const sub = document.createElement("span");
        sub.className = "item-sub";
        const flag = flagFor(item.country_of_origin);
        if (flag) {
          const f = document.createElement("span");
          f.className = "flag";
          f.setAttribute("aria-hidden", "true");
          f.textContent = flag;
          sub.appendChild(f);
        }
        sub.appendChild(document.createTextNode(metaBits.join(" · ")));
        name.appendChild(sub);
      }

      li.append(rating, name);
      if ((item.stock ?? 0) > 0) {
        const stock = document.createElement("span");
        stock.className = "stock-badge";
        stock.textContent = `×${item.stock}`;
        stock.title = `${item.stock} at home`;
        li.appendChild(stock);
      }
      li.tabIndex = 0;
      li.addEventListener("click", () => openItemDetail(item));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") openItemDetail(item); });
      ul.appendChild(li);
    }
    details.appendChild(ul);
    container.appendChild(details);
  }
}

function initSearch() {
  const textEl = document.getElementById("search-text");
  const preview = document.getElementById("search-preview");
  const answerEl = document.getElementById("search-answer");
  const askBtn = document.getElementById("search-ask");
  const photo = wirePhotoInputs({
    cameraId: "search-photo-camera",
    uploadId: "search-photo-upload",
    preview,
  });

  async function loadItems() {
    // Only show skeletons on the first load — subsequent tab re-opens already
    // have items rendered, and flashing skeletons over them would be jarring.
    if (allItems.length === 0) renderSkeleton();
    try {
      const { items } = await apiFetch("/items");
      allItems = items;
      renderGroups(textEl.value);
    } catch (e) {
      document.getElementById("items-groups").innerHTML = `<li class="empty">Error: ${e.message}</li>`;
    }
  }

  // Live filter as you type — no network, just re-render the snapshot.
  textEl.addEventListener("input", () => renderGroups(textEl.value));

  // "In stock" chip: toggle the stock>0 constraint and re-render (no network).
  const stockToggle = document.getElementById("filter-instock");
  stockToggle.addEventListener("click", () => {
    inStockOnly = !inStockOnly;
    stockToggle.classList.toggle("active", inStockOnly);
    stockToggle.setAttribute("aria-pressed", String(inStockOnly));
    renderGroups(textEl.value);
  });

  // "Wishlist" chip: toggle the status: to-try constraint and re-render.
  const wishlistToggle = document.getElementById("filter-wishlist");
  wishlistToggle.addEventListener("click", () => {
    wishlistOnly = !wishlistOnly;
    wishlistToggle.classList.toggle("active", wishlistOnly);
    wishlistToggle.setAttribute("aria-pressed", String(wishlistOnly));
    renderGroups(textEl.value);
  });

  askBtn.addEventListener("click", async () => {
    const question = textEl.value.trim();
    const file = photo.getFile();
    if (!question && !file) {
      answerEl.textContent = "Type a question or attach a photo.";
      return;
    }
    askBtn.disabled = true;
    answerEl.textContent = "Thinking...";
    try {
      const form = new FormData();
      form.append("question", question || "What is this and have I tasted it before?");
      if (file) form.append("image", file);
      const { lookup_id } = await apiFetch("/lookup", { method: "POST", body: form });
      await pollJob(`/lookup/${lookup_id}`, answerEl, {
        workingMessage: "Thinking...",
        onDone: (result) => { answerEl.textContent = result.answer; },
        timeoutMs: 180000,
      });
    } catch (e) {
      answerEl.textContent = `Error: ${e.message}`;
    } finally {
      askBtn.disabled = false;
    }
  });

  // Refresh the list whenever the Search tab is opened (cheap: one GET).
  document.querySelector('[data-tab="search"]').addEventListener("click", loadItems);
  reloadItems = loadItems;  // let the detail modal refresh after edit/delete
  loadItems();
}

// --- admin panel ---

function initAdmin() {
  const statusEl = document.getElementById("admin-status");
  const imageSel = document.getElementById("admin-image-model");
  const textSel = document.getElementById("admin-text-model");
  const saveBtn = document.getElementById("admin-save");
  const saveStatus = document.getElementById("admin-save-status");
  const jobsEl = document.getElementById("admin-jobs");

  function fillSelect(sel, models, current) {
    sel.innerHTML = "";
    const def = document.createElement("option");
    def.value = "";
    def.textContent = "(worker default — config.yaml)";
    sel.appendChild(def);
    for (const m of models) {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = `${m.label} ${m.cost}`;
      sel.appendChild(opt);
    }
    sel.value = current || "";
  }

  async function loadModels() {
    const [{ models }, settings] = await Promise.all([
      apiFetch("/admin/models"),
      apiFetch("/admin/settings"),
    ]);
    fillSelect(imageSel, models, settings.image_model);
    fillSelect(textSel, models, settings.text_model);
  }

  async function loadStatus() {
    const s = await apiFetch("/admin/status");
    const jobCounts = s.job_counts || {};
    const totalJobs = Object.values(jobCounts).reduce((a, b) => a + b, 0);
    const snapAge = s.snapshot_updated_at ? timeAgo(s.snapshot_updated_at) : "never";

    // Stat tiles: text-ink values (dataviz "not a chart" case), no plot.
    statusEl.innerHTML = "";
    const strip = document.createElement("div");
    strip.className = "stats";
    strip.title = s.snapshot_updated_at
      ? `Last snapshot: ${new Date(s.snapshot_updated_at).toLocaleString()}`
      : "Worker hasn't pushed a snapshot yet";
    for (const [value, label] of [[s.items_count, "Items"], [snapAge, "Snapshot"], [totalJobs, "Jobs"]]) {
      const stat = document.createElement("div");
      stat.className = "stat";
      stat.innerHTML = `<span class="stat-value"></span><span class="stat-label"></span>`;
      stat.querySelector(".stat-value").textContent = String(value);
      stat.querySelector(".stat-label").textContent = label;
      strip.appendChild(stat);
    }
    statusEl.appendChild(strip);
    // The per-status breakdown (done/failed/…) now lives just above the recent
    // jobs list, scoped to those last 10 — see loadJobs().
  }

  function timeAgo(iso) {
    // Compact single-token forms (now / 3m / 2h / 1d) so the stat-tile value
    // never wraps to a second line and stays aligned with the sibling tiles.
    // The exact timestamp is in the strip's title tooltip.
    const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (secs < 60) return "now";
    const mins = secs / 60;
    if (mins < 60) return `${Math.floor(mins)}m`;
    const hrs = mins / 60;
    if (hrs < 24) return `${Math.floor(hrs)}h`;
    return `${Math.floor(hrs / 24)}d`;
  }

  // --- token usage (WRK-10 ledger, served by GET /admin/usage) ---
  //
  // Deliberately not a chart. The headline is "what did today cost", which is
  // three numbers — a KPI row of stat tiles, matching the Status card above.
  // The history behind it is ~14 rows of one measure, so it's a table with an
  // in-row magnitude bar rather than a plot: single series, one hue, no legend,
  // and every number still printed as text so the bar is reinforcement and
  // never the only channel.

  function compactNum(n) {
    if (n < 1000) return String(n);
    if (n < 1e6) return `${(n / 1000).toFixed(n < 1e4 ? 1 : 0)}K`;
    return `${(n / 1e6).toFixed(1)}M`;
  }

  async function loadUsage() {
    const { days, totals } = await apiFetch("/admin/usage?days=14");
    const latestEl = document.getElementById("usage-latest");
    const daysEl = document.getElementById("usage-days");
    const totalEl = document.getElementById("usage-total");

    latestEl.innerHTML = "";
    daysEl.innerHTML = "";
    if (!days.length) {
      totalEl.textContent = "No model calls recorded yet.";
      return;
    }

    // The most recent day the worker reported. Called "Today" only when it
    // matches the browser's date — the worker's timezone is its own business,
    // so the rest of the time the date is shown plainly instead of implied.
    const latest = days[0];
    const isToday = latest.day === new Date().toLocaleDateString("sv");  // sv = ISO-shaped
    const strip = document.createElement("div");
    strip.className = "stats";
    strip.title = `${isToday ? "Today" : latest.day} — ${latest.input_tokens.toLocaleString()} in, `
      + `${latest.output_tokens.toLocaleString()} out, across ${latest.calls} call(s)`;
    const tiles = [
      [compactNum(latest.input_tokens), "Input"],
      [compactNum(latest.output_tokens), "Output"],
      [String(latest.calls), "Calls"],
    ];
    for (const [value, label] of tiles) {
      const stat = document.createElement("div");
      stat.className = "stat";
      stat.innerHTML = `<span class="stat-value"></span><span class="stat-label"></span>`;
      stat.querySelector(".stat-value").textContent = value;
      stat.querySelector(".stat-label").textContent = label;
      strip.appendChild(stat);
    }
    const caption = document.createElement("div");
    caption.className = "usage-caption";
    caption.textContent = isToday ? "Today" : `Latest day — ${latest.day}`;
    latestEl.appendChild(caption);
    latestEl.appendChild(strip);

    // Per-model split for that day, so a single expensive model is visible
    // rather than averaged into the day's total.
    if (latest.by_model.length > 1) {
      const split = document.createElement("div");
      split.className = "usage-split";
      split.textContent = latest.by_model
        .map((m) => `${m.model} ${compactNum(m.input_tokens + m.output_tokens)}`)
        .join(" · ");
      latestEl.appendChild(split);
    }

    // History. Bars are scaled to the busiest day shown, so the column reads as
    // relative load; the exact numbers sit beside them.
    const peak = Math.max(...days.map((d) => d.input_tokens + d.output_tokens), 1);
    for (const d of days) {
      const total = d.input_tokens + d.output_tokens;
      const li = document.createElement("li");
      li.title = `${d.day}: ${d.input_tokens.toLocaleString()} in, `
        + `${d.output_tokens.toLocaleString()} out, ${d.calls} call(s)`;
      li.innerHTML = `
        <span class="usage-day"></span>
        <span class="usage-bar"><span class="usage-bar-fill"></span></span>
        <span class="usage-num"></span>`;
      li.querySelector(".usage-day").textContent = d.day.slice(5);  // MM-DD
      li.querySelector(".usage-bar-fill").style.width = `${Math.max(2, (total / peak) * 100)}%`;
      li.querySelector(".usage-num").textContent = compactNum(total);
      daysEl.appendChild(li);
    }

    totalEl.textContent =
      `All time: ${totals.input_tokens.toLocaleString()} in / `
      + `${totals.output_tokens.toLocaleString()} out over ${totals.calls} call(s), `
      + `${totals.days} day(s) since ${totals.since}.`;
  }

  async function loadJobs() {
    const { jobs } = await apiFetch("/admin/jobs?limit=10");

    // Per-status breakdown for exactly these last 10 jobs (done/failed/…),
    // shown right above the list so the numbers match what's below. Ordered
    // done → failed → the rest so the two the user cares about lead.
    const statsEl = document.getElementById("admin-jobs-stats");
    statsEl.innerHTML = "";
    const counts = {};
    for (const j of jobs) counts[j.status] = (counts[j.status] || 0) + 1;
    const order = ["done", "failed", "processing", "pending"];
    const statuses = [
      ...order.filter((s) => s in counts),
      ...Object.keys(counts).filter((s) => !order.includes(s)),
    ];
    for (const status of statuses) {
      const pill = document.createElement("span");
      pill.className = `job-pill ${status}`;
      pill.textContent = `${counts[status]} ${status}`;
      statsEl.appendChild(pill);
    }

    jobsEl.innerHTML = jobs.length ? "" : "<li>No jobs yet.</li>";
    for (const j of jobs) {
      const li = document.createElement("li");
      const when = new Date(j.created_at).toLocaleString();
      li.textContent = `${when} — ${j.type} — ${j.status}`;
      if (j.status === "failed" && j.error) {
        const err = document.createElement("div");
        err.className = "admin-error";
        err.textContent = j.error.length > 200 ? `${j.error.slice(0, 200)}…` : j.error;
        li.appendChild(err);
      }
      jobsEl.appendChild(li);
    }
  }

  async function refreshAll() {
    try {
      await Promise.all([loadModels(), loadStatus(), loadUsage(), loadJobs()]);
    } catch (e) {
      statusEl.textContent = `Error loading admin data: ${e.message}`;
    }
  }

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving...";
    try {
      await apiFetch("/admin/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          image_model: imageSel.value || null,
          text_model: textSel.value || null,
        }),
      });
      saveStatus.textContent = "Saved — applies from the next job.";
    } catch (e) {
      saveStatus.textContent = `Error: ${e.message}`;
    } finally {
      saveBtn.disabled = false;
    }
  });

  // Refresh whenever the tab is opened (cheap: five small GETs).
  document.querySelector('[data-tab="admin"]').addEventListener("click", refreshAll);
}

// --- maintain (AI bulk edit of existing records) ---
//
// Two-step by design: "Propose" runs the plan job (writes nothing) and shows
// a checklist of proposed changes; "Apply selected" runs the apply job on
// only the ticked ones. See relay/app/routes/manage.py.

let managePlan = [];

function initManage() {
  const instructionEl = document.getElementById("manage-instruction");
  const proposeBtn = document.getElementById("manage-propose");
  const repairBtn = document.getElementById("manage-repair-pairings");
  const statusEl = document.getElementById("manage-status");
  const planBox = document.getElementById("manage-plan");
  const summaryEl = document.getElementById("manage-summary");
  const changesEl = document.getElementById("manage-changes");
  const applyBtn = document.getElementById("manage-apply");
  const applyStatus = document.getElementById("manage-apply-status");
  const selectAllBtn = document.getElementById("manage-select-all");
  const discardBtn = document.getElementById("manage-discard");

  function renderPlan(plan) {
    managePlan = plan.changes || [];
    summaryEl.textContent = plan.summary || "";
    changesEl.innerHTML = "";
    if (managePlan.length === 0) {
      changesEl.innerHTML = "<li>No changes proposed.</li>";
      applyBtn.disabled = true;
    } else {
      applyBtn.disabled = false;
      managePlan.forEach((change, idx) => {
        const li = document.createElement("li");
        const label = document.createElement("label");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = true;
        cb.dataset.idx = String(idx);
        const title = document.createElement("b");
        title.textContent = ` ${change.name || change.doc_id}`;
        label.append(cb, title);
        if (change.type) label.append(document.createTextNode(` (${change.type})`));
        li.appendChild(label);

        const edits = (change.edits || [])
          .map((e) => `${e.field}: ${Array.isArray(e.value) ? e.value.join(", ") : e.value}`)
          .concat(change.generate_uid ? ["+ mint uid"] : []);
        if (edits.length) {
          const ed = document.createElement("div");
          ed.className = "manage-edits";
          ed.textContent = edits.join("  ·  ");
          li.appendChild(ed);
        } else if (!change.pairings && !change.cocktails) {
          // A change with nothing to apply. It used to render as a bare title +
          // reason, indistinguishable from a real edit, and applying it did
          // nothing while reporting success. Now it is labelled here and
          // rejected on apply — and it starts unticked so "apply selected"
          // skips it by default.
          cb.checked = false;
          const ed = document.createElement("div");
          ed.className = "manage-edits manage-empty";
          ed.textContent = "no value proposed — nothing to apply";
          li.appendChild(ed);
        }
        // Regenerate-pairings changes carry a structured `pairings` list instead
        // of edits — show each proposed profile + its owned matches.
        if (Array.isArray(change.pairings) && change.pairings.length) {
          const pd = document.createElement("div");
          pd.className = "manage-edits";
          pd.textContent = "pairings: " + change.pairings.map((p) => {
            const m = (p.matches || []).map((x) => x.name || x.item).filter(Boolean);
            return p.profile + (m.length ? ` [${m.join(", ")}]` : "");
          }).join("  ·  ");
          li.appendChild(pd);
        }
        // Cigar/pipe changes also carry classical cocktails (name only in review).
        if (Array.isArray(change.cocktails) && change.cocktails.length) {
          const cd = document.createElement("div");
          cd.className = "manage-edits";
          cd.textContent = "cocktails: " + change.cocktails.map((c) => c.name).filter(Boolean).join(", ");
          li.appendChild(cd);
        }
        if (change.reason) {
          const rs = document.createElement("div");
          rs.className = "manage-reason";
          rs.textContent = change.reason;
          li.appendChild(rs);
        }
        changesEl.appendChild(li);
      });
    }
    planBox.classList.remove("hidden");
  }

  proposeBtn.addEventListener("click", async () => {
    const instruction = instructionEl.value.trim();
    if (!instruction) {
      statusEl.textContent = "Describe a change first.";
      return;
    }
    proposeBtn.disabled = true;
    planBox.classList.add("hidden");
    applyStatus.textContent = "";
    statusEl.textContent = "Planning...";
    try {
      const { manage_id } = await apiFetch("/manage", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction }),
      });
      await pollJob(`/manage/${manage_id}`, statusEl, {
        workingMessage: "Planning...",
        onDone: (result) => {
          statusEl.textContent = `Proposed ${(result.changes || []).length} change(s) — review below.`;
          renderPlan(result);
        },
        timeoutMs: 300000,
      });
    } catch (e) {
      statusEl.textContent = `Error: ${e.message}`;
    } finally {
      proposeBtn.disabled = false;
    }
  });

  repairBtn.addEventListener("click", async () => {
    repairBtn.disabled = true;
    planBox.classList.add("hidden");
    applyStatus.textContent = "";
    statusEl.textContent = "Regenerating pairings...";
    try {
      const { manage_id } = await apiFetch("/manage/repair-pairings", { method: "POST" });
      await pollJob(`/manage/${manage_id}`, statusEl, {
        workingMessage: "Regenerating pairings...",
        // Runs in batches across the whole vault, so allow generous headroom as
        // the collection grows (the job keeps running server-side regardless).
        timeoutMs: 600000,
        onDone: (result) => {
          statusEl.textContent = `Proposed pairings for ${(result.changes || []).length} item(s) — review below.`;
          renderPlan(result);
        },
      });
    } catch (e) {
      statusEl.textContent = `Error: ${e.message}`;
    } finally {
      repairBtn.disabled = false;
    }
  });

  applyBtn.addEventListener("click", async () => {
    const selected = [...changesEl.querySelectorAll("input[type=checkbox]:checked")]
      .map((cb) => managePlan[Number(cb.dataset.idx)]);
    if (selected.length === 0) {
      applyStatus.textContent = "Select at least one change.";
      return;
    }
    applyBtn.disabled = true;
    applyStatus.textContent = `Applying ${selected.length} change(s)...`;
    try {
      const { manage_id } = await apiFetch("/manage/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ changes: selected }),
      });
      await pollJob(`/manage/${manage_id}`, applyStatus, {
        workingMessage: "Applying...",
        timeoutMs: 300000,
        onDone: (result) => {
          const failedLines = (result.results || [])
            .filter((r) => r.status === "failed")
            .map((r) => `  ✗ ${r.name}: ${r.error}`);
          applyStatus.textContent =
            `Applied ${result.applied}, failed ${result.failed}.` +
            (failedLines.length ? `\n${failedLines.join("\n")}` : "");
          planBox.classList.add("hidden");
          managePlan = [];
          // Pull the fresh snapshot so Search reflects the applied edits
          // immediately, without needing a tab switch to re-fetch.
          reloadItems();
        },
      });
    } catch (e) {
      applyStatus.textContent = `Error: ${e.message}`;
    } finally {
      applyBtn.disabled = false;
    }
  });

  selectAllBtn.addEventListener("click", () => {
    const boxes = [...changesEl.querySelectorAll("input[type=checkbox]")];
    const allChecked = boxes.every((b) => b.checked);
    boxes.forEach((b) => { b.checked = !allChecked; });
  });

  discardBtn.addEventListener("click", () => {
    planBox.classList.add("hidden");
    managePlan = [];
    applyStatus.textContent = "";
    statusEl.textContent = "";
  });
}

// --- sync (records <-> Obsidian vault) ---

// One labelled list in the sync diff (records-without-file / files-without-
// record). `rows` are {main, sub?}; `hint` is the fix for that mismatch kind.
function diffSection(title, hint, rows) {
  const sec = document.createElement("div");
  sec.className = "diff-section";
  const h = document.createElement("div");
  h.className = "diff-title";
  h.textContent = `${title} (${rows.length})`;
  sec.appendChild(h);
  const hintEl = document.createElement("div");
  hintEl.className = "diff-hint";
  hintEl.textContent = hint;
  sec.appendChild(hintEl);
  const ul = document.createElement("ul");
  ul.className = "diff-items";
  for (const r of rows) {
    const li = document.createElement("li");
    const main = document.createElement("span");
    main.className = "diff-main";
    main.textContent = r.main;
    li.appendChild(main);
    if (r.sub) {
      const sub = document.createElement("span");
      sub.className = "diff-sub";
      sub.textContent = r.sub;
      li.appendChild(sub);
    }
    ul.appendChild(li);
  }
  sec.appendChild(ul);
  return sec;
}
//
// Gap indicator + a safe one-way rebuild (DB -> Obsidian). Both go through
// the worker as jobs (see relay/app/routes/sync.py) since only the worker can
// reach CouchDB.

function initSync() {
  const gapEl = document.getElementById("sync-gap");
  const summaryEl = document.getElementById("sync-summary");
  const msgEl = document.getElementById("sync-msg");
  const checkBtn = document.getElementById("sync-check");
  const rebuildBtn = document.getElementById("sync-rebuild-vault");
  const rebuildRecordsBtn = document.getElementById("sync-rebuild-records");
  const normalizeBtn = document.getElementById("sync-normalize");

  function renderGap(state) {
    // The one-line result pill sits inline next to the Check button (they're
    // the relevant pair). The count tiles + delta detail render below.
    summaryEl.innerHTML = "";
    const pill = document.createElement("span");
    const missing = (state.records || 0) - (state.vault_files || 0);
    if (missing > 0) {
      pill.className = "job-pill failed";
      pill.textContent = `${missing} record(s) missing in Obsidian`;
    } else if (missing < 0) {
      pill.className = "job-pill";
      pill.textContent = `${-missing} vault file(s) with no record`;
    } else {
      pill.className = "job-pill done";
      pill.textContent = "in sync";
    }
    summaryEl.appendChild(pill);

    gapEl.innerHTML = "";
    const strip = document.createElement("div");
    strip.className = "stats";
    for (const [value, label] of [[state.records, "Records"], [state.vault_files, "Vault files"]]) {
      const stat = document.createElement("div");
      stat.className = "stat";
      stat.innerHTML = `<span class="stat-value"></span><span class="stat-label"></span>`;
      stat.querySelector(".stat-value").textContent = String(value ?? "—");
      stat.querySelector(".stat-label").textContent = label;
      strip.appendChild(stat);
    }
    gapEl.appendChild(strip);

    // The actual delta behind the counts, so a nonzero gap is explainable.
    const rwf = state.records_without_file || [];
    const fwr = state.files_without_record || [];
    const col = state.colliding_records || [];
    if (rwf.length || fwr.length || col.length) {
      const diff = document.createElement("div");
      diff.className = "sync-diff";
      if (rwf.length) {
        diff.appendChild(diffSection(
          "Records with no vault file", "rebuild vault to restore",
          rwf.map((r) => ({
            main: [r.producer, r.name].filter(Boolean).join(" — ") || r._id,
            sub: r.type,
          })),
        ));
      }
      if (fwr.length) {
        diff.appendChild(diffSection(
          "Vault files with no record", "rebuild records to import, or delete in Obsidian",
          fwr.map((f) => ({ main: f.path })),
        ));
      }
      // Records that share one vault path: this is how `records` can exceed
      // `vault_files` while neither one-sided list explains it.
      if (col.length) {
        diff.appendChild(diffSection(
          "Records sharing one vault file", "rename or re-date one so each gets its own file",
          col.map((c) => ({
            main: c.path,
            sub: (c.records || []).map((r) => r.name || r._id).join(" + "),
          })),
        ));
      }
      gapEl.appendChild(diff);
    }
  }

  async function check() {
    checkBtn.disabled = true;
    try {
      const { sync_id } = await apiFetch("/sync/status", { method: "POST" });
      await pollJob(`/sync/${sync_id}`, msgEl, {
        workingMessage: "Checking...",
        onDone: (result) => { msgEl.textContent = ""; renderGap(result); },
      });
    } catch (e) {
      msgEl.textContent = `Error: ${e.message}`;
    } finally {
      checkBtn.disabled = false;
    }
  }

  rebuildBtn.addEventListener("click", async () => {
    if (!confirm("Rebuild the Obsidian vault files from the database records? This re-creates files (including any deleted in Obsidian). It never changes the records.")) {
      return;
    }
    rebuildBtn.disabled = true;
    try {
      const { sync_id } = await apiFetch("/sync/rebuild-vault", { method: "POST" });
      await pollJob(`/sync/${sync_id}`, msgEl, {
        workingMessage: "Rebuilding vault...",
        onDone: (result) => {
          msgEl.textContent = `Rebuilt ${result.rebuilt}, failed ${result.failed}.`;
          renderGap(result);
        },
      });
    } catch (e) {
      msgEl.textContent = `Error: ${e.message}`;
    } finally {
      rebuildBtn.disabled = false;
    }
  });

  rebuildRecordsBtn.addEventListener("click", async () => {
    if (!confirm("Pull edits from the Obsidian vault back into the records? This adds/updates records from files that exist and never deletes a record.")) {
      return;
    }
    rebuildRecordsBtn.disabled = true;
    try {
      const { sync_id } = await apiFetch("/sync/rebuild-records", { method: "POST" });
      await pollJob(`/sync/${sync_id}`, msgEl, {
        workingMessage: "Importing from vault...",
        onDone: (result) => {
          msgEl.textContent = `Upserted ${result.upserted}, skipped ${result.skipped}, failed ${result.failed}.`;
          renderGap(result);
        },
      });
    } catch (e) {
      msgEl.textContent = `Error: ${e.message}`;
    } finally {
      rebuildRecordsBtn.disabled = false;
    }
  });

  normalizeBtn.addEventListener("click", async () => {
    if (!confirm("Re-check every record against the current schema and fix field inconsistencies (e.g. origin_country → country_of_origin)? Deterministic and safe.")) {
      return;
    }
    normalizeBtn.disabled = true;
    try {
      const { sync_id } = await apiFetch("/sync/normalize", { method: "POST" });
      await pollJob(`/sync/${sync_id}`, msgEl, {
        workingMessage: "Normalizing records...",
        onDone: (result) => {
          msgEl.textContent = `Normalized ${result.changed}, skipped ${result.skipped}, failed ${result.failed}.`;
          renderGap(result);
        },
      });
    } catch (e) {
      msgEl.textContent = `Error: ${e.message}`;
    } finally {
      normalizeBtn.disabled = false;
    }
  });

  checkBtn.addEventListener("click", check);
  check();
}

// --- item detail modal (view / edit / delete) ---

const MODAL_HIDDEN_FIELDS = new Set([
  "_id", "_rev", "uid", "markdown", "schema_version", "created", "updated", "source", "doc_id",
]);

// Editable fields per type, in display order. Fields absent on an item render
// as empty inputs so they can be filled in.
// Fallback edit forms (see the note on GROUPS) — replaced by /categories.
let EDIT_FIELDS = {
  whisky: ["name", "producer", "rating", "status", "stock", "country_of_origin", "region", "category", "peated", "cask", "age_years", "abv", "price_sek", "recommended_by", "tags", "notes", "common_notes"],
  cigar: ["name", "producer", "rating", "status", "stock", "country_of_origin", "wrapper", "vitola", "strength", "price_sek", "recommended_by", "tags", "notes", "common_notes"],
  coffee: ["name", "producer", "rating", "status", "stock", "country_of_origin", "origin", "roaster", "process", "roast_level", "grind_size", "dose_g", "brew_time_s", "grinder", "machine", "price_sek", "recommended_by", "tags", "notes", "common_notes"],
  pairing: ["rating", "tags", "notes"],
};

let FIELD_KIND = {
  rating: "number", price_sek: "number", age_years: "number", dose_g: "number", brew_time_s: "number", stock: "number", abv: "number",
  peated: "bool", status: "status", tags: "tags", components: "tags", notes: "textarea", common_notes: "textarea",
};

// Replace the fallbacks with the worker's category registry (GET /categories).
// Each category carries its group label + ordered edit fields (with render
// kinds), so a backend-added category needs no change here.
function applyCategories(meta) {
  if (!Array.isArray(meta) || meta.length === 0) return;
  GROUPS = meta.map((c) => [c.type, c.label]);
  EDIT_FIELDS = {};
  const kinds = {};
  for (const c of meta) {
    EDIT_FIELDS[c.type] = (c.edit_fields || []).map((f) => f.key);
    for (const f of c.edit_fields || []) kinds[f.key] = f.kind || "text";
  }
  FIELD_KIND = { ...FIELD_KIND, ...kinds };
}

async function initCategories() {
  try {
    const { categories } = await apiFetch("/categories");
    applyCategories(categories);
    // Re-render the (possibly already-rendered) list with the real group order.
    if (typeof reloadItems === "function") renderGroups(document.getElementById("search-text")?.value || "");
  } catch {
    // Non-fatal: the baked-in fallbacks above keep the app fully usable.
  }
}

function labelFor(key) {
  return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function openItemDetail(item) {
  renderDetailRead(item);
  document.getElementById("item-msg").textContent = "";
  document.getElementById("item-modal").classList.remove("hidden");
}

function closeModal() {
  document.getElementById("item-modal").classList.add("hidden");
}

// Render an item's cross-category pairing suggestions (companion↔drink): each is
// an ideal `profile` plus 0-2 `matches` from the vault. A match the user owns is
// a clickable button that opens that item; anything else shows as plain text.
// Tolerates the legacy {item, name, reason} suggestion shape too.
function renderPairings(item, box) {
  const sugg = item.pairings_suggested;
  if (!Array.isArray(sugg) || sugg.length === 0) return;

  const head = document.createElement("h3");
  head.className = "detail-subhead";
  head.textContent = "Pairs with";
  box.appendChild(head);

  const wrap = document.createElement("div");
  wrap.className = "pairings";
  for (const s of sugg) {
    const row = document.createElement("div");
    row.className = "pairing";

    if (s.profile) {
      const p = document.createElement("div");
      p.className = "pairing-profile";
      p.textContent = s.profile;
      row.appendChild(p);
    }

    let matches = Array.isArray(s.matches) ? s.matches : [];
    if (!s.profile && matches.length === 0 && (s.item || s.name)) {
      matches = [{ item: s.item, name: s.name }];  // legacy shape
    }
    for (const m of matches) {
      const owned = allItems.find((i) => i._id === m.item);
      const el = document.createElement(owned ? "button" : "span");
      el.className = owned ? "pairing-match owned" : "pairing-match";
      el.textContent = "→ " + (owned
        ? ([owned.producer, owned.name].filter(Boolean).join(" — ") || owned._id)
        : (m.name || m.item || "?"));
      if (owned) el.addEventListener("click", () => openItemDetail(owned));
      row.appendChild(el);
    }

    if (s.reason) {
      const r = document.createElement("div");
      r.className = "pairing-reason";
      r.textContent = s.reason;
      row.appendChild(r);
    }
    wrap.appendChild(row);
  }
  box.appendChild(wrap);
}

// Classical cocktail pairings (cigar/pipe only): name + why, no vault
// matching — a cocktail isn't inventory. Rendered as its own block below the
// drink pairings in the item detail.
function renderCocktails(item, box) {
  const list = item.cocktail_pairings;
  if (!Array.isArray(list) || list.length === 0) return;

  const head = document.createElement("h3");
  head.className = "detail-subhead";
  head.textContent = "Classic cocktails";
  box.appendChild(head);

  const wrap = document.createElement("div");
  wrap.className = "pairings";
  for (const c of list) {
    const row = document.createElement("div");
    row.className = "pairing cocktail";
    const n = document.createElement("div");
    n.className = "pairing-profile";
    n.textContent = "🍸 " + (c.name || "?");
    row.appendChild(n);
    if (c.reason) {
      const r = document.createElement("div");
      r.className = "pairing-reason";
      r.textContent = c.reason;
      row.appendChild(r);
    }
    wrap.appendChild(row);
  }
  box.appendChild(wrap);
}

// A 5-glyph star row (whole + one half via a clipped gradient) for the detail
// modal, where there's room to show the rating rather than just the number.
function starRow(rating) {
  const row = document.createElement("div");
  row.className = "detail-stars";
  if (typeof rating !== "number") {
    row.classList.add("untasted");
    row.textContent = "Not yet tasted";
    return row;
  }
  const rounded = Math.round(rating * 2) / 2;
  const full = Math.floor(rounded);
  const half = rounded - full === 0.5;
  for (let i = 0; i < 5; i++) {
    const s = document.createElement("span");
    s.textContent = "★";
    if (i < full) { /* filled — inherits --star */ }
    else if (i === full && half) s.className = "star-half";
    else s.className = "star-empty";
    row.appendChild(s);
  }
  const val = document.createElement("span");
  val.className = "stars-value";
  val.textContent = rating.toFixed(1);
  row.appendChild(val);
  return row;
}

// Local (not UTC) calendar date as YYYY-MM-DD — a tasting is dated by the day
// it happened where you are, and toISOString() would roll it back a day for
// anything logged in the evening east of Greenwich.
function todayISO() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Inline rating control on the detail read view: the star row plus a button
// that swaps it for a slider and writes the rating straight through
// record_update.
//
// This is how a recommendation (`status: to-try`) becomes a real tasting. Only
// a tasted note may carry a rating — the schema rejects a rated to-try outright
// — so rating a wishlist entry ALSO flips its status and re-dates it to today,
// all in one write. Before this, the only route was the full edit form, where
// setting a rating without also remembering to switch the status just failed
// the save.
function ratingControl(item) {
  const wrap = document.createElement("div");
  wrap.className = "rating-control";

  function renderRead() {
    wrap.innerHTML = "";
    const rated = typeof item.rating === "number";
    const row = starRow(rated ? item.rating : undefined);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rate-btn";
    btn.textContent = rated ? "change" : "Rate it";
    btn.setAttribute("aria-label", rated ? "Change rating" : "Rate this now that you've tasted it");
    btn.addEventListener("click", renderEdit);
    row.appendChild(btn);
    wrap.appendChild(row);
  }

  function renderEdit() {
    wrap.innerHTML = "";
    const row = document.createElement("div");
    row.className = "rating-row";

    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "1"; slider.max = "5"; slider.step = "0.1";
    slider.value = typeof item.rating === "number" ? String(item.rating) : "3";
    const valueEl = document.createElement("span");
    valueEl.className = "rating-value";
    const render = () => { valueEl.textContent = `${Number(slider.value).toFixed(1)} ★`; };
    slider.addEventListener("input", render);
    render();

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.textContent = "Save";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "rating-clear";
    cancelBtn.textContent = "cancel";
    cancelBtn.addEventListener("click", renderRead);
    saveBtn.addEventListener("click", () => commit(Number(slider.value), [saveBtn, cancelBtn]));

    row.append(slider, valueEl, saveBtn, cancelBtn);
    wrap.appendChild(row);

    if (item.status === "to-try") {
      const hint = document.createElement("p");
      hint.className = "rate-hint";
      hint.textContent = "Saving marks this as tasted, dated today.";
      wrap.appendChild(hint);
    }
  }

  async function commit(rating, buttons) {
    const fields = { rating: Number(rating.toFixed(1)) };
    if (item.status === "to-try") {
      fields.status = "tasted";
      fields.date = todayISO();  // the day it was tasted, not the day it was recommended
    }
    const msgEl = document.getElementById("item-msg");
    for (const b of buttons) b.disabled = true;
    try {
      const { record_id } = await apiFetch("/record/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ doc_id: item._id, fields }),
      });
      await pollJob(`/record/${record_id}`, msgEl, {
        workingMessage: "Saving rating...",
        onDone: async () => {
          Object.assign(item, fields);
          msgEl.textContent = "";
          renderDetailRead(item);  // status/date moved too — redraw the whole card
          await reloadItems();
        },
      });
    } catch (e) {
      msgEl.textContent = `Error: ${e.message}`;
    }
    for (const b of buttons) b.disabled = false;  // no-op once onDone replaced them
  }

  renderRead();
  return wrap;
}

// Inline stock stepper — writes stock straight through record_update, so
// adjusting "how many at home" needs no trip through the full edit form.
function stockStepper(item) {
  const wrap = document.createElement("div");
  wrap.className = "stock-stepper";
  const label = document.createElement("span");
  label.className = "stepper-label";
  label.textContent = "At home";
  const minus = document.createElement("button");
  minus.type = "button"; minus.textContent = "−"; minus.setAttribute("aria-label", "Decrease stock");
  const countEl = document.createElement("span");
  countEl.className = "stepper-count";
  const plus = document.createElement("button");
  plus.type = "button"; plus.textContent = "+"; plus.setAttribute("aria-label", "Increase stock");

  const render = () => {
    countEl.textContent = `×${item.stock ?? 0}`;
    minus.disabled = (item.stock ?? 0) <= 0;
  };
  render();

  async function step(delta) {
    const next = Math.max(0, (item.stock ?? 0) + delta);
    if (next === (item.stock ?? 0)) return;
    minus.disabled = plus.disabled = true;
    const msgEl = document.getElementById("item-msg");
    try {
      const { record_id } = await apiFetch("/record/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ doc_id: item._id, fields: { stock: next } }),
      });
      await pollJob(`/record/${record_id}`, msgEl, {
        workingMessage: "Updating stock...",
        onDone: async () => {
          item.stock = next;
          msgEl.textContent = "";
          render();
          plus.disabled = false;
          await reloadItems();  // keep the list badge in sync
        },
      });
    } catch (e) {
      msgEl.textContent = `Error: ${e.message}`;
      render();
      plus.disabled = false;
    }
  }
  minus.addEventListener("click", () => step(-1));
  plus.addEventListener("click", () => step(1));
  wrap.append(label, minus, countEl, plus);
  return wrap;
}

function renderDetailRead(item) {
  const box = document.getElementById("item-detail");
  box.innerHTML = "";

  const title = document.createElement("h2");
  const flag = flagFor(item.country_of_origin);
  title.textContent = (flag ? flag + " " : "") + ([item.producer, item.name].filter(Boolean).join(" — ") || item._id);
  box.appendChild(title);

  // Rating + stock get expressive treatments; everything else is the grid.
  const isItem = item.type !== "pairing";
  box.appendChild(ratingControl(item));
  if (isItem) box.appendChild(stockStepper(item));

  const dl = document.createElement("dl");
  dl.className = "detail-grid";
  for (const [k, v] of Object.entries(item)) {
    // rating and stock are shown above; don't repeat them in the grid.
    if (MODAL_HIDDEN_FIELDS.has(k) || k === "rating" || k === "stock" || v == null || v === "") continue;
    let display;
    if (Array.isArray(v)) {
      if (v.length === 0 || typeof v[0] === "object") continue; // skip empty / object arrays
      display = v.join(", ");
    } else if (typeof v === "object") {
      continue;
    } else {
      display = String(v);
    }
    const dt = document.createElement("dt");
    dt.textContent = labelFor(k);
    const dd = document.createElement("dd");
    // A flag glyph beside the country reinforces the origin at a glance.
    const kFlag = k === "country_of_origin" ? flagFor(v) : "";
    dd.textContent = (kFlag ? kFlag + " " : "") + display;
    dl.append(dt, dd);
  }
  box.appendChild(dl);
  renderPairings(item, box);
  renderCocktails(item, box);

  const actions = document.createElement("div");
  actions.className = "manage-actions";
  const editBtn = document.createElement("button");
  editBtn.textContent = "Edit";
  editBtn.addEventListener("click", () => renderDetailEdit(item));
  const delBtn = document.createElement("button");
  delBtn.className = "rating-clear danger";
  delBtn.textContent = "Delete";
  delBtn.addEventListener("click", () => deleteItem(item));
  actions.append(editBtn, delBtn);
  box.appendChild(actions);
}

function renderDetailEdit(item) {
  const box = document.getElementById("item-detail");
  box.innerHTML = "";

  const title = document.createElement("h2");
  title.textContent = "Edit";
  box.appendChild(title);

  const fields = EDIT_FIELDS[item.type] || EDIT_FIELDS.whisky;
  const getters = {};

  for (const key of fields) {
    const kind = FIELD_KIND[key] || "text";
    const cur = item[key];
    const label = document.createElement("label");
    label.className = "admin-label";
    label.textContent = labelFor(key);

    let input;
    if (kind === "textarea") {
      input = document.createElement("textarea");
      input.value = cur ?? "";
      getters[key] = () => input.value;
    } else if (kind === "tags") {
      input = document.createElement("input");
      input.value = Array.isArray(cur) ? cur.join(", ") : "";
      getters[key] = () => input.value.split(",").map((s) => s.trim()).filter(Boolean);
    } else if (kind === "bool") {
      input = document.createElement("select");
      for (const [val, txt] of [["", "—"], ["true", "yes"], ["false", "no"]]) {
        const opt = document.createElement("option");
        opt.value = val; opt.textContent = txt;
        input.appendChild(opt);
      }
      input.value = cur === true ? "true" : cur === false ? "false" : "";
      getters[key] = () => (input.value === "" ? null : input.value === "true");
    } else if (kind === "status") {
      input = document.createElement("select");
      for (const val of ["tasted", "to-try"]) {
        const opt = document.createElement("option");
        opt.value = val; opt.textContent = val;
        input.appendChild(opt);
      }
      input.value = cur ?? "tasted";
      getters[key] = () => input.value;
    } else {
      input = document.createElement("input");
      if (kind === "number") input.type = "number";
      input.value = cur ?? "";
      getters[key] = () => {
        const raw = input.value.trim();
        if (raw === "") return null;
        return kind === "number" ? Number(raw) : raw;
      };
    }
    label.appendChild(input);
    box.appendChild(label);
  }

  const actions = document.createElement("div");
  actions.className = "manage-actions";
  const saveBtn = document.createElement("button");
  saveBtn.textContent = "Save";
  saveBtn.addEventListener("click", () => saveEdit(item, getters));
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "rating-clear";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => renderDetailRead(item));
  actions.append(saveBtn, cancelBtn);
  box.appendChild(actions);
}

function changedFields(item, getters) {
  const fields = {};
  for (const [key, get] of Object.entries(getters)) {
    const next = get();
    const cur = item[key] ?? (FIELD_KIND[key] === "tags" ? [] : null);
    if (JSON.stringify(next) !== JSON.stringify(cur)) fields[key] = next;
  }
  return fields;
}

async function saveEdit(item, getters) {
  const fields = changedFields(item, getters);
  const msgEl = document.getElementById("item-msg");
  if (Object.keys(fields).length === 0) {
    renderDetailRead(item);
    return;
  }
  try {
    const { record_id } = await apiFetch("/record/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doc_id: item._id, fields }),
    });
    await pollJob(`/record/${record_id}`, msgEl, {
      workingMessage: "Saving...",
      onDone: async () => { await reloadItems(); closeModal(); },
    });
  } catch (e) {
    msgEl.textContent = `Error: ${e.message}`;
  }
}

async function deleteItem(item) {
  const name = [item.producer, item.name].filter(Boolean).join(" — ") || item._id;
  if (!confirm(`Delete "${name}"? This removes the record and its Obsidian file.`)) return;
  const msgEl = document.getElementById("item-msg");
  try {
    const { record_id } = await apiFetch("/record/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doc_id: item._id }),
    });
    await pollJob(`/record/${record_id}`, msgEl, {
      workingMessage: "Deleting...",
      onDone: async () => { await reloadItems(); closeModal(); },
    });
  } catch (e) {
    msgEl.textContent = `Error: ${e.message}`;
  }
}

function initItemModal() {
  const modal = document.getElementById("item-modal");
  document.getElementById("item-close").addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) closeModal();
  });
}

// Home-screen iOS PWAs (display: standalone) have a long-standing WebKit bug
// where rotating landscape -> portrait leaves the layout viewport stuck at
// the landscape width — body's max-width:520px etc. get computed against the
// stale wider viewport, so content overflows past the right edge with no way
// to scroll it into view. Safari tabs recover on their own; standalone mode
// doesn't. Forcing a reflow after the rotation settles (WebKit fires
// orientationchange before the resize is actually applied, hence the
// setTimeout) makes it recompute against the real portrait width.
function fixStandaloneRotationViewport() {
  window.addEventListener("orientationchange", () => {
    setTimeout(() => {
      document.documentElement.style.display = "none";
      void document.documentElement.offsetHeight; // force layout flush
      document.documentElement.style.display = "";
    }, 50);
  });
}

// Service worker: install-ability only (no caching) — see sw.js.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {
    // Non-fatal — the app works identically without it.
  });
}

fixStandaloneRotationViewport();
initSetup();
initTabs();
initCategories();  // async: swap the fallback groups/forms for the worker's registry
initRatingSlider();
initAdd();
initSearch();
initAdmin();
initManage();
initSync();
initItemModal();
