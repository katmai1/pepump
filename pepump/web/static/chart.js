// Gráfico de precio de una posición (SVG, sin librerías).
//
// Una sola escala de precio: la línea del precio y, sobre ella, los niveles
// que usa la estrategia (entrada, activación, stop-loss inicial, nivel de
// venta del trailing, objetivo de la baja de entrada), más marcas de compra,
// armado y venta. Al pasar el ratón: hora, precio y % respecto a la entrada.
//
// Los colores salen de las variables CSS del tema (claro/oscuro); los textos
// van siempre en los colores de texto, nunca en el de la serie.
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

class PriceChart {
  constructor(box, legend, caption, fmtPrice) {
    this.box = box;
    this.legend = legend;
    this.caption = caption;
    this.fmtPrice = fmtPrice;
    this.hoverX = null; // posición del ratón (px dentro del área de datos) o null
    this.snap = null;
    this.points = [];
    new ResizeObserver(() => this.draw()).observe(box);
  }

  render(snap, points, now) {
    this.snap = snap;
    this.points = points;
    this.now = now;
    this.draw();
  }

  // --- datos ------------------------------------------------------------- //

  series() {
    const snap = this.snap;
    const pos = snap.position;
    const pts = this.points.slice();
    const end = pos && pos.closed_at ? pos.closed_at : (snap.finished_at || this.now);
    // La línea llega hasta "ahora" con el último precio conocido.
    if (pts.length && !(pos && pos.closed) && snap.running && snap.latest_price) {
      const last = pts[pts.length - 1];
      if (end > last[0]) pts.push([end, snap.latest_price]);
    }
    const s = snap.settings;
    const levels = [];
    if (pos) {
      const start = pos.opened_at;
      const armedAt = pos.armed_at || (pos.armed ? start : null);
      levels.push({ key: "entry", label: "Entrada", value: pos.market_entry_price, from: start, to: end });
      if (!pos.armed || armedAt > start) {
        const until = armedAt || end;
        levels.push({ key: "activation", label: `Activación +${s.activation_pct} %`, value: pos.activation_price, from: start, to: until });
        levels.push({ key: "stop", label: `Stop inicial −${s.initial_stop_pct} %`, value: pos.initial_stop_price, from: start, to: until });
      }
      if (pos.armed && armedAt) {
        // Nivel de venta: máximo acumulado desde que se armó × (1 − trailing).
        const steps = [];
        let max = 0;
        for (const [t, p] of pts) {
          if (t < armedAt) continue;
          if (p > max) {
            max = p;
            steps.push([t, max * (1 - s.trailing_pct / 100)]);
          }
        }
        if (pos.highest_price > max) steps.push([steps.length ? steps[steps.length - 1][0] : armedAt, pos.highest_price * (1 - s.trailing_pct / 100)]);
        if (steps.length) {
          if (steps[0][0] > armedAt) steps.unshift([armedAt, steps[0][1]]);
          levels.push({ key: "trailing", label: `Venta (trailing ${s.trailing_pct} %)`, steps, to: end });
        }
      }
    } else if (snap.entry_target_price) {
      const from = pts.length ? pts[0][0] : end;
      levels.push({ key: "dip", label: `Compra ≤ (baja ${s.entry_dip_pct} %)`, value: snap.entry_target_price, from, to: end });
    }
    const marks = [];
    if (pos) {
      marks.push({ key: "buy", label: "Compra", t: pos.opened_at, p: pos.market_entry_price });
      if (pos.armed && pos.armed_at) {
        const near = pts.find((q) => q[0] >= pos.armed_at);
        marks.push({ key: "armed", label: "Armado", t: pos.armed_at, p: near ? near[1] : pos.activation_price });
      }
      if (pos.closed && pos.closed_at && pos.exit_price) {
        marks.push({ key: "sell", label: "Venta", t: pos.closed_at, p: pos.exit_price });
      }
    }
    return { pts, levels, marks, end };
  }

