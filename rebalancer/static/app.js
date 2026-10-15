// Portfolio rebalancer web screen. Plain ES module, no build step, no outside requests.
// The draft (targets, tickers, notes) is edited here or by the chat. Orders go out only
// from the plan dialog's send button, after the person has seen the exact orders.

const TOKEN = document.querySelector('meta[name="session-token"]').content;
const SVG_NS = "http://www.w3.org/2000/svg";
const SLEEVES = ["core", "satellite"];
const SLEEVE_LABEL = { core: "코어", satellite: "위성", exit: "정리 대상", cash: "현금" };
const SLEEVE_NOTE = {
  core: "예측 없이 오래 유지하는 자리예요.",
  satellite: "매크로 판단을 담는 자리예요. 이유와 틀렸다고 볼 조건이 꼭 있어야 해요.",
};
const SUGGESTIONS = [
  "방산 비중을 5%로 줄이고 그만큼 VWRA에 넣어 줘",
  "지금 초안으로 이번 달에 뭘 사게 되는지 보여 줘",
  "위성 아이디어들의 틀렸다고 볼 조건을 다시 점검해 줘",
  "수수료를 아끼려면 주문을 어떻게 나누는 게 좋을까?",
];

// Colour pairs that fail the palette validator when they touch (fail) or need labels (warn).
const BAD_PAIRS = new Set(["1-7", "2-4", "2-5", "2-6", "2-8", "3-5", "3-6", "4-8", "5-8"]);
const WARN_PAIRS = new Set(["3-8", "4-6", "6-8"]);

// Ring geometry in the 300 x 300 viewBox.
const C = 150;
const OUTER = [96, 140];
const INNER = [84, 90];

const S = {
  app: null,
  draft: null,
  holdings: null,
  holdingsError: null,
  holdingsLoading: false,
  view: "all",
  hover: null,
  expanded: new Set(),
  adding: null,
  chatBusy: false,
  pendingChat: "",
  sync: { timer: null, inflight: null, pending: false },
  plan: null,
  planLoading: false,
  job: null,
  dialog: null,
  lastQuestionId: null,
  pollTimer: null,
  contracts: {},
  confirmDelete: null,
};

// Small helpers ----------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null) continue;
    node.setAttribute(k, v);
  }
  return node;
}

const ICONS = {
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  warn: ["M12 3.8 21.2 20H2.8z", "M12 10v4.4", "M12 17.3v.2"],
  x: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "M9 9l6 6", "M15 9l-6 6"],
  info: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "M12 11v5.5", "M12 7.8v.2"],
  chevron: ["M6 9l6 6 6-6"],
  clock: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "M12 7.5V12l3 2"],
  minus: ["M6 12h12"],
};

function icon(name, tone = "") {
  const node = svg("svg", { viewBox: "0 0 24 24", class: `icon ${tone}`.trim(), "aria-hidden": "true" });
  for (const d of ICONS[name]) node.append(svg("path", { d }));
  return node;
}

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

async function api(method, path, body) {
  const headers = { "X-Session-Token": TOKEN };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError("서버에 연결할 수 없어요. 터미널에서 서버가 켜져 있는지 확인하세요.", 0, {});
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `요청이 실패했어요 (HTTP ${res.status}).`;
    throw new ApiError(detail, res.status, data);
  }
  return data;
}

