/* netmon - mapa en vivo (Cytoscape.js + Server-Sent Events). Sin historial. */
"use strict";

const COLORS = {
  ok: "#2ecc71", warning: "#f5b014", critical: "#ff4d4f", down: "#ff4d4f",
  admin_down: "#6b7785", unknown: "#4a5563",
  up: "#2ecc71", stale: "#f5b014", unreachable: "#ff4d4f", no_snmp: "#6b7785",
};
const STATUS_TEXT = {
  ok: "Normal", warning: "Utilización elevada", critical: "Utilización alta",
  down: "Caído (oper down)", admin_down: "Apagado (admin down)", unknown: "Sin datos",
  up: "Responde SNMP", stale: "Dato atrasado", unreachable: "Sin respuesta", no_snmp: "Sin SNMP",
};
const KIND = {   // tipo visual por fabricante
  cloud: { glyph: "WAN", shape: "ellipse", w: 110, h: 68 },
  mikrotik: { glyph: "RTR", shape: "ellipse", w: 86, h: 86 },
  fortinet: { glyph: "FW", shape: "hexagon", w: 96, h: 84 },
  aruba: { glyph: "SW", shape: "round-rectangle", w: 108, h: 60 },
  linux: { glyph: "SRV", shape: "round-rectangle", w: 74, h: 74 },
};

let cy = null;
let snap = null;
let selected = null;          // {type: "node"|"edge", id}
let events = [];              // linea de tiempo (mas nuevo primero), solo en memoria
const expanded = new Set();   // eventos con varbinds desplegados

/* ---------- utilidades ---------- */
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBps(v) {
  if (v === null || v === undefined) return "–";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + " Mb/s";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + " kb/s";
  return v.toFixed(0) + " b/s";
}
function fmtUptime(s) {
  if (s === null || s === undefined) return "–";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
        m = Math.floor(s % 3600 / 60), ss = s % 60;
  return (d ? d + "d " : "") + `${h}h ${m}m ${ss}s`;
}
function ago(epoch) {
  if (!epoch) return "nunca";
  const s = Math.max(0, Math.round(Date.now() / 1000 - epoch));
  return s < 60 ? `hace ${s} s` : `hace ${Math.floor(s / 60)} min ${s % 60} s`;
}
const badge = (st) => `<span class="badge ${esc(st)}">${esc(STATUS_TEXT[st] || st)}</span>`;
const stClass = (v) => v === "up" ? "st-up" : (v === "down" ? "st-down" : "st-off");

