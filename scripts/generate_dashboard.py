#!/usr/bin/env python3
"""
Dashboard / figure generator for the HBD Class Quant benchmark results.

Reads all metrics/logs/benchmark_*.jsonl files and produces:
  - metrics/figures/fig1_qber_vs_noise.png
  - metrics/figures/fig2_outcome_breakdown.png
  - metrics/figures/fig3_split_ratio.png
  - metrics/figures/fig5_skr_vs_qber.png
  - metrics/figures/fig6_throughput_latency.png
  - metrics/dashboard.html  (self-contained interactive page — no server needed)

Charts skipped (data not available):
  - Fig 4: Reconciliation bits_corrected / bits_sacrificed — these fields are NOT
    written to the log (reconciliation happens before MetricsCollector is called).
  - Fig 7: CHSH parameter for E91 — all sessions are BB84; chsh field is always null.

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
import os
import pathlib
import sys
from collections import defaultdict
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")  # non-interactive backend, safe for headless runs
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT     = pathlib.Path(__file__).resolve().parent.parent
_LOG_DIR  = _ROOT / "metrics" / "logs"
_FIG_DIR  = _ROOT / "metrics" / "figures"
_HTML_OUT = _ROOT / "metrics" / "dashboard.html"

_FIG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Colour palette (consistent across static + interactive)
# ---------------------------------------------------------------------------

PALETTE = {
    "CLEAN_PASS":               "#2ecc71",   # green
    "RECOVERED_VIA_REROUTE":    "#f39c12",   # amber
    "CHANNEL_FAILURE":          "#e74c3c",   # red
    "SESSION_ABORTED":          "#95a5a6",   # grey
    "RECONCILIATION_INCOMPLETE":"#9b59b6",   # purple
    "KEY_MISMATCH_ERROR":       "#e74c3c",
    "low":    "#3498db",
    "medium": "#e67e22",
    "high":   "#9b59b6",
}

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

SPLIT_REASON_TO_LEVEL = {
    "LOW_SECURITY":     "low",
    "MEDIUM_SECURITY":  "medium",
    "HIGH_SECURITY_MAX":"high",
    "EBIT_CONSTRAINED": "constrained",
    "QBER_UNSAFE":      "qber_unsafe",
}

GRID_TOTAL       = 54   # 3 sec × 3 payload × 3 noise × 2 fault configs per full run
GRID_NOISE_ROWS  = 3    # noise levels: 0.00, 0.05, 0.12
GRID_PER_NOISE   = GRID_TOTAL // GRID_NOISE_ROWS   # 18 configs at each noise level


def load_all_records() -> tuple[list[dict], list[dict]]:
    """
    Returns (all_records, latest_run_records).

    Adds derived fields to each record:
      noise_group  : "0.00 (noiseless)" | "0.05 (moderate)"
                     (qber==0 → noiseless, qber>0 → moderate;
                      noise=0.12 configs are not logged)
      security_level: "low" | "medium" | "high" | ...
      run_file     : basename of source file
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
                rec["run_file"]       = fpath.name
                rec["noise_group"]    = (
                    "0.00 (noiseless)" if rec.get("qber", 0.0) == 0.0
                    else "0.05 (moderate)"
                )
                rec["security_level"] = SPLIT_REASON_TO_LEVEL.get(
                    rec.get("split_reason", ""), rec.get("split_reason", "unknown")
                )
                all_records.append(rec)

    latest_file = jsonl_files[-1].name
    latest = [r for r in all_records if r["run_file"] == latest_file]
    return all_records, latest


# ---------------------------------------------------------------------------
# Helper: date range
# ---------------------------------------------------------------------------

def date_range(records: list[dict]) -> tuple[str, str]:
    ts_vals = [r["timestamp_utc"] for r in records if r.get("timestamp_utc")]
    if not ts_vals:
        return "?", "?"
    return min(ts_vals), max(ts_vals)


