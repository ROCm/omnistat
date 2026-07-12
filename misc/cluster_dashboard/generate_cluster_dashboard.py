#!/usr/bin/env python3
# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -------------------------------------------------------------------------------
"""
generate_cluster_dashboard.py — Grafana dashboard for a multi-rack HPC cluster.

Each rack is a vertical column where servers are drawn proportional to their U height. Compute
servers show live Omnistat telemetry and can link to companion node dashboards. Non-compute servers
(network, storage, blank) render as solid coloured blocks with a text label.

Usage:
    python generate_cluster_dashboard.py cluster_mapping.yaml
    python generate_cluster_dashboard.py cluster_mapping.yaml --output my.json --preview

Cluster YAML schema — see cluster_mapping.yaml for a documented example.

GPU field names: "{hostname}-gpu{card}"  (cards 0 .. gpu_count-1)
Must match Prometheus legendFormat: "{{instance}}-gpu{{card}}"
"""

import argparse
import json
import re
import random
import sys
from pathlib import Path

import yaml

UNIT_SYM = {"celsius": "°C", "percent": "%", "watt": "W"}


# ── Canvas geometry ────────────────────────────────────────────────────────────

U_PX = 14  # pixels per rack unit
RACK_WIDTH = 220  # px width of each rack column
RACK_GAP = 6  # horizontal gap between adjacent racks
U_LABEL_W = 30  # left column reserved for U numbers (drawn once)
LEFT_MARGIN = U_LABEL_W + 6
TOP_MARGIN = 8  # above title bar
TITLE_H = 28  # title bar height
HEADER_H = 42  # combined title + color bar row
HEADER_SPLIT = 310  # x-position of vertical divider between title and color bar
RACK_LABEL_H = 24  # rack-ID label above each rack outline
SERVER_PAD = 3  # padding inside each server box (applied top/bottom/sides)
GPU_GAP = 2  # gap between GPU boxes within a compute server
RIGHT_MARGIN = 20


# ── Non-compute type palette  (bg, border, text) ──────────────────────────────

TYPE_PALETTE = {
    "network": ("#0d2050", "#1a3a8a", "#88aadd"),
    "storage": ("#142038", "#1e3860", "#7799bb"),
    "blank": ("#0a0a14", "#1e2038", "#222233"),  # subtle rule, not a full box
    "other": ("#141420", "#222230", "#7777aa"),
}

COMPUTE_BG = "#0e0e1c"
COMPUTE_BORDER = "#3a3a5a"

# ── Color scheme definitions for static color bar ─────────────────────────────
# Each entry: list of (position 0..1, hex color) stops
COLOR_SCHEMES = {
    "continuous-GrYlRd": [(0.0, "#2e7d32"), (0.5, "#f9a825"), (1.0, "#c62828")],
    "continuous-RdYlGr": [(0.0, "#c62828"), (0.5, "#f9a825"), (1.0, "#2e7d32")],
    "continuous-BlYlRd": [(0.0, "#1565c0"), (0.5, "#f9a825"), (1.0, "#c62828")],
}


def _interp_color(stops: list, t: float) -> str:
    """Interpolate RGB between color stops at position t ∈ [0, 1]."""
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            r = int(int(c0[1:3], 16) + f * (int(c1[1:3], 16) - int(c0[1:3], 16)))
            g = int(int(c0[3:5], 16) + f * (int(c1[3:5], 16) - int(c0[3:5], 16)))
            b = int(int(c0[5:7], 16) + f * (int(c1[5:7], 16) - int(c0[5:7], 16)))
            return f"#{r:02x}{g:02x}{b:02x}"
    return stops[-1][1]


