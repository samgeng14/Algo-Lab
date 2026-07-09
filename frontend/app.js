/* Algo-Lab dashboard: polls the backend and renders portfolio + market state. */

const $ = (id) => document.getElementById(id);
const usd = (cents) =>
  (cents < 0 ? "-$" : "$") + Math.abs(cents / 100).toFixed(2);
const signedUsd = (cents) => (cents >= 0 ? "+" : "") + usd(cents).replace("$-", "-$");

let equityData = [];

async function refresh() {
  try {
    const [state, markets] = await Promise.all([
      fetch("/api/state").then((r) => r.json()),
      fetch("/api/markets").then((r) => r.json()),
    ]);
    renderHeader(state);
    renderTiles(state);
    renderPositions(state.positions);
    renderMarkets(markets);
    renderTrades(state.trades);
    renderErrors(state.last_cycle);
    equityData = state.equity;
    renderChart();
  } catch (e) {
    console.error("refresh failed", e);
  }
}

function renderHeader(s) {
  const badge = $("mode-badge");
  badge.textContent = s.mode === "live" ? "LIVE · REAL MONEY" : "PAPER TRADING";
  badge.className = "badge " + s.mode;
  $("last-cycle").textContent = s.last_cycle.at
    ? "last cycle " + new Date(s.last_cycle.at).toLocaleTimeString()
    : "no cycle run yet";
  const auto = $("btn-auto");
  auto.textContent = s.auto_enabled
    ? `Pause auto (${s.cycle_minutes}m)`
    : "Resume auto";
  auto.dataset.enabled = s.auto_enabled;
  $("btn-reset").hidden = s.mode !== "paper";
}

function renderTiles(s) {
  $("t-total").textContent = usd(s.total_cents);
  const delta = s.total_cents - s.starting_cents;
  const dEl = $("t-total-delta");
  dEl.textContent = `${signedUsd(delta)} vs start (${usd(s.starting_cents)})`;
  dEl.className = "delta " + (delta >= 0 ? "pos" : "neg");
  $("t-cash").textContent = usd(s.cash_cents);
  setPnl($("t-unreal"), s.unrealized_pnl_cents);
  setPnl($("t-real"), s.realized_pnl_cents);
  $("t-settled").textContent = s.settled_count;
  $("t-winrate").textContent = s.settled_count
    ? `${Math.round((100 * s.win_count) / s.settled_count)}% winners`
    : "";
}

function setPnl(el, cents) {
  el.textContent = signedUsd(cents);
  el.className = "value " + (cents > 0 ? "pos" : cents < 0 ? "neg" : "");
}

function sideChip(side) {
  return `<span class="side-chip">${side}</span>`;
}

function fillTable(tableId, rowsHtml) {
  const table = $(tableId);
  table.querySelector("tbody").innerHTML = rowsHtml.join("");
  const note = table.parentElement.querySelector(".empty-note");
  if (note) note.hidden = rowsHtml.length > 0;
  table.hidden = rowsHtml.length === 0;
}

function renderPositions(positions) {
  fillTable(
    "positions-table",
    positions.map((p) => {
      const cls = p.unrealized_cents > 0 ? "pos" : p.unrealized_cents < 0 ? "neg" : "";
      return `<tr>
        <td title="${p.ticker}">${p.title}</td><td>${p.city ?? ""}</td>
        <td>${sideChip(p.side)}</td>
        <td class="num">${p.qty}</td>
        <td class="num">${Math.round(p.avg_price_cents)}¢</td>
        <td class="num">${usd(p.mark_cents)}</td>
        <td class="num ${cls}">${signedUsd(p.unrealized_cents)}</td>
        <td>${p.event_date ?? ""}</td>
      </tr>`;
    })
  );
}

function renderMarkets(m) {
  $("scan-note").textContent = m.at
    ? `· ${m.rows.length} markets at ${new Date(m.at).toLocaleTimeString()}`
    : "";
  fillTable(
    "markets-table",
    m.rows.slice(0, 40).map((r) => {
      const edgeCls = r.best_edge > 0.02 ? "pos" : "";
      return `<tr>
        <td title="${r.ticker}">${r.title}</td><td>${r.city}</td><td>${r.event_date}</td>
        <td class="num">${r.forecast_high}°F</td>
        <td class="num">${r.running_high != null ? r.running_high.toFixed(1) + "°F" : "—"}</td>
        <td class="num">${r.yes_bid}¢ / ${r.yes_ask}¢</td>
        <td class="num">${(100 * r.model_prob_yes).toFixed(0)}%</td>
        <td class="num ${edgeCls}">${(100 * r.best_edge).toFixed(1)}¢</td>
      </tr>`;
    })
  );
}

function renderTrades(trades) {
  fillTable(
    "trades-table",
    trades.map(
      (t) => `<tr>
        <td>${new Date(t.at).toLocaleString()}</td>
        <td>${t.ticker}</td><td>${t.action}</td>
        <td>${sideChip(t.side)}</td>
        <td class="num">${t.qty}</td>
        <td class="num">${t.price_cents}¢</td>
        <td class="num">${t.fee_cents}¢</td>
        <td class="muted">${t.note ?? ""}</td>
      </tr>`
    )
  );
}