function usd(v, digits = 0) {
  const n = Number(v) || 0;
  const s = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${n < 0 ? "-" : ""}$${s}`;
}

const pct = (v, d = 1) => `${(Number(v) || 0).toFixed(d)}%`;
const plain = (v) => (Number.isInteger(v) ? String(v) : String(+Number(v).toFixed(2)));
function signed(v, d = 1) {
  const r = Number((Number(v) || 0).toFixed(d));
  if (r === 0) return (0).toFixed(d);
  return `${r > 0 ? "+" : "-"}${Math.abs(r).toFixed(d)}`;
}

function clock(iso) {
  if (!iso) return "";
  const t = iso.split("T")[1] || "";
  return t.slice(0, 5);
}

function dayLabel(iso) {
  if (!iso) return "";
  const [, m, d] = iso.split("T")[0].split("-").map(Number);
  return `${m}월 ${d}일`;
}

let toastTimer = null;
function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), 3800);
}

// The model behind the screen ------------------------------------------------------

function slotMap() {
  const order = [];
  for (const i of S.app.saved.instruments) order.push(i.symbol);
  for (const i of S.draft.portfolio.instruments) if (!order.includes(i.symbol)) order.push(i.symbol);
  const map = {};
  order.forEach((sym, i) => (map[sym] = i < 8 ? i + 1 : 0));
  return map;
}

function model() {
  const P = S.draft.portfolio;
  const H = S.holdings;
  const total = H ? Math.max(H.total, 0) : 0;
  const cash = H ? Math.max(H.cash, 0) : 0;
  const values = {};
  for (const p of (H && H.positions) || []) values[p.symbol] = p.value || 0;
  const share = (v) => (total > 0 ? (v / total) * 100 : 0);

  const symTarget = {};
  for (const a of P.allocations) symTarget[a.symbol] = (symTarget[a.symbol] || 0) + a.target;
  const counts = {};
  for (const a of P.allocations) counts[a.symbol] = (counts[a.symbol] || 0) + 1;
  const saved = {};
  for (const a of S.app.saved.allocations) saved[`${a.sleeve}:${a.symbol}`] = a.target;
  const instruments = Object.fromEntries(P.instruments.map((i) => [i.symbol, i]));
  const slots = slotMap();

  const rows = P.allocations.map((a) => {
    const key = `${a.sleeve}:${a.symbol}`;
    const part = symTarget[a.symbol] > 0 ? a.target / symTarget[a.symbol] : 0;
    const value = (values[a.symbol] || 0) * part;
    return {
      ...a,
      key,
      value,
      now: share(value),
      saved: key in saved ? saved[key] : null,
      shared: counts[a.symbol] > 1,
      slot: slots[a.symbol] ?? 0,
      inst: instruments[a.symbol],
    };
  });

  const allocated = new Set(P.allocations.map((a) => a.symbol));
  const exits = P.instruments
    .filter((i) => !allocated.has(i.symbol))
    .map((i) => ({ symbol: i.symbol, name: i.name, value: values[i.symbol] || 0, now: share(values[i.symbol] || 0), slot: slots[i.symbol] ?? 0 }));

  const sleeves = {};
  for (const s of SLEEVES) {
    const mine = rows.filter((r) => r.sleeve === s);
    sleeves[s] = {
      target: mine.reduce((t, r) => t + r.target, 0),
      saved: S.app.saved.allocations.filter((a) => a.sleeve === s).reduce((t, a) => t + a.target, 0),
      value: mine.reduce((t, r) => t + r.value, 0),
    };
    sleeves[s].now = share(sleeves[s].value);
  }
  const exitValue = exits.reduce((t, e) => t + e.value, 0);
  sleeves.exit = { value: exitValue, now: share(exitValue), target: 0 };
  sleeves.cash = { value: cash, now: share(cash), target: 0 };
  const sum = rows.reduce((t, r) => t + r.target, 0);
  return { total, cash, rows, exits, sleeves, sum, slots, hasHoldings: Boolean(H) && total > 0 };
}

// Donut ---------------------------------------------------------------------------

function pairCost(a, b) {
  if (typeof a !== "number" || typeof b !== "number" || !a || !b || a === b) return 0;
  const k = a < b ? `${a}-${b}` : `${b}-${a}`;
  return BAD_PAIRS.has(k) ? 100 : WARN_PAIRS.has(k) ? 1 : 0;
}

function permutations(list) {
  if (list.length <= 1 || list.length > 6) return [list];
  const out = [];
  const walk = (rest, acc) => {
    if (!rest.length) return out.push(acc);
    rest.forEach((x, i) => walk([...rest.slice(0, i), ...rest.slice(i + 1)], [...acc, x]));
  };
  walk(list, []);
  return out;
}

// Order segments inside each group so that colours that fail the colour-vision checks never
// touch (the ring wraps around), shared holdings sit at the group border, and slot order wins ties.
function arrange(groups) {
  const result = [];
  let prev = null;
  groups.forEach((g, gi) => {
    const isLast = gi === groups.length - 1;
    let best = null;
    let bestCost = Infinity;
    for (const perm of permutations(g.segs)) {
      let cost = 0;
      if (prev) cost += pairCost(prev.slot, perm[0].slot) - (prev.symbol === perm[0].symbol ? 0.5 : 0);
      for (let i = 1; i < perm.length; i++) cost += pairCost(perm[i - 1].slot, perm[i].slot);
      if (groups.length === 1 && perm.length > 1) cost += pairCost(perm[perm.length - 1].slot, perm[0].slot);
      else if (isLast && result.length && result[0].segs.length) cost += pairCost(perm[perm.length - 1].slot, result[0].segs[0].slot);
      if (g.key === "core") cost += perm.findIndex((s) => s.shared) === perm.length - 1 ? -0.3 : 0;
      for (let i = 1; i < perm.length; i++) if ((perm[i].order ?? 0) < (perm[i - 1].order ?? 0)) cost += 0.01;
      if (cost < bestCost) {
        bestCost = cost;
        best = perm;
      }
    }
    result.push({ ...g, segs: best });
    prev = best[best.length - 1];
  });
  return result;
}

function arcPath(r0, r1, a0, a1) {
  const p = (r, a) => [C + r * Math.sin(a), C - r * Math.cos(a)];
  if (a1 - a0 >= Math.PI * 2 - 1e-6) {
    return [
      `M${C} ${C - r1}A${r1} ${r1} 0 1 1 ${C - 0.01} ${C - r1}Z`,
      `M${C} ${C - r0}A${r0} ${r0} 0 1 0 ${C + 0.01} ${C - r0}Z`,
    ].join("");
  }
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const [x0, y0] = p(r1, a0);
  const [x1, y1] = p(r1, a1);
  const [x2, y2] = p(r0, a1);
  const [x3, y3] = p(r0, a0);
  const f = (n) => n.toFixed(2);
  return `M${f(x0)} ${f(y0)}A${r1} ${r1} 0 ${large} 1 ${f(x1)} ${f(y1)}L${f(x2)} ${f(y2)}A${r0} ${r0} 0 ${large} 0 ${f(x3)} ${f(y3)}Z`;
}

function donutGroups(m) {
  const groups = [];
  for (const s of SLEEVES) {
    const segs = m.rows
      .filter((r) => r.sleeve === s && r.value > 0)
      .map((r, i) => ({ key: r.key, symbol: r.symbol, label: r.label, value: r.value, slot: r.slot, shared: r.shared, group: s, order: r.slot || 99 + i }));
    if (segs.length) groups.push({ key: s, segs });
  }
  const exitSegs = m.exits
    .filter((e) => e.value > 0)
    .map((e) => ({ key: `exit:${e.symbol}`, symbol: e.symbol, label: e.name, value: e.value, slot: e.slot, group: "exit", order: e.slot || 99 }));
  if (exitSegs.length) groups.push({ key: "exit", segs: exitSegs });
  if (m.cash > 0) groups.push({ key: "cash", segs: [{ key: "cash", symbol: "현금", label: "", value: m.cash, slot: "cash", group: "cash", order: 999 }] });
  return groups;
}

function segInfo(m, key) {
  // What the centre of the ring says about one segment or group.
  if (!key) return null;
  if (key.startsWith("ring:")) {
    const g = key.slice(5);
    const s = m.sleeves[g];
    const tgt = g in { core: 1, satellite: 1 } ? `목표 ${plain(s.target)}%` : usd(s.value);
    return { top: SLEEVE_LABEL[g], main: pct(s.now), sub: tgt };
  }
  if (key === "cash") return { top: "현금", main: pct(m.sleeves.cash.now), sub: usd(m.cash) };
  if (key.startsWith("exit:")) {
    const e = m.exits.find((x) => `exit:${x.symbol}` === key);
    return e ? { top: `${e.symbol} (정리 대상)`, main: pct(e.now), sub: usd(e.value) } : null;
  }
  const r = m.rows.find((x) => x.key === key);
  if (!r) return null;
  if (S.view === "all") return { top: r.symbol, main: pct(r.now), sub: `목표 ${plain(r.target)}%  ${usd(r.value)}` };
  const g = m.sleeves[S.view];
  const inside = g.value > 0 ? (r.value / g.value) * 100 : 0;
  return { top: r.symbol, main: pct(inside), sub: `${SLEEVE_LABEL[S.view]} 안에서, 전체의 ${pct(r.now)}` };
}

function centerDefault(m) {
  if (S.holdingsError && !S.holdings) return { top: "", main: "잔고 없음", sub: "아래 안내를 확인하세요" };
  if (!S.holdings) return { top: "", main: "불러오는 중", sub: "" };
  if (S.view === "all") return { top: "총자산", main: usd(m.total), sub: m.total > 0 ? `현금 ${pct(m.sleeves.cash.now)}` : "" };
  const g = m.sleeves[S.view];
  return { top: SLEEVE_LABEL[S.view], main: usd(g.value), sub: `전체의 ${pct(g.now)}` };
}

function drawCenter(info) {
  const root = $("#donut-center");
  if (!root) return;
  root.replaceChildren();
  const lines = [];
  if (info.top) lines.push(["center-top", info.top]);
  lines.push(["center-main", info.main]);
  if (info.sub) lines.push(["center-sub", info.sub]);
  const heights = { "center-top": 22, "center-main": 34, "center-sub": 20 };
  const total = lines.reduce((t, [c]) => t + heights[c], 0);
  let y = C - total / 2;
  for (const [cls, text] of lines) {
    y += heights[cls];
    const t = svg("text", { class: cls, x: C, y: y - (cls === "center-main" ? 8 : 5), "text-anchor": "middle" });
    t.textContent = text;
    root.append(t);
  }
}

function renderDonut(m) {
  const root = $("#donut");
  root.replaceChildren();
  const title = svg("title");
  title.textContent = "지금 비중 도넛 차트";
  root.append(title);

  let groups = donutGroups(m);
  if (S.view !== "all") groups = groups.filter((g) => g.key === S.view);
  const total = groups.reduce((t, g) => t + g.segs.reduce((u, s) => u + s.value, 0), 0);

  if (!total) {
    root.append(svg("circle", { class: "empty-ring", cx: C, cy: C, r: (OUTER[0] + OUTER[1]) / 2 }));
  } else {
    const arranged = arrange(groups);
    let a = 0;
    const segLayer = svg("g");
    const ringLayer = svg("g");
    const placed = [];
    for (const g of arranged) {
      const gStart = a;
      for (const s of g.segs) {
        const sweep = (s.value / total) * Math.PI * 2;
        placed.push({ slot: s.slot, end: a + sweep });
        const label = s.key === "cash" ? `현금 ${pct(m.sleeves.cash.now)}` : `${s.symbol} ${pct((s.value / (m.total || 1)) * 100)}`;
        const path = svg("path", {
          d: arcPath(OUTER[0], OUTER[1], a, a + sweep),
          class: `seg slot-${s.slot}${s.key === "cash" && S.view === "all" ? " static" : ""}`,
          "fill-rule": "evenodd",
          tabindex: "0",
          role: "button",
          "aria-label": label,
          "data-key": s.key,
          "data-group": g.key,
        });
        segLayer.append(path);
        a += sweep;
      }
      if (S.view === "all" && g.key !== "cash") {
        ringLayer.append(
          svg("path", {
            d: arcPath(INNER[0], INNER[1], gStart, a),
            class: `seg ring-${g.key}`,
            "fill-rule": "evenodd",
            tabindex: "-1",
            "data-key": `ring:${g.key}`,
            "data-group": g.key,
          }),
        );
      }
    }
    if (S.view !== "all") {
      root.append(svg("path", { d: arcPath(INNER[0], INNER[1], 0, Math.PI * 2), class: `seg static ring-${S.view}`, "fill-rule": "evenodd" }));
    }
    // Where two colours that fail the colour-vision check must touch, widen the gap between them.
    const seps = svg("g", { "aria-hidden": "true" });
    if (placed.length > 1) {
      placed.forEach((seg, i) => {
        const next = placed[(i + 1) % placed.length];
        if (pairCost(seg.slot, next.slot) < 100) return;
        const ang = seg.end;
        seps.append(
          svg("line", {
            class: "sep",
            x1: (C + (OUTER[0] - 1) * Math.sin(ang)).toFixed(2),
            y1: (C - (OUTER[0] - 1) * Math.cos(ang)).toFixed(2),
            x2: (C + (OUTER[1] + 1) * Math.sin(ang)).toFixed(2),
            y2: (C - (OUTER[1] + 1) * Math.cos(ang)).toFixed(2),
          }),
        );
      });
    }
    root.append(segLayer, seps, ringLayer);
  }
  root.append(svg("g", { id: "donut-center" }));
  drawCenter(segInfo(m, S.hover) || centerDefault(m));

  for (const node of root.querySelectorAll("[data-key]")) {
    const key = node.dataset.key;
    const enter = () => setHover(key);
    const leave = () => setHover(null);
    node.addEventListener("pointerenter", enter);
    node.addEventListener("pointerleave", leave);
    node.addEventListener("focus", enter);
    node.addEventListener("blur", leave);
    node.addEventListener("click", () => activateSegment(key, node.dataset.group));
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activateSegment(key, node.dataset.group);
      }
    });
  }
}

function setHover(key) {
  S.hover = key;
  const m = model();
  for (const node of document.querySelectorAll("#donut [data-key]")) {
    const k = node.dataset.key;
    const related = !key || k === key || (key.startsWith("ring:") && node.dataset.group === key.slice(5)) || k === `ring:${node.dataset.group}`;
    node.classList.toggle("dim", Boolean(key) && !related);
  }
  for (const node of document.querySelectorAll("#legend [data-key]")) node.classList.toggle("active", node.dataset.key === key);
  drawCenter(segInfo(m, key) || centerDefault(m));
}

function activateSegment(key, group) {
  if (S.view === "all") {
    if (group === "cash") return;
    drill(group);
    return;
  }
  if (key.startsWith("exit:")) return focusRow(`exit:${key.slice(5)}`);
  focusRow(key);
}

function drill(view) {
  S.view = view;
  S.hover = null;
  renderOverview();
  const first = $("#donut [data-key]");
  if (first) first.focus({ preventScroll: true });
}

function focusRow(key) {
  const row = document.querySelector(`.row[data-key="${CSS.escape(key)}"]`);
  if (!row) return;
  if (window.matchMedia("(max-width: 1100px)").matches) setPane("weights");
  row.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  row.classList.remove("flash");
  void row.offsetWidth;
  row.classList.add("flash");
  const input = row.querySelector("input");
  if (input) input.focus({ preventScroll: true });
}

function renderLegend(m) {
  const list = $("#legend");
  list.replaceChildren();
  if (!S.holdings || m.total <= 0) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  if (S.view === "all") {
    const groups = donutGroups(m);
    for (const g of groups) {
      const s = m.sleeves[g.key];
      const clickable = g.key !== "cash";
      const swClass = g.key === "cash" ? "slot-cash" : `ring-${g.key}`;
      const target = g.key in { core: 1, satellite: 1 } ? el("small", { text: `목표 ${plain(s.target)}%` }) : null;
      const item = el(
        "li",
        {
          class: `legend-item${clickable ? " clickable" : ""}`,
          tabindex: clickable ? "0" : null,
          role: clickable ? "button" : null,
          "aria-label": clickable ? `${SLEEVE_LABEL[g.key]} ${pct(s.now)}, 눌러서 세부 비중 보기` : null,
          dataset: { key: `ring:${g.key}` },
        },
        el("span", { class: `sw ${swClass}`, "aria-hidden": "true" }),
        el("span", { class: "legend-name" }, SLEEVE_LABEL[g.key], target),
        el("span", { class: "legend-value" }, pct(s.now), el("small", { text: usd(s.value) })),
        g.key === "cash"
          ? null
          : el(
              "span",
              { class: "legend-members" },
              g.segs.map((x) => el("span", {}, el("span", { class: `sw slot-${x.slot}`, "aria-hidden": "true" }), `${x.symbol} ${pct((x.value / m.total) * 100)}`)),
            ),
      );
      if (clickable) bindActivate(item, () => drill(g.key));
      item.addEventListener("pointerenter", () => setHover(`ring:${g.key}`));
      item.addEventListener("pointerleave", () => setHover(null));
      list.append(item);
    }
    return;
  }
  const g = donutGroups(m).find((x) => x.key === S.view);
  if (!g) return;
  const gTotal = g.segs.reduce((t, s) => t + s.value, 0);
  for (const s of [...g.segs].sort((a, b) => b.value - a.value)) {
    const row = m.rows.find((r) => r.key === s.key);
    const target = row ? `, 목표 ${plain(row.target)}%` : "";
    const item = el(
      "li",
      { class: "legend-item clickable", tabindex: "0", role: "button", dataset: { key: s.key }, "aria-label": `${s.symbol}, 목록에서 찾기` },
      el("span", { class: `sw slot-${s.slot}`, "aria-hidden": "true" }),
      el("span", { class: "legend-name" }, s.symbol, s.label ? el("small", { text: s.label }) : null),
      el("span", { class: "legend-value" }, pct((s.value / gTotal) * 100), el("small", { text: `전체 ${pct((s.value / m.total) * 100)}${target}` })),
    );
    bindActivate(item, () => focusRow(s.key.startsWith("exit:") ? s.key : s.key));
    item.addEventListener("pointerenter", () => setHover(s.key));
    item.addEventListener("pointerleave", () => setHover(null));
    list.append(item);
  }
}

function bindActivate(node, fn) {
  node.addEventListener("click", fn);
  node.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fn();
    }
  });
}

function renderOverview() {
  const m = model();
  renderDonut(m);
  renderLegend(m);
  const crumbs = $("#crumbs");
  crumbs.replaceChildren();
  if (S.view === "all") {
    crumbs.append(el("span", { class: "here", "aria-current": "page", text: "전체" }));
    $("#overview-title").textContent = "지금 비중";
    $("#donut-hint").textContent = m.hasHoldings ? "칸을 누르면 그 안의 세부 비중을 볼 수 있어요." : "";
  } else {
    crumbs.append(
      el("button", { type: "button", class: "btn text", text: "전체", onclick: () => drill("all") }),
      el("span", { "aria-hidden": "true", text: "/" }),
      el("span", { class: "here", "aria-current": "page", text: SLEEVE_LABEL[S.view] }),
    );
    $("#overview-title").textContent = `${SLEEVE_LABEL[S.view]} 세부 비중`;
    $("#donut-hint").textContent = "종목을 누르면 아래 목록에서 찾아 줘요. Esc를 누르면 전체로 돌아가요.";
  }
  renderNotice(m);
}

function renderNotice(m) {
  const box = $("#holdings-notice");
  const lines = [];
  let tone = "info";
  if (S.holdingsError) {
    tone = "critical";
    lines.push(S.holdingsError.message);
  }
  const H = S.holdings;
  if (H) {
    if (H.total <= 0 && !S.holdingsError) lines.push("아직 잔고가 없어요. 입금하고 첫 주문을 보내면 여기에 비중이 나타나요.");
    else if (H.positions.length === 0 && H.cash > 0) lines.push("아직 보유 종목이 없어요. 첫 주문을 보내면 여기에 비중이 나타나요.");
    if (H.missing_conids && H.missing_conids.length)
      lines.push(`conid가 없는 종목이 있어요: ${H.missing_conids.join(", ")}. 목록에서 종목을 펼쳐 'IBKR에서 찾기'로 채우고 저장하세요.`);
    if (H.unknown && H.unknown.length) lines.push(`목록에 없는 종목을 보유 중이에요: ${H.unknown.join(", ")}. 앱은 이 종목을 사고팔지 않아요.`);
    if (H.unpriced && H.unpriced.length) lines.push(`시세를 받지 못한 종목이 있어요: ${H.unpriced.join(", ")}. 비중 계산에서 빠졌어요.`);
  }
  if (!lines.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.replaceChildren(icon(tone === "critical" ? "x" : "info", tone === "critical" ? "critical" : ""), el("div", {}, lines.map((t) => el("p", { text: t }))));
}

// Header ---------------------------------------------------------------------------

function renderHeader() {
  const b = S.app.broker;
  const chip = $("#broker-chip");
  chip.textContent = b.label;
  chip.title = b.detail || "";
  chip.classList.toggle("live", Boolean(b.live));
  const month = S.app.month;
  const mm = Number(month.key.split("-")[1]);
  $("#month-status").textContent = month.executed_at
    ? `${mm}월 주문함 (${dayLabel(month.executed_at)}, ${month.mode_label})`
    : `${mm}월 주문 전`;
  const asOf = $("#as-of");
  if (S.holdingsLoading) asOf.textContent = "잔고 불러오는 중";
  else if (S.holdings) asOf.textContent = `${clock(S.holdings.fetched_at)} 기준`;
  else asOf.textContent = "";
}

// Weights editor ----------------------------------------------------------------------

function renderWeights() {
  const m = model();
  const root = $("#groups");
  root.replaceChildren();
  root.disabled = S.chatBusy;
  root.append(
    el(
      "div",
      { class: "col-head", "aria-hidden": "true" },
      el("span"),
      el("span", { text: "종목" }),
      el("span", { text: "지금과 목표" }),
      el("span", { text: "지금" }),
      el("span", { text: "목표" }),
      el("span", { text: "차이" }),
      el("span"),
    ),
  );
  const scale = bulletScale(m);
  for (const sleeve of SLEEVES) root.append(renderGroup(sleeve, m, scale));
  if (m.exits.length) root.append(renderExits(m));
  updateDerived(m);
}

function bulletScale(m) {
  let max = 10;
  for (const r of m.rows) max = Math.max(max, r.now, r.target, r.saved || 0);
  return max * 1.15;
}

function renderGroup(sleeve, m, scale) {
  const group = el("div", { class: `group${S.view === sleeve ? " focus" : ""}`, dataset: { sleeve } });
  group.append(
    el(
      "div",
      { class: "group-head" },
      el("h3", { text: SLEEVE_LABEL[sleeve] }),
      el("span", { class: "group-figures", "data-role": "group-figures" }),
      el("p", { class: "group-note", text: SLEEVE_NOTE[sleeve] }),
    ),
  );
  const list = el("ul", { class: "rows" });
  for (const r of m.rows.filter((x) => x.sleeve === sleeve)) list.append(renderRow(r, scale));
  group.append(list);

  if (S.adding === sleeve) group.append(renderAddForm(sleeve));
  else
    group.append(
      el("button", {
        type: "button",
        class: "btn ghost small add",
        text: `${SLEEVE_LABEL[sleeve]}에 종목 추가`,
        onclick: () => {
          S.adding = sleeve;
          renderWeights();
          const first = $(`.add-form[data-sleeve="${sleeve}"] input`);
          if (first) first.focus();
        },
      }),
    );
  return group;
}

function renderRow(r, scale) {
  const id = `detail-${r.sleeve}-${r.symbol}`.replace(/[^a-zA-Z0-9-]/g, "_");
  const open = S.expanded.has(r.key);
  const needsNotes = r.sleeve === "satellite" && (!r.thesis || !r.invalidation);
  const row = el("li", { class: "row", dataset: { key: r.key } });
  const input = el("input", {
    type: "number",
    inputmode: "decimal",
    min: "0",
    max: "100",
    step: "0.5",
    value: plain(r.target),
    "aria-label": `${r.symbol} ${SLEEVE_LABEL[r.sleeve]} 목표 비중`,
    oninput: (e) => {
      const v = parseFloat(e.target.value);
      if (Number.isFinite(v) && v >= 0 && v <= 100) setAllocation(r.key, { target: Math.round(v * 100) / 100 });
    },
    onchange: (e) => {
      const a = findAllocation(r.key);
      if (a) e.target.value = plain(a.target);
    },
  });
  row.append(
    el("span", { class: `sw slot-${r.slot}`, "aria-hidden": "true" }),
    el(
      "div",
      { class: "name" },
      el("span", { class: "sym", text: r.symbol }),
      el("span", { class: "label", "data-role": "label", text: r.label || "" }),
      needsNotes ? el("span", { class: "flag" }, icon("warn", "warning"), "메모 필요") : null,
      r.inst && r.inst.exchange && !r.inst.exchange.toUpperCase().includes("LSE") ? el("span", { class: "flag", text: r.inst.exchange }) : null,
    ),
    el(
      "div",
      { class: "bullet", "aria-hidden": "true" },
      el("span", { class: `bar slot-${r.slot}`, "data-role": "bar" }),
      el("span", { class: "tick", "data-role": "tick-saved" }),
      el("span", { class: "tick draft", "data-role": "tick-draft" }),
    ),
    el("span", { class: "now", "data-role": "now" }),
    el("label", { class: "target" }, input, el("span", { class: "unit", text: "%" })),
    el("span", { class: "diff", "data-role": "diff" }),
    el(
      "button",
      {
        type: "button",
        class: "toggle",
        "aria-expanded": open ? "true" : "false",
        "aria-controls": id,
        "aria-label": `${r.symbol} 메모와 종목 정보 ${open ? "접기" : "펼치기"}`,
        onclick: (e) => {
          const btn = e.currentTarget;
          const now = !S.expanded.has(r.key);
          if (now) S.expanded.add(r.key);
          else S.expanded.delete(r.key);
          btn.setAttribute("aria-expanded", now ? "true" : "false");
          btn.setAttribute("aria-label", `${r.symbol} 메모와 종목 정보 ${now ? "접기" : "펼치기"}`);
          $(`#${id}`).hidden = !now;
        },
      },
      icon("chevron"),
    ),
  );
  row.append(renderDetail(r, id, open));
  void scale;
  return row;
}

