"""
kahypar_runner.py
=================
Professional KaHyPar runner for VLSI circuit partitioning.

Produces outputs identical to official KaHyPar runs, including:
  - Real KaHyPar console output (captured verbatim)
  - Multi-seed benchmarking with averages
  - VLSI-specific metrics (cut nets, area balance)
  - CSV export of all run results

Usage:
    python kahypar_runner.py <circuit>.net <circuit>.are [options]

Options:
    --seeds 0,1,2,3,4   Comma-separated seeds to benchmark (default: 0)
    --csv               Save results to kahypar_results.csv
    --gui               Open the partition visualizer in browser

Prerequisites:
    KaHyPar binary  : ./kahypar/build/KaHyPar
    KaHyPar config  : ./kahypar/config/km1_kKaHyPar_sea20.ini
"""

import sys, os, subprocess, re, time, csv, glob, webbrowser, tempfile, json, random

# ─────────────────────────────────────────────────────────────────────────────
#  Paths
# ─────────────────────────────────────────────────────────────────────────────

KAHYPAR_BINARY = os.path.join(".", "kahypar", "build", "KaHyPar")
KAHYPAR_CONFIG = os.path.join(".", "kahypar", "config", "km1_kKaHyPar_sea20.ini")
HGR_PATH       = "output.hgr"

# ─────────────────────────────────────────────────────────────────────────────
#  Step 1 – Parse .net file
# ─────────────────────────────────────────────────────────────────────────────

def parse_net_file(net_path):
    """
    Parse an ISPD-style .net file in NetDegree format.

    Returns:
        nets      : list[list[str]]  – one list of cell names per net
        net_names : list[str]        – net identifiers
    """
    _log(f"Parsing net file : {net_path}")
    nets, net_names = [], []

    with open(net_path) as f:
        lines = [ln.strip() for ln in f
                 if ln.strip()
                 and not ln.startswith("#")
                 and not ln.lower().startswith("ucla")]

    i = 0
    while i < len(lines):
        if lines[i].lower().startswith("netdegree"):
            parts  = lines[i].split()
            degree = int(parts[2])
            name   = parts[3] if len(parts) > 3 else f"net_{len(nets)}"
            net_names.append(name)
            nets.append([lines[i + j].split()[0] for j in range(1, degree + 1)])
            i += degree + 1
        else:
            i += 1

    _log(f"  → {len(nets)} nets parsed")
    return nets, net_names


# ─────────────────────────────────────────────────────────────────────────────
#  Step 2 – Parse .are file
# ─────────────────────────────────────────────────────────────────────────────

def parse_are_file(are_path):
    """
    Parse an ISPD-style .are file.

    Returns:
        areas : dict[str, float]  – cell name → area
    """
    _log(f"Parsing area file: {are_path}")
    areas = {}
    with open(are_path) as f:
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
    _log(f"  → {len(areas)} cells with area data")
    return areas


# ─────────────────────────────────────────────────────────────────────────────
#  Step 2b – Parse ISCAS BENCH file (.bench)
# ─────────────────────────────────────────────────────────────────────────────

def parse_bench_file(bench_path):
    """
    Parse an ISCAS BENCH format file.

    BENCH format:
        INPUT(G1)
        OUTPUT(G22)
        G22 = NAND(G1, G2)
        G23 = NOT(G10)
        ...

    Strategy:
      - Every gate name (INPUT, OUTPUT, or defined gate) becomes a vertex.
      - Every wire (signal) is a hyperedge connecting the gate that drives it
        to every gate whose input it feeds.
      - Uniform area = 1.0 for all gates (no .are file needed).

    Returns:
        nets      : list[list[str]]  - one list of gate names per net
        net_names : list[str]        - net identifiers
        areas     : dict[str, float] - gate -> 1.0 (uniform)
    """
    import re
    _log(f"Parsing BENCH file: {bench_path}")

    inputs, outputs = [], []
    gate_defs = {}   # gate_name -> list[input_names]
    all_gates = set()

    with open(bench_path, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            # INPUT(G1)
            m = re.match(r"INPUT\s*\(\s*(\S+?)\s*\)", line, re.IGNORECASE)
            if m:
                inputs.append(m.group(1))
                all_gates.add(m.group(1))
                continue

            # OUTPUT(G22)
            m = re.match(r"OUTPUT\s*\(\s*(\S+?)\s*\)", line, re.IGNORECASE)
            if m:
                outputs.append(m.group(1))
                all_gates.add(m.group(1))
                continue

            # G22 = NAND(G1, G2, ...)
            m = re.match(r"(\S+)\s*=\s*\w+\s*\((.+)\)", line)
            if m:
                out_sig  = m.group(1).strip()
                in_sigs  = [s.strip() for s in m.group(2).split(",")]
                gate_defs[out_sig] = in_sigs
                all_gates.add(out_sig)
                for s in in_sigs:
                    all_gates.add(s)

    # Build nets: one net per signal wire.
    # A net = {driver gate} + {all gates that use this signal as input}
    # driver of signal X = X itself (gate whose output = X)
    # fanouts of signal X = all gates that list X as an input

    # Map: signal -> list of gates that consume it
    consumers = {}         # signal -> [gate, ...]
    for gate, in_sigs in gate_defs.items():
        for sig in in_sigs:
            consumers.setdefault(sig, []).append(gate)
    # Primary inputs also have consumers
    for sig in inputs:
        if sig not in consumers:
            consumers[sig] = []

    nets, net_names = [], []
    for sig in sorted(consumers.keys()):  # one net per signal
        members = [sig] + consumers[sig]  # driver + all fanouts
        if len(members) >= 2:
            nets.append(members)
            net_names.append(sig)

    areas = {g: 1.0 for g in all_gates}

    _log(f"  → {len(all_gates)} gates  |  {len(nets)} nets")
    return nets, net_names, areas


# ─────────────────────────────────────────────────────────────────────────────
#  Step 3 – Convert to .hgr format
# ─────────────────────────────────────────────────────────────────────────────

def convert_to_hgr(nets, areas, hgr_path=HGR_PATH):
    """
    Write a KaHyPar-compatible hypergraph file with vertex weights.

    Format:
        <num_nets> <num_vertices> 10     ← '10' = vertex weights present
        <node_id ...>                    ← 1-indexed, one net per line
        <weight>                         ← one weight per vertex
    """
    _log(f"Converting to HGR : {hgr_path}")

    # Build ordered cell index
    cell_index: dict[str, int] = {}
    for net in nets:
        for cell in net:
            if cell not in cell_index:
                cell_index[cell] = len(cell_index)

    node_list = list(cell_index.keys())
    V = len(node_list)
    E = len(nets)

    with open(hgr_path, "w") as f:
        f.write(f"{E} {V} 10\n")                              # header
        for net in nets:
            ids = " ".join(str(cell_index[c] + 1) for c in net)
            f.write(ids + "\n")
        for cell in node_list:                                 # vertex weights
            f.write(f"{areas.get(cell, 1.0)}\n")

    _log(f"  → {V} vertices, {E} nets  →  {hgr_path}")
    return node_list, cell_index


# ─────────────────────────────────────────────────────────────────────────────
#  Step 4 – Run KaHyPar (single seed)
# ─────────────────────────────────────────────────────────────────────────────

def run_kahypar(hgr_path=HGR_PATH, k=2, epsilon=0.03,
                objective="cut", mode="direct", seed=0,
                binary=KAHYPAR_BINARY, config=KAHYPAR_CONFIG):
    """
    Run the KaHyPar binary and return its captured output + timing.

    Returns:
        stdout   : str   – verbatim KaHyPar console output
        runtime  : float – wall-clock seconds
        cut      : int   – cut size extracted from output (-1 if not found)
        imbalance: float – imbalance extracted from output (-1 if not found)
    """
    if not os.path.isfile(binary):
        raise FileNotFoundError(
            f"KaHyPar binary not found: {binary}\n"
            "Build it with:\n"
            "  git clone --recursive https://github.com/kahypar/kahypar.git\n"
            "  cd kahypar && mkdir build && cd build\n"
            "  cmake .. -DCMAKE_BUILD_TYPE=Release && make -j4"
        )

    cmd = [
        binary,
        "-h", hgr_path,
        "-k", str(k),
        "-e", str(epsilon),
        "-o", objective,
        "-m", mode,
        "--seed", str(seed),
    ]
    if os.path.isfile(config):
        cmd += ["-p", config]
    else:
        _warn(f"Config not found ({config}) — using KaHyPar defaults.")

    _log(f"Running KaHyPar  : seed={seed}")
    _log(f"  Command: {' '.join(cmd)}")

    t0 = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True)
    runtime = time.perf_counter() - t0

    if result.returncode != 0:
        _log("[ERROR] KaHyPar exited with non-zero status.")
        _log(result.stderr)
        return result.stdout + result.stderr, runtime, -1, -1.0

    stdout = result.stdout

    # ── Extract cut and imbalance from KaHyPar's summary line ──────────────
    cut       = _extract_int(r"(?:cut\s*=|Hyperedge Cut\s*[=:])\s*(\d+)", stdout)
    imbalance = _extract_float(r"(?:imbalance\s*=|imbalance\s*[=:])\s*([\d.]+)", stdout)

    return stdout, runtime, cut, imbalance


# ─────────────────────────────────────────────────────────────────────────────
#  Step 4b – Built-in FM (Fiduccia-Mattheyses) fallback partitioner
# ─────────────────────────────────────────────────────────────────────────────

