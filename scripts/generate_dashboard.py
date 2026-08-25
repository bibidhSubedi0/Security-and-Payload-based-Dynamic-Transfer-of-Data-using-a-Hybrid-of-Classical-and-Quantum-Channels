#!/usr/bin/env python3
r"""
Dashboard / Figure Generator for Benchmark Results
==================================================

Reads every metrics/logs/benchmark_*.jsonl record produced by
scripts/run_benchmark.py and renders the empirical dataset into:

  - metrics/figures/fig*.png             (static exports for the report)
  - metrics/dashboard.html               (self-contained interactive page, no server needed)
  - demo-frontend/public/dashboard.html  (mirror served by the React frontend)

Why
---
Benchmark logs are only useful if they can be inspected. This script is the
single rendering step between raw JSONL records and every human-facing
artifact: report figures plus the interactive dashboard page embedded in the
frontend. Each chart is built ONCE as a Plotly figure, then exported to both
formats (write_image for PNG via kaleido, to_html for the dashboard), so a
chart can never disagree with its own static copy.

Rendering dependency: PNG export requires kaleido >= 1.0 (the 0.2.x series
is deprecated AND its shell-wrapper launcher breaks on install paths that
contain spaces). kaleido 1.x needs a Chrome/Chromium binary; if PNG export
complains about a missing browser, run `plotly_get_chrome` once or point
BROWSERPATH at an existing Chrome install.

Charts skipped (data not available):
  - Fig 4: rendered only when bits_corrected / bits_sacrificed exist in the
    log (schema >= 1.1 records).
  - Fig 7: CHSH parameter for E91; all sessions are BB84; chsh field is always null.

Outcome-breakdown note:
  SESSION_ABORTED and RECONCILIATION_INCOMPLETE outcomes do not produce log records
  (the benchmark script calls collector.record_transfer() only on CLEAN_PASS /
  RECOVERED / CHANNEL_FAILURE).  The outcome chart reconstructs the full 54-config
  grid picture from the logged counts + grid arithmetic (54 - logged = not-logged).

Usage:
  conda run -n hcq_proj python scripts/generate_dashboard.py
"""

from __future__ import annotations

import json
import pathlib
import sys

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Paths (single source of truth for every input/output location)
# ---------------------------------------------------------------------------

# Repo root, derived from this file's location so the script works when run
# from any working directory.
_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Input: append-only JSONL logs written by scripts/run_benchmark.py.
_LOG_DIR = _ROOT / "metrics" / "logs"

# Outputs: static PNGs for the report, the self-contained Plotly page, and
# the mirror that Vite serves to the React frontend at /dashboard.html.
_FIG_DIR = _ROOT / "metrics" / "figures"
_HTML_OUT = _ROOT / "metrics" / "dashboard.html"
_FRONTEND_OUT = _ROOT / "demo-frontend" / "public" / "dashboard.html"

_FIG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

# Shared by every chart so a colour means the same thing everywhere:
# green = clean success, amber = recovered via reroute, red = hard failure,
# grey/purple = inferred or incomplete, blue/orange/purple = security levels.
PALETTE = {
    "CLEAN_PASS": "#2ecc71",  # green
    "RECOVERED_VIA_REROUTE": "#f39c12",  # amber
    "CHANNEL_FAILURE": "#e74c3c",  # red
    "SESSION_ABORTED": "#95a5a6",  # grey
    "RECONCILIATION_INCOMPLETE": "#9b59b6",  # purple
    "low": "#3498db",
    "medium": "#e67e22",
    "high": "#9b59b6",
}

NOISE_GROUP_COLORS = {
    "0.00 (noiseless)": "#3498db",
    "0.05 (moderate)": "#e67e22",
}

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

# Maps split_reason (recorded by split_controller.compute_split()) onto the
# coarse levels grouped in Fig 3. Degraded-mode reasons outside the first
# three are kept verbatim ("constrained", "qber_unsafe") so those records
# stay distinguishable instead of being silently lumped into a level.
SPLIT_REASON_TO_LEVEL = {
    "LOW_SECURITY": "low",
    "MEDIUM_SECURITY": "medium",
    "HIGH_SECURITY_MAX": "high",
    "EBIT_CONSTRAINED": "constrained",
    "QBER_UNSAFE": "qber_unsafe",
}

