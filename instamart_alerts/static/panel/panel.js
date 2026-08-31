/* Instamart Alerts — control panel.
 *
 * One page, no build step. State lives in `state`; the server pushes every
 * change worth knowing about over /api/events, so the UI never polls.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// A chat id is a signed integer, or an @username for a public channel.
const CHAT_ID_RE = /^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$/;

const state = {
  settings: {},
  watches: [],
  chatIds: [],
  botUsername: "",
  store: {},
  scheduler: {},
  paths: {},
  dirty: false,
  clockSkew: 0,          // server time minus browser time
  seen: new Set(),       // event seq numbers already rendered
  follow: true,
  levels: "ALL",
  filter: "",
  logs: 0,
};

/* ── tiny helpers ─────────────────────────────────────────────── */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const clock = (ts) =>
  new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false });

const nf = (n) => (Math.round(n * 10) / 10).toString();

function relative(seconds) {
  if (seconds <= 0) return "now";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  if (m >= 60) return `${Math.floor(m / 60)}h ${m % 60}m`;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload = null;
  try {
    payload = await res.json();
  } catch {
    payload = null;
  }
  if (!res.ok) {
    const detail = payload?.detail;
    throw new Error(
      typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : `HTTP ${res.status}`
    );
  }
  return payload;
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind ? "is-" + kind : ""}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 250);
  }, 4200);
}

/** Runs an async action with a spinner on the button it came from. */
async function withBusy(button, fn) {
  if (!button) return fn();
  button.classList.add("is-busy");
  button.disabled = true;
  try {
    return await fn();
  } finally {
    button.classList.remove("is-busy");
    button.disabled = false;
  }
}

/* ── boot ─────────────────────────────────────────────────────── */

async function boot() {
  const session = await api("/api/session");
  if (!session.authed) return showLock(session);
  $("#lock").hidden = true;
  $("#app").hidden = false;
  $("#btn-logout").hidden = !session.password_required;

  const data = await api("/api/bootstrap");
  state.clockSkew = data.now - Date.now() / 1000;
  applySnapshot(data);
  // History arrives as two lists; interleave them so the console reads in the
  // order things actually happened.
  [...data.logs, ...data.runs]
    .sort((a, b) => a.ts - b.ts || a.seq - b.seq)
    .forEach((e) => {
      if (e.seq) state.seen.add(e.seq);
      (e.type === "run" ? renderRun : renderLog)(e, { replayed: true });
    });
  scrollConsole(true);
  connectStream();
  setInterval(tickClock, 1000);
}

function showLock(session) {
  $("#app").hidden = true;
  $("#lock").hidden = false;
  if (!session.password_required) {
    $("#lock-hint").textContent =
      "This panel only answers localhost. Set IM_WEB_PASSWORD to reach it from another machine.";
    $("#lock-form").querySelector(".field").hidden = true;
    $("#lock-form").querySelector("button").hidden = true;
  }
}

/* ── snapshot → UI ────────────────────────────────────────────── */

