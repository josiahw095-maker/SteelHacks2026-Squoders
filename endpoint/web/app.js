/* Mesh Mail endpoint - the screen.
 *
 * Four calls to the server and nothing else: GET /api/state, POST /api/connect,
 * POST /api/reply, POST /api/compose. Nothing here knows what a packet is, so
 * moving to a Web Bluetooth transport later means replacing these four and
 * leaving the interface alone.
 */

const POLL_MS = 2000;

const el = (id) => document.getElementById(id);
const openThreads = new Set();   // conversations the reader has expanded
const drafts = {};               // half-typed replies, kept across refreshes
let sending = false;             // pause polling mid-send so nothing redraws
let lastCount = 0;               // to notice new mail arriving

const clock = (t) => new Date(t * 1000)
  .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/* The live feed shows seconds and milliseconds: it is how you read the gaps
 * between packets, which is what the timing diagnostics are about. */
const clockMs = (t) => {
  const d = new Date(t * 1000);
  // 24-hour, like the gateway's log, so the two can be read side by side.
  return d.toLocaleTimeString([], { hourCycle: "h23", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    + "." + String(d.getMilliseconds()).padStart(3, "0");
};

function esc(text) {
  const n = document.createElement("div");
  n.textContent = text == null ? "" : text;
  return n.innerHTML;
}

function initials(name) {
  const parts = (name || "?").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return (parts[0][0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
}

/* The page polls every couple of seconds, but the radio is usually quiet.
 * Rewriting innerHTML on every poll made the whole list flash and threw away
 * whatever the reader was typing, so each section redraws only when the data
 * behind it has actually changed. */
const drawn = {};
function changed(key, value) {
  const sig = JSON.stringify(value);
  if (drawn[key] === sig) return false;
  drawn[key] = sig;
  return true;
}

let toastTimer;
function toast(message, kind) {
  const t = el("toast");
  t.textContent = message;
  t.className = "toast on " + (kind || "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast " + (kind || ""); }, 2600);
}

/* ---------- rendering ---------- */

function renderStatus(state) {
  const pill = el("status");
  const live = !state.error;
  pill.className = "pill " + (live ? "live" : "bad");
  pill.querySelector("span").textContent = state.error ? "not connected" : state.status;

  const sel = el("transport");
  const want = (state.options || []).map((o) => o.value).join("|");
  if (sel.dataset.built !== want) {
    sel.innerHTML = state.options
      .map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("");
    sel.dataset.built = want;
  }
  if (document.activeElement !== sel) sel.value = state.target;
}

function renderStats(t) {
  if (!changed("stats", t)) return;
  el("stats").innerHTML = [
    [t.messages, "emails", true],
    [t.packets, "packets", false],
    [t.airbytes + " B", "sent over LoRa", false],
    [t.delivered + " B", "delivered", false],
  ].map(([v, k, hero]) =>
    `<div class="stat${hero ? " hero" : ""}"><b>${v}</b><span>${k}</span></div>`).join("");
}

function renderRail(state) {
  if (!changed("rail", [state.totals, state.target, state.node, state.error])) {
    if (changed("feed", state.feed)) renderFeed(state.feed);
    return;
  }
  const t = state.totals;
  const saved = t.delivered > 0
    ? Math.round((1 - t.airbytes / t.delivered) * 100) : 0;
  el("rail-node").textContent = state.node || "";
  el("rail-link").innerHTML = [
    ["Transport", esc(state.target)],
    ["Status", state.error ? "error" : "connected"],
    ["Emails", t.messages],
    ["Packets", t.packets],
    ["Bytes on air", t.airbytes + " B"],
    ["Text delivered", t.delivered + " B"],
    ["Bandwidth saved", saved + "%"],
  ].map(([k, v]) => `<div class="kv"><span>${k}</span><b>${v}</b></div>`).join("");

  if (changed("feed", state.feed)) renderFeed(state.feed);
}

function renderFeed(feed) {
  feed = feed || [];
  el("rail-count").textContent = feed.length ? feed.length + " recent" : "";
  el("feed").innerHTML = feed.length
    ? feed.map((e) => `<div class="ev ${esc(e.kind)}">
        <time>${clockMs(e.at)}</time>
        <span class="what">${esc(e.text)}</span>
        <span class="sz">${e.bytes ? e.bytes + " B" : ""}</span></div>`).join("")
    : `<div class="empty">Nothing on the air yet.</div>`;
}

function renderArriving(list) {
  if (!changed("arriving", list)) return;
  el("arriving").innerHTML = list.map((w) => `
    <div class="thread arriving">
      <div style="padding:.85rem 1rem">
        <div class="subject"><span class="wave"><i></i><i></i><i></i></span>Message arriving</div>
        <div class="from">id ${esc(w.id)} &nbsp;·&nbsp; ${w.packets} packet(s) received${
          w.asked ? ` &nbsp;·&nbsp; asked for the missing parts ${w.asked}×` : ""}</div>
      </div>
    </div>`).join("");
}

function renderThreads(threads) {
  if (!changed("threads", threads)) return;

  // Rebuilding replaces the textarea the reader may be typing in, so put the
  // caret back where it was afterwards.
  const active = document.activeElement;
  const typing = active && active.tagName === "TEXTAREA" && active.closest("form[data-reply]")
    ? { thread: active.closest("form[data-reply]").dataset.reply,
        start: active.selectionStart, end: active.selectionEnd }
    : null;

  const box = el("threads");
  if (!threads.length) {
    box.innerHTML = `<div class="empty">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
        <rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M3 7l9 6 9-6"/></svg>
      <h3>No mail yet</h3><div>Messages appear here as the radio receives them.</div></div>`;
    return;
  }

  box.innerHTML = threads.map((t, i) => {
    const isOpen = openThreads.has(String(t.thread)) || (openThreads.size === 0 && i === 0);
    const body = t.messages.map((m) => {
      const pct = Math.max(6, Math.min(100, Math.round((1 - m.airbytes / Math.max(m.delivered, 1)) * 100)));
      return `<div class="msg">
        <div class="from"><strong style="color:var(--ink)">${esc(m.sender || "unknown")}</strong>
          &nbsp;<span class="when">${clock(m.at)}</span></div>
        <p>${esc(m.body)}</p>
        <div class="meta">
          <span>${m.packets} packet${m.packets === 1 ? "" : "s"}</span><span>·</span>
          <span>${m.airbytes} B on air</span><span>·</span>
          <span>${m.delivered} B delivered</span>
          <span class="gauge"><i style="width:${pct}%"></i></span>
          <span class="ratio">${m.ratio}×</span>
        </div></div>`;
    }).join("");

    return `<details class="thread" data-thread="${esc(String(t.thread))}" ${isOpen ? "open" : ""}>
      <summary>
        <span class="avatar">${esc(initials(t.sender))}</span>
        <span class="who"><span class="subject">${esc(t.subject)}</span>
          <span class="from">${esc(t.sender || "unknown")}</span></span>
        ${t.messages.length > 1 ? `<span class="count">${t.messages.length}</span>` : ""}
        <span class="when">${clock(t.at)}</span>
      </summary>
      ${body}
      <form data-reply="${esc(String(t.thread))}">
        <textarea rows="2" placeholder="Write a reply…">${esc(drafts[t.thread] || "")}</textarea>
        <div class="track"><i></i></div>
        <div class="row"><button type="submit">Send reply</button>
          <span class="note"></span></div>
      </form></details>`;
  }).join("");

  box.querySelectorAll("details.thread").forEach((d) => {
    d.addEventListener("toggle", () =>
      d.open ? openThreads.add(d.dataset.thread) : openThreads.delete(d.dataset.thread));
  });
  box.querySelectorAll("form[data-reply]").forEach((f) => {
    const area = f.querySelector("textarea");
    area.addEventListener("input", () => { drafts[f.dataset.reply] = area.value; });
    f.addEventListener("submit", (e) => { e.preventDefault(); sendReply(f); });
  });

  if (typing) {
    const back = box.querySelector(`form[data-reply="${typing.thread}"] textarea`);
    if (back) {
      back.focus();
      try { back.setSelectionRange(typing.start, typing.end); } catch (err) {}
    }
  }
}

/* ---------- server ---------- */

async function refresh() {
  if (sending) return;
  try {
    const state = await (await fetch("/api/state")).json();
    renderStatus(state);
    renderStats(state.totals);
    renderRail(state);
    renderArriving(state.waiting);
    renderThreads(state.threads);
    if (state.totals.messages > lastCount && lastCount !== 0) toast("New mail received", "good");
    lastCount = state.totals.messages;
  } catch (err) {
    const pill = el("status");
    pill.className = "pill bad";
    pill.querySelector("span").textContent = "server unreachable";
  }
}

async function watchProgress(track) {
  const fill = track.querySelector("i");
  track.classList.add("on");
  fill.style.width = "5%";
  while (sending) {
    try {
      const p = (await (await fetch("/api/state")).json()).progress;
      if (p && p.total) fill.style.width = Math.round((p.sent / p.total) * 100) + "%";
      if (p && !p.active) break;
    } catch (err) { break; }
    await new Promise((r) => setTimeout(r, 300));
  }
  fill.style.width = "100%";
  setTimeout(() => { track.classList.remove("on"); fill.style.width = "0"; }, 700);
}

async function post(url, payload, track, note, button) {
  sending = true;
  button.disabled = true;
  note.className = "note";
  note.textContent = "";
  const watching = watchProgress(track);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const out = await res.json();
    sending = false;
    await watching;
    if (out.ok) { note.className = "note good"; note.textContent = `sent in ${out.packets} packet(s)`; }
    else { note.className = "note bad"; note.textContent = out.error || "send failed"; toast(out.error || "send failed", "bad"); }
    return out.ok;
  } catch (err) {
    note.className = "note bad";
    note.textContent = "could not reach the server";
    return false;
  } finally {
    sending = false;
    button.disabled = false;
  }
}

async function sendReply(form) {
  const area = form.querySelector("textarea");
  if (!area.value.trim()) { toast("Nothing to send", "bad"); return; }
  const ok = await post("/api/reply",
    { thread: Number(form.dataset.reply), body: area.value },
    form.querySelector(".track"), form.querySelector(".note"),
    form.querySelector("button"));
  if (ok) {
    delete drafts[form.dataset.reply];
    area.value = "";
    delete drawn.threads;          // force one redraw so the box clears
    toast("Reply sent", "good");
  }
  refresh();
}

/* ---------- wiring ---------- */

el("transport").addEventListener("change", async (e) => {
  const target = e.target.value;
  const pill = el("status");
  pill.className = "pill";
  pill.querySelector("span").textContent = "switching…";
  try {
    const out = await (await fetch("/api/connect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    })).json();
    toast(out.ok ? out.status : (out.error || "could not connect"), out.ok ? "good" : "bad");
  } catch (err) {
    toast("could not reach the server", "bad");
  }
  lastCount = 0;
  refresh();
});

const sheet = el("sheet");
el("open-compose").addEventListener("click", () => {
  sheet.classList.add("on");
  sheet.querySelector('input[name="to"]').focus();
});
el("close-compose").addEventListener("click", () => sheet.classList.remove("on"));
sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.classList.remove("on"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") sheet.classList.remove("on"); });

el("compose").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const ok = await post("/api/compose",
    { to: f.to.value, subject: f.subject.value, body: f.body.value },
    el("compose-track"), el("compose-note"), f.querySelector("button"));
  if (ok) {
    f.subject.value = ""; f.body.value = "";
    sheet.classList.remove("on");
    toast("Email sent over LoRa", "good");
  }
});

refresh();
setInterval(refresh, POLL_MS);