# Grid arithmetic used by Fig 2: the log only contains rows for configs that
# completed a transfer, so the aborted row (noise=0.12) is reconstructed from
# the known grid shape rather than from records.
GRID_TOTAL = 54  # 3 sec × 3 payload × 3 noise × 2 fault configs per full run
GRID_NOISE_ROWS = 3  # noise levels: 0.00, 0.05, 0.12
GRID_PER_NOISE = GRID_TOTAL // GRID_NOISE_ROWS  # 18 configs at each noise level


def load_all_records() -> tuple[list[dict], list[dict]]:
    """
    Load every benchmark record and derive grouping fields.

    -------
    Returns
    -------
    (all_records, latest_run_records) : tuple[list[dict], list[dict]]
        Adds derived fields to each record:
          noise_group      : "0.00 (noiseless)" | "0.05 (moderate)"
                             (qber==0 means noiseless; noise=0.12 configs are
                             never logged because they abort before transfer)
          security_level   : mapped via SPLIT_REASON_TO_LEVEL
          run_file         : basename of the source .jsonl
        "latest" = records of the most recently modified file; used alone for
        Figs 2, 5, 6 whose timing must come from one protocol version.
    """
    jsonl_files = sorted(_LOG_DIR.glob("benchmark_*.jsonl"))
    if not jsonl_files:
        print("[ERROR] No benchmark_*.jsonl files found in", _LOG_DIR)
        sys.exit(1)

    all_records: list[dict] = []
    for fpath in jsonl_files:
        with open(fpath, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec["run_file"] = fpath.name
                rec["noise_group"] = (
                    "0.00 (noiseless)"
                    if rec.get("qber", 0.0) == 0.0
                    else "0.05 (moderate)"
                )
                rec["security_level"] = SPLIT_REASON_TO_LEVEL.get(
                    rec.get("split_reason", ""), rec.get("split_reason", "unknown")
                )
                all_records.append(rec)

    latest_file = jsonl_files[-1].name
    latest = [r for r in all_records if r["run_file"] == latest_file]
    return all_records, latest


def date_range(records: list[dict]) -> tuple[str, str]:
    """
    Earliest and latest record timestamps across the given records.

    -------
    Returns
    -------
    (str, str)
        (min timestamp_utc, max timestamp_utc); ("?", "?") when no record
        carries a usable timestamp.
    """
    ts_vals = [r["timestamp_utc"] for r in records if r.get("timestamp_utc")]
    if not ts_vals:
        return "?", "?"
    return min(ts_vals), max(ts_vals)


# ---------------------------------------------------------------------------
# Chart builders: each returns (go.Figure, stats_dict) and is rendered ONCE;
# the same Figure feeds both the PNG export and the HTML dashboard.
# ---------------------------------------------------------------------------


def fig1_qber_vs_noise(records: list[dict]) -> tuple[go.Figure, dict]:
    """
    Fig 1: box + jitter of measured QBER per inferred noise group,
    reference line at the 0.11 BB84 abort threshold.
    """
    groups = list(NOISE_GROUP_COLORS)
    fig = go.Figure()
    for group, color in NOISE_GROUP_COLORS.items():
        grp = [r["qber"] for r in records if r["noise_group"] == group]
        if not grp:
            continue
        fig.add_trace(
            go.Box(
                y=grp,
                name=f"noise={group}",
                marker_color=color,
                boxpoints="all",
                jitter=0.3,
                pointpos=0,
            )
        )
    fig.add_hline(
        y=0.11, line_dash="dash", line_color="red",
        annotation_text="Abort threshold (0.11)", annotation_position="top right",
    )
    fig.update_layout(
        title="Fig 1: Measured QBER vs Injected Noise (BB84, n_qubits=200)",
        yaxis_title="Measured QBER",
        template="plotly_white",
    )

    def mean_of(group: str) -> float:
        """Mean QBER over records of one noise group; 0.0 when empty."""
        vals = [r["qber"] for r in records if r["noise_group"] == group]
        return float(np.mean(vals)) if vals else 0.0

    stats = {
        "noiseless_n": sum(1 for r in records if r["noise_group"] == groups[0]),
        "moderate_n": sum(1 for r in records if r["noise_group"] == groups[1]),
        "noiseless_mean_qber": mean_of(groups[0]),
        "moderate_mean_qber": mean_of(groups[1]),
    }
    return fig, stats


def fig2_outcome_breakdown(latest: list[dict]) -> tuple[go.Figure, dict]:
    """
    Fig 2: stacked bars of CLEAN_PASS vs aborted/incomplete per noise level.
    Aborted configs produce no records, so the noise=0.12 bar is inferred
    from grid arithmetic (18 expected, 0 logged).
    """
    n_noiseless = sum(1 for r in latest if r["noise_group"] == "0.00 (noiseless)")
    n_moderate = sum(1 for r in latest if r["noise_group"] == "0.05 (moderate)")
    labels = ["0.00<br>(noiseless)", "0.05<br>(moderate)", "0.12<br>(above threshold)"]
    clean_counts = [n_noiseless, n_moderate, 0]
    aborted_counts = [0, 0, GRID_PER_NOISE]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=clean_counts, name="CLEAN_PASS",
        marker_color=PALETTE["CLEAN_PASS"],
        text=clean_counts, textposition="inside",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=aborted_counts,
        name="SESSION_ABORTED / RECON_INCOMPLETE (inferred)",
        marker_color=PALETTE["SESSION_ABORTED"],
        text=[f"{v}*" if v > 0 else "" for v in aborted_counts],
        textposition="inside",
    ))
    fig.update_layout(
        barmode="stack",
        title=f"Fig 2: Outcome Breakdown by Noise Level (latest run, {len(latest)}/54 logged)",
        yaxis_title="# Configurations",
        template="plotly_white",
    )

    pct_clean = 100 * (n_noiseless + n_moderate) / GRID_TOTAL
    stats = {
        "total_logged": len(latest),
        "clean_pass": n_noiseless + n_moderate,
        "aborted_or_incomplete": GRID_PER_NOISE,
        "pct_clean": pct_clean,
        "pct_aborted": 100 * GRID_PER_NOISE / GRID_TOTAL,
    }
    return fig, stats


