// Panel web de PePump. Recibe por WebSocket el estado de todas las
// posiciones cada segundo y los logs en el momento en que salen; las
// órdenes (cerrar una posición, cerrar todas) van por POST a /api.
//
// OJO: el nombre y el símbolo del token vienen de la metadata on-chain, que
// escribe quien creó el token: todo texto se pone con textContent (nunca
// innerHTML) para que un nombre malicioso no pueda inyectar HTML.
"use strict";

const MAX_LOGS = 3000;

const state = {
  live: null,
  positions: [],
  logs: [],
  selectedId: null,
  logFilter: "",
};

// ------------------------------------------------------------------ formato

const SUBSCRIPT = "₀₁₂₃₄₅₆₇₈₉";

function fmtPrice(p) {
  if (p === null || p === undefined || !isFinite(p)) return "—";
  if (p >= 1) return p.toFixed(4);
  if (p >= 0.001) return p.toPrecision(4);
  // 0.0000001234 -> 0.0₆1234 (ceros contados en subíndice, como en los DEX)
  const [mant, exp] = p.toExponential(3).split("e");
  const zeros = -parseInt(exp, 10) - 1;
  const digits = mant.replace(".", "").replace(/0+$/, "") || "0";
  const sub = String(zeros).split("").map((d) => SUBSCRIPT[+d]).join("");
  return `0.0${sub}${digits}`;
}

function fmtSigned(x, decimals, suffix = "") {
  if (x === null || x === undefined || !isFinite(x)) return "—";
  const abs = Math.abs(x).toFixed(decimals);
  if (Number(abs) === 0) return `${abs}${suffix}`;
  return `${x > 0 ? "+" : "−"}${abs}${suffix}`;
}

const fmtPct = (x) => fmtSigned(x, 2, " %");
const fmtSolSigned = (x) => fmtSigned(x, 4);
const fmtSol = (x) => (x === null || x === undefined ? "—" : x.toFixed(4));

