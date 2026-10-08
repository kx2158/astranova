/* Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited. AN-AIXENI-7f3c9e21 */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
let api = null;
let state = null;
let current = null;            // current Astra conversation id
const running = new Set();     // conversations with an active agent
let live = null;               // streaming state for the current Astra conversation
let flive = null;              // streaming state for the friend
const FRIEND = "friend";
const STAR = '<svg><use href="#flare"/></svg>';

/* ---------------- utilities ---------------- */
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function toast(text, opts = {}) {
  const t = document.createElement("div"); t.className = "toast" + (opts.onclick ? " click" : ""); t.textContent = text;
  if (opts.onclick) t.onclick = () => { opts.onclick(); t.remove(); };
  document.body.appendChild(t); setTimeout(() => t.remove(), opts.ms || 3200);
}
function linkify(escaped) {
  return escaped.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2">$2</a>')
    .replace(/(^|[\s(:])([A-Za-z]:\\[^\n<>"|?*]+?\.[A-Za-z0-9]{1,6})(?=[\s).,;]|$)/g, '$1<a class="path" data-path="$2">$2</a>');
}
function hm(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function viewActive(name) { return $("#view-" + name).classList.contains("active"); }
function confirmInline(btn, label) {   // two-step button instead of a blocking dialog
  if (btn.dataset.armed) { delete btn.dataset.armed; btn.textContent = btn.dataset.orig; return true; }
  btn.dataset.orig = btn.textContent; btn.dataset.armed = "1"; btn.textContent = label;
  setTimeout(() => { if (btn.dataset.armed) { delete btn.dataset.armed; btn.textContent = btn.dataset.orig; } }, 3000);
  return false;
}
function nodash(t) {
  return String(t ?? "").replace(/(\d)\s*\u2013\s*(\d)/g, "$1-$2").replace(/\s*[\u2014\u2015\u2013]\s*/g, ", ");
}
function md(src) {
  src = nodash(src);
  const blocks = [];
  let s = esc(src).replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, c) => { blocks.push(c); return `\u0000${blocks.length - 1}\u0000`; });
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>")
       .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
       .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2">$1</a>')
       .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2">$2</a>')
       .replace(/(^|[\s(:])([A-Za-z]:\\[^\n<>"|?*]+?\.[A-Za-z0-9]{1,6})(?=[\s).,;]|$)/g, '$1<a class="path" data-path="$2">$2</a>');
  // Lists keep their real numbers: models often put a blank line or a short explanation between items, which
  // used to close the list and restart it at 1. Items carry their own number (value=), explanations stay inside.
  const out = []; let list = null, li = null, gap = false;
  const flushLi = () => { if (li) { out.push(li.html + (li.sub ? `<ul>${li.sub.join("")}</ul>` : "") + "</li>"); li = null; } };
  const closeList = () => { flushLi(); if (list) { out.push(`</${list.type}>`); list = null; } };
  for (const line of s.split("\n")) {
    if (!line.trim()) { if (list) gap = true; continue; }
    const m = line.match(/^(\s*)([-*\u2022]|\d+[.)])\s+(.*)/), h = line.match(/^#{1,4}\s+(.*)/);
    if (m) {
      const indent = m[1].replace(/\t/g, "    ").length, ordered = /\d/.test(m[2]);
      if (list && li && indent >= 2) { (li.sub = li.sub || []).push(`<li>${m[3]}</li>`); gap = false; continue; }
      const type = ordered ? "ol" : "ul";
      if (list && list.type !== type) closeList();
      if (!list) { out.push(`<${type}>`); list = { type }; }
      flushLi();
      li = { html: ordered ? `<li value="${parseInt(m[2], 10)}">${m[3]}` : `<li>${m[3]}` };
      gap = false; continue;
    }
    if (list && li && !h && (/^\s{2,}/.test(line) || !gap)) { li.html += `<span class="li-more">${line.trim()}</span>`; continue; }
    closeList(); gap = false;
    if (h) out.push(`<h3>${h[1]}</h3>`);
    else out.push(`<p>${line}</p>`);
  }
  closeList();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre>${blocks[i]}</pre>`);
}
function fmtTime(ts) { const d = new Date(ts * 1000); return d.toLocaleString([], { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }); }
function fmtWhen(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const same = d.toDateString() === now.toDateString();
  return (same ? "today " : d.toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short" }) + " ") + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function host(u) { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; } }
function autosize(el) { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 220) + "px"; }
function bar(el, p) { el.classList.toggle("indet", p == null); el.querySelector("div").style.width = p == null ? "" : Math.round(p * 100) + "%"; }

document.addEventListener("click", e => {
  const a = e.target.closest("a");
  if (!a) return;
  if (a.dataset.path) { e.preventDefault(); api.open_local(a.dataset.path).then(ok => { if (!ok) toast("File not found"); }); return; }
  const h = a.getAttribute("href");
  if (h && /^https?:/.test(h)) { e.preventDefault(); api.open_url(h); }
});

/* ---------------- navigation ---------------- */
function showView(name) {
  // colour lives in the chats; everywhere else the tint fades down to near-monochrome
  document.body.classList.toggle("muted", !["chat", "friend"].includes(name));
  document.body.dataset.view = name;
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + name));
  $$(".nav").forEach(n => n.classList.toggle("active", n.dataset.view === name));
  if (name !== "chat") $$(".conv").forEach(c => c.classList.remove("active"));
  if (name === "friend") { $("#friendDot").classList.add("hidden"); loadFriend(); setTimeout(() => $("#finput").focus(), 50); }
  if (name === "routines") loadRoutines();
  api.ui_view(name);
  if (name === "memory") loadMemory();
  if (name === "calendar") loadWeek();
  if (name === "activity") loadActivity();
  if (name === "settings") fillSettings();
}
$$(".nav").forEach(n => n.onclick = () => n.dataset.view === "chat" && !current ? newChat() : showView(n.dataset.view));

/* ---------------- conversations ---------------- */
async function refreshConvs() {
  const list = await api.list_conversations();
  const chatActive = $("#view-chat").classList.contains("active");
  $("#convList").innerHTML = list.map(c => `
    <button class="conv ${chatActive && c.id === current ? "active" : ""}" data-id="${c.id}">
      ${running.has(c.id) ? '<i class="live"></i>' : ""}<span>${esc(c.title || "New chat")}</span><i class="x" data-del="${c.id}">×</i>
    </button>`).join("");
  $$(".conv").forEach(b => b.onclick = async e => {
    if (e.target.dataset.del) {
      e.stopPropagation();
      await api.delete_conversation(e.target.dataset.del);
      if (current === e.target.dataset.del) newChat();
      return refreshConvs();
    }
    openConv(b.dataset.id);
  });
  $("#panicBtn").classList.toggle("hidden", running.size === 0);
}

function newChat() {
  current = null; live = null;
  $("#messages").innerHTML = "";
  $("#empty").classList.remove("hidden");
  setRunningUI(false);
  showView("chat"); refreshConvs();
  $("#input").focus();
}
$("#newChat").onclick = newChat;

async function openConv(id) {
  current = id; live = null;
  showView("chat");
  const { items, running: isRunning, notes } = await api.load_conversation(id);
  $("#messages").innerHTML = ""; lastTs.delete($("#messages"));
  delete noteCards[id];
  $("#empty").classList.toggle("hidden", items.length > 0);
  for (const it of items) {
    if (it.role === "user") addUser(it.text, it.ts);
    else if (it.role === "assistant") addAssistant(it.text, it.ts);
    else if (it.role === "step") addStep(it.label, it.args, "ok");
    else if (it.role === "brief") addBrief(it);
  }
  if (notes && notes.length) { const nc = ensureNotes(id, $("#messages"), true); notes.forEach(n => nc.item(n.item, host(n.source || ""))); nc.finish(notes.length); }
  (state.requests || []).filter(r => !r.conv || r.conv === id).forEach(addRequest);
  if (isRunning) { running.add(id); live = { text: "" }; thinking(true); }
  setRunningUI(running.has(id));
  refreshConvs(); scrollDown(true);
}

/* ---------------- Astra chat rendering ---------------- */
function scrollDown(force, el = $("#thread")) {
  if (force || el.scrollHeight - el.scrollTop - el.clientHeight < 220) el.scrollTop = el.scrollHeight;
}
function stripThink(t) {   // safety net in the UI too, for every way models mark their reasoning
  t = String(t || "").replace(/<(think|thinking|reasoning)>[\s\S]*?(<\/\1>|$)/gi, "")
    .replace(/<\|channel\|>\s*analysis[\s\S]*?(<\|end\|>|$)/gi, "").replace(/<\|[a-z]+\|>(assistant|final|commentary)?(<\|message\|>)?/gi, "");
  const orphan = t.search(/<\/(think|thinking|reasoning)>/i);
  return orphan >= 0 ? t.slice(t.indexOf(">", orphan) + 1) : t;
}
/* time marks: a separator when time has moved on, and the time on every message */
const lastTs = new WeakMap();
function whenLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (sameDay(d, now)) return `Today ${t}`;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (sameDay(d, y)) return `Yesterday ${t}`;
  if (now - d < 6 * 86400000) return `${d.toLocaleDateString([], { weekday: "long" })} ${t}`;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" })} ${t}`;
}
function timeSep(container, ts) {
  ts = ts || Date.now() / 1000;
  const prev = lastTs.get(container);
  if (!prev || ts - prev > 30 * 60) {
    const sep = document.createElement("div"); sep.className = "tsep"; sep.textContent = whenLabel(ts);
    container.appendChild(sep);
  }
  lastTs.set(container, ts);
  return ts;
}
function addUser(text, ts) {
  ts = timeSep($("#messages"), ts);
  const d = document.createElement("div"); d.className = "msg user";
  d.innerHTML = `<div class="bubble">${esc(text)}</div><div class="mtime">${hm(ts)}</div>`; $("#messages").appendChild(d); return d;
}
function markQueued() {
  const last = [...$$("#messages .msg.user")].pop();
  if (last) { last.classList.add("queued"); last.querySelector(".bubble").insertAdjacentHTML("beforeend", '<span class="qtag">queued</span>'); }
}
function addAssistant(text, ts) {
  ts = timeSep($("#messages"), ts);
  const d = document.createElement("div"); d.className = "msg assistant"; d.dataset.time = hm(ts);
  d.innerHTML = `<span class="astar">${STAR}</span><div class="body">${md(text)}<button class="say" title="Read out loud"><svg viewBox="0 0 24 24"><path d="M4 9h4l5-4v14l-5-4H4z M16 8.5a5 5 0 0 1 0 7 M18.8 6a8.5 8.5 0 0 1 0 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button></div>`;
  d.querySelector(".say").onclick = () => api.speak(d.querySelector(".body").innerText);
  d.querySelector(".body").insertAdjacentHTML("beforeend", `<div class="mtime">${hm(ts)}</div>`);
  $("#messages").appendChild(d); return d;
}
function addBrief(b) {
  if (!b || !b.goal) return;
  const d = document.createElement("div"); d.className = "brief";
  d.innerHTML = `<div class="bh">Task</div><div class="bg">${esc(b.goal)}</div>` +
    (b.tags && b.tags.length ? `<div class="tags">${b.tags.map(t => `<span>${esc(t)}</span>`).join("")}</div>` : "") +
    (b.plan && b.plan.length ? `<div class="plan">${b.plan.map((p, i) => `${i + 1}. ${esc(p)}`).join(" &nbsp; ")}</div>` : "");
  $("#messages").appendChild(d);
}
function stepsGroup(container = $("#messages")) {
  const last = container.lastElementChild;
  if (last && last.classList.contains("steps-group")) return last;
  const g = document.createElement("div"); g.className = "steps-group"; container.appendChild(g); return g;
}
function argSummary(args) {
  if (!args) return "";
  if (typeof args === "string") return args;
  if (args.url) return host(args.url);
  const v = args.question || args.name || args.app && args.to && `${args.app} → ${args.to}` || args.window || args.query || args.keys || args.to ||
    args.text || args.description || args.question || args.title || args.fact || args.task || args.path ||
    (args.element != null ? `[${args.element}]` : "") || Object.values(args)[0] || "";
  return String(v);
}
function addStep(label, args, status, container) {
  const s = document.createElement("div"); s.className = "step " + status;
  let a = ""; try { a = argSummary(args); } catch { a = ""; }
  s.innerHTML = `<span class="ind"></span><span class="lab">${esc(label)}</span><span class="arg">${esc(a)}</span>`;
  stepsGroup(container).appendChild(s); return s;
}
function setRunningUI(on) {
  $("#sendBtn").classList.toggle("hidden", on);
  $("#stopBtn").classList.toggle("hidden", !on);
}
function thinking(on, label) {
  if (live && live.thinkEl) { live.thinkEl.remove(); live.thinkEl = null; }
  if (on && live) {
    const t = document.createElement("div"); t.className = "thinking";
    t.innerHTML = `<span class="astar">${STAR}</span><span class="stars"><i></i><i></i><i></i></span><span>${esc(label || "")}</span>`;
    $("#messages").appendChild(t); live.thinkEl = t; scrollDown();
  }
}

/* ---------------- requests (approvals / questions) ---------------- */
function requestTarget(r) {
  const conv = r.conv || "";
  if (conv.startsWith("nova-task-")) {
    const card = taskCards[conv.slice(10)];
    if (card && card.el.isConnected) { card.el.classList.remove("collapsed"); return { box: card.body, view: "friend", scroll: $("#fthread") }; }
    return { box: $("#fmessages"), view: "friend", scroll: $("#fthread") };
  }
  if (conv === FRIEND) return { box: $("#fmessages"), view: "friend", scroll: $("#fthread") };
  return { box: $("#messages"), view: "chat", scroll: $("#thread") };
}
function addRequest(r) {
  if ($(`.request[data-id="${r.id}"]`)) return;
  const tgt = requestTarget(r);
  const d = document.createElement("div"); d.className = "request"; d.dataset.id = r.id;
  const details = r.details ? `<pre>${esc(r.details)}</pre>` : "";
  const turn = r.title.startsWith("Your turn");
  if (r.kind === "confirm") {
    d.innerHTML = `<div class="rtitle">${esc(r.title)}</div>${details}
      <div class="row"><button class="btn" data-a="yes">${turn ? "Done" : "Approve"}</button><button class="btn-ghost" data-a="no">${turn ? "Cancel" : "Decline"}</button></div>`;
    d.querySelector('[data-a="yes"]').onclick = () => api.answer(r.id, true);
    d.querySelector('[data-a="no"]').onclick = () => api.answer(r.id, false);
  } else {
    d.innerHTML = `<div class="rtitle">${esc(r.title)}</div>${details}
      <div class="row"><input placeholder="Your answer"><button class="btn">Send</button></div>`;
    const inp = d.querySelector("input");
    const go = () => { if (inp.value.trim()) api.answer(r.id, inp.value.trim()); };
    d.querySelector("button").onclick = go;
    inp.onkeydown = e => { if (e.key === "Enter") go(); };
    setTimeout(() => inp.focus(), 50);
  }
  if (tgt.view === "chat") { thinking(false); if (r.conv && current && r.conv !== current) return toast("Astra needs your OK in another task", { onclick: () => openConv(r.conv), ms: 8000 }); }
  tgt.box.appendChild(d);
  if (tgt.view === "chat") $("#empty").classList.add("hidden"); else $("#fempty").classList.add("hidden");
  scrollDown(true, tgt.scroll);
  if (!viewActive(tgt.view)) toast(tgt.view === "friend" ? "Astra needs your OK in Nova's chat" : "Astra needs your OK", { onclick: () => tgt.view === "chat" && r.conv ? openConv(r.conv) : showView(tgt.view), ms: 8000 });
}
function resolveRequest(id, answer) {
  const d = $(`.request[data-id="${id}"]`); if (!d) return;
  d.classList.add("done");
  d.querySelector(".row").innerHTML = `<span class="muted small">${answer === true ? "Approved" : answer === false || answer == null ? "Declined" : "Answered: " + esc(answer)}</span>`;
}

/* ---------------- sending (Astra) ---------------- */
const input = $("#input");
input.addEventListener("input", () => autosize(input));
input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
$("#sendBtn").onclick = send;
$("#stopBtn").onclick = () => current && api.stop(current);
$("#panicBtn").onclick = () => { api.panic(); toast("Stopped everything"); };
$$(".chip").forEach(c => c.onclick = () => { input.value = c.dataset.text; send(); });

const sideJobs = new Set();
async function send() {
  let text = input.value.trim();
  if (!text) return;
  closeSlash();
  const cmd = text.match(/^\/(\w+)\s*([\s\S]*)$/);
  if (cmd && COMMANDS[cmd[1].toLowerCase()] && COMMANDS[cmd[1].toLowerCase()].run) {
    const handled = await COMMANDS[cmd[1].toLowerCase()].run(cmd[2].trim());
    if (handled !== false) { input.value = ""; autosize(input); return; }
    text = input.value.trim();
  }
  burstFrom($("#sendBtn"));
  input.value = ""; autosize(input);
  $("#empty").classList.add("hidden");
  addUser(text);
  const res = await api.send_message(current, text, "astra");
  if (res && res.queued) { markQueued(); toast(`Queued. Runs right after the current task (${res.queued} waiting)`); return; }
  if (res && res.side) {   // Astra is busy here: this runs alongside as its own task
    const last = [...$$("#messages .msg.user")].pop(); if (last) last.remove();
    running.add(res.conv); sideJobs.add(res.conv); refreshConvs();
    toast("Astra's still on the other job, so this one runs alongside it. Click to watch.", { ms: 6500, onclick: () => openConv(res.conv) });
    return;
  }
  if (!current) current = res.conv;
  running.add(current);
  live = { text: "", bodyEl: null };
  thinking(true);
  setRunningUI(true); scrollDown(true); refreshConvs();
}

/* ---------------- Nova (one chat: Astra's jobs show up inside it) ---------------- */
const finput = $("#finput");
const noteCards = {};          // conv -> notes card
const taskCards = {};          // task id -> {el, body, step}
finput.addEventListener("input", () => autosize(finput));
finput.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); fsend(); } });
$("#fsendBtn").onclick = fsend;
$("#friendClear").onclick = async e => {
  if (!confirmInline(e.target, "Delete the chat?")) return;
  await api.friend_clear(); $("#fmessages").innerHTML = ""; $("#fempty").classList.remove("hidden");
};
let lastFrom = null, revealQ = Promise.resolve();

function friendName() { return (state && state.config.friend.name) || "Nova"; }
function friendInitial() { return friendName().trim().charAt(0).toUpperCase() || "N"; }
function fbubble(text, me, pop, ts) {
  $("#fempty").classList.add("hidden");
  const before = lastTs.get($("#fmessages"));
  ts = timeSep($("#fmessages"), ts);
  if (before !== undefined && ts - before > 30 * 60) lastFrom = null;
  const d = document.createElement("div");
  const from = me ? "me" : "them";
  d.className = "fmsg " + (me ? "me" : "") + (from !== lastFrom ? " first" : "") + (pop ? " pop" : "");
  d.innerHTML = (me ? "" : `<div class="orb"><span>${esc(friendInitial())}</span></div>`) + `<div class="fb">${linkify(esc(me ? text : nodash(text)))}</div><span class="ft">${hm(ts)}</span>`;
  $("#fmessages").appendChild(d); lastFrom = from;
  scrollDown(true, $("#fthread")); return d;
}
function splitTexts(t) { return String(t || "").split(/\n\s*\n/).map(x => x.trim()).filter(Boolean); }
function addF(text, me, ts) {
  if (me) return fbubble(text, true, false, ts);
  text = stripThink(text);
  let last = null; for (const p of splitTexts(text)) last = fbubble(p, false, false, ts); return last;
}
function addFNote(text, ts) {
  const d = document.createElement("div"); d.className = "fmsg note"; d.textContent = text;
  if (ts) d.title = whenLabel(ts);
  $("#fmessages").appendChild(d); lastFrom = null; scrollDown(true, $("#fthread"));
}
function revealTexts(text) {
  // Nova's texts arrive one after another, with a little typing pause between them, like a person
  const parts = splitTexts(stripThink(text));
  revealQ = revealQ.then(async () => {
    for (let i = 0; i < parts.length; i++) {
      if (i) { ftyping(true); await new Promise(r => setTimeout(r, Math.min(1400, 350 + parts[i].length * 18))); }
      ftyping(false); fbubble(parts[i], false, true);
    }
  });
  return revealQ;
}
function ftyping(on) {
  if (flive && flive.typingEl) { flive.typingEl.remove(); flive.typingEl = null; }
  if (on) {
    flive = flive || {};
    const d = document.createElement("div"); d.className = "fmsg" + (lastFrom === "them" ? "" : " first");
    d.innerHTML = `<div class="orb"><span>${esc(friendInitial())}</span></div><div class="fb typing"><i></i><i></i><i></i></div>`;
    if (lastFrom !== "them") d.querySelector(".orb").style.visibility = "visible";
    $("#fmessages").appendChild(d); flive.typingEl = d; scrollDown(true, $("#fthread"));
  }
}
function setFriendStatus() {
  const busy = Object.values(taskCards).some(c => c.el.classList.contains("running"));
  $("#friendStatus").textContent = running.has(FRIEND) ? "typing" : busy ? "astra is working on something" : "here";
}

/* Astra's job card */
function taskCard(tid, text, status = "running", steps = [], result = "") {
  let c = taskCards[tid];
  if (c && c.el.isConnected) { if (text) c.el.querySelector(".tt span").textContent = text; return c; }
  const el = document.createElement("div");
  el.className = "task " + status + (status === "running" ? "" : " collapsed");
  el.innerHTML = `<div class="th" role="button" tabindex="0" title="Show what Astra did"><span class="tic">${STAR}</span><div class="tt"><b>Astra</b><span>${esc(text)}</span></div>
    <span class="tstate"></span><button class="btn-ghost sm tstop hidden">Stop</button><span class="tchev"><svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></span></div>
    <div class="tbody"><div class="tsteps"></div><div class="tres hidden"></div></div>`;
  timeSep($("#fmessages"));
  $("#fmessages").appendChild(el); lastFrom = null;
  c = { el, body: el.querySelector(".tsteps"), res: el.querySelector(".tres"), step: null };
  const toggle = e => { if (!e.target.closest("button")) { el.classList.toggle("collapsed"); if (!el.classList.contains("collapsed")) fillTask(tid); } };
  el.querySelector(".th").onclick = toggle;
  el.querySelector(".th").onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(e); } };
  el.querySelector(".tstop").onclick = () => api.stop_task(tid);
  taskCards[tid] = c;
  steps.forEach(st => addStep(st.label, st.args, "ok", c.body));
  if (result) setTaskResult(tid, result);
  setTaskState(tid, status, steps.length);
  return c;
}
function setTaskResult(tid, text) {
  const c = taskCards[tid]; if (!c || !text) return;
  c.res.classList.remove("hidden");
  c.res.innerHTML = `<div class="trh">Astra's report</div>` + md(text);
}
async function fillTask(tid) {   // load steps and the report when a card is opened (older jobs)
  const c = taskCards[tid]; if (!c || c.filled) return;
  c.filled = true;
  try {
    const d = await api.task_details(tid);
    if (!c.body.querySelector(".step") && d.steps && d.steps.length) d.steps.forEach(st => addStep(st.label, st.args, "ok", c.body));
    if (d.result) setTaskResult(tid, d.result);
    if (!c.body.querySelector(".step") && !d.result) c.body.innerHTML = `<p class="muted small">No details saved for this one.</p>`;
  } catch { c.filled = false; }
}
function setTaskState(tid, status, n) {
  const c = taskCards[tid]; if (!c) return;
  const collapsed = c.el.classList.contains("collapsed");
  c.el.className = "task " + status + (collapsed && status !== "running" ? " collapsed" : "");
  n = n ?? c.body.querySelectorAll(".step").length;
  const label = { running: "working", done: "done", failed: "didn't work", stopped: "stopped", interrupted: "interrupted" }[status] || status;
  c.el.querySelector(".tstate").textContent = `${label} · ${n} step${n === 1 ? "" : "s"}`;
  c.el.querySelector(".tstop").classList.toggle("hidden", status !== "running");
  setFriendStatus();
}
const taskHandlers = {
  phase(e) { const c = taskCards[e.task]; if (c) c.el.querySelector(".tstate").textContent = e.text; },
  tool_start(e) { const c = taskCard(e.task, ""); c.step = addStep(e.label, e.args, "run", c.body); setTaskState(e.task, "running"); scrollDown(false, $("#fthread")); },
  tool_end(e) { const c = taskCards[e.task]; if (!c || !c.step) return; c.step.className = "step " + (e.ok ? "ok" : "fail"); c.step.title = e.preview || ""; c.step = null; },
  browser_peek(e) { const c = taskCards[e.task]; if (c) showPeek(c.body, e); },
  notes(e) { const c = taskCards[e.task]; if (c) { notesEvent(e, c.body); scrollDown(false, $("#fthread")); } },
};

async function loadFriend() {
  $("#friendName").textContent = friendName();
  $("#friendAvatar").innerHTML = `<span>${esc(friendInitial())}</span>`; $("#fbigInitial").textContent = friendInitial();
  $("#navFriend").textContent = friendName();
  finput.placeholder = `Message ${friendName()}`;
  const { items, running: isRunning, tasks_running: tr } = await api.friend_history();
  $("#fmessages").innerHTML = ""; lastFrom = null; lastTs.delete($("#fmessages"));
  for (const k in taskCards) delete taskCards[k];
  $("#fempty").classList.toggle("hidden", items.length > 0);
  for (const it of items) {
    if (it.role === "user") addF(it.text, true, it.ts);
    else if (it.role === "assistant") addF(it.text, false, it.ts);
    else if (it.role === "note") addFNote(it.text, it.ts);
    else if (it.role === "task") taskCard(it.id, it.task, (tr || []).includes(it.id) ? "running" : it.status, it.steps || [], it.result || "");
  }
  for (const tid of tr || []) {
    api.task_details(tid).then(d => { const c = taskCards[tid]; if (c && d.notes && d.notes.length) { const nc = ensureNotes("nova-task-" + tid, c.body, true); d.notes.forEach(n => nc.item(n.item, host(n.source || ""))); } });
  }
  (state.requests || []).filter(r => r.conv === FRIEND || (r.conv || "").startsWith("nova-task-")).forEach(addRequest);
  if (isRunning) { running.add(FRIEND); ftyping(true); }
  setFriendStatus(); scrollDown(true, $("#fthread"));
}
async function fsend() {
  const text = finput.value.trim();
  if (!text) return;
  burstFrom($("#fsendBtn"));
  finput.value = ""; autosize(finput);
  addF(text, true);
  const res = await api.send_message(null, text, "friend");
  if (res && res.queued) return;
  running.add(FRIEND); flive = {}; ftyping(true); setFriendStatus();
}
const friendHandlers = {
  typing() { ftyping(true); },
  assistant_start() { ftyping(true); $("#friendStatus").textContent = "typing"; },
  token() {},
  tool_start(e) { if (e.name !== "hand_to_astra") ftyping(true); },
  tool_end() {},
  task_start(e) { ftyping(false); taskCard(e.task, e.text, "running"); setFriendStatus(); scrollDown(true, $("#fthread")); },
  task_done(e) {
    const c = taskCards[e.task];
    if (c) { setTaskState(e.task, e.status); if (e.text) setTaskResult(e.task, e.text); if (e.status === "done") c.el.classList.add("collapsed"); }
    if (e.status !== "stopped") ftyping(true);
  },
  done(e) {
    running.delete(FRIEND); ftyping(false);
    if (e.text) revealTexts(e.text).then(setFriendStatus); else setFriendStatus();
    flive = null;
    if (!viewActive("friend")) $("#friendDot").classList.remove("hidden");
  },
  event_note(e) { revealQ = revealQ.then(() => addFNote(e.text, Date.now() / 1000)); },
  nova_checkin(e) { if (!viewActive("friend")) toast(`${e.name}: ${e.text}`, { ms: 9000, onclick: () => showView("friend") }); },
  error(e) { toast(e.text.slice(0, 140)); },
};

/* browser peek + research notes (Astra chat and job cards) */
function showPeek(container, e) {
  if (state.config.ui && state.config.ui.peek === false) return;
  let p = container.querySelector(":scope > .peek");
  if (!p) { p = document.createElement("div"); p.className = "peek"; p.innerHTML = `<img alt="What Astra's browser sees"><div class="purl"></div>`; }
  container.appendChild(p);
  p.querySelector("img").src = e.img; p.querySelector(".purl").textContent = host(e.url);
}
function ensureNotes(conv, container, quiet) {
  if (noteCards[conv] && noteCards[conv].el.isConnected) return noteCards[conv];
  const el = document.createElement("div"); el.className = "notes";
  el.innerHTML = `<div class="nh"><b>Notes</b><small>0 items</small></div><div class="nsrc hidden"></div><ul></ul><div class="nfoot hidden"></div>`;
  container.appendChild(el);
  const ul = el.querySelector("ul"), src = el.querySelector(".nsrc"), count = el.querySelector("small");
  let n = 0;
  const c = {
    el,
    start(topic) { el.querySelector("b").textContent = topic ? `Notes: ${topic}` : "Notes"; },
    status(text) { count.textContent = text; },
    source(url, title, st) {
      src.classList.remove("hidden");
      let x = [...src.children].find(z => z.dataset.u === url);
      if (!x) { x = document.createElement("span"); x.dataset.u = url; src.appendChild(x); }
      x.className = st === "reading" ? "reading" : ""; x.textContent = `${host(url)}${st === "reading" ? " (reading)" : st ? ", " + st : ""}`;
    },
    item(it, from) {
      n++; count.textContent = `${n} item${n === 1 ? "" : "s"}`;
      const li = document.createElement("li");
      const fields = Object.entries(it).filter(([k, v]) => !["name", "link", "notes"].includes(k) && v);
      li.innerHTML = `<div class="nt">${esc(it.name)}</div>` +
        (fields.length ? `<div class="nf">${fields.map(([k, v]) => `<em>${esc(k.replace(/_/g, " "))}:</em> ${esc(v)}`).join("&ensp;")}</div>` : "") +
        (it.notes ? `<div class="nf">${linkify(esc(it.notes))}</div>` : "") +
        (it.link ? `<div class="nf"><a href="${esc(it.link)}">${esc(host(it.link))}</a></div>` : "");
      ul.appendChild(li);
    },
    finish(total, path) {
      count.textContent = `${total} item${total === 1 ? "" : "s"}`;
      if (path) { const f = el.querySelector(".nfoot"); f.classList.remove("hidden"); f.innerHTML = `Saved to ${linkify(esc(path))}`; }
    },
  };
  if (!quiet) c.status("Starting");
  noteCards[conv] = c; return c;
}
function notesEvent(e, container) {
  const c = ensureNotes(e.conv, container, e.action !== "start");
  if (e.action === "start") c.start(e.topic);
  else if (e.action === "status") c.status(e.text);
  else if (e.action === "source") c.source(e.url, e.title, e.state);
  else if (e.action === "item") c.item(e.item, e.source);
  else if (e.action === "done") c.finish(e.count, e.path);
}

/* ---------------- events from Python ---------------- */
const handlers = {
  phase(e) { if (e.conv === current) { live = live || {}; thinking(true, e.text); } },
  brief(e) { if (e.conv !== current) return; thinking(false); addBrief(e); thinking(true); scrollDown(); },
  retract(e) {
    if (e.conv !== current || !live || !live.bodyEl) return;
    live.bodyEl.parentElement.remove(); live.bodyEl = null; live.text = "";
    thinking(true, "Double-checking links");
  },
  assistant_start(e) { if (window.Horizon) Horizon.pulse(); if (e.conv !== current) return; live = live || {}; live.text = ""; live.bodyEl = null; thinking(true); },
  token(e) {
    if (e.conv !== current || !live) return;
    if (!live.bodyEl) { thinking(false); const m = addAssistant(""); live.bodyEl = m.querySelector(".body"); live.bodyEl.classList.add("cursor"); }
    live.text += e.text;
    const say = live.bodyEl.querySelector(".say");
    live.bodyEl.innerHTML = md(stripThink(live.text));
    if (say) live.bodyEl.appendChild(say);
    scrollDown();
  },
  thinking() {},
  tool_start(e) {
    if (e.conv !== current) return;
    if (live && live.bodyEl) { live.bodyEl.classList.remove("cursor"); if (!live.text.trim()) live.bodyEl.parentElement.remove(); live.bodyEl = null; }
    thinking(false);
    live = live || {}; live.step = addStep(e.label, e.args, "run"); scrollDown();
  },
  tool_end(e) {
    if (e.conv !== current || !live || !live.step) return;
    live.step.className = "step " + (e.ok ? "ok" : "fail"); live.step.title = e.preview || ""; live.step = null;
    thinking(true);
  },
  done(e) {
    running.delete(e.conv);
    if (e.conv !== current && sideJobs.has(e.conv)) {
      sideJobs.delete(e.conv);
      toast("Finished in the background: " + String(e.text || "").replace(/\s+/g, " ").slice(0, 90), { ms: 9000, onclick: () => openConv(e.conv) });
    }
    if (e.conv === current) {
      thinking(false);
      if (live && live.bodyEl) {
        live.bodyEl.classList.remove("cursor");
        if (e.text) {   // the final, cleaned text replaces what streamed in (never any model thinking)
          const say = live.bodyEl.querySelector(".say"), t = live.bodyEl.querySelector(".mtime");
          live.bodyEl.innerHTML = md(e.text); if (say) live.bodyEl.appendChild(say); if (t) live.bodyEl.appendChild(t);
        }
      }
      else if (e.text && (!live || !live.text)) addAssistant(e.text);
      live = null; setRunningUI(false); scrollDown();
    }
    refreshConvs();
  },
  error(e) { if (e.conv === current) toast(e.text.slice(0, 140)); },
  request(e) { addRequest(e); },
  request_done(e) { resolveRequest(e.id, e.answer); },
  conv_title() { refreshConvs(); },
  conv_list_changed() { api.running_list().then(ids => { running.forEach(id => { if (!ids.includes(id) && id !== FRIEND) running.delete(id); }); ids.forEach(id => running.add(id)); refreshConvs(); }); },
  engine(e) { state.engine_up = e.up; setEngine(e.up ? (e.warm ? "ready" : "loading") : "off"); },
  notify(e) { toast(e.text); },
  model_too_big(e) {
    toast(e.text, { ms: 15000, onclick: async () => {
      const r = await api.use_best_model(); state.config = r.config;
      toast(`Switching to ${r.picked.model}. It downloads once, then replies get much quicker.`, { ms: 6000 });
    } });
  },
  update_ready(e) {
    state.update = { status: "ready", version: e.version }; renderUpdate();
    toast(`AstraNova ${e.version} is ready. It goes in when you close the app, or click to restart now.`, { ms: 9000, onclick: () => api.update_restart() });
  },
  panic() { toast("Stopped everything (Ctrl+Alt+X)"); },
  queue(e) {
    if (e.conv !== current || !e.started) return;
    const q = $("#messages .msg.user.queued");
    if (q) { q.classList.remove("queued"); const t = q.querySelector(".qtag"); if (t) t.remove(); $("#messages").appendChild(q); }
    running.add(e.conv); live = { text: "" }; thinking(true); setRunningUI(true);
  },
  mood(e) { setMood(e.mood); },
  context(e) { if (e.conv === current) thinking(true, `Using a bigger memory for this (${Math.round(e.tokens / 1024)}K)`); },
  browser_peek(e) { if (e.conv === current) { showPeek($("#messages"), e); if (live && live.thinkEl) $("#messages").appendChild(live.thinkEl); scrollDown(); } },
  notes(e) { if (e.conv === current) { notesEvent(e, $("#messages")); if (live && live.thinkEl) $("#messages").appendChild(live.thinkEl); scrollDown(); } },
  calendar_changed() { if (viewActive("calendar")) loadWeek(); },
  phone_status(e) { renderPhone(e); },
  discord_account(e) { renderDA(e); },
  telegram_status(e) { renderTG(e); },
  context_imported(e) { $("#ctxStatus").textContent = e.text; toast(e.text); api.get_context().then(t => $("#ctxText").value = t); },
  context_changed() { if ($("#tab-context").classList.contains("active")) api.get_context().then(t => $("#ctxText").value = t); },
  routines_changed() { if ($("#view-routines").classList.contains("active")) loadRoutines(); },
  memory_changed() { if ($("#view-memory").classList.contains("active")) loadMemory(); },
  discord_status(e) { renderDiscord(e); },
  spotify(e) {
    $("#spConnect").disabled = false; $("#spConnect").textContent = "Connect";
    if (e.ok) { toast(`Spotify connected as ${e.user}`); renderSpotify({ connected: true, user: e.user }); }
    else toast("Spotify: " + e.error);
  },
  setup: setupEvent,
  setup_complete() {
    $$(".sstep").forEach(s => s.className = "sstep done");
    setTimeout(() => { $("#setup").classList.add("hidden"); loadState().then(() => showWelcome(true)); setEngine("ready"); }, 700);
  },
  setup_error(e) { $("#setupError").textContent = e.text; $("#setupError").classList.remove("hidden"); $("#setupStart").disabled = false; $("#setupStart").textContent = "Try again"; },
  model_progress(e) { const p = $("#modelProgress"); p.classList.remove("hidden"); bar(p, e.progress); },
  model_ready(e) { $("#modelProgress").classList.add("hidden"); toast(`${e.name} ready`); loadState().then(renderModels); },
  model_error(e) { $("#modelProgress").classList.add("hidden"); toast("Model download failed: " + e.text); },
};
window.astra = {
  onEvent: e => {
    try {
      const conv = String(e.conv || "");
      if (conv.startsWith("nova-task-")) {
        e.task = e.task || conv.slice(10);
        if (taskHandlers[e.type]) return taskHandlers[e.type](e);
        if (["request", "request_done"].includes(e.type)) return handlers[e.type](e);
        return;
      }
      if (e.type === "mood") return handlers.mood(e);
      if (conv === FRIEND && friendHandlers[e.type]) return friendHandlers[e.type](e);
      if (conv === FRIEND && ["conv_title", "phase", "context"].includes(e.type)) return;
      (handlers[e.type] || (() => {}))(e);
    } catch (err) { console.error(err); }
  },
};

function setEngine(s) {
  const dot = $("#engineDot"), txt = $("#engineText");
  dot.className = "dot " + (s === "ready" ? "on" : s === "loading" ? "wait" : "");
  const m = state?.config?.model || {};
  const cloud = m.provider === "openai";
  const name = cloud ? m.api_model : m.name;
  txt.textContent = s === "ready" ? `${name || ""} · ${cloud ? "cloud" : "local"}` : s === "loading" ? "Loading model" : "Engine offline";
}

/* ---------------- setup ---------------- */
function setupEvent(e) {
  const steps = ["engine", "model", "vision", "browser"];
  const idx = steps.indexOf(e.step);
  $$(".sstep").forEach((s, i) => {
    if (i < idx) s.className = "sstep done";
    else if (i === idx) { s.className = "sstep " + (e.done ? "done" : "run"); s.querySelector("em").textContent = e.status + (e.progress != null && !e.done ? ` ${Math.round(e.progress * 100)}%` : ""); }
  });
  bar($("#setupBar"), e.done ? 1 : e.progress);
}
function startSetup() {
  $("#setupStart").disabled = true; $("#setupStart").textContent = "Installing";
  $("#setupError").classList.add("hidden"); bar($("#setupBar"), null);
  api.run_setup();
}
$("#setupStart").onclick = startSetup;
$("#sCloudGo").onclick = async () => {
  const key = $("#sCloudKey").value.trim();
  if (!key && !/127\.0\.0\.1|localhost/.test($("#sCloudBase").value)) return toast("Paste your API key");
  await api.save_settings("model", { provider: "openai", api_base: $("#sCloudBase").value.trim(), api_model: $("#sCloudModel").value.trim() });
  if (key) await api.set_secret("api_key", key);
  await loadState(); startSetup();
};

/* ---------------- routines ---------------- */
const repeatLabel = { once: "once", daily: "every day", weekdays: "weekdays", weekly: "every week", hourly: "every hour" };
async function loadRoutines() {
  const rows = await api.list_routines();
  const el = $("#routineList");
  if (!rows.length) { el.innerHTML = `<p class="muted">No routines yet.</p>`; return; }
  el.innerHTML = rows.map(r => `
    <div class="item" data-id="${r.id}">
      <div class="it"><h3>${esc(r.title)}</h3>
        <div class="meta">${r.enabled ? "Next " + esc(fmtWhen(r.next_run)) : "Off"} · ${esc(repeatLabel[r.repeat] || r.repeat)}${r.last_run ? " · last ran " + esc(fmtWhen(r.last_run)) : ""}</div>
        <div class="meta">${esc(r.prompt)}</div>
        ${r.last_result ? `<div class="meta">→ ${esc(r.last_result.slice(0, 160))}</div>` : ""}</div>
      <div class="actions">
        <label class="toggle"><input type="checkbox" ${r.enabled ? "checked" : ""} data-act="toggle"><span></span></label>
        <button class="btn-ghost sm" data-act="run">Run now</button>
        <button class="btn-ghost sm" data-act="edit">Edit</button>
        <button class="btn-ghost sm" data-act="del">Delete</button>
      </div>
    </div>`).join("");
  $$(".item", el).forEach(card => {
    const id = +card.dataset.id, r = rows.find(x => x.id === id);
    card.querySelector('[data-act="toggle"]').onchange = e => api.toggle_routine(id, e.target.checked);
    card.querySelector('[data-act="run"]').onclick = () => { api.run_routine(id); toast("Running"); };
    card.querySelector('[data-act="del"]').onclick = async () => { await api.delete_routine(id); loadRoutines(); };
    card.querySelector('[data-act="edit"]').onclick = () => editRoutine(r);
  });
}
function editRoutine(r) {
  $("#routineForm").classList.remove("hidden");
  $("#rId").value = r ? r.id : ""; $("#rTitle").value = r ? r.title : ""; $("#rPrompt").value = r ? r.prompt : "";
  $("#rWhen").value = r ? new Date(r.next_run * 1000).toISOString().slice(0, 16).replace("T", " ") : "";
  $("#rRepeat").value = r ? r.repeat : "once"; $("#rPrompt").focus();
}
$("#addRoutine").onclick = () => editRoutine(null);
$("#rCancel").onclick = () => $("#routineForm").classList.add("hidden");
$("#rSave").onclick = async () => {
  let when = $("#rWhen").value.trim();
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(when) && $("#rId").value) {
    const d = new Date(when.replace(" ", "T") + "Z"); // value came from toISOString (UTC) -> convert to local
    const p = n => String(n).padStart(2, "0");
    when = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  const res = await api.save_routine({ id: $("#rId").value || null, title: $("#rTitle").value, prompt: $("#rPrompt").value, when, repeat: $("#rRepeat").value });
  if (res.error) return toast(res.error);
  $("#routineForm").classList.add("hidden"); toast("Saved"); loadRoutines();
};

/* ---------------- memory ---------------- */
const KEEP_LABEL = { "": "deciding", day: "1 day", week: "1 week", month: "1 month", season: "3 months", year: "1 year", until: "until date", forever: "forever" };
function keepLeft(m) {
  if (!m.expires) return m.keep === "" || m.keep == null ? "deciding" : "forever";
  const d = (m.expires * 1000 - Date.now()) / DAY;
  return d < 1 ? "today" : d < 2 ? "1 day left" : `${Math.round(d)} days left`;
}
function renderMemory(rows) {
  const src = { auto: "auto", friend: "nova", astra: "astra", you: "you" };
  $("#memList").innerHTML = rows.length ? rows.map(m => `<div class="mem"><span class="ms">${esc(src[m.source] || m.source)}</span><span class="mt" contenteditable="true" spellcheck="false" data-id="${m.id}">${esc(m.text)}</span>
    <label class="mkeep" title="How long Astra and Nova keep this"><span>${esc(keepLeft(m))}</span><select data-id="${m.id}">${["day", "week", "month", "season", "year", "forever"].map(k => `<option value="${k}" ${m.keep === k ? "selected" : ""}>${KEEP_LABEL[k]}</option>`).join("")}${m.keep === "until" ? `<option value="until" selected>until date</option>` : ""}${!m.keep ? `<option value="" selected>let them decide</option>` : ""}</select></label>
    <button class="x" data-id="${m.id}" title="Forget">×</button></div>`).join("")
    : `<p class="muted">Nothing yet. Astra and Nova save things as you talk, or add them here.</p>`;
  $$("#memList .x").forEach(b => b.onclick = async () => renderMemory(await api.delete_memory(+b.dataset.id)));
  $$("#memList .mkeep select").forEach(sel => sel.onchange = async () => { if (sel.value) renderMemory(await api.set_memory_keep(+sel.dataset.id, sel.value)); });
  $$("#memList .mt").forEach(t => {
    const orig = t.textContent;
    t.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); t.blur(); } if (e.key === "Escape") { t.textContent = orig; t.blur(); } };
    t.onblur = async () => { const v = t.textContent.trim(); if (v && v !== orig) { await api.edit_memory(+t.dataset.id, v); toast("Memory updated"); } };
  });
}
async function loadMemory() { renderMemory(await api.list_memories()); }
$("#memAdd").onclick = async () => { const v = $("#memInput").value.trim(); if (!v) return; $("#memInput").value = ""; renderMemory(await api.add_memory(v)); };
$("#memInput").onkeydown = e => { if (e.key === "Enter") $("#memAdd").click(); };

