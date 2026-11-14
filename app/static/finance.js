// Financial performance (Plan and invest, tab "Financial performance"): revenue, operating costs, EBITDA, total costs and profit before tax
// for the regulatory year to date against budget, the last twelve months as a chart, and the profit and loss by line (gold gld_finance_month,
// illustrative rates; see pipelines/finance.py).
import { api, h, money, table, chart, chartBox, axis } from "./ui.js";

const big$ = (v) => v < 0 ? `-${big$(-v)}` : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `$${Math.round(v / 1e3)}k` : `$${Math.round(v)}`;
const noun = (n, one, many) => n === 1 ? one : many;   // the noun for a count

export async function financeTab(body) {
  const f = await api("/api/finance");
  body.append(f && f.months ? financeCard(f) : h("p", { class: "muted" }, "No financial figures are available yet. The finance data has not been built."));
}

// Revenue, costs and profit: the regulatory year to date against budget, and the last twelve months (gold gld_finance_month).
function financeCard(f) {
  const a = f.ytd, b = f.ytd_budget, costs = (t) => t.opex + t.depreciation + t.finance_cost;
  const vs = (act, bud, lowerIsBetter) => {
    if (!bud) return h("div", { class: "lbl" }, "No budget has been set.");
    const d = act - bud, good = lowerIsBetter ? d <= 0 : d >= 0;
    return h("div", { class: "lbl", style: `color:${good ? "#2e9c6a" : "#d2462f"}` }, `${d >= 0 ? "+" : "-"}${big$(Math.abs(d))} against a budget of ${big$(bud)}.`);
  };
  const margin = (v) => a.revenue ? ` ${Math.round(100 * v / a.revenue)}% margin` : "";
  const k = (label, v, sub, extra) => h("div", { class: "fin-k" }, h("div", { class: "kh" }, label), h("div", { class: "midv num" }, big$(v)), sub, extra || null);
  const yr = `${f.regulatory_year - 1}-${String(f.regulatory_year).slice(2)}`;
  const c = chartBox("tall");
  const lines = table([{ l: "Line", k: "line_label" }, { l: "Group", k: "line_group_label" },
    { l: "Actual", f: (r) => money(r.actual), r: 1 }, { l: "Budget", f: (r) => money(r.budget), r: 1 },
    { l: "How it is worked out", f: (r) => h("span", { class: "muted small" }, r.basis, r.is_twin_driven ? " From the twin's own records." : "") }], f.lines);
  const det = h("details", { class: "fin-det" }, h("summary", {}, "Profit and loss by line, year to date"), lines);
  const el = h("div", { class: "card fin" },
    h("h3", {}, "Financial performance", h("span", { class: "sub" }, `Regulatory year ${yr} to date (${f.months_in_year} ${noun(f.months_in_year, "month", "months")}), illustrative rates.`)),
    h("div", { class: "fin-grid" },
      k("Revenue", a.revenue, vs(a.revenue, b.revenue)),
      k("Operating costs", a.opex, vs(a.opex, b.opex, true)),
      k("EBITDA", a.ebitda, vs(a.ebitda, b.ebitda), h("div", { class: "lbl" }, margin(a.ebitda))),
      k("Total costs", costs(a), vs(costs(a), costs(b), true), h("div", { class: "lbl" }, "Operating, depreciation and finance costs.")),
      k("Profit before tax", a.profit, vs(a.profit, b.profit), h("div", { class: "lbl" }, margin(a.profit)))),
    h("h4", { class: "fin-h4" }, "Last twelve months"), c, det);
  const m = f.months, lab = m.map((x) => new Date(x.month).toLocaleDateString("en-AU", { month: "short", year: "2-digit" }));
  setTimeout(() => chart(c, { legend: { right: 10, top: 0, textStyle: { color: "#5f7185" } }, grid: { left: 64, right: 20, top: 34, bottom: 30 },
    tooltip: { trigger: "axis", valueFormatter: (v) => big$(v) },
    xAxis: axis({ type: "category", data: lab }), yAxis: axis({ type: "value", axisLabel: { formatter: (v) => big$(v) } }),
    series: [
      { type: "bar", name: "Revenue", data: m.map((x) => x.revenue), color: "#0c5a66", barGap: "8%" },
      { type: "bar", name: "Operating costs", stack: "c", data: m.map((x) => x.opex), color: "#e2a33c" },
      { type: "bar", name: "Depreciation and finance", stack: "c", data: m.map((x) => x.depreciation + x.finance_cost), color: "#b9c4d0" },
      { type: "line", name: "Profit before tax", data: m.map((x) => x.profit), color: "#2e9c6a", lineStyle: { width: 2.4 }, symbolSize: 6 }] }), 0);
  return el;
}
