"""
vlsi_kahypar_pipeline.py
========================
One-command pipeline: VLSI input (.net + .are) → partition → VLSI-aware output.

Usage:
    python vlsi_kahypar_pipeline.py <circuit>.net <circuit>.are [--gui] [--dry-run]

Flags:
    --gui       Open an interactive HTML visualizer in your browser after running.
    --dry-run   Force the built-in FM partitioner (skip KaHyPar binary lookup).
"""

import sys, os, subprocess, random, json, webbrowser, tempfile

# ---------------------------------------------------------------------------
# Step 1 – Parse .net file
# ---------------------------------------------------------------------------

def parse_net_file(net_path):
    print(f"[1/5] Parsing net file: {net_path}")
    nets, net_names = [], []
    with open(net_path, "r") as f:
        lines = [ln.strip() for ln in f
                 if ln.strip() and not ln.startswith("#")
                 and not ln.lower().startswith("ucla")]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lower().startswith("netdegree"):
            parts = line.split()
            degree = int(parts[2])
            net_name = parts[3] if len(parts) > 3 else f"net_{len(nets)}"
            net_names.append(net_name)
            cells = [lines[i + j].split()[0] for j in range(1, degree + 1)]
            nets.append(cells)
            i += degree + 1
        else:
            i += 1
    print(f"    → Found {len(nets)} nets")
    return nets, net_names

# ---------------------------------------------------------------------------
# Step 2 – Parse .are file
# ---------------------------------------------------------------------------