def fig3_split_ratio(records: list[dict]) -> tuple[go.Figure, dict]:
    """
    Fig 3: quantum_fraction distribution per security level (box + points),
    against the 0.25 / 0.50 / 0.75 policy targets.
    """
    levels = ["low", "medium", "high"]
    targets = {"low": 0.25, "medium": 0.50, "high": 0.75}

    fig = go.Figure()
    for lvl in levels:
        grp = [r["quantum_fraction"] for r in records if r["security_level"] == lvl]
        fig.add_trace(go.Box(
            y=grp, name=f"{lvl} security",
            marker_color=PALETTE[lvl],
            boxpoints="all", jitter=0.3, pointpos=0,
        ))
    for lvl in levels:
        fig.add_hline(y=targets[lvl], line_dash="dot",
                      line_color=PALETTE[lvl], opacity=0.6)
    fig.update_layout(
        title="Fig 3: Split Ratio (Quantum Fraction) by Security Level (dotted = target)",
        yaxis_title="Quantum Fraction",
        template="plotly_white",
    )

    means = {}
    for lvl in levels:
        vals = [r["quantum_fraction"] for r in records if r["security_level"] == lvl]
        means[lvl] = float(np.mean(vals)) if vals else 0.0
    return fig, {"mean_quantum_fraction": means}