function applySnapshot(data) {
  state.settings = data.settings;
  state.store = data.store;
  state.scheduler = data.scheduler;
  state.paths = { watchlist: data.watchlist_path, data: data.data_dir };
  if (data.watches) {
    state.watches = data.watches.map(normaliseWatch);
    renderWatches();
    setDirty(false);
  }

  const s = data.settings;
  $("#in-token").value = s.bot_token_masked || "";
  $("#in-token").dataset.masked = s.bot_token_set ? "1" : "";
  state.chatIds = [...(s.chat_ids || [])];
  renderChatIds();
  $("#in-area").value = s.area || "";
  $("#in-minutes").value = s.poll_minutes;
  $("#in-cooldown").value = s.cooldown_hours;
  $("#in-proxy").value = s.proxy || "";
  $("#in-build").value = s.build_version || "";
  $("#in-build").placeholder = s.build_version_default || "2.367.0";
  $("#in-bootstrap").value = s.bootstrap_seconds ?? 30;

  const fail = data.bootstrap_failure;
  $("#boot-fail").hidden = !fail;
  if (fail) {
    // Cache-bust, or a second failure shows the first one's screenshot.
    $("#boot-fail-img").src = `${fail.screenshot}?t=${Math.round(fail.at)}`;
    $("#boot-fail-at").textContent = new Date(fail.at * 1000).toLocaleString();
  }

  const env = data.environment || {};
  $("#env-pill").textContent = env.password_set ? "password set" : "localhost only";
  $("#env-pill").className = `pill ${env.password_set ? "is-good" : "pill--quiet"}`;
  $("#env-list").textContent = [
    "IM_WEB_PASSWORD",
    "IM_DATA_DIR",
    "IM_WATCHLIST",
    env.headless ? null : "IM_HEADLESS=0",
    env.mini_app_dev_mode ? "IM_WEB_DEV=1" : null,
  ]
    .filter(Boolean)
    .join("  ");

  paintTelegramPill();
  const stg = $("#stat-telegram");
  stg.textContent = s.telegram_ready ? "ready" : "off";
  stg.className = `stat__v ${s.telegram_ready ? "is-good" : "is-bad"}`;

  const store = data.store || {};
  $("#stat-store").textContent = store.store_id || "—";
  $("#store-pill").textContent = store.store_id
    ? `store ${store.store_id}`
    : "no store pinned";
  $("#store-pill").className = `pill ${store.store_id ? "is-good" : "pill--quiet"}`;
  $("#store-kv").innerHTML = store.store_id
    ? `<span class="k">resolved</span><span class="v">${esc(store.area_label || "—")}</span>
       <span class="k">store id</span><span class="v">${esc(store.store_id)}</span>`
    : `<span class="k">status</span><span class="v">resolved on the next check or price pull</span>`;

  $("#watchlist-path").textContent = data.watchlist_path || "";
  $("#foot-paths").textContent = `data ${data.data_dir || ""}`;

  applyScheduler(data.scheduler);
  updateWatchStat();
}

/** One pill, two facts: which bot is answering, and how many people it tells. */
function paintTelegramPill() {
  const s = state.settings;
  const n = (s.chat_ids || []).length;
  const who = state.botUsername ? `@${state.botUsername}` : "connected";
  const pill = $("#tg-pill");
  pill.textContent = s.telegram_ready
    ? `${who} · ${n} recipient${n === 1 ? "" : "s"}`
    : "not connected";
  pill.className = `pill ${s.telegram_ready ? "is-good" : ""}`;
}

function applyScheduler(sched) {
  if (!sched) return;
  state.scheduler = sched;
  $("#poller-toggle").checked = !!sched.running;
  const dot = $("#pulse");
  dot.classList.toggle("is-live", !!sched.running && !sched.busy);
  dot.classList.toggle("is-busy", !!sched.busy);
  $("#btn-run").disabled = !!sched.busy;
  $("#btn-dry").disabled = !!sched.busy;
  tickClock();
}

function tickClock() {
  const sched = state.scheduler || {};
  const el = $("#stat-next");
  if (sched.busy) {
    el.textContent = "running";
    el.className = "stat__v is-warm";
    return;
  }
  if (!sched.running || !sched.next_run) {
    el.textContent = "off";
    el.className = "stat__v";
    return;
  }
  const now = Date.now() / 1000 + state.clockSkew;
  el.textContent = relative(sched.next_run - now);
  el.className = "stat__v is-warm";
}

function updateWatchStat() {
  const on = state.watches.filter((w) => w.enabled).length;
  $("#stat-watches").textContent = `${on}/${state.watches.length}`;
}

/* ── event stream ─────────────────────────────────────────────── */

let stream = null;
let retry = 1000;

function connectStream() {
  stream = new EventSource("/api/events");
  stream.onopen = () => {
    retry = 1000;
    $("#live-dot").classList.add("is-on");
    $("#console-status").textContent = "live";
  };
  stream.onmessage = (msg) => {
    let event;
    try {
      event = JSON.parse(msg.data);
    } catch {
      return;
    }
    if (event.seq && state.seen.has(event.seq)) return;
    if (event.seq) state.seen.add(event.seq);
    if (event.type === "log") renderLog(event);
    else if (event.type === "run") renderRun(event);
    else if (event.type === "status" || event.type === "hello")
      applyScheduler(event.scheduler);
  };
  stream.onerror = () => {
    $("#live-dot").classList.remove("is-on");
    $("#console-status").textContent = "reconnecting…";
    stream.close();
    retry = Math.min(retry * 2, 15000);
    setTimeout(connectStream, retry);
  };
}