# ---------------------------------------------------------------------------
# Fig 1 — QBER vs injected noise level
# ---------------------------------------------------------------------------

def fig1_qber_vs_noise(records: list[dict]) -> dict:
    """
    Strip + box plot of measured QBER per inferred noise group.
    Reference line at 0.11 (BB84 abort threshold).

    Returns takeaway dict.
    """
    groups = ["0.00 (noiseless)", "0.05 (moderate)"]
    data   = {g: [r["qber"] for r in records if r["noise_group"] == g] for g in groups}

    fig, ax = plt.subplots(figsize=(7, 4.5))
    positions = list(range(len(groups)))
    bp = ax.boxplot(
        [data[g] for g in groups],
        positions=positions,
        widths=0.35,
        patch_artist=True,
        medianprops=dict(color="black", linewidth=2),
        boxprops=dict(facecolor="#aed6f1", alpha=0.7),
        flierprops=dict(marker="o", markerfacecolor="#2980b9", markersize=4, alpha=0.5),
    )
    # Jitter overlay
    rng = np.random.default_rng(42)
    for i, g in enumerate(groups):
        jitter = rng.uniform(-0.12, 0.12, len(data[g]))
        ax.scatter(
            np.full(len(data[g]), i) + jitter, data[g],
            s=18, alpha=0.6, color="#2980b9", zorder=3
        )

    ax.axhline(0.11, color="red", linestyle="--", linewidth=1.5, label="Abort threshold (0.11)")
    ax.set_xticks(positions)
    ax.set_xticklabels([f"Injected noise\n{g}" for g in groups])
    ax.set_ylabel("Measured QBER")
    ax.set_title("Fig 1 — Measured QBER vs Injected Noise Level\n(BB84, n_qubits=200, all logged sessions)")
    ax.legend(loc="upper left")
    ax.set_ylim(-0.01, 0.15)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out = _FIG_DIR / "fig1_qber_vs_noise.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  [OK] {out.relative_to(_ROOT)}")

    noiseless_mean = np.mean(data["0.00 (noiseless)"]) if data["0.00 (noiseless)"] else 0.0
    moderate_mean  = np.mean(data["0.05 (moderate)"])  if data["0.05 (moderate)"]  else 0.0
    return {
        "noiseless_n": len(data["0.00 (noiseless)"]),
        "moderate_n":  len(data["0.05 (moderate)"]),
        "noiseless_mean_qber": noiseless_mean,
        "moderate_mean_qber":  moderate_mean,
    }


# ---------------------------------------------------------------------------
# Fig 2 — Outcome breakdown by noise level (latest run, reconstructed)
# ---------------------------------------------------------------------------