def colorbar_els(prefix: str, top: int, left: int, height: int, metric: dict) -> list:
    """Metric label + gradient bar + min/max labels (no bg — shares title bar)."""
    stops = COLOR_SCHEMES.get(metric.get("color", "continuous-GrYlRd"), COLOR_SCHEMES["continuous-GrYlRd"])
    min_val = metric.get("min", 0)
    max_val = metric.get("max", 100)

    name_w = 160  # metric label width
    num_w = 46  # min/max label width
    bar_gap = 14  # gap between num labels and gradient edge
    bar_w = 300  # gradient width
    bar_pad = 10  # vertical inset for gradient within row

    bar_left = left + name_w + num_w + bar_gap
    bar_h = height - 2 * bar_pad
    N = 30
    seg_w = bar_w / N

    els = []
    # Metric label — white, 13pt
    els.append(
        text_el(
            f"{prefix}-name",
            top,
            left,
            name_w,
            height,
            metric["label"],
            font_size=13,
            color="#ffffff",
            align="left",
        )
    )
    # Min value
    els.append(
        text_el(
            f"{prefix}-min",
            top,
            left + name_w,
            num_w,
            height,
            str(min_val),
            font_size=12,
            color="#ffffff",
            align="right",
        )
    )
    # Gradient segments
    for i in range(N):
        t = i / (N - 1)
        color = _interp_color(stops, t)
        els.append(
            {
                "type": "rectangle",
                "name": f"{prefix}-seg-{i}",
                "connections": [],
                "constraint": _constraint(),
                "background": {"color": {"fixed": color}, "image": None},
                "border": {"color": {"fixed": color}, "width": 0},
                "config": {
                    "align": "center",
                    "valign": "middle",
                    "links": [],
                    "text": {"fixed": ""},
                },
                "placement": {
                    "top": top + bar_pad,
                    "left": round(bar_left + i * seg_w),
                    "width": max(1, round(seg_w + 0.5)),
                    "height": bar_h,
                    "rotation": 0,
                },
            }
        )
    # Max value
    els.append(
        text_el(
            f"{prefix}-max",
            top,
            round(bar_left + bar_w + bar_gap),
            num_w,
            height,
            str(max_val),
            font_size=12,
            color="#ffffff",
            align="left",
        )
    )
    return els


# GPU threshold colours — same palette as the Helios single-rack dashboard
GPU_THRESHOLDS = [
    {"color": "#2e7d5e", "value": None},  # < 70 °C  muted green
    {"color": "#b07d2a", "value": 70},  # 70–84 °C amber
    {"color": "#933030", "value": 85},  # ≥ 85 °C  muted red
]


# ── Canvas element factories ──────────────────────────────────────────────────
# background / border are TOP-LEVEL on the element, not inside config.


def _constraint():
    return {"horizontal": "left", "vertical": "top"}


def rect(name, top, left, width, height, bg="#0f0f1e", border_color="#555566", border_width=1):
    return {
        "type": "rectangle",
        "name": name,
        "connections": [],
        "constraint": _constraint(),
        "background": {"color": {"fixed": bg}, "image": None},
        "border": {"color": {"fixed": border_color}, "width": border_width},
        "config": {
            "align": "center",
            "valign": "middle",
            "links": [],
            "text": {"fixed": ""},
        },
        "placement": {
            "top": top,
            "left": left,
            "width": width,
            "height": height,
            "rotation": 0,
        },
    }


def text_el(
    name,
    top,
    left,
    width,
    height,
    content,
    font_size=10,
    color="#aaaaaa",
    align="center",
):
    return {
        "type": "text",
        "name": name,
        "connections": [],
        "constraint": _constraint(),
        "background": {"color": {"fixed": "transparent"}, "image": None},
        "border": {"color": {"fixed": "transparent"}, "width": 0},
        "config": {
            "align": align,
            "valign": "middle",
            "links": [],
            "color": {"fixed": color},
            "size": font_size,
            "text": {"fixed": content},
        },
        "placement": {
            "top": top,
            "left": left,
            "width": width,
            "height": height,
            "rotation": 0,
        },
    }


def gpu_metric_el(name, top, left, width, height, field_name, show_value=True, metric_label=""):
    # Keep text field-bound for data; hide visually when show_value=False.
    text_color = "#ffffff" if show_value else "rgba(0,0,0,0)"
    return {
        "type": "rectangle",
        "name": name,
        "connections": [],
        "constraint": _constraint(),
        "background": {
            "color": {"field": field_name, "fixed": "#2a2a3e"},
            "image": None,
        },
        "border": {"color": {"fixed": "#333355"}, "width": 1},
        "config": {
            "align": "center",
            "valign": "middle",
            "links": [],
            "size": 11,
            "color": {"fixed": text_color},
            "text": {"field": field_name, "mode": "field"},
        },
        "placement": {
            "top": top,
            "left": left,
            "width": width,
            "height": height,
            "rotation": 0,
        },
    }