/* ---------------- activity ---------------- */
async function loadActivity() {
  const rows = await api.activity();
  $("#activityList").innerHTML = rows.length ? rows.map(r => `<div class="entry"><span class="ts">${fmtTime(r.ts)}</span><span class="k">${esc(r.kind)}</span><span class="tx">${esc(r.text)}</span></div>`).join("")
    : `<p class="muted">Nothing yet.</p>`;
}

/* ---------------- settings ---------------- */
$$(".tab").forEach(t => t.onclick = () => {
  $$(".tab").forEach(x => x.classList.toggle("active", x === t));
  $$(".tabpane").forEach(p => p.classList.toggle("active", p.id === "tab-" + t.dataset.tab));
  if (t.dataset.tab === "apps") fillApps();
  if (t.dataset.tab === "discord") api.discord_status().then(renderDiscord);
  if (t.dataset.tab === "context") api.get_context().then(x => $("#ctxText").value = x);
  if (t.dataset.tab === "voice") fillVoice();
  if (t.dataset.tab === "accounts") fillAccounts();
  if (t.dataset.tab === "dcacct") fillDA();
  if (t.dataset.tab === "telegram") api.telegram_status().then(renderTG);
  if (t.dataset.tab === "phone") api.phone_status().then(renderPhone);
  if (t.dataset.tab === "model") api.local_models().then(renderLocalModels);
  if (t.dataset.tab === "friendset" || t.dataset.tab === "look") renderLearned();
  if (t.dataset.tab === "about") {
    $("#aboutVer").textContent = "Version " + (state.version || "") + (state.public ? " · public edition" : "");
    $("#updBox").classList.toggle("hidden", !state.public);
    if (state.public) api.update_status().then(u => { state.update = u; renderUpdate(); });
  }
  if (t.dataset.tab === "email") loadMailLog();
});
const save = async (section, values) => { state.config = await api.save_settings(section, values); return state.config; };
const bindToggle = (sec, key, el, conv = v => v) => el.onchange = () => save(sec, { [key]: conv(el.type === "checkbox" ? el.checked : el.value) });