function renderDetail(r, id, open) {
  const box = el("div", { class: "detail", id, hidden: !open });
  box.append(
    field("설명", el("input", { type: "text", maxlength: "60", value: r.label || "", oninput: (e) => setAllocation(r.key, { label: e.target.value }) })),
  );
  if (r.sleeve === "satellite") {
    box.append(
      field(
        "담는 이유",
        el("textarea", { rows: "2", maxlength: "500", oninput: (e) => setAllocation(r.key, { thesis: e.target.value }) }, r.thesis || ""),
        !r.thesis,
      ),
      field(
        "틀렸다고 볼 조건",
        el("textarea", { rows: "2", maxlength: "500", oninput: (e) => setAllocation(r.key, { invalidation: e.target.value }) }, r.invalidation || ""),
        !r.invalidation,
      ),
    );
  } else {
    box.append(el("p", { class: "detail-note", text: "코어는 예측 없이 유지하는 자리라 이유와 조건이 없어도 돼요." }));
  }
  const inst = r.inst || { name: "", exchange: "", conid: null };
  box.append(
    el(
      "div",
      { class: "field-row" },
      field("상품 이름", el("input", { type: "text", maxlength: "120", value: inst.name || "", oninput: (e) => setInstrument(r.symbol, { name: e.target.value }) })),
      field("거래소", el("input", { type: "text", maxlength: "20", value: inst.exchange || "", oninput: (e) => setInstrument(r.symbol, { exchange: e.target.value }) })),
      field(
        "IBKR conid",
        el("input", {
          type: "number",
          inputmode: "numeric",
          min: "1",
          value: inst.conid || "",
          "data-role": "conid",
          oninput: (e) => {
            const v = e.target.value.trim();
            if (!v) setInstrument(r.symbol, { conid: null });
            else if (/^\d+$/.test(v)) setInstrument(r.symbol, { conid: Number(v) });
          },
        }),
      ),
    ),
  );
  if (r.shared) box.append(el("p", { class: "detail-note", text: `${r.symbol}: 코어와 위성 두 칸에 있어요. 상품 정보는 두 칸이 같이 써요.` }));
  const actions = el("div", { class: "detail-actions" });
  if (S.app.broker.kind === "ibkr") {
    actions.append(el("button", { type: "button", class: "btn small", text: "IBKR에서 conid 찾기", onclick: () => findContracts(r.symbol, inst.exchange || "LSE") }));
  } else {
    actions.append(el("span", { class: "detail-note", text: "conid는 IBKR로 실행했을 때 'IBKR에서 찾기'로 채울 수 있어요." }));
  }
  actions.append(
    el("button", {
      type: "button",
      class: "btn small",
      text: `${SLEEVE_LABEL[r.sleeve]}에서 빼기`,
      onclick: () => removeAllocation(r.key),
    }),
  );
  box.append(actions);
  const c = S.contracts[r.symbol];
  if (c) box.append(renderCandidates(r.symbol, c));
  return box;
}

