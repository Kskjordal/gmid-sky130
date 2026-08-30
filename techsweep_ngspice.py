#!/usr/bin/env python3
"""
techsweep_ngspice.py -- gm/ID look-up-table generator for Skywater sky130
                        that writes .pkl files GIDE can load directly.

GIDE (https://github.com/hasanshahata/GIDE-Universal-Design-Studio) ships a
LUT generator that drives Cadence Spectre and parses PSF raw files.  This
script is a drop-in replacement for that step: it drives *ngspice* against the
open_pdks sky130A models (the simulator + PDK you already have inside the
aicex Docker container) and writes .pkl files with exactly the dictionary
schema GIDE's core/data_loader.py expects.

Usage (inside the aicex container):

    python3 techsweep_ngspice.py --config sky130_config.json --outdir luts

Design notes
------------
* One ngspice process per device.  All requested channel lengths live in the
  same netlist as parallel instances (xm0, xm1, ...) driven by the same bias
  sources, so a single DC analysis characterises every L at once.  This is
  necessary because sky130 model *binning* is resolved when the netlist is
  parsed -- you cannot `alter` L and expect the right bin.

* ngspice's `dc` command supports two nested source sweeps, so VGS (inner)
  and VDS (outer) are covered by one analysis.  VSB is looped by altering the
  bulk source between analyses.

* Results come back through `wrdata` in ASCII with `wr_singlescale`, which is
  robust and version-independent (no raw-file parsing).

* Signs: ngspice returns magnitudes for PMOS small-signal quantities, so both
  device polarities land in the LUT as positive numbers.  Capacitances are
  stored as magnitudes for both polarities, matching what GIDE's PMOS path
  does internally.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

# --------------------------------------------------------------------------
# Signals pulled out of every operating point.
#
# Key   = the name GIDE's data_loader.py looks for in the pickle
# Value = the ngspice BSIM4 instance parameter
# --------------------------------------------------------------------------
SIGNALS = {
    "ids": "id",
    "gm": "gm",
    "gds": "gds",
    "gmb": "gmbs",
    "vth": "vth",
    "vdsat": "vdsat",
    "cgg": "cgg",
    "cgs": "cgs",
    "cgd": "cgd",
    "cdd": "cdd",
    "css": "css",
    "cgb": "cgb",
    "csg": "csg",
    "cdg": "cdg",
    "csb": "csb",
    "cdb": "cdb",
}

# Stored as magnitudes regardless of the simulator's sign convention.
ABS_KEYS = {"cgg", "cgs", "cgd", "cdd", "css", "cgb", "csg", "cdg", "csb",
            "cdb", "ids", "gm", "gds", "gmb", "vdsat"}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def parse_eng(val) -> float:
    """Parse engineering notation: '180n', '1.2u', '600m', '0.9'."""
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower()
    if not s:
        return 0.0
    if s.endswith("meg"):
        return float(s[:-3]) * 1e6
    mult = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3,
            "k": 1e3, "g": 1e9, "t": 1e12}
    if s[-1] in mult:
        try:
            return float(s[:-1]) * mult[s[-1]]
        except ValueError:
            pass
    return float(s)


def parse_vector(spec) -> np.ndarray:
    """Parse 'start:step:stop' or a comma/space separated list, or a list."""
    if isinstance(spec, (list, tuple)):
        return np.array([parse_eng(v) for v in spec], dtype=float)
    out = []
    for tok in str(spec).replace(",", " ").split():
        if ":" in tok:
            a, b, c = (parse_eng(x) for x in tok.split(":"))
            n = int(round((c - a) / b)) + 1
            out.extend(a + b * np.arange(n))
        else:
            out.append(parse_eng(tok))
    return np.array(out, dtype=float)


def fmt(x: float) -> str:
    return f"{x:.12g}"


# Where open_pdks / volare / ciel typically drop the ngspice model library.
_LIB_CANDIDATES = [
    "{PDK_ROOT}/{PDK}/libs.tech/ngspice/sky130.lib.spice",
    "/opt/pdk/share/pdk/{PDK}/libs.tech/ngspice/sky130.lib.spice",
    "/usr/local/share/pdk/{PDK}/libs.tech/ngspice/sky130.lib.spice",
    "/usr/share/pdk/{PDK}/libs.tech/ngspice/sky130.lib.spice",
    "{HOME}/pdk/{PDK}/libs.tech/ngspice/sky130.lib.spice",
    "{HOME}/.volare/{PDK}/libs.tech/ngspice/sky130.lib.spice",
    "{HOME}/.ciel/{PDK}/libs.tech/ngspice/sky130.lib.spice",
]


def find_model_file(spec: str) -> str:
    """Resolve pdk.MODEL_FILE, auto-discovering it when set to 'auto'."""
    if spec and spec.lower() != "auto":
        return os.path.expanduser(os.path.expandvars(spec))
    env = {
        "PDK_ROOT": os.environ.get("PDK_ROOT", "/opt/pdk/share/pdk"),
        "PDK": os.environ.get("PDK", "sky130A"),
        "HOME": os.path.expanduser("~"),
    }
    tried = []
    for tmpl in _LIB_CANDIDATES:
        path = tmpl.format(**env)
        tried.append(path)
        if os.path.exists(path):
            print(f"Found sky130 models: {path}", flush=True)
            return path
    raise SystemExit(
        "Could not auto-locate sky130.lib.spice. Set pdk.MODEL_FILE in the "
        "config file explicitly. Tried:\n  " + "\n  ".join(tried))


# --------------------------------------------------------------------------
# Netlist construction
# --------------------------------------------------------------------------
def build_netlist(dev, cfg, L_vec, VGS, VDS, VSB, datadir):
    """Return the ngspice deck for one device, covering every L in L_vec."""
    model = dev["model"]
    is_p = dev.get("type", "n").lower().startswith("p")
    W = parse_eng(dev.get("W", cfg["sweep"].get("W", "5u")))
    nf = int(dev.get("NFING", cfg["sweep"].get("NFING", 1)))

    # sky130 decks run with .option scale=1u, so w/l are given in microns.
    W_um = W * 1e6
    sign = -1.0 if is_p else 1.0

    lib = cfg["pdk"]["MODEL_FILE"]
    corner = cfg["pdk"].get("CORNER", "tt")
    temp = parse_eng(cfg["pdk"].get("TEMP", 27))

    lines = [
        f"* GIDE gm/ID techsweep -- {model}",
        f".lib {lib} {corner}",
        f".temp {fmt(temp)}",
        "",
        "vd d 0 dc 0",
        "vg g 0 dc 0",
        "vb b 0 dc 0",
        "",
    ]
    # Optional source/drain diffusion geometry. Left at zero by default (the
    # same assumption GIDE's Spectre flow makes): the LUT then describes the
    # intrinsic device, with no diffusion capacitance and no S/D parasitic
    # resistance. Set DIFF_EXT (or explicit AD/AS/PD/PS) in the config to make
    # the LUT match a real drawn layout -- check the value against the
    # transistor library you actually use.
    geo = dict(dev.get("geometry", cfg["sweep"].get("geometry", {})))
    ext = parse_eng(geo.pop("DIFF_EXT", 0.0)) * 1e6      # microns
    if ext > 0:
        geo.setdefault("ad", W_um * ext)
        geo.setdefault("as", W_um * ext)
        geo.setdefault("pd", 2.0 * (W_um + ext))
        geo.setdefault("ps", 2.0 * (W_um + ext))
    geo_str = "".join(f" {k}={fmt(parse_eng(v))}" for k, v in geo.items())

    for i, L in enumerate(L_vec):
        lines.append(
            f"xm{i} d g 0 b {model} L={fmt(L * 1e6)} W={fmt(W_um)} "
            f"nf={nf}{geo_str}"
        )

    # Every vector we want out of the run, in a fixed column order.
    vecs = [
        f"@m.xm{i}.m{model}[{SIGNALS[k]}]"
        for i in range(len(L_vec))
        for k in SIGNALS
    ]

    lines += ["", ".control", "set wr_singlescale", "set wr_vecnames",
              "option noinit"]

    # `save` in chunks so no single line becomes unreasonably long.
    for start in range(0, len(vecs), 8):
        chunk = vecs[start:start + 8]
        lines.append("save " + " ".join(chunk) if start == 0
                     else "+ " + " ".join(chunk))

    for k, vsb in enumerate(VSB):
        out = os.path.join(datadir, f"vsb{k:03d}.dat")
        lines += [
            "",
            f"* ---- VSB = {fmt(vsb)} V ----",
            f"alter vb dc = {fmt(-sign * vsb)}",
            f"dc vg {fmt(sign * VGS[0])} {fmt(sign * VGS[-1])} "
            f"{fmt(sign * (VGS[1] - VGS[0]) if len(VGS) > 1 else sign * 0.1)} "
            f"vd {fmt(sign * VDS[0])} {fmt(sign * VDS[-1])} "
            f"{fmt(sign * (VDS[1] - VDS[0]) if len(VDS) > 1 else sign * 0.1)}",
            f"wrdata {out} " + " ".join(vecs),
        ]

    lines += ["", "quit", ".endc", ".end", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Running / parsing
# --------------------------------------------------------------------------
def run_ngspice(deck_path, workdir, ngspice="ngspice", verbose=False):
    log = os.path.join(workdir, "ngspice.log")
    with open(log, "w") as fh:
        proc = subprocess.run([ngspice, "-b", deck_path],
                              stdout=fh, stderr=subprocess.STDOUT,
                              cwd=workdir)
    if proc.returncode != 0:
        with open(log) as fh:
            tail = "".join(fh.readlines()[-40:])
        raise RuntimeError(f"ngspice failed (exit {proc.returncode}):\n{tail}")
    if verbose:
        with open(log) as fh:
            sys.stdout.write(fh.read())
    return log


def read_wrdata(path, ncols_expected):
    """Read a wrdata ASCII file written with wr_singlescale + wr_vecnames."""
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(("v-sweep", "#")):
                continue
            parts = line.split()
            try:
                rows.append([float(p) for p in parts])
            except ValueError:
                continue
    arr = np.array(rows, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != ncols_expected + 1:
        raise RuntimeError(
            f"{path}: expected {ncols_expected + 1} columns, got "
            f"{arr.shape[1] if arr.ndim == 2 else '?'}")
    return arr[:, 1:]           # drop the sweep-scale column


# --------------------------------------------------------------------------
# Per-device characterisation
# --------------------------------------------------------------------------
def characterise(dev, cfg, outdir, keep=False, ngspice="ngspice",
                 verbose=False, dtype=np.float64):
    model = dev["model"]
    VGS = parse_vector(cfg["sweep"]["VGS_VEC"])
    VDS = parse_vector(cfg["sweep"]["VDS_VEC"])
    VSB = parse_vector(cfg["sweep"]["VSB_VEC"])
    L_vec = parse_vector(dev["L_VEC"])
    W = parse_eng(dev.get("W", cfg["sweep"].get("W", "5u")))
    nf = int(dev.get("NFING", cfg["sweep"].get("NFING", 1)))

    nL, nG, nD, nS = len(L_vec), len(VGS), len(VDS), len(VSB)
    nsig = len(SIGNALS)

    print(f"[{model}] {nL} L x {nG} VGS x {nD} VDS x {nS} VSB "
          f"= {nL * nG * nD * nS:,} points", flush=True)

    workdir = tempfile.mkdtemp(prefix=f"techsweep_{model}_")
    datadir = os.path.join(workdir, "data")
    os.makedirs(datadir, exist_ok=True)

    try:
        deck = build_netlist(dev, cfg, L_vec, VGS, VDS, VSB, datadir)
        deck_path = os.path.join(workdir, "techsweep.spice")
        with open(deck_path, "w") as fh:
            fh.write(deck)

        t0 = time.time()
        run_ngspice(deck_path, workdir, ngspice=ngspice, verbose=verbose)
        print(f"[{model}] ngspice finished in {time.time() - t0:.1f} s",
              flush=True)

        data = {k: np.zeros((nL, nG, nD, nS)) for k in SIGNALS}

        for k in range(nS):
            path = os.path.join(datadir, f"vsb{k:03d}.dat")
            block = read_wrdata(path, nL * nsig)
            if block.shape[0] != nG * nD:
                raise RuntimeError(
                    f"{path}: expected {nG * nD} rows, got {block.shape[0]}")
            # wrdata row order: VGS inner, VDS outer
            block = block.reshape(nD, nG, nL, nsig)
            for si, key in enumerate(SIGNALS):
                # -> (nL, nG)
                data[key][:, :, :, k] = np.transpose(
                    block[:, :, :, si], (2, 1, 0))

        for key in data:
            if key in ABS_KEYS:
                data[key] = np.abs(data[key])
            data[key] = data[key].astype(dtype, copy=False)

        info = (
            f"GIDE ngspice LUT; PDK: {os.path.basename(cfg['pdk']['MODEL_FILE'])}; "
            f"Device: {model}; Corner: {cfg['pdk'].get('CORNER', 'tt')}; "
            f"Temp: {parse_eng(cfg['pdk'].get('TEMP', 27))}C; "
            f"L_Range: [{L_vec.min():.3g}, {L_vec.max():.3g}]; "
            f"VGS_Range: [{VGS.min():.3g}, {VGS.max():.3g}]; "
            f"VDS_Range: [{VDS.min():.3g}, {VDS.max():.3g}]; "
            f"VSB_Range: [{VSB.min():.3g}, {VSB.max():.3g}]; "
            f"Ref_W: {W:.3g}; NFING: {nf}"
        )

        out = {
            "L": L_vec, "VGS": VGS, "VDS": VDS, "VSB": VSB,
            "W": W, "NFING": nf, "INFO": info,
        }
        out.update(data)

        os.makedirs(outdir, exist_ok=True)
        pkl = os.path.join(outdir, dev.get("output", f"{model}.pkl"))
        with open(pkl, "wb") as fh:
            pickle.dump(out, fh)
        print(f"[{model}] wrote {pkl} "
              f"({os.path.getsize(pkl) / 1e6:.1f} MB)", flush=True)
        return pkl
    finally:
        if keep:
            print(f"[{model}] work files kept in {workdir}", flush=True)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="sky130_config.json")
    ap.add_argument("--outdir", default="luts")
    ap.add_argument("--device", action="append",
                    help="only run this device (repeatable); matches the "
                         "'name' or 'model' field")
    ap.add_argument("--ngspice", default="ngspice")
    ap.add_argument("--keep", action="store_true",
                    help="keep netlists and raw ASCII output for debugging")
    ap.add_argument("--float32", action="store_true",
                    help="store arrays as float32 (halves .pkl size; GIDE "
                         "promotes back to float64 on load)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    with open(args.config) as fh:
        cfg = json.load(fh)

    lib = find_model_file(cfg["pdk"].get("MODEL_FILE", "auto"))
    if not os.path.exists(lib):
        sys.exit(f"Model library not found: {lib}\n"
                 f"Set pdk.MODEL_FILE in {args.config} to your "
                 f"sky130.lib.spice path.")
    cfg["pdk"]["MODEL_FILE"] = lib

    if shutil.which(args.ngspice) is None:
        sys.exit(f"'{args.ngspice}' not found on PATH.")

    devices = cfg["devices"]
    if args.device:
        want = set(args.device)
        devices = [d for d in devices
                   if d.get("name") in want or d.get("model") in want]
        if not devices:
            sys.exit(f"No device matched {sorted(want)}")

    t0 = time.time()
    written = []
    for dev in devices:
        written.append(characterise(
            dev, cfg, args.outdir, keep=args.keep, ngspice=args.ngspice,
            verbose=args.verbose,
            dtype=np.float32 if args.float32 else np.float64))
    print(f"\nDone in {time.time() - t0:.1f} s. {len(written)} LUT(s):")
    for p in written:
        print("  " + p)


if __name__ == "__main__":
    main()