/* ── console ──────────────────────────────────────────────────── */

const body = () => $("#log-body");

function passesFilter(el) {
  const level = el.dataset.level || "";
  if (state.levels !== "ALL") {
    if (state.levels === "ERROR" && !["ERROR", "CRITICAL"].includes(level)) return false;
    if (state.levels === "WARNING" && !["WARNING", "ERROR", "CRITICAL"].includes(level))
      return false;
    if (state.levels === "INFO" && level === "DEBUG") return false;
  }
  if (state.filter && !(el.dataset.text || "").includes(state.filter)) return false;
  return true;
}

function attach(el) {
  $("#log-empty").hidden = true;
  el.hidden = !passesFilter(el);
  body().appendChild(el);
  // Keep the DOM bounded; the server keeps the authoritative history.
  const lines = body().children;
  while (lines.length > 1600) lines[1].remove();
  scrollConsole();
}

function renderLog(event, opts = {}) {
  const level = (event.level || "INFO").toUpperCase();
  const el = document.createElement("div");
  el.className = `line line--${level}` + (opts.replayed || event.replayed ? " is-replayed" : "");
  el.dataset.level = level;
  el.dataset.text = `${event.logger} ${event.message}`.toLowerCase();
  el.innerHTML =
    `<span class="line__t">${clock(event.ts)}</span>` +
    `<span class="line__l">${esc(level.slice(0, 4))}</span>` +
    `<span class="line__m"><b>${esc(event.logger || "")}</b> ${esc(event.message)}</span>`;
  attach(el);
  state.logs += 1;
  $("#log-count").textContent = `${state.logs} lines`;
}

function renderRun(event, opts = {}) {
  const el = document.createElement("div");
  el.className =
    "runblock" + (event.ok ? "" : " is-bad") + (opts.replayed ? " is-replayed" : "");
  el.dataset.level = event.ok ? "INFO" : "ERROR";
  const label = `${event.trigger === "scheduled" ? "scheduled" : "manual"} ${
    event.dry_run ? "dry run" : "check"
  }`;
  el.dataset.text = label;

  if (!event.ok) {
    el.innerHTML =
      `<div class="runblock__head">${esc(label)} failed <time>${clock(event.ts)}</time></div>` +
      `<div class="rrow"><span class="rrow__name">${esc(event.error)}</span></div>`;
    attach(el);
    return;
  }

  const rows = (event.results || [])
    .map((r) => {
      const hit = r.best >= r.threshold;
      const head =
        `<div class="rrow">` +
        `<span class="rrow__name">${esc(r.name)}</span>` +
        `<span class="rrow__num">${r.tracked} tracked · ${r.hits} over ${nf(r.threshold)}%</span>` +
        `<span class="rrow__best ${hit ? "is-hit" : ""}">best ${nf(r.best)}%</span>` +
        `</div>`;
      const err = r.error
        ? `<div class="rhit">error: ${esc(r.error)}</div>`
        : "";
      const hits = (r.alerted || [])
        .map(
          (p) =>
            `<div class="rhit">${nf(p.discount_pct)}% · ₹${nf(p.price)} <s>₹${nf(p.mrp)}</s> · ` +
            `<a href="${esc(p.url)}" target="_blank" rel="noreferrer">${esc(p.name)}</a> ` +
            `${esc(p.quantity || "")}</div>`
        )
        .join("");
      return head + err + hits;
    })
    .join("");

  const alerted = (event.results || []).reduce((n, r) => n + (r.alerted || []).length, 0);
  el.innerHTML =
    `<div class="runblock__head">${esc(label)} · ${event.duration}s · ` +
    `${alerted} ${event.dry_run ? "would alert" : "sent"}` +
    `<time>${clock(event.ts)}</time></div>` +
    `<div class="runblock__rows">${rows || '<div class="rrow"><span class="rrow__name">no watches ran</span></div>'}</div>`;
  attach(el);
  if (!opts.replayed && !event.dry_run && alerted) {
    toast(`${alerted} alert${alerted === 1 ? "" : "s"} sent to Telegram`, "good");
  }
}

function scrollConsole(force = false) {
  if (!state.follow && !force) return;
  requestAnimationFrame(() => {
    body().scrollTop = body().scrollHeight;
  });
}