function fillSettings() {
  const c = state.config;
  $$("[data-p]").forEach(i => i.value = c.profile[i.dataset.p] || "");
  $("#aStyle").value = c.assistant.style || "concise";
  // model
  setProvider(c.model.provider || "ollama", false);
  $("#mThink").checked = !!c.model.think; $("#mKeep").value = c.model.keep_alive || "10m"; $("#mLight").value = c.model.light || "auto"; $("#mCtx").value = String(c.model.num_ctx);
  $("#mMaxCtx").value = c.model.auto_ctx === false ? "0" : String(c.model.max_ctx || 49152);
  $("#cBase").value = c.model.api_base || ""; $("#cModel").value = c.model.api_model || ""; $("#cVision").value = c.model.api_vision_model || "";
  const preset = [...$("#cPreset").options].find(o => o.value === c.model.api_base);
  $("#cPreset").value = preset ? preset.value : "custom";
  $("#cKey").placeholder = c.secrets.api_key ? "saved - paste a new one to replace" : "sk-...";
  renderModels();
  // appearance, privacy, safety extras
  const ap = c.appearance || {};
  $$("#bgSeg button").forEach(b => b.classList.toggle("on", b.dataset.bg === (ap.background || "light")));
  $("#moodAccent").checked = !!ap.mood_accent; renderSwatches(); renderMoodMap();
  $("#lWelcome").checked = (c.ui || {}).welcome !== false; $("#lPeek").checked = (c.ui || {}).peek !== false;
  $("#lMotion").checked = localStorage.getItem("astra-calm") === "1";
  $("#privExtra").value = (c.privacy || {}).extra_private || "";
  $("#sBg").checked = c.control.background_mode !== false; $("#sDisk").checked = !!c.control.full_disk;
  $("#tgOwner").value = c.telegram.owner_username || "";
  // friend
  const f = c.friend;
  $("#fName").value = f.name || ""; $("#fPersona").value = f.personality || ""; $("#fHandoff").checked = !!f.can_hand_off;
  $("#fGenz").checked = (f.texting || "genz") === "genz"; $("#fMemory").checked = f.auto_memory !== false; $("#fEvents").checked = f.auto_events !== false;
  $("#fProactive").checked = !!f.proactive; $("#fLevel").value = f.proactive_level || "normal";
  $("#fQuietA").value = f.quiet_start || "23:00"; $("#fQuietB").value = f.quiet_end || "09:00"; $("#fDiscord").checked = !!f.proactive_discord;
  // safety
  $("#sControl").checked = !!c.control.enabled; $("#sMsg").checked = !!c.control.confirm_messages; $("#sCmd").checked = !!c.control.confirm_commands;
  $("#sFiles").checked = !!c.control.confirm_files; $("#sHotkey").checked = !!c.control.stop_hotkey; $("#sBlocked").value = c.control.blocked_apps || "";
  $("#sEmail").checked = !!c.approvals.send_email; $("#bHeadless").checked = !!c.browser.headless;
  $("#bCookies").value = c.browser.cookies || "decline"; $("#bExt").value = c.browser.extensions_dir || "";
  // email
  renderMailAccounts();
  const m = c.mail;
  $("#mEvery").value = String(m.check_minutes || 10); $("#mNotify").checked = m.notify_important !== false; $("#mAuto").checked = !!m.auto_reply;
  $("#mAutoText").value = m.auto_reply_text || ""; $("#mBlacklist").value = m.blacklist || "";
  const fl = m.filter || {}, hide = fl.hide || ["security", "account", "shopping", "promotion", "newsletter", "social", "spam"];
  $("#fOnly").checked = fl.only_important !== false; $("#fAlways").value = fl.always || ""; $("#fNever").value = fl.never || "";
  const KINDS = { security: "Security alerts (new sign-ins, codes)", account: "Account notices", promotion: "Ads and sales",
    newsletter: "Newsletters", social: "Social media notifications", shopping: "Orders and shipping", spam: "Spam", other: "Everything else unsorted" };
  $("#fHide").innerHTML = Object.entries(KINDS).map(([k, l]) => `<label class="chipbox"><input type="checkbox" value="${k}" ${hide.includes(k) ? "checked" : ""}><span>${l}</span></label>`).join("");
  // calendar + phone
  renderCalSources(); $("#calRemind").value = String(c.calendar.remind_minutes || 30);
  $("#phOn").checked = !!c.phone.enabled; $("#phTunnel").checked = c.phone.tunnel !== false; $("#phLan").checked = !!c.phone.lan;
  // discord
  const d = c.discord;
  $("#dcEnabled").checked = !!d.enabled; $("#dcApprovals").checked = !!d.approvals_to_dm; $("#dcOwnerName").value = d.owner_username || "";
  $("#dcUpdates").checked = !!d.task_updates; $("#dcServers").checked = !!d.reply_in_servers; $("#dcOthers").value = d.others_mode === "off" ? "off" : "friend";
  $("#dcToken").placeholder = c.secrets.discord_token ? "saved - paste a new one to replace" : "paste token";
  if ($("#tab-apps").classList.contains("active")) fillApps();
}