function toneClass(x) {
  if (x === null || x === undefined || !isFinite(x) || Math.abs(x) < 1e-12) return "";
  return x > 0 ? "up" : "down";
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || seconds < 0) return "—";
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min ${r} s`;
  return `${r} s`;
}

function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString("es-ES", { hour12: false });
}

function shortMint(mint) {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}

function tokenTitle(snap) {
  if (snap.token_symbol) return `$${snap.token_symbol}`;
  if (snap.token_name) return snap.token_name;
  return shortMint(snap.mint);
}

function logTag(mint) {
  if (!mint) return "";
  const snap = state.positions.find((p) => p.mint === mint);
  return snap && snap.token_symbol ? `$${snap.token_symbol} ${mint.slice(0, 4)}` : shortMint(mint);
}

// ---------------------------------------------------------------- estado

function statusOf(snap) {
  const pos = snap.position;
  if (snap.phase === "open" && !snap.running) {
    return { icon: "⚠", label: "ABIERTA SIN VIGILAR", tone: "critical" };
  }
  if (snap.close_requested && !["closed", "stopped"].includes(snap.phase)) {
    return { icon: "◐", label: "Cerrando", tone: "warn" };
  }
  switch (snap.phase) {
    case "starting": return { icon: "◌", label: "Arrancando", tone: "accent" };
    case "connecting": return { icon: "◌", label: "Conectando", tone: "accent" };
    case "waiting_entry": return { icon: "⏳", label: "Esperando entrada", tone: "accent" };
    case "buying": return { icon: "◐", label: "Comprando", tone: "accent" };
    case "open":
      return pos && pos.armed
        ? { icon: "▲", label: "Trailing armado", tone: "good" }
        : { icon: "●", label: "Esperando activación", tone: "accent" };
    case "closing": return { icon: "◐", label: "Vendiendo", tone: "warn" };
    case "closed": return { icon: "✓", label: "Cerrada", tone: "" };
    case "stopped": return { icon: "■", label: "Terminada sin posición", tone: "" };
    default: return { icon: "·", label: snap.phase, tone: "" };
  }
}

// Valor de la posición si se vendiera ahora al precio actual, menos lo que
// costó entrar. No descuenta las comisiones de la venta.
function openPnlSol(snap) {
  const pos = snap.position;
  if (!pos || pos.closed || snap.latest_price === null) return null;
  return pos.token_amount * snap.latest_price - pos.sol_amount;
}

// Próximo nivel que dispara algo: la venta (armado) o la activación.
function nextLevel(snap) {
  const pos = snap.position;
  const price = snap.latest_price;
  if (pos && !pos.closed) {
    if (pos.armed && pos.trailing_stop_price) {
      return { kind: "venta ≤", value: pos.trailing_stop_price,
               dist: price ? (pos.trailing_stop_price / price - 1) * 100 : null };
    }
    return { kind: "activa ≥", value: pos.activation_price,
             dist: price ? (pos.activation_price / price - 1) * 100 : null };
  }
  if (!pos && snap.entry_target_price) {
    return { kind: "compra ≤", value: snap.entry_target_price,
             dist: price ? (snap.entry_target_price / price - 1) * 100 : null };
  }
  return null;
}

// ------------------------------------------------------------------- DOM

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function renderMode() {
  const badge = document.getElementById("mode");
  if (state.live === null) return;
  badge.hidden = false;
  badge.textContent = state.live ? "⚠ MODO REAL" : "Simulado";
  badge.classList.toggle("live", state.live);
  document.title = state.live ? "PePump · REAL" : "PePump · simulado";
}

function renderTiles() {
  const ps = state.positions;
  const running = ps.filter((p) => p.running).length;
  const open = ps.filter((p) => p.position && !p.position.closed);
  const closed = ps.filter((p) => p.position && p.position.closed);

  document.getElementById("t-active").textContent = String(running);
  document.getElementById("t-active-sub").textContent = `de ${ps.length} en esta sesión`;

  const invested = open.reduce((acc, p) => acc + p.position.sol_amount, 0);
  document.getElementById("t-invested").textContent = open.length ? fmtSol(invested) : "—";

  const pnls = open.map(openPnlSol).filter((x) => x !== null);
  const openPnl = pnls.reduce((a, b) => a + b, 0);
  const tOpen = document.getElementById("t-open-pnl");
  tOpen.textContent = pnls.length ? fmtSolSigned(openPnl) : "—";
  tOpen.className = `tile-value ${pnls.length ? toneClass(openPnl) : ""}`;

  const realized = closed.reduce((acc, p) => acc + (p.position.realized_pnl_sol || 0), 0);
  const tReal = document.getElementById("t-realized");
  tReal.textContent = closed.length ? fmtSolSigned(realized) : "—";
  tReal.className = `tile-value ${closed.length ? toneClass(realized) : ""}`;
  document.getElementById("t-realized-sub").textContent =
    `SOL, ${closed.length} ${closed.length === 1 ? "cerrada" : "cerradas"}`;
}

function renderTable(now) {
  const tbody = document.querySelector("#positions tbody");
  const rows = [];
  // Más recientes arriba.
  for (const snap of [...state.positions].reverse()) {
    const pos = snap.position;
    const st = statusOf(snap);
    const lvl = nextLevel(snap);
    const live = pos && !pos.closed;

    let pnlMarket = null;
    let pnlNet = null;
    if (pos && pos.closed) {
      pnlNet = pos.realized_pnl_pct;
    } else if (pos) {
      pnlMarket = pos.market_pnl_pct;
      pnlNet = pos.entry_is_real_fill ? pos.net_pnl_pct : null;
    }

    const since = pos ? pos.opened_at : snap.started_at;
    const until = (pos && pos.closed_at) || snap.finished_at || now;

    const tr = el("tr", { dataset: { id: snap.id }, class: snap.id === state.selectedId ? "selected" : null,
                          tabindex: "0", "aria-label": `Posición ${snap.id}, ${tokenTitle(snap)}` },
      el("td", { dataset: { label: "#" } }, `#${snap.id}`),
      el("td", { dataset: { label: "Token" } },
        el("div", { class: "token" },
          el("span", { class: "token-name" }, tokenTitle(snap)),
          // Sin símbolo todavía, el título ya ES el mint acortado.
          tokenTitle(snap) === shortMint(snap.mint) ? null : el("span", { class: "token-mint" }, shortMint(snap.mint)))),
      el("td", { dataset: { label: "Estado" } },
        el("span", { class: "status", dataset: { tone: st.tone } },
          el("span", { class: "ico", "aria-hidden": "true" }, st.icon), st.label)),
      el("td", { class: "num", dataset: { label: "Precio" } }, live || !pos ? fmtPrice(snap.latest_price) : fmtPrice(pos.exit_price)),
      el("td", { class: "num", dataset: { label: "Entrada" } }, pos ? fmtPrice(pos.market_entry_price) : "—"),
      el("td", { class: "num", dataset: { label: "Próximo nivel" } },
        lvl ? el("span", {}, el("span", { class: "level-kind" }, `${lvl.kind} ${lvl.dist === null ? "" : fmtPct(lvl.dist)}`), fmtPrice(lvl.value)) : "—"),
      el("td", { class: `num ${toneClass(pnlMarket)}`, dataset: { label: "PnL mercado" } }, fmtPct(pnlMarket)),
      el("td", { class: `num ${toneClass(pnlNet)}`, dataset: { label: pos && pos.closed ? "PnL realizado" : "PnL neto" } }, fmtPct(pnlNet)),
      el("td", { class: "num", dataset: { label: "Tiempo" } }, since ? fmtDuration(until - since) : "—"),
      el("td", { class: "actions" }, actionButton(snap, "btn-sm")),
    );
    rows.push(tr);
  }
  // Reconstruir la tabla cada segundo le quitaría el foco a la fila (o al
  // botón) en el que está el teclado: se le devuelve al mismo.
  const active = document.activeElement;
  const focusedId = active && tbody.contains(active) ? active.closest("tr[data-id]")?.dataset.id : undefined;
  const focusedAction = active && active.dataset ? active.dataset.action : undefined;
  tbody.replaceChildren(...rows);
  if (focusedId !== undefined) {
    const row = tbody.querySelector(`tr[data-id="${focusedId}"]`);
    const target = focusedAction ? row?.querySelector(`[data-action="${focusedAction}"]`) : row;
    (target || row)?.focus();
  }
  document.getElementById("empty").hidden = state.positions.length > 0;
  document.getElementById("positions").hidden = state.positions.length === 0;
}