function field(label, control, missing = false) {
  return el("label", { class: `field${missing ? " missing" : ""}` }, el("span", { text: label }), control);
}

async function findContracts(symbol, exchange) {
  S.contracts[symbol] = { loading: true, items: [], error: "" };
  renderWeights();
  try {
    const query = `symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}`;
    const res = await api("GET", `/api/contracts?${query}`);
    S.contracts[symbol] = { loading: false, items: res.candidates || [], error: "" };
  } catch (err) {
    S.contracts[symbol] = { loading: false, items: [], error: err.message };
  }
  renderWeights();
}

function renderCandidates(symbol, c) {
  const wrap = el("div", { class: "field" }, el("span", { text: `${symbol} 후보` }));
  if (c.loading) wrap.append(el("p", { class: "detail-note", text: "IBKR에서 찾는 중이에요." }));
  else if (c.error) wrap.append(el("p", { class: "form-error" }, icon("x", "critical"), c.error));
  else if (!c.items.length) wrap.append(el("p", { class: "detail-note", text: "후보가 없어요. IBKR 앱에서 종목을 검색해 conid를 확인하세요." }));
  else {
    const list = el("ul", { class: "candidates" });
    for (const item of c.items) {
      list.append(
        el(
          "li",
          {},
          el(
            "button",
            {
              type: "button",
              class: `btn small${item.recommended ? " rec" : ""}`,
              onclick: () => {
                setInstrument(symbol, { conid: item.conid });
                delete S.contracts[symbol];
                renderWeights();
                toast(`${symbol}의 conid를 넣었어요 (${item.conid}). 저장하면 반영돼요.`);
              },
            },
            el("span", { text: `${item.exchange} ${item.currency} ${item.name}` }),
            el("span", { text: `${item.conid}${item.recommended ? " 추천" : ""}` }),
          ),
        ),
      );
    }
    wrap.append(list);
  }
  return wrap;
}

function renderExits(m) {
  const group = el("div", { class: "group", dataset: { sleeve: "exit" } });
  group.append(
    el(
      "div",
      { class: "group-head" },
      el("h3", { text: "정리 대상" }),
      el("p", { class: "group-note", text: "목표에서 뺀 종목이에요. 보유 중이면 리밸런싱 때 팔아요. 매도 포함 리밸런싱으로 계산하면 지금 정리할 수 있어요." }),
    ),
  );
  const list = el("ul", { class: "rows" });
  for (const e of m.exits) {
    const confirming = S.confirmDelete === e.symbol;
    const held = e.value > 0;
    list.append(
      el(
        "li",
        { class: "row exit-row", dataset: { key: `exit:${e.symbol}` } },
        el("span", { class: `sw slot-${e.slot}`, "aria-hidden": "true" }),
        el("div", { class: "name" }, el("span", { class: "sym", text: e.symbol }), el("span", { class: "label", text: e.name })),
        el("span", { class: "now", text: m.hasHoldings ? pct(e.now) : "-" }),
        el(
          "span",
          { class: "action" },
          el("button", {
            type: "button",
            class: "btn small",
            text: confirming ? "한 번 더 누르면 지워요" : "목록에서 지우기",
            onclick: () => removeInstrument(e.symbol, held),
          }),
        ),
        el("p", { class: "meta", text: held ? `보유 ${usd(e.value)}. 지우면 앱이 이 종목을 더 이상 사고팔지 않아요.` : "보유하지 않아요. 지워도 돼요." }),
      ),
    );
  }
  group.append(list);
  return group;
}

function renderAddForm(sleeve) {
  const sat = sleeve === "satellite";
  const err = el("p", { class: "form-error", hidden: true });
  const sym = el("input", { type: "text", name: "symbol", maxlength: "12", autocomplete: "off", required: true, placeholder: "예: CSPX" });
  const name = el("input", { type: "text", name: "name", maxlength: "120", placeholder: "예: iShares Core S&P 500 UCITS ETF" });
  const exch = el("input", { type: "text", name: "exchange", maxlength: "20", value: "LSE" });
  const target = el("input", { type: "number", name: "target", inputmode: "decimal", min: "0.5", max: "100", step: "0.5", required: true });
  const label = el("input", { type: "text", name: "label", maxlength: "60" });
  const thesis = el("textarea", { name: "thesis", rows: "2", maxlength: "500" });
  const inval = el("textarea", { name: "invalidation", rows: "2", maxlength: "500" });
  sym.addEventListener("blur", () => {
    const s = sym.value.trim().toUpperCase();
    sym.value = s;
    const known = S.draft.portfolio.instruments.find((i) => i.symbol === s);
    if (known) {
      if (!name.value) name.value = known.name;
      exch.value = known.exchange;
    }
  });
  const form = el(
    "form",
    {
      class: "add-form",
      dataset: { sleeve },
      onsubmit: (e) => {
        e.preventDefault();
        const problem = addAllocation(sleeve, {
          symbol: sym.value,
          name: name.value,
          exchange: exch.value,
          target: target.value,
          label: label.value,
          thesis: thesis.value,
          invalidation: inval.value,
        });
        if (problem) {
          err.hidden = false;
          err.replaceChildren(icon("x", "critical"), problem);
        }
      },
    },
    el("h4", { text: `${SLEEVE_LABEL[sleeve]}에 종목 추가` }),
    el("div", { class: "field-row" }, field("티커", sym), field("거래소", exch), field("목표 (%)", target)),
    field("상품 이름", name),
    field("설명", label),
    sat ? field("담는 이유", thesis) : null,
    sat ? field("틀렸다고 볼 조건", inval) : null,
    el("p", { class: "detail-note", text: `통화는 ${S.app.base_currency}만 쓸 수 있어요. 추가해도 저장 전까지는 초안이에요.` }),
    err,
    el(
      "div",
      { class: "form-actions" },
      el("button", { type: "button", class: "btn", text: "취소", onclick: () => ((S.adding = null), renderWeights()) }),
      el("button", { type: "submit", class: "btn primary", text: "초안에 추가" }),
    ),
  );
  return form;
}