$("#saveProfile").onclick = async () => {
  const p = {}; $$("[data-p]").forEach(i => p[i.dataset.p] = i.value.trim());
  await save("profile", p); await save("assistant", { style: $("#aStyle").value });
  await save("privacy", { extra_private: $("#privExtra").value.trim() }); setGreeting(); toast("Saved");
};
$("#openWorkspace").onclick = () => api.open_workspace();
$("#openData").onclick = () => api.open_data_folder();

// model
function setProvider(p, persist = true) {
  $$("#providerSeg button").forEach(b => b.classList.toggle("on", b.dataset.prov === p));
  $("#localModel").classList.toggle("hidden", p !== "ollama");
  $("#cloudModel").classList.toggle("hidden", p !== "openai");
  if (persist) save("model", { provider: p }).then(() => setEngine("loading"));
}
$$("#providerSeg button").forEach(b => b.onclick = () => setProvider(b.dataset.prov));
function modelRow(m, cur, kind) {
  const have = (state.installed || []).some(n => n === m.name || n === m.name + ":latest");
  const v = (state.gpu || {}).vram_gb || 0, rec = state.recommended || {};
  const best = m.name === (kind === "model" ? rec.model : rec.vision);
  const tag = best ? `<em class="mtag ok">best for your GPU</em>` : v && m.min_vram > v + .5 ? `<em class="mtag warn">too big for ${Math.round(v)} GB</em>` : "";
  return `<div class="model ${m.name === cur ? "sel" : ""}" data-name="${m.name}" data-kind="${kind}"><span class="radio"></span>
    <div class="mi"><b>${esc(m.label)} ${tag}</b><small>${esc(m.name)} · ${esc(m.note)}</small></div><span class="sz">${esc(m.size)}<br>${have ? "installed" : "download"}</span></div>`;
}
function renderGpu() {
  const g = state.gpu || {}, r = state.recommended || {};
  $("#gpuHead").textContent = g.name ? `${g.name} · ${g.vram_gb} GB` : "Couldn't read your graphics card";
  $("#gpuSub").textContent = g.name ? `Best fit: ${r.model} with ${Math.round(r.ctx / 1024)}K context, ${r.vision} for screen vision.`
    : `Using settings that work on 8 GB cards. Pick a bigger model below if yours has more memory.`;
}
$("#mBest").onclick = async () => {
  const r = await api.use_best_model(); state.config = r.config; renderModels(); fillModelCtx();
  toast(`Switching to ${r.picked.model}${(state.installed || []).includes(r.picked.model) ? "" : " (downloading)"}`);
};
function fillModelCtx() { const c = state.config.model; $("#mCtx").value = c.num_ctx; $("#mMaxCtx").value = c.auto_ctx === false ? 0 : c.max_ctx; }
function renderModels() {
  const c = state.config.model; renderGpu();
  $("#modelList").innerHTML = state.models.map(m => modelRow(m, c.name, "model")).join("");
  $("#visionList").innerHTML = state.vision_models.map(m => modelRow(m, c.vision, "vision")).join("");
  $$(".model").forEach(m => m.onclick = () => {
    const cur = m.dataset.kind === "model" ? state.config.model.name : state.config.model.vision;
    if (m.dataset.name === cur && (state.installed || []).includes(cur)) return;
    toast("Getting " + m.dataset.name); api.pull_model(m.dataset.name, m.dataset.kind);
  });
}
bindToggle("model", "think", $("#mThink"));
bindToggle("model", "keep_alive", $("#mKeep"));
bindToggle("model", "light", $("#mLight"));
bindToggle("model", "num_ctx", $("#mCtx"), v => +v);
$("#mMaxCtx").onchange = () => { const v = +$("#mMaxCtx").value; save("model", v ? { auto_ctx: true, max_ctx: v } : { auto_ctx: false }); };
$("#lWelcome").onchange = () => save("ui", { welcome: $("#lWelcome").checked });
$("#lPeek").onchange = () => save("ui", { peek: $("#lPeek").checked });
$("#lMotion").onchange = () => setCalm($("#lMotion").checked);
$("#cPreset").onchange = () => { if ($("#cPreset").value !== "custom") $("#cBase").value = $("#cPreset").value; };
$("#cSave").onclick = async () => {
  await save("model", { provider: "openai", api_base: $("#cBase").value.trim(), api_model: $("#cModel").value.trim(), api_vision_model: $("#cVision").value.trim() });
  if ($("#cKey").value.trim()) { await api.set_secret("api_key", $("#cKey").value.trim()); $("#cKey").value = ""; }
  await loadState(); fillSettings(); toast("Saved"); setEngine("ready");
};
$("#cTest").onclick = async () => { $("#cStatus").textContent = "Testing"; const r = await api.test_cloud(); $("#cStatus").textContent = r.ok ? "Works." : "Failed: " + r.error; };