// ---------------------------------------------------------------- órdenes

// Se le puede pedir el cierre: el bot corre y nadie lo pidió todavía.
const closable = (snap) => snap.running && !snap.close_requested;

function actionButton(snap, extraClass = "") {
  if (closable(snap)) {
    // Sin posición todavía (esperando entrada) no hay nada que vender:
    // cerrar es cancelar la entrada.
    const label = snap.position ? "Cerrar" : "Cancelar";
    return el("button", { type: "button", class: `btn btn-danger-outline ${extraClass}`,
                          dataset: { action: "close", id: snap.id },
                          "aria-label": `${label} posición ${snap.id} (${tokenTitle(snap)})` }, label);
  }
  if (snap.running && snap.close_requested) {
    return el("button", { type: "button", class: `btn ${extraClass}`, disabled: "" }, "Cerrando…");
  }
  return null;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    method: options.method || "GET",
    credentials: "same-origin",
    // X-PePump: el servidor rechaza cualquier orden sin ella (una web
    // ajena no la puede mandar).
    headers: { "X-PePump": "1", ...(options.body ? { "Content-Type": "application/json" } : {}) },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* sin cuerpo */ }
  if (res.status === 401 && path !== "/api/login") showLogin();
  if (!res.ok) throw new Error((data && data.detail) || `Error ${res.status}`);
  return data;
}