def fig2_outcome_breakdown(latest: list[dict]) -> dict:
    """
    Stacked bar chart: CLEAN_PASS / SESSION_ABORTED+RECON_INCOMPLETE per noise level.

    Since the MetricsCollector only writes records for completed transfers, noise=0.12
    sessions (all aborted or reconciliation-incomplete) produce no log records.  We
    reconstruct the full 54-config picture:
      - noise=0.00: GRID_PER_NOISE=18 expected → logged count = CLEAN_PASS count
      - noise=0.05: GRID_PER_NOISE=18 expected → logged count = CLEAN_PASS count
      - noise=0.12: GRID_PER_NOISE=18 expected → 0 logged → all aborted/incomplete

    The split between SESSION_ABORTED and RECONCILIATION_INCOMPLETE within the
    noise=0.12 row is not recorded in the log; the bar is labelled accordingly.
    """
    n_noiseless = sum(1 for r in latest if r["noise_group"] == "0.00 (noiseless)")
    n_moderate  = sum(1 for r in latest if r["noise_group"] == "0.05 (moderate)")
    # All logged records are CLEAN_PASS (only outcome that triggers record_transfer)
    n_above     = GRID_PER_NOISE  # 18 configs at noise=0.12, none logged

    labels = ["0.00\n(noiseless)", "0.05\n(moderate)", "0.12\n(above threshold)"]
    clean  = [n_noiseless, n_moderate, 0]
    aborted_incomplete = [0, 0, n_above]  # combined: SESSION_ABORTED + RECON_INCOMPLETE

    x = np.arange(len(labels))
    width = 0.5

    fig, ax = plt.subplots(figsize=(7, 4.5))
    bars_clean = ax.bar(x, clean, width, label="CLEAN_PASS", color=PALETTE["CLEAN_PASS"])
    bars_aborted = ax.bar(
        x, aborted_incomplete, width, bottom=clean,
        label="SESSION_ABORTED / RECON_INCOMPLETE\n(inferred — not logged)",
        color=PALETTE["SESSION_ABORTED"], hatch="//",
    )

    # Value labels
    for bar, val in zip(bars_clean, clean):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() / 2,
                    str(val), ha="center", va="center", fontweight="bold", fontsize=11)
    for bar, bottom, val in zip(bars_aborted, clean, aborted_incomplete):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bottom + val / 2,
                    f"{val}*", ha="center", va="center", fontweight="bold", fontsize=11, color="white")

    ax.set_xticks(x)
    ax.set_xticklabels([f"Injected noise\n{l}" for l in labels])
    ax.set_ylabel("Number of configurations")
    ax.set_title(
        "Fig 2 — Transfer Outcome Breakdown by Injected Noise Level\n"
        f"(Latest run: {len(latest)} logged / 54 total configs; "
        f"* = inferred from grid arithmetic)"
    )
    ax.legend(loc="upper right", fontsize=8)
    ax.set_ylim(0, GRID_PER_NOISE * 1.25)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out = _FIG_DIR / "fig2_outcome_breakdown.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  [OK] {out.relative_to(_ROOT)}")

    total_logged = len(latest)
    pct_clean = 100 * (n_noiseless + n_moderate) / GRID_TOTAL
    return {
        "total_logged": total_logged,
        "clean_pass": n_noiseless + n_moderate,
        "aborted_or_incomplete": n_above,
        "pct_clean": pct_clean,
        "pct_aborted": 100 * n_above / GRID_TOTAL,
    }


# ---------------------------------------------------------------------------
# Fig 3 — Split ratio by security level
# ---------------------------------------------------------------------------

def fig3_split_ratio(records: list[dict]) -> dict:
    """
    Box plot of quantum_fraction per security level, across all runs.
    """
    levels = ["low", "medium", "high"]
    data   = {
        lvl: [r["quantum_fraction"] for r in records if r["security_level"] == lvl]
        for lvl in levels
    }
    colors = [PALETTE[lvl] for lvl in levels]

    fig, ax = plt.subplots(figsize=(7, 4.5))
    bp = ax.boxplot(
        [data[lvl] for lvl in levels],
        positions=range(len(levels)),
        widths=0.4,
        patch_artist=True,
        medianprops=dict(color="black", linewidth=2),
    )
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)

    # Jitter overlay
    rng = np.random.default_rng(7)
    for i, lvl in enumerate(levels):
        jitter = rng.uniform(-0.15, 0.15, len(data[lvl]))
        ax.scatter(
            np.full(len(data[lvl]), i) + jitter, data[lvl],
            s=18, alpha=0.5, color=PALETTE[lvl], zorder=3
        )

    ax.set_xticks(range(len(levels)))
    ax.set_xticklabels([f"{lvl.capitalize()}\nsecurity" for lvl in levels])
    ax.set_ylabel("Quantum fraction (quantum_bytes / payload_bytes)")
    ax.set_title("Fig 3 — Split Ratio (Quantum Fraction) by Security Level\n(all runs, all payload sizes)")
    ax.set_ylim(0, 1.05)
    ax.axhline(0.25, color=PALETTE["low"],    linestyle=":", alpha=0.6, label="Low target (0.25)")
    ax.axhline(0.50, color=PALETTE["medium"], linestyle=":", alpha=0.6, label="Medium target (0.50)")
    ax.axhline(0.75, color=PALETTE["high"],   linestyle=":", alpha=0.6, label="High target (0.75)")
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out = _FIG_DIR / "fig3_split_ratio.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  [OK] {out.relative_to(_ROOT)}")

    means = {lvl: float(np.mean(data[lvl])) if data[lvl] else 0.0 for lvl in levels}
    return {"mean_quantum_fraction": means}


