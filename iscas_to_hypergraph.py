"""
iscas_to_hypergraph.py
======================
Parse ISCAS'85 / ISCAS'89 circuit netlists and emit them as a hypergraph.

Supported input formats
-----------------------
  .bench   – ISCAS BENCH format  (INPUT/OUTPUT/gate = ...)
  .isc     – ISCAS ISC  format   (gate-level column format)
  .ckt     – SELF v2.1 format    (INPUT/OUTPUT/GATE lines)
  .net     – UCLA NetDegree format (used by the KaHyPar pipeline)
  .blif    – Berkeley BLIF format (.model / .inputs / .outputs / .names)

Output formats (written to the same directory as the input)
-----------------------------------------------------------
  <circuit>.hgr   – KaHyPar / hMETIS hypergraph format
                    Header:  <num_nets> <num_nodes>
                    One line per net listing 1-indexed node IDs.

  <circuit>_hypergraph_report.txt
                  – Human-readable hypergraph report with:
                    • Summary statistics
                    • Full node list with degree
                    • Full net list with members
                    • Adjacency list (nodes sharing at least one net)

Usage
-----
  python iscas_to_hypergraph.py <netlist_file> [--no-hgr] [--no-report]

  python iscas_to_hypergraph.py c17.bench
  python iscas_to_hypergraph.py c432.isc
  python iscas_to_hypergraph.py circuit.net
  python iscas_to_hypergraph.py s27.bench

Batch mode (convert every supported file in a directory):
  python iscas_to_hypergraph.py --batch <directory>
"""

import sys
import os
import re
import argparse
from collections import defaultdict
from typing import Dict, List, Tuple, Optional


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Format parsers
#     Each parser returns: (nets, net_names, nodes, input_nodes, output_nodes)
#     where
#       nets        : List[List[str]]  – each net is a list of node names
#       net_names   : List[str]        – names for each net (same length)
#       nodes       : Dict[str, dict]  – node metadata (type, gate_type, …)
#       input_nodes : List[str]
#       output_nodes: List[str]
# ─────────────────────────────────────────────────────────────────────────────

def _clean_lines(path: str):
    """Read a file and return non-empty, comment-stripped lines."""
    with open(path, "r", errors="replace") as fh:
        lines = []
        for raw in fh:
            ln = raw.strip()
            # strip inline comments after '#'
            if "#" in ln:
                ln = ln[: ln.index("#")].strip()
            if ln:
                lines.append(ln)
    return lines


# ── BENCH parser ─────────────────────────────────────────────────────────────
def parse_bench(path: str):
    """
    BENCH format example
    --------------------
    INPUT(G1)
    OUTPUT(G22)
    G3  = NOT(G1)
    G6  = NAND(G1, G3)
    ...
    """
    lines = _clean_lines(path)

    nodes: Dict[str, dict] = {}
    input_nodes: List[str] = []
    output_nodes: List[str] = []

    # gate_name -> list of input gate names
    fanin_map: Dict[str, List[str]] = {}

    for ln in lines:
        ln_up = ln.upper()

        # INPUT(xxx)
        m = re.match(r"INPUT\s*\(\s*(\S+?)\s*\)", ln, re.IGNORECASE)
        if m:
            name = m.group(1)
            input_nodes.append(name)
            nodes[name] = {"type": "INPUT"}
            continue

        # OUTPUT(xxx)
        m = re.match(r"OUTPUT\s*\(\s*(\S+?)\s*\)", ln, re.IGNORECASE)
        if m:
            name = m.group(1)
            output_nodes.append(name)
            if name not in nodes:
                nodes[name] = {"type": "OUTPUT"}
            else:
                nodes[name]["type"] = "OUTPUT"
            continue

        # gate = TYPE(fanins...)
        m = re.match(r"(\S+)\s*=\s*(\w+)\s*\(([^)]*)\)", ln)
        if m:
            gate_name = m.group(1)
            gate_type = m.group(2).upper()
            fanins_raw = m.group(3)
            fanins = [f.strip() for f in fanins_raw.split(",") if f.strip()]
            if gate_name not in nodes:
                nodes[gate_name] = {"type": "GATE", "gate_type": gate_type}
            else:
                nodes[gate_name].update({"type": "GATE", "gate_type": gate_type})
            fanin_map[gate_name] = fanins
            for fi in fanins:
                if fi not in nodes:
                    nodes[fi] = {"type": "WIRE"}
            continue

    # Build nets: one net per gate output wire (gate + all its fanins)
    nets: List[List[str]] = []
    net_names: List[str] = []

    for gate, fanins in fanin_map.items():
        net = [gate] + fanins
        nets.append(net)
        net_names.append(f"net_{gate}")

    # Add primary inputs that are not already in any net as isolated stubs
    all_in_nets = {n for net in nets for n in net}
    for pi in input_nodes:
        if pi not in all_in_nets:
            nets.append([pi])
            net_names.append(f"net_{pi}")

    return nets, net_names, nodes, input_nodes, output_nodes