/* ---------- mapa ---------- */
function buildMap(s) {
  const els = [];
  for (const d of s.devices) {
    const k = KIND[d.vendor] || KIND.linux;
    els.push({
      group: "nodes",
      data: { id: d.id, glyph: k.glyph, shape: k.shape, w: k.w, h: k.h },
      position: d.pos ? { x: d.pos.x, y: d.pos.y } : undefined,
    });
  }
  for (const l of s.links) {
    els.push({ group: "edges", data: { id: l.id, source: l.a.device, target: l.b.device } });
  }

  cy = cytoscape({
    container: document.getElementById("map"),
    elements: els,
    layout: s.devices.every((d) => d.pos) ? { name: "preset", padding: 30 } : { name: "breadthfirst", padding: 30 },
    minZoom: 0.4, maxZoom: 2.5, wheelSensitivity: 0.2,
    style: [
      { selector: "node", style: {
        "shape": "data(shape)", "width": "data(w)", "height": "data(h)",
        "background-color": "#18212c", "border-width": 3, "border-color": "#6b7785",
        "label": "data(glyph)", "color": "#d7e0ea", "font-family": "Consolas, monospace",
        "font-size": 18, "font-weight": 700, "text-valign": "center", "text-halign": "center",
        "border-width": 4,
      }},
      { selector: "edge", style: {
        "width": 6, "line-color": "#4a5563", "curve-style": "bezier",
        "label": "data(label)", "font-size": 15, "font-family": "Consolas, monospace", "color": "#d7e0ea",
        "text-background-color": "#0b1016", "text-background-opacity": 0.9, "text-background-padding": 2,
        "source-label": "data(srcLabel)", "target-label": "data(dstLabel)",
        "source-text-offset": 44, "target-text-offset": 44, "text-rotation": "none",
        "text-margin-y": -14,                                  // % de utilizacion arriba de la linea
        "source-text-margin-y": 14, "target-text-margin-y": 14, // ifName debajo de la linea
        "source-font-size": 12, "target-font-size": 12, "color": "#d7e0ea",
      }},
      { selector: "edge[status = 'admin_down']", style: { "line-style": "dashed", "line-dash-pattern": [12, 8] } },
      { selector: "edge[status = 'down']", style: { "line-style": "dashed", "line-dash-pattern": [14, 7] } },
      { selector: "edge[status = 'unknown']", style: { "line-style": "dotted" } },
      { selector: ":selected", style: { "overlay-color": "#38bdf8", "overlay-opacity": 0.18, "overlay-padding": 6 } },
    ],
  });

  new ResizeObserver(() => { cy.resize(); cy.fit(undefined, 30); }).observe(document.getElementById("map"));
  document.getElementById("fit-btn").addEventListener("click", () => cy.fit(undefined, 30));
  document.getElementById("legend-btn").addEventListener("click", () =>
    document.querySelector(".legend").classList.toggle("collapsed"));
  cy.on("tap", "node", (e) => select("node", e.target.id()));
  cy.on("tap", "edge", (e) => select("edge", e.target.id()));
  cy.on("tap", (e) => { if (e.target === cy) select(null); });
}

function paint(s) {
  const devs = Object.fromEntries(s.devices.map((d) => [d.id, d]));
  cy.batch(() => {
    for (const d of s.devices) {
      const n = cy.getElementById(d.id);
      n.style("border-color", COLORS[d.status] || "#6b7785");
    }
    for (const l of s.links) {
      const e = cy.getElementById(l.id);
      const nameOf = (end) => (end.iface && end.iface.name) ? end.iface.name : "";
      e.data({
        status: l.status,
        label: l.utilization_pct === null ? "" : `${l.utilization_pct.toFixed(0)}%`,
        srcLabel: nameOf(l.a), dstLabel: nameOf(l.b),
      });
      e.style("line-color", COLORS[l.status] || "#4a5563");
      e.style("width", l.status === "critical" ? 10 : (l.status === "warning" ? 8 : 6));
    }
  });
  drawCaptions(devs);   // nombre e IP debajo de cada nodo
}

/* Las etiquetas de nombre se dibujan como nodos auxiliares sin interaccion. */
function drawCaptions(devs) {
  for (const id of Object.keys(devs)) {
    const d = devs[id];
    const capId = `cap-${id}`;
    const n = cy.getElementById(id);
    let cap = cy.getElementById(capId);
    if (cap.empty()) {
      cap = cy.add({ group: "nodes", data: { id: capId }, position: { x: 0, y: 0 }, selectable: false, grabbable: false });
      cap.style({
        "width": 1, "height": 1, "background-opacity": 0, "border-width": 0, "events": "no",
        "text-wrap": "wrap", "text-valign": "bottom", "text-halign": "center", "font-size": 15,
        "color": "#d7e0ea", "text-background-color": "#0b1016", "text-background-opacity": 0.85,
        "text-background-padding": 3, "font-family": "Segoe UI, system-ui, sans-serif",
      });
    }
    cap.style("label", d.name);   // en el mapa solo el nombre; el detalle va en la ficha
    cap.position({ x: n.position("x"), y: n.position("y") + n.height() / 2 + 2 });
  }
  cy.off("position.captions");
  cy.on("position.captions", "node", (e) => {
    const cap = cy.getElementById(`cap-${e.target.id()}`);
    if (!cap.empty()) cap.position({ x: e.target.position("x"), y: e.target.position("y") + e.target.height() / 2 + 2 });
  });
}