function toast(message, kind = "") {
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, message);
  document.getElementById("toasts").append(node);
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 4000);
}

// Diálogo de confirmación: resuelve true si se confirma.
function confirmDialog(title, bodyNodes, okLabel) {
  const dialog = document.getElementById("confirm");
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-body").replaceChildren(...bodyNodes);
  document.getElementById("confirm-ok").textContent = okLabel;
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.returnValue = "cancel";
    dialog.showModal();
    // El foco empieza en "Volver": un Enter de más no vende nada.
    dialog.querySelector('button[value="cancel"]').focus();
  });
}

function facts(pairs) {
  const dl = el("dl", { class: "facts" });
  for (const [k, v, tone] of pairs) dl.append(el("dt", {}, k), el("dd", { class: tone || "" }, v));
  return dl;
}

function realWarning(text) {
  return state.live ? el("p", { class: "warn-real" }, `⚠ MODO REAL: ${text}`) : null;
}

async function closePosition(id) {
  const snap = state.positions.find((p) => p.id === id);
  if (!snap || !closable(snap)) return;
  const pos = snap.position;
  let ok;
  if (pos && !pos.closed) {
    const pnlSol = openPnlSol(snap);
    ok = await confirmDialog(`¿Cerrar #${snap.id} ${tokenTitle(snap)}?`, [
      el("p", {}, "Se venden todos los tokens de esta posición al precio de mercado actual."),
      facts([
        ["Precio actual", fmtPrice(snap.latest_price)],
        ["Entrada", fmtPrice(pos.market_entry_price)],
        ["PnL mercado", fmtPct(pos.market_pnl_pct), toneClass(pos.market_pnl_pct)],
        ["PnL si se vende ahora", `${fmtSolSigned(pnlSol)} SOL`, toneClass(pnlSol)],
      ]),
      realWarning("se vende de verdad, con SOL real."),
    ].filter(Boolean), "Vender ahora");
  } else {
    ok = await confirmDialog(`¿Cancelar #${snap.id} ${tokenTitle(snap)}?`, [
      el("p", {}, "Todavía no se compró nada: se deja de esperar la entrada y no se compra."),
    ], "Cancelar entrada");
  }
  if (!ok) return;
  try {
    await api(`/api/positions/${id}/close`, { method: "POST" });
    toast(`Cierre pedido para #${id}. El resultado aparecerá en la tabla y en los logs.`);
  } catch (err) {
    toast(`No se pudo cerrar #${id}: ${err.message}`, "error");
  }
}

async function closeAll() {
  const targets = state.positions.filter(closable);
  if (!targets.length) return;
  const abiertas = targets.filter((p) => p.position && !p.position.closed);
  const esperando = targets.length - abiertas.length;
  const pnls = abiertas.map(openPnlSol).filter((x) => x !== null);
  const pnl = pnls.reduce((a, b) => a + b, 0);
  const ok = await confirmDialog(`¿Cerrar ${targets.length === 1 ? "la posición activa" : `las ${targets.length} posiciones activas`}?`, [
    facts([
      ["Se venden", `${abiertas.length} ${abiertas.length === 1 ? "posición" : "posiciones"} al precio actual`],
      ...(esperando ? [["Se cancelan", `${esperando} esperando entrada`]] : []),
      ...(pnls.length ? [["PnL si se vende ahora", `${fmtSolSigned(pnl)} SOL`, toneClass(pnl)]] : []),
    ]),
    realWarning("se vende todo de verdad, con SOL real."),
  ].filter(Boolean), "Cerrar todas");
  if (!ok) return;
  try {
    const res = await api("/api/positions/close-all", { method: "POST" });
    toast(`Cierre pedido para ${res.requested} ${res.requested === 1 ? "posición" : "posiciones"}.`);
  } catch (err) {
    toast(`No se pudieron cerrar: ${err.message}`, "error");
  }
}