// apps
async function fillApps() {
  const c = state.config.apps;
  $("#spClient").value = c.spotify_client_id || ""; $("#spRedirect").textContent = c.spotify_redirect;
  $("#musicService").value = c.music_service || "spotify";
  api.spotify_status().then(renderSpotify);
  loadPlaybooks();
}
function pill(sel, text, on) { const p = $(sel); p.textContent = text; p.classList.toggle("on", !!on); }
function renderSpotify(s) {
  pill("#spStatus", s.connected ? `Connected · ${s.user}` : "Not connected", s.connected);
  $("#spDisconnect").classList.toggle("hidden", !s.connected);
}
$("#spConnect").onclick = async () => {
  const cid = $("#spClient").value.trim();
  if (!/^[0-9a-f]{32}$/i.test(cid)) return toast("The Client ID is a 32-character code from the Spotify dashboard");
  await save("apps", { spotify_client_id: cid });
  $("#spConnect").disabled = true; $("#spConnect").textContent = "Approve in browser";
  api.spotify_connect();
};
$("#spDisconnect").onclick = async () => renderSpotify(await api.spotify_disconnect());
$$("[data-act]", $("#tab-apps")).forEach(b => b.onclick = async () => {
  b.disabled = true; $("#appOut").textContent = "Working";
  try { const out = await api.app_action(b.dataset.act); $("#appOut").textContent = ""; toast(out.slice(0, 260)); fillApps(); }
  finally { b.disabled = false; }
});
$("#saveApps").onclick = async () => {
  await save("apps", { music_service: $("#musicService").value });
  toast("Saved"); fillApps();
};
let playbooks = [];
async function loadPlaybooks() {
  playbooks = await api.get_playbooks();
  const sel = $("#pbSelect"), keep = sel.value;
  sel.innerHTML = playbooks.map(p => `<option value="${p.key}">${esc(p.label)}${p.custom ? " (edited)" : ""}</option>`).join("");
  if (keep) sel.value = keep;
  showPlaybook();
}
function showPlaybook() { const p = playbooks.find(x => x.key === $("#pbSelect").value) || playbooks[0]; if (p) $("#pbText").value = p.custom || p.default; }
$("#pbSelect").onchange = showPlaybook;
$("#pbSave").onclick = async () => { playbooks = await api.save_playbook($("#pbSelect").value, $("#pbText").value); toast("Playbook saved"); loadPlaybooks(); };
$("#pbReset").onclick = async () => { playbooks = await api.save_playbook($("#pbSelect").value, ""); toast("Reset"); loadPlaybooks(); };

// discord
function renderDiscord(s) {
  if (!s) return;
  const dot = $("#dcDot"), head = $("#dcHead"), sub = $("#dcSub");
  $("#dcUnpair").classList.toggle("hidden", !s.owner);
  $("#dcInvite").classList.toggle("hidden", !(s.connected && s.invite));
  if (s.invite) { $("#dcInviteLink").textContent = s.invite; $("#dcInviteLink").setAttribute("href", s.invite); }
  $("#dcCode").classList.toggle("hidden", !(s.pairing && s.code));
  if (s.code) $("#dcCodeText").textContent = "pair " + s.code;
  $("#dcUser").classList.toggle("hidden", !s.user_install);
  if (s.user_install) { $("#dcUserLink").textContent = "Add " + (s.command || "nova") + " to my Discord account"; $("#dcUserLink").setAttribute("href", s.user_install); }
  if (s.command) $("#dcCmd").textContent = "/" + s.command;
  if (s.error) { dot.className = "dot err"; head.textContent = "Problem"; sub.textContent = s.error; return; }
  if (!s.enabled) { dot.className = "dot"; head.textContent = "Off"; sub.textContent = s.has_token ? "Token saved. Switch the bot on to connect." : "Run Astra as a Discord bot. Optional."; return; }
  if (!s.connected) { dot.className = "dot wait"; head.textContent = "Connecting"; sub.textContent = "Logging in to Discord..."; return; }
  dot.className = "dot on"; head.textContent = `Online as ${s.bot}`;
  sub.textContent = s.owner ? `Paired with ${s.owner_name || s.owner}. DM the bot to give tasks.` : "Online, not paired yet. Press Pair.";
  if (s.owner && !$("#dcCode").classList.contains("hidden")) $("#dcCode").classList.add("hidden");
}
$("#dcConnect").onclick = async () => {
  const tok = $("#dcToken").value.trim();
  if (tok) { if (tok.length < 50) return toast("That doesn't look like a bot token"); await api.set_secret("discord_token", tok); $("#dcToken").value = ""; }
  if (!$("#dcEnabled").checked) $("#dcEnabled").checked = true;
  await save("discord", { enabled: true });
  await loadState(); fillSettings(); renderDiscord(await api.discord_status());
};
$("#dcEnabled").onchange = async () => { await save("discord", { enabled: $("#dcEnabled").checked }); renderDiscord(await api.discord_status()); };
$("#dcPair").onclick = async () => { const r = await api.discord_pair(); if (!r.ok) return toast(r.error); renderDiscord(r); };
$("#dcUnpair").onclick = async () => renderDiscord(await api.discord_unpair());
$("#dcTest").onclick = async () => { const r = await api.discord_test(); toast(r.ok ? "Test DM sent" : r.error); };
bindToggle("discord", "approvals_to_dm", $("#dcApprovals"));
bindToggle("discord", "task_updates", $("#dcUpdates"));
bindToggle("discord", "reply_in_servers", $("#dcServers"));
bindToggle("discord", "others_mode", $("#dcOthers"));
setInterval(() => { if ($("#tab-discord").classList.contains("active") && $("#view-settings").classList.contains("active")) api.discord_status().then(renderDiscord); }, 3000);

// friend settings
$("#fSave").onclick = async () => {
  await save("friend", { name: $("#fName").value.trim() || "Nova", personality: $("#fPersona").value.trim(), can_hand_off: $("#fHandoff").checked,
    texting: $("#fGenz").checked ? "genz" : "normal", auto_memory: $("#fMemory").checked, auto_events: $("#fEvents").checked, proactive: $("#fProactive").checked,
    proactive_level: $("#fLevel").value, quiet_start: $("#fQuietA").value || "23:00", quiet_end: $("#fQuietB").value || "09:00",
    proactive_discord: $("#fDiscord").checked });
  $("#navFriend").textContent = friendName(); toast("Saved");
};

// safety
$("#saveSafety").onclick = async () => {
  await save("control", { background_mode: $("#sBg").checked, full_disk: $("#sDisk").checked, enabled: $("#sControl").checked, confirm_messages: $("#sMsg").checked, confirm_commands: $("#sCmd").checked,
    confirm_files: $("#sFiles").checked, stop_hotkey: $("#sHotkey").checked, blocked_apps: $("#sBlocked").value.trim() });
  await save("approvals", { send_email: $("#sEmail").checked });
  await save("browser", { headless: $("#bHeadless").checked, cookies: $("#bCookies").value, extensions_dir: $("#bExt").value.trim() });
  toast("Saved");
};

// email accounts
function renderMailAccounts() {
  const accs = state.config.mail.accounts || [], ok = state.config.mail_secrets || {};
  $("#mailAccounts").innerHTML = accs.map(a => `<div class="acct" data-id="${a.id}"><div class="ai"><b>${esc(a.label || a.address)}</b>
    <small>${esc(a.address)}${a.watch ? ", watched" : ""}${ok[a.id] ? "" : ", no password saved"}${a.important ? ", important: " + esc(a.important) : ""}</small></div>
    <button class="btn-ghost sm" data-a="edit">Edit</button><button class="btn-ghost sm" data-a="del">Remove</button></div>`).join("") ||
    `<p class="muted small">No accounts yet.</p>`;
  $$(".acct").forEach(row => {
    const a = accs.find(x => x.id === row.dataset.id);
    row.querySelector('[data-a="edit"]').onclick = () => editMail(a);
    row.querySelector('[data-a="del"]').onclick = async e => { if (!confirmInline(e.target, "Sure?")) return; state.config = await api.delete_mail_account(a.id); renderMailAccounts(); };
  });
}
function editMail(a) {
  $("#mailForm").classList.remove("hidden"); $("#maStatus").textContent = "";
  $("#maId").value = a ? a.id : ""; $("#maLabel").value = a ? a.label || "" : ""; $("#maAddress").value = a ? a.address : "";
  $("#maPassword").value = ""; $("#maPassword").placeholder = a && state.config.mail_secrets[a.id] ? "saved - leave empty to keep" : "app password";
  $("#maUser").value = a ? a.username || "" : ""; $("#maImap").value = a ? a.imap_host || "" : ""; $("#maSmtp").value = a ? a.smtp_host || "" : "";
  $("#maPort").value = a ? a.smtp_port || "" : ""; $("#maWatch").checked = a ? a.watch !== false : true; $("#maImportant").value = a ? a.important || "" : "";
  $("#maLabel").focus();
}
$("#mailAdd").onclick = () => editMail(null);
$("#maCancel").onclick = () => $("#mailForm").classList.add("hidden");
$("#maAddress").onchange = async () => {
  const p = await api.email_preset($("#maAddress").value);
  if (p.smtp_host) { if (!$("#maSmtp").value) $("#maSmtp").value = p.smtp_host; if (!$("#maPort").value) $("#maPort").value = p.smtp_port; if (!$("#maImap").value) $("#maImap").value = p.imap_host; }
};
async function saveMailForm() {
  const r = await api.save_mail_account({ id: $("#maId").value || null, label: $("#maLabel").value, address: $("#maAddress").value, username: $("#maUser").value,
    imap_host: $("#maImap").value, smtp_host: $("#maSmtp").value, smtp_port: +$("#maPort").value || 0, watch: $("#maWatch").checked, important: $("#maImportant").value }, $("#maPassword").value);
  if (r.error) { toast(r.error); return null; }
  state.config = r.config; $("#maId").value = r.id; $("#maPassword").value = ""; renderMailAccounts(); return r.id;
}
$("#maSave").onclick = async () => { if (await saveMailForm()) { $("#mailForm").classList.add("hidden"); toast("Account saved"); } };
$("#maTest").onclick = async () => { const id = await saveMailForm(); if (!id) return; $("#maStatus").textContent = "Testing"; const r = await api.test_email(id); $("#maStatus").textContent = r.ok ? "Works." : "Failed: " + r.error; };
$("#saveMail").onclick = async () => {
  await save("mail", { check_minutes: +$("#mEvery").value, notify_important: $("#mNotify").checked, auto_reply: $("#mAuto").checked,
    auto_reply_text: $("#mAutoText").value, blacklist: $("#mBlacklist").value,
    filter: { only_important: $("#fOnly").checked, always: $("#fAlways").value, never: $("#fNever").value,
      hide: $$("#fHide input:checked").map(i => i.value) } });
  toast("Saved");
};
$("#checkMail").onclick = () => { api.check_mail_now(); toast("Checking mail"); setTimeout(loadMailLog, 8000); };
async function loadMailLog() {
  const rows = await api.mail_digest();
  $("#mailLog").innerHTML = rows.length ? rows.map(r => `<div class="entry"><span class="ts">${fmtTime(r.ts)}</span><span class="k">${esc(r.category)}${r.important ? " !" : ""}</span><span class="tx">${esc(r.sender)}: ${esc(r.subject)}${r.summary ? "\n" + esc(r.summary) : ""}${r.replied ? "\n(auto-replied)" : ""}</span></div>`).join("")
    : `<p class="muted small">Nothing sorted yet.</p>`;
}

// calendar settings
function renderCalSources() {
  const srcs = state.config.calendar.sources || [];
  $("#calSources").innerHTML = srcs.map(s => `<div class="srcrow"><b>${esc(s.name)}</b><span class="grow">${esc(s.url ? host(s.url) : s.path)}</span><button class="btn-ghost sm" data-id="${s.id}">Remove</button></div>`).join("");
  $$("#calSources button").forEach(b => b.onclick = async () => { state.config = await api.calendar_remove_source(b.dataset.id); renderCalSources(); });
}
$("#calPick").onclick = async () => { const p = await api.pick_file("ics"); if (p) $("#calUrl").value = p; };
$("#calAdd").onclick = async () => {
  $("#calAdd").disabled = true;
  const r = await api.calendar_add_source($("#calName").value.trim(), $("#calUrl").value.trim());
  $("#calAdd").disabled = false;
  if (r.error) return toast(r.error);
  state.config = r.config; $("#calName").value = ""; $("#calUrl").value = ""; renderCalSources(); toast(`Imported ${r.count} events`);
};
$("#calRemind").onchange = () => save("calendar", { remind_minutes: +$("#calRemind").value });