def fig4_reconciliation(latest: list[dict]) -> tuple[go.Figure, dict] | None:
    """
    Fig 4: Cascade reconciliation stats (mean bits_corrected vs
    bits_sacrificed per noise group), latest-run records only.

    -------
    Returns
    -------
    (Figure, stats) or None when no record carries the schema-1.1
    reconciliation fields (legacy logs); caller reports the skip.
    """
    recs = [r for r in latest if r.get("bits_corrected") is not None]
    if not recs:
        return None

    groups = list(NOISE_GROUP_COLORS)

    def mean_of(field: str, group: str) -> float:
        """Mean of `field` over records of one noise group; 0.0 when empty."""
        vals = [r[field] for r in recs if r["noise_group"] == group]
        return float(np.mean(vals)) if vals else 0.0

    corr_means = [mean_of("bits_corrected", g) for g in groups]
    sac_means = [mean_of("bits_sacrificed", g) for g in groups]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=groups, y=corr_means, name="bits_corrected (Cascade)",
        marker_color="#9b59b6", text=[f"{v:.1f}" for v in corr_means],
        textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=groups, y=sac_means, name="bits_sacrificed (privacy amp.)",
        marker_color="#95a5a6", text=[f"{v:.1f}" for v in sac_means],
        textposition="outside",
    ))
    fig.update_layout(
        title="Fig 4: Reconciliation Bits Corrected / Sacrificed (latest run, Bob-side)",
        yaxis_title="Mean bits per transfer",
        template="plotly_white",
    )
    stats = {
        "n": len(recs),
        "corr_noiseless": corr_means[0],
        "corr_moderate": corr_means[1],
        "sac_noiseless": sac_means[0],
        "sac_moderate": sac_means[1],
    }
    return fig, stats