function kv(label, value, opts = {}) {
  const v = el("div", { class: `v ${opts.mono ? "mono" : ""} ${opts.tone || ""}` });
  if (value instanceof Node) v.append(value);
  else v.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  return el("div", { class: "kv" }, el("div", { class: "k" }, label), v);
}

function renderDetail() {
  const box = document.getElementById("detail");
  const snap = state.positions.find((p) => p.id === state.selectedId);
  if (!snap) {
    box.hidden = true;
    return;
  }
  // Mismo truco que en la tabla: no rehacer el botón bajo el puntero.
  if (pointerDown) return;
  box.hidden = false;
  const pos = snap.position;
  const s = snap.settings;
  const st = statusOf(snap);
  const name = snap.token_name && snap.token_symbol ? `${snap.token_name} ($${snap.token_symbol})` : tokenTitle(snap);
  document.getElementById("detail-title").textContent = `#${snap.id} · ${name}`;
  const detailAction = actionButton(snap);
  document.getElementById("detail-actions").replaceChildren(...(detailAction ? [detailAction] : []));

  const link = el("a", { href: `https://pump.fun/${encodeURIComponent(snap.mint)}`, target: "_blank", rel: "noopener noreferrer" }, snap.mint);
  const items = [
    kv("Mint", link, { mono: true }),
    kv("Estado", `${st.icon} ${st.label}`),
    kv("Modo", snap.live ? "REAL" : "Simulado"),
    kv("Fuente del precio", snap.price_source === "feed" ? "Feed en vivo (PumpPortal)" : `On-chain (${snap.price_source})`),
    kv("Precio actual", fmtPrice(snap.latest_price)),
  ];
  if (snap.entry_target_price && !pos) items.push(kv("Objetivo de entrada", fmtPrice(snap.entry_target_price)));
  if (snap.end_reason) items.push(kv("Motivo de fin", snap.end_reason));
  if (snap.close_reason) items.push(kv("Cierre pedido", snap.close_reason));

  if (pos) {
    items.push(
      kv("Entrada (mercado)", fmtPrice(pos.market_entry_price)),
      kv("Coste real por token", pos.entry_is_real_fill ? fmtPrice(pos.entry_price) : "— (estimado)"),
      kv("SOL invertidos", fmtSol(pos.sol_amount)),
      kv("Tokens", pos.token_amount.toLocaleString("es-ES", { maximumFractionDigits: 2 })),
      kv("Abierta a las", fmtTime(pos.opened_at)),
      kv("Máximo", fmtPrice(pos.highest_price)),
      kv(`Activación (+${s.activation_pct} %)`, fmtPrice(pos.activation_price)),
      kv(`Stop-loss inicial (−${s.initial_stop_pct} %)`, fmtPrice(pos.initial_stop_price)),
      kv(`Nivel de venta (trailing ${s.trailing_pct} %)`, pos.armed ? fmtPrice(pos.trailing_stop_price) : "sin armar"),
    );
    if (!pos.closed) {
      items.push(
        kv("PnL mercado", fmtPct(pos.market_pnl_pct), { tone: toneClass(pos.market_pnl_pct) }),
        kv("PnL neto", pos.entry_is_real_fill ? fmtPct(pos.net_pnl_pct) : "— (sin datos reales)",
           { tone: pos.entry_is_real_fill ? toneClass(pos.net_pnl_pct) : "" }),
        kv("PnL si se vende ahora", `${fmtSolSigned(openPnlSol(snap))} SOL`, { tone: toneClass(openPnlSol(snap)) }),
      );
    } else {
      items.push(
        kv("Cerrada a las", fmtTime(pos.closed_at)),
        kv("Precio de salida", fmtPrice(pos.exit_price)),
        kv("SOL recibidos", fmtSol(pos.proceeds_sol)),
        kv("PnL realizado", `${fmtPct(pos.realized_pnl_pct)} · ${fmtSolSigned(pos.realized_pnl_sol)} SOL${pos.exit_is_real_fill ? "" : " (estimado)"}`,
           { tone: toneClass(pos.realized_pnl_sol) }),
        kv("Motivo de la venta", pos.close_reason),
      );
    }
  }
  items.push(
    kv("Compra", `${s.buy_sol} SOL`),
    kv("Slippage", `${s.slippage} %`),
    kv("Priority fee", `${s.priority_fee} SOL`),
    kv("Pool", s.pool),
    kv("Baja de entrada", s.entry_dip_pct > 0 ? `−${s.entry_dip_pct} %` : "no"),
    kv("Base de la estrategia", s.strategy_price_basis === "real" ? "coste real" : "precio de mercado"),
  );
  document.getElementById("detail-body").replaceChildren(...items);
}