// phone
let qrLoaded = null, lastQr = "";
function loadQrLib() {
  if (qrLoaded) return qrLoaded;
  qrLoaded = new Promise(res => {
    const s = document.createElement("script"); s.src = "https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js";
    s.onload = () => res(true); s.onerror = () => { qrLoaded = null; res(false); }; document.head.appendChild(s);
  });
  return qrLoaded;
}
async function renderPhone(s) {
  if (!s) return;
  const on = state.config.phone.enabled;
  $("#phDot").className = "dot " + (s.link ? "on" : s.running ? "wait" : on ? "err" : "");
  $("#phHead").textContent = !on ? "Off" : s.public ? "Ready, reachable from anywhere" : s.link ? "Ready on your home Wi-Fi" : s.running ? "Starting" : "Not running";
  $("#phSub").textContent = s.error || (s.link ? "Scan the code with your iPhone camera." : on && !s.tunnel_wanted && !state.config.phone.lan ? "Turn on the tunnel or home Wi-Fi access so the phone can reach this PC." : "Turn it on, then scan the code with your iPhone camera.");
  $("#phBox").classList.toggle("hidden", !s.link);
  if (s.link && s.link !== lastQr) {
    lastQr = s.link; $("#phQr").innerHTML = "";
    if (await loadQrLib() && window.QRCode) new QRCode($("#phQr"), { text: s.link, width: 172, height: 172, correctLevel: QRCode.CorrectLevel.M });
    else $("#phQr").textContent = "Couldn't draw the code here. Use Copy link or Send link to my Discord.";
  }
  $("#phCopy").onclick = () => { navigator.clipboard.writeText(s.link).then(() => toast("Link copied"), () => toast(s.link, { ms: 12000 })); };
}
const savePhone = async () => { await save("phone", { enabled: $("#phOn").checked, tunnel: $("#phTunnel").checked, lan: $("#phLan").checked }); setTimeout(() => api.phone_status().then(renderPhone), 800); };
$("#phOn").onchange = savePhone; $("#phTunnel").onchange = savePhone; $("#phLan").onchange = savePhone;
$("#phDm").onclick = async () => { const r = await api.phone_send_link(); toast(r.ok ? "Sent to your Discord DMs" : r.error); };
$("#phNewKey").onclick = async e => { if (!confirmInline(e.target, "Old link stops working. Sure?")) return; lastQr = ""; renderPhone(await api.phone_new_key()); toast("New key made. Open the new link on your phone."); };

/* ---------------- calendar: month + week ---------------- */
const cal = { mode: "month", anchor: new Date(), sel: null, evs: [] };
const DAY = 86400000;
function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function sameDay(a, b) { return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); }
function mondayOf(d) { const x = startOfDay(d); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; }
function calRange() {
  if (cal.mode === "week") { const a = mondayOf(cal.anchor); return [a, new Date(a.getTime() + 7 * DAY)]; }
  const first = new Date(cal.anchor.getFullYear(), cal.anchor.getMonth(), 1);
  const a = mondayOf(first); return [a, new Date(a.getTime() + 42 * DAY)];
}
function evsOn(d) { const a = startOfDay(d).getTime() / 1000, b = a + 86400; return cal.evs.filter(e => e.start < b && e.end > a).sort((x, y) => x.all_day - y.all_day || x.start - y.start); }
function evTime(e) { return e.all_day ? "all day" : hm(e.start); }
async function loadWeek() {
  const [a, b] = calRange();
  cal.evs = await api.calendar_range(a.getTime() / 1000, b.getTime() / 1000);
  const today = startOfDay(new Date());
  $("#calTitle").textContent = cal.mode === "month"
    ? cal.anchor.toLocaleDateString([], { month: "long", year: "numeric" })
    : `Week of ${a.toLocaleDateString([], { day: "numeric", month: "long" })}`;
  $("#calDow").innerHTML = [...Array(7)].map((_, i) => `<span>${new Date(a.getTime() + i * DAY).toLocaleDateString([], { weekday: "short" })}</span>`).join("");
  const n = cal.mode === "month" ? 42 : 7;
  $("#calGrid").className = cal.mode === "month" ? "month" : "month weekmode";
  $("#calGrid").innerHTML = [...Array(n)].map((_, i) => {
    const d = new Date(a.getTime() + i * DAY + 3600000); d.setHours(0, 0, 0, 0);
    const list = evsOn(d), max = cal.mode === "month" ? 3 : 12;
    const out = cal.mode === "month" && d.getMonth() !== cal.anchor.getMonth();
    const cls = ["mday", out ? "out" : "", sameDay(d, today) ? "today" : "", cal.sel && sameDay(d, cal.sel) ? "sel" : "", d < today ? "past" : ""].join(" ");
    return `<div class="${cls}" data-d="${d.getTime()}"><div class="dn">${d.getDate()}</div>` +
      list.slice(0, max).map(e => `<div class="ev${e.mine ? " mine" : ""}${e.repeat ? " rep" : ""}${e.done ? " done" : ""}${e.task && !e.done && e.end * 1000 < Date.now() ? " overdue" : ""}" title="${esc(e.title)}"><i>${evTime(e)}</i>${esc(e.title)}</div>`).join("") +
      (list.length > max ? `<div class="more">+${list.length - max} more</div>` : "") + `</div>`;
  }).join("");
  $$("#calGrid .mday").forEach(el => el.onclick = () => { cal.sel = new Date(+el.dataset.d); $$("#calGrid .mday").forEach(x => x.classList.toggle("sel", x === el)); renderDay(); });
  if (!cal.sel || cal.sel < a || cal.sel >= b) cal.sel = today >= a && today < b ? today : new Date(cal.mode === "month" ? new Date(cal.anchor.getFullYear(), cal.anchor.getMonth(), 1) : a);
  renderDay();
  $("#calHint").classList.toggle("hidden", (state.config.calendar.sources || []).length > 0 || cal.evs.length > 0);
}
function renderDay() {
  const d = cal.sel, list = evsOn(d);
  $("#dvTitle").textContent = d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  $("#dvSub").textContent = list.length ? `${list.length} thing${list.length === 1 ? "" : "s"}` : "nothing planned";
  $("#dvList").innerHTML = list.map(e => `<div class="dv-ev${e.done ? " done" : ""}"><label class="dv-check" title="${e.done ? "Mark as not done" : "Mark as done"}"><input type="checkbox" data-uid="${esc(e.uid)}" data-start="${e.start}" ${e.done ? "checked" : ""}><i></i></label><span class="t">${e.all_day ? "all day" : hm(e.start) + " – " + hm(e.end)}</span><span class="n">${e.repeat ? `<em class="rep-i" title="Repeats">↻</em>` : ""}${esc(e.title)}${e.location ? `<small>${esc(e.location)}</small>` : ""}</span><span class="c">${esc(e.cal)}</span>${e.mine ? `<button class="x" data-uid="${esc(e.uid)}" data-start="${e.start}" data-rep="${e.repeat ? 1 : ""}" title="Remove">×</button>` : ""}</div>`).join("") || `<p class="muted small">A free day.</p>`;
  $$("#dvList .x").forEach(b => b.onclick = async () => {
    if (b.dataset.rep) {   // repeating: just this day, or all of them
      const row = b.closest(".dv-ev"); if (row.querySelector(".rep-ask")) return;
      row.insertAdjacentHTML("beforeend", `<span class="rep-ask"><button class="btn-ghost sm" data-s="one">Just this day</button><button class="btn-ghost sm" data-s="all">All of them</button></span>`);
      row.querySelectorAll(".rep-ask button").forEach(x => x.onclick = async () => { await api.calendar_delete_event(b.dataset.uid, +b.dataset.start, x.dataset.s); loadWeek(); });
      return;
    }
    await api.calendar_delete_event(b.dataset.uid, +b.dataset.start, "one"); loadWeek();
  });
  $$("#dvList .dv-check input").forEach(c => c.onchange = async () => { await api.calendar_set_done(c.dataset.uid, +c.dataset.start, c.checked); loadWeek(); });
}
$("#dvAdd").onclick = async () => {
  const title = $("#dvName").value.trim(); if (!title) return $("#dvName").focus();
  const t = $("#dvTime").value.trim(), d = cal.sel;
  const ymd = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const rep = $("#dvRepeat").value;
  const r = await api.calendar_add_event(title, t ? `${ymd} ${t.replace(".", ":")}` : ymd, 60, rep);
  if (!r.ok) return toast(r.text && r.text.startsWith("Already") ? r.text : "That time didn't work, try 14:00");
  if (rep) toast(r.text.replace(/^Added: /, ""));
  $("#dvName").value = ""; $("#dvTime").value = ""; $("#dvRepeat").value = ""; loadWeek();
};
$("#dvName").onkeydown = $("#dvTime").onkeydown = e => { if (e.key === "Enter") $("#dvAdd").click(); };
function calShift(dir) {
  const x = new Date(cal.anchor);
  if (cal.mode === "month") { x.setDate(1); x.setMonth(x.getMonth() + dir); } else x.setDate(x.getDate() + 7 * dir);
  cal.anchor = x; cal.sel = null; loadWeek();
}
$("#calPrev").onclick = () => calShift(-1); $("#calNext").onclick = () => calShift(1);
$("#calToday").onclick = () => { cal.anchor = new Date(); cal.sel = null; loadWeek(); };
$$("#calMode button").forEach(b => b.onclick = () => { cal.mode = b.dataset.m; $$("#calMode button").forEach(x => x.classList.toggle("on", x === b)); loadWeek(); });
$("#calRefresh").onclick = async () => { $("#calRefresh").disabled = true; const r = await api.calendar_refresh(); $("#calRefresh").disabled = false; loadWeek(); const errs = Object.values(r).filter(v => typeof v === "string"); toast(errs.length ? errs[0] : "Calendars refreshed"); };

$("#rpImg").onclick = () => api.presence_image();
$("#bExtPick").onclick = async () => { const p = await api.pick_file("folder"); if (p) $("#bExt").value = p; };

/* ---------------- slash commands ---------------- */
const COMMANDS = {
  nova: { icon: "orbit", hint: "say something to Nova", run: async t => {
    showView("friend"); if (t) { finput.value = t; fsend(); } return true; } },
  friend: { hidden: true, icon: "orbit", hint: "", run: async t => COMMANDS.nova.run(t) },
  research: { icon: "star4s", hint: "research something and keep notes, e.g. /research weekend events near me", run: async t => {
    if (!t) { toast("What should Astra research?"); return true; } input.value = "Research this and take notes as you go: " + t; return false; } },
  remember: { icon: "star4s", hint: "save a fact to memory", run: async t => {
    if (!t) { toast("What should I remember?"); return true; } await api.add_memory(t); toast("Remembered"); return true; } },
  routine: { icon: "moon", hint: "schedule something, e.g. /routine every day 18:00 remind me to stream", run: async t => {
    if (!t) { showView("routines"); return true; } input.value = "Schedule this routine: " + t; return false; } },
  new: { icon: "star4", hint: "start a new task", run: async () => { newChat(); return true; } },
  stop: { icon: "comet", hint: "stop everything", run: async () => { api.panic(); toast("Stopped everything"); return true; } },
};
let slashSel = 0;
function slashMatches() {
  const m = input.value.match(/^\/(\w*)$/);
  if (!m) return null;
  return Object.keys(COMMANDS).filter(k => !COMMANDS[k].hidden && k.startsWith(m[1].toLowerCase()));
}
function renderSlash() {
  const list = slashMatches();
  const box = $("#slash");
  if (!list || !list.length) return closeSlash();
  slashSel = Math.min(slashSel, list.length - 1);
  box.innerHTML = list.map((k, i) => `<div class="si ${i === slashSel ? "on" : ""}" data-k="${k}"><svg><use href="#${COMMANDS[k].icon}"/></svg><b>/${k}</b><span>${esc(COMMANDS[k].hint)}</span></div>`).join("");
  box.classList.remove("hidden");
  $$(".si", box).forEach(el => el.onmousedown = e => { e.preventDefault(); pickSlash(el.dataset.k); });
}
function closeSlash() { $("#slash").classList.add("hidden"); slashSel = 0; }
function pickSlash(k) {
  input.value = "/" + k + " "; closeSlash(); input.focus();
  if (["new", "stop"].includes(k)) send();
}
input.addEventListener("input", renderSlash);
input.addEventListener("blur", () => setTimeout(closeSlash, 120));
input.addEventListener("keydown", e => {
  if ($("#slash").classList.contains("hidden")) return;
  const list = slashMatches() || [];
  if (e.key === "ArrowDown") { e.preventDefault(); slashSel = (slashSel + 1) % list.length; renderSlash(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); slashSel = (slashSel - 1 + list.length) % list.length; renderSlash(); }
  else if (e.key === "Tab" || e.key === "Enter") { e.preventDefault(); e.stopImmediatePropagation(); pickSlash(list[slashSel]); }
  else if (e.key === "Escape") closeSlash();
}, true);