function updateDerived(m = model()) {
  const scale = bulletScale(m);
  // Sum and problems
  const sum = $("#sum");
  sum.replaceChildren();
  const ok = Math.abs(m.sum - 100) <= 0.01;
  sum.classList.toggle("off", !ok);
  if (ok) sum.append(icon("check", "good"), "합계 100%");
  else {
    const gap = m.sum - 100;
    sum.append(icon("warn", "warning"), `합계 ${plain(m.sum)}% (${plain(Math.abs(+gap.toFixed(2)))}%p ${gap > 0 ? "넘쳐요" : "모자라요"})`);
  }
  const problems = $("#problems");
  problems.replaceChildren();
  const list = (S.draft.problems || []).filter((p) => !(ok === false && p.startsWith("목표 비중 합계")));
  problems.hidden = !list.length;
  for (const p of list) problems.append(el("li", {}, icon("x", "critical"), el("span", { text: p })));

  // Group figures
  for (const node of document.querySelectorAll(".group[data-sleeve]")) {
    const g = node.dataset.sleeve;
    const fig = node.querySelector('[data-role="group-figures"]');
    if (!fig || !(g in m.sleeves)) continue;
    const s = m.sleeves[g];
    fig.textContent = m.hasHoldings ? `목표 ${plain(s.target)}%, 지금 ${pct(s.now)}` : `목표 ${plain(s.target)}%`;
  }

  // Rows
  const drift = S.app.rules.drift_threshold_pp;
  for (const r of m.rows) {
    const row = document.querySelector(`.row[data-key="${CSS.escape(r.key)}"]`);
    if (!row) continue;
    const bar = row.querySelector('[data-role="bar"]');
    const tickSaved = row.querySelector('[data-role="tick-saved"]');
    const tickDraft = row.querySelector('[data-role="tick-draft"]');
    bar.style.width = m.hasHoldings ? `${Math.min(100, (r.now / scale) * 100)}%` : "0";
    const changed = r.saved === null || Math.abs(r.saved - r.target) > 1e-9;
    tickSaved.hidden = r.saved === null;
    if (r.saved !== null) tickSaved.style.left = `${(r.saved / scale) * 100}%`;
    tickDraft.hidden = !changed;
    tickDraft.style.left = `${(r.target / scale) * 100}%`;
    row.querySelector('[data-role="now"]').textContent = m.hasHoldings ? pct(r.now) : "-";
    const diffNode = row.querySelector('[data-role="diff"]');
    if (m.hasHoldings) {
      const d = r.now - r.target;
      diffNode.textContent = `${signed(d)}%p`;
      diffNode.classList.toggle("big", Math.abs(d) >= drift);
    } else {
      diffNode.textContent = "-";
      diffNode.classList.remove("big");
    }
    const input = row.querySelector(".target input");
    if (input) {
      input.classList.toggle("changed", changed);
      input.title = changed ? (r.saved === null ? "새로 추가한 종목이에요" : `저장된 목표 ${plain(r.saved)}%`) : "";
    }
    const label = row.querySelector('[data-role="label"]');
    if (label && document.activeElement?.closest(".detail") === null) label.textContent = r.label || "";
  }
  renderBar();
  renderOverview();
}

// Draft edits ----------------------------------------------------------------------

function findAllocation(key) {
  const [sleeve, symbol] = key.split(":");
  return S.draft.portfolio.allocations.find((a) => a.sleeve === sleeve && a.symbol === symbol);
}

function setAllocation(key, patch) {
  const a = findAllocation(key);
  if (!a) return;
  Object.assign(a, patch);
  if ("label" in patch) {
    const label = document.querySelector(`.row[data-key="${CSS.escape(key)}"] [data-role="label"]`);
    if (label) label.textContent = patch.label;
  }
  if ("thesis" in patch || "invalidation" in patch) {
    const row = document.querySelector(`.row[data-key="${CSS.escape(key)}"]`);
    if (row) {
      const areas = row.querySelectorAll("textarea");
      areas.forEach((t) => t.closest(".field").classList.toggle("missing", !t.value.trim()));
    }
  }
  queueSync();
  updateDerived();
}

function setInstrument(symbol, patch) {
  const inst = S.draft.portfolio.instruments.find((i) => i.symbol === symbol);
  if (!inst) return;
  Object.assign(inst, patch);
  queueSync();
  renderBar();
}

function removeAllocation(key) {
  const [sleeve, symbol] = key.split(":");
  const P = S.draft.portfolio;
  P.allocations = P.allocations.filter((a) => !(a.sleeve === sleeve && a.symbol === symbol));
  const still = P.allocations.some((a) => a.symbol === symbol);
  const held = S.holdings ? S.holdings.positions.some((p) => p.symbol === symbol && p.quantity > 0) : true;
  if (!still && !held) P.instruments = P.instruments.filter((i) => i.symbol !== symbol);
  S.expanded.delete(key);
  queueSync();
  renderWeights();
  toast(
    still || !held
      ? `${symbol}: ${SLEEVE_LABEL[sleeve]}에서 뺐어요. 합계를 100%로 맞춰 주세요.`
      : `${symbol}: 목표에서 뺐어요. 보유 중이라 정리 대상으로 남겨 뒀어요.`,
  );
}

function removeInstrument(symbol, held) {
  if (held && S.confirmDelete !== symbol) {
    S.confirmDelete = symbol;
    renderWeights();
    setTimeout(() => {
      if (S.confirmDelete === symbol) {
        S.confirmDelete = null;
        renderWeights();
      }
    }, 5000);
    return;
  }
  S.confirmDelete = null;
  const P = S.draft.portfolio;
  P.instruments = P.instruments.filter((i) => i.symbol !== symbol);
  queueSync();
  renderWeights();
}

function addAllocation(sleeve, f) {
  const symbol = f.symbol.trim().toUpperCase();
  if (!/^[A-Z0-9][A-Z0-9.\-]{0,11}$/.test(symbol)) return "티커는 영문 대문자와 숫자로 12자 이내여야 해요.";
  const target = parseFloat(f.target);
  if (!Number.isFinite(target) || target <= 0 || target > 100) return "목표 비중은 0보다 크고 100 이하인 숫자여야 해요.";
  const P = S.draft.portfolio;
  if (P.allocations.some((a) => a.sleeve === sleeve && a.symbol === symbol)) return `${symbol}: 이미 ${SLEEVE_LABEL[sleeve]}에 있어요. 목록에서 비중을 바꾸세요.`;
  const known = P.instruments.find((i) => i.symbol === symbol);
  if (!known) {
    if (!f.name.trim()) return "처음 넣는 종목이라 상품 이름이 필요해요.";
    P.instruments.push({ symbol, name: f.name.trim(), exchange: (f.exchange || "LSE").trim().toUpperCase(), currency: S.app.base_currency, conid: null });
  }
  P.allocations.push({
    sleeve,
    symbol,
    target: Math.round(target * 100) / 100,
    label: f.label.trim(),
    thesis: f.thesis.trim(),
    invalidation: f.invalidation.trim(),
  });
  P.allocations.sort((a, b) => SLEEVES.indexOf(a.sleeve) - SLEEVES.indexOf(b.sleeve));
  S.adding = null;
  queueSync();
  renderWeights();
  toast(`${symbol}: ${SLEEVE_LABEL[sleeve]}에 ${plain(target)}%로 넣었어요. 합계를 100%로 맞춰 주세요.`);
  return "";
}

function queueSync() {
  S.draft.dirty = true;
  clearTimeout(S.sync.timer);
  S.sync.timer = setTimeout(flushSync, 350);
}

function flushSync() {
  clearTimeout(S.sync.timer);
  S.sync.timer = null;
  if (S.sync.inflight) {
    S.sync.pending = true;
    return S.sync.inflight;
  }
  const body = { version: S.draft.version, portfolio: S.draft.portfolio };
  S.sync.inflight = api("PUT", "/api/draft", body)
    .then((view) => {
      S.draft.version = view.version;
      S.draft.problems = view.problems;
      S.draft.changes = view.changes;
      S.draft.dirty = view.dirty;
      if (!S.sync.pending && !S.sync.timer) S.draft.portfolio = view.portfolio;
      updateDerived();
    })
    .catch(async (err) => {
      if (err.status === 409 && err.data && err.data.draft) {
        S.draft = err.data.draft;
        renderWeights();
        toast("초안이 다른 곳에서 바뀌어서 다시 불러왔어요.");
      } else {
        toast(err.message);
      }
    })
    .finally(() => {
      S.sync.inflight = null;
      if (S.sync.pending) {
        S.sync.pending = false;
        flushSync();
      }
    });
  return S.sync.inflight;
}