// ------------------------------------------------------------------- logs

function logMatches(entry) {
  if (!state.logFilter) return true;
  if (state.logFilter === "__general__") return !entry.mint;
  return entry.mint === state.logFilter;
}

function logNode(entry) {
  const tag = logTag(entry.mint);
  return el("div", { class: "log", dataset: { level: entry.level } },
    el("span", { class: "ts" }, fmtTime(entry.ts)),
    el("span", { class: "lvl" }, entry.level === "WARNING" ? "WARN" : entry.level),
    el("span", { class: "tag" }, tag ? `[${tag}]` : ""),
    el("span", { class: "msg" }, entry.message));
}

function renderLogs() {
  const box = document.getElementById("logs");
  box.replaceChildren(...state.logs.filter(logMatches).map(logNode));
  scrollLogs();
}

function appendLogs(entries) {
  state.logs.push(...entries);
  const overflow = state.logs.length - MAX_LOGS;
  if (overflow > 0) state.logs.splice(0, overflow);
  const box = document.getElementById("logs");
  for (const e of entries) if (logMatches(e)) box.append(logNode(e));
  while (box.childElementCount > MAX_LOGS) box.firstElementChild.remove();
  scrollLogs();
}

function scrollLogs() {
  if (document.getElementById("log-follow").checked) {
    const box = document.getElementById("logs");
    box.scrollTop = box.scrollHeight;
  }
}

function renderLogFilterOptions() {
  const select = document.getElementById("log-filter");
  const wanted = state.positions.map((p) => [p.mint, `#${p.id} ${tokenTitle(p)}`]);
  const current = [...select.options].slice(2).map((o) => [o.value, o.textContent]);
  if (JSON.stringify(wanted) === JSON.stringify(current)) return;
  while (select.options.length > 2) select.remove(2);
  for (const [value, label] of wanted) select.append(el("option", { value }, label));
  select.value = state.logFilter;
}

// ---------------------------------------------------------------- render

let serverOffset = 0; // reloj del servidor - reloj local (para las duraciones)

// Mientras haya un botón del ratón (o un dedo) pulsado sobre la tabla o el
// detalle, no se reconstruyen: si el botón "Cerrar" se reemplazara entre
// pulsar y soltar, el clic se perdería.
let pointerDown = false;

function render() {
  const now = Date.now() / 1000 + serverOffset;
  renderMode();
  renderTiles();
  if (!pointerDown) renderTable(now);
  document.getElementById("close-all").disabled = !state.positions.some(closable);
  renderDetail();
  renderLogFilterOptions();
}

function applyState(s) {
  state.live = s.live;
  state.positions = s.positions;
  serverOffset = s.server_time - Date.now() / 1000;
  render();
}

// ------------------------------------------------------------- conexión

function setConn(kind, label) {
  const c = document.getElementById("conn");
  c.dataset.state = kind;
  c.querySelector(".conn-label").textContent = label;
}