/* ---------- panel lateral ---------- */
function select(type, id) {
  selected = type ? { type, id } : null;
  renderPanel();
}

function renderPanel() {
  const empty = document.getElementById("panel-empty");
  const body = document.getElementById("panel-body");
  if (!snap) return;
  renderSummary();
  if (!selected) { empty.hidden = false; body.hidden = true; return; }
  empty.hidden = true; body.hidden = false;
  body.innerHTML = selected.type === "node" ? deviceHtml(selected.id) : linkHtml(selected.id);
  body.querySelector(".close")?.addEventListener("click", () => { cy.elements().unselect(); select(null); });
}

function renderSummary() {
  const c = (arr, key) => arr.reduce((a, x) => (a[x[key]] = (a[x[key]] || 0) + 1, a), {});
  const dv = c(snap.devices.filter((d) => d.snmp), "status");
  const lk = c(snap.links, "status");
  const tile = (n, l, cls) => `<div class="tile"><div class="n" style="color:${COLORS[cls] || "#d7e0ea"}">${n || 0}</div><div class="l">${l}</div></div>`;
  document.getElementById("summary").innerHTML = `
    <h3>Equipos SNMP</h3>
    <div class="summary-grid">
      ${tile(dv.up, "Responden", "up")}${tile(dv.stale, "Dato atrasado", "stale")}
      ${tile(dv.unreachable, "Sin respuesta", "unreachable")}${tile(dv.unknown, "Sin sondear aún", "unknown")}
    </div>
    <h3>Enlaces</h3>
    <div class="summary-grid">
      ${tile(lk.ok, "Normal", "ok")}${tile(lk.warning, "Elevada", "warning")}
      ${tile(lk.critical, "Alta", "critical")}${tile((lk.down || 0), "Caídos", "down")}
      ${tile(lk.admin_down, "Apagados", "admin_down")}${tile(lk.unknown, "Sin datos", "unknown")}
    </div>`;
}