# ── Geometry helpers ───────────────────────────────────────────────────────────


def rack_left(col: int) -> int:
    """Canvas x for the left edge of rack column (1-indexed)."""
    return LEFT_MARGIN + (col - 1) * (RACK_WIDTH + RACK_GAP)


def server_top_px(rack_top: int, rack_max_u: int, u_start: int, u_height: int) -> int:
    """
    Canvas y for the top of a server.
    u_start is 1-indexed from the BOTTOM of the rack (physical convention).
    """
    return rack_top + (rack_max_u - u_start - u_height + 1) * U_PX


def server_h_px(u_height: int) -> int:
    """Pixel height of a server, leaving a 1 px gap between adjacent servers."""
    return u_height * U_PX - 1


# ── Auto-blank fill ────────────────────────────────────────────────────────────


def fill_blanks(servers: list, max_u: int) -> list:
    """
    Return servers list with 1U blank panels inserted for every unoccupied
    U slot up to max_u.  Explicit blanks in the YAML are no longer required.
    """
    occupied: set = set()
    for srv in servers:
        for u in range(srv["u_start"], srv["u_start"] + srv["u_height"]):
            occupied.add(u)
    result = list(servers)
    for u in range(1, max_u + 1):
        if u not in occupied:
            result.append({"u_start": u, "u_height": 1, "type": "blank"})
    return result


# ── Element builders ───────────────────────────────────────────────────────────


def u_label_els(rack_top: int, max_u: int) -> list:
    """U-number column drawn once on the far left."""
    els = []
    # Show every U if spacing allows; otherwise every 5
    step = 1 if U_PX >= 10 else 5
    for u in range(1, max_u + 1, step):
        uy = rack_top + (max_u - u) * U_PX
        els.append(
            text_el(
                f"u-lbl-{u}",
                uy,
                0,
                U_LABEL_W - 2,
                U_PX,
                str(u),
                font_size=8,
                color="#445566",
                align="right",
            )
        )
    return els


