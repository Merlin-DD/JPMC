/* Dashboard runtime: palette toggle + auto-refresh poll.
 *
 * All formatting arrives pre-rendered from the server (the `display`
 * fields), so this file never formats a number itself — that keeps the
 * accounting-parentheses rule in exactly one place, in Python.
 */

(function () {
  "use strict";

  var PALETTE_COOKIE = "palette";
  var PALETTES = ["standard", "cvd"];
  var ONE_YEAR = 60 * 60 * 24 * 365;

  /* ---------------- palette ---------------- */

  function setPalette(name) {
    document.documentElement.setAttribute("data-palette", name);
    // Cookie, not localStorage: the server reads it to render the right
    // palette into the first response, so there's no flash on load.
    document.cookie =
      PALETTE_COOKIE + "=" + name + ";path=/;max-age=" + ONE_YEAR + ";SameSite=Lax";
    var label = document.getElementById("palette-name");
    if (label) label.textContent = name;
    // Anything painting outside CSS — the risk chart — re-reads its
    // colours from the custom properties when this fires.
    document.dispatchEvent(
      new CustomEvent("palettechange", { detail: { palette: name } })
    );
  }

  var toggle = document.getElementById("palette-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-palette");
      var next = PALETTES[(PALETTES.indexOf(current) + 1) % PALETTES.length];
      setPalette(next);
    });
  }

  /* ---------------- auto refresh ---------------- */

  var refreshSeconds = parseInt(document.body.dataset.refreshSeconds, 10);
  if (!refreshSeconds || refreshSeconds < 5) refreshSeconds = 60;

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el && text !== undefined && text !== null) el.textContent = text;
  }

  function signClass(el, sign) {
    el.classList.remove("pos", "neg", "zero");
    if (sign) el.classList.add(sign);
  }

  function renderHeader(data) {
    setText("fetch-hkt", data.fetch.last_successful_fetch_hkt);
    setText("fetch-age", data.fetch.age_seconds_display);

    // Staleness describes the marks, not the poller: a fetch that
    // succeeded seconds ago can still be carrying Friday's closes.
    var staleness = document.getElementById("staleness");
    if (staleness) {
      staleness.setAttribute("data-level", data.marks.level);
      setText("staleness-label", data.marks.level_label);
      setText("staleness-detail", data.marks.detail);
    }

    if (data.asof) {
      setText("asof-compact", data.asof.compact);
    }

    // Commentary is written by the scheduler on its own slower cadence;
    // the poll only ever reflects whatever row is already cached.
    if (data.commentary) {
      setText("commentary-text", data.commentary.text);
      setText("commentary-asof", data.commentary.asof_date);
      setText("commentary-generated", data.commentary.generated_at_hkt);
      setText("commentary-source", data.commentary.source_label);
    }

    var venues = document.getElementById("venues");
    if (venues) {
      data.venues.forEach(function (v) {
        var pill = venues.querySelector('[data-venue="' + v.venue + '"]');
        if (pill) {
          pill.setAttribute("data-status", v.status);
          pill.textContent = v.venue + " " + v.status;
        }
      });
    }

    var recon = document.getElementById("recon");
    if (recon) {
      recon.setAttribute("data-level", data.recon.ok ? "ok" : "warn");
      setText("recon-label", data.recon.label);
      setText("recon-diff", data.recon.display);
    }
  }

  function renderKpis(data) {
    var strip = document.getElementById("kpis");
    if (!strip) return;
    data.kpis.forEach(function (kpi) {
      var card = strip.querySelector('[data-key="' + kpi.key + '"] .value');
      if (!card) return;
      card.textContent = kpi.display;
      var unit = document.createElement("span");
      unit.className = "unit";
      unit.textContent = "USD";
      card.appendChild(unit);
      signClass(card, kpi.sign);
    });
  }

  function cell(value, cls, sign) {
    var td = document.createElement("td");
    td.className = cls + (sign ? " " + sign : "");
    td.textContent = value;
    return td;
  }

  function tag(text, kind, title) {
    var span = document.createElement("span");
    span.className = "tag";
    if (kind) span.setAttribute("data-kind", kind);
    if (title) span.setAttribute("title", title);
    span.textContent = text;
    return span;
  }

  function renderRows(data) {
    var tbody = document.getElementById("rows");
    if (!tbody) return;

    var frag = document.createDocumentFragment();
    data.rows.forEach(function (row) {
      var tr = document.createElement("tr");
      tr.setAttribute("data-stale", row.is_stale ? "true" : "false");

      var tickerCell = document.createElement("td");
      tickerCell.className = "ticker";
      tickerCell.setAttribute("title", row.venue + " · as of " + row.asof_date);
      tickerCell.textContent = row.ticker;
      if (row.is_stale) {
        tickerCell.appendChild(
          tag("STALE", null, "No fresh bar; marked at last traded price")
        );
      }
      if (!row.recon_ok) {
        tickerCell.appendChild(
          tag("BREAK", "break", "Legs do not tie to market value change")
        );
      }
      tr.appendChild(tickerCell);

      tr.appendChild(cell(row.name, "name"));
      tr.appendChild(cell(row.currency, "ccy"));
      tr.appendChild(cell(row.shares_display, "num"));
      row.legs.forEach(function (leg) {
        tr.appendChild(cell(leg.display, "num", leg.sign));
      });
      tr.appendChild(cell(row.total.display, "num", row.total.sign));
      frag.appendChild(tr);
    });

    tbody.replaceChildren(frag);

    var sub = document.getElementById("panel-sub");
    if (sub && data.asof) {
      var text =
        data.totals.positions + " positions · as of " + data.asof.display;
      if (data.totals.stale_rows) {
        text += " · " + data.totals.stale_rows + " marked at last traded price";
      }
      sub.textContent = text;
    }

    var totals = document.getElementById("totals");
    if (totals && data.totals.legs) {
      var tr = document.createElement("tr");
      var label = document.createElement("td");
      label.setAttribute("colspan", "3");
      label.textContent = "Total";
      tr.appendChild(label);
      tr.appendChild(cell("", "num"));
      data.totals.legs.forEach(function (leg) {
        tr.appendChild(cell(leg.display, "num", leg.sign));
      });
      tr.appendChild(cell(data.totals.total.display, "num", data.totals.total.sign));
      totals.replaceChildren(tr);
    }
  }

  function poll() {
    fetch("/api/summary", { headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        renderHeader(data);
        if (!data.empty) {
          renderKpis(data);
          renderRows(data);
        }
      })
      .catch(function (err) {
        // A failed poll leaves the last good render in place; the
        // staleness badge will age into amber/red on its own.
        console.warn("summary poll failed:", err);
      });
  }

  setInterval(poll, refreshSeconds * 1000);
})();