async function settleDraft() {
  if (S.sync.timer) flushSync();
  while (S.sync.inflight) await S.sync.inflight;
}

// Draft bar ------------------------------------------------------------------------

function renderBar() {
  const status = $("#draft-status");
  status.replaceChildren();
  status.className = "draft-status";
  const problems = S.draft.problems || [];
  const changes = S.draft.changes || [];
  const m = model();
  const sumOk = Math.abs(m.sum - 100) <= 0.01;
  if (S.chatBusy) {
    status.append(el("span", { class: "pencil", text: "초안" }), el("span", { class: "summary", text: "채팅이 초안을 고치는 중이에요." }));
  } else if (!sumOk || problems.length) {
    const text = !sumOk ? `합계가 ${plain(m.sum)}%예요. 100%로 맞춰야 저장하고 주문할 수 있어요.` : problems[0];
    status.append(icon("warn", "warning"), el("span", { class: "summary", text }));
  } else if (S.draft.dirty) {
    const summary = changes.length ? `저장 안 한 변경 ${changes.length}개: ${changes.map((c) => c.text).join(", ")}` : "저장 안 한 변경이 있어요.";
    status.append(el("span", { class: "pencil", text: "초안" }), el("span", { class: "summary", text: summary, title: summary }));
  } else {
    status.classList.add("clean");
    status.append(el("span", { class: "summary", text: "저장된 목표 그대로예요." }));
  }
  const blocked = !sumOk || problems.length > 0;
  $("#draft-reset").hidden = !S.draft.dirty;
  $("#draft-save").hidden = !S.draft.dirty;
  $("#draft-save").disabled = blocked || S.chatBusy;
  $("#draft-reset").disabled = S.chatBusy;
  $("#open-plan").disabled = blocked || S.chatBusy;
  const jobOpen = $("#job-open");
  jobOpen.hidden = !(S.job && S.job.active);
}

// Holdings ------------------------------------------------------------------------

async function loadHoldings(refresh) {
  S.holdingsLoading = true;
  renderHeader();
  try {
    S.holdings = await api("GET", `/api/holdings${refresh ? "?refresh=true" : ""}`);
    S.holdingsError = null;
  } catch (err) {
    S.holdingsError = { message: err.message };
  } finally {
    S.holdingsLoading = false;
  }
  renderHeader();
  renderWeights();
}

// Plan dialog ----------------------------------------------------------------------

function openDialog() {
  const d = $("#plan-dialog");
  if (!d.open) d.showModal();
}

function closeDialog() {
  const d = $("#plan-dialog");
  if (d.open) d.close();
  S.dialog = null;
}

async function openPlan(force = false) {
  await settleDraft();
  const m = model();
  if (Math.abs(m.sum - 100) > 0.01 || (S.draft.problems || []).length) {
    toast("초안에 고칠 점이 있어요. 목표 비중 위의 안내를 확인하세요.");
    return;
  }
  S.dialog = "plan";
  S.planLoading = true;
  S.plan = null;
  renderPlanLoading("주문 계획을 계산하고 있어요.");
  openDialog();
  try {
    S.plan = await api("POST", "/api/plan", { force_rebalance: force });
    renderPlan();
  } catch (err) {
    renderPlanError(err, force);
  } finally {
    S.planLoading = false;
  }
}

function renderPlanLoading(text) {
  $("#plan-title").replaceChildren("주문 계획");
  $("#plan-body").replaceChildren(el("p", { class: "loading", text }));
  $("#plan-foot").replaceChildren(el("button", { type: "button", class: "btn", text: "닫기", onclick: closeDialog }));
}

function renderPlanError(err, force) {
  const body = $("#plan-body");
  body.replaceChildren(el("div", { class: "callout" }, icon("x", "critical"), el("div", {}, el("p", { text: err.message }))));
  const problems = (err.data && err.data.problems) || [];
  if (problems.length) body.append(el("ul", { class: "list-plain" }, problems.map((p) => el("li", {}, icon("x", "critical"), el("span", { text: p })))));
  $("#plan-foot").replaceChildren(
    el("button", { type: "button", class: "btn", text: "닫기", onclick: closeDialog }),
    el("button", { type: "button", class: "btn primary", text: "다시 계산", onclick: () => openPlan(force) }),
  );
}

function ordersTable(plan, { laterNote = false } = {}) {
  const table = el("table", { class: "orders" });
  table.append(
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", { text: "구분" }),
        el("th", { text: "종목" }),
        el("th", { class: "r", text: "수량" }),
        el("th", { class: "r", text: "지정가" }),
        el("th", { class: "r", text: "금액" }),
        el("th", { class: "r fee-col", text: "예상 수수료" }),
      ),
    ),
  );
  const tbody = el("tbody");
  let laterShown = false;
  for (const o of plan.orders) {
    const later = laterNote && !o.sent_now;
    if (later && !laterShown) {
      laterShown = true;
      tbody.append(el("tr", { class: "group-row" }, el("td", { colspan: "6", text: "매도 체결 뒤 다시 계산할 매수 (지금은 예상)" })));
    }
    tbody.append(
      el(
        "tr",
        { class: later ? "later" : "" },
        el("td", { class: "side", text: o.side_label }),
        el("td", { text: o.symbol }),
        el("td", { class: "r", text: `${o.quantity}주` }),
        el("td", { class: "r", text: usd(o.limit_price, 2) }),
        el("td", { class: "r", text: usd(o.notional, 2) }),
        el("td", { class: "r fee-col", text: usd(o.fee, 2) }),
      ),
    );
  }
  table.append(tbody);
  return table;
}

function renderPlan() {
  const p = S.plan;
  const live = p.broker && p.broker.live;
  $("#plan-title").replaceChildren("주문 계획", el("span", { class: "mode-chip", text: p.mode_label }));
  const body = $("#plan-body");
  body.replaceChildren();
  body.append(el("p", { class: "reason", text: p.reason }));

  if (live) body.append(el("div", { class: "callout" }, icon("warn", "critical"), el("p", { text: `${p.broker.label}예요. 실제 돈으로 주문이 나가요.` })));
  if (p.dirty)
    body.append(
      el("div", { class: "callout pencil" }, icon("info"), el("p", { text: "저장하지 않은 새 목표로 계산했어요. 주문을 보내면 새 목표도 함께 저장돼요." })),
    );
  let repeat = null;
  if (p.month && p.month.executed_at) {
    repeat = el("input", { type: "checkbox", id: "allow-repeat", onchange: () => renderPlanFoot() });
    body.append(
      el(
        "div",
        { class: "callout" },
        icon("warn", "warning"),
        el(
          "div",
          {},
          el("p", { text: `${dayLabel(p.month.executed_at)}에 이번 달 주문을 이미 보냈어요. 같은 달에 두 번 보내는 건 막아 뒀어요.` }),
          el("label", {}, repeat, "이번 달에 한 번 더 보낼게요"),
        ),
      ),
    );
  }

  if (p.orders.length) {
    if (p.two_step)
      body.append(
        el(
          "div",
          { class: "callout" },
          icon("info"),
          el("p", { text: "매도 주문만 먼저 보내요. IBKR에서 체결을 확인한 뒤, 그때 남은 현금으로 매수 계획을 다시 계산해서 한 번 더 확인받아요." }),
        ),
      );
    body.append(ordersTable(p, { laterNote: p.two_step }));
    const traded = p.buy_total + p.sell_total;
    body.append(
      el(
        "div",
        { class: "totals" },
        el("div", { class: "total" }, el("span", { text: "매수 합계" }), el("strong", { class: "num", text: usd(p.buy_total, 2) })),
        p.sell_total ? el("div", { class: "total" }, el("span", { text: "매도 합계" }), el("strong", { class: "num", text: usd(p.sell_total, 2) })) : null,
        el(
          "div",
          { class: "total" },
          el("span", { text: "예상 수수료" }),
          el("strong", { class: "num", text: usd(p.fee_total, 2) }),
          el("small", { text: `주문 금액의 ${traded ? ((p.fee_total / traded) * 100).toFixed(2) : "0.00"}%` }),
        ),
        el("div", { class: "total" }, el("span", { text: "주문 후 현금 약" }), el("strong", { class: "num", text: usd(p.projected_cash, 2) })),
      ),
    );
    const fx = p.fx && p.fx.fee ? ` 입금한 SGD를 USD로 바꿀 땐 IBKR 환전 수수료가 따로 들어요 (월 ${usd(p.fx.amount)} 기준 약 ${usd(p.fx.fee, 2)}).` : "";
    body.append(el("p", { class: "fine", text: `수수료 기준: ${p.fee_rule}. 실제 금액은 IBKR 체결 내역에서 확인하세요.${fx}` }));
    if (p.leftover_note) body.append(el("p", { class: "fine", text: p.leftover_note }));
  } else {
    body.append(el("div", { class: "callout" }, icon("check", "good"), el("p", { text: "이번에 보낼 주문이 없어요. 목표 비중에 충분히 가까워요." })));
  }

  if (p.warnings.length) {
    body.append(
      el("div", {}, el("h3", { class: "visually-hidden", text: "확인할 점" }), el("ul", { class: "list-plain" }, p.warnings.map((w) => el("li", {}, icon("warn", "warning"), el("span", { text: w }))))),
    );
  }
  if (p.problems.length) {
    body.append(
      el(
        "div",
        { class: "callout" },
        icon("x", "critical"),
        el("div", {}, el("p", { text: "안전 점검에 걸려서 보낼 수 없어요." }), el("ul", { class: "list-plain" }, p.problems.map((x) => el("li", { text: x })))),
      ),
    );
  }

  const after = el("table", { class: "after" });
  after.append(
    el(
      "thead",
      {},
      el("tr", {}, el("th", { text: "종목" }), el("th", { class: "r", text: "목표" }), el("th", { class: "r", text: "지금" }), el("th", { class: "r", text: "주문 후" })),
    ),
  );
  const tb = el("tbody");
  for (const w of p.weights)
    tb.append(
      el(
        "tr",
        {},
        el("td", { text: w.exit ? `${w.symbol} (정리 대상)` : w.symbol }),
        el("td", { class: "r", text: pct(w.target) }),
        el("td", { class: "r", text: pct(w.now) }),
        el("td", { class: "r", text: pct(w.after) }),
      ),
    );
  for (const s of p.sleeves)
    tb.append(
      el(
        "tr",
        {},
        el("td", { text: SLEEVE_LABEL[s.sleeve] }),
        el("td", { class: "r", text: pct(s.target) }),
        el("td", { class: "r", text: pct(s.now) }),
        el("td", { class: "r", text: pct(s.after) }),
      ),
    );
  tb.append(el("tr", {}, el("td", { text: "현금" }), el("td", { class: "r", text: "" }), el("td", { class: "r", text: pct(p.cash_now) }), el("td", { class: "r", text: pct(p.cash_after) })));
  after.append(tb);
  body.append(el("details", { class: "more" }, el("summary", { text: "주문 후 비중 보기" }), after));

  const force = el("input", { type: "checkbox", checked: p.force_rebalance, onchange: (e) => openPlan(e.target.checked) });
  body.append(el("label", { class: "check" }, force, "매도까지 포함해서 목표에 맞추기 (리밸런싱으로 계산)"));
  renderPlanFoot();
}