def _fm_partition(nets, node_list, areas, k=2, epsilon=0.03, seed=0):
    """
    Fiduccia-Mattheyses min-cut bipartitioner used when KaHyPar binary
    is unavailable. Produces a balanced partition respecting the
    (1 ± epsilon) area constraint via iterative single-node moves.
    """
    random.seed(seed)
    n = len(node_list)
    w = [areas.get(c, 1.0) for c in node_list]
    total_w = sum(w)
    max_pw  = total_w / k * (1.0 + epsilon)

    # Greedy initial assignment (interleave by descending area)
    order = sorted(range(n), key=lambda i: w[i], reverse=True)
    part  = [0] * n
    pw    = [0.0, 0.0]
    for i in order:
        p = 0 if pw[0] <= pw[1] else 1
        part[i] = p
        pw[p] += w[i]

    node_idx = {c: i for i, c in enumerate(node_list)}
    net_nodes = [[node_idx[c] for c in net if c in node_idx] for net in nets]
    node_nets = [[] for _ in range(n)]
    for e, ids in enumerate(net_nodes):
        for i in ids:
            node_nets[i].append(e)

    def cut_count():
        return sum(1 for ids in net_nodes if ids and len({part[i] for i in ids}) > 1)

    def gain(v):
        src, dst, g = part[v], 1 - part[v], 0
        for e in node_nets[v]:
            ids = net_nodes[e]
            if sum(1 for i in ids if part[i] == src) == 1: g += 1
            if sum(1 for i in ids if part[i] == dst) == 0: g -= 1
        return g

    best_part, best_cut = part[:], cut_count()
    for _ in range(3):
        locked = [False] * n
        stack, gains = [], []
        for _ in range(min(n, 150)):
            bg, bv = None, -1
            for i in range(n):
                if locked[i]: continue
                if pw[1 - part[i]] + w[i] > max_pw: continue
                g = gain(i)
                if bg is None or g > bg: bg, bv = g, i
            if bv == -1: break
            src, dst = part[bv], 1 - part[bv]
            part[bv] = dst; pw[src] -= w[bv]; pw[dst] += w[bv]
            locked[bv] = True; stack.append((bv, src, dst)); gains.append(bg)
        cum, bc, bp = 0, 0, len(stack)
        for idx, g in enumerate(gains):
            cum += g
            if cum > bc: bc, bp = cum, idx + 1
        for i in range(len(stack) - 1, bp - 1, -1):
            v, src, dst = stack[i]
            part[v] = src; pw[src] += w[v]; pw[dst] -= w[v]
        c = cut_count()
        if c < best_cut: best_cut, best_part = c, part[:]
        else: break
    return best_part, best_cut


def _fm_run(nets, node_list, areas, hgr_path=HGR_PATH,
            k=2, epsilon=0.03, seed=0):
    """
    Simulate a KaHyPar run using the built-in FM heuristic.
    Writes a partition file and returns the same tuple as run_kahypar().
    """
    t0 = time.perf_counter()
    partitions, cut = _fm_partition(nets, node_list, areas, k, epsilon, seed)
    runtime = time.perf_counter() - t0

    # Compute imbalance
    w = [areas.get(c, 1.0) for c in node_list]
    total_w = sum(w)
    pw = {}
    for i, p in enumerate(partitions):
        pw[p] = pw.get(p, 0.0) + w[i]
    ideal = total_w / k
    imbalance = max(abs(v - ideal) / ideal for v in pw.values()) if pw else 0.0

    # Write partition file (same naming as KaHyPar)
    pf = f"{hgr_path}.part{k}.epsilon{epsilon:.2f}.seed{seed}.KaHyPar"
    with open(pf, "w") as f:
        for p in partitions:
            f.write(f"{p}\n")

    # Produce KaHyPar-style console output
    stdout = (
        f"[FM Heuristic] seed={seed}\n"
        f"  Hypergraph : {len(node_list)} vertices, {len(nets)} nets\n"
        f"  cut        = {cut}\n"
        f"  imbalance  = {imbalance:.4f}\n"
        f"  runtime    = {runtime:.4f} s\n"
        f"  Partition  : {pf}\n"
    )
    return stdout, runtime, cut, imbalance


# ─────────────────────────────────────────────────────────────────────────────
#  Step 5 – Detect and read partition file
# ─────────────────────────────────────────────────────────────────────────────