def rack_els(
    rack: dict,
    rack_top: int,
    max_u_global: int,
    rack_idx: int,
    omnistat_port: int = 8000,
    show_values: bool = True,
    metric_label: str = "",
) -> list:
    """All canvas elements for one rack column."""
    els = []
    rack_id = rack["rack_id"]
    col = rack.get("col", rack_idx + 1)
    max_u = rack.get("height_u", max_u_global)
    servers = rack.get("servers", [])
    lx = rack_left(col)

    # Align rack bottoms — shorter racks sit at the same bottom as tallest
    rack_h = max_u * U_PX
    ry = rack_top + (max_u_global - max_u) * U_PX  # top of rack outline

    # ── Rack label (above outline) ────────────────────────────────────────────
    label_y = ry - RACK_LABEL_H
    els.append(
        text_el(
            f"rack-{rack_id}-lbl",
            label_y,
            lx,
            RACK_WIDTH,
            RACK_LABEL_H,
            rack.get("label", rack_id),
            font_size=14,
            color="#a0b4d0",
        )
    )

    # ── Per-server elements (blanks auto-filled for unoccupied U slots) ──────
    servers = fill_blanks(servers, max_u)
    for si, srv in enumerate(servers):
        u_start = srv["u_start"]
        u_height = srv["u_height"]
        stype = srv.get("type", "blank")
        sname = f"rack-{rack_id}-s{si}"

        sy = server_top_px(ry, max_u, u_start, u_height)
        sh = server_h_px(u_height)
        sw = RACK_WIDTH - 4  # 2 px inset on each side
        sx = lx + 2

        if stype == "compute":
            hostname = srv.get("hostname", f"node-{si}")
            vms = srv.get("vms")
            gpu_count = len(vms) if vms else srv.get("gpu_count", 4)

            # Server background
            els.append(
                rect(
                    sname,
                    sy,
                    sx,
                    sw,
                    sh,
                    bg=COMPUTE_BG,
                    border_color=COMPUTE_BORDER,
                    border_width=2,
                )
            )

            # GPU metric boxes — laid out horizontally, card 0 leftmost
            gpu_w = (sw - 2 * SERVER_PAD - (gpu_count - 1) * GPU_GAP) // gpu_count
            gpu_h = sh - 2 * SERVER_PAD

            for card in range(gpu_count):
                gx = sx + SERVER_PAD + card * (gpu_w + GPU_GAP)
                gy = sy + SERVER_PAD
                if vms:
                    vm = vms[card]
                    port = vm.get("omnistat_port", omnistat_port)
                    field = f"{vm['hostname']}:{port}-gpu0"
                    el_name = vm["hostname"]
                else:
                    field = f"{hostname}:{omnistat_port}-gpu{card}"
                    el_name = hostname
                els.append(
                    gpu_metric_el(
                        el_name,
                        gy,
                        gx,
                        gpu_w,
                        gpu_h,
                        field,
                        show_value=show_values,
                        metric_label=metric_label,
                    )
                )

        elif stype == "blank":
            els.append(
                rect(
                    sname,
                    sy,
                    sx,
                    sw,
                    sh,
                    bg="#0a0a12",
                    border_color="#1e2038",
                    border_width=1,
                )
            )

        else:
            # network / storage / other — solid block + label
            palette = TYPE_PALETTE.get(stype, TYPE_PALETTE["other"])
            bg, border, txt_color = palette
            if "color" in srv:  # per-item override
                bg = srv["color"]
            label = srv.get("label", stype.title())

            els.append(rect(sname, sy, sx, sw, sh, bg=bg, border_color=border, border_width=2))
            # Only draw text if there's enough vertical room
            if sh >= 12:
                els.append(
                    text_el(
                        f"{sname}-lbl",
                        sy,
                        sx,
                        sw,
                        sh,
                        label,
                        font_size=10,
                        color=txt_color,
                    )
                )

    # ── Rack background (drawn first, behind servers) ─────────────────────────
    # Rack background with border — drawn first (behind servers) so it never
    # blocks hover events on GPU elements.
    els.insert(
        1,
        rect(
            f"rack-{rack_id}-bg",
            ry,
            lx,
            RACK_WIDTH,
            rack_h,
            bg="#09090f",
            border_color="#3a4a5a",
            border_width=1,
        ),
    )

    # Top edge drawn last (on top) because the border on rack-bg is covered by
    # server elements that start at ry.
    els.append(
        rect(
            f"rack-{rack_id}-bt",
            ry,
            lx,
            RACK_WIDTH,
            1,
            bg="#3a4a5a",
            border_color="#3a4a5a",
            border_width=0,
        )
    )

    return els


# ── Dashboard builder ──────────────────────────────────────────────────────────