function renderPlanFoot() {
  const p = S.plan;
  const foot = $("#plan-foot");
  foot.replaceChildren();
  const repeat = $("#allow-repeat");
  const needsRepeat = Boolean(p.month && p.month.executed_at);
  const blocked = !p.orders.length || p.problems.length > 0 || (needsRepeat && !(repeat && repeat.checked));
  let label = p.two_step ? `매도 주문 보내기 (${p.send_count}건)` : `주문 보내기 (${p.send_count}건)`;
  if (p.dirty) label = `목표 저장하고 ${label}`;
  if (p.broker && p.broker.live) label = `실계좌로 ${label}`;
  if (!p.orders.length) label = "보낼 주문이 없어요";
  foot.append(
    el("span", { class: "left", text: `계획은 10분 동안 쓸 수 있어요.` }),
    el("button", { type: "button", class: "btn", text: "닫기", onclick: closeDialog }),
    el("button", { type: "button", class: "btn primary", id: "send-orders", text: label, disabled: blocked, onclick: sendOrders }),
  );
}

async function sendOrders(e) {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "보내는 중이에요";
  const repeat = $("#allow-repeat");
  try {
    S.job = await api("POST", "/api/orders", { plan_id: S.plan.id, allow_repeat: Boolean(repeat && repeat.checked) });
    S.dialog = "job";
    if (S.plan.dirty) await reloadState();
    renderJob();
    startPolling();
  } catch (err) {
    toast(err.message);
    btn.disabled = false;
    renderPlanFoot();
  }
}

const STATUS = {
  pending: ["대기", "clock", ""],
  sending: ["보내는 중", "clock", ""],
  submitted: ["접수됨", "check", "good"],
  declined: ["보내지 않음", "minus", ""],
  rejected: ["거부됨", "x", "critical"],
  error: ["확인 필요", "x", "critical"],
  skipped: ["건너뜀", "minus", ""],
};

function renderJob() {
  const j = S.job;
  if (!j) return;
  $("#plan-title").replaceChildren("주문 진행", el("span", { class: "mode-chip", text: j.mode_label }));
  const body = $("#plan-body");
  body.replaceChildren();

  if (j.question) {
    const q = j.question;
    body.append(
      el(
        "div",
        { class: "question", role: "alertdialog", "aria-labelledby": "q-title" },
        el("h3", { id: "q-title" }, icon("warn", "warning"), "IBKR가 주문 전에 확인을 요청했어요"),
        el("p", { text: `${q.side_label} ${q.symbol} ${q.quantity}주, 지정가 ${usd(q.limit_price, 2)}` }),
        el("ul", {}, q.messages.map((m) => el("li", { text: m }))),
        el("p", { class: "fine", text: "내용을 읽고 이 주문을 보낼지 직접 정해 주세요. 15분 안에 답하지 않으면 보내지 않아요." }),
        el(
          "div",
          { class: "actions" },
          el("button", { type: "button", class: "btn primary", text: "승인하고 이 주문 보내기", onclick: () => answer(true) }),
          el("button", { type: "button", class: "btn", text: "이 주문 보내지 않기", onclick: () => answer(false) }),
        ),
      ),
    );
  }

  const table = el("table", { class: "orders" });
  table.append(
    el("thead", {}, el("tr", {}, el("th", { text: "구분" }), el("th", { text: "종목" }), el("th", { class: "r", text: "수량" }), el("th", { class: "r", text: "지정가" }), el("th", { text: "상태" }))),
  );
  const tb = el("tbody");
  for (const o of j.orders) {
    const [text, ic, tone] = STATUS[o.status] || [o.status, "info", ""];
    tb.append(
      el(
        "tr",
        {},
        el("td", { class: "side", text: o.side_label }),
        el("td", { text: o.symbol }),
        el("td", { class: "r", text: `${o.quantity}주` }),
        el("td", { class: "r", text: usd(o.limit_price, 2) }),
        el(
          "td",
          {},
          el("span", { class: `status${tone ? "" : " muted"}` }, icon(ic, tone), text),
          o.detail ? el("div", { class: "fine", text: o.detail }) : null,
        ),
      ),
    );
  }
  table.append(tb);
  body.append(table);

  if (j.error) body.append(el("div", { class: "callout" }, icon("x", "critical"), el("p", { text: j.error })));

  const foot = $("#plan-foot");
  foot.replaceChildren();
  if (j.status === "running") {
    body.prepend(el("p", { class: "reason", text: "주문을 보내는 중이에요. 이 창을 닫아도 계속 보내요." }));
    foot.append(el("button", { type: "button", class: "btn", text: "닫기", onclick: closeDialog }));
  } else if (j.status === "question") {
    foot.append(el("span", { class: "left", text: "위 확인 요청에 답해야 다음 주문으로 넘어가요." }));
  } else if (j.status === "sells_done") {
    body.prepend(el("p", { class: "reason", text: "매도 주문을 보냈어요. IBKR에서 체결된 걸 확인한 뒤 매수 계획을 계산하세요." }));
    foot.append(
      el("button", { type: "button", class: "btn", text: "여기서 끝내기", onclick: finishJob }),
      el("button", { type: "button", class: "btn primary", text: "체결 확인했어요. 매수 계획 계산", onclick: buyPlan }),
    );
  } else if (j.status === "buy_plan_ready" && j.buy_plan) {
    const bp = j.buy_plan;
    body.append(el("h3", { class: "reason", text: "매수 계획 (지금 현금 기준)" }));
    if (bp.orders.length) {
      body.append(ordersTable(bp));
      body.append(el("p", { class: "fine", text: `매수 합계 ${usd(bp.buy_total, 2)}, 예상 수수료 ${usd(bp.fee_total, 2)}, 주문 후 현금 약 ${usd(bp.projected_cash, 2)}` }));
    } else {
      body.append(el("p", { class: "reason", text: "지금 현금으로 살 주문이 없어요." }));
    }
    for (const w of bp.warnings) body.append(el("p", { class: "fine", text: w }));
    for (const x of bp.problems) body.append(el("div", { class: "callout" }, icon("x", "critical"), el("p", { text: x })));
    foot.append(
      el("button", { type: "button", class: "btn", text: "여기서 끝내기", onclick: finishJob }),
      el("button", { type: "button", class: "btn", text: "다시 계산", onclick: buyPlan }),
      el("button", {
        type: "button",
        class: "btn primary",
        text: bp.orders.length ? `매수 주문 보내기 (${bp.buys_count ?? bp.send_count}건)` : "끝내기",
        disabled: bp.problems.length > 0,
        onclick: bp.orders.length ? sendBuys : finishJob,
      }),
    );
  } else {
    const sent = j.submitted;
    body.prepend(
      el(
        "div",
        { class: "callout" },
        icon(sent ? "check" : "info", sent ? "good" : ""),
        el("p", { text: sent ? `주문 ${sent}건이 접수됐어요. 체결 여부는 IBKR에서 확인하세요.` : "접수된 주문이 없어요. 이번 달 실행으로 기록하지 않았어요." }),
      ),
    );
    foot.append(el("button", { type: "button", class: "btn primary", text: "닫기", onclick: closeDialog }));
  }
}

function startPolling() {
  stopPolling();
  S.pollTimer = setInterval(pollJob, 1000);
}

function stopPolling() {
  clearInterval(S.pollTimer);
  S.pollTimer = null;
}

