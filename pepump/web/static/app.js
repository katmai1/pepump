// Panel web de PePump. Recibe por WebSocket el estado de todas las
// posiciones cada segundo y los logs en el momento en que salen; las
// órdenes (abrir una posición, cerrar una, cerrar todas) van por POST a /api.
//
// OJO: el nombre y el símbolo del token vienen de la metadata on-chain, que
// escribe quien creó el token: todo texto se pone con textContent (nunca
// innerHTML) para que un nombre malicioso no pueda inyectar HTML.
"use strict";

const MAX_LOGS = 3000;

// Cualquier fallo de la página se muestra en pantalla: un botón que "no hace
// nada" sin explicación es lo peor que puede pasar en una app que compra.
window.addEventListener("error", (e) => reportError(e.error || e.message));
window.addEventListener("unhandledrejection", (e) => reportError(e.reason));
function reportError(err) {
  const msg = err && err.message ? err.message : String(err);
  console.error(err);
  try { toast(`Error en la página: ${msg}. Recarga (Ctrl+F5) y, si sigue, avisa.`, "error"); } catch { /* aún sin DOM */ }
}

const state = {
  live: null,
  positions: [],
  logs: [],
  selectedId: null,
  logFilter: "",
  view: "main",      // "main" (posiciones) o "history"
  wallet: null,      // {address, sol, source} de /api/wallet
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
  if (snap.phase === "open" && !snap.running && snap.detached) {
    return { icon: "⏸", label: "Detenida sin vender", tone: "warn" };
  }
  if (snap.phase === "open" && !snap.running) {
    return { icon: "⚠", label: "ABIERTA SIN VIGILAR", tone: "critical" };
  }
  if (snap.sell_failures > 0 && !(pos && pos.closed)) {
    // La venta falló y el bot la está reintentando con más slippage.
    return { icon: "⚠", label: `Venta fallida ×${snap.sell_failures} · reintentando`,
             tone: snap.sell_failures >= 3 ? "critical" : "warn" };
  }
  if (snap.close_requested && !["closed", "stopped"].includes(snap.phase)) {
    return { icon: "◐", label: "Cerrando", tone: "warn" };
  }
  switch (snap.phase) {
    case "starting": return { icon: "◌", label: "Arrancando", tone: "accent" };
    case "connecting": return { icon: "◌", label: "Conectando", tone: "accent" };
    case "waiting_entry": {
      // Con baja de entrada: cuánto le falta bajar al precio para comprar.
      const missing = dipMissingPct(snap);
      if (missing !== null) return { icon: "⏳", label: `Esperando baja · falta ${missing.toFixed(2)} %`, tone: "accent" };
      if (snap.entry_target_price) return { icon: "⏳", label: "Esperando baja", tone: "accent" };
      return { icon: "⏳", label: "Esperando primer precio", tone: "accent" };
    }
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

// Cuánto (%) tiene que bajar todavía el precio para tocar el objetivo de la
// baja de entrada. null si no hay baja o todavía no hay precio.
function dipMissingPct(snap) {
  if (snap.position || !snap.entry_target_price || !snap.latest_price) return null;
  return Math.max(0, (1 - snap.entry_target_price / snap.latest_price) * 100);
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

// Cada posición es REAL o simulada (se elige al abrirla). La etiqueta de
// arriba avisa si hay alguna real activa.
function renderMode() {
  const badge = document.getElementById("mode");
  if (state.live === null) return;
  const reales = state.positions.filter((p) => p.live && p.running).length;
  // Solo aparece si hay dinero real en juego.
  badge.hidden = reales === 0;
  badge.textContent = `⚠ ${reales} ${reales === 1 ? "posición REAL" : "posiciones REALES"}`;
  document.title = reales ? `PePump · ${reales} REAL` : "PePump";
}

function money(ps) {
  const open = ps.filter((p) => p.position && !p.position.closed);
  const closed = ps.filter((p) => p.position && p.position.closed);
  const pnls = open.map(openPnlSol).filter((x) => x !== null);
  return {
    open: open.length,
    closed: closed.length,
    invested: open.length ? open.reduce((a, p) => a + p.position.sol_amount, 0) : null,
    openPnl: pnls.length ? pnls.reduce((a, b) => a + b, 0) : null,
    realized: closed.length ? closed.reduce((a, p) => a + (p.position.realized_pnl_sol || 0), 0) : null,
  };
}

function setTile(id, value, text, sub) {
  const node = document.getElementById(id);
  node.textContent = text;
  node.className = `tile-value ${value === null ? "" : toneClass(value)}`;
  document.getElementById(`${id}-sub`).textContent = sub;
}

// El dinero real y el simulado NUNCA se suman: las cifras grandes son de las
// posiciones reales (si hay alguna en la sesión) y lo simulado va aparte.
function renderTiles() {
  const ps = state.positions;
  const real = money(ps.filter((p) => p.live));
  const sim = money(ps.filter((p) => !p.live));
  const hayReal = ps.some((p) => p.live);
  const main = hayReal ? real : sim;
  const other = hayReal ? sim : null;
  const etiqueta = hayReal ? "reales" : "simuladas";
  const aparte = (v, fmt) => (other && v !== null ? ` · simulado: ${fmt(v)}` : "");

  const running = ps.filter((p) => p.running);
  const nReal = running.filter((p) => p.live).length;
  document.getElementById("t-active").textContent = String(running.length);
  document.getElementById("t-active-sub").textContent =
    running.length
      ? [nReal ? `${nReal} ${nReal === 1 ? "real" : "reales"}` : null,
         running.length - nReal ? `${running.length - nReal} ${running.length - nReal === 1 ? "simulada" : "simuladas"}` : null]
          .filter(Boolean).join(" · ")
      : `de ${ps.length} en esta sesión`;

  setTile("t-invested", null, main.invested === null ? "—" : fmtSol(main.invested),
          `SOL, ${etiqueta}${aparte(other && other.invested, fmtSol)}`);
  setTile("t-open-pnl", main.openPnl, main.openPnl === null ? "—" : fmtSolSigned(main.openPnl),
          `SOL ${etiqueta}, si se vendiera ahora${aparte(other && other.openPnl, fmtSolSigned)}`);
  setTile("t-realized", main.realized, main.realized === null ? "—" : fmtSolSigned(main.realized),
          `SOL ${etiqueta}, ${main.closed} ${main.closed === 1 ? "cerrada" : "cerradas"}${aparte(other && other.realized, fmtSolSigned)}`);
}

// Sigue en la página principal: el bot corre, o terminó dejando la posición
// abierta (venta fallida, hay que revisarla). Las cerradas o canceladas
// pasan al historial.
const isActive = (snap) => snap.running || Boolean(snap.position && !snap.position.closed);

function renderTable(now) {
  const tbody = document.querySelector("#positions tbody");
  const rows = [];
  const visibles = state.positions.filter(isActive);
  // Más recientes arriba.
  for (const snap of [...visibles].reverse()) {
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
      pnlNet = pos.has_entry_costs ? pos.net_pnl_pct : null;
    }

    const since = pos ? pos.opened_at : snap.started_at;
    const until = (pos && pos.closed_at) || snap.finished_at || now;

    const tr = el("tr", { dataset: { id: snap.id }, class: snap.id === state.selectedId ? "selected" : null,
                          tabindex: "0", "aria-label": `Posición ${snap.id}, ${tokenTitle(snap)}` },
      el("td", { dataset: { label: "#" } }, `#${snap.id}`),
      el("td", { dataset: { label: "Token" } },
        el("div", { class: "token" },
          el("span", { class: "token-name" }, tokenTitle(snap),
            snap.live ? el("span", { class: "tag-pill real", title: "Posición con SOL real" }, "REAL") : null,
            snap.recovered ? el("span", { class: "tag-pill", title: "Retomada de una ejecución anterior del bot" }, "retomada") : null),
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
  document.getElementById("empty").hidden = visibles.length > 0;
  document.getElementById("positions").hidden = visibles.length === 0;
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
function confirmDialog(title, bodyNodes, okLabel, danger = true) {
  const dialog = document.getElementById("confirm");
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-body").replaceChildren(...bodyNodes);
  const okButton = document.getElementById("confirm-ok");
  okButton.textContent = okLabel;
  okButton.classList.toggle("btn-danger", danger);
  okButton.classList.toggle("btn-primary", !danger);
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

function realWarning(isReal, text) {
  return isReal ? el("p", { class: "warn-real" }, `⚠ REAL: ${text}`) : null;
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
      realWarning(snap.live, "se vende de verdad, con SOL real."),
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
    realWarning(targets.some((p) => p.live),
                `${targets.filter((p) => p.live).length} de ellas son REALES y se venden de verdad.`),
  ].filter(Boolean), "Cerrar todas");
  if (!ok) return;
  try {
    const res = await api("/api/positions/close-all", { method: "POST" });
    toast(`Cierre pedido para ${res.requested} ${res.requested === 1 ? "posición" : "posiciones"}.`);
  } catch (err) {
    toast(`No se pudieron cerrar: ${err.message}`, "error");
  }
}

// --------------------------------------------------------- nueva posición

const NUMERIC_FIELDS = ["buy_sol", "activation_pct", "trailing_pct", "initial_stop_pct",
                        "entry_dip_pct", "slippage", "priority_fee"];
const TEXT_FIELDS = ["pool", "strategy_price_basis"];
let defaults = null;
let previewSeq = 0;
let previewData = null;

const openForm = () => document.getElementById("open-form");
const field = (name) => openForm().elements[name];

function fillDefaults() {
  for (const name of [...NUMERIC_FIELDS, ...TEXT_FIELDS]) field(name).value = defaults.settings[name];
  // Siempre empieza en simulado: lo real se elige a propósito, cada vez.
  field("mode").value = "sim";
  updateModeUI();
}

const chosenReal = () => field("mode").value === "real";

// Que se note en todo el formulario que la compra va a ser con SOL real.
function updateModeUI() {
  const real = chosenReal();
  document.getElementById("open-dialog").classList.toggle("is-real", real);
  document.getElementById("open-title").textContent = real ? "Nueva posición · REAL" : "Nueva posición · simulada";
  const submit = document.getElementById("open-submit");
  submit.textContent = real ? "Revisar y comprar con SOL real…" : "Revisar y comprar (simulado)…";
  submit.classList.toggle("btn-danger", real);
  submit.classList.toggle("btn-primary", !real);
  if (real) loadWallet();  // saldo al momento de decidir una compra real
  renderFormWallet();
}

// En modo real, el saldo de la wallet al lado del importe (y aviso si no
// llega). El servidor lo vuelve a comprobar, con saldo fresco, al comprar.
function renderFormWallet() {
  const note = document.getElementById("f-wallet");
  if (!chosenReal()) {
    note.hidden = true;
    return;
  }
  note.hidden = false;
  note.className = "wallet-note";
  const w = state.wallet;
  if (!w || !w.address) {
    note.textContent = "Saldo desconocido: pon tu wallet_address en [pumpportal] del .toml para verlo.";
    return;
  }
  if (w.sol === null) {
    note.textContent = "No se pudo consultar el saldo de la wallet (¿RPC?).";
    return;
  }
  const buy = Number(field("buy_sol").value.replace(",", ".")) || 0;
  const fee = Number(field("priority_fee").value.replace(",", ".")) || 0;
  const needed = buy + fee + BUY_OVERHEAD_SOL;
  if (w.sol < needed) {
    note.classList.add("bad");
    note.textContent = `Saldo insuficiente: ${fmtWalletSol(w.sol)} SOL en la wallet; esta compra necesita unos ${needed.toFixed(4)}.`;
  } else {
    note.textContent = `Saldo de la wallet: ${fmtWalletSol(w.sol)} SOL`;
  }
}

// Mismo margen que manager.BUY_OVERHEAD_SOL (rent de la cuenta del token y comisiones).
const BUY_OVERHEAD_SOL = 0.004;

// ------------------------------------------------------------------ wallet

// Saldo completo: SOL tiene 9 decimales (1 lamport = 0,000000001 SOL).
const fmtWalletSol = (sol) => sol.toFixed(9);

let walletEvents = null; // firma de las compras/ventas reales ya vistas
let finishedIds = null; // ids de las posiciones de la sesión que ya terminaron

// fresh: sin la caché del servidor (tras una compra/venta, o a mano).
async function loadWallet({ fresh = false, notify = false } = {}) {
  try {
    state.wallet = await api(`/api/wallet${fresh ? "?fresh=1" : ""}`);
  } catch {
    return;
  }
  if (notify) toast("Saldo actualizado.");
  renderWallet();
  if (document.getElementById("open-dialog").open) renderFormWallet();
}

function renderWallet() {
  const box = document.getElementById("wallet");
  const w = state.wallet;
  if (!w) return;
  box.hidden = false;
  if (!w.address) {
    box.replaceChildren("Wallet: sin configurar");
    box.title = "Pon la dirección pública de tu wallet en wallet_address, sección [pumpportal] del .toml";
    return;
  }
  box.title = `${w.address}${w.source === "learned" ? " (aprendida de la última compra)" : ""} · pulsa para actualizar el saldo`;
  box.replaceChildren(`Wallet ${shortMint(w.address)}: `,
                      el("strong", {}, w.sol === null ? "—" : fmtWalletSol(w.sol)), " SOL");
}

async function openNewPosition() {
  try {
    defaults = await api("/api/defaults");
  } catch (err) {
    toast(`No se pudo abrir el formulario: ${err.message}`, "error");
    return;
  }
  const form = openForm();
  form.reset();
  fillDefaults();
  field("buy_sol").max = defaults.max_buy_sol;
  document.getElementById("f-buy-hint").textContent = `Máximo ${defaults.max_buy_sol} SOL por posición (max_buy_sol)`;
  document.getElementById("open-error").hidden = true;
  document.getElementById("f-preview").hidden = true;
  previewData = null;
  form.querySelector("details").open = false;
  document.getElementById("open-dialog").showModal();
  field("mint").focus();
}

function showOpenError(message) {
  const box = document.getElementById("open-error");
  box.textContent = message;
  box.hidden = !message;
}

const MARKETS = {
  bondingcurve: "En la curva de pump.fun",
  pumpswap: "Migrado a PumpSwap",
  other: "No está en pump.fun ni en PumpSwap: se intentará Raydium CPMM / Meteora (DLMM o DAMM v2)",
  jupiter: "No está en pump.fun ni en PumpSwap: si tampoco está en Raydium CPMM / Meteora, irá por Jupiter (precio = lo que darían al vender)",
  unknown: "No se pudo comprobar dónde cotiza (¿RPC?)",
};

function renderPreview(p, error) {
  const box = document.getElementById("f-preview");
  box.hidden = false;
  box.className = "preview field-full";
  if (error) {
    box.classList.add("bad");
    box.replaceChildren(el("span", {}, error));
    return;
  }
  if (!p) {
    box.replaceChildren(el("span", { class: "p-meta" }, "Comprobando el token…"));
    return;
  }
  const name = p.name && p.symbol ? `${p.name} ($${p.symbol})` : p.symbol ? `$${p.symbol}` : p.name || "Sin nombre on-chain";
  const parts = [el("span", { class: "p-name" }, name),
                 el("span", { class: "p-meta" }, MARKETS[p.market] || p.market)];
  if (p.price) parts.push(el("span", { class: "p-meta" }, `Precio ahora: ${fmtPrice(p.price)} SOL/token`));
  if (p.active_position) {
    box.classList.add("bad");
    parts.push(el("span", {}, `Ya hay una posición activa de este token (#${p.active_position}).`));
  } else if (p.market === "other" || p.market === "unknown") {
    box.classList.add("warn");
  }
  box.replaceChildren(...parts);
}

let previewTimer = null;
function onMintInput() {
  clearTimeout(previewTimer);
  previewData = null;
  const mint = field("mint").value.trim();
  field("mint").removeAttribute("aria-invalid");
  if (mint.length < 32) {
    document.getElementById("f-preview").hidden = true;
    return;
  }
  renderPreview(null);
  // Espera a que termines de pegar/escribir: cada consulta gasta RPC.
  previewTimer = setTimeout(async () => {
    const seq = ++previewSeq;
    try {
      const p = await api(`/api/preview?mint=${encodeURIComponent(mint)}`);
      if (seq !== previewSeq) return;
      previewData = p;
      renderPreview(p);
    } catch (err) {
      if (seq !== previewSeq) return;
      field("mint").setAttribute("aria-invalid", "true");
      renderPreview(null, err.message);
    }
  }, 400);
}

function readSettings() {
  const settings = {};
  for (const name of NUMERIC_FIELDS) {
    const raw = field(name).value.trim().replace(",", ".");
    const value = Number(raw);
    if (raw === "" || !isFinite(value)) throw new Error(`Revisa el campo "${field(name).closest("label").querySelector("span").textContent}".`);
    // Solo se manda lo que cambia respecto al .toml.
    if (value !== defaults.settings[name]) settings[name] = value;
  }
  for (const name of TEXT_FIELDS) {
    if (field(name).value !== defaults.settings[name]) settings[name] = field(name).value;
  }
  // El modo se manda SIEMPRE: que no dependa de qué ponga el .toml.
  settings.live = chosenReal();
  return settings;
}

async function submitNewPosition(e) {
  e.preventDefault();
  try {
    await doSubmitNewPosition();
  } catch (err) {
    console.error(err);
    showOpenError(`Error inesperado: ${err && err.message ? err.message : err}`);
    document.getElementById("open-submit").disabled = false;
  }
}

async function doSubmitNewPosition() {
  showOpenError("");
  const mint = field("mint").value.trim();
  if (!mint) {
    showOpenError("Falta el mint.");
    field("mint").focus();
    return;
  }
  let settings;
  try {
    settings = readSettings();
  } catch (err) {
    showOpenError(err.message);
    return;
  }
  const buySol = settings.buy_sol ?? defaults.settings.buy_sol;
  if (buySol <= 0 || buySol > defaults.max_buy_sol) {
    showOpenError(`El importe tiene que estar entre 0 y ${defaults.max_buy_sol} SOL (max_buy_sol).`);
    field("buy_sol").focus();
    return;
  }
  if (previewData && previewData.active_position) {
    showOpenError(`Ya hay una posición activa de este token (#${previewData.active_position}).`);
    return;
  }

  const s = { ...defaults.settings, ...settings };
  const p = previewData;
  const tokenName = p && (p.symbol ? `$${p.symbol}` : p.name);
  const isReal = settings.live;
  const ok = await confirmDialog(`${isReal ? "COMPRA REAL: " : "Simulada: "}¿comprar ${buySol} SOL de ${tokenName || shortMint(mint)}?`, [
    facts([
      ["Token", p ? `${p.name || "—"}${p.symbol ? ` ($${p.symbol})` : ""}` : shortMint(mint)],
      ["Mint", shortMint(mint)],
      ["Dónde", p ? (MARKETS[p.market] || p.market) : "sin comprobar"],
      ...(p && p.price ? [["Precio ahora", `${fmtPrice(p.price)} SOL/token`]] : []),
      ["Modo", isReal ? "REAL (SOL de verdad)" : "simulado (no se compra nada)", isReal ? "down" : ""],
      ["Compra", `${buySol} SOL`],
      ["Entrada", s.entry_dip_pct > 0 ? `cuando baje un ${s.entry_dip_pct} %` : "al primer precio"],
      ["Estrategia", `activa +${s.activation_pct} % · trailing ${s.trailing_pct} % · stop −${s.initial_stop_pct} %`],
    ]),
    realWarning(isReal, `se compra de verdad, con ${buySol} SOL reales de tu wallet.`),
  ].filter(Boolean), isReal ? "Comprar con SOL real" : "Comprar (simulado)", isReal);
  if (!ok) return;

  const submit = document.getElementById("open-submit");
  submit.disabled = true;
  try {
    const res = await api("/api/positions", { method: "POST", body: { mint, settings } });
    document.getElementById("open-dialog").close();
    toast(`Posición #${res.id} abierta. Comprará en cuanto haya precio.`);
  } catch (err) {
    showOpenError(err.message);
  } finally {
    submit.disabled = false;
  }
}

function renderNewButton() {
  const button = document.getElementById("new-position");
  const activas = state.positions.filter(isActive).length;
  const max = defaults ? defaults.max_open_positions : null;
  const full = max !== null && activas >= max;
  button.disabled = full;
  button.title = full ? `Límite de ${max} posiciones activas (max_open_positions)` : "";
}

function kv(label, value, opts = {}) {
  const v = el("div", { class: `v ${opts.mono ? "mono" : ""} ${opts.tone || ""}` });
  if (value instanceof Node) v.append(value);
  else v.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  return el("div", { class: "kv" }, el("div", { class: "k" }, label), v);
}

function renderDetail() {
  const box = document.getElementById("detail");
  const snap = state.positions.find((p) => p.id === state.selectedId && isActive(p));
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
  updateChart(snap);
  const detailAction = actionButton(snap);
  document.getElementById("detail-actions").replaceChildren(...(detailAction ? [detailAction] : []));

  const link = el("a", { href: `https://pump.fun/${encodeURIComponent(snap.mint)}`, target: "_blank", rel: "noopener noreferrer" }, snap.mint);
  const items = [
    kv("Mint", link, { mono: true }),
    kv("Estado", `${st.icon} ${st.label}`),
    kv("Modo", snap.live ? "REAL (SOL de verdad)" : "Simulado", { tone: snap.live ? "down" : "" }),
    ...(snap.recovered ? [kv("Origen", "Retomada de una ejecución anterior")] : []),
    kv("Fuente del precio", snap.price_source === "feed" ? "Feed en vivo (PumpPortal)"
      : snap.price_source === "jupiter" ? "Jupiter (cotización de venta; órdenes por Jupiter)"
      : `On-chain (${snap.price_source})`),
    kv("Precio actual", fmtPrice(snap.latest_price)),
  ];
  if (snap.entry_target_price && !pos) {
    const missing = dipMissingPct(snap);
    items.push(
      kv(`Referencia (baja de ${snap.settings.entry_dip_pct} %)`, fmtPrice(snap.entry_reference_price)),
      kv("Compra cuando el precio llegue a", fmtPrice(snap.entry_target_price)),
      kv("Le falta bajar", missing === null ? "—" : `${missing.toFixed(2)} %`),
    );
  }
  if (snap.end_reason) items.push(kv("Motivo de fin", snap.end_reason));
  if (snap.close_reason) items.push(kv("Cierre pedido", snap.close_reason));

  if (pos) {
    items.push(
      kv("Entrada (mercado)", fmtPrice(pos.market_entry_price)),
      kv(pos.entry_is_real_fill || !pos.has_entry_costs ? "Coste real por token" : "Coste por token (simulado)",
         pos.has_entry_costs ? fmtPrice(pos.entry_price) : "— (estimado)"),
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
        kv("PnL neto", pos.has_entry_costs ? fmtPct(pos.net_pnl_pct) : "— (sin datos de costes)",
           { tone: pos.has_entry_costs ? toneClass(pos.net_pnl_pct) : "" }),
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

// ----------------------------------------------------------------- gráfico

let chart = null;
const chartData = { id: null, points: [], lastFetch: 0, loading: false };

async function fetchPrices(id, after) {
  chartData.loading = true;
  try {
    const width = document.getElementById("chart-box").clientWidth || 800;
    const res = await api(`/api/positions/${id}/prices?after=${after}&max_points=${Math.round(width * 2)}`);
    if (chartData.id !== id) return;
    if (after === 0) chartData.points = res.points;
    else chartData.points.push(...res.points);
    chartData.lastFetch = Date.now();
  } catch {
    /* se reintenta en el próximo refresco */
  } finally {
    chartData.loading = false;
  }
}

function updateChart(snap) {
  const figure = document.getElementById("chart");
  figure.hidden = false;
  if (!chart) {
    chart = new PriceChart(document.getElementById("chart-box"), document.getElementById("chart-legend"),
                           document.getElementById("chart-caption"), fmtPrice);
  }
  if (chartData.id !== snap.id) {
    chartData.id = snap.id;
    chartData.points = [];
    chartData.lastFetch = 0;
    fetchPrices(snap.id, 0).then(() => chart.render(snap, chartData.points, Date.now() / 1000 + serverOffset));
  } else if (!chartData.loading && snap.running && Date.now() - chartData.lastFetch > 2000) {
    const last = chartData.points.length ? chartData.points[chartData.points.length - 1][0] : 0;
    fetchPrices(snap.id, last);
  }
  chart.render(snap, chartData.points, Date.now() / 1000 + serverOffset);
}

// --------------------------------------------------------------- historial

const HISTORY_STATUS = {
  closed: "✓ Cerrada",
  cancelled: "■ Sin compra",
  review: "⚠ Revisar a mano",
};

function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("es-ES", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

let historyData = { rows: [], summary: { real: null, sim: null } };

async function loadHistory() {
  try {
    historyData = await api("/api/history?limit=100");
  } catch {
    return;
  }
  renderHistory();
}

const historyMode = () => document.getElementById("history-mode").value;

// Mismo criterio que las cifras de arriba de la página principal: lo real y
// lo simulado nunca se suman. Con "Todas", las cifras grandes son las reales
// (si hay) y lo simulado va aparte en la línea pequeña.
function renderHistorySummary() {
  const { real, sim } = historyData.summary;
  const mode = historyMode();
  let main, other, etiqueta;
  if (mode === "real") [main, other, etiqueta] = [real, null, "reales"];
  else if (mode === "sim") [main, other, etiqueta] = [sim, null, "simuladas"];
  else if (real) [main, other, etiqueta] = [real, sim, "reales"];
  else [main, other, etiqueta] = [sim, null, "simuladas"];
  const aparte = (txt) => (other ? ` · simulado: ${txt}` : "");

  if (!main) {
    for (const id of ["h-pnl", "h-count", "h-winrate", "h-avg"]) setTile(id, null, "—", "");
    document.getElementById("h-pnl-sub").textContent = "Todavía no hay posiciones cerradas.";
    return;
  }
  const roi = main.invested_sol ? (main.pnl_sol / main.invested_sol) * 100 : null;
  setTile("h-pnl", main.pnl_sol, fmtSolSigned(main.pnl_sol),
          `SOL ${etiqueta}${roi === null ? "" : ` · ${fmtPct(roi)} sobre ${fmtSol(main.invested_sol)}`}` +
          aparte(`${fmtSolSigned(other && other.pnl_sol)} SOL`));
  setTile("h-count", null, String(main.closed),
          `${main.wins} ganadoras · ${main.losses} perdedoras` + aparte(String(other && other.closed)));
  const winrate = main.closed ? (main.wins / main.closed) * 100 : null;
  setTile("h-winrate", null, winrate === null ? "—" : `${winrate.toFixed(0)} %`,
          `de las ${etiqueta}` + aparte(other && other.closed ? `${((other.wins / other.closed) * 100).toFixed(0)} %` : "—"));
  setTile("h-avg", main.avg_pct, fmtPct(main.avg_pct),
          `mejor ${fmtPct(main.best_pct)} · peor ${fmtPct(main.worst_pct)}`);
}

function renderHistory() {
  renderHistorySummary();
  const mode = historyMode();
  const rows = historyData.rows.filter((r) => mode === "all" || r.live === (mode === "real"));
  // "Revisar a mano" no se borra: solo cuenta lo que sí se puede limpiar.
  document.getElementById("history-clear").disabled =
    !historyData.rows.some((r) => r.status !== "review");
  document.getElementById("history-empty").hidden = rows.length > 0;
  document.querySelector("#history table").hidden = rows.length === 0;
  document.querySelector("#history tbody").replaceChildren(...rows.map((r) => {
    const title = r.token_symbol ? `$${r.token_symbol}` : shortMint(r.mint);
    return el("tr", {},
      el("td", { dataset: { label: "#" } }, `#${r.id}`),
      el("td", { dataset: { label: "Token" } },
        el("div", { class: "token" },
          el("span", { class: "token-name" }, title, r.live ? el("span", { class: "tag-pill real" }, "REAL") : null),
          title === shortMint(r.mint) ? null : el("span", { class: "token-mint" }, shortMint(r.mint)))),
      el("td", { dataset: { label: "Resultado" } }, HISTORY_STATUS[r.status] || r.status),
      el("td", { class: `num ${toneClass(r.realized_pnl_pct)}`, dataset: { label: "PnL" } }, fmtPct(r.realized_pnl_pct)),
      el("td", { class: `num ${toneClass(r.realized_pnl_sol)}`, dataset: { label: "PnL SOL" } }, fmtSolSigned(r.realized_pnl_sol)),
      el("td", { class: "reason", dataset: { label: "Motivo" } }, r.close_reason || r.end_reason || "—"),
      el("td", { class: "num", dataset: { label: "Fecha" } }, fmtDate(r.closed_at || r.updated_at)),
    );
  }));
}

async function clearHistory() {
  const mode = historyMode();
  const que = { all: "todas las posiciones terminadas", real: "las posiciones REALES terminadas",
                sim: "las posiciones simuladas terminadas" }[mode];
  const ok = await confirmDialog("¿Limpiar el historial?", [
    el("p", {}, `Se borran ${que} (cerradas y sin compra) y sus gráficos. No se puede deshacer.`),
    el("p", { class: "muted small" },
       "Las marcadas «Revisar a mano» se mantienen. El archivo .csv de órdenes cerradas no se toca."),
    realWarning(mode !== "sim" && historyData.rows.some((r) => r.live),
                "también se borra el PnL de las posiciones reales."),
  ].filter(Boolean), "Borrar");
  if (!ok) return;
  try {
    const res = await api("/api/history/clear", { method: "POST", body: { mode } });
    toast(`Historial limpio: ${res.deleted} ${res.deleted === 1 ? "posición borrada" : "posiciones borradas"}.`);
  } catch (err) {
    toast(`No se pudo limpiar el historial: ${err.message}`, "error");
  }
  loadHistory();
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
  renderNewButton();
  renderDetail();
  renderLogFilterOptions();
}

function applyState(s) {
  state.live = s.live;
  state.positions = s.positions;
  // Una posición REAL compró o vendió: el saldo de la wallet cambió.
  const firma = s.positions.filter((p) => p.live && p.position)
    .map((p) => `${p.id}:${p.position.closed ? "c" : "o"}`).join(",");
  if (walletEvents !== null && firma !== walletEvents) loadWallet({ fresh: true });
  walletEvents = firma;
  // Una posición terminó: pasa al historial; si se está mirando, se recarga.
  const terminadas = s.positions.filter((p) => !isActive(p)).map((p) => p.id).join(",");
  if (finishedIds !== null && terminadas !== finishedIds && state.view === "history") loadHistory();
  finishedIds = terminadas;
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
      if (state.view === "history") loadHistory();
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
  document.getElementById("nav").hidden = true;
  document.getElementById("wallet").hidden = true;
  document.getElementById("login").hidden = false;
  document.getElementById("logout").hidden = true;
  setConn("down", "Sin sesión");
  connected = false;
  document.getElementById("login-password").focus();
}

function showApp(authRequired) {
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
  document.getElementById("nav").hidden = false;
  showView();
  loadWallet();
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
    else {
      showApp(s.auth_required);
      api("/api/defaults").then((d) => { defaults = d; renderNewButton(); }).catch(() => {});
    }
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
    api("/api/defaults").then((d) => { defaults = d; renderNewButton(); }).catch(() => {});
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
document.getElementById("history-clear").addEventListener("click", clearHistory);
document.getElementById("history-mode").addEventListener("change", renderHistory);
document.getElementById("new-position").addEventListener("click", openNewPosition);
document.getElementById("open-form").addEventListener("submit", submitNewPosition);
// El aviso de error es del último intento: se quita en cuanto tocas algo.
document.getElementById("open-form").addEventListener("input", () => showOpenError(""));
document.getElementById("open-cancel").addEventListener("click", () => document.getElementById("open-dialog").close());
document.getElementById("f-mint").addEventListener("input", onMintInput);
document.getElementById("f-reset").addEventListener("click", fillDefaults);
document.getElementById("f-mode").addEventListener("change", updateModeUI);
for (const name of ["buy_sol", "priority_fee"]) {
  document.getElementById("open-form").elements[name].addEventListener("input", renderFormWallet);
}
// El saldo NO se consulta periódicamente (cada consulta gasta una llamada
// del RPC): solo al cargar, al elegir Real en el formulario, cuando una
// posición real compra o vende (ver walletEvents) y al pulsar sobre él.
document.getElementById("wallet").addEventListener("click", () => loadWallet({ fresh: true, notify: true }));
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

// La URL dice qué se ve: #historial, o la posición seleccionada (#pos=3).
// Así hay enlace directo y se mantiene al recargar.
function syncHash() {
  history.replaceState(null, "", state.selectedId === null ? location.pathname : `#pos=${state.selectedId}`);
}
function readHash() {
  if (location.hash === "#historial") {
    state.view = "history";
  } else {
    state.view = "main";
    const m = location.hash.match(/^#pos=(\d+)$/);
    state.selectedId = m ? Number(m[1]) : null;
  }
}
function showView() {
  document.getElementById("view-main").hidden = state.view !== "main";
  document.getElementById("view-history").hidden = state.view !== "history";
  for (const a of document.querySelectorAll("#nav a")) {
    if (a.dataset.view === state.view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  if (state.view === "history") loadHistory();
}
window.addEventListener("hashchange", () => { readHash(); showView(); render(); });
readHash();
document.getElementById("log-filter").addEventListener("change", (e) => {
  state.logFilter = e.target.value;
  renderLogs();
});

start();