let retryDelay = 1000;

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => {
    retryDelay = 1000;
    setConn("live", "En vivo");
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "hello") {
      state.logs = msg.logs.slice(-MAX_LOGS);
      applyState(msg.state);
      renderLogs();
    } else if (msg.type === "state") {
      applyState(msg.state);
    } else if (msg.type === "logs") {
      appendLogs(msg.logs);
    }
  };
  ws.onclose = (ev) => {
    if (ev.code === 4401) {
      // Sin sesión (o caducó): a la pantalla de contraseña, sin reintentar.
      showLogin();
      return;
    }
    setConn("down", `Sin conexión · reintento en ${Math.round(retryDelay / 1000)} s`);
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 10000);
  };
}

// ------------------------------------------------------------------ sesión

let connected = false;

function showLogin() {
  document.getElementById("app").hidden = true;
  document.getElementById("login").hidden = false;
  document.getElementById("logout").hidden = true;
  setConn("down", "Sin sesión");
  connected = false;
  document.getElementById("login-password").focus();
}

function showApp(authRequired) {
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
  document.getElementById("logout").hidden = !authRequired;
  if (!connected) {
    connected = true;
    connect();
  }
}

async function start() {
  try {
    const s = await api("/api/session");
    if (s.auth_required && !s.authenticated) showLogin();
    else showApp(s.auth_required);
  } catch (err) {
    setConn("down", "Sin conexión");
    setTimeout(start, 3000);
  }
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("login-password");
  const error = document.getElementById("login-error");
  error.hidden = true;
  try {
    await api("/api/login", { method: "POST", body: { password: input.value } });
    input.value = "";
    showApp(true);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
    input.select();
  }
});

document.getElementById("logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch { /* da igual */ }
  location.reload();
});

// ----------------------------------------------------------------- eventos

function selectRow(target) {
  const tr = target.closest("tr[data-id]");
  if (!tr) return;
  const id = Number(tr.dataset.id);
  state.selectedId = state.selectedId === id ? null : id;
  syncHash();
  render();
  if (state.selectedId !== null) document.getElementById("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function onActionClick(e) {
  const button = e.target.closest("button[data-action]");
  if (!button) return false;
  e.stopPropagation();
  if (button.dataset.action === "close") closePosition(Number(button.dataset.id));
  return true;
}

document.querySelector("#positions tbody").addEventListener("click", (e) => {
  if (!onActionClick(e)) selectRow(e.target);
});
document.getElementById("detail-actions").addEventListener("click", onActionClick);
document.getElementById("close-all").addEventListener("click", closeAll);
for (const id of ["positions", "detail"]) {
  const zone = document.getElementById(id);
  zone.addEventListener("pointerdown", () => { pointerDown = true; });
}
for (const evName of ["pointerup", "pointercancel"]) {
  // En window: el dedo/ratón se puede soltar fuera de la tabla.
  window.addEventListener(evName, () => {
    if (!pointerDown) return;
    // Después del click (que va justo detrás del pointerup).
    setTimeout(() => { pointerDown = false; render(); }, 0);
  });
}
document.querySelector("#positions tbody").addEventListener("keydown", (e) => {
  if (e.target.closest("button")) return; // Enter en un botón es su propio clic
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    selectRow(e.target);
  }
});
document.getElementById("detail-close").addEventListener("click", () => {
  state.selectedId = null;
  syncHash();
  render();
});

// La posición seleccionada va en la URL (#pos=3): enlace directo y se
// mantiene al recargar.
function syncHash() {
  history.replaceState(null, "", state.selectedId === null ? location.pathname : `#pos=${state.selectedId}`);
}
function readHash() {
  const m = location.hash.match(/^#pos=(\d+)$/);
  state.selectedId = m ? Number(m[1]) : null;
}
window.addEventListener("hashchange", () => { readHash(); render(); });
readHash();
document.getElementById("log-filter").addEventListener("change", (e) => {
  state.logFilter = e.target.value;
  renderLogs();
});

start();