def build_dashboard(cluster: dict) -> dict:
    """Grafana 13 native tab-per-metric dashboard."""
    cluster_id = cluster.get("cluster_id", "cluster")
    display_name = cluster.get("display_name", cluster_id)
    omnistat_port = cluster.get("omnistat_port", 8000)
    racks = cluster.get("racks", [])
    metrics = cluster.get(
        "metrics",
        [
            {
                "name": "rocm_temperature_celsius",
                "label": "Temperature (°C)",
                "unit": "celsius",
                "min": 30,
                "max": 80,
                "color": "continuous-GrYlRd",
            },
        ],
    )

    if not racks:
        sys.exit("Error: cluster YAML has no racks defined.")

    max_u = max(r.get("height_u", 42) for r in racks)
    n_cols = max(r.get("col", i + 1) for i, r in enumerate(racks))
    canvas_w = LEFT_MARGIN + n_cols * RACK_WIDTH + (n_cols - 1) * RACK_GAP + RIGHT_MARGIN
    header_w = canvas_w - RIGHT_MARGIN  # align right edge with last rack

    header_top = TOP_MARGIN
    rack_top = header_top + HEADER_H + RACK_LABEL_H

    def make_rack_elements(m, port):
        els = []
        # Header outline — right edge flush with last rack
        els.append(
            rect(
                "title-bar",
                header_top,
                0,
                header_w,
                HEADER_H,
                bg="#141428",
                border_color="#2a2a48",
                border_width=1,
            )
        )
        # Color bar section — slightly different background
        els.append(
            rect(
                "title-bar-cb",
                header_top + 1,
                HEADER_SPLIT,
                header_w - HEADER_SPLIT - 1,
                HEADER_H - 2,
                bg="#1c2040",
                border_color="transparent",
                border_width=0,
            )
        )
        # 1px accent line at the boundary
        els.append(
            rect(
                "title-div",
                header_top,
                HEADER_SPLIT,
                1,
                HEADER_H,
                bg="#3a4a6a",
                border_color="#3a4a6a",
                border_width=0,
            )
        )
        # Title text (left side)
        els.append(
            text_el(
                "title-txt",
                header_top,
                8,
                HEADER_SPLIT - 20,
                HEADER_H,
                f"{display_name}  —  Rack View",
                font_size=14,
                color="#c0d0e8",
                align="left",
            )
        )
        # Color bar content (right of divider)
        els.extend(colorbar_els("cb", header_top, HEADER_SPLIT + 28, HEADER_H, m))
        # U labels and racks
        els.extend(u_label_els(rack_top, max_u))
        base_label = re.sub(r"\s*\(.*?\)\s*$", "", m.get("label", "")).strip()
        unit_sym = UNIT_SYM.get(m.get("unit", ""), "")
        tooltip = f"{base_label} {{{{card=${{__field.labels.card}}}}}}: ${{__value.numeric}}"
        if unit_sym:
            tooltip += f" {unit_sym}"
        for i, rack in enumerate(racks):
            els.extend(
                rack_els(
                    rack,
                    rack_top,
                    max_u,
                    i,
                    port,
                    show_values=m.get("show_values", True),
                    metric_label=tooltip,
                )
            )
        return els

    # Build node data link for fieldConfig.defaults.links
    grafana_url = cluster.get("grafana_url", "").rstrip("/")
    node_path = cluster.get("node_dashboard_path", "")
    node_url = (
        (grafana_url + node_path).replace("{instance}", "${__field.labels.instance}")
        if grafana_url and node_path
        else ""
    )

    # Build elements dict and tabs
    elements = {}
    tabs = []

    for idx, m in enumerate(metrics):
        panel_name = f"panel-{idx + 1}"
        elements[panel_name] = {
            "kind": "Panel",
            "spec": {
                "data": {
                    "kind": "QueryGroup",
                    "spec": {
                        "queries": [
                            {
                                "kind": "PanelQuery",
                                "spec": {
                                    "hidden": False,
                                    "query": {
                                        "group": "",
                                        "kind": "DataQuery",
                                        "spec": {
                                            "editorMode": "code",
                                            "exemplar": False,
                                            "expr": m["name"],
                                            "instant": True,
                                            "legendFormat": "{{instance}}-gpu{{card}}",
                                            "range": False,
                                        },
                                        "version": "v0",
                                    },
                                    "refId": "A",
                                },
                            }
                        ],
                        "queryOptions": {},
                        "transformations": [],
                    },
                },
                "description": "",
                "id": idx + 1,
                "links": [],
                "title": "",
                "vizConfig": {
                    "group": "canvas",
                    "kind": "VizConfig",
                    "spec": {
                        "fieldConfig": {
                            "defaults": {
                                "unit": m.get("unit", "none"),
                                "decimals": 1,
                                "min": m.get("min", 0),
                                "max": m.get("max", 100),
                                "color": {"mode": m.get("color", "continuous-GrYlRd")},
                                "thresholds": {
                                    "mode": "absolute",
                                    "steps": GPU_THRESHOLDS,
                                },
                                "links": (
                                    [
                                        {
                                            "title": "Node dashboard",
                                            "url": node_url,
                                            "targetBlank": True,
                                        }
                                    ]
                                    if node_url
                                    else []
                                ),
                                "custom": {},
                            },
                            "overrides": [],
                        },
                        "options": {
                            "inlineEditing": False,
                            "panZoom": False,
                            "showAdvancedTypes": True,
                            "tooltip": {"disableForOneClick": False, "mode": "single"},
                            "zoomToContent": False,
                            "root": {
                                "type": "frame",
                                "name": "root",
                                "elements": make_rack_elements(m, omnistat_port),
                                "placement": {
                                    "height": 100,
                                    "left": 0,
                                    "width": 100,
                                    "top": 0,
                                    "rotation": 0,
                                },
                                "background": {
                                    "color": {"fixed": "transparent"},
                                    "image": None,
                                },
                                "border": {"color": {"fixed": "dark-blue"}},
                            },
                        },
                    },
                    "version": "13.0.1",
                },
            },
        }

        tabs.append(
            {
                "kind": "TabsLayoutTab",
                "spec": {
                    "title": m["label"],
                    "layout": {
                        "kind": "RowsLayout",
                        "spec": {
                            "rows": [
                                {
                                    "kind": "RowsLayoutRow",
                                    "spec": {
                                        "collapse": False,
                                        "title": "",
                                        "layout": {
                                            "kind": "GridLayout",
                                            "spec": {
                                                "items": [
                                                    {
                                                        "kind": "GridLayoutItem",
                                                        "spec": {
                                                            "element": {
                                                                "kind": "ElementReference",
                                                                "name": panel_name,
                                                            },
                                                            "height": 40,
                                                            "width": 24,
                                                            "x": 0,
                                                            "y": 0,
                                                        },
                                                    }
                                                ],
                                            },
                                        },
                                    },
                                }
                            ],
                        },
                    },
                },
            }
        )

    return {
        "title": f"{cluster_id}  —  Rack View",
        "tags": ["gpu", "rocm", "cluster", "omnistat"],
        "editable": True,
        "liveNow": False,
        "preload": False,
        "cursorSync": "Off",
        "links": [],
        "elements": elements,
        "layout": {
            "kind": "TabsLayout",
            "spec": {"tabs": tabs},
        },
        "timeSettings": {
            "autoRefresh": "30s",
            "autoRefreshIntervals": [
                "5s",
                "10s",
                "30s",
                "1m",
                "5m",
                "15m",
                "30m",
                "1h",
            ],
            "fiscalYearStartMonth": 0,
            "from": "now-5m",
            "hideTimepicker": False,
            "timezone": "browser",
            "to": "now",
        },
        "variables": [],
        "annotations": [
            {
                "kind": "AnnotationQuery",
                "spec": {
                    "builtIn": True,
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "query": {
                        "datasource": {"name": "-- Grafana --"},
                        "group": "grafana",
                        "kind": "DataQuery",
                        "spec": {},
                        "version": "v0",
                    },
                },
            }
        ],
        "preferences": {"layout": {"kind": "GridLayout", "spec": {"items": []}}},
    }