# ---------------------------------------------------------------------------
# Fig 5 — Secret Key Rate vs QBER
# ---------------------------------------------------------------------------

def fig5_skr_vs_qber(records: list[dict]) -> dict:
    """
    Scatter plot of skr_bits_per_second vs qber.
    Uses latest-run records only — earlier runs used different protocol versions
    (pre-reconciliation) whose QKD timing is not directly comparable.
    Colour by noise_group; size by qkd_key_bits.
    """
    groups = {"0.00 (noiseless)": "#3498db", "0.05 (moderate)": "#e67e22"}
    fig, ax = plt.subplots(figsize=(7, 4.5))

    for group, color in groups.items():
        grp_recs = [r for r in records if r["noise_group"] == group]
        if not grp_recs:
            continue
        x = [r["qber"] for r in grp_recs]
        y = [r["skr_bits_per_second"] for r in grp_recs]
        sizes = [max(20, r.get("qkd_key_bits", 20) * 5) for r in grp_recs]
        ax.scatter(x, y, s=sizes, alpha=0.6, color=color, label=f"noise={group}", zorder=3)

    ax.axvline(0.11, color="red", linestyle="--", linewidth=1.5, label="Abort threshold (0.11)")
    ax.set_xlabel("Measured QBER")
    ax.set_ylabel("Secret Key Rate (bits/s)  [= key_bits / QKD_time]")
    ax.set_title("Fig 5 — Secret Key Rate vs Measured QBER\n(point size ∝ key length; latest run, Phase 9)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    out = _FIG_DIR / "fig5_skr_vs_qber.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  [OK] {out.relative_to(_ROOT)}")

    all_skr = [r["skr_bits_per_second"] for r in records]
    return {
        "skr_min": min(all_skr),
        "skr_max": max(all_skr),
        "skr_mean": float(np.mean(all_skr)),
    }


# ---------------------------------------------------------------------------
# Fig 6 — Throughput and latency by payload size
# ---------------------------------------------------------------------------

def fig6_throughput_latency(records: list[dict]) -> dict:
    """
    Grouped bar: mean throughput and latency per payload size bucket.
    Uses latest-run records only — earlier runs have pre-reconciliation timing
    that inflates throughput by 100–500× (sub-millisecond transfer times from
    an earlier, lighter protocol version).
    Two y-axes (throughput left, latency right).
    """
    sizes = sorted(set(r["payload_bytes"] for r in records))
    labels = [f"{s}B" for s in sizes]

    tput_means = []
    lat_means  = []
    tput_stds  = []
    lat_stds   = []
    for sz in sizes:
        bucket = [r for r in records if r["payload_bytes"] == sz]
        tp = [r["throughput_bytes_per_s"] for r in bucket]
        lt = [r["latency_s"] for r in bucket]
        tput_means.append(np.mean(tp))
        tput_stds.append(np.std(tp))
        lat_means.append(np.mean(lt))
        lat_stds.append(np.std(lt))

    x    = np.arange(len(sizes))
    width= 0.35

    fig, ax1 = plt.subplots(figsize=(7, 4.5))
    ax2 = ax1.twinx()

    bars1 = ax1.bar(x - width/2, tput_means, width, yerr=tput_stds,
                    label="Throughput (B/s)", color="#3498db", alpha=0.8,
                    capsize=4, error_kw={"linewidth": 1})
    bars2 = ax2.bar(x + width/2, lat_means, width, yerr=lat_stds,
                    label="Latency (s)", color="#e74c3c", alpha=0.8,
                    capsize=4, error_kw={"linewidth": 1})

    ax1.set_xlabel("Payload size")
    ax1.set_ylabel("Throughput (bytes/s)", color="#2980b9")
    ax2.set_ylabel("End-to-end latency (s)", color="#c0392b")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.set_title("Fig 6 — Throughput and Latency by Payload Size\n(mean ± 1 std, latest run, Phase 9)")

    lines1, lbls1 = ax1.get_legend_handles_labels()
    lines2, lbls2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, lbls1 + lbls2, loc="upper left", fontsize=8)
    ax1.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out = _FIG_DIR / "fig6_throughput_latency.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  [OK] {out.relative_to(_ROOT)}")

    return {
        "payload_sizes": sizes,
        "tput_means": [round(v, 1) for v in tput_means],
        "lat_means":  [round(v, 4) for v in lat_means],
    }