# ── ISC parser ────────────────────────────────────────────────────────────────
def parse_isc(path: str):
    """
    ISC format (column layout):
    -------------------------------------------
    *  c17
    *  ISCAS-85 benchmark
    1gat  inpt  1  0   >
    2gat  inpt  1  0   >
    ...
    10gat nand  1  2  1gat 2gat
    ...
    -------------------------------------------
    Columns: name  type  #fanout  #fanin  fanin_names...
    """
    lines = _clean_lines(path)

    nodes: Dict[str, dict] = {}
    input_nodes: List[str] = []
    output_nodes: List[str] = []
    fanin_map: Dict[str, List[str]] = {}

    for ln in lines:
        if ln.startswith("*"):
            continue
        parts = ln.split()
        if len(parts) < 4:
            continue
        name = parts[0]
        gtype = parts[1].lower()

        # fanout / fanin counts
        try:
            n_fanout = int(parts[2])
            n_fanin = int(parts[3])
        except ValueError:
            continue

        fanins = parts[4: 4 + n_fanin]

        if gtype in ("inpt",):
            nodes[name] = {"type": "INPUT"}
            input_nodes.append(name)
        elif gtype in ("from",):
            # branch node – treat as wire
            nodes[name] = {"type": "WIRE"}
        elif gtype in ("buff", "not"):
            nodes[name] = {"type": "GATE", "gate_type": gtype.upper()}
        else:
            nodes[name] = {"type": "GATE", "gate_type": gtype.upper()}

        if fanins:
            fanin_map[name] = fanins
            for fi in fanins:
                if fi not in nodes:
                    nodes[fi] = {"type": "WIRE"}

    # Detect outputs: nodes with zero fanout (n_fanout might be 0 or marked ">")
    # Re-parse for output detection
    with open(path, "r", errors="replace") as fh:
        for raw in fh:
            ln = raw.strip()
            if not ln or ln.startswith("*"):
                continue
            parts = ln.split()
            if len(parts) < 3:
                continue
            name = parts[0]
            try:
                n_fanout = int(parts[2])
                if n_fanout == 0 and name in nodes:
                    output_nodes.append(name)
                    nodes[name]["type"] = "OUTPUT"
            except (ValueError, IndexError):
                pass

    nets: List[List[str]] = []
    net_names: List[str] = []
    for gate, fanins in fanin_map.items():
        nets.append([gate] + fanins)
        net_names.append(f"net_{gate}")

    return nets, net_names, nodes, input_nodes, output_nodes


# ── SELF / .ckt parser ────────────────────────────────────────────────────────
def parse_ckt(path: str):
    """
    SELF (.ckt) v2.1 format:
    ------------------------
    INPUT  G1, G2, G3
    OUTPUT G22, G23
    GATE   NAND  G10  G1  G2
    GATE   NOT   G11  G10
    ...
    """
    lines = _clean_lines(path)

    nodes: Dict[str, dict] = {}
    input_nodes: List[str] = []
    output_nodes: List[str] = []
    fanin_map: Dict[str, List[str]] = {}

    for ln in lines:
        up = ln.upper()

        if up.startswith("INPUT"):
            # INPUT  G1, G2  or  INPUT G1 G2
            rest = ln[len("INPUT"):].replace(",", " ").split()
            for name in rest:
                input_nodes.append(name)
                nodes[name] = {"type": "INPUT"}

        elif up.startswith("OUTPUT"):
            rest = ln[len("OUTPUT"):].replace(",", " ").split()
            for name in rest:
                output_nodes.append(name)
                if name not in nodes:
                    nodes[name] = {"type": "OUTPUT"}
                else:
                    nodes[name]["type"] = "OUTPUT"

        elif up.startswith("GATE"):
            # GATE  <type>  <output>  <fanin1> <fanin2> ...
            parts = ln.split()
            if len(parts) < 3:
                continue
            gate_type = parts[1].upper()
            gate_out = parts[2]
            fanins = parts[3:]
            nodes[gate_out] = {"type": "GATE", "gate_type": gate_type}
            fanin_map[gate_out] = fanins
            for fi in fanins:
                if fi not in nodes:
                    nodes[fi] = {"type": "WIRE"}

    nets: List[List[str]] = []
    net_names: List[str] = []
    for gate, fanins in fanin_map.items():
        nets.append([gate] + fanins)
        net_names.append(f"net_{gate}")

    return nets, net_names, nodes, input_nodes, output_nodes