function reapplyFilters() {
  let visible = 0;
  for (const el of body().children) {
    if (el.id === "log-empty") continue;
    el.hidden = !passesFilter(el);
    if (!el.hidden) visible += 1;
  }
  $("#log-empty").hidden = visible > 0;
  if (!visible) $("#log-empty").textContent = "nothing matches this filter.";
}

/* ── recipients ───────────────────────────────────────────────── */

let repaintChatIds = () => {};

function renderChatIds() {
  const host = $("#chat-ids");
  host.innerHTML = "";
  repaintChatIds = tagField(
    host,
    state.chatIds,
    () => {},
    "958113963 or @channel",
    (value) => {
      if (CHAT_ID_RE.test(value)) return true;
      toast(`“${value}” is not a chat id — expected a number, or @channelname.`, "bad");
      return false;
    }
  );
}

function addRecipient(id) {
  if (state.chatIds.includes(id)) return false;
  state.chatIds.push(id);
  repaintChatIds();
  return true;
}

/* ── watches ──────────────────────────────────────────────────── */

function normaliseWatch(w) {
  return {
    name: w.name || "",
    query: w.query || "",
    min_discount_pct: Number(w.min_discount_pct ?? 50),
    categories: [...(w.categories || [])],
    include: [...(w.include || [])],
    exclude: [...(w.exclude || [])],
    max_price: w.max_price ?? null,
    in_stock_only: w.in_stock_only !== false,
    enabled: w.enabled !== false,
  };
}

function setDirty(value) {
  state.dirty = value;
  $("#watch-dirty").hidden = !value;
}

function renderWatches() {
  const host = $("#watches");
  const open = new Set(
    $$(".watch.is-open", host).map((el) => Number(el.dataset.index))
  );
  host.innerHTML = "";
  state.watches.forEach((watch, index) => {
    host.appendChild(watchNode(watch, index, open.has(index)));
  });
  updateWatchStat();
}

function watchNode(watch, index, isOpen) {
  const node = $("#tpl-watch").content.firstElementChild.cloneNode(true);
  node.dataset.index = index;
  node.classList.toggle("is-open", isOpen);
  node.classList.toggle("is-off", !watch.enabled);

  const field = (name) => $(`[data-f="${name}"]`, node);
  field("name").value = watch.name;
  field("query").value = watch.query;
  field("enabled").checked = watch.enabled;
  field("in_stock_only").checked = watch.in_stock_only;
  field("max_price").value = watch.max_price ?? "";
  const slider = field("min_discount_pct");
  slider.value = watch.min_discount_pct;

  const paint = () => {
    const pct = Number(slider.value);
    $('[data-role="pct"]', node).textContent = nf(pct);
    $('[data-role="pct-label"]', node).textContent = `${nf(pct)}%`;
    slider.style.setProperty("--fill", `${(pct / 95) * 100}%`);
  };
  paint();

  const commit = () => {
    watch.name = field("name").value.trim();
    watch.query = field("query").value.trim();
    watch.enabled = field("enabled").checked;
    watch.in_stock_only = field("in_stock_only").checked;
    watch.min_discount_pct = Number(slider.value);
    const price = field("max_price").value.trim();
    watch.max_price = price === "" ? null : Number(price);
    node.classList.toggle("is-off", !watch.enabled);
    updateWatchStat();
    setDirty(true);
  };

  $$("input", node).forEach((input) => {
    input.addEventListener("input", () => {
      if (input === slider) paint();
      commit();
    });
  });

  ["categories", "include", "exclude"].forEach((key) => {
    tagField($(`[data-tags="${key}"]`, node), watch[key], (values) => {
      watch[key] = values;
      setDirty(true);
    }, key === "categories" ? "Eggs, Dairy…" : "add…");
  });

  node.addEventListener("click", (e) => {
    const action = e.target.closest("[data-act]")?.dataset.act;
    if (action === "toggle-open") node.classList.toggle("is-open");
    if (action === "delete") {
      state.watches.splice(index, 1);
      renderWatches();
      setDirty(true);
    }
    if (action === "preview") previewWatch(node, watch, e.target);
  });

  return node;
}