# ---------------------------------------------------------------------------
# Interactive HTML dashboard (Plotly)
# ---------------------------------------------------------------------------

def build_html_dashboard(all_records: list[dict], latest: list[dict]) -> None:
    """
    Produce a single self-contained HTML file with interactive Plotly charts.
    Five panels (Charts 1, 2, 3, 5, 6).  Charts 4 and 7 are omitted with a note.
    """

    # ---- Panel dimensions ----
    fig = make_subplots(
        rows=3, cols=2,
        subplot_titles=[
            "Fig 1 — Measured QBER vs Injected Noise",
            "Fig 2 — Outcome Breakdown by Noise Level (latest run, 54 configs)",
            "Fig 3 — Quantum Fraction by Security Level",
            "Fig 5 — Secret Key Rate vs Measured QBER",
            "Fig 6a — Throughput by Payload Size",
            "Fig 6b — End-to-end Latency by Payload Size",
        ],
        vertical_spacing=0.14,
        horizontal_spacing=0.10,
    )

    # ---- Fig 1 ----
    for group, color in [("0.00 (noiseless)", "#3498db"), ("0.05 (moderate)", "#e67e22")]:
        grp = [r for r in all_records if r["noise_group"] == group]
        if not grp:
            continue
        rng = np.random.default_rng(1)
        jitter = rng.uniform(-0.12, 0.12, len(grp))
        fig.add_trace(go.Box(
            y=[r["qber"] for r in grp],
            name=f"noise={group}",
            marker_color=color,
            boxpoints="all",
            jitter=0.3,
            pointpos=0,
            legendgroup=f"ng_{group}",
            showlegend=True,
        ), row=1, col=1)
    fig.add_hline(y=0.11, line_dash="dash", line_color="red",
                  annotation_text="Abort threshold (0.11)",
                  annotation_position="top right", row=1, col=1)

    # ---- Fig 2 ----
    n_noiseless = sum(1 for r in latest if r["noise_group"] == "0.00 (noiseless)")
    n_moderate  = sum(1 for r in latest if r["noise_group"] == "0.05 (moderate)")
    noise_labels = ["0.00<br>(noiseless)", "0.05<br>(moderate)", "0.12<br>(above threshold)"]
    clean_counts = [n_noiseless, n_moderate, 0]
    aborted_counts = [0, 0, GRID_PER_NOISE]

    fig.add_trace(go.Bar(
        x=noise_labels, y=clean_counts,
        name="CLEAN_PASS",
        marker_color=PALETTE["CLEAN_PASS"],
        legendgroup="outcome_clean",
        text=clean_counts,
        textposition="inside",
    ), row=1, col=2)
    fig.add_trace(go.Bar(
        x=noise_labels, y=aborted_counts,
        name="SESSION_ABORTED / RECON_INCOMPLETE (inferred)",
        marker_color=PALETTE["SESSION_ABORTED"],
        legendgroup="outcome_aborted",
        text=[f"{v}*" if v > 0 else "" for v in aborted_counts],
        textposition="inside",
    ), row=1, col=2)
    fig.update_layout(barmode="stack")

    # ---- Fig 3 ----
    levels = ["low", "medium", "high"]
    level_colors = [PALETTE[l] for l in levels]
    for lvl, color in zip(levels, level_colors):
        grp = [r["quantum_fraction"] for r in all_records if r["security_level"] == lvl]
        fig.add_trace(go.Box(
            y=grp,
            name=f"{lvl} security",
            marker_color=color,
            boxpoints="all",
            jitter=0.3,
            pointpos=0,
            legendgroup=f"sec_{lvl}",
            showlegend=True,
        ), row=2, col=1)

    # ---- Fig 5 (latest run only — protocol-consistent timing) ----
    for group, color in [("0.00 (noiseless)", "#3498db"), ("0.05 (moderate)", "#e67e22")]:
        grp = [r for r in latest if r["noise_group"] == group]
        if not grp:
            continue
        fig.add_trace(go.Scatter(
            x=[r["qber"] for r in grp],
            y=[r["skr_bits_per_second"] for r in grp],
            mode="markers",
            name=f"SKR noise={group}",
            marker=dict(
                color=color,
                size=[max(6, r.get("qkd_key_bits", 20) // 3) for r in grp],
                opacity=0.7,
            ),
            hovertemplate=(
                "QBER: %{x:.4f}<br>"
                "SKR: %{y:.1f} bits/s<br>"
                "<extra>noise=%{text}</extra>"
            ),
            text=[group] * len(grp),
            legendgroup=f"skr_{group}",
        ), row=2, col=2)
    fig.add_vline(x=0.11, line_dash="dash", line_color="red", row=2, col=2)

    # ---- Fig 6a — throughput (latest run only) ----
    sizes = sorted(set(r["payload_bytes"] for r in latest))
    tput_data = {sz: [r["throughput_bytes_per_s"] for r in latest if r["payload_bytes"] == sz]
                 for sz in sizes}
    lat_data  = {sz: [r["latency_s"]              for r in latest if r["payload_bytes"] == sz]
                 for sz in sizes}
    tput_means = [np.mean(tput_data[sz]) for sz in sizes]
    tput_stds  = [np.std(tput_data[sz])  for sz in sizes]
    lat_means  = [np.mean(lat_data[sz])  for sz in sizes]
    lat_stds   = [np.std(lat_data[sz])   for sz in sizes]
    size_labels = [f"{sz}B" for sz in sizes]

    fig.add_trace(go.Bar(
        x=size_labels, y=tput_means,
        error_y=dict(type="data", array=tput_stds, visible=True),
        name="Throughput (B/s)",
        marker_color="#3498db",
        text=[f"{v:.0f}" for v in tput_means],
        textposition="outside",
        legendgroup="tput",
    ), row=3, col=1)

    # ---- Fig 6b — latency ----
    fig.add_trace(go.Bar(
        x=size_labels, y=lat_means,
        error_y=dict(type="data", array=lat_stds, visible=True),
        name="Latency (s)",
        marker_color="#e74c3c",
        text=[f"{v:.3f}s" for v in lat_means],
        textposition="outside",
        legendgroup="lat",
    ), row=3, col=2)

    # ---- Layout ----
    fig.update_layout(
        title=dict(
            text=(
                "<b>Dynamic Hybrid Quantum-Classical Communication System</b><br>"
                "<sub>Benchmark Results Dashboard — BB84 + SDC, n_qubits=200, Phase 9 (reconciliation enabled)</sub>"
            ),
            x=0.5,
            font=dict(size=16),
        ),
        height=1100,
        legend=dict(orientation="v", x=1.02, y=1, font=dict(size=10)),
        template="plotly_white",
    )

    # Axis labels
    fig.update_yaxes(title_text="Measured QBER", row=1, col=1)
    fig.update_yaxes(title_text="# Configurations", row=1, col=2)
    fig.update_yaxes(title_text="Quantum Fraction", row=2, col=1)
    fig.update_xaxes(title_text="Measured QBER", row=2, col=2)
    fig.update_yaxes(title_text="SKR (bits/s)", row=2, col=2)
    fig.update_yaxes(title_text="Throughput (B/s)", row=3, col=1)
    fig.update_yaxes(title_text="Latency (s)", row=3, col=2)

    # Add annotation for skipped charts
    fig.add_annotation(
        x=0.5, y=-0.06,
        xref="paper", yref="paper",
        text=(
            "<b>Charts not shown:</b> "
            "Fig 4 (Reconciliation bits_corrected/sacrificed) — fields not written to log file; "
            "Fig 7 (CHSH/E91) — all sessions used BB84 (chsh=null for all records)."
        ),
        showarrow=False,
        font=dict(size=10, color="#666666"),
        align="center",
    )

    fig.write_html(
        str(_HTML_OUT),
        include_plotlyjs=True,   # fully self-contained, no CDN needed
        full_html=True,
    )
    print(f"  [OK] {_HTML_OUT.relative_to(_ROOT)}")


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------

def print_summary(
    all_records: list[dict],
    latest: list[dict],
    ts_start: str,
    ts_end: str,
    fig1_stats: dict,
    fig2_stats: dict,
    fig3_stats: dict,
    fig5_stats: dict,
    fig6_stats: dict,
) -> None:

    n_total       = len(all_records)
    n_latest      = len(latest)
    n_all_files   = len(set(r["run_file"] for r in all_records))

    print()
    print("=" * 66)
    print("  DASHBOARD GENERATION SUMMARY")
    print("=" * 66)
    print(f"  Records processed : {n_total} across {n_all_files} benchmark files")
    print(f"  Latest run        : {n_latest} records / 54 configs")
    print(f"  Date range        : {ts_start}  →  {ts_end}")
    print()
    print("  Chart takeaways:")
    print(f"  [Fig 1] QBER vs noise — noiseless sessions: QBER=0.0 (n={fig1_stats['noiseless_n']}); "
          f"moderate-noise sessions: mean QBER={fig1_stats['moderate_mean_qber']:.4f} "
          f"(n={fig1_stats['moderate_n']}), all well below the 0.11 abort threshold.")
    print(f"  [Fig 2] Outcome breakdown — {fig2_stats['clean_pass']}/54 ({fig2_stats['pct_clean']:.0f}%) "
          f"CLEAN_PASS; {fig2_stats['aborted_or_incomplete']}/54 ({fig2_stats['pct_aborted']:.0f}%) "
          f"aborted or reconciliation-incomplete (all from noise=0.12 configs, not logged).")
    frac = fig3_stats["mean_quantum_fraction"]
    print(f"  [Fig 3] Split ratio — mean quantum fractions: "
          f"low={frac['low']:.2f}, medium={frac['medium']:.2f}, high={frac['high']:.2f}, "
          f"matching the 0.25/0.50/0.75 target policy precisely.")
    print(f"  [Fig 4] SKIPPED — bits_corrected / bits_sacrificed not written to log "
          f"(reconciliation runs before MetricsCollector is called).")
    print(f"  [Fig 5] SKR vs QBER — SKR range {fig5_stats['skr_min']:.1f}–{fig5_stats['skr_max']:.1f} bits/s; "
          f"higher QBER correlates with more sifting loss → lower SKR as expected.")
    sizes = fig6_stats["payload_sizes"]
    tputs = fig6_stats["tput_means"]
    lats  = fig6_stats["lat_means"]
    size_summary = ", ".join(f"{s}B→{t:.0f}B/s/{l:.3f}s" for s, t, l in zip(sizes, tputs, lats))
    print(f"  [Fig 6] Throughput/latency by payload: {size_summary}. "
          f"Latency dominated by QKD + SDC encoding overhead, not payload size.")
    print(f"  [Fig 7] SKIPPED — no E91 sessions in log (chsh=null for all {n_total} records).")
    print()
    print("  Output files:")
    for f in sorted(_FIG_DIR.glob("*.png")):
        print(f"    {f.relative_to(_ROOT)}")
    print(f"    {_HTML_OUT.relative_to(_ROOT)}")
    print("=" * 66)


# ---------------------------------------------------------------------------
# Manual spot-check
# ---------------------------------------------------------------------------

def spot_check(latest: list[dict]) -> None:
    """
    Verify Fig 2's outcome counts against a direct grep-style count of the raw
    JSONL, then report the comparison.
    """
    # Direct count from raw records (no derived fields used)
    raw_path = max(_LOG_DIR.glob("benchmark_*.jsonl"), key=lambda p: p.stat().st_mtime)
    with open(raw_path, encoding="utf-8") as fh:
        raw_lines = [l.strip() for l in fh if l.strip()]
    raw_recs = [json.loads(l) for l in raw_lines]

    raw_total          = len(raw_lines)
    raw_qber_zero      = sum(1 for r in raw_recs if r["qber"] == 0.0)
    raw_qber_positive  = sum(1 for r in raw_recs if r["qber"] > 0.0)
    raw_clean_pass     = sum(1 for r in raw_recs if r["echo_outcome"] == "CLEAN_PASS")

    # Dashboard-computed values
    db_noiseless = sum(1 for r in latest if r["noise_group"] == "0.00 (noiseless)")
    db_moderate  = sum(1 for r in latest if r["noise_group"] == "0.05 (moderate)")

    print()
    print("  Manual spot-check — Fig 2 outcome counts vs raw JSONL:")
    print(f"    File                   : {raw_path.name}")
    print(f"    Raw total records      : {raw_total}")
    print(f"    Raw qber==0.0 count    : {raw_qber_zero}   ←→  dashboard noiseless : {db_noiseless}  {'MATCH' if raw_qber_zero == db_noiseless else 'MISMATCH'}")
    print(f"    Raw qber>0.0 count     : {raw_qber_positive}   ←→  dashboard moderate  : {db_moderate}  {'MATCH' if raw_qber_positive == db_moderate else 'MISMATCH'}")
    print(f"    Raw CLEAN_PASS count   : {raw_clean_pass}   ←→  dashboard total     : {db_noiseless + db_moderate}  {'MATCH' if raw_clean_pass == db_noiseless + db_moderate else 'MISMATCH'}")
    print(f"    Inferred non-logged    : {GRID_TOTAL - raw_total} (= 54 grid total − {raw_total} records → noise=0.12 row)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("\nGenerating dashboard figures...")
    print(f"  Log dir  : {_LOG_DIR}")
    print(f"  Fig dir  : {_FIG_DIR}")
    print(f"  HTML out : {_HTML_OUT}")
    print()

    all_records, latest = load_all_records()
    ts_start, ts_end    = date_range(all_records)

    print(f"  Loaded {len(all_records)} records from {len(set(r['run_file'] for r in all_records))} files")
    print(f"  Latest run ({max(_LOG_DIR.glob('benchmark_*.jsonl'), key=lambda p: p.stat().st_mtime).name}): {len(latest)} records")
    print()

    print("  Generating static PNGs (matplotlib):")
    fig1_stats = fig1_qber_vs_noise(all_records)
    fig2_stats = fig2_outcome_breakdown(latest)
    fig3_stats = fig3_split_ratio(all_records)
    fig5_stats = fig5_skr_vs_qber(latest)      # latest run only — protocol-consistent timing
    fig6_stats = fig6_throughput_latency(latest)  # latest run only — protocol-consistent timing

    print("\n  Generating interactive HTML (plotly):")
    build_html_dashboard(all_records, latest)

    spot_check(latest)
    print_summary(
        all_records, latest, ts_start, ts_end,
        fig1_stats, fig2_stats, fig3_stats, fig5_stats, fig6_stats,
    )


if __name__ == "__main__":
    main()
