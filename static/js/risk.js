/* Risk page: stacked P&L-leg chart.
 *
 * Colours are never hardcoded here and never resolved by dataset index.
 * Each dataset carries its own `seriesKey` ("equity", "fx", ...), and
 * every colour lookup goes through that key to the matching
 * `--series-<key>` custom property on <html>. Swapping the palette
 * swaps only those properties, so the chart follows automatically.
 *
 * backgroundColor is a *scriptable* option — a function Chart.js calls
 * while resolving each element — rather than a fixed string. That is
 * load-bearing: Chart.js caches resolved per-element options, and
 * `chart.update("none")` reuses that cache. Assigning a new string to
 * `dataset.backgroundColor` updated the dataset but left the painted
 * bars on the previous palette's colours. A scriptable option is
 * re-evaluated on every resolve, so the bars can never disagree with
 * the legend.
 */

(function () {
  "use strict";

  var node = document.getElementById("chart-data");
  var canvas = document.getElementById("pnl-chart");
  if (!node || !canvas) return;

  var model = JSON.parse(node.textContent);

  function cssVar(name) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
  }

  /* The single mapping from a series to its colour. Everything that
   * paints — bars, legend swatches — goes through this by name. */
  function seriesColour(key) {
    return cssVar("--series-" + key);
  }

  function theme() {
    return {
      text: cssVar("--text"),
      dim: cssVar("--text-dim"),
      border: cssVar("--border"),
      surface: cssVar("--surface"),
    };
  }

  function paintLegend() {
    model.series.forEach(function (s) {
      var swatch = document.querySelector(
        '#chart-legend i[data-series="' + s.key + '"]'
      );
      if (swatch) swatch.style.background = seriesColour(s.key);
    });
  }

  if (typeof Chart === "undefined") {
    // CDN blocked or offline. The tables above carry the same numbers,
    // so degrade quietly rather than throwing.
    canvas.parentNode.innerHTML =
      '<div class="empty">Chart library unavailable — the figures are in the tables above.</div>';
    return;
  }

  var t = theme();

  var chart = new Chart(canvas, {
    type: "bar",
    data: {
      labels: model.labels,
      datasets: model.series.map(function (s) {
        return {
          label: s.label,
          // Explicit, name-based binding to --series-<key>. Never index.
          seriesKey: s.key,
          data: s.values,
          backgroundColor: function (ctx) {
            var key = (ctx.dataset && ctx.dataset.seriesKey) || s.key;
            return seriesColour(key);
          },
          borderWidth: 0,
          // Carried through so the tooltip can show the server-formatted
          // string rather than re-implementing the accounting format.
          displays: s.displays,
        };
      }),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        x: {
          stacked: true,
          ticks: { color: t.dim, maxRotation: 0, autoSkipPadding: 16 },
          grid: { display: false },
        },
        y: {
          stacked: true,
          ticks: {
            color: t.dim,
            callback: function (value) {
              return value.toLocaleString();
            },
          },
          grid: { color: t.border },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: t.surface,
          borderColor: t.border,
          borderWidth: 1,
          titleColor: t.text,
          bodyColor: t.text,
          callbacks: {
            label: function (ctx) {
              var d = ctx.dataset.displays;
              var shown = d ? d[ctx.dataIndex] : ctx.parsed.y;
              return ctx.dataset.label + ": " + shown;
            },
            footer: function (items) {
              if (!items.length) return "";
              return "Total: " + model.totals[items[0].dataIndex];
            },
          },
        },
      },
    },
  });

  paintLegend();

  document.addEventListener("palettechange", function () {
    var next = theme();

    // Bar colours need no assignment — the scriptable backgroundColor
    // re-reads --series-<seriesKey> when Chart.js resolves elements.
    // Axis and tooltip chrome are plain values, so they do.
    chart.options.scales.x.ticks.color = next.dim;
    chart.options.scales.y.ticks.color = next.dim;
    chart.options.scales.y.grid.color = next.border;

    var tip = chart.options.plugins.tooltip;
    tip.backgroundColor = next.surface;
    tip.borderColor = next.border;
    tip.titleColor = next.text;
    tip.bodyColor = next.text;

    // Default mode, not "none": "none" reuses cached element options and
    // was the other half of the stale-colour bug. `animation: false`
    // already means this does not animate.
    chart.update();
    paintLegend();
  });
})();