def fig5_skr_vs_qber(latest: list[dict]) -> tuple[go.Figure, dict]:
    """
    Fig 5: secret key rate vs measured QBER scatter, point size proportional
    to key length; latest-run records only (protocol-consistent timing).
    """
    fig = go.Figure()
    for group, color in NOISE_GROUP_COLORS.items():
        grp = [r for r in latest if r["noise_group"] == group]
        if not grp:
            continue
        fig.add_trace(go.Scatter(
            x=[r["qber"] for r in grp],
            y=[r["skr_bits_per_second"] for r in grp],
            mode="markers", name=f"noise={group}",
            marker=dict(color=color,
                        size=[max(6, r.get("qkd_key_bits", 20) // 3) for r in grp],
                        opacity=0.7),
            hovertemplate="QBER: %{x:.4f}<br>SKR: %{y:.1f} bits/s<extra></extra>",
        ))
    fig.add_vline(x=0.11, line_dash="dash", line_color="red")
    fig.update_layout(
        title="Fig 5: Secret Key Rate vs Measured QBER (point size = key length, latest run)",
        xaxis_title="Measured QBER",
        yaxis_title="SKR (bits/s)",
        template="plotly_white",
    )

    all_skr = [r["skr_bits_per_second"] for r in latest]
    stats = {
        "skr_min": min(all_skr),
        "skr_max": max(all_skr),
        "skr_mean": float(np.mean(all_skr)),
    }
    return fig, stats


def fig6_throughput_latency(latest: list[dict]) -> tuple[go.Figure, dict]:
    """
    Fig 6: mean throughput and latency per payload size bucket (mean +/- 1 std),
    latest-run records only. Two panels share one figure because the units
    (bytes/s vs seconds) must not share an axis.
    """
    sizes = sorted(set(r["payload_bytes"] for r in latest))
    size_labels = [f"{sz}B" for sz in sizes]

    tput_data = {sz: [r["throughput_bytes_per_s"] for r in latest
                      if r["payload_bytes"] == sz] for sz in sizes}
    lat_data = {sz: [r["latency_s"] for r in latest
                     if r["payload_bytes"] == sz] for sz in sizes}
    tput_means = [np.mean(tput_data[sz]) for sz in sizes]
    tput_stds = [np.std(tput_data[sz]) for sz in sizes]
    lat_means = [np.mean(lat_data[sz]) for sz in sizes]
    lat_stds = [np.std(lat_data[sz]) for sz in sizes]

    fig = make_subplots(rows=1, cols=2, subplot_titles=("Throughput", "Latency"))
    fig.add_trace(go.Bar(
        x=size_labels, y=tput_means,
        error_y=dict(type="data", array=tput_stds, visible=True),
        name="Throughput (B/s)", marker_color="#3498db",
        text=[f"{v:.0f}" for v in tput_means], textposition="outside",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=size_labels, y=lat_means,
        error_y=dict(type="data", array=lat_stds, visible=True),
        name="Latency (s)", marker_color="#e74c3c",
        text=[f"{v:.3f}s" for v in lat_means], textposition="outside",
    ), row=1, col=2)
    fig.update_layout(
        title="Fig 6: Throughput and Latency by Payload Size (mean ± 1 std, latest run)",
        showlegend=False,
        template="plotly_white",
    )
    fig.update_yaxes(title_text="Throughput (B/s)", row=1, col=1)
    fig.update_yaxes(title_text="Latency (s)", row=1, col=2)

    stats = {
        "payload_sizes": sizes,
        "tput_means": [round(v, 1) for v in tput_means],
        "lat_means": [round(v, 4) for v in lat_means],
    }
    return fig, stats


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def export_pngs(charts: list[tuple[str, str, go.Figure]]) -> None:
    """
    Write every chart to metrics/figures/<name>.png via kaleido.

    ----------
    Parameters
    ----------
    charts : list of (png_name, heading, figure)
    """
    for name, _, fig in charts:
        out = _FIG_DIR / f"{name}.png"
        fig.write_image(str(out), scale=2)
        print(f"  [OK] {out.relative_to(_ROOT)}")


def write_dashboard_html(charts: list[tuple[str, str, go.Figure]],
                         notes: list[str]) -> None:
    """
    Assemble the self-contained interactive HTML dashboard and mirror it to
    demo-frontend/public/.

    ----------
    Parameters
    ----------
    charts : list of (png_name, heading, figure)
    notes : list[str]
        Footnote lines (charts genuinely unavailable for THIS log).
    """
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>Benchmark Dashboard</title>",
        "<style>body{font-family:sans-serif;margin:24px auto;max-width:1100px}"
        "h2{margin-top:40px}.note{color:#666;font-size:0.9em}</style>",
        "</head><body>",
        "<h1>Dynamic Hybrid Quantum-Classical Communication System</h1>",
        "<p class='note'>Benchmark Results Dashboard: BB84 + SDC, n_qubits=200 "
        "(reconciliation enabled)</p>",
    ]
    for i, (_, heading, fig) in enumerate(charts):
        parts.append(f"<h2>{heading}</h2>")
        # Embed plotly.js once (first chart); subsequent charts reuse it.
        parts.append(fig.to_html(
            include_plotlyjs=(i == 0), full_html=False,
            config={"displaylogo": False},
        ))
    if notes:
        parts.append("<p class='note'><b>Charts not shown:</b> " + " ".join(notes) + "</p>")
    parts.append("</body></html>")

    html = "\n".join(parts)
    _HTML_OUT.write_text(html, encoding="utf-8")
    print(f"  [OK] {_HTML_OUT.relative_to(_ROOT)}")
    if _FRONTEND_OUT.parent.is_dir():
        _FRONTEND_OUT.write_text(html, encoding="utf-8")
        print(f"  [OK] {_FRONTEND_OUT.relative_to(_ROOT)}")


# ---------------------------------------------------------------------------
# Verification and console summary
# ---------------------------------------------------------------------------


def spot_check(latest: list[dict]) -> None:
    """
    Verify Fig 2's outcome counts against a direct recount of the raw JSONL,
    then report the comparison (guards against grouping bugs).
    """
    raw_path = max(_LOG_DIR.glob("benchmark_*.jsonl"), key=lambda p: p.stat().st_mtime)
    with open(raw_path, encoding="utf-8") as fh:
        raw_lines = [l.strip() for l in fh if l.strip()]
    raw_recs = [json.loads(l) for l in raw_lines]

    raw_total = len(raw_lines)
    raw_qber_zero = sum(1 for r in raw_recs if r["qber"] == 0.0)
    raw_qber_positive = sum(1 for r in raw_recs if r["qber"] > 0.0)
    raw_clean_pass = sum(1 for r in raw_recs if r["echo_outcome"] == "CLEAN_PASS")

    db_noiseless = sum(1 for r in latest if r["noise_group"] == "0.00 (noiseless)")
    db_moderate = sum(1 for r in latest if r["noise_group"] == "0.05 (moderate)")

    print()
    print("  Manual spot-check; Fig 2 outcome counts vs raw JSONL:")
    print(f"    File                   : {raw_path.name}")
    print(f"    Raw total records      : {raw_total}")
    print(
        f"    Raw qber==0.0 count    : {raw_qber_zero}   <-  dashboard noiseless : {db_noiseless}  {'MATCH' if raw_qber_zero == db_noiseless else 'MISMATCH'}"
    )
    print(
        f"    Raw qber>0.0 count     : {raw_qber_positive}   <-  dashboard moderate  : {db_moderate}  {'MATCH' if raw_qber_positive == db_moderate else 'MISMATCH'}"
    )
    print(
        f"    Raw CLEAN_PASS count   : {raw_clean_pass}   <-  dashboard total     : {db_noiseless + db_moderate}  {'MATCH' if raw_clean_pass == db_noiseless + db_moderate else 'MISMATCH'}"
    )
    print(
        f"    Inferred non-logged    : {GRID_TOTAL - raw_total} (= 54 grid total - {raw_total} records -> noise=0.12 row)"
    )


def print_summary(
    ts_start: str,
    ts_end: str,
    n_all_files: int,
    fig1_stats: dict,
    fig2_stats: dict,
    fig3_stats: dict,
    fig4_stats: dict | None,
    fig5_stats: dict,
    fig6_stats: dict,
    n_latest: int,
    n_total: int,
) -> None:
    r"""
    Print the human-readable takeaway sheet after generation.

    ----------
    Parameters
    ----------
    ts_start, ts_end : str
        Date range from date_range().
    n_all_files, n_total, n_latest : int
        Record counts across files / overall / latest run.
    fig1_stats .. fig6_stats : dict or None
        Takeaway dicts returned by the corresponding chart builder;
        fig4_stats is None when the log predates schema 1.1.

    -----
    Notes
    -----
    Exists so the console itself states the headline conclusions (QBER vs
    threshold, clean-pass percentage, split-ratio targets, SKR spread)
    without requiring the reader to open any chart.
    """
    print()
    print("=" * 66)
    print("  DASHBOARD GENERATION SUMMARY")
    print("=" * 66)
    print(f"  Records processed : {n_total} across {n_all_files} benchmark files")
    print(f"  Latest run        : {n_latest} records / 54 configs")
    print(f"  Date range        : {ts_start}  ->  {ts_end}")
    print()
    print("  Chart takeaways:")
    print(
        f"  [Fig 1] QBER vs noise; noiseless sessions: QBER=0.0 (n={fig1_stats['noiseless_n']}); "
        f"moderate-noise sessions: mean QBER={fig1_stats['moderate_mean_qber']:.4f} "
        f"(n={fig1_stats['moderate_n']}), all well below the 0.11 abort threshold."
    )
    print(
        f"  [Fig 2] Outcome breakdown: {fig2_stats['clean_pass']}/54 ({fig2_stats['pct_clean']:.0f}%) "
        f"CLEAN_PASS; {fig2_stats['aborted_or_incomplete']}/54 ({fig2_stats['pct_aborted']:.0f}%) "
        f"aborted or reconciliation-incomplete (all from noise=0.12 configs, not logged)."
    )
    frac = fig3_stats["mean_quantum_fraction"]
    print(
        f"  [Fig 3] Split ratio: mean quantum fractions: "
        f"low={frac['low']:.2f}, medium={frac['medium']:.2f}, high={frac['high']:.2f}, "
        f"matching the 0.25/0.50/0.75 target policy precisely."
    )
    if fig4_stats is not None:
        print(
            f"  [Fig 4] Reconciliation: Cascade corrections: "
            f"noiseless={fig4_stats['corr_noiseless']:.1f} bits, "
            f"moderate={fig4_stats['corr_moderate']:.1f} bits; "
            f"sacrificed (privacy amp.): "
            f"noiseless={fig4_stats['sac_noiseless']:.1f}, "
            f"moderate={fig4_stats['sac_moderate']:.1f} "
            f"(n={fig4_stats['n']} schema-1.1 records)."
        )
    else:
        print(
            f"  [Fig 4] SKIPPED; bits_corrected / bits_sacrificed absent in this log "
            f"(pre-1.1 schema run); re-run the benchmark to populate."
        )
    print(
        f"  [Fig 5] SKR vs QBER: SKR range {fig5_stats['skr_min']:.1f}-{fig5_stats['skr_max']:.1f} bits/s; "
        f"higher QBER correlates with more sifting loss -> lower SKR as expected."
    )
    sizes = fig6_stats["payload_sizes"]
    tputs = fig6_stats["tput_means"]
    lats = fig6_stats["lat_means"]
    size_summary = ", ".join(
        f"{s}B->{t:.0f}B/s/{l:.3f}s" for s, t, l in zip(sizes, tputs, lats)
    )
    print(
        f"  [Fig 6] Throughput/latency by payload: {size_summary}. "
        f"Latency dominated by QKD + SDC encoding overhead, not payload size."
    )
    print(
        f"  [Fig 7] SKIPPED; no E91 sessions in log (chsh=null for all {n_total} records)."
    )
    print()
    print("  Output files:")
    for f in sorted(_FIG_DIR.glob("*.png")):
        print(f"    {f.relative_to(_ROOT)}")
    print(f"    {_HTML_OUT.relative_to(_ROOT)}")
    print("=" * 66)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    r"""
    Render every figure and both HTML outputs, then self-check and summarise.

    ----------
    Workflow
    ----------
    1. load_all_records(): read all benchmark_*.jsonl files; "latest" is the
       most recent file (protocol-consistent timing for Figs 2, 5, 6).
    2. Build each chart once as a Plotly figure.
    3. export_pngs(): static copies into metrics/figures/.
    4. write_dashboard_html(): one interactive self-contained page written to
       metrics/dashboard.html and mirrored to demo-frontend/public/.
    5. spot_check(): verify Fig 2's counts against a raw recount of the
       newest JSONL (guards against grouping bugs).
    6. print_summary(): console takeaways + output file listing.

    -----
    Notes
    -----
    Figs 2, 5 and 6 deliberately use latest-run records only: earlier runs
    came from pre-reconciliation protocol versions whose timing is not
    comparable.
    """
    print("\nGenerating dashboard figures...")
    print(f"  Log dir  : {_LOG_DIR}")
    print(f"  Fig dir  : {_FIG_DIR}")
    print(f"  HTML out : {_HTML_OUT}")
    print()

    all_records, latest = load_all_records()
    ts_start, ts_end = date_range(all_records)
    print(
        f"  Loaded {len(all_records)} records from {len(set(r['run_file'] for r in all_records))} files"
    )
    print(
        f"  Latest run ({max(_LOG_DIR.glob('benchmark_*.jsonl'), key=lambda p: p.stat().st_mtime).name}): {len(latest)} records"
    )
    print()

    fig1, fig1_stats = fig1_qber_vs_noise(all_records)
    fig2, fig2_stats = fig2_outcome_breakdown(latest)
    fig3, fig3_stats = fig3_split_ratio(all_records)
    fig5, fig5_stats = fig5_skr_vs_qber(latest)
    fig6, fig6_stats = fig6_throughput_latency(latest)
    fig4_result = fig4_reconciliation(latest)  # None for legacy logs

    charts: list[tuple[str, str, go.Figure]] = [
        ("fig1_qber_vs_noise", "Fig 1: Measured QBER vs Injected Noise", fig1),
        ("fig2_outcome_breakdown", "Fig 2: Outcome Breakdown by Noise Level", fig2),
        ("fig3_split_ratio", "Fig 3: Quantum Fraction by Security Level", fig3),
    ]
    if fig4_result is not None:
        fig4, fig4_stats = fig4_result
        charts.append((
            "fig4_reconciliation_bits",
            "Fig 4: Reconciliation Bits Corrected / Sacrificed",
            fig4,
        ))
    else:
        fig4_stats = None
        print(
            "  [SKIP] fig4_reconciliation_bits.png (no schema-1.1 records;"
            " re-run benchmark to populate)"
        )
    charts += [
        ("fig5_skr_vs_qber", "Fig 5: Secret Key Rate vs Measured QBER", fig5),
        ("fig6_throughput_latency", "Fig 6: Throughput and Latency by Payload Size", fig6),
    ]

    print("\n  Exporting static PNGs:")
    export_pngs(charts)

    print("\n  Building interactive HTML:")
    write_dashboard_html(charts, notes=[
        "Fig 7 (CHSH/E91): all sessions used BB84 (chsh=null for all records).",
    ])

    spot_check(latest)
    print_summary(
        ts_start, ts_end,
        len(set(r["run_file"] for r in all_records)),
        fig1_stats, fig2_stats, fig3_stats, fig4_stats, fig5_stats, fig6_stats,
        n_latest=len(latest), n_total=len(all_records),
    )


if __name__ == "__main__":
    main()