/** Chip input backed by an array. Returns its repaint, for outside edits. */
function tagField(host, values, onChange, placeholder, validate) {
  const input = document.createElement("input");
  input.placeholder = placeholder;
  input.spellcheck = false;

  const paint = () => {
    $$(".tag", host).forEach((t) => t.remove());
    values.forEach((value, i) => {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.innerHTML = `${esc(value)}<button type="button" title="Remove">✕</button>`;
      tag.querySelector("button").onclick = () => {
        values.splice(i, 1);
        paint();
        onChange(values);
      };
      host.insertBefore(tag, input);
    });
  };

  const add = () => {
    const value = input.value.trim().replace(/,$/, "");
    if (!value) return (input.value = "");
    if (validate && !validate(value)) return;   // leave it in place to be fixed
    if (!values.includes(value)) {
      values.push(value);
      paint();
      onChange(values);
    }
    input.value = "";
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      add();
    } else if (e.key === "Backspace" && !input.value && values.length) {
      values.pop();
      paint();
      onChange(values);
    }
  });
  input.addEventListener("blur", add);

  host.appendChild(input);
  paint();
  return paint;
}

async function previewWatch(node, watch, button) {
  const host = $('[data-role="preview"]', node);
  if (!watch.query) return toast("Give this watch a search term first", "bad");
  node.classList.add("is-open");
  host.hidden = false;
  host.innerHTML = `<div class="preview__head">searching ${esc(watch.query)}… this can take a moment on a cold session</div>`;

  await withBusy(button, async () => {
    try {
      const data = await api(`/api/preview?query=${encodeURIComponent(watch.query)}`);
      renderPreview(host, data, watch);
    } catch (e) {
      host.innerHTML = `<div class="preview__head" style="color:var(--alarm)">${esc(e.message)}</div>`;
    }
  });
}

function renderPreview(host, data, watch) {
  const matches = (p) => {
    const hay = `${p.category} ${p.sub_category}`.toLowerCase();
    const name = (p.name || "").toLowerCase();
    if (watch.in_stock_only && !p.in_stock) return false;
    if (watch.categories.length && !watch.categories.some((c) => hay.includes(c.toLowerCase())))
      return false;
    if (watch.include.length && !watch.include.some((s) => name.includes(s.toLowerCase())))
      return false;
    if (watch.exclude.some((s) => name.includes(s.toLowerCase()))) return false;
    return true;
  };

  const kept = data.products.filter(matches);
  const rows = kept
    .map((p) => {
      const hit =
        p.discount_pct >= watch.min_discount_pct &&
        (watch.max_price == null || p.price <= watch.max_price);
      return (
        `<div class="prow ${hit ? "is-hit" : ""} ${p.in_stock ? "" : "is-out"}">` +
        `<span class="prow__pct">${nf(p.discount_pct)}%</span>` +
        `<span class="prow__name"><a href="${esc(p.url)}" target="_blank" rel="noreferrer">${esc(p.name)}</a>` +
        `<small>${esc(p.quantity || "")}${p.category ? " · " + esc(p.category) : ""}${p.in_stock ? "" : " · out of stock"}</small></span>` +
        `<span class="prow__price">₹${nf(p.price)}<s>₹${nf(p.mrp)}</s></span>` +
        `<span class="prow__bar"><i style="width:${Math.min(100, p.discount_pct)}%"></i></span>` +
        `</div>`
      );
    })
    .join("");

  host.innerHTML =
    `<div class="preview__head">` +
    `<b>${kept.length}</b> after filters of ${data.products.length} results · store ${esc(data.store_id)} · ` +
    `${esc(data.area || "")}</div>` +
    `<div class="preview__list">${rows || '<div class="prow"><span class="prow__name">nothing survives these filters</span></div>'}</div>`;
}

/* ── wiring ───────────────────────────────────────────────────── */