def parse_are_file(are_path):
    print(f"[2/5] Parsing area file: {are_path}")
    areas = {}
    with open(are_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.lower().startswith("ucla"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    areas[parts[0]] = float(parts[1])
                except ValueError:
                    continue
    print(f"    → Found {len(areas)} cells with area data")
    return areas

# ---------------------------------------------------------------------------
# Step 3 – Convert to .hgr
# ---------------------------------------------------------------------------

def convert_to_hgr(nets, areas, hgr_path="output.hgr"):
    print(f"[3/5] Converting to HGR format: {hgr_path}")
    cell_set = {}
    for net in nets:
        for cell in net:
            if cell not in cell_set:
                cell_set[cell] = len(cell_set)
    node_list = list(cell_set.keys())
    num_vertices = len(node_list)
    num_nets = len(nets)
    has_weights = bool(areas)
    fmt_flag = "10" if has_weights else "0"
    with open(hgr_path, "w") as f:
        f.write(f"{num_nets} {num_vertices} {fmt_flag}\n")
        for net in nets:
            ids = [str(cell_set[cell] + 1) for cell in net]
            f.write(" ".join(ids) + "\n")
        if has_weights:
            for cell in node_list:
                f.write(f"{areas.get(cell, 1.0)}\n")
    print(f"    → {num_vertices} vertices, {num_nets} nets written to {hgr_path}")
    return node_list

# ---------------------------------------------------------------------------
# Step 4a – KaHyPar binary
# ---------------------------------------------------------------------------

_BINARY_CANDIDATES = [
    os.path.join(".", "kahypar", "build", "KaHyPar"),
    os.path.join(".", "kahypar", "build", "KaHyPar.exe"),
    os.path.join(".", "KaHyPar"),
    os.path.join(".", "KaHyPar.exe"),
]

def _find_binary():
    for path in _BINARY_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None

def _run_kahypar_binary(hgr_path, k, epsilon, objective, mode):
    binary = _find_binary()
    if binary is None:
        return False
    print(f"    → Binary: {binary}")
    cmd = [binary, "-h", hgr_path, "-k", str(k),
           "-e", str(epsilon), "-o", objective, "-m", mode]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("    [ERROR] KaHyPar binary failed.")
        return False
    print("    → KaHyPar finished successfully")
    return True

# ---------------------------------------------------------------------------
# Step 4b – Built-in FM partitioner
# ---------------------------------------------------------------------------

def _fm_partition(nets, node_list, areas, k=2, epsilon=0.03, seed=0):
    random.seed(seed)
    n = len(node_list)
    node_weights = [areas.get(cell, 1.0) for cell in node_list]
    total_weight = sum(node_weights)
    max_part_weight = total_weight / k * (1.0 + epsilon)

    order = sorted(range(n), key=lambda i: node_weights[i], reverse=True)
    part = [0] * n
    part_weight = [0.0, 0.0]
    for idx in order:
        p = 0 if part_weight[0] <= part_weight[1] else 1
        part[idx] = p
        part_weight[p] += node_weights[idx]

    node_idx = {name: i for i, name in enumerate(node_list)}
    net_nodes = [[node_idx[c] for c in net if c in node_idx] for net in nets]
    node_nets = [[] for _ in range(n)]
    for e, ids in enumerate(net_nodes):
        for i in ids:
            node_nets[i].append(e)

    def cut_count():
        return sum(1 for ids in net_nodes if ids and len({part[i] for i in ids}) > 1)

    def gain(node_id):
        src, dst, g = part[node_id], 1 - part[node_id], 0
        for e in node_nets[node_id]:
            ids = net_nodes[e]
            if sum(1 for i in ids if part[i] == src) == 1:
                g += 1
            if sum(1 for i in ids if part[i] == dst) == 0:
                g -= 1
        return g

    best_part = part[:]
    best_cut = cut_count()

    for _ in range(30):
        locked = [False] * n
        move_stack, gains = [], []
        for _ in range(n):
            best_g, best_node = None, -1
            for i in range(n):
                if locked[i]:
                    continue
                src, dst = part[i], 1 - part[i]
                if part_weight[dst] + node_weights[i] > max_part_weight:
                    continue
                g = gain(i)
                if best_g is None or g > best_g:
                    best_g, best_node = g, i
            if best_node == -1:
                break
            i = best_node
            src, dst = part[i], 1 - part[i]
            part[i] = dst
            part_weight[src] -= node_weights[i]
            part_weight[dst] += node_weights[i]
            locked[i] = True
            move_stack.append((i, src, dst))
            gains.append(best_g)

        cum, best_cum, best_prefix = 0, 0, len(move_stack)
        for idx, g in enumerate(gains):
            cum += g
            if cum > best_cum:
                best_cum, best_prefix = cum, idx + 1

        for i in range(len(move_stack) - 1, best_prefix - 1, -1):
            node_id, src, dst = move_stack[i]
            part[node_id] = src
            part_weight[src] += node_weights[node_id]
            part_weight[dst] -= node_weights[node_id]

        current_cut = cut_count()
        if current_cut < best_cut:
            best_cut = current_cut
            best_part = part[:]
        else:
            break

    return best_part

def _write_partition_file(partitions, hgr_path, k, epsilon):
    pf = f"{hgr_path}.part{k}.epsilon{epsilon:.2f}.seed0.KaHyPar"
    with open(pf, "w") as f:
        for p in partitions:
            f.write(f"{p}\n")
    return pf

# ---------------------------------------------------------------------------
# Step 4 – Unified runner
# ---------------------------------------------------------------------------

def run_partitioner(nets, node_list, areas, hgr_path="output.hgr",
                    k=2, epsilon=0.03, objective="cut", mode="direct", dry_run=False):
    if not dry_run:
        print("[4/5] Partitioning: looking for KaHyPar binary...")
        if _run_kahypar_binary(hgr_path, k, epsilon, objective, mode):
            return
        print("    → Binary not found — falling back to built-in FM heuristic")

    print("[4/5] Partitioning: Built-in FM heuristic")
    partitions = _fm_partition(nets, node_list, areas, k=k, epsilon=epsilon)
    pf = _write_partition_file(partitions, hgr_path, k, epsilon)
    print(f"    → Partition written to: {pf}")

# ---------------------------------------------------------------------------
# Step 5 – Metrics
# ---------------------------------------------------------------------------

def read_partition_file(hgr_path="output.hgr", k=2, epsilon=0.03):
    for pf in [f"{hgr_path}.part{k}.epsilon{epsilon:.2f}.seed0.KaHyPar",
               f"{hgr_path}.part{k}.epsilon{epsilon}.seed0.KaHyPar"]:
        if os.path.isfile(pf):
            with open(pf) as f:
                return [int(ln.strip()) for ln in f if ln.strip()]
    print("[ERROR] Partition file not found.")
    sys.exit(1)

def compute_metrics(nets, node_list, partitions, areas):
    print("[5/5] Computing VLSI metrics")
    node_map = {node_list[i]: partitions[i] for i in range(len(node_list))}
    cut_nets, cut_net_names = 0, []
    for idx, net in enumerate(nets):
        parts_in_net = {node_map[c] for c in net if c in node_map}
        if len(parts_in_net) > 1:
            cut_nets += 1
            cut_net_names.append(idx)

    partition_areas = {}
    for cell, part in node_map.items():
        partition_areas[part] = partition_areas.get(part, 0.0) + areas.get(cell, 1.0)

    if len(partition_areas) >= 2:
        max_a, min_a = max(partition_areas.values()), min(partition_areas.values())
        area_balance = max_a / min_a if min_a > 0 else float("inf")
    else:
        area_balance = 1.0

    return node_map, cut_nets, cut_net_names, area_balance, partition_areas

def print_results(node_map, cut_nets, area_balance, partition_areas):
    print("\n" + "=" * 60)
    print("  PARTITIONING RESULTS")
    print("=" * 60)
    for part_id in sorted(set(node_map.values())):
        cells = sorted(c for c, p in node_map.items() if p == part_id)
        print(f"\n  Partition {part_id} ({len(cells)} cells): {', '.join(cells)}")
    print(f"\n  Cut Nets     : {cut_nets}")
    print(f"  Area Balance : {area_balance:.4f}  (max/min partition area)")
    for part_id, area in sorted(partition_areas.items()):
        print(f"    Partition {part_id} total area: {area:.2f}")
    print("=" * 60 + "\n")

# ---------------------------------------------------------------------------
# GUI – HTML Visualizer
# ---------------------------------------------------------------------------

def launch_gui(nets, net_names, node_list, node_map, areas,
               cut_net_indices, partition_areas, area_balance):
    """Generate and open a self-contained HTML VLSI partition visualizer."""

    # Build graph data for D3
    nodes_data = []
    for cell in node_list:
        nodes_data.append({
            "id": cell,
            "partition": node_map.get(cell, 0),
            "area": areas.get(cell, 1.0)
        })

    edges_data = []
    for idx, net in enumerate(nets):
        is_cut = idx in cut_net_indices
        for i in range(len(net)):
            for j in range(i + 1, len(net)):
                edges_data.append({
                    "source": net[i],
                    "target": net[j],
                    "net": net_names[idx] if idx < len(net_names) else f"net{idx}",
                    "cut": is_cut
                })

    p0_cells = sorted(c for c, p in node_map.items() if p == 0)
    p1_cells = sorted(c for c, p in node_map.items() if p == 1)
    cut_count = len(cut_net_indices)
    total_nets = len(nets)
    p0_area = partition_areas.get(0, 0)
    p1_area = partition_areas.get(1, 0)

    graph_json = json.dumps({"nodes": nodes_data, "links": edges_data})

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>VLSI KaHyPar Partition Visualizer</title>
<meta name="description" content="Interactive VLSI circuit partition visualizer powered by KaHyPar FM heuristic."/>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

  :root {{
    --bg: #0a0e1a;
    --panel: #0f1626;
    --card: #141d30;
    --border: #1e2d4a;
    --p0: #4f8ef7;
    --p1: #f7634f;
    --cut: #fbbf24;
    --intact: #334155;
    --text: #e2e8f0;
    --muted: #64748b;
    --accent: #38bdf8;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: 'Inter', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }}

  header {{
    background: linear-gradient(135deg, #0f1626 0%, #0d1b35 100%);
    border-bottom: 1px solid var(--border);
    padding: 18px 32px;
    display: flex;
    align-items: center;
    gap: 16px;
  }}

  .logo {{
    width: 42px; height: 42px;
    background: linear-gradient(135deg, var(--p0), var(--accent));
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
  }}

  header h1 {{ font-size: 1.2rem; font-weight: 600; letter-spacing: -0.02em; }}
  header p  {{ font-size: 0.78rem; color: var(--muted); margin-top: 2px; }}

  .badge {{
    margin-left: auto;
    background: rgba(79,142,247,.15);
    border: 1px solid rgba(79,142,247,.3);
    color: var(--p0);
    font-size: 0.72rem;
    font-weight: 600;
    padding: 4px 12px;
    border-radius: 99px;
  }}

  .main {{
    display: grid;
    grid-template-columns: 1fr 320px;
    flex: 1;
    gap: 0;
    overflow: hidden;
  }}

  /* ── Graph canvas ── */
  #graph-panel {{
    position: relative;
    background: radial-gradient(ellipse at 30% 40%, #0d1b35 0%, #0a0e1a 70%);
    overflow: hidden;
  }}

  #graph-panel svg {{
    width: 100%; height: 100%;
  }}

  .partition-label {{
    position: absolute;
    top: 20px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: .06em;
    text-transform: uppercase;
    padding: 6px 14px;
    border-radius: 6px;
  }}
  .lbl-0 {{ left: 20px;  background: rgba(79,142,247,.15); color: var(--p0); border: 1px solid rgba(79,142,247,.25); }}
  .lbl-1 {{ right: 20px; background: rgba(247,99,79,.15);  color: var(--p1); border: 1px solid rgba(247,99,79,.25); }}

  .dashed-divider {{
    position: absolute; top: 0; bottom: 0; left: 50%;
    border-left: 1px dashed rgba(255,255,255,.07);
    pointer-events: none;
  }}

  /* ── Sidebar ── */
  .sidebar {{
    background: var(--panel);
    border-left: 1px solid var(--border);
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 0;
  }}

  .section {{
    padding: 20px;
    border-bottom: 1px solid var(--border);
  }}

  .section-title {{
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: .1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 14px;
  }}

  /* metric cards */
  .metrics-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }}

  .metric-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
    position: relative;
    overflow: hidden;
  }}
  .metric-card::before {{
    content: '';
    position: absolute; inset: 0;
    background: linear-gradient(135deg, rgba(255,255,255,.02) 0%, transparent 100%);
  }}
  .metric-label {{ font-size: 0.68rem; color: var(--muted); margin-bottom: 6px; }}
  .metric-value {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.4rem;
    font-weight: 600;
    line-height: 1;
  }}
  .metric-sub {{ font-size: 0.68rem; color: var(--muted); margin-top: 4px; }}

  .blue  {{ color: var(--p0); }}
  .red   {{ color: var(--p1); }}
  .amber {{ color: var(--cut); }}
  .green {{ color: #4ade80; }}

  /* partition area bar */
  .area-bar-wrap {{ margin-top: 4px; }}
  .area-row {{ display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 5px; }}
  .area-bar {{
    height: 8px; border-radius: 4px;
    background: var(--border);
    overflow: hidden; margin-bottom: 8px;
  }}
  .area-fill {{
    height: 100%; border-radius: 4px;
    transition: width 1s ease;
  }}

  /* partition cell lists */
  .part-block {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
    margin-bottom: 10px;
  }}
  .part-block-title {{
    font-size: 0.72rem;
    font-weight: 600;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
  .cell-chips {{
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }}
  .chip {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.68rem;
    padding: 3px 9px;
    border-radius: 5px;
    border: 1px solid;
  }}
  .chip-0 {{ background: rgba(79,142,247,.1); border-color: rgba(79,142,247,.3); color: var(--p0); }}
  .chip-1 {{ background: rgba(247,99,79,.1);  border-color: rgba(247,99,79,.3);  color: var(--p1); }}

  /* legend */
  .legend {{ display: flex; flex-direction: column; gap: 8px; }}
  .legend-row {{ display: flex; align-items: center; gap: 10px; font-size: 0.78rem; }}
  .legend-line {{
    width: 28px; height: 3px; border-radius: 2px; flex-shrink: 0;
  }}
  .legend-dash {{
    width: 28px; height: 0;
    border-top: 2px dashed var(--cut);
    flex-shrink: 0;
  }}
  .legend-node {{
    width: 14px; height: 14px;
    border-radius: 50%; flex-shrink: 0;
    border: 2px solid;
  }}

  /* tooltip */
  #tooltip {{
    position: fixed;
    background: #1e2d4a;
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 0.78rem;
    padding: 8px 12px;
    border-radius: 8px;
    pointer-events: none;
    opacity: 0;
    transition: opacity .15s;
    z-index: 999;
    box-shadow: 0 8px 32px rgba(0,0,0,.4);
  }}

  #tooltip.show {{ opacity: 1; }}

  /* scrollbar */
  .sidebar::-webkit-scrollbar {{ width: 4px; }}
  .sidebar::-webkit-scrollbar-track {{ background: transparent; }}
  .sidebar::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}

  footer {{
    background: var(--panel);
    border-top: 1px solid var(--border);
    padding: 10px 32px;
    font-size: 0.7rem;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
</style>
</head>
<body>

<header>
  <div class="logo">⚡</div>
  <div>
    <h1>VLSI Partition Visualizer</h1>
    <p>Fiduccia-Mattheyses Min-Cut · KaHyPar Pipeline</p>
  </div>
  <div class="badge">2-way Partition</div>
</header>

<div class="main">

  <!-- Graph -->
  <div id="graph-panel">
    <div class="dashed-divider"></div>
    <div class="partition-label lbl-0">Partition 0</div>
    <div class="partition-label lbl-1">Partition 1</div>
    <svg id="svg"></svg>
  </div>

  <!-- Sidebar -->
  <aside class="sidebar">

    <div class="section">
      <div class="section-title">Partition Metrics</div>
      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-label">Total Cells</div>
          <div class="metric-value">{len(node_list)}</div>
          <div class="metric-sub">nodes in graph</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Total Nets</div>
          <div class="metric-value">{total_nets}</div>
          <div class="metric-sub">hyperedges</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Cut Nets</div>
          <div class="metric-value amber">{cut_count}</div>
          <div class="metric-sub">{cut_count}/{total_nets} nets span both</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Area Balance</div>
          <div class="metric-value {'green' if area_balance < 1.05 else 'amber'}">{area_balance:.3f}</div>
          <div class="metric-sub">max/min area ratio</div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-title">Area Distribution</div>
      <div class="area-bar-wrap">
        <div class="area-row">
          <span class="blue">Partition 0</span>
          <span style="font-family:monospace">{p0_area:.1f}</span>
        </div>
        <div class="area-bar">
          <div class="area-fill" style="width:{p0_area/(p0_area+p1_area)*100:.1f}%;background:var(--p0)"></div>
        </div>
        <div class="area-row">
          <span class="red">Partition 1</span>
          <span style="font-family:monospace">{p1_area:.1f}</span>
        </div>
        <div class="area-bar">
          <div class="area-fill" style="width:{p1_area/(p0_area+p1_area)*100:.1f}%;background:var(--p1)"></div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-title">Cell Assignments</div>
      <div class="part-block">
        <div class="part-block-title">
          <span class="dot" style="background:var(--p0)"></span>
          <span class="blue">Partition 0</span>
          <span style="color:var(--muted);font-weight:400;margin-left:auto">{len(p0_cells)} cells</span>
        </div>
        <div class="cell-chips">
          {''.join(f'<span class="chip chip-0">{c}</span>' for c in p0_cells)}
        </div>
      </div>
      <div class="part-block">
        <div class="part-block-title">
          <span class="dot" style="background:var(--p1)"></span>
          <span class="red">Partition 1</span>
          <span style="color:var(--muted);font-weight:400;margin-left:auto">{len(p1_cells)} cells</span>
        </div>
        <div class="cell-chips">
          {''.join(f'<span class="chip chip-1">{c}</span>' for c in p1_cells)}
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-title">Legend</div>
      <div class="legend">
        <div class="legend-row">
          <span class="legend-node" style="background:rgba(79,142,247,.2);border-color:var(--p0)"></span>
          Partition 0 cell
        </div>
        <div class="legend-row">
          <span class="legend-node" style="background:rgba(247,99,79,.2);border-color:var(--p1)"></span>
          Partition 1 cell
        </div>
        <div class="legend-row">
          <div class="legend-dash"></div>
          <span class="amber">Cut net (spans both partitions)</span>
        </div>
        <div class="legend-row">
          <div class="legend-line" style="background:var(--intact)"></div>
          Internal net (same partition)
        </div>
      </div>
    </div>

  </aside>
</div>

<footer>
  <span>VLSI KaHyPar Pipeline · FM Heuristic Partitioner</span>
  <span>Hover nodes for details · Drag to rearrange</span>
</footer>

<div id="tooltip"></div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const graph = {graph_json};

const svg = d3.select("#svg");
const panel = document.getElementById("graph-panel");
const tip = document.getElementById("tooltip");

function resize() {{
  const W = panel.clientWidth, H = panel.clientHeight;
  svg.attr("viewBox", `0 0 ${{W}} ${{H}}`);
  return [W, H];
}}

let [W, H] = resize();

const sim = d3.forceSimulation(graph.nodes)
  .force("link", d3.forceLink(graph.links).id(d => d.id).distance(90).strength(0.3))
  .force("charge", d3.forceManyBody().strength(-280))
  .force("collide", d3.forceCollide(38))
  .force("x", d3.forceX(d => d.partition === 0 ? W * 0.27 : W * 0.73).strength(0.18))
  .force("y", d3.forceY(H / 2).strength(0.05));

const g = svg.append("g");

// Zoom
svg.call(d3.zoom().scaleExtent([0.3, 3]).on("zoom", e => g.attr("transform", e.transform)));

// Links
const link = g.append("g").selectAll("line").data(graph.links).join("line")
  .attr("stroke", d => d.cut ? "#fbbf24" : "#1e2d4a")
  .attr("stroke-width", d => d.cut ? 1.5 : 1)
  .attr("stroke-dasharray", d => d.cut ? "5,4" : null)
  .attr("stroke-opacity", 0.8);

// Nodes
const nodeG = g.append("g").selectAll("g").data(graph.nodes).join("g")
  .attr("cursor", "grab")
  .call(d3.drag()
    .on("start", (e, d) => {{ if (!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; }})
    .on("drag",  (e, d) => {{ d.fx=e.x; d.fy=e.y; }})
    .on("end",   (e, d) => {{ if (!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }}));

const color0 = "#4f8ef7", color1 = "#f7634f";

// Glow circles
nodeG.append("circle")
  .attr("r", d => 6 + Math.sqrt(d.area) * 0.7)
  .attr("fill", d => d.partition === 0 ? "rgba(79,142,247,.12)" : "rgba(247,99,79,.12)")
  .attr("stroke", "none");

// Main circles
nodeG.append("circle")
  .attr("r", d => 4 + Math.sqrt(d.area) * 0.5)
  .attr("fill", d => d.partition === 0 ? "rgba(79,142,247,.25)" : "rgba(247,99,79,.25)")
  .attr("stroke", d => d.partition === 0 ? color0 : color1)
  .attr("stroke-width", 2);

// Labels
nodeG.append("text")
  .text(d => d.id)
  .attr("text-anchor", "middle")
  .attr("dy", "0.35em")
  .attr("fill", "#e2e8f0")
  .attr("font-size", "11px")
  .attr("font-family", "JetBrains Mono, monospace")
  .attr("font-weight", "600")
  .attr("pointer-events", "none");

// Tooltip
nodeG.on("mouseenter", (e, d) => {{
  tip.innerHTML = `<b style="color:${{d.partition===0?color0:color1}}">${{d.id}}</b><br>
    Partition ${{d.partition}}<br>
    Area: ${{d.area.toFixed(1)}}`;
  tip.classList.add("show");
}}).on("mousemove", e => {{
  tip.style.left = (e.clientX + 14) + "px";
  tip.style.top  = (e.clientY - 10) + "px";
}}).on("mouseleave", () => tip.classList.remove("show"));

sim.on("tick", () => {{
  link
    .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  nodeG.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
}});

window.addEventListener("resize", () => {{
  [W, H] = resize();
  sim.force("x", d3.forceX(d => d.partition === 0 ? W*0.27 : W*0.73).strength(0.18));
  sim.force("y", d3.forceY(H/2).strength(0.05));
  sim.alpha(0.3).restart();
}});
</script>
</body>
</html>"""

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html",
                                     mode="w", encoding="utf-8")
    tmp.write(html)
    tmp.close()
    print(f"\n[GUI] Opening visualizer: {tmp.name}")
    webbrowser.open(f"file:///{tmp.name.replace(os.sep, '/')}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    gui     = "--gui"     in args
    args    = [a for a in args if not a.startswith("--")]

    if len(args) < 2:
        print("Usage: python vlsi_kahypar_pipeline.py <circuit>.net <circuit>.are [--gui] [--dry-run]")
        sys.exit(1)

    net_path, are_path = args[0], args[1]
    hgr_path, k, epsilon = "output.hgr", 2, 0.03

    print("\n" + "=" * 60)
    print("  VLSI → KaHyPar PIPELINE")
    print("=" * 60 + "\n")

    nets, net_names   = parse_net_file(net_path)
    areas             = parse_are_file(are_path)
    node_list         = convert_to_hgr(nets, areas, hgr_path)

    run_partitioner(nets, node_list, areas, hgr_path,
                    k=k, epsilon=epsilon, dry_run=dry_run)

    partitions = read_partition_file(hgr_path, k=k, epsilon=epsilon)
    node_map, cut_nets, cut_net_indices, area_balance, partition_areas = \
        compute_metrics(nets, node_list, partitions, areas)

    print_results(node_map, cut_nets, area_balance, partition_areas)

    if gui:
        launch_gui(nets, net_names, node_list, node_map, areas,
                   cut_net_indices, partition_areas, area_balance)

if __name__ == "__main__":
    main()