function deviceHtml(id) {
  const d = snap.devices.find((x) => x.id === id);
  if (!d) return "";
  const head = `<div class="panel-head"><h2>${esc(d.name)}</h2><button class="close">✕</button></div>${badge(d.status)}`;
  if (!d.snmp) {
    return `${head}<dl class="kv" style="margin-top:12px">
      <dt>Dirección IP</dt><dd>${esc(d.ip || "–")}</dd>
      <dt>Tipo</dt><dd>${esc(d.vendor)}</dd></dl>
      <p class="hint">Este equipo no se consulta por SNMP. Su disponibilidad se medirá con ICMP en un módulo posterior.</p>`;
  }
  const rows = (d.interfaces || []).map((i) => `
    <tr><td>${esc(i.name ?? "?")}</td>
      <td class="${stClass(i.admin)}">${esc(i.admin ?? "–")}</td>
      <td class="${stClass(i.oper)}">${esc(i.oper ?? "–")}${i.source === "trap" ? ' <span class="trap-mark" title="Informado por trap, pendiente de confirmar por sondeo">T</span>' : ""}</td>
      <td class="num">${fmtBps(i.in_bps)}</td><td class="num">${fmtBps(i.out_bps)}</td>
      <td>${i.counter ? i.counter + "b" : "–"}</td></tr>`).join("");
  return `${head}
    <dl class="kv" style="margin-top:12px">
      <dt>Dirección IP</dt><dd>${esc(d.ip)}</dd>
      <dt>sysName</dt><dd>${esc(d.sys_name ?? "–")}</dd>
      <dt>Fabricante</dt><dd>${esc(d.vendor)}</dd>
      <dt>Uptime</dt><dd>${fmtUptime(d.uptime_s)}</dd>
      <dt>Último dato</dt><dd>${ago(d.last_ok)}</dd>
      <dt>Respuesta SNMP</dt><dd>${d.poll_ms === null ? "–" : d.poll_ms + " ms"}</dd>
      ${d.last_error ? `<dt>Error</dt><dd class="err">${esc(d.last_error)}</dd>` : ""}
    </dl>
    ${resourcesHtml(d)}
    <h3>Interfaces monitoreadas</h3>
    <table class="ifs"><thead><tr><th>ifName</th><th>Admin</th><th>Oper</th><th>Entrada</th><th>Salida</th><th>Cont.</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="6">Sin datos</td></tr>`}</tbody></table>`;
}

function fmtBytes(b) {
  if (b === null || b === undefined) return null;
  return b >= 1073741824 ? (b / 1073741824).toFixed(2) + " GiB" : (b / 1048576).toFixed(0) + " MiB";
}

function resBar(label, pct, extra) {
  const NA = `<span class="hint">no disponible</span>`;
  if (pct === null || pct === undefined) return `<dt>${label}</dt><dd>${NA}</dd>`;
  const color = pct >= 80 ? COLORS.critical : (pct >= 50 ? COLORS.warning : COLORS.ok);
  return `<dt>${label}</dt><dd>${pct} %${extra ? ` <span class="hint">· ${extra}</span>` : ""}
    <div class="bar"><span style="width:${Math.min(100, pct)}%;background:${color}"></span></div></dd>`;
}

function resourcesHtml(d) {
  const r = d.resources;
  if (!r) return `<h3>CPU y memoria</h3><p class="hint">Todavía sin consultar (cada 30 s).</p>`;
  if (r.error) return `<h3>CPU y memoria</h3><p class="err">No disponible: ${esc(r.error)}</p>`;
  // Total de RAM: el informado por SNMP; si el equipo no lo informa, el del inventario (marcado con *)
  let memExtra = fmtBytes(r.mem_total_bytes) || "";
  if (!memExtra && d.mem_total_mb_inventory) {
    memExtra = `<span title="Valor del inventario (RAM asignada en GNS3); el equipo no lo informa por SNMP">${d.mem_total_mb_inventory} MiB*</span>`;
  }
  return `<h3>CPU y memoria</h3>
    <dl class="kv">${resBar("CPU", r.cpu_pct, "")}${resBar("Memoria", r.mem_pct, memExtra)}
      <dt>Consultado</dt><dd>${ago(r.ts)}</dd></dl>`;
}

function linkHtml(id) {
  const l = snap.links.find((x) => x.id === id);
  if (!l) return "";
  const devName = (did) => (snap.devices.find((d) => d.id === did) || {}).name || did;
  const pct = l.utilization_pct;
  const bar = pct === null ? "" :
    `<div class="bar"><span style="width:${Math.min(100, pct)}%;background:${COLORS[l.status]}"></span></div>`;
  const end = (e) => {
    const i = e.iface;
    if (e.ifindex === null) return `<div class="end"><div class="t">${esc(devName(e.device))}</div><span class="hint">Extremo sin SNMP</span></div>`;
    if (!i) return `<div class="end"><div class="t">${esc(devName(e.device))} · ifIndex ${e.ifindex}</div><span class="hint">Sin datos (equipo ${esc(STATUS_TEXT[e.device_status] || e.device_status || "sin sondear")})</span></div>`;
    if (!i.exists) return `<div class="end"><div class="t">${esc(devName(e.device))} · ifIndex ${e.ifindex}</div><span class="err">La interfaz no existe en el equipo</span></div>`;
    return `<div class="end"><div class="t">${esc(devName(e.device))} · ${esc(i.name)} <span class="hint">(ifIndex ${i.ifindex})</span></div>
      <dl class="kv"><dt>Admin / Oper</dt><dd><span class="${stClass(i.admin)}">${esc(i.admin)}</span> / <span class="${stClass(i.oper)}">${esc(i.oper)}</span>${i.source === "trap" ? ' <span class="trap-mark" title="Informado por trap">T</span>' : ""}</dd>
      <dt>Entrada</dt><dd>${fmtBps(i.in_bps)}</dd><dt>Salida</dt><dd>${fmtBps(i.out_bps)}</dd>
      <dt>Contador</dt><dd>${i.counter ? i.counter + " bits" : "–"}</dd></dl></div>`;
  };
  return `<div class="panel-head"><h2>Enlace ${esc(l.id)}</h2><button class="close">✕</button></div>${badge(l.status)}
    <dl class="kv" style="margin-top:12px">
      <dt>Utilización</dt><dd>${pct === null ? "–" : pct.toFixed(1) + " %"}</dd>
      <dt>Capacidad ref.</dt><dd>${fmtBps(l.capacity_bps)}</dd>
    </dl>${bar}
    <h3>Extremos</h3><div class="ends">${end(l.a)}${end(l.b)}</div>`;
}

/* ---------- linea de tiempo de eventos ---------- */
const TYPE_TEXT = {
  linkDown: "linkDown", linkUp: "linkUp", configChange: "cambio config.", coldStart: "coldStart",
  warmStart: "warmStart", authFailure: "fallo auth.", other: "otro",
};
const CONF_TEXT = {
  coincide: "✓ sondeo coincide", difiere: "≠ sondeo difiere", sin_respuesta: "sin respuesta",
  no_monitoreada: "interfaz no monitoreada",
};
const hhmmss = (epoch) => new Date(epoch * 1000).toLocaleTimeString("es-AR", { hour12: false });

function onEvent(ev, isNew) {
  const i = events.findIndex((e) => e.id === ev.id);
  if (i >= 0) events[i] = ev; else { events.unshift(ev); events = events.slice(0, 200); }
  renderTimeline(isNew && i < 0 ? ev.id : null);
  if (isNew && i < 0 && ev.link && cy) flashLink(ev.link, ev.type);
}

function flashLink(id, type) {
  const e = cy.getElementById(id);
  if (e.empty()) return;
  const color = type === "linkDown" ? "#ff4d4f" : (type === "linkUp" ? "#2ecc71" : "#38bdf8");
  e.style({ "overlay-color": color, "overlay-opacity": 0.35, "overlay-padding": 10 });
  setTimeout(() => e.removeStyle("overlay-color overlay-opacity overlay-padding"), 2500);
}

function renderTimeline(freshId) {
  const body = document.getElementById("tl-body");
  if (!events.length) {
    body.innerHTML = `<tr class="tl-empty"><td colspan="8">Sin eventos recibidos.</td></tr>`;
    return;
  }
  body.innerHTML = events.map((e) => {
    const iface = e.ifindex === null || e.ifindex === undefined ? "–" :
      `${esc(e.ifname || "?")} <span class="hint">(${e.ifindex})</span>`;
    const st = e.new_oper ? `${esc(e.prev_oper ?? "?")} → ${esc(e.new_oper)}` : "–";
    const conf = e.confirmation ? `<span class="conf-${esc(e.confirmation)}">${esc(CONF_TEXT[e.confirmation] || e.confirmation)}</span>`
      : (e.new_oper ? `<span class="conf-pending">pendiente…</span>` : "–");
    const flags = (e.duplicates ? `<span class="flag" title="Traps idénticos recibidos dentro de 2 s">×${e.duplicates + 1}</span>` : "")
      + (e.out_of_order ? `<span class="flag" title="sysUpTime del trap menor al del trap anterior">fuera de orden</span>` : "");
    const who = e.device_name ? esc(e.device_name) : `<span class="err">desconocido ${esc(e.source_ip)}</span>`;
    const row = `<tr class="ev${e.id === freshId ? " fresh" : ""}" data-id="${e.id}" title="${esc(e.description)} · OID ${esc(e.trap_oid)}">
      <td>${hhmmss(e.ts)}</td><td>${who}</td>
      <td><span class="ev-type ev-${esc(e.type)}">${esc(TYPE_TEXT[e.type] || e.type)}</span></td>
      <td>${iface}</td><td>${st}</td><td>${esc(e.link || "–")}</td><td>${conf}</td><td>${flags}</td></tr>`;
    const vb = expanded.has(e.id) ? `<tr class="vb"><td colspan="8">${esc(e.version)} · origen ${esc(e.source_ip)} · trap ${esc(e.trap_oid)}${e.agent_uptime !== null ? " · sysUpTime " + e.agent_uptime : ""}\n${(e.varbinds || []).map(([o, v]) => esc(o) + " = " + esc(v)).join("\n")}</td></tr>` : "";
    return row + vb;
  }).join("");
  body.querySelectorAll("tr.ev").forEach((tr) => tr.addEventListener("click", () => {
    const id = Number(tr.dataset.id);
    expanded.has(id) ? expanded.delete(id) : expanded.add(id);
    renderTimeline(null);
  }));
}

async function clearEvents() {
  const btn = document.getElementById("tl-clear");
  btn.disabled = true;
  try {
    const r = await fetch("/api/events/clear", { method: "POST" });
    if (r.ok) { events = []; expanded.clear(); renderTimeline(null); }
  } finally { btn.disabled = false; }
}
document.getElementById("tl-clear").addEventListener("click", clearEvents);

function renderTrapStats() {
  const t = snap && snap.traps;
  const el = document.getElementById("trap-stats");
  if (!t) return;
  if (!t.enabled) { el.textContent = "Receptor de traps deshabilitado"; return; }
  el.textContent = `Recibidos ${t.received} · eventos ${t.events} · duplicados ${t.duplicates}` +
    ` · descartados ${t.bad_community + t.malformed} · origen desconocido ${t.unknown_source}`;
}

/* ---------- cabecera ---------- */
function renderHeader() {
  if (!snap) return;
  document.getElementById("updated").textContent = `Actualizado ${ago(snap.ts)}`;
  document.getElementById("interval").textContent = `Sondeo cada ${snap.interval_s} s`;
  const t = snap.thresholds;
  document.getElementById("lg-warn").textContent = `Elevada (≥ ${t.warning_pct}%)`;
  document.getElementById("lg-crit").textContent = `Alta (≥ ${t.critical_pct}%)`;
}

function setConn(state) {
  const el = document.getElementById("conn");
  el.className = "pill " + (state === "live" ? "live" : (state === "lost" ? "lost" : ""));
  document.getElementById("conn-text").textContent =
    state === "live" ? "En vivo" : (state === "lost" ? "Reconectando…" : "Conectando…");
}

/* ---------- datos en vivo ---------- */
function onSnapshot(s) {
  if (!s || !s.devices || !s.devices.length) return;
  const first = !cy;
  snap = s;
  if (first) buildMap(s);
  paint(s);
  renderHeader();
  renderTrapStats();
  renderPanel();
}

function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("open", () => setConn("live"));
  es.addEventListener("snapshot", (ev) => { setConn("live"); onSnapshot(JSON.parse(ev.data)); });
  es.addEventListener("events", (ev) => { events = JSON.parse(ev.data); renderTimeline(null); });
  es.addEventListener("trap", (ev) => onEvent(JSON.parse(ev.data), true));
  es.addEventListener("error", () => setConn("lost"));   // EventSource reintenta solo
}

fetch("/api/snapshot").then((r) => r.json()).then(onSnapshot).catch(() => {});
connect();
setInterval(() => {            // refresca los "hace N s" sin esperar al servidor
  renderHeader();
  if (snap && cy) drawCaptions(Object.fromEntries(snap.devices.map((d) => [d.id, d])));
  if (selected && selected.type === "node") renderPanel();
}, 1000);