function wire() {
  // Telegram
  $("#btn-reveal").onclick = () => {
    const input = $("#in-token");
    input.type = input.type === "password" ? "text" : "password";
  };
  $("#in-token").addEventListener("focus", (e) => {
    if (e.target.dataset.masked === "1") {
      e.target.value = "";
      e.target.dataset.masked = "";
    }
  });

  $("#btn-save-tg").onclick = (e) =>
    withBusy(e.target, async () => {
      const token = $("#in-token").value.trim();
      const payload = { chat_id: state.chatIds };
      if (token && !token.includes("•")) payload.bot_token = token;
      try {
        applySnapshot(await api("/api/settings", { method: "PUT", body: payload }));
        showTgResult("Saved.", "good");
        verifyToken();
      } catch (err) {
        showTgResult(err.message, "bad");
      }
    });

  $("#btn-find-chat").onclick = (e) =>
    withBusy(e.target, async () => {
      const picker = $("#chat-picker");
      picker.hidden = false;
      picker.innerHTML = `<div class="result">asking Telegram…</div>`;
      try {
        const data = await api("/api/telegram/chats");
        data.chats.forEach((c) => (c.added = state.chatIds.includes(c.id)));
        if (!data.ok) {
          picker.innerHTML = `<div class="result is-bad">${esc(data.error)}</div>`;
          return;
        }
        if (!data.chats.length) {
          picker.innerHTML =
            `<div class="result">No one has messaged this bot yet. Open ` +
            `<a class="field__aside" href="https://t.me/${esc(data.username)}" target="_blank" rel="noreferrer">@${esc(data.username)} ↗</a>` +
            `, press Start, then try again.</div>`;
          return;
        }
        picker.innerHTML = data.chats
          .map(
            (c) =>
              `<button class="chat-opt ${c.added ? "is-added" : ""}" type="button" ` +
              `data-id="${esc(c.id)}"><b>${esc(c.id)}</b> ${esc(c.label)}` +
              `<span>${esc(c.type)}</span></button>`
          )
          .join("");
        $$(".chat-opt", picker).forEach((btn) => {
          btn.onclick = () => {
            const id = btn.dataset.id;
            if (addRecipient(id)) {
              btn.classList.add("is-added");
              toast(`${id} added — press Save connection to keep it.`);
            } else {
              toast(`${id} is already a recipient.`);
            }
          };
        });
      } catch (err) {
        picker.innerHTML = `<div class="result is-bad">${esc(err.message)}</div>`;
      }
    });

  $("#btn-test").onclick = (e) =>
    withBusy(e.target, async () => {
      try {
        const data = await api("/api/telegram/test", { method: "POST" });
        const n = (data.delivered || []).length;
        if (data.ok) {
          showTgResult(
            `Test alert delivered to ${n} recipient${n === 1 ? "" : "s"} — check Telegram.`,
            "good"
          );
        } else if (n) {
          showTgResult(
            `Delivered to ${data.delivered.join(", ")}. ${data.error}`,
            "bad"
          );
        } else {
          showTgResult(data.error, "bad");
        }
      } catch (err) {
        showTgResult(err.message, "bad");
      }
    });

  // Area
  $("#btn-save-area").onclick = (e) =>
    withBusy(e.target, async () => {
      try {
        applySnapshot(
          await api("/api/settings", { method: "PUT", body: { area: $("#in-area").value } })
        );
        toast("Area saved — the dark store will re-resolve.", "good");
      } catch (err) {
        toast(err.message, "bad");
      }
    });

  // Watches
  $("#btn-add-watch").onclick = () => {
    state.watches.push(
      normaliseWatch({ name: "New watch", query: "", min_discount_pct: 50 })
    );
    renderWatches();
    const last = $("#watches").lastElementChild;
    last.classList.add("is-open");
    $('[data-f="name"]', last).select();
    setDirty(true);
  };

  $("#btn-save-watches").onclick = (e) =>
    withBusy(e.target, async () => {
      const bad = state.watches.find((w) => !w.query.trim());
      if (bad) return toast("Every watch needs a search term.", "bad");
      try {
        const data = await api("/api/watches", {
          method: "PUT",
          body: {
            watches: state.watches.map((w) => ({
              ...w,
              name: w.name || w.query,
            })),
          },
        });
        state.watches = data.watches.map(normaliseWatch);
        setDirty(false);
        toast("Watchlist saved.", "good");
      } catch (err) {
        toast(err.message, "bad");
      }
    });

  // Runs
  const run = (dry) => (e) =>
    withBusy(e.target, async () => {
      try {
        await api("/api/check", { method: "POST", body: { dry_run: dry } });
        toast(dry ? "Dry run started — watch the console." : "Check started.", "good");
      } catch (err) {
        toast(err.message, "bad");
      }
    });
  $("#btn-run").onclick = run(false);
  $("#btn-dry").onclick = run(true);

  $("#btn-save-conn").onclick = (e) =>
    withBusy(e.target, async () => {
      try {
        applySnapshot(
          await api("/api/settings", {
            method: "PUT",
            body: {
              proxy: $("#in-proxy").value,
              build_version: $("#in-build").value,
              bootstrap_seconds: Number($("#in-bootstrap").value) || undefined,
            },
          })
        );
        toast("Connection settings saved.", "good");
      } catch (err) {
        toast(err.message, "bad");
      }
    });

  $("#btn-save-timing").onclick = (e) =>
    withBusy(e.target, async () => {
      try {
        applySnapshot(
          await api("/api/settings", {
            method: "PUT",
            body: {
              poll_minutes: Number($("#in-minutes").value),
              cooldown_hours: Number($("#in-cooldown").value),
            },
          })
        );
        toast("Timing saved.", "good");
      } catch (err) {
        toast(err.message, "bad");
      }
    });

  $("#poller-toggle").onchange = async (e) => {
    const enabled = e.target.checked;
    try {
      applyScheduler(
        await api("/api/poller", {
          method: "POST",
          body: { enabled, minutes: Number($("#in-minutes").value) || undefined },
        })
      );
      toast(enabled ? "Poller on." : "Poller off.", enabled ? "good" : "");
    } catch (err) {
      e.target.checked = !enabled;
      toast(err.message, "bad");
    }
  };

  const reset = (target, message) => (e) =>
    withBusy(e.target, async () => {
      try {
        await api("/api/reset", { method: "POST", body: { target } });
        toast(message, "good");
        if (target !== "logs") applySnapshot(await api("/api/bootstrap"));
      } catch (err) {
        toast(err.message, "bad");
      }
    });
  $("#btn-reset-session").onclick = reset("session", "Session cleared.");
  $("#btn-reset-alerts").onclick = reset("alerts", "Alert history cleared.");

  // Console
  $("#levels").onclick = (e) => {
    const btn = e.target.closest(".lvl");
    if (!btn) return;
    $$(".lvl").forEach((b) => b.classList.toggle("is-on", b === btn));
    state.levels = btn.dataset.level;
    reapplyFilters();
  };
  $("#log-filter").oninput = (e) => {
    state.filter = e.target.value.trim().toLowerCase();
    reapplyFilters();
  };
  $("#btn-follow").onclick = (e) => {
    state.follow = !state.follow;
    e.target.dataset.on = state.follow ? "1" : "";
    scrollConsole();
  };
  body().addEventListener("scroll", () => {
    const atBottom =
      body().scrollHeight - body().scrollTop - body().clientHeight < 40;
    if (!atBottom && state.follow) {
      state.follow = false;
      $("#btn-follow").dataset.on = "";
    }
  });
  $("#btn-clear-logs").onclick = async () => {
    await api("/api/reset", { method: "POST", body: { target: "logs" } });
    body().querySelectorAll(".line, .runblock").forEach((el) => el.remove());
    state.logs = 0;
    $("#log-count").textContent = "0 lines";
  };
  $("#btn-download").onclick = () => {
    const text = $$(".line", body())
      .map((el) => el.textContent.replace(/\s+/g, " ").trim())
      .join("\n");
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `instamart-console-${new Date().toISOString().slice(0, 19)}.log`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Auth
  $("#lock-form").onsubmit = async (e) => {
    e.preventDefault();
    $("#lock-error").hidden = true;
    try {
      await api("/api/login", {
        method: "POST",
        body: { password: $("#lock-password").value },
      });
      location.reload();
    } catch (err) {
      $("#lock-error").textContent = err.message;
      $("#lock-error").hidden = false;
    }
  };
  $("#btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.reload();
  };

  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

function showTgResult(message, kind) {
  const el = $("#tg-result");
  el.hidden = false;
  el.textContent = message;
  el.className = `result is-${kind}`;
}

async function verifyToken() {
  try {
    const data = await api("/api/telegram/identity");
    if (data.ok) {
      state.botUsername = data.username;
      paintTelegramPill();
    }
  } catch {
    /* the pill already reflects the saved state */
  }
}

wire();
boot().catch((e) => {
  document.documentElement.dataset.boot = "failed";
  toast(`Could not load the panel: ${e.message}`, "bad");
});