function renderErrors(lastCycle) {
  const errs = (lastCycle && lastCycle.errors) || [];
  $("errors-panel").hidden = errs.length === 0;
  $("errors-list").innerHTML = errs.map((e) => `<li>${e}</li>`).join("");
}

/* ---- equity curve (single-series SVG line, crosshair tooltip) ---- */

function renderChart() {
  const svg = $("equity-chart");
  const empty = $("chart-empty");
  svg.innerHTML = "";
  if (equityData.length < 2) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  const css = getComputedStyle(document.documentElement);
  const W = svg.clientWidth || 800;
  const H = 220;
  const pad = { l: 48, r: 16, t: 12, b: 24 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const pts = equityData
    .map((d) => ({ t: new Date(d.at).getTime(), v: d.total_cents / 100 }))
    .sort((a, b) => a.t - b.t);
  const tMin = pts[0].t, tMax = pts[pts.length - 1].t;
  let vMin = Math.min(...pts.map((p) => p.v));
  let vMax = Math.max(...pts.map((p) => p.v));
  if (vMax - vMin < 0.5) { vMin -= 0.25; vMax += 0.25; }
  const x = (t) => pad.l + ((t - tMin) / Math.max(tMax - tMin, 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - vMin) / (vMax - vMin)) * (H - pad.t - pad.b);

  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, attrs) => {
    const e = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    svg.appendChild(e);
    return e;
  };

  // hairline gridlines + clean y ticks
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = vMin + ((vMax - vMin) * i) / ticks;
    el("line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v),
                 stroke: css.getPropertyValue("--grid").trim(), "stroke-width": 1 });
    const label = el("text", { x: pad.l - 8, y: y(v) + 4, "text-anchor": "end",
                               "font-size": 11, fill: css.getPropertyValue("--muted").trim() });
    label.textContent = "$" + v.toFixed(2);
  }

  const series = css.getPropertyValue("--series-1").trim();
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");

  // area wash at ~10% opacity, then the 2px line
  el("path", {
    d: `${line}L${x(tMax).toFixed(1)},${H - pad.b}L${x(tMin).toFixed(1)},${H - pad.b}Z`,
    fill: series, opacity: 0.1,
  });
  el("path", { d: line, fill: "none", stroke: series, "stroke-width": 2,
               "stroke-linejoin": "round", "stroke-linecap": "round" });

  // end marker (>=8px with 2px surface ring) + end label
  const last = pts[pts.length - 1];
  el("circle", { cx: x(last.t), cy: y(last.v), r: 6,
                 fill: series, stroke: css.getPropertyValue("--surface").trim(), "stroke-width": 2 });
  const endLabel = el("text", { x: Math.min(x(last.t) + 8, W - pad.r), y: y(last.v) - 8,
                                "font-size": 11, "font-weight": 600, "text-anchor": "end",
                                fill: css.getPropertyValue("--ink").trim() });
  endLabel.textContent = "$" + last.v.toFixed(2);

  // crosshair + tooltip
  const cross = el("line", { y1: pad.t, y2: H - pad.b,
                             stroke: css.getPropertyValue("--baseline").trim(),
                             "stroke-width": 1, visibility: "hidden" });
  const dot = el("circle", { r: 5, fill: series, visibility: "hidden",
                             stroke: css.getPropertyValue("--surface").trim(), "stroke-width": 2 });
  const tip = $("tooltip");

  svg.onmousemove = (ev) => {
    const rect = svg.getBoundingClientRect();
    const mx = ((ev.clientX - rect.left) / rect.width) * W;
    let best = pts[0], bd = Infinity;
    for (const p of pts) {
      const d = Math.abs(x(p.t) - mx);
      if (d < bd) { bd = d; best = p; }
    }
    cross.setAttribute("x1", x(best.t));
    cross.setAttribute("x2", x(best.t));
    cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(best.t));
    dot.setAttribute("cy", y(best.v));
    dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.innerHTML = `<strong>$${best.v.toFixed(2)}</strong><br>
      <span class="tt-time">${new Date(best.t).toLocaleString()}</span>`;
    const left = (x(best.t) / W) * rect.width;
    tip.style.left = Math.min(left + 12, rect.width - tip.offsetWidth - 4) + "px";
    tip.style.top = (y(best.v) / H) * rect.height - 40 + "px";
  };
  svg.onmouseleave = () => {
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    tip.hidden = true;
  };
}

/* ---- controls ---- */

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

$("btn-cycle").onclick = async (e) => {
  e.target.disabled = true;
  e.target.textContent = "Running…";
  try {
    await post("/api/cycle");
    await refresh();
  } finally {
    e.target.disabled = false;
    e.target.textContent = "Run cycle now";
  }
};

$("btn-auto").onclick = async (e) => {
  await post("/api/auto", { enabled: e.target.dataset.enabled !== "true" });
  refresh();
};

$("btn-reset").onclick = async () => {
  if (confirm("Reset the paper account to its starting bankroll? All history is erased.")) {
    await post("/api/reset");
    refresh();
  }
};

window.addEventListener("resize", () => renderChart());
refresh();
setInterval(refresh, 30000);