# ── UCLA .net / NetDegree parser ──────────────────────────────────────────────
def parse_net(path: str):
    """UCLA NetDegree format (as used in the KaHyPar pipeline)."""
    nodes: Dict[str, dict] = {}
    input_nodes: List[str] = []
    output_nodes: List[str] = []
    nets: List[List[str]] = []
    net_names: List[str] = []

    with open(path, "r", errors="replace") as fh:
        lines = [ln.strip() for ln in fh
                 if ln.strip()
                 and not ln.strip().startswith("#")
                 and not ln.strip().lower().startswith("ucla")]

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.lower().startswith("netdegree"):
            parts = ln.split()
            degree = int(parts[2])
            net_name = parts[3] if len(parts) > 3 else f"net_{len(nets)}"
            net_names.append(net_name)
            net_cells = []
            for j in range(1, degree + 1):
                cell_line = lines[i + j].split()
                cell_name = cell_line[0]
                direction = cell_line[1] if len(cell_line) > 1 else "I"
                net_cells.append(cell_name)
                if cell_name not in nodes:
                    nodes[cell_name] = {"type": "CELL"}
                if direction == "I" and cell_name not in input_nodes:
                    input_nodes.append(cell_name)
                elif direction == "O" and cell_name not in output_nodes:
                    output_nodes.append(cell_name)
            nets.append(net_cells)
            i += degree + 1
        else:
            i += 1

    return nets, net_names, nodes, input_nodes, output_nodes


# ── BLIF parser ───────────────────────────────────────────────────────────────
def parse_blif(path: str):
    """
    Basic BLIF parser (.model / .inputs / .outputs / .names).
    Each .names block defines one gate output; its cover lines are ignored,
    only the signal connectivity is extracted.
    """
    lines = _clean_lines(path)

    nodes: Dict[str, dict] = {}
    input_nodes: List[str] = []
    output_nodes: List[str] = []
    fanin_map: Dict[str, List[str]] = {}

    i = 0
    while i < len(lines):
        ln = lines[i]
        parts = ln.split()
        kw = parts[0].lower() if parts else ""

        if kw == ".inputs":
            names = parts[1:]
            # may continue on next lines starting without '.'
            j = i + 1
            while j < len(lines) and not lines[j].startswith("."):
                names += lines[j].split()
                j += 1
            for n in names:
                if n and not n.startswith("\\"):
                    input_nodes.append(n)
                    nodes[n] = {"type": "INPUT"}
            i = j
            continue

        elif kw == ".outputs":
            names = parts[1:]
            j = i + 1
            while j < len(lines) and not lines[j].startswith("."):
                names += lines[j].split()
                j += 1
            for n in names:
                if n:
                    output_nodes.append(n)
                    if n not in nodes:
                        nodes[n] = {"type": "OUTPUT"}
            i = j
            continue

        elif kw == ".names":
            # .names in1 in2 ... out
            signals = parts[1:]
            if not signals:
                i += 1
                continue
            gate_out = signals[-1]
            fanins = signals[:-1]
            nodes[gate_out] = {"type": "GATE", "gate_type": "LUT"}
            if fanins:
                fanin_map[gate_out] = fanins
                for fi in fanins:
                    if fi not in nodes:
                        nodes[fi] = {"type": "WIRE"}
            # skip cover lines
            j = i + 1
            while j < len(lines) and not lines[j].startswith("."):
                j += 1
            i = j
            continue

        elif kw in (".latch",):
            # .latch in out [type] [ctrl] [init]
            if len(parts) >= 3:
                gate_in, gate_out = parts[1], parts[2]
                nodes[gate_out] = {"type": "GATE", "gate_type": "LATCH"}
                fanin_map[gate_out] = [gate_in]
                if gate_in not in nodes:
                    nodes[gate_in] = {"type": "WIRE"}
            i += 1
            continue

        else:
            i += 1

    nets: List[List[str]] = []
    net_names: List[str] = []
    for gate, fanins in fanin_map.items():
        nets.append([gate] + fanins)
        net_names.append(f"net_{gate}")

    return nets, net_names, nodes, input_nodes, output_nodes


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Main converter: netlists → hypergraph data structures
# ─────────────────────────────────────────────────────────────────────────────