/* ---------------- theme: background, accent, mood accent ---------------- */
const PALETTES = {
  lavender: { name: "Lavender", lav: [167, 139, 250], peri: [165, 180, 252], rose: [233, 168, 250], deep: "#5b4bb5" },
  rose: { name: "Rose", lav: [244, 133, 177], peri: [251, 182, 206], rose: [253, 186, 140], deep: "#a33a66" },
  ocean: { name: "Ocean", lav: [96, 165, 250], peri: [125, 211, 252], rose: [167, 139, 250], deep: "#24589e" },
  mint: { name: "Mint", lav: [52, 211, 153], peri: [125, 211, 252], rose: [190, 242, 160], deep: "#17725a" },
  sunset: { name: "Sunset", lav: [251, 146, 60], peri: [244, 114, 182], rose: [250, 204, 21], deep: "#a8471c" },
  aurora: { name: "Aurora", lav: [45, 212, 191], peri: [167, 139, 250], rose: [244, 114, 182], deep: "#1c7a70" },
  mono: { name: "Mono", lav: [150, 150, 165], peri: [195, 195, 205], rose: [120, 120, 135], deep: "#3a3a44" },
  // mood-only palettes (also pickable as an accent)
  spark: { name: "Spark", lav: [255, 94, 135], peri: [255, 179, 71], rose: [255, 230, 109], deep: "#b03a2e", mood: true },
  candy: { name: "Candy", lav: [255, 128, 200], peri: [140, 200, 255], rose: [200, 160, 255], deep: "#a0337a", mood: true },
  honey: { name: "Honey", lav: [240, 170, 90], peri: [230, 190, 150], rose: [205, 120, 90], deep: "#8a5221", mood: true },
  midnight: { name: "Midnight", lav: [99, 102, 241], peri: [76, 95, 160], rose: [129, 140, 248], deep: "#2c2f7a", mood: true },
  dusk: { name: "Dusk", lav: [156, 128, 196], peri: [148, 163, 184], rose: [196, 160, 200], deep: "#4f4570", mood: true },
  storm: { name: "Storm", lav: [129, 120, 220], peri: [110, 160, 200], rose: [170, 120, 200], deep: "#3d3a85", mood: true },
  ember: { name: "Ember", lav: [239, 68, 68], peri: [251, 113, 133], rose: [249, 115, 22], deep: "#9b1c1c", mood: true },
  prism: { name: "Prism", lav: [236, 72, 153], peri: [59, 130, 246], rose: [250, 204, 21], deep: "#7a2f91", mood: true },
  sepia: { name: "Sepia", lav: [214, 160, 120], peri: [190, 170, 200], rose: [232, 190, 160], deep: "#7a4f36", mood: true },
  gold: { name: "Gold", lav: [234, 179, 8], peri: [253, 224, 140], rose: [251, 146, 60], deep: "#856108", mood: true },
  dawn: { name: "Dawn", lav: [253, 164, 175], peri: [186, 230, 253], rose: [254, 215, 170], deep: "#9c4a5e", mood: true },
  royal: { name: "Royal", lav: [124, 58, 237], peri: [234, 179, 8], rose: [192, 132, 252], deep: "#4c1d95", mood: true },
  fog: { name: "Fog", lav: [160, 170, 180], peri: [190, 200, 210], rose: [175, 165, 190], deep: "#4b5563", mood: true },
};
// how each feeling looks. warm = up, cool = down, deep = heavy, bright mixes = energy
const MOOD_PALETTE = {
  happy: "sunset", excited: "spark", playful: "candy", calm: "mint", cozy: "honey",
  sad: "ocean", lonely: "midnight", tired: "dusk", anxious: "storm", stressed: "lavender",
  angry: "ember", focused: "aurora", creative: "prism", romantic: "rose", dreamy: "lavender",
  nostalgic: "sepia", grateful: "gold", hopeful: "dawn", confident: "royal", bored: "fog", neutral: null,
};
let shown = null, mood = "neutral", tween = null;
function targetPalette() {
  const a = state.config.appearance || {};
  const m = a.mood_accent ? MOOD_PALETTE[mood] : null;
  return PALETTES[m || a.accent] || PALETTES.lavender;
}
function setVars(p) {
  const st = document.body.style;
  st.setProperty("--lav", p.lav.map(Math.round).join(", "));
  st.setProperty("--peri", p.peri.map(Math.round).join(", "));
  st.setProperty("--rose", p.rose.map(Math.round).join(", "));
  if (p.deep) st.setProperty("--deep", p.deep);
}
function applyTheme(animate) {
  const a = state.config.appearance || {};
  const hz = a.background === "horizon";
  document.body.classList.toggle("dark", a.background === "dark" || hz);
  document.body.classList.toggle("horizon", hz);
  if (window.Horizon) { if (hz) { if (!Horizon.start()) document.body.classList.remove("horizon"); } else Horizon.stop(); }
  const to = targetPalette();
  if (!animate || !shown || document.body.classList.contains("calm")) {
    shown = JSON.parse(JSON.stringify(to)); setVars(shown); if (window.Sky) Sky.refresh(); return;
  }
  const from = JSON.parse(JSON.stringify(shown)), t0 = performance.now(), dur = 1400;
  cancelAnimationFrame(tween);
  const step = now => {
    const k = Math.min(1, (now - t0) / dur), e = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
    for (const key of ["lav", "peri", "rose"]) shown[key] = from[key].map((v, i) => v + (to[key][i] - v) * e);
    shown.deep = to.deep; setVars(shown);
    if (k < 1) tween = requestAnimationFrame(step); else if (window.Sky) Sky.refresh();
  };
  tween = requestAnimationFrame(step);
}
function grad(p) { return `linear-gradient(120deg, rgb(${p.lav}), rgb(${p.peri}) 55%, rgb(${p.rose}))`; }
function setMood(m) {
  const prev = mood;
  mood = MOOD_PALETTE[m] !== undefined ? m : "neutral";
  const on = !!(state.config.appearance || {}).mood_accent;
  if ($("#moodNow")) $("#moodNow").textContent = "Current mood: " + mood;
  const chip = $("#moodChip");
  if (chip) {
    chip.classList.toggle("hidden", !on || mood === "neutral");
    chip.querySelector("span").textContent = mood;
    chip.querySelector("i").style.background = grad(PALETTES[MOOD_PALETTE[mood]] || PALETTES.lavender);
  }
  $$("#moodMap .mm").forEach(x => x.classList.toggle("on", x.dataset.m === mood));
  if (on && prev !== mood) { applyTheme(true); if (window.Sky && mood !== "neutral") Sky.refresh(); }
}
function renderMoodMap() {
  $("#moodMap").innerHTML = Object.entries(MOOD_PALETTE).filter(([, p]) => p).map(([m, p]) =>
    `<span class="mm ${m === mood ? "on" : ""}" data-m="${m}" title="preview"><i style="background:${grad(PALETTES[p])}"></i>${m}</span>`).join("");
  $$("#moodMap .mm").forEach(x => x.onclick = () => setMood(x.dataset.m === mood ? "neutral" : x.dataset.m));
}
function renderSwatches() {
  const cur = (state.config.appearance || {}).accent || "lavender";
  $("#swatches").innerHTML = Object.entries(PALETTES).filter(([k, p]) => !p.mood || k === cur).map(([k, p]) => `<div class="swatch ${k === cur ? "on" : ""}" data-k="${k}">
    <i style="background: linear-gradient(120deg, rgb(${p.lav}), rgb(${p.peri}) 55%, rgb(${p.rose}))"></i>${p.name}</div>`).join("");
  $$(".swatch").forEach(el => el.onclick = async () => { await save("appearance", { accent: el.dataset.k }); renderSwatches(); applyTheme(true); });
}
$$("#bgSeg button").forEach(b => b.onclick = async () => {
  await save("appearance", { background: b.dataset.bg });
  $$("#bgSeg button").forEach(x => x.classList.toggle("on", x === b)); applyTheme(true);
});
$("#moodAccent").onchange = async () => { await save("appearance", { mood_accent: $("#moodAccent").checked }); applyTheme(true); setMood(mood); };

/* ---------------- personal context ---------------- */
$("#ctxSave").onclick = async () => { await api.save_context($("#ctxText").value); $("#ctxStatus").textContent = "Saved."; };
$("#ctxImport").onclick = async () => { const r = await api.import_context(); if (r.ok) $("#ctxStatus").textContent = `Importing ${r.file}... this can take a few minutes.`; };

/* ---------------- voice ---------------- */
let voiceOpts = null;
async function fillVoice() {
  fillStt();
  const v = state.config.voice || {};
  $("#vOn").checked = !!v.enabled; $("#vAstra").checked = v.astra !== false; $("#vFriend").checked = v.friend !== false;
  $("#vEngine").value = v.engine || "windows"; $("#vRate").value = v.rate || 0;
  voiceOpts = voiceOpts || await api.voice_options();
  renderVoices(v.voice);
}
function renderVoices(sel) {
  const list = $("#vEngine").value === "piper" ? voiceOpts.piper : voiceOpts.windows;
  const lab = voiceOpts.labels || {}, have = voiceOpts.have || [];
  $("#vVoice").innerHTML = (list.length ? list : ["(default)"]).map(n => `<option value="${esc(n)}" ${n === sel ? "selected" : ""}>${esc(lab[n] ? lab[n] + (have.includes(n) ? "" : " · download") : n)}</option>`).join("");
  $("#vGet").classList.toggle("hidden", $("#vEngine").value !== "piper");
}
$("#vEngine").onchange = () => renderVoices();
$("#vSave").onclick = async () => { await save("voice", { enabled: $("#vOn").checked, astra: $("#vAstra").checked, friend: $("#vFriend").checked,
  engine: $("#vEngine").value, voice: $("#vVoice").value === "(default)" ? "" : $("#vVoice").value, rate: +$("#vRate").value || 0 }); toast("Saved"); };
$("#vGet").onclick = () => { api.voice_install($("#vVoice").value); toast("Downloading voice"); };
$("#vTest").onclick = async () => { await $("#vSave").onclick(); api.speak(`Hi ${state.config.profile.name || ""}, it's Astra. This is my voice.`); };
$("#vStop").onclick = () => api.stop_speaking();

/* ---------------- accounts ---------------- */
function fillAccounts() {
  const c = state.config;
  $("#uniProv").value = c.university.provider || "moodle"; $("#uniUrl").value = c.university.url || ""; $("#uniUser").value = c.university.username || "";
  pill("#uniStatus", c.university.url ? "Set up" : "Not set up", !!c.university.url);
}
$("#uniSave").onclick = async () => { await save("university", { provider: $("#uniProv").value, url: $("#uniUrl").value.trim(), username: $("#uniUser").value.trim() });
  if ($("#uniPass").value) { await api.set_secret("uni_password", $("#uniPass").value); $("#uniPass").value = ""; }
  if ($("#uniToken").value) { await api.set_secret("canvas_token", $("#uniToken").value.trim()); $("#uniToken").value = ""; }
  await loadState(); fillAccounts(); toast("Saved"); };
$("#uniTest").onclick = async () => { $("#uniOut").textContent = "Testing"; const r = await api.test_university(); $("#uniOut").textContent = r.ok ? r.text.split("\n").slice(0, 3).join(" · ") : "Failed: " + r.error; };


/* ---------------- Discord account, presence, Auto-Astra ---------------- */
function renderDA(s) {
  if (!s) return;
  $("#daDot").className = "dot " + (s.connected ? "on" : s.status === "waiting for login" ? "wait" : "");
  $("#daHead").textContent = s.connected ? `Connected${s.me ? " as " + s.me : ""}` : s.status === "waiting for login" ? "Waiting for you to log in" : "Not connected";
  $("#daLogout").classList.toggle("hidden", !s.connected);
  $("#rpErr").textContent = s.presence_error ? "Rich Presence: " + s.presence_error : "";
  $("#aaLog").innerHTML = (s.log || []).slice().reverse().map(l => `<div class="entry"><span class="ts">${fmtTime(l.t)}</span><span class="k">auto</span><span class="tx">${esc(nodash(l.text))}</span></div>`).join("")
    || `<p class="sub small">Auto-Astra replies show up here.</p>`;
}
function fillDA() {
  const c = state.config, a = c.auto_astra || {}, d = c.discord_account || {};
  $("#rpOn").checked = d.rich_presence !== false; $("#rpId").value = d.rpc_client_id || "";
  $("#aaOn").checked = !!a.enabled; $("#aaWho").value = a.who || "allowlist"; $("#aaAllow").value = a.allow || "";
  $("#aaEvery").value = a.check_seconds || 45; $("#aaStyle").value = a.style_notes || ""; $("#aaDisclose").checked = a.disclose !== false;
  api.discord_account_state().then(renderDA);
}
$("#daLogin").onclick = () => { api.discord_login(); toast("A Discord window opens. Log in there; it hides itself afterwards."); };
$("#daLogout").onclick = async () => renderDA(await api.discord_logout());
$("#rpOn").onchange = () => save("discord_account", { rich_presence: $("#rpOn").checked });
$("#rpId").onchange = () => save("discord_account", { rpc_client_id: $("#rpId").value.trim() });
$("#aaSave").onclick = async () => {
  if ($("#aaOn").checked && !state.config.discord_account.connected) toast("Log in to Discord first");
  await save("auto_astra", { enabled: $("#aaOn").checked, who: $("#aaWho").value, allow: $("#aaAllow").value.trim(),
    check_seconds: Math.max(20, +$("#aaEvery").value || 45), style_notes: $("#aaStyle").value.trim(), disclose: $("#aaDisclose").checked });
  toast($("#aaOn").checked ? "Auto-Astra is on" : "Auto-Astra is off");
};
setInterval(() => { if ($("#tab-dcacct").classList.contains("active") && $("#view-settings").classList.contains("active")) api.discord_account_state().then(renderDA); }, 4000);

/* ---------------- Telegram ---------------- */
function renderTG(s) {
  if (!s) return;
  $("#tgDisconnect").classList.toggle("hidden", !s.connected);
  if (s.error) { $("#tgDot").className = "dot err"; $("#tgHead").textContent = "Problem"; $("#tgSub").textContent = s.error; return; }
  if (s.connected) { $("#tgDot").className = "dot on"; $("#tgHead").textContent = `Connected${s.bot ? " to @" + s.bot : ""}`;
    $("#tgSub").textContent = `Talking as ${s.mode === "astra" ? "Astra (PC tasks)" : "Nova"}. /menu switches.`; $("#tgLinkBox").classList.add("hidden"); return; }
  if (s.pairing && s.link) { $("#tgDot").className = "dot wait"; $("#tgHead").textContent = "Waiting for you to press Start";
    $("#tgLink").textContent = s.link; $("#tgLink").setAttribute("href", s.link); $("#tgLinkBox").classList.remove("hidden"); return; }
  $("#tgDot").className = "dot"; $("#tgHead").textContent = s.has_token ? "Token saved, not linked yet" : "Not connected";
}
$("#tgConnect").onclick = async () => {
  const tok = $("#tgToken").value.trim();
  await save("telegram", { owner_username: $("#tgOwner").value.trim().replace(/^@/, "") });
  if (tok) { if (!/^\d{5,}:[\w-]{30,}$/.test(tok)) return toast("That doesn't look like a bot token"); await api.set_secret("telegram_token", tok); $("#tgToken").value = ""; }
  const r = await api.telegram_pair();
  if (!r.ok) return renderTG({ error: r.error });
  renderTG(await api.telegram_status());
};
$("#tgTest").onclick = async () => { const r = await api.telegram_test(); toast(r.ok ? "Sent" : r.error); };
$("#tgDisconnect").onclick = async () => renderTG(await api.telegram_disconnect());
setInterval(() => { if ($("#tab-telegram").classList.contains("active") && $("#view-settings").classList.contains("active")) api.telegram_status().then(renderTG); }, 3000);

/* ---------------- Discord bot (friend only) ---------------- */
$("#dcSaveOpts").onclick = async () => {
  await save("discord", { owner_username: $("#dcOwnerName").value.trim().replace(/^@/, ""), reply_in_servers: $("#dcServers").checked,
    others_mode: $("#dcOthers").value, approvals_to_dm: $("#dcApprovals").checked, task_updates: $("#dcUpdates").checked });
  toast("Saved");
};
$("#reindex").onclick = () => { api.rebuild_file_index(); toast("Rebuilding the file index in the background"); };