# ── SVG preview ───────────────────────────────────────────────────────────────


def _svg_interp_color(stops: list, t: float) -> str:
    """Interpolate RGB for SVG gradient at position t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            r = int(int(c0[1:3], 16) + f * (int(c1[1:3], 16) - int(c0[1:3], 16)))
            g = int(int(c0[3:5], 16) + f * (int(c1[3:5], 16) - int(c0[3:5], 16)))
            b = int(int(c0[5:7], 16) + f * (int(c1[5:7], 16) - int(c0[5:7], 16)))
            return f"#{r:02x}{g:02x}{b:02x}"
    return stops[-1][1]


def generate_preview_svg(cluster: dict, output_path: Path) -> None:
    """Static SVG approximating the Grafana canvas layout with random temps."""
    cluster_id = cluster.get("cluster_id", "cluster")
    display_name = cluster.get("display_name", cluster_id)
    racks = cluster.get("racks", [])
    metrics = cluster.get(
        "metrics",
        [
            {
                "name": "rocm_temperature_celsius",
                "label": "Temperature (\u00b0C)",
                "unit": "celsius",
                "min": 30,
                "max": 80,
                "color": "continuous-GrYlRd",
            },
        ],
    )
    default_metric = metrics[0]
    max_u = max(r.get("height_u", 42) for r in racks)
    n_cols = max(r.get("col", i + 1) for i, r in enumerate(racks))
    rng = random.Random(42)

    canvas_w = LEFT_MARGIN + n_cols * RACK_WIDTH + (n_cols - 1) * RACK_GAP + RIGHT_MARGIN
    header_w = canvas_w - RIGHT_MARGIN
    header_top = TOP_MARGIN
    rack_top = header_top + HEADER_H + RACK_LABEL_H
    canvas_h = rack_top + max_u * U_PX + 20

    # Color scheme for GPU boxes and colorbar
    scheme_name = default_metric.get("color", "continuous-GrYlRd")
    stops = COLOR_SCHEMES.get(scheme_name, COLOR_SCHEMES["continuous-GrYlRd"])
    m_min = default_metric.get("min", 30)
    m_max = default_metric.get("max", 80)

    L: list = []
    a = L.append

    a(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{canvas_w}" height="{canvas_h}" '
        f"style=\"background:#0d0d0d; font-family:'Courier New',monospace;\">"
    )

    # ── Header bar with colorbar ──────────────────────────────────────────────
    # Scale colorbar to fit available width
    title_w = min(HEADER_SPLIT, header_w // 2)
    cb_left = title_w + 10
    avail_w = header_w - cb_left - 10
    bar_pad = 10
    bar_h = HEADER_H - 2 * bar_pad
    num_w = 30
    bar_gap = 6

    # Metric label gets up to 45% of available space
    name_w = min(160, int(avail_w * 0.45))
    bar_w = avail_w - name_w - 2 * num_w - 2 * bar_gap
    bar_w = max(40, bar_w)  # minimum gradient width
    bar_left = cb_left + name_w + num_w + bar_gap
    N_seg = min(30, bar_w // 4)
    seg_w = bar_w / max(N_seg, 1)

    a(
        f'<rect x="0" y="{header_top}" width="{header_w}" height="{HEADER_H}" '
        f'fill="#141428" stroke="#2a2a48" stroke-width="1"/>'
    )
    # Color bar background (right section)
    a(
        f'<rect x="{title_w}" y="{header_top + 1}" '
        f'width="{header_w - title_w - 1}" height="{HEADER_H - 2}" '
        f'fill="#1c2040"/>'
    )
    # Divider line
    a(f'<rect x="{title_w}" y="{header_top}" width="1" height="{HEADER_H}" ' f'fill="#3a4a6a"/>')
    # Title text
    a(
        f'<text x="8" y="{header_top + HEADER_H // 2 + 5}" fill="#c0d0e8" '
        f'font-size="14" font-weight="bold">{display_name}  \u2014  Rack View</text>'
    )
    # Metric name (full label including unit)
    a(
        f'<text x="{cb_left}" y="{header_top + HEADER_H // 2 + 5}" fill="#ffffff" '
        f'font-size="11">{default_metric["label"]}</text>'
    )
    # Min value
    a(
        f'<text x="{cb_left + name_w + num_w}" y="{header_top + HEADER_H // 2 + 5}" '
        f'fill="#ffffff" font-size="11" text-anchor="end">{m_min}</text>'
    )
    # Gradient segments
    for i in range(N_seg):
        t = i / (N_seg - 1) if N_seg > 1 else 0.5
        color = _svg_interp_color(stops, t)
        sx = round(bar_left + i * seg_w)
        sw = max(1, round(seg_w + 0.5))
        a(f'<rect x="{sx}" y="{header_top + bar_pad}" width="{sw}" ' f'height="{bar_h}" fill="{color}"/>')
    # Max value
    a(
        f'<text x="{round(bar_left + bar_w + bar_gap)}" '
        f'y="{header_top + HEADER_H // 2 + 5}" fill="#ffffff" font-size="11">'
        f"{m_max}</text>"
    )

    # ── U number column ───────────────────────────────────────────────────────
    step = 1 if U_PX >= 10 else 5
    for u in range(1, max_u + 1, step):
        uy = rack_top + (max_u - u) * U_PX + U_PX // 2 + 3
        a(f'<text x="{U_LABEL_W - 2}" y="{uy}" fill="#8899aa" font-size="8" ' f'text-anchor="end">{u}</text>')

    # ── Per-rack ──────────────────────────────────────────────────────────────
    for i, rack in enumerate(racks):
        rack_id = rack["rack_id"]
        col = rack.get("col", i + 1)
        r_max_u = rack.get("height_u", max_u)
        servers = rack.get("servers", [])
        lx = rack_left(col)
        rack_h = r_max_u * U_PX
        ry = rack_top + (max_u - r_max_u) * U_PX

        # Rack outline
        a(
            f'<rect x="{lx}" y="{ry}" width="{RACK_WIDTH}" height="{rack_h}" '
            f'fill="#09090f" stroke="#3a4a5a" stroke-width="1"/>'
        )

        # Rack label
        ly = ry - RACK_LABEL_H // 2 + 4
        a(
            f'<text x="{lx + RACK_WIDTH // 2}" y="{ly}" fill="#a0b4d0" '
            f'font-size="14" font-weight="bold" text-anchor="middle">'
            f'{rack.get("label", rack_id)}</text>'
        )

        # Servers
        servers = fill_blanks(servers, r_max_u)
        for si, srv in enumerate(servers):
            u_start = srv["u_start"]
            u_height = srv["u_height"]
            stype = srv.get("type", "blank")
            sy = server_top_px(ry, r_max_u, u_start, u_height)
            sh = server_h_px(u_height)
            sw = RACK_WIDTH - 4
            sx = lx + 2

            if stype == "compute":
                vms = srv.get("vms")
                gpu_count = len(vms) if vms else srv.get("gpu_count", 4)
                a(
                    f'<rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" '
                    f'fill="{COMPUTE_BG}" stroke="{COMPUTE_BORDER}" stroke-width="2"/>'
                )
                gpu_w = (sw - 2 * SERVER_PAD - (gpu_count - 1) * GPU_GAP) // gpu_count
                gpu_h = sh - 2 * SERVER_PAD
                for card in range(gpu_count):
                    gx = sx + SERVER_PAD + card * (gpu_w + GPU_GAP)
                    gy = sy + SERVER_PAD
                    t = rng.uniform(m_min, m_max * 1.1)
                    t_norm = (t - m_min) / (m_max - m_min) if m_max > m_min else 0.5
                    fc = _svg_interp_color(stops, t_norm)
                    a(
                        f'<rect x="{gx}" y="{gy}" width="{gpu_w}" height="{gpu_h}" '
                        f'fill="{fc}" stroke="#333355" stroke-width="1" rx="1"/>'
                    )

            elif stype == "blank":
                a(
                    f'<rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" '
                    f'fill="#0a0a12" stroke="#1e2038" stroke-width="1"/>'
                )

            else:
                palette = TYPE_PALETTE.get(stype, TYPE_PALETTE["other"])
                bg, border, txt_color = palette
                if "color" in srv:
                    bg = srv["color"]
                label = srv.get("label", stype.title())
                a(
                    f'<rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" '
                    f'fill="{bg}" stroke="{border}" stroke-width="2"/>'
                )
                if sh >= 12:
                    cy = sy + sh // 2 + 4
                    a(
                        f'<text x="{sx + sw // 2}" y="{cy}" fill="{txt_color}" '
                        f'font-size="10" font-weight="bold" text-anchor="middle">'
                        f"{label}</text>"
                    )

    a("</svg>")
    output_path.write_text("\n".join(L))
    print(f"Preview SVG \u2192 {output_path}")


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Grafana Canvas dashboard for a multi-rack HPC cluster.")
    parser.add_argument(
        "cluster",
        metavar="CLUSTER_YAML",
        help="Cluster layout YAML file (see example_cluster.yaml)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="cluster_dashboard.json",
        help="Output JSON file (default: cluster_dashboard.json)",
    )
    parser.add_argument("--preview", "-p", action="store_true", help="Also write a static SVG preview")
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    args = parser.parse_args()

    path = Path(args.cluster)
    if not path.exists():
        sys.exit(f"Error: '{path}' not found.")

    with open(path) as fh:
        cluster = yaml.safe_load(fh)

    if "racks" not in cluster:
        sys.exit("Error: cluster YAML must contain a 'racks' list.")

    dashboard = build_dashboard(cluster)
    out = Path(args.output)
    with open(out, "w") as fh:
        json.dump(dashboard, fh, indent=2)

    n_racks = len(cluster["racks"])
    n_gpus = sum(
        srv.get("gpu_count", 0)
        for r in cluster["racks"]
        for srv in r.get("servers", [])
        if srv.get("type") == "compute"
    )
    print(f"Written → {out}  ({n_racks} racks, {n_gpus} GPUs)")

    if args.preview:
        generate_preview_svg(cluster, out.with_suffix(".svg"))

    print(f"\nImport into Grafana:  Dashboards → Import → upload {out}")


if __name__ == "__main__":
    main()