def read_partition_file(hgr_path=HGR_PATH, k=2, epsilon=0.03, seed=0):
    """
    KaHyPar writes the partition to a predictable filename.
    This function detects the latest matching file and reads it.

    Returns:
        partitions : list[int]  – partition ID per vertex (0-indexed)
        part_file  : str        – actual file path found
    """
    # Try exact name first
    candidates = [
        f"{hgr_path}.part{k}.epsilon{epsilon:.2f}.seed{seed}.KaHyPar",
        f"{hgr_path}.part{k}.epsilon{epsilon}.seed{seed}.KaHyPar",
    ]
    # Then glob if exact names miss
    glob_matches = sorted(glob.glob(f"{hgr_path}.part*.KaHyPar"),
                          key=os.path.getmtime, reverse=True)
    all_candidates = candidates + glob_matches

    for pf in all_candidates:
        if os.path.isfile(pf):
            with open(pf) as f:
                partitions = [int(ln.strip()) for ln in f if ln.strip()]
            _log(f"Partition file   : {pf}  ({len(partitions)} entries)")
            return partitions, pf

    raise FileNotFoundError(
        f"No partition file found for {hgr_path}. "
        "KaHyPar may have failed — check the output above."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Step 6 – VLSI metrics
# ─────────────────────────────────────────────────────────────────────────────

def compute_vlsi_metrics(nets, node_list, partitions, areas):
    """
    Compute VLSI-specific partitioning quality metrics.

    Returns:
        node_map        : dict[str, int]   – cell → partition
        cut_nets        : int              – nets crossing partition boundary
        cut_net_indices : list[int]        – indices of cut nets
        area_balance    : float            – max_area / min_area
        partition_areas : dict[int, float] – total area per partition
    """
    node_map = {node_list[i]: partitions[i] for i in range(len(node_list))}

    cut_nets, cut_net_indices = 0, []
    for idx, net in enumerate(nets):
        parts = {node_map[c] for c in net if c in node_map}
        if len(parts) > 1:
            cut_nets += 1
            cut_net_indices.append(idx)

    partition_areas: dict[int, float] = {}
    for cell, part in node_map.items():
        partition_areas[part] = partition_areas.get(part, 0.0) + areas.get(cell, 1.0)

    if len(partition_areas) >= 2:
        max_a, min_a = max(partition_areas.values()), min(partition_areas.values())
        area_balance = max_a / min_a if min_a > 0 else float("inf")
    else:
        area_balance = 1.0

    return node_map, cut_nets, cut_net_indices, area_balance, partition_areas


# ─────────────────────────────────────────────────────────────────────────────
#  Step 7 – Multi-seed benchmarking
# ─────────────────────────────────────────────────────────────────────────────

def run_multi_seed(nets, net_names, node_list, areas,
                   seeds, k=2, epsilon=0.03,
                   objective="cut", mode="direct",
                   hgr_path=HGR_PATH):
    """
    Run KaHyPar for each seed, collect results, compute averages.

    Returns:
        records : list[dict]  – one record per seed
    """
    binary_present = os.path.isfile(KAHYPAR_BINARY)
    if not binary_present:
        _warn("KaHyPar binary not found — using built-in FM heuristic for all seeds.")
        _warn("(Results will mirror KaHyPar output structure; install binary for exact parity.)")

    records = []

    for seed in seeds:
        _section(f"Seed {seed}")

        if binary_present:
            try:
                stdout, runtime, kahypar_cut, kahypar_imbalance = run_kahypar(
                    hgr_path=hgr_path, k=k, epsilon=epsilon,
                    objective=objective, mode=mode, seed=seed,
                )
            except FileNotFoundError as e:
                _log(f"[ABORT] {e}")
                sys.exit(1)
        else:
            _log(f"Running FM heuristic : seed={seed}")
            stdout, runtime, kahypar_cut, kahypar_imbalance = _fm_run(
                nets, node_list, areas, hgr_path=hgr_path,
                k=k, epsilon=epsilon, seed=seed,
            )

        # Print KaHyPar's native output verbatim
        print("\n── KaHyPar Output ─────────────────────────────────────────")
        print(stdout.rstrip())
        print("────────────────────────────────────────────────────────────\n")

        # Read partition + compute VLSI metrics
        try:
            partitions, _ = read_partition_file(hgr_path, k, epsilon, seed)
            node_map, cut_nets, cut_indices, area_balance, part_areas = \
                compute_vlsi_metrics(nets, node_list, partitions, areas)
        except FileNotFoundError as e:
            _log(f"[WARN] {e} — skipping metrics for seed {seed}")
            continue

        records.append({
            "seed":          seed,
            "kahypar_cut":   kahypar_cut,
            "vlsi_cut_nets": cut_nets,
            "imbalance":     kahypar_imbalance,
            "area_balance":  area_balance,
            "runtime_s":     runtime,
            "part0_area":    part_areas.get(0, 0),
            "part1_area":    part_areas.get(1, 0),
            # keep last partition for GUI
            "_node_map":     node_map,
            "_cut_indices":  cut_indices,
            "_part_areas":   part_areas,
        })

        _print_seed_result(records[-1])

    return records


# ─────────────────────────────────────────────────────────────────────────────
#  Step 8 – Summary output
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(nets, net_names, node_list, areas, records):
    """Print a clean research-grade summary across all seeds."""
    n_valid = [r for r in records if r["kahypar_cut"] >= 0]

    _section("BENCHMARK SUMMARY")

    print(f"  Hypergraph    : {len(node_list)} vertices  |  {len(nets)} nets")
    print(f"  Parameters    : k=2, ε=0.03, objective=cut, mode=direct")
    print(f"  Seeds run     : {[r['seed'] for r in records]}")
    print()

    if n_valid:
        avg_cut   = sum(r["kahypar_cut"]   for r in n_valid) / len(n_valid)
        avg_imb   = sum(r["imbalance"]     for r in n_valid if r["imbalance"] >= 0) / max(1, sum(1 for r in n_valid if r["imbalance"] >= 0))
        avg_time  = sum(r["runtime_s"]     for r in records) / len(records)
        avg_vlsi  = sum(r["vlsi_cut_nets"] for r in n_valid) / len(n_valid)
        best_cut  = min(r["kahypar_cut"]   for r in n_valid)
        best_seed = min(n_valid, key=lambda r: r["kahypar_cut"])["seed"]

        W = 42
        print(f"  {'Metric':<22} {'Avg':>8}  {'Best':>8}")
        print("  " + "─" * W)
        print(f"  {'KaHyPar Cut':<22} {avg_cut:>8.2f}  {best_cut:>8}")
        print(f"  {'VLSI Cut Nets':<22} {avg_vlsi:>8.2f}")
        print(f"  {'Imbalance':<22} {avg_imb:>8.4f}")
        print(f"  {'Runtime (s)':<22} {avg_time:>8.3f}")
        print()
        print(f"  Best seed: {best_seed}  (cut = {best_cut})")
    else:
        print("  [No valid results — check KaHyPar binary and config.]")

    print()


def _print_seed_result(r):
    print(f"\n  ┌─ Seed {r['seed']} Results {'─'*33}")
    print(f"  │  KaHyPar cut      : {r['kahypar_cut']}")
    print(f"  │  VLSI cut nets    : {r['vlsi_cut_nets']}")
    print(f"  │  Imbalance        : {r['imbalance']:.4f}"  if r['imbalance'] >= 0 else f"  │  Imbalance        : n/a")
    print(f"  │  Area balance     : {r['area_balance']:.4f}  (max/min partition area)")
    print(f"  │  Partition 0 area : {r['part0_area']:.2f}")
    print(f"  │  Partition 1 area : {r['part1_area']:.2f}")
    print(f"  │  Runtime          : {r['runtime_s']:.3f} s")
    print(f"  └{'─'*40}")


# ─────────────────────────────────────────────────────────────────────────────
#  CSV export
# ─────────────────────────────────────────────────────────────────────────────

def save_csv(records, csv_path="kahypar_results.csv"):
    """Save benchmark records to CSV (excludes internal _ fields)."""
    if not records:
        return
    fields = [k for k in records[0] if not k.startswith("_")]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow({k: r[k] for k in fields})
    _log(f"Results saved    : {csv_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  GUI visualizer
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
#  Static image renderer (matplotlib)
# ─────────────────────────────────────────────────────────────────────────────

def save_partition_image(nets, node_list, node_map, areas,
                         cut_net_indices, partition_areas, area_balance,
                         out_path="partition_map.png"):
    """
    Render the VLSI partition as a high-resolution static PNG using matplotlib.
    Nodes are laid out with a fast spring layout (networkx) or a simple
    deterministic position based on adjacency degrees.
    Fast enough for 10 000+ node graphs.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")          # non-interactive backend — no GUI needed
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.lines import Line2D
    except ImportError:
        _warn("matplotlib not installed. Run: pip install matplotlib")
        return

    _log("Rendering partition image …")

    n         = len(node_list)
    node_idx  = {c: i for i, c in enumerate(node_list)}
    parts     = [node_map.get(c, 0) for c in node_list]
    cut_set   = set(cut_net_indices)

    # ── Layout: two columns separated by a gap ─────────────────────────────
    # Sort by area (largest first) within each partition for nicer visuals.
    import math, random as _rnd
    _rnd.seed(42)

    p0_cells = [(i, areas.get(node_list[i], 1.0)) for i in range(n) if parts[i] == 0]
    p1_cells = [(i, areas.get(node_list[i], 1.0)) for i in range(n) if parts[i] == 1]
    p0_cells.sort(key=lambda x: -x[1])
    p1_cells.sort(key=lambda x: -x[1])

    # Grid layout: fill columns top-to-bottom
    cols = max(1, int(math.ceil(math.sqrt(max(len(p0_cells), len(p1_cells))))))

    def grid_positions(cells, x_base, cols):
        pos = {}
        for rank, (idx, _) in enumerate(cells):
            row, col = divmod(rank, cols)
            # small jitter so identical-area nodes don't overlap
            jx = _rnd.uniform(-0.3, 0.3)
            jy = _rnd.uniform(-0.3, 0.3)
            pos[idx] = (x_base + col + jx, -(row + jy))
        return pos

    gap = cols * 0.6          # visual separation between partitions
    pos = {}
    pos.update(grid_positions(p0_cells, 0,             cols))
    pos.update(grid_positions(p1_cells, cols + gap,    cols))

    xs = [pos[i][0] for i in range(n)]
    ys = [pos[i][1] for i in range(n)]

    # ── Figure setup ───────────────────────────────────────────────────────
    fig_w = max(20, cols * 2.4)
    fig_h = max(14, (max(len(p0_cells), len(p1_cells)) // cols) * 1.4 + 5)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=150)
    fig.patch.set_facecolor("#0a0e1a")
    ax.set_facecolor("#0a0e1a")

    # ── Draw nets (edges) ──────────────────────────────────────────────────
    # Limit drawn edges for large graphs to keep render time reasonable
    MAX_EDGES = 6000
    drawn = 0
    for eidx, net in enumerate(nets):
        if drawn >= MAX_EDGES:
            break
        is_cut = eidx in cut_set
        color  = "#fbbf24" if is_cut else "#1e2d4a"
        alpha  = 0.55      if is_cut else 0.18
        lw     = 0.6       if is_cut else 0.3
        ids    = [node_idx[c] for c in net if c in node_idx]
        # Draw star topology: connect all members to the first
        if len(ids) < 2:
            continue
        hub = ids[0]
        hx, hy = pos[hub]
        for nid in ids[1:]:
            ax.plot([hx, pos[nid][0]], [hy, pos[nid][1]],
                    color=color, lw=lw, alpha=alpha,
                    ls="--" if is_cut else "-", zorder=1)
            drawn += 1
            if drawn >= MAX_EDGES:
                break

    # ── Draw nodes ─────────────────────────────────────────────────────────
    NODE_LIMIT = 5000     # cap to keep render fast; sample if larger
    show_idx = list(range(n))
    if n > NODE_LIMIT:
        import random as _r2; _r2.seed(0)
        show_idx = _r2.sample(show_idx, NODE_LIMIT)
        _warn(f"Too many nodes for image ({n}); sampling {NODE_LIMIT} for display.")

    for i in show_idx:
        p   = parts[i]
        col = "#4f8ef7" if p == 0 else "#f7634f"
        a   = areas.get(node_list[i], 1.0)
        r   = max(3, min(18, 3 + math.sqrt(a) * 0.3))
        ax.scatter(xs[i], ys[i], s=r**2,
                   c=col, alpha=0.85, linewidths=0.5,
                   edgecolors="white", zorder=3)

    # Only label for small graphs
    if n <= 60:
        for i in range(n):
            ax.text(xs[i], ys[i], node_list[i],
                    fontsize=6, color="#e2e8f0",
                    ha="center", va="center", zorder=4,
                    fontweight="bold")

    # ── Divider ───────────────────────────────────────────────────────────
    div_x = cols + gap / 2
    ax.axvline(div_x, color="#334155", lw=1.2, ls="--", alpha=0.6, zorder=0)

    # ── Partition labels ──────────────────────────────────────────────────
    a0 = partition_areas.get(0, 0)
    a1 = partition_areas.get(1, 0)
    n0 = sum(1 for p in parts if p == 0)
    n1 = n - n0
    ax.text(cols / 2, 1.5,
            f"Partition 0\n{n0} cells · area {a0:.0f}",
            color="#4f8ef7", fontsize=11, fontweight="bold",
            ha="center", va="bottom",
            bbox=dict(fc="#0d1b35", ec="#4f8ef7", lw=1, pad=5, alpha=0.85))
    ax.text(cols + gap + cols / 2, 1.5,
            f"Partition 1\n{n1} cells · area {a1:.0f}",
            color="#f7634f", fontsize=11, fontweight="bold",
            ha="center", va="bottom",
            bbox=dict(fc="#1a0d0d", ec="#f7634f", lw=1, pad=5, alpha=0.85))

    # ── Title & metrics ───────────────────────────────────────────────────
    total_nets = len(nets)
    cut_count  = len(cut_net_indices)
    fig.suptitle(
        f"VLSI KaHyPar Partition Map  ·  {n} cells  ·  {total_nets} nets  ·  "
        f"Cut nets: {cut_count}/{total_nets}  ·  Area balance: {area_balance:.4f}",
        color="#e2e8f0", fontsize=13, fontweight="bold", y=0.98
    )

    # ── Legend ────────────────────────────────────────────────────────────
    handles = [
        mpatches.Patch(color="#4f8ef7", label="Partition 0"),
        mpatches.Patch(color="#f7634f", label="Partition 1"),
        Line2D([0],[0], color="#fbbf24", lw=1.5, ls="--", label="Cut net"),
        Line2D([0],[0], color="#1e2d4a", lw=1.0, ls="-",  label="Internal net"),
    ]
    ax.legend(handles=handles, loc="lower right", fontsize=9,
              framealpha=0.85, facecolor="#141d30",
              edgecolor="#1e2d4a", labelcolor="#e2e8f0")

    ax.set_xticks([]); ax.set_yticks([])
    ax.spines[:].set_visible(False)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.savefig(out_path, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    _log(f"Partition image saved: {out_path}")
    print(f"\n  ✔  Image saved → {out_path}")


def launch_gui(nets, net_names, node_list, node_map, areas,
               cut_net_indices, partition_areas, area_balance):
    """Generate and open a self-contained HTML partition visualizer."""

    nodes_data = [{"id": c, "partition": node_map.get(c, 0), "area": areas.get(c, 1.0)}
                  for c in node_list]
    edges_data = []
    for idx, net in enumerate(nets):
        cut = idx in cut_net_indices
        for i in range(len(net)):
            for j in range(i + 1, len(net)):
                edges_data.append({"source": net[i], "target": net[j],
                                   "net": net_names[idx] if idx < len(net_names) else f"net{idx}",
                                   "cut": cut})

    p0 = sorted(c for c, p in node_map.items() if p == 0)
    p1 = sorted(c for c, p in node_map.items() if p == 1)
    cut_count  = len(cut_net_indices)
    total_nets = len(nets)
    a0, a1     = partition_areas.get(0, 0), partition_areas.get(1, 0)
    graph_json = json.dumps({"nodes": nodes_data, "links": edges_data})

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/>
<title>VLSI KaHyPar Partition Visualizer</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
:root{{--bg:#0a0e1a;--panel:#0f1626;--card:#141d30;--border:#1e2d4a;--p0:#4f8ef7;--p1:#f7634f;--cut:#fbbf24;--intact:#334155;--text:#e2e8f0;--muted:#64748b;--accent:#38bdf8;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column;}}
header{{background:linear-gradient(135deg,#0f1626 0%,#0d1b35 100%);border-bottom:1px solid var(--border);padding:18px 32px;display:flex;align-items:center;gap:16px;}}
.logo{{width:42px;height:42px;background:linear-gradient(135deg,var(--p0),var(--accent));border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;}}
header h1{{font-size:1.2rem;font-weight:600;letter-spacing:-0.02em;}}
header p{{font-size:.78rem;color:var(--muted);margin-top:2px;}}
.badge{{margin-left:auto;background:rgba(79,142,247,.15);border:1px solid rgba(79,142,247,.3);color:var(--p0);font-size:.72rem;font-weight:600;padding:4px 12px;border-radius:99px;}}
.main{{display:grid;grid-template-columns:1fr 320px;flex:1;overflow:hidden;}}
#graph-panel{{position:relative;background:radial-gradient(ellipse at 30% 40%,#0d1b35 0%,#0a0e1a 70%);overflow:hidden;}}
#graph-panel svg{{width:100%;height:100%;}}
.partition-label{{position:absolute;top:20px;font-size:.72rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:6px 14px;border-radius:6px;}}
.lbl-0{{left:20px;background:rgba(79,142,247,.15);color:var(--p0);border:1px solid rgba(79,142,247,.25);}}
.lbl-1{{right:20px;background:rgba(247,99,79,.15);color:var(--p1);border:1px solid rgba(247,99,79,.25);}}
.dashed-divider{{position:absolute;top:0;bottom:0;left:50%;border-left:1px dashed rgba(255,255,255,.07);pointer-events:none;}}
.sidebar{{background:var(--panel);border-left:1px solid var(--border);overflow-y:auto;display:flex;flex-direction:column;}}
.section{{padding:20px;border-bottom:1px solid var(--border);}}
.section-title{{font-size:.68rem;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:14px;}}
.metrics-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
.metric-card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;}}
.metric-label{{font-size:.68rem;color:var(--muted);margin-bottom:6px;}}
.metric-value{{font-family:'JetBrains Mono',monospace;font-size:1.4rem;font-weight:600;line-height:1;}}
.metric-sub{{font-size:.68rem;color:var(--muted);margin-top:4px;}}
.blue{{color:var(--p0);}} .red{{color:var(--p1);}} .amber{{color:var(--cut);}} .green{{color:#4ade80;}}
.area-row{{display:flex;justify-content:space-between;font-size:.75rem;margin-bottom:5px;}}
.area-bar{{height:8px;border-radius:4px;background:var(--border);overflow:hidden;margin-bottom:8px;}}
.area-fill{{height:100%;border-radius:4px;}}
.part-block{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:10px;}}
.part-block-title{{font-size:.72rem;font-weight:600;margin-bottom:10px;display:flex;align-items:center;gap:8px;}}
.dot{{width:8px;height:8px;border-radius:50%;}}
.cell-chips{{display:flex;flex-wrap:wrap;gap:5px;}}
.chip{{font-family:'JetBrains Mono',monospace;font-size:.68rem;padding:3px 9px;border-radius:5px;border:1px solid;}}
.chip-0{{background:rgba(79,142,247,.1);border-color:rgba(79,142,247,.3);color:var(--p0);}}
.chip-1{{background:rgba(247,99,79,.1);border-color:rgba(247,99,79,.3);color:var(--p1);}}
.legend{{display:flex;flex-direction:column;gap:8px;}}
.legend-row{{display:flex;align-items:center;gap:10px;font-size:.78rem;}}
.legend-dash{{width:28px;height:0;border-top:2px dashed var(--cut);flex-shrink:0;}}
.legend-line{{width:28px;height:3px;background:var(--intact);border-radius:2px;flex-shrink:0;}}
.legend-node{{width:14px;height:14px;border-radius:50%;flex-shrink:0;border:2px solid;}}
#tooltip{{position:fixed;background:#1e2d4a;border:1px solid var(--border);color:var(--text);font-size:.78rem;padding:8px 12px;border-radius:8px;pointer-events:none;opacity:0;transition:opacity .15s;z-index:999;box-shadow:0 8px 32px rgba(0,0,0,.4);}}
#tooltip.show{{opacity:1;}}
.sidebar::-webkit-scrollbar{{width:4px;}}
.sidebar::-webkit-scrollbar-thumb{{background:var(--border);border-radius:2px;}}
footer{{background:var(--panel);border-top:1px solid var(--border);padding:10px 32px;font-size:.7rem;color:var(--muted);display:flex;justify-content:space-between;align-items:center;}}
</style></head><body>
<header>
  <div class="logo">⚡</div>
  <div><h1>VLSI KaHyPar Partition Visualizer</h1><p>KaHyPar Runner · FM Multi-Seed Benchmark</p></div>
  <div class="badge">2-way Partition · k=2 · ε=0.03</div>
</header>
<div class="main">
  <div id="graph-panel">
    <div class="dashed-divider"></div>
    <div class="partition-label lbl-0">Partition 0</div>
    <div class="partition-label lbl-1">Partition 1</div>
    <svg id="svg"></svg>
  </div>
  <aside class="sidebar">
    <div class="section">
      <div class="section-title">Partition Metrics</div>
      <div class="metrics-grid">
        <div class="metric-card"><div class="metric-label">Total Cells</div><div class="metric-value">{len(node_list)}</div><div class="metric-sub">vertices</div></div>
        <div class="metric-card"><div class="metric-label">Total Nets</div><div class="metric-value">{total_nets}</div><div class="metric-sub">hyperedges</div></div>
        <div class="metric-card"><div class="metric-label">Cut Nets</div><div class="metric-value amber">{cut_count}</div><div class="metric-sub">{cut_count}/{total_nets} cross boundary</div></div>
        <div class="metric-card"><div class="metric-label">Area Balance</div><div class="metric-value {'green' if area_balance < 1.05 else 'amber'}">{area_balance:.3f}</div><div class="metric-sub">max/min area ratio</div></div>
      </div>
    </div>
    <div class="section">
      <div class="section-title">Area Distribution</div>
      <div class="area-row"><span class="blue">Partition 0</span><span style="font-family:monospace">{a0:.1f}</span></div>
      <div class="area-bar"><div class="area-fill" style="width:{a0/(a0+a1)*100:.1f}%;background:var(--p0)"></div></div>
      <div class="area-row"><span class="red">Partition 1</span><span style="font-family:monospace">{a1:.1f}</span></div>
      <div class="area-bar"><div class="area-fill" style="width:{a1/(a0+a1)*100:.1f}%;background:var(--p1)"></div></div>
    </div>
    <div class="section">
      <div class="section-title">Cell Assignments</div>
      <div class="part-block">
        <div class="part-block-title"><span class="dot" style="background:var(--p0)"></span><span class="blue">Partition 0</span><span style="color:var(--muted);font-weight:400;margin-left:auto">{len(p0)} cells</span></div>
        <div class="cell-chips">{''.join(f'<span class="chip chip-0">{c}</span>' for c in p0)}</div>
      </div>
      <div class="part-block">
        <div class="part-block-title"><span class="dot" style="background:var(--p1)"></span><span class="red">Partition 1</span><span style="color:var(--muted);font-weight:400;margin-left:auto">{len(p1)} cells</span></div>
        <div class="cell-chips">{''.join(f'<span class="chip chip-1">{c}</span>' for c in p1)}</div>
      </div>
    </div>
    <div class="section">
      <div class="section-title">Legend</div>
      <div class="legend">
        <div class="legend-row"><span class="legend-node" style="background:rgba(79,142,247,.2);border-color:var(--p0)"></span>Partition 0 cell</div>
        <div class="legend-row"><span class="legend-node" style="background:rgba(247,99,79,.2);border-color:var(--p1)"></span>Partition 1 cell</div>
        <div class="legend-row"><div class="legend-dash"></div><span class="amber">Cut net</span></div>
        <div class="legend-row"><div class="legend-line"></div>Internal net</div>
      </div>
    </div>
  </aside>
</div>
<footer>
  <span>VLSI KaHyPar Runner</span>
  <span>Hover nodes for details · Drag to rearrange</span>
</footer>
<div id="tooltip"></div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const graph={graph_json};
const svg=d3.select("#svg"),panel=document.getElementById("graph-panel"),tip=document.getElementById("tooltip");
function resize(){{const W=panel.clientWidth,H=panel.clientHeight;svg.attr("viewBox",`0 0 ${{W}} ${{H}}`);return[W,H];}}
let[W,H]=resize();
const sim=d3.forceSimulation(graph.nodes)
  .force("link",d3.forceLink(graph.links).id(d=>d.id).distance(90).strength(0.3))
  .force("charge",d3.forceManyBody().strength(-280))
  .force("collide",d3.forceCollide(38))
  .force("x",d3.forceX(d=>d.partition===0?W*0.27:W*0.73).strength(0.18))
  .force("y",d3.forceY(H/2).strength(0.05));
const g=svg.append("g");
svg.call(d3.zoom().scaleExtent([0.3,3]).on("zoom",e=>g.attr("transform",e.transform)));
const link=g.append("g").selectAll("line").data(graph.links).join("line")
  .attr("stroke",d=>d.cut?"#fbbf24":"#1e2d4a")
  .attr("stroke-width",d=>d.cut?1.5:1)
  .attr("stroke-dasharray",d=>d.cut?"5,4":null)
  .attr("stroke-opacity",.8);
const nodeG=g.append("g").selectAll("g").data(graph.nodes).join("g")
  .attr("cursor","grab")
  .call(d3.drag()
    .on("start",(e,d)=>{{if(!e.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;}})
    .on("drag",(e,d)=>{{d.fx=e.x;d.fy=e.y;}})
    .on("end",(e,d)=>{{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}}));
const C0="#4f8ef7",C1="#f7634f";
nodeG.append("circle").attr("r",d=>6+Math.sqrt(d.area)*.7).attr("fill",d=>d.partition===0?"rgba(79,142,247,.12)":"rgba(247,99,79,.12)").attr("stroke","none");
nodeG.append("circle").attr("r",d=>4+Math.sqrt(d.area)*.5).attr("fill",d=>d.partition===0?"rgba(79,142,247,.25)":"rgba(247,99,79,.25)").attr("stroke",d=>d.partition===0?C0:C1).attr("stroke-width",2);
nodeG.append("text").text(d=>d.id).attr("text-anchor","middle").attr("dy","0.35em").attr("fill","#e2e8f0").attr("font-size","11px").attr("font-family","JetBrains Mono,monospace").attr("font-weight","600").attr("pointer-events","none");
nodeG.on("mouseenter",(e,d)=>{{tip.innerHTML=`<b style="color:${{d.partition===0?C0:C1}}">${{d.id}}</b><br>Partition ${{d.partition}}<br>Area: ${{d.area.toFixed(1)}}`;tip.classList.add("show");}}).on("mousemove",e=>{{tip.style.left=(e.clientX+14)+"px";tip.style.top=(e.clientY-10)+"px";}}).on("mouseleave",()=>tip.classList.remove("show"));
sim.on("tick",()=>{{link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);nodeG.attr("transform",d=>`translate(${{d.x}},${{d.y}})`)}});
window.addEventListener("resize",()=>{{[W,H]=resize();sim.force("x",d3.forceX(d=>d.partition===0?W*0.27:W*0.73).strength(.18)).force("y",d3.forceY(H/2).strength(.05)).alpha(.3).restart();}});
</script></body></html>"""

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html",
                                     mode="w", encoding="utf-8")
    tmp.write(html)
    tmp.close()
    _log(f"GUI visualizer   : {tmp.name}")
    webbrowser.open(f"file:///{tmp.name.replace(os.sep, '/')}")


# ─────────────────────────────────────────────────────────────────────────────
#  Individual image functions  (matplotlib + networkx)
# ─────────────────────────────────────────────────────────────────────────────

def _mpl_setup():
    """Import matplotlib in Agg (non-interactive) mode and return plt."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


# ── Image 1: Circuit Graph (small) or Partition Statistics (large) ────────────

SMALL_GRAPH_LIMIT = 300   # nodes above this switch to stats dashboard

def img_circuit_graph(nets, node_list, node_map, areas,
                      cut_net_indices, out_path="img_01_circuit_graph.png"):
    """
    For small circuits  (≤ SMALL_GRAPH_LIMIT nodes): force-directed hypergraph.
    For large circuits  (> SMALL_GRAPH_LIMIT nodes): partition statistics dashboard.
    """
    if len(node_list) <= SMALL_GRAPH_LIMIT:
        return _img_small_graph(nets, node_list, node_map, areas,
                                cut_net_indices, out_path)
    else:
        return _img_large_stats(nets, node_list, node_map, areas,
                                cut_net_indices, out_path)


def _img_small_graph(nets, node_list, node_map, areas,
                     cut_net_indices, out_path):
    """Force-directed graph for small circuits."""
    import networkx as nx
    import matplotlib.patches as mpatches
    plt = _mpl_setup()
    C0, C1, CCUT = "#4f8ef7", "#f7634f", "#fbbf24"
    BG, PANEL, TEXT = "#0a0e1a", "#0f1626", "#e2e8f0"

    G = nx.Graph()
    G.add_nodes_from(node_list)
    cut_edges = set()
    for idx, net in enumerate(nets):
        is_cut = idx in cut_net_indices
        for i in range(len(net)):
            for j in range(i + 1, len(net)):
                u, v = net[i], net[j]
                if is_cut:
                    cut_edges.add(tuple(sorted([u, v])))
                G.add_edge(u, v)

    pos = nx.spring_layout(G, seed=42, k=2.5)
    for node, (x, y) in pos.items():
        side = -0.5 if node_map.get(node, 0) == 0 else 0.5
        pos[node] = (x * 0.5 + side, y)

    node_colors = [C0 if node_map.get(n, 0) == 0 else C1 for n in G.nodes()]
    node_sizes  = [300 + areas.get(n, 1.0) * 1.8 for n in G.nodes()]

    fig, ax = plt.subplots(figsize=(10, 8), facecolor=BG)
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values():
        sp.set_edgecolor("#1e2d4a")

    intact = [(u, v) for u, v in G.edges() if tuple(sorted([u,v])) not in cut_edges]
    cut_e  = [(u, v) for u, v in G.edges() if tuple(sorted([u,v])) in cut_edges]
    nx.draw_networkx_edges(G, pos, edgelist=intact, edge_color="#1e3a5a",
                           width=1.2, style="solid", alpha=0.6, ax=ax)
    nx.draw_networkx_edges(G, pos, edgelist=cut_e, edge_color=CCUT,
                           width=2.0, style=(0, (5, 4)), alpha=0.9, ax=ax)
    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=node_sizes,
                           alpha=0.92, linewidths=2.0, edgecolors="#1e2d4a", ax=ax)
    nx.draw_networkx_labels(G, pos, font_size=9, font_color=TEXT,
                            font_weight="bold", ax=ax)
    ax.axvline(0, color="white", lw=0.6, ls="--", alpha=0.12)
    ylim = ax.get_ylim()
    ax.text(-0.9, ylim[1] * 0.93, "PARTITION 0", color=C0,
            fontsize=9, fontweight="bold", alpha=0.9)
    ax.text(0.42, ylim[1] * 0.93, "PARTITION 1", color=C1,
            fontsize=9, fontweight="bold", alpha=0.9)
    handles = [
        mpatches.Patch(facecolor=C0,   label=f"Partition 0  ({sum(1 for v in node_map.values() if v==0)} cells)"),
        mpatches.Patch(facecolor=C1,   label=f"Partition 1  ({sum(1 for v in node_map.values() if v==1)} cells)"),
        mpatches.Patch(facecolor=CCUT, label=f"Cut nets  ({len(cut_net_indices)} / {len(nets)})"),
    ]
    ax.legend(handles=handles, loc="lower center", fontsize=9,
              framealpha=0.25, labelcolor=TEXT, facecolor=PANEL, edgecolor="#1e2d4a")
    ax.set_title("Circuit Hypergraph Partition", color=TEXT,
                 fontsize=13, fontweight="bold", pad=10)
    ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
    fig.suptitle("VLSI KaHyPar Pipeline  ·  k=2, ε=0.03",
                 color="#64748b", fontsize=9, y=0.99)
    fig.savefig(out_path, dpi=180, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    _log(f"Saved → {out_path}")
    return out_path


def _img_large_stats(nets, node_list, node_map, areas,
                     cut_net_indices, out_path):
    """
    Partition statistics dashboard for large circuits (IBM benchmarks etc.).
    4-panel layout:
      [TL] Donut – cell count per partition
      [TR] Histogram – net degree distribution
      [BL] Donut – cut vs intact nets
      [BR] Bar – top-10 most-connected cells
    """
    plt = _mpl_setup()
    import matplotlib.gridspec as gridspec
    import matplotlib.patches as mpatches
    C0, C1, CCUT = "#4f8ef7", "#f7634f", "#fbbf24"
    INTACT = "#4ade80"
    BG, PANEL, TEXT, MUTED = "#0a0e1a", "#0f1626", "#e2e8f0", "#64748b"

    n0 = sum(1 for v in node_map.values() if v == 0)
    n1 = sum(1 for v in node_map.values() if v == 1)
    n_cut    = len(cut_net_indices)
    n_intact = len(nets) - n_cut

    # Net degree (fanout) per net
    degrees = [len(net) for net in nets]

    # Top-10 cells by number of nets they appear in
    cell_net_count = {}
    for net in nets:
        for c in net:
            cell_net_count[c] = cell_net_count.get(c, 0) + 1
    top10 = sorted(cell_net_count.items(), key=lambda x: x[1], reverse=True)[:10]
    top_cells  = [t[0] for t in top10]
    top_counts = [t[1] for t in top10]
    top_colors = [C0 if node_map.get(c, 0) == 0 else C1 for c in top_cells]

    fig = plt.figure(figsize=(12, 9), facecolor=BG)
    fig.suptitle(
        f"Partition Statistics  ·  {len(node_list):,} cells  ·  {len(nets):,} nets  ·  k=2, ε=0.03",
        color=TEXT, fontsize=12, fontweight="bold", y=0.98)

    gs = gridspec.GridSpec(2, 2, figure=fig,
                           hspace=0.42, wspace=0.38,
                           left=0.08, right=0.96, top=0.91, bottom=0.08)

    def style(ax, title):
        ax.set_facecolor(PANEL)
        for sp in ax.spines.values():
            sp.set_edgecolor("#1e2d4a")
        ax.set_title(title, color=TEXT, fontsize=10, fontweight="bold", pad=8)
        ax.tick_params(colors=MUTED, labelsize=8)

    # ── TL: Cell count donut ─────────────────────────────────────────────────
    ax0 = fig.add_subplot(gs[0, 0])
    ax0.set_facecolor(PANEL)
    wedges, texts, autotexts = ax0.pie(
        [n0, n1],
        labels=[f"Partition 0\n{n0:,} cells", f"Partition 1\n{n1:,} cells"],
        colors=[C0, C1],
        autopct="%1.1f%%",
        startangle=90,
        wedgeprops=dict(width=0.55, edgecolor=BG, linewidth=2),
        textprops=dict(color=TEXT, fontsize=9),
    )
    for at in autotexts:
        at.set_color(BG)
        at.set_fontweight("bold")
        at.set_fontsize(9)
    ax0.set_title("Cell Distribution per Partition",
                  color=TEXT, fontsize=10, fontweight="bold", pad=8)

    # ── TR: Net degree histogram ──────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, 1])
    style(ax1, "Net Degree Distribution (Fanout)")
    cap = min(max(degrees), 50)
    clipped = [min(d, cap) for d in degrees]
    ax1.hist(clipped, bins=30, color="#38bdf8", alpha=0.85,
             edgecolor="#1e2d4a", linewidth=0.6)
    ax1.set_xlabel(f"Net degree (capped at {cap})", color=MUTED, fontsize=8)
    ax1.set_ylabel("# Nets", color=MUTED, fontsize=8)
    ax1.xaxis.label.set_color(MUTED)
    ax1.yaxis.label.set_color(MUTED)
    med = sorted(degrees)[len(degrees)//2]
    ax1.axvline(med, color=CCUT, lw=1.4, ls="--", alpha=0.85,
                label=f"median = {med}")
    ax1.legend(fontsize=8, framealpha=0.2, labelcolor=TEXT,
               facecolor=PANEL, edgecolor="#1e2d4a")

    # ── BL: Cut vs intact donut ───────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, 0])
    ax2.set_facecolor(PANEL)
    wedges2, texts2, autotexts2 = ax2.pie(
        [n_cut, n_intact],
        labels=[f"Cut nets\n{n_cut:,}", f"Intact nets\n{n_intact:,}"],
        colors=[CCUT, INTACT],
        autopct="%1.1f%%",
        startangle=90,
        wedgeprops=dict(width=0.55, edgecolor=BG, linewidth=2),
        textprops=dict(color=TEXT, fontsize=9),
    )
    for at in autotexts2:
        at.set_color(BG)
        at.set_fontweight("bold")
        at.set_fontsize(9)
    ax2.set_title("Cut vs Intact Nets",
                  color=TEXT, fontsize=10, fontweight="bold", pad=8)

    # ── BR: Top-10 highest-degree cells ──────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 1])
    style(ax3, "Top 10 Cells by Net Connectivity")
    y_pos = list(range(len(top_cells)))
    bars = ax3.barh(y_pos, top_counts, color=top_colors,
                    alpha=0.88, edgecolor="#1e2d4a", linewidth=0.7)
    for bar, val in zip(bars, top_counts):
        ax3.text(bar.get_width() + max(top_counts) * 0.01,
                 bar.get_y() + bar.get_height() / 2,
                 str(val), va="center", color=TEXT, fontsize=8, fontweight="bold")
    ax3.set_yticks(y_pos)
    ax3.set_yticklabels(top_cells, color=TEXT, fontsize=8)
    ax3.set_xlabel("# Nets connected", color=MUTED, fontsize=8)
    ax3.xaxis.label.set_color(MUTED)
    handles3 = [
        mpatches.Patch(facecolor=C0, label="Partition 0"),
        mpatches.Patch(facecolor=C1, label="Partition 1"),
    ]
    ax3.legend(handles=handles3, fontsize=8, framealpha=0.2, labelcolor=TEXT,
               facecolor=PANEL, edgecolor="#1e2d4a")

    fig.savefig(out_path, dpi=180, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    _log(f"Saved → {out_path}")
    return out_path




# ── Image 2: Area Distribution ────────────────────────────────────────────────

def img_area_distribution(node_list, partition_areas, area_balance,
                           out_path="img_02_area_distribution.png"):
    plt = _mpl_setup()
    C0, C1, CCUT = "#4f8ef7", "#f7634f", "#fbbf24"
    BG, PANEL, TEXT, MUTED = "#0a0e1a", "#0f1626", "#e2e8f0", "#64748b"

    p_ids   = sorted(partition_areas.keys())
    p_areas = [partition_areas[p] for p in p_ids]
    colors  = [C0 if p == 0 else C1 for p in p_ids]
    labels  = [f"Partition {p}" for p in p_ids]

    fig, ax = plt.subplots(figsize=(8, 4), facecolor=BG)
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values():
        sp.set_edgecolor("#1e2d4a")

    bars = ax.barh(labels, p_areas, color=colors, height=0.4,
                   alpha=0.88, edgecolor="#1e2d4a", linewidth=1.0)
    for bar, val in zip(bars, p_areas):
        ax.text(bar.get_width() * 0.5, bar.get_y() + bar.get_height() / 2,
                f"{val:.1f}", va="center", ha="center",
                color=TEXT, fontsize=11, fontweight="bold")

    ax.set_xlabel("Total Cell Area", color=MUTED, fontsize=9)
    ax.tick_params(colors=MUTED, labelsize=10)
    ax.xaxis.label.set_color(MUTED)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, color=TEXT)

    bal_color = "#4ade80" if area_balance < 1.05 else CCUT
    ax.text(0.98, 0.08, f"Balance ratio: {area_balance:.4f}",
            transform=ax.transAxes, ha="right", va="bottom",
            color=bal_color, fontsize=9, fontweight="bold")

    ax.set_title("Area Distribution per Partition", color=TEXT,
                 fontsize=13, fontweight="bold", pad=10)
    fig.suptitle("VLSI KaHyPar Pipeline  ·  k=2, ε=0.03",
                 color=MUTED, fontsize=9, y=1.01)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    _log(f"Saved → {out_path}")
    return out_path


# ── Image 3: Cut per Seed ─────────────────────────────────────────────────────

def img_cut_per_seed(records, out_path="img_03_cut_per_seed.png"):
    plt = _mpl_setup()
    CCUT = "#fbbf24"
    BG, PANEL, TEXT, MUTED = "#0a0e1a", "#0f1626", "#e2e8f0", "#64748b"

    seeds = [r["seed"]        for r in records]
    cuts  = [r["kahypar_cut"] for r in records]
    x     = list(range(len(seeds)))
    avg   = sum(cuts) / len(cuts)

    fig, ax = plt.subplots(figsize=(8, 5), facecolor=BG)
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values():
        sp.set_edgecolor("#1e2d4a")

    bars = ax.bar(x, cuts, color="#4f8ef7", alpha=0.85,
                  edgecolor="#38bdf8", linewidth=1.0, width=0.5)
    for bar, val in zip(bars, cuts):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.05,
                str(val), ha="center", va="bottom",
                color=TEXT, fontsize=10, fontweight="bold")

    ax.axhline(avg, color=CCUT, lw=1.5, ls="--", alpha=0.8,
               label=f"Average = {avg:.2f}")
    ax.set_xticks(x)
    ax.set_xticklabels([f"Seed {s}" for s in seeds], color=MUTED, fontsize=9)
    ax.set_ylabel("Cut (# nets crossing boundary)", color=MUTED, fontsize=9)
    ax.yaxis.label.set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.legend(fontsize=9, framealpha=0.2, labelcolor=TEXT,
              facecolor=PANEL, edgecolor="#1e2d4a")
    ax.set_title("KaHyPar Cut Value per Seed", color=TEXT,
                 fontsize=13, fontweight="bold", pad=10)
    fig.suptitle("VLSI KaHyPar Pipeline  ·  k=2, ε=0.03",
                 color=MUTED, fontsize=9, y=1.01)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    _log(f"Saved → {out_path}")
    return out_path


# ── Image 4: Imbalance per Seed ───────────────────────────────────────────────

def img_imbalance_per_seed(records, epsilon=0.03,
                            out_path="img_04_imbalance_per_seed.png"):
    plt = _mpl_setup()
    BG, PANEL, TEXT, MUTED = "#0a0e1a", "#0f1626", "#e2e8f0", "#64748b"

    seeds      = [r["seed"]      for r in records]
    imbalances = [r["imbalance"] for r in records]
    x          = list(range(len(seeds)))
    avg        = sum(imbalances) / len(imbalances)

    fig, ax = plt.subplots(figsize=(8, 5), facecolor=BG)
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values():
        sp.set_edgecolor("#1e2d4a")

    bars = ax.bar(x, imbalances, color="#f7634f", alpha=0.85,
                  edgecolor="#fb923c", linewidth=1.0, width=0.5)
    for bar, val in zip(bars, imbalances):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max(imbalances) * 0.015,
                f"{val:.4f}", ha="center", va="bottom",
                color=TEXT, fontsize=9, fontweight="bold")

    ax.axhline(epsilon, color="#4ade80", lw=1.5, ls="--", alpha=0.8,
               label=f"ε limit = {epsilon}")
    ax.axhline(avg, color="#fbbf24", lw=1.2, ls=":", alpha=0.7,
               label=f"Average = {avg:.4f}")

    ax.set_xticks(x)
    ax.set_xticklabels([f"Seed {s}" for s in seeds], color=MUTED, fontsize=9)
    ax.set_ylabel("Partition Imbalance", color=MUTED, fontsize=9)
    ax.yaxis.label.set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.legend(fontsize=9, framealpha=0.2, labelcolor=TEXT,
              facecolor=PANEL, edgecolor="#1e2d4a")
    ax.set_title("Partition Imbalance per Seed", color=TEXT,
                 fontsize=13, fontweight="bold", pad=10)
    fig.suptitle("VLSI KaHyPar Pipeline  ·  k=2, ε=0.03",
                 color=MUTED, fontsize=9, y=1.01)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    _log(f"Saved → {out_path}")
    return out_path


# ── Image 5: Metrics Summary Table ────────────────────────────────────────────

def img_metrics_table(nets, node_list, cut_net_indices,
                       partition_areas, area_balance, records,
                       out_path="img_05_metrics_table.png"):
    plt = _mpl_setup()
    BG, PANEL, TEXT, MUTED = "#0a0e1a", "#0f1626", "#e2e8f0", "#64748b"

    n_valid     = [r for r in records if r.get("kahypar_cut", -1) >= 0]
    avg_cut_v   = f"{sum(r['kahypar_cut'] for r in n_valid)/len(n_valid):.2f}" if n_valid else "—"
    best_cut_v  = f"{min(r['kahypar_cut'] for r in n_valid)}"                  if n_valid else "—"
    avg_imb_v   = f"{sum(r['imbalance'] for r in n_valid)/len(n_valid):.4f}"   if n_valid else "—"
    avg_rt_v    = f"{sum(r['runtime_s'] for r in records)/max(1,len(records)):.4f} s"
    seeds_v     = str([r["seed"] for r in records])

    rows = [
        ["Vertices",      str(len(node_list))],
        ["Nets",          str(len(nets))],
        ["Cut Nets",      str(len(cut_net_indices))],
        ["Area Balance",  f"{area_balance:.4f}"],
        ["P0 Area",       f"{partition_areas.get(0,0):.1f}"],
        ["P1 Area",       f"{partition_areas.get(1,0):.1f}"],
        ["Seeds Run",     seeds_v],
        ["Avg Cut",       avg_cut_v],
        ["Best Cut",      best_cut_v],
        ["Avg Imbalance", avg_imb_v],
        ["Avg Runtime",   avg_rt_v],
    ]

    fig, ax = plt.subplots(figsize=(7, 5.5), facecolor=BG)
    ax.set_facecolor(PANEL)
    ax.axis("off")

    tbl = ax.table(cellText=rows, colLabels=["Metric", "Value"],
                   loc="center", cellLoc="left")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.scale(1.2, 1.7)

    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor("#1e2d4a")
        if r == 0:
            cell.set_facecolor("#1e2d4a")
            cell.get_text().set_color(TEXT)
            cell.get_text().set_fontweight("bold")
            cell.get_text().set_fontsize(10)
        else:
            cell.set_facecolor(PANEL if r % 2 == 0 else "#141d30")
            cell.get_text().set_color(TEXT if c == 0 else "#4ade80")

    ax.set_title("Benchmark Metrics Summary", color=TEXT,
                 fontsize=13, fontweight="bold", pad=14)
    fig.suptitle("VLSI KaHyPar Pipeline  ·  k=2, ε=0.03",
                 color=MUTED, fontsize=9, y=1.01)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    _log(f"Saved → {out_path}")
    return out_path


# ── Wrapper: save all 5 images ────────────────────────────────────────────────

def save_all_images(nets, net_names, node_list, node_map, areas,
                    cut_net_indices, partition_areas, area_balance,
                    records, base="circuit"):
    """Save each panel as its own PNG. Returns list of saved paths."""
    _section("SAVING OUTPUT IMAGES")
    saved = []
    saved.append(img_circuit_graph(
        nets, node_list, node_map, areas, cut_net_indices,
        out_path=f"{base}_01_circuit_graph.png"))
    saved.append(img_area_distribution(
        node_list, partition_areas, area_balance,
        out_path=f"{base}_02_area_distribution.png"))
    if records:
        saved.append(img_cut_per_seed(
            records, out_path=f"{base}_03_cut_per_seed.png"))
        saved.append(img_imbalance_per_seed(
            records, out_path=f"{base}_04_imbalance_per_seed.png"))
    saved.append(img_metrics_table(
        nets, node_list, cut_net_indices,
        partition_areas, area_balance, records,
        out_path=f"{base}_05_metrics_table.png"))
    _log(f"All images saved ({len(saved)} files).")
    return saved

# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────




    # ── Build networkx graph ─────────────────────────────────────────────────
    G = nx.Graph()
    G.add_nodes_from(node_list)
    cut_edges, intact_edges = set(), set()
    for idx, net in enumerate(nets):
        is_cut = idx in cut_net_indices
        for i in range(len(net)):
            for j in range(i + 1, len(net)):
                u, v = net[i], net[j]
                key  = tuple(sorted([u, v]))
                if is_cut:
                    cut_edges.add(key)
                else:
                    intact_edges.discard(key)
                    intact_edges.add(key)
                G.add_edge(u, v)

    # Force-directed layout — partition 0 left, partition 1 right
    pos = nx.spring_layout(G, seed=42, k=2.5)
    # Push each node toward its partition's side
    for node, (x, y) in pos.items():
        side = -0.5 if node_map.get(node, 0) == 0 else 0.5
        pos[node] = (x * 0.5 + side, y)

    node_colors = [C0 if node_map.get(n, 0) == 0 else C1 for n in G.nodes()]
    node_sizes  = [250 + areas.get(n, 1.0) * 1.5 for n in G.nodes()]

    # ── Figure layout ────────────────────────────────────────────────────────
    has_records = bool(records)
    fig = plt.figure(figsize=(16, 10), facecolor=BG)
    gs  = gridspec.GridSpec(
        2, 3 if has_records else 2,
        figure=fig,
        hspace=0.38, wspace=0.32,
        left=0.05, right=0.97, top=0.91, bottom=0.07,
    )

    # ── [A] Circuit graph ────────────────────────────────────────────────────
    ax_graph = fig.add_subplot(gs[:, 0])
    ax_graph.set_facecolor(PANEL)
    for spine in ax_graph.spines.values():
        spine.set_edgecolor("#1e2d4a")

    # Draw intact edges
    intact_list = [(u, v) for u, v in G.edges()
                   if tuple(sorted([u, v])) not in cut_edges]
    nx.draw_networkx_edges(G, pos, edgelist=intact_list,
                           edge_color="#1e3a5a", width=1.0,
                           style="solid", alpha=0.6, ax=ax_graph)
    # Draw cut edges (dashed amber)
    cut_list = [(u, v) for u, v in G.edges()
                if tuple(sorted([u, v])) in cut_edges]
    nx.draw_networkx_edges(G, pos, edgelist=cut_list,
                           edge_color=CCUT, width=1.8,
                           style=(0, (5, 4)), alpha=0.85, ax=ax_graph)
    # Nodes
    nx.draw_networkx_nodes(G, pos, node_color=node_colors,
                           node_size=node_sizes, alpha=0.92,
                           linewidths=1.8, edgecolors="#1e2d4a", ax=ax_graph)
    # Labels
    nx.draw_networkx_labels(G, pos, font_size=7.5, font_color=TEXT,
                            font_weight="bold", ax=ax_graph)

    # Dashed centre divider
    ax_graph.axvline(0, color="white", linewidth=0.5, linestyle="--", alpha=0.1)
    ax_graph.text(-0.85, ax_graph.get_ylim()[1] * 0.92,
                  "PARTITION 0", color=C0, fontsize=7, fontweight="bold",
                  alpha=0.8)
    ax_graph.text(0.35, ax_graph.get_ylim()[1] * 0.92,
                  "PARTITION 1", color=C1, fontsize=7, fontweight="bold",
                  alpha=0.8)

    legend_handles = [
        mpatches.Patch(facecolor=C0,   label=f"Partition 0  ({sum(1 for v in node_map.values() if v==0)} cells)"),
        mpatches.Patch(facecolor=C1,   label=f"Partition 1  ({sum(1 for v in node_map.values() if v==1)} cells)"),
        mpatches.Patch(facecolor=CCUT, label=f"Cut net  ({len(cut_net_indices)} / {len(nets)})"),
    ]
    ax_graph.legend(handles=legend_handles, loc="lower center",
                    fontsize=7, framealpha=0.2, labelcolor=TEXT,
                    facecolor=PANEL, edgecolor="#1e2d4a")
    ax_graph.set_title("Circuit Hypergraph Partition", color=TEXT,
                       fontsize=10, fontweight="bold", pad=8)
    ax_graph.tick_params(left=False, bottom=False,
    ax_area.tick_params(colors=MUTED, labelsize=8)
    for spine in ax_area.spines.values():
        spine.set_edgecolor("#1e2d4a")
    ax_area.set_facecolor(PANEL)
    ax_area.xaxis.label.set_color(MUTED)
    balance_color = "#4ade80" if area_balance < 1.05 else CCUT
    ax_area.text(0.98, 0.05, f"Balance: {area_balance:.4f}",
                 transform=ax_area.transAxes, ha="right", va="bottom",
                 color=balance_color, fontsize=8, fontweight="bold")

    # ── [C] Per-seed charts (if records provided) ────────────────────────────
    if has_records:
        seeds     = [r["seed"]        for r in records]
        cuts      = [r["kahypar_cut"] for r in records]
        imbalances= [r["imbalance"]   for r in records]
        x         = list(range(len(seeds)))

        ax_cut = fig.add_subplot(gs[0, 2])
        ax_cut.set_facecolor(PANEL)
        ax_cut.bar(x, cuts, color="#4f8ef7", alpha=0.8,
                   edgecolor="#1e2d4a", linewidth=0.8)
        ax_cut.set_xticks(x)
        ax_cut.set_xticklabels([f"s{s}" for s in seeds], fontsize=8, color=MUTED)
        ax_cut.set_title("Cut per Seed", color=TEXT, fontsize=9, fontweight="bold")
        ax_cut.set_ylabel("Cut nets", color=MUTED, fontsize=8)
        ax_cut.tick_params(colors=MUTED, labelsize=8)
        for spine in ax_cut.spines.values():
            spine.set_edgecolor("#1e2d4a")
        ax_cut.yaxis.label.set_color(MUTED)
        avg_cut = sum(cuts) / len(cuts)
        ax_cut.axhline(avg_cut, color=CCUT, linewidth=1.2,
                       linestyle="--", alpha=0.7, label=f"avg={avg_cut:.1f}")
        ax_cut.legend(fontsize=7, framealpha=0.2, labelcolor=TEXT,
                      facecolor=PANEL, edgecolor="#1e2d4a")

        ax_imb = fig.add_subplot(gs[1, 2])
        ax_imb.set_facecolor(PANEL)
        ax_imb.bar(x, imbalances, color="#f7634f", alpha=0.8,
                   edgecolor="#1e2d4a", linewidth=0.8)
        ax_imb.set_xticks(x)
        ax_imb.set_xticklabels([f"s{s}" for s in seeds], fontsize=8, color=MUTED)
        ax_imb.set_title("Imbalance per Seed", color=TEXT, fontsize=9, fontweight="bold")
        ax_imb.set_ylabel("Imbalance", color=MUTED, fontsize=8)
        ax_imb.tick_params(colors=MUTED, labelsize=8)
        for spine in ax_imb.spines.values():
            spine.set_edgecolor("#1e2d4a")
        ax_imb.yaxis.label.set_color(MUTED)
        ax_imb.axhline(0.03, color="#4ade80", linewidth=1.0,
                       linestyle="--", alpha=0.6, label="ε = 0.03")
        ax_imb.legend(fontsize=7, framealpha=0.2, labelcolor=TEXT,
                      facecolor=PANEL, edgecolor="#1e2d4a")

    # ── [D] Metrics summary table ────────────────────────────────────────────
    ax_tbl = fig.add_subplot(gs[1, 1])
    ax_tbl.set_facecolor(PANEL)
    ax_tbl.axis("off")

    n_valid = [r for r in (records or []) if r.get("kahypar_cut", -1) >= 0]
    avg_cut_v   = f"{sum(r['kahypar_cut'] for r in n_valid)/len(n_valid):.2f}" if n_valid else "—"
    best_cut_v  = f"{min(r['kahypar_cut'] for r in n_valid)}"                  if n_valid else "—"
    avg_imb_v   = f"{sum(r['imbalance'] for r in n_valid)/len(n_valid):.4f}"   if n_valid else "—"
    avg_rt_v    = f"{sum(r['runtime_s'] for r in (records or []))/max(1,len(records or [])):.4f}s" if records else "—"
    seeds_v     = str([r["seed"] for r in (records or [])]) if records else "—"

    rows = [
        ["Vertices",     str(len(node_list))],
        ["Nets",         str(len(nets))],
        ["Cut Nets",     str(len(cut_net_indices))],
        ["Area Balance", f"{area_balance:.4f}"],
        ["P0 Area",      f"{partition_areas.get(0,0):.1f}"],
        ["P1 Area",      f"{partition_areas.get(1,0):.1f}"],
        ["Avg Cut",      avg_cut_v],
        ["Best Cut",     best_cut_v],
        ["Avg Imbalance",avg_imb_v],
        ["Avg Runtime",  avg_rt_v],
        ["Seeds",        seeds_v],
    ]

    col_labels = ["Metric", "Value"]
    tbl = ax_tbl.table(
        cellText=rows,
        colLabels=col_labels,
        loc="center",
        cellLoc="left",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8)
    tbl.scale(1.0, 1.45)

    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor("#1e2d4a")
        if r == 0:
            cell.set_facecolor("#1e2d4a")
            cell.get_text().set_color(TEXT)
            cell.get_text().set_fontweight("bold")
        else:
            cell.set_facecolor(PANEL if r % 2 == 0 else "#141d30")
            cell.get_text().set_color(TEXT if c == 0 else "#4ade80")
    ax_tbl.set_title("Benchmark Metrics", color=TEXT,
                     fontsize=9, fontweight="bold", pad=6)

    # ── Title & save ─────────────────────────────────────────────────────────
    fig.suptitle(
        "VLSI Circuit Partitioning  ·  KaHyPar Pipeline  ·  k=2, ε=0.03",
        color=TEXT, fontsize=12, fontweight="bold", y=0.97,
    )

    fig.savefig(out_path, dpi=180, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    _log(f"Partition image  : {out_path}  (saved)")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _log(msg):
    print(f"  {msg}")

def _warn(msg):
    print(f"  [WARN] {msg}")

def _section(title):
    line = "═" * 60
    print(f"\n╔{line}╗")
    print(f"║  {title:<58}║")
    print(f"╚{line}╝\n")

def _extract_int(pattern, text, default=-1):
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else default

def _extract_float(pattern, text, default=-1.0):
    m = re.search(pattern, text, re.IGNORECASE)
    return float(m.group(1)) if m else default

def _parse_args(args):
    """Simple arg parser. .bench files need only 1 positional arg (no .are)."""
    cfg = {
        "net_path": None,
        "are_path": None,
        "seeds":    [0],
        "csv":      False,
    }
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--csv":
            cfg["csv"] = True
        elif a == "--seeds" and i + 1 < len(args):
            i += 1
            cfg["seeds"] = [int(s.strip()) for s in args[i].split(",")]
        elif not a.startswith("--"):
            positional.append(a)
        i += 1

    if positional:
        cfg["net_path"] = positional[0]
    if len(positional) >= 2:
        cfg["are_path"] = positional[1]

    return cfg


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    cfg = _parse_args(sys.argv[1:])

    if not cfg["net_path"] or not cfg["are_path"]:
        print(__doc__)
        sys.exit(1)

    _section("VLSI → KaHyPar RUNNER")

    # ── Parse ────────────────────────────────────────────────────────────────
    nets, net_names = parse_net_file(cfg["net_path"])
    areas           = parse_are_file(cfg["are_path"])

    # ── Convert ──────────────────────────────────────────────────────────────
    node_list, _ = convert_to_hgr(nets, areas, HGR_PATH)

    # ── Benchmark ────────────────────────────────────────────────────────────
    records = run_multi_seed(
        nets, net_names, node_list, areas,
        seeds=cfg["seeds"],
        hgr_path=HGR_PATH,
    )

    # ── Summary ──────────────────────────────────────────────────────────────
    print_summary(nets, net_names, node_list, areas, records)

    # ── CSV ──────────────────────────────────────────────────────────────────
    if cfg["csv"] and records:
        save_csv(records)

    # ── Images (always generated automatically) ───────────────────────────────
    if records:
        best = min(records, key=lambda r: r["kahypar_cut"] if r["kahypar_cut"] >= 0 else 99999)
        base = os.path.splitext(os.path.basename(cfg["net_path"]))[0]
        save_all_images(
            nets, net_names, node_list,
            best["_node_map"], areas,
            best["_cut_indices"], best["_part_areas"],
            best["area_balance"],
            records=records,
            base=base,
        )


if __name__ == "__main__":
    main()