  // --- dibujo ------------------------------------------------------------ //

  draw() {
    if (!this.snap) return;
    const { pts, levels, marks, end } = this.series();
    const width = Math.max(280, this.box.clientWidth);
    const height = width < 500 ? 200 : 260;
    const m = { top: 12, right: 92, bottom: 26, left: 12 };
    const w = width - m.left - m.right;
    const h = height - m.top - m.bottom;

    if (pts.length < 2) {
      this.box.replaceChildren(Object.assign(document.createElement("p"), {
        className: "chart-empty", textContent: "Todavía no hay precios suficientes para el gráfico.",
      }));
      this.legend.replaceChildren();
      return;
    }

    // Escalas: tiempo en X; en Y el precio y los niveles visibles.
    const t0 = pts[0][0];
    const t1 = Math.max(end, pts[pts.length - 1][0], t0 + 1);
    const values = pts.map((p) => p[1]);
    for (const l of levels) {
      if (l.steps) values.push(...l.steps.map((s) => s[1]));
      else values.push(l.value);
    }
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    const pad = (hi - lo) * 0.08 || hi * 0.05;
    lo = Math.max(0, lo - pad);
    hi += pad;
    const x = (t) => m.left + ((t - t0) / (t1 - t0)) * w;
    const y = (p) => m.top + (1 - (p - lo) / (hi - lo)) * h;

    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, class: "chart-svg",
                              role: "img", "aria-label": "Gráfico de precio de la posición" });

    // Rejilla horizontal y etiquetas de precio (a la derecha).
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const p = lo + ((hi - lo) * i) / ticks;
      root.append(svg("line", { x1: m.left, x2: m.left + w, y1: y(p), y2: y(p), class: "grid" }));
      root.append(svg("text", { x: m.left + w + 8, y: y(p) + 4, class: "axis" }, this.fmtPrice(p)));
    }
    // Etiquetas de tiempo.
    const span = t1 - t0;
    const fmtT = (t) => new Date(t * 1000).toLocaleTimeString("es-ES", span > 86400
      ? { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }
      : { hour: "2-digit", minute: "2-digit", second: span < 600 ? "2-digit" : undefined, hour12: false });
    const nx = width < 500 ? 3 : 5;
    for (let i = 0; i <= nx; i++) {
      const t = t0 + (span * i) / nx;
      root.append(svg("text", { x: x(t), y: height - 6, class: "axis",
                                "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle" }, fmtT(t)));
    }

    // Niveles (debajo del precio).
    for (const l of levels) {
      if (l.steps) {
        let d = "";
        l.steps.forEach(([t, p], i) => {
          d += i === 0 ? `M${x(t)},${y(p)}` : `H${x(t)}V${y(p)}`;
        });
        d += `H${x(l.to)}`;
        root.append(svg("path", { d, class: `level level-${l.key}` }));
      } else {
        root.append(svg("line", { x1: x(l.from), x2: x(l.to), y1: y(l.value), y2: y(l.value), class: `level level-${l.key}` }));
      }
    }

    // Precio.
    const d = pts.map(([t, p], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(p).toFixed(1)}`).join("");
    root.append(svg("path", { d, class: "price-line" }));

    // Marcas de compra / armado / venta, con anillo del color de fondo.
    for (const mk of marks) {
      if (mk.t < t0 || mk.t > t1) continue;
      const g = svg("g", { class: `mark mark-${mk.key}` });
      g.append(svg("circle", { cx: x(mk.t), cy: y(mk.p), r: 5.5 }));
      g.append(svg("title", {}, `${mk.label}: ${this.fmtPrice(mk.p)}`));
      root.append(g);
    }

    // Capa de hover: línea vertical, punto y etiqueta.
    const hover = svg("g", { class: "hover", visibility: "hidden" });
    const vline = svg("line", { y1: m.top, y2: m.top + h, class: "crosshair" });
    const dot = svg("circle", { r: 4.5, class: "hover-dot" });
    const tipBg = svg("rect", { rx: 6, class: "tip-bg", height: 42 });
    const tip1 = svg("text", { class: "tip-strong" });
    const tip2 = svg("text", { class: "tip" });
    hover.append(vline, dot, tipBg, tip1, tip2);
    root.append(hover);
    const hit = svg("rect", { x: m.left, y: m.top, width: w, height: h, fill: "transparent", class: "hit" });
    root.append(hit);

    const entry = this.snap.position ? this.snap.position.market_entry_price : null;
    const showAt = (px) => {
      if (px === null) {
        hover.setAttribute("visibility", "hidden");
        return;
      }
      const t = t0 + ((px - m.left) / w) * (t1 - t0);
      // Punto más cercano en el tiempo (búsqueda binaria).
      let a = 0;
      let b = pts.length - 1;
      while (b - a > 1) {
        const mid = (a + b) >> 1;
        if (pts[mid][0] < t) a = mid; else b = mid;
      }
      const [pt, pp] = Math.abs(pts[a][0] - t) < Math.abs(pts[b][0] - t) ? pts[a] : pts[b];
      const cx = x(pt);
      const cy = y(pp);
      vline.setAttribute("x1", cx);
      vline.setAttribute("x2", cx);
      dot.setAttribute("cx", cx);
      dot.setAttribute("cy", cy);
      tip1.textContent = `${this.fmtPrice(pp)} SOL`;
      const hora = new Date(pt * 1000).toLocaleTimeString("es-ES", { hour12: false });
      tip2.textContent = entry ? `${hora} · ${((pp / entry - 1) * 100).toFixed(2)} % vs entrada` : hora;
      const tw = Math.max(tip1.getComputedTextLength(), tip2.getComputedTextLength()) + 20;
      const tx = cx + 12 + tw > m.left + w ? cx - 12 - tw : cx + 12;
      const ty = Math.min(Math.max(cy - 21, m.top), m.top + h - 42);
      tipBg.setAttribute("x", tx);
      tipBg.setAttribute("y", ty);
      tipBg.setAttribute("width", tw);
      tip1.setAttribute("x", tx + 10);
      tip1.setAttribute("y", ty + 17);
      tip2.setAttribute("x", tx + 10);
      tip2.setAttribute("y", ty + 33);
      hover.setAttribute("visibility", "visible");
    };
    const toLocal = (ev) => {
      const r = root.getBoundingClientRect();
      return ((ev.clientX - r.left) / r.width) * width;
    };
    hit.addEventListener("pointermove", (ev) => { this.hoverX = toLocal(ev); showAt(this.hoverX); });
    hit.addEventListener("pointerleave", () => { this.hoverX = null; showAt(null); });

    this.box.replaceChildren(root);
    // Se redibuja cada segundo: si el ratón estaba encima, se mantiene.
    if (this.hoverX !== null) showAt(this.hoverX);
    this.drawLegend(levels, marks);
    const last = pts[pts.length - 1][1];
    this.caption.textContent = `Precio de ${this.fmtPrice(pts[0][1])} a ${this.fmtPrice(last)} SOL entre ${fmtT(t0)} y ${fmtT(t1)}.`;
  }

  drawLegend(levels, marks) {
    const items = [["price", "Precio"]];
    for (const l of levels) items.push([l.key, l.label]);
    for (const mk of marks) items.push([`mark-${mk.key}`, mk.label]);
    this.legend.replaceChildren(...items.map(([key, label]) => {
      const item = document.createElement("span");
      item.className = "legend-item";
      const sw = document.createElement("span");
      sw.className = `swatch swatch-${key}`;
      sw.setAttribute("aria-hidden", "true");
      item.append(sw, document.createTextNode(label));
      return item;
    }));
  }
}

window.PriceChart = PriceChart;