def build_hypergraph(nets, nodes):
    """
    Build core hypergraph data structures.

    Returns
    -------
    node_list   : List[str]   – ordered node names (index = node_id - 1)
    node_id     : Dict[str, int]  – name → 1-indexed ID
    nets_clean  : List[List[int]] – nets as lists of 1-indexed node IDs (no singletons)
    node_degree : Dict[str, int]  – how many nets each node belongs to
    net_degree  : List[int]       – size (degree) of each net
    """
    # Collect all nodes referenced in nets
    all_names = set(nodes.keys())
    for net in nets:
        all_names.update(net)

    node_list = sorted(all_names)
    node_id = {name: idx + 1 for idx, name in enumerate(node_list)}

    nets_clean = []
    for net in nets:
        ids = sorted({node_id[n] for n in net if n in node_id})
        if len(ids) >= 2:   # skip isolated / single-node nets
            nets_clean.append(ids)

    node_degree: Dict[str, int] = defaultdict(int)
    for net in nets_clean:
        for nid in net:
            node_degree[node_list[nid - 1]] += 1

    net_degree = [len(net) for net in nets_clean]

    return node_list, node_id, nets_clean, dict(node_degree), net_degree


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Writers
# ─────────────────────────────────────────────────────────────────────────────
# -----------------------------------------------------------------------------

def write_hgr(out_path: str, node_list, nets_clean):
    """Write KaHyPar/hMETIS .hgr hypergraph file."""
    num_nets = len(nets_clean)
    num_nodes = len(node_list)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"{num_nets} {num_nodes}\n")
        for net in nets_clean:
            fh.write(" ".join(map(str, net)) + "\n")
    print(f"  [HGR]    -> {out_path}  ({num_nets} nets, {num_nodes} nodes)")