/* ---------------- welcome ---------------- */
async function showWelcome(force) {
  const ui = state.config.ui || {};
  if (!force && ui.welcome === false) return;
  const p = state.config.profile;
  const h = new Date().getHours();
  const part = h < 5 ? "Still up" : h < 12 ? "Good morning" : h < 18 ? "Hey" : "Good evening";
  $("#wDate").textContent = new Date().toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  $("#wHello").textContent = p.name ? `${part}, ${p.name}.` : "Welcome to AstraNova.";
  $("#wOnboard").classList.toggle("hidden", !!p.name);
  $("#wShow").checked = ui.welcome !== false;
  $("#wNovaBtn").textContent = `Talk to ${friendName()}`;
  $("#welcome").classList.remove("hidden");
  const em = $("#wEmblem");
  if (window.Sky && !em.firstChild) Sky.flare(em);
  const W = $("#welcome"); W.classList.remove("intro"); void W.offsetWidth; W.classList.add("intro");
  try {
    const w = await api.welcome();
    const lines = [];
    (w.today || []).forEach(t => lines.push(`<li>${esc(t)}</li>`));
    if (!lines.length && w.next) lines.push(`<li>Next: ${esc(w.next.title)}, ${esc(w.next.when)}</li>`);
    if (w.mail) lines.push(`<li>${w.mail} important email${w.mail === 1 ? "" : "s"} today</li>`);
    if (w.tasks) lines.push(`<li>Astra is working on ${w.tasks} job${w.tasks === 1 ? "" : "s"}</li>`);
    if (!lines.length && !(state.config.calendar.sources || []).length && p.name) lines.push(`<li class="hint">Import your calendar in Settings so Nova knows your day.</li>`);
    $("#wToday").innerHTML = lines.join("");
    $("#wNova").classList.toggle("hidden", !w.last_nova);
    if (w.last_nova) $("#wNova").textContent = `${friendName()}: "${w.last_nova}"`;
  } catch { /* fine without it */ }
}
async function closeWelcome(view) {
  const name = $("#wName").value.trim();
  if (name) { await save("profile", { name }); }
  if ($("#wShow").checked !== (state.config.ui.welcome !== false)) await save("ui", { welcome: $("#wShow").checked });
  $("#welcome").classList.add("leaving");
  setTimeout(() => { $("#welcome").classList.add("hidden"); $("#welcome").classList.remove("leaving"); }, 520);
  setGreeting();
  if (view === "chat") newChat(); else showView(view);
}
$("#wNovaBtn").onclick = () => closeWelcome("friend");
$("#wAstraBtn").onclick = () => closeWelcome("chat");
$("#wName").onkeydown = e => { if (e.key === "Enter") closeWelcome("friend"); };
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#welcome").classList.contains("hidden")) closeWelcome("friend"); });

/* ---------------- boot ---------------- */
function setGreeting() {
  const n = state.config.profile.name, h = new Date().getHours();
  const part = h < 5 ? "Late night" : h < 12 ? "Morning" : h < 18 ? "Afternoon" : "Evening";
  $("#greeting").textContent = n ? `${part}, ${n}. What should I do?` : "What should I do?";
}
async function loadState() { state = await api.get_state(); try { registerCustomMoods(); } catch (e) {} return state; }
function setCalm(on) { localStorage.setItem("astra-calm", on ? "1" : "0"); document.body.classList.toggle("calm", on); if (window.Sky) Sky.redraw(); }
function burstFrom(el) { if (window.Horizon) Horizon.pulse(); if (!window.Sky || !el) return; const r = el.getBoundingClientRect(); Sky.burst(r.left + r.width / 2, r.top + r.height / 2); }
async function boot() {
  api = window.pywebview.api;
  setCalm(localStorage.getItem("astra-calm") === "1");
  await loadState();
  try { applyTheme(); } catch (e) { console.error(e); }
  if (window.Sky) { try { Sky.flare($("#drop")); Sky.flare($("#setupDrop")); } catch (e) { console.error(e); } }
  document.body.classList.toggle("public", !!state.public);
  (state.running || []).forEach(id => running.add(id));
  if (!state.config.setup_done) {
    const m = state.models.find(x => x.name === state.config.model.name);
    if (m) $("#setupModelName").textContent = `${m.name}, about ${m.size}`;
    const v = state.vision_models.find(x => x.name === state.config.model.vision);
    if (v) $("#setupVisionName").textContent = `${v.name}, about ${v.size}`;
    $("#setup").classList.remove("hidden");
  }
  setEngine(state.engine_up ? "ready" : "loading");
  setGreeting();
  $("#navFriend").textContent = state.config.friend.name || "Nova";
  refreshConvs();
  (state.requests || []).filter(r => !String(r.conv || "").startsWith("nova-task-") && r.conv !== FRIEND).forEach(addRequest);
  if (state.config.setup_done) showWelcome(); else input.focus();
}
if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);


/* ---------------- start over ---------------- */
const resetDlg = $("#resetDlg");
$("#resetOpen").onclick = () => { $("#resetPhrase").value = ""; $("#resetWs").checked = false; $("#resetGo").disabled = true; resetDlg.classList.remove("hidden"); $("#resetCancel").focus(); };
$("#resetCancel").onclick = () => resetDlg.classList.add("hidden");
resetDlg.addEventListener("keydown", e => { if (e.key === "Escape") resetDlg.classList.add("hidden"); });
$("#resetPhrase").oninput = () => { $("#resetGo").disabled = $("#resetPhrase").value.trim().toUpperCase() !== "DELETE"; };
$("#resetGo").onclick = async () => {
  if (!confirm("Last check: delete all of AstraNova's chats, memories, settings and connections?")) return;
  resetDlg.classList.add("hidden");
  const fx = $("#resetFx"), txt = $("#resetFxText"), wait = ms => new Promise(r => setTimeout(r, ms));
  fx.classList.remove("hidden"); await wait(30); fx.classList.add("on");        // the world goes dark
  await wait(1700); fx.classList.add("out");                                     // Astra fades out
  const res = await api.reset_everything($("#resetPhrase").value, $("#resetWs").checked);
  await wait(2300);
  if (!res || !res.ok) { fx.classList.remove("on", "out"); setTimeout(() => fx.classList.add("hidden"), 1600); return toast((res && res.error) || "Reset failed"); }
  try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}
  await wait(900); fx.classList.remove("out"); fx.classList.add("relight");     // and reignites
  txt.textContent = "ASTRANOVA"; txt.classList.add("on");
  await wait(2800); location.reload();
};

/* ---------------- speech to text ---------------- */
const sttInput = t => $(t === "friend" ? "#finput" : "#input");
let sttTarget = null, sttToastShown = false;
async function sttToggle(target) {
  const btn = $(`.mic[data-stt="${target}"]`), box = sttInput(target);
  if (sttTarget === target && btn.classList.contains("rec")) { api.stt_stop(); return; }
  box.focus();
  const r = await api.stt_start(target);
  if (r && r.mode === "whisper") { sttTarget = target; btn.classList.add("busy"); }
  if (r && r.mode === "mac") toast("Press the Fn (globe) key twice to talk. Your Mac types what you say right here.", { ms: 5000 });
}
$$(".mic").forEach(b => b.onclick = () => sttToggle(b.dataset.stt));
addEventListener("keydown", e => {
  if (e.ctrlKey && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "m" && (viewActive("chat") || viewActive("friend"))) {
    e.preventDefault(); sttToggle(viewActive("friend") ? "friend" : "astra");
  }
});
handlers.stt = e => {
  const btn = $(`.mic[data-stt="${e.target}"]`); if (!btn) return;
  if (e.state === "installing") { if (!sttToastShown) { sttToastShown = true; toast("Setting up speech recognition (once)…"); } return; }
  if (e.state === "listening") { btn.classList.remove("busy"); btn.classList.add("rec"); return; }
  if (e.state === "level") { btn.style.setProperty("--lvl", e.level); return; }
  if (e.state === "transcribing") { btn.classList.remove("rec"); btn.classList.add("busy"); btn.style.setProperty("--lvl", 0); return; }
  btn.classList.remove("rec", "busy"); btn.style.setProperty("--lvl", 0); sttTarget = null;
  if (e.state === "error") return toast(e.text);
  const t = (e.text || "").trim(); if (!t) return;
  const box = sttInput(e.target); box.value = (box.value.trim() ? box.value.trimEnd() + " " : "") + t; autosize(box); box.focus();
  if (e.auto_send) (e.target === "friend" ? fsend : send)();
};
function fillStt() {
  const c = state.config.voice_input || {};
  $("#sttEngine").value = c.engine || "windows"; $("#sttModel").value = c.model || "base"; $("#sttSend").checked = !!c.auto_send;
}
const saveStt = () => save("voice_input", { engine: $("#sttEngine").value, model: $("#sttModel").value, auto_send: $("#sttSend").checked });
["#sttEngine", "#sttModel", "#sttSend"].forEach(s => $(s).onchange = saveStt);
$("#sttGet").onclick = async () => { await saveStt(); api.stt_prepare(); toast("Downloading speech recognition"); };

/* ---------------- downloaded models ---------------- */
const gb = b => (b / 1073741824).toFixed(b > 10737418240 ? 0 : 1) + " GB";
function renderLocalModels(r) {
  const box = $("#localModels"); if (!r) return;
  if (!r.models.length) { box.innerHTML = `<p class="sub small">No models downloaded yet, or the AI engine isn't running.</p>`; return; }
  box.innerHTML = r.models.map(m => `<div class="lm"><span class="lmn">${esc(m.name)}</span><span class="lms">${gb(m.size)}</span>
    ${m.in_use ? `<em class="mtag ok">in use</em>` : `<button class="btn-ghost sm" data-del="${esc(m.name)}">Delete</button>`}</div>`).join("");
  $("#mClean").textContent = r.unused_bytes ? `Delete unused models (${gb(r.unused_bytes)})` : "Delete unused models";
  $("#mClean").disabled = !r.unused_bytes;
  $$("[data-del]", box).forEach(b => b.onclick = async () => {
    if (!confirmInline(b, "Sure? Delete")) return;
    b.disabled = true; const res = await api.delete_model(b.dataset.del);
    if (!res.ok) { b.disabled = false; return toast(res.error); }
    toast(`Deleted ${b.dataset.del}`); renderLocalModels(res); loadState().then(renderModels);
  });
}
$("#mClean").onclick = async () => {
  if (!confirmInline($("#mClean"), "Delete them all? Click again")) return;
  $("#mClean").disabled = true; const r = await api.delete_unused_models();
  toast(r.deleted.length ? `Deleted ${r.deleted.length} model${r.deleted.length > 1 ? "s" : ""}` : "Nothing to delete");
  renderLocalModels(r); loadState().then(renderModels);
};

/* ---------------- learned moods and Nova's style ---------------- */
function hexOf(c) { return "#" + c.map(v => Math.max(0, Math.min(255, Math.round(v * .55))).toString(16).padStart(2, "0")).join(""); }
function registerCustomMoods(list) {
  Object.keys(MOOD_PALETTE).forEach(k => { if (MOOD_PALETTE[k] && String(MOOD_PALETTE[k]).startsWith("c_")) delete MOOD_PALETTE[k]; });
  (list || (state.config.personal || {}).custom_moods || []).forEach(m => {
    const key = "c_" + m.name;
    PALETTES[key] = { name: m.name[0].toUpperCase() + m.name.slice(1), lav: m.colors[0], peri: m.colors[1], rose: m.colors[2], deep: hexOf(m.colors[0]), mood: true };
    MOOD_PALETTE[m.name] = key;
  });
}
function renderLearned() {
  const p = state.config.personal || {};
  $("#pAdapt").checked = p.adapt !== false; $("#pMoods").checked = p.learn_moods !== false;
  $("#pStyle").textContent = p.learned_style || "Nothing yet. She learns after you've chatted a bit.";
  const ms = p.custom_moods || [];
  $("#learnedMoods").innerHTML = ms.length ? ms.map(m => `<span class="lmood" title="${esc(m.description || "")}"><i style="background:linear-gradient(120deg, rgb(${m.colors[0]}), rgb(${m.colors[1]}) 55%, rgb(${m.colors[2]}))"></i>${esc(m.name)}<button aria-label="Forget ${esc(m.name)}" data-forget="${esc(m.name)}">×</button></span>`).join("")
    : `<p class="sub small">No learned moods yet.</p>`;
  $$("[data-forget]").forEach(b => b.onclick = async () => { state.config = await api.personal_forget(b.dataset.forget); registerCustomMoods(); renderLearned(); renderMoodMap(); });
}
$("#pAdapt").onchange = () => save("personal", { adapt: $("#pAdapt").checked });
$("#pMoods").onchange = () => save("personal", { learn_moods: $("#pMoods").checked });
$("#pForgetStyle").onclick = async () => { if (!confirmInline($("#pForgetStyle"), "Sure? Forget")) return; state.config = await api.personal_forget("style"); renderLearned(); toast("Forgotten"); };
$("#pLearn").onclick = async () => {
  $("#pLearn").disabled = true; $("#pLearn").textContent = "Learning…";
  const r = await api.personal_learn_now(); $("#pLearn").disabled = false; $("#pLearn").textContent = "Learn now";
  if (!r.ok) return toast(r.why === "no messages yet" ? "Chat a bit first, then try again" : "Couldn't learn: " + r.why);
  await loadState(); registerCustomMoods(); renderLearned(); renderMoodMap();
  toast(r.added && r.added.length ? "Learned, and a new mood: " + r.added.join(", ") : "Updated what Nova knows about how you talk");
};
handlers.custom_moods = e => { (state.config.personal = state.config.personal || {}).custom_moods = e.moods; registerCustomMoods(e.moods); if ($("#moodMap")) renderMoodMap(); };

// ---- updates (public edition) ----
function renderUpdate() {
  const u = state.update || {}; const line = $("#updLine"); if (!line) return;
  $("#updAuto").checked = (state.config.updates || {}).auto !== false;
  const words = { checking: "Looking for a new version...", downloading: `Downloading version ${u.version}...`,
    ready: `Version ${u.version} is ready`, installing: "Installing...", error: "Couldn't check right now, it tries again later" };
  line.textContent = words[u.status] || ("Up to date" + (u.checked ? ", checked " + hm(u.checked) : ""));
  $("#updGo").classList.toggle("hidden", u.status !== "ready");
}
$("#updAuto").addEventListener("change", async e => { state.config = await api.save_settings("updates", { auto: e.target.checked }); });
$("#updCheck").addEventListener("click", async () => { state.update = { status: "checking" }; renderUpdate(); state.update = await api.update_check(); renderUpdate(); });
$("#updGo").addEventListener("click", () => { $("#updLine").textContent = "Installing..."; api.update_restart(); });