async function pollJob() {
  if (!S.job) return stopPolling();
  try {
    const before = S.job.status;
    S.job = await api("GET", `/api/jobs/${S.job.id}`);
    const q = S.job.question;
    if (q && q.id !== S.lastQuestionId) {
      // Bring a new broker question to the front once; closing the dialog is respected after that.
      S.lastQuestionId = q.id;
      if (S.dialog !== "job") {
        S.dialog = "job";
        openDialog();
        toast("IBKR가 주문 확인을 요청했어요.");
      }
    }
    if (S.dialog === "job") renderJob();
    renderBar();
    if (!["running", "question"].includes(S.job.status)) {
      stopPolling();
      if (before !== S.job.status) afterJobStep();
    }
  } catch (err) {
    stopPolling();
    toast(err.message);
  }
}

async function afterJobStep() {
  await reloadState();
  await loadHoldings(true);
}

async function answer(approve) {
  const q = S.job && S.job.question;
  if (!q) return;
  for (const b of document.querySelectorAll(".question button")) b.disabled = true;
  try {
    S.job = await api("POST", `/api/jobs/${S.job.id}/answer`, { question_id: q.id, approve });
    renderJob();
    startPolling();
  } catch (err) {
    toast(err.message);
    renderJob();
  }
}

async function buyPlan() {
  renderPlanLoading("지금 잔고로 매수 계획을 계산하고 있어요.");
  try {
    S.job = await api("POST", `/api/jobs/${S.job.id}/buy-plan`);
    if (S.job.buy_plan) S.job.buy_plan.buys_count = S.job.buy_plan.orders.length;
  } catch (err) {
    toast(err.message);
  }
  renderJob();
}

async function sendBuys(e) {
  e.currentTarget.disabled = true;
  try {
    S.job = await api("POST", `/api/jobs/${S.job.id}/buys`);
    renderJob();
    startPolling();
  } catch (err) {
    toast(err.message);
    renderJob();
  }
}

async function finishJob() {
  try {
    S.job = await api("POST", `/api/jobs/${S.job.id}/finish`);
  } catch (err) {
    toast(err.message);
  }
  renderJob();
  renderBar();
  await afterJobStep();
}

// Chat ---------------------------------------------------------------------------

function renderRich(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text || "").split("\n");
  let list = null;
  let para = null;
  const inline = (s) => {
    const out = [];
    const re = /\*\*([^*]+)\*\*/g;
    let last = 0;
    let mt;
    while ((mt = re.exec(s))) {
      if (mt.index > last) out.push(s.slice(last, mt.index));
      out.push(el("strong", { text: mt[1] }));
      last = re.lastIndex;
    }
    if (last < s.length) out.push(s.slice(last));
    return out;
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (!line.trim()) {
      list = null;
      para = null;
      continue;
    }
    if (bullet) {
      para = null;
      if (!list) {
        list = el("ul");
        frag.append(list);
      }
      list.append(el("li", {}, inline(bullet[1].replace(/^#+\s*/, ""))));
    } else {
      list = null;
      const content = line.replace(/^#+\s*/, "");
      if (!para) {
        para = el("p");
        frag.append(para);
      } else para.append(el("br"));
      para.append(...inline(content));
    }
  }
  return frag;
}

function renderChat() {
  const chat = S.app.chat;
  $("#chat-model").textContent = chat.enabled ? `${chat.provider} ${chat.model}` : "꺼져 있어요";
  const log = $("#chat-log");
  log.replaceChildren();
  if (!chat.enabled) {
    log.append(
      el(
        "div",
        { class: "chat-off" },
        el("p", { text: chat.reason || "채팅이 꺼져 있어요." }),
        el("p", {}, "키는 프로젝트 폴더의 ", el("code", { text: ".env" }), " 파일에 한 줄로 넣으면 돼요. 예: ", el("code", { text: "ANTHROPIC_API_KEY=sk-ant-..." })),
        el("p", {}, "OpenAI나 Gemini 키를 쓰려면 ", el("code", { text: "config.yaml" }), "의 ", el("code", { text: "assistant.provider" }), "를 바꾸고 서버를 다시 켜세요."),
      ),
    );
  } else if (!chat.transcript.length && !S.chatBusy) {
    log.append(
      el(
        "div",
        { class: "suggestions" },
        el("p", { text: "이렇게 말해 보세요." }),
        SUGGESTIONS.map((s) =>
          el("button", {
            type: "button",
            class: "btn",
            text: s,
            onclick: () => {
              const input = $("#chat-input");
              input.value = s;
              input.focus();
            },
          }),
        ),
      ),
    );
  }
  for (const m of chat.transcript) log.append(renderMessage(m, chat));
  if (S.chatBusy) {
    if (S.pendingChat) log.append(renderMessage({ role: "user", text: S.pendingChat }, chat));
    log.append(el("p", { class: "typing", text: "답을 쓰는 중이에요" }));
  }
  log.scrollTop = log.scrollHeight;
  const input = $("#chat-input");
  input.disabled = !chat.enabled || S.chatBusy;
  $("#chat-send").disabled = !chat.enabled || S.chatBusy;
  $("#chat-reset").disabled = !chat.enabled || S.chatBusy || !chat.transcript.length;
}

function renderMessage(m, chat) {
  if (m.role === "user") return el("div", { class: "msg user", text: m.text });
  if (m.role === "error")
    return el(
      "div",
      { class: "msg error" },
      icon("x", "critical"),
      el("div", {}, el("p", { text: m.text }), m.changes && m.changes.length ? changeList(m.changes) : null),
    );
  const box = el("div", { class: "msg assistant" }, el("span", { class: "msg-who", text: chat.provider }));
  box.append(renderRich(m.text));
  if (m.changes && m.changes.length) box.append(changeList(m.changes));
  return box;
}

function changeList(changes) {
  return el("div", { class: "changes" }, el("strong", { text: "초안에 반영했어요" }), el("ul", {}, changes.map((c) => el("li", { text: c }))));
}

async function sendChat(text) {
  const message = text.trim();
  if (!message || S.chatBusy) return;
  await settleDraft();
  S.chatBusy = true;
  S.pendingChat = message;
  $("#chat-input").value = "";
  renderChat();
  renderWeights();
  try {
    const res = await api("POST", "/api/chat", { message });
    S.app.chat = res.chat;
    S.draft = res.draft;
    if (res.changes && res.changes.length) {
      toast(`초안에 ${res.changes.length}곳을 반영했어요. 저장이나 주문은 버튼으로 해요.`);
    }
  } catch (err) {
    toast(err.message);
    try {
      S.app.chat = await api("GET", "/api/chat");
    } catch {
      /* keep the old transcript */
    }
  } finally {
    S.chatBusy = false;
    S.pendingChat = "";
    renderChat();
    renderWeights();
    $("#chat-input").focus();
  }
}

// Page wiring ------------------------------------------------------------------------

function setPane(pane) {
  document.body.dataset.pane = pane;
  $("#tab-weights").setAttribute("aria-selected", pane === "weights" ? "true" : "false");
  $("#tab-chat").setAttribute("aria-selected", pane === "chat" ? "true" : "false");
}

async function reloadState() {
  try {
    const app = await api("GET", "/api/state");
    S.app = app;
    if (!S.sync.timer && !S.sync.inflight) S.draft = app.draft;
    if (app.job) S.job = app.job;
    renderHeader();
    renderChat();
    renderWeights();
  } catch (err) {
    toast(err.message);
  }
}

function bind() {
  $("#refresh").addEventListener("click", () => loadHoldings(true));
  $("#tab-weights").addEventListener("click", () => setPane("weights"));
  $("#tab-chat").addEventListener("click", () => setPane("chat"));
  $("#plan-close").addEventListener("click", closeDialog);
  $("#plan-dialog").addEventListener("close", () => (S.dialog = null));
  $("#open-plan").addEventListener("click", () => openPlan(false));
  $("#job-open").addEventListener("click", () => {
    S.dialog = "job";
    renderJob();
    openDialog();
    if (["running", "question"].includes(S.job.status)) startPolling();
  });
  $("#draft-reset").addEventListener("click", async () => {
    await settleDraft();
    try {
      S.draft = await api("POST", "/api/draft/reset");
      S.expanded.clear();
      renderWeights();
      toast("저장된 목표로 되돌렸어요.");
    } catch (err) {
      toast(err.message);
    }
  });
  $("#draft-save").addEventListener("click", async () => {
    await settleDraft();
    try {
      S.app = await api("POST", "/api/draft/save");
      S.draft = S.app.draft;
      renderHeader();
      renderWeights();
      toast("목표를 저장했어요. 이전 파일은 history 폴더에 남아요.");
    } catch (err) {
      const problems = (err.data && err.data.problems) || [];
      toast(problems.length ? problems[0] : err.message);
    }
  });
  $("#chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    sendChat($("#chat-input").value);
  });
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      sendChat(e.target.value);
    }
  });
  $("#chat-reset").addEventListener("click", async () => {
    try {
      S.app.chat = await api("POST", "/api/chat/reset");
      renderChat();
    } catch (err) {
      toast(err.message);
    }
  });
  $("#donut").addEventListener("keydown", (e) => {
    if (e.key === "Escape" && S.view !== "all") drill("all");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && S.view !== "all" && !$("#plan-dialog").open && document.activeElement?.closest(".overview")) drill("all");
  });
}

async function boot() {
  bind();
  try {
    S.app = await api("GET", "/api/state");
  } catch (err) {
    $("#groups").replaceChildren(el("p", { class: "notice", text: err.message }));
    return;
  }
  S.draft = S.app.draft;
  S.job = S.app.job;
  renderHeader();
  renderChat();
  renderWeights();
  loadHoldings(false);
  if (S.job && S.job.active) {
    renderBar();
    if (["running", "question"].includes(S.job.status)) startPolling();
  }
}

boot();
