#!/usr/bin/env python3
"""
check_lut.py -- sanity-check a gm/ID LUT produced by techsweep_ngspice.py
before you trust it in GIDE.

    python3 check_lut.py luts/SKY130A_130nm_nfet_01v8.pkl --vds 0.9 --png nch.png

Prints a physics report (monotonicity, subthreshold gm/ID limit, fT and
intrinsic-gain ranges) and, unless --no-plot is given, draws the four classic
gm/ID design charts at the requested VDS and VSB = 0:

    gm/ID vs VGS      gm/ID vs ID/W (the sizing chart)
    fT vs gm/ID       gm/gds vs gm/ID
"""

from __future__ import annotations

import argparse
import pickle
import sys

import numpy as np

KT_Q = 1.380649e-23 * 300.15 / 1.602176634e-19   # ~25.9 mV


def load(path):
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    for k in ("L", "VGS", "VDS", "VSB", "ids", "gm", "gds", "cgg"):
        if k not in d:
            sys.exit(f"{path}: missing key '{k}' -- not a GIDE-format LUT?")
    return d


def report(d, path):
    L, VGS, VDS, VSB = d["L"], d["VGS"], d["VDS"], d["VSB"]
    ID = np.abs(d["ids"]).astype(float)
    GM = np.abs(d["gm"]).astype(float)
    GDS = np.abs(d["gds"]).astype(float)
    CGG = np.abs(d["cgg"]).astype(float)
    W = float(d["W"]) * int(d.get("NFING", 1))

    with np.errstate(divide="ignore", invalid="ignore"):
        gmid = np.where(ID > 0, GM / ID, np.nan)
        gain = np.where(GDS > 0, GM / GDS, np.nan)
        ft = np.where(CGG > 0, GM / (2 * np.pi * CGG), np.nan)

    print(f"== {path}")
    print(f"   {d.get('INFO', '')}")
    print(f"   grid      : {len(L)} L x {len(VGS)} VGS x {len(VDS)} VDS "
          f"x {len(VSB)} VSB   ({ID.size:,} points)")
    print(f"   L (um)    : {np.array2string(L * 1e6, precision=3)}")
    print(f"   VGS (V)   : {VGS[0]:.3g} .. {VGS[-1]:.3g}  step "
          f"{(VGS[1] - VGS[0]) * 1e3:.3g} mV")
    print(f"   VDS (V)   : {VDS[0]:.3g} .. {VDS[-1]:.3g}")
    print(f"   VSB (V)   : {np.array2string(VSB, precision=3)}")
    print(f"   W_ref     : {W * 1e6:.4g} um   (nf = {d.get('NFING', 1)})")
    print(f"   dtype     : {ID.dtype}")

    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"   [{'PASS' if cond else 'FAIL'}] {label}{detail}")

    iv = len(VDS) - 1
    mono_g = all(np.all(np.diff(ID[i, :, iv, 0]) >= -1e-18)
                 for i in range(len(L)))
    check("ID rises monotonically with VGS", mono_g)

    ig = len(VGS) - 1
    mono_d = all(np.all(np.diff(ID[i, ig, :, 0]) >= -1e-18)
                 for i in range(len(L)))
    check("ID rises monotonically with VDS", mono_d)

    if len(VSB) > 1:
        vth = np.abs(d["vth"]).astype(float)
        rise = np.all(np.diff(vth[:, ig, iv, :], axis=-1) > 0)
        check("|Vth| rises with VSB (body effect)", rise)

    gm_max = np.nanmax(gmid)
    ideal = 1.0 / KT_Q
    check("subthreshold gm/ID below the kT/q limit", gm_max <= ideal * 1.05,
          f"  (max {gm_max:.1f} 1/V, limit {ideal:.1f})")
    check("subthreshold gm/ID reaches a sane maximum", gm_max > 20,
          f"  (max {gm_max:.1f} 1/V)")

    finite = np.isfinite(ft) & (ft > 0)
    print(f"   fT        : {np.nanmin(ft[finite]) / 1e6:.3g} MHz .. "
          f"{np.nanmax(ft[finite]) / 1e9:.4g} GHz")
    gfin = np.isfinite(gain) & (gain > 0)
    print(f"   gm/gds    : {np.nanmin(gain[gfin]):.3g} .. "
          f"{np.nanmax(gain[gfin]):.4g}")
    if W:
        print(f"   ID/W max  : {np.nanmax(ID) / (W * 1e6) * 1e6:.4g} uA/um")

    nan = sum(int(np.count_nonzero(~np.isfinite(np.asarray(v, dtype=float))))
              for k, v in d.items() if isinstance(v, np.ndarray))
    check("no NaN/Inf anywhere in the LUT", nan == 0, f"  ({nan} found)")

    print(f"   -> {'LUT looks healthy' if ok else 'CHECK THE FAILURES ABOVE'}\n")
    return ok


def plot(d, path, vds, png):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available -- skipping plots")
        return

    L, VGS, VDS = d["L"], d["VGS"], d["VDS"]
    iv = int(np.argmin(np.abs(VDS - vds)))
    ID = np.abs(d["ids"]).astype(float)[:, :, iv, 0]
    GM = np.abs(d["gm"]).astype(float)[:, :, iv, 0]
    GDS = np.abs(d["gds"]).astype(float)[:, :, iv, 0]
    CGG = np.abs(d["cgg"]).astype(float)[:, :, iv, 0]
    W = float(d["W"]) * int(d.get("NFING", 1))

    with np.errstate(divide="ignore", invalid="ignore"):
        gmid = GM / ID
        idw = ID / (W * 1e6)          # uA/um -> A/um; units cancel in the plot
        gain = GM / GDS
        ft = GM / (2 * np.pi * CGG)

    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    fig.suptitle(f"{d.get('INFO', path).split(';')[2].strip()}   "
                 f"@ VDS = {VDS[iv]:.3g} V, VSB = 0")

    for i, Lv in enumerate(L):
        lab = f"L={Lv * 1e6:g}u"
        ax[0, 0].plot(VGS, gmid[i], label=lab)
        ax[0, 1].semilogx(idw[i], gmid[i], label=lab)
        ax[1, 0].plot(gmid[i], ft[i] / 1e9, label=lab)
        ax[1, 1].plot(gmid[i], gain[i], label=lab)

    ax[0, 0].set(xlabel="VGS [V]", ylabel="gm/ID [1/V]")
    ax[0, 1].set(xlabel="ID/W [A/um]", ylabel="gm/ID [1/V]")
    ax[1, 0].set(xlabel="gm/ID [1/V]", ylabel="fT [GHz]", yscale="log")
    ax[1, 1].set(xlabel="gm/ID [1/V]", ylabel="gm/gds [V/V]", yscale="log")
    for a in ax.ravel():
        a.grid(True, which="both", alpha=0.3)
        a.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(png, dpi=130)
    print(f"   wrote {png}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lut", nargs="+")
    ap.add_argument("--vds", type=float, default=0.9)
    ap.add_argument("--png", default=None,
                    help="output image (default: <lut>.png)")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    allok = True
    for p in args.lut:
        d = load(p)
        allok &= report(d, p)
        if not args.no_plot:
            plot(d, p, args.vds, args.png or p.rsplit(".", 1)[0] + ".png")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