def write_report(out_path: str,
                 circuit_name: str,
                 fmt: str,
                 node_list, node_id, nets_clean, net_names,
                 nodes, input_nodes, output_nodes,
                 node_degree, net_degree):
    """Write a full human-readable hypergraph report."""

    num_nodes = len(node_list)
    num_nets = len(nets_clean)
    total_pins = sum(net_degree)
    avg_net_degree = total_pins / num_nets if num_nets else 0
    max_net_degree = max(net_degree) if net_degree else 0
    min_net_degree = min(net_degree) if net_degree else 0
    avg_node_degree = total_pins / num_nodes if num_nodes else 0
    max_node_degree = max(node_degree.values()) if node_degree else 0

    # Degree distribution for nets
    deg_dist: Dict[int, int] = defaultdict(int)
    for d in net_degree:
        deg_dist[d] += 1

    with open(out_path, "w", encoding="utf-8") as fh:

        def w(line=""):
            fh.write(line + "\n")

        w("=" * 72)
        w(f"  HYPERGRAPH REPORT  --  {circuit_name.upper()}")
        w("=" * 72)
        w()

        # -- Summary ----------------------------------------------------------
        w("+-- SUMMARY " + "-" * 59)
        w(f"|  Source file     : {circuit_name}")
        w(f"|  Format          : {fmt}")
        w(f"|  Nodes (vertices): {num_nodes}")
        w(f"|    Primary inputs: {len(input_nodes)}")
        w(f"|    Primary outputs:{len(output_nodes)}")
        w(f"|  Nets (hyperedges): {num_nets}")
        w(f"|  Total pins       : {total_pins}")
        w(f"|  Avg net degree   : {avg_net_degree:.2f}")
        w(f"|  Min net degree   : {min_net_degree}")
        w(f"|  Max net degree   : {max_net_degree}")
        w(f"|  Avg node degree  : {avg_node_degree:.2f}")
        w(f"|  Max node degree  : {max_node_degree}")
        w("+" + "-" * 71)
        w()

        # ── Net degree distribution ──────────────────────────────────────────
        w("+-- NET DEGREE DISTRIBUTION " + "-" * 43)
        for d in sorted(deg_dist):
            bar = "#" * min(40, deg_dist[d])
            w(f"|  degree {d:3d} : {deg_dist[d]:5d} nets  {bar}")
        w("+" + "-" * 71)
        w()

        # ── Primary I/O ──────────────────────────────────────────────────────
        w("+-- PRIMARY INPUTS  " + "-" * 50)
        if input_nodes:
            chunks = [input_nodes[i:i+8] for i in range(0, len(input_nodes), 8)]
            for chunk in chunks:
                w("|  " + "  ".join(f"{n:<10}" for n in chunk))
        else:
            w("|  (none detected)")
        w("+" + "-" * 71)
        w()

        w("+-- PRIMARY OUTPUTS " + "-" * 50)
        if output_nodes:
            chunks = [output_nodes[i:i+8] for i in range(0, len(output_nodes), 8)]
            for chunk in chunks:
                w("|  " + "  ".join(f"{n:<10}" for n in chunk))
        else:
            w("|  (none detected)")
        w("+" + "-" * 71)
        w()

        # ── Node list ────────────────────────────────────────────────────────
        w("+-- NODE LIST  (id, name, type, net-degree) " + "-" * 27)
        w(f"|  {'ID':>6}  {'Name':<20}  {'Type':<10}  {'Degree':>6}")
        w("|  " + "-" * 48)
        for name in node_list:
            nid = node_id[name]
            meta = nodes.get(name, {})
            ntype = meta.get("type", "UNKNOWN")
            if "gate_type" in meta:
                ntype = f"GATE/{meta['gate_type']}"
            degree = node_degree.get(name, 0)
            w(f"|  {nid:>6}  {name:<20}  {ntype:<10}  {degree:>6}")
        w("+" + "-" * 71)
        w()

        # ── Net (hyperedge) list ─────────────────────────────────────────────
        w("+-- NET LIST  (id, name, degree, members) " + "-" * 29)
        w(f"|  {'ID':>6}  {'Net Name':<24}  {'Deg':>4}  Members")
        w("|  " + "-" * 65)
        for idx, (net, nname) in enumerate(zip(nets_clean, net_names)):
            nid = idx + 1
            members_str = " ".join(node_list[n - 1] for n in net)
            # Wrap long lines
            prefix = f"|  {nid:>6}  {nname:<24}  {len(net):>4}  "
            line_budget = 72 - len(prefix)
            tokens = members_str.split()
            lines_out, cur = [], []
            cur_len = 0
            for tok in tokens:
                if cur_len + len(tok) + 1 > line_budget and cur:
                    lines_out.append(prefix + " ".join(cur))
                    prefix = "|  " + " " * (len(prefix) - 3)
                    cur, cur_len = [tok], len(tok)
                else:
                    cur.append(tok)
                    cur_len += len(tok) + 1
            if cur:
                lines_out.append(prefix + " ".join(cur))
            w("\n".join(lines_out))
        w("+" + "-" * 71)
        w()

        # ── Node-to-net adjacency ────────────────────────────────────────────
        w("+-- NODE -> NET ADJACENCY " + "-" * 46)
        # Build reverse map
        node_nets: Dict[str, List[int]] = defaultdict(list)
        for nidx, net in enumerate(nets_clean):
            for nid in net:
                node_nets[node_list[nid - 1]].append(nidx + 1)

        w(f"|  {'Name':<20}  {'Degree':>6}  Nets")
        w("|  " + "-" * 60)
        for name in node_list:
            net_ids = node_nets.get(name, [])
            w(f"|  {name:<20}  {len(net_ids):>6}  {' '.join(map(str, net_ids))}")
        w("+" + "-" * 71)
        w()

        # ── HGR format inline ─────────────────────────────────────────────────
        w("+-- HGR FORMAT PREVIEW (first 30 nets) " + "-" * 31)
        w(f"|  {num_nets} {num_nodes}   # <num_nets> <num_nodes>")
        for net in nets_clean[:30]:
            w("|  " + " ".join(map(str, net)))
        if num_nets > 30:
            w(f"|  ... ({num_nets - 30} more nets in .hgr file)")
        w("+" + "-" * 71)
        w()
        w("End of report.")

    print(f"  [REPORT] → {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Format detector
# ─────────────────────────────────────────────────────────────────────────────

def detect_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".bench":
        return "BENCH"
    if ext == ".isc":
        return "ISC"
    if ext == ".ckt":
        return "SELF"
    if ext == ".net":
        return "UCLA-NET"
    if ext == ".blif":
        return "BLIF"
    # Try to sniff content
    try:
        with open(path, "r", errors="replace") as fh:
            head = "".join(fh.readline() for _ in range(20)).upper()
        if "INPUT(" in head or "OUTPUT(" in head:
            return "BENCH"
        if "NETDEGREE" in head:
            return "UCLA-NET"
        if ".INPUTS" in head or ".OUTPUTS" in head:
            return "BLIF"
    except Exception:
        pass
    return "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Top-level converter
# ─────────────────────────────────────────────────────────────────────────────

def convert_file(path: str, write_hgr_file: bool = True, write_report_file: bool = True):
    if not os.path.isfile(path):
        print(f"[ERROR] File not found: {path}")
        return

    fmt = detect_format(path)
    circuit_name = os.path.basename(path)
    base = os.path.splitext(path)[0]

    print(f"\n{'='*60}")
    print(f"  Converting: {circuit_name}  (format: {fmt})")
    print(f"{'='*60}")

    # ── Parse ────────────────────────────────────────────────────────────────
    parsers = {
        "BENCH":    parse_bench,
        "ISC":      parse_isc,
        "SELF":     parse_ckt,
        "UCLA-NET": parse_net,
        "BLIF":     parse_blif,
    }

    if fmt not in parsers:
        print(f"[ERROR] Unknown or unsupported format: {fmt}")
        return

    nets, net_names, nodes, input_nodes, output_nodes = parsers[fmt](path)

    print(f"  Parsed  : {len(nets)} nets, {len(nodes)} nodes")
    print(f"  Inputs  : {len(input_nodes)}   Outputs: {len(output_nodes)}")

    # ── Build hypergraph ─────────────────────────────────────────────────────
    node_list, node_id, nets_clean, node_degree, net_degree = build_hypergraph(nets, nodes)

    print(f"  Hypergraph: {len(nets_clean)} nets (≥2 nodes), {len(node_list)} vertices")

    # ── Write outputs ────────────────────────────────────────────────────────
    if write_hgr_file:
        write_hgr(base + ".hgr", node_list, nets_clean)

    if write_report_file:
        report_path = base + "_hypergraph_report.txt"
        write_report(
            report_path, circuit_name, fmt,
            node_list, node_id, nets_clean, net_names,
            nodes, input_nodes, output_nodes,
            node_degree, net_degree
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Batch mode
# ─────────────────────────────────────────────────────────────────────────────

SUPPORTED_EXTS = {".bench", ".isc", ".ckt", ".net", ".blif"}

def batch_convert(directory: str, write_hgr_file=True, write_report_file=True):
    found = []
    for fname in sorted(os.listdir(directory)):
        ext = os.path.splitext(fname)[1].lower()
        if ext in SUPPORTED_EXTS:
            found.append(os.path.join(directory, fname))

    if not found:
        print(f"[BATCH] No supported netlist files found in: {directory}")
        return

    print(f"[BATCH] Found {len(found)} file(s) in: {directory}")
    for fp in found:
        convert_file(fp, write_hgr_file, write_report_file)

    print(f"\n[BATCH] Done. Converted {len(found)} file(s).")


# ─────────────────────────────────────────────────────────────────────────────
# 7.  CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="ISCAS / VLSI circuit netlist → hypergraph converter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    ap.add_argument("input", nargs="?", help="Netlist file (or directory with --batch)")
    ap.add_argument("--batch", "-b", metavar="DIR",
                    help="Convert every supported file in DIR")
    ap.add_argument("--no-hgr", action="store_true",
                    help="Skip writing .hgr file")
    ap.add_argument("--no-report", action="store_true",
                    help="Skip writing text report")
    args = ap.parse_args()

    write_hgr_file   = not args.no_hgr
    write_report_file = not args.no_report

    if args.batch:
        batch_convert(args.batch, write_hgr_file, write_report_file)
    elif args.input:
        convert_file(args.input, write_hgr_file, write_report_file)
    else:
        ap.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
