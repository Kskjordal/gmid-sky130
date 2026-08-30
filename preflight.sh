#!/usr/bin/env bash
#
# preflight.sh -- check whether this machine can already run the sky130 gm/ID
#                 LUT generator, and if so, tell you exactly how to invoke it.
#
# Read-only: installs nothing, needs no sudo. Run this BEFORE setup_wsl.sh --
# if you already have ngspice and open_pdks (e.g. under /opt), there is
# nothing to install.
#
#   bash preflight.sh
#
set -uo pipefail

ok()   { printf '\033[1;32m  OK  \033[0m %s\n' "$*"; }
bad()  { printf '\033[1;31m FAIL \033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m NOTE \033[0m %s\n' "$*"; }
hdr()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

FAILED=0

# --------------------------------------------------------------------------
hdr "ngspice"
# --------------------------------------------------------------------------
NGSPICE=""
for cand in ngspice /opt/eda/bin/ngspice /usr/local/bin/ngspice /usr/bin/ngspice; do
    if command -v "$cand" >/dev/null 2>&1; then NGSPICE="$(command -v "$cand")"; break; fi
done
if [ -n "$NGSPICE" ]; then
    ok "$NGSPICE  ($("$NGSPICE" -v 2>&1 | grep -m1 -o 'ngspice-[0-9]*' || echo 'version unknown'))"
else
    bad "ngspice not found (tried PATH, /opt/eda/bin, /usr/local/bin, /usr/bin)"
    warn "install with: sudo apt-get install -y ngspice"
    FAILED=1
fi

# --------------------------------------------------------------------------
hdr "python3 + numpy"
# --------------------------------------------------------------------------
PY=""
for cand in "$HOME/.venv-gmid/bin/python3" /opt/eda/python3/bin/python3 python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import numpy" 2>/dev/null; then
        PY="$(command -v "$cand")"; break
    fi
done
if [ -n "$PY" ]; then
    ok "$PY  (numpy $("$PY" -c 'import numpy;print(numpy.__version__)'))"
    if "$PY" -c "import matplotlib" 2>/dev/null; then
        ok "matplotlib present (check_lut.py can draw its charts)"
    else
        warn "no matplotlib -- check_lut.py will still print its report, just no plots"
    fi
else
    bad "no python3 with numpy found"
    warn "install with: sudo apt-get install -y python3-numpy python3-matplotlib"
    FAILED=1
fi

# --------------------------------------------------------------------------
hdr "sky130A ngspice models"
# --------------------------------------------------------------------------
LIB=""
CANDIDATES=(
    "${PDK_ROOT:-}/${PDK:-sky130A}/libs.tech/ngspice/sky130.lib.spice"
    "/opt/pdk/share/pdk/sky130A/libs.tech/ngspice/sky130.lib.spice"
    "/opt/pdk/sky130A/libs.tech/ngspice/sky130.lib.spice"
    "/usr/local/share/pdk/sky130A/libs.tech/ngspice/sky130.lib.spice"
    "/usr/share/pdk/sky130A/libs.tech/ngspice/sky130.lib.spice"
    "$HOME/pdk/sky130A/libs.tech/ngspice/sky130.lib.spice"
    "$HOME/.volare/sky130A/libs.tech/ngspice/sky130.lib.spice"
    "$HOME/.ciel/sky130A/libs.tech/ngspice/sky130.lib.spice"
)
for c in "${CANDIDATES[@]}"; do
    [ -n "$c" ] || continue
    if [ -f "$c" ]; then LIB="$c"; break; fi
done

if [ -z "$LIB" ]; then
    warn "not in the usual places -- searching /opt (this takes a moment)"
    LIB="$(find /opt -maxdepth 8 -name sky130.lib.spice -path '*ngspice*' \
           -print -quit 2>/dev/null)"
fi

if [ -n "$LIB" ]; then
    ok "$LIB"
    PDK_DIR="${LIB%/libs.tech/ngspice/sky130.lib.spice}"
    ok "PDK_ROOT would be: $(dirname "$PDK_DIR")"
else
    bad "sky130.lib.spice not found"
    warn "run setup_wsl.sh to fetch a prebuilt sky130A, or point"
    warn "pdk.MODEL_FILE in sky130_config.json at your own copy"
    FAILED=1
fi

# --------------------------------------------------------------------------
hdr "smoke test"
# --------------------------------------------------------------------------
if [ -n "$NGSPICE" ] && [ -n "$LIB" ]; then
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    cat > "$TMP/smoke.spice" <<EOF
* one operating point on an nfet_01v8
.lib $LIB tt
vd d 0 0.9
vg g 0 0.9
vb b 0 0
xm1 d g 0 b sky130_fd_pr__nfet_01v8 L=0.5 W=5 nf=1
.control
op
print @m.xm1.msky130_fd_pr__nfet_01v8[id] @m.xm1.msky130_fd_pr__nfet_01v8[gm]
+ @m.xm1.msky130_fd_pr__nfet_01v8[vth] @m.xm1.msky130_fd_pr__nfet_01v8[cgg]
+ @m.xm1.msky130_fd_pr__nfet_01v8[vdsat] @m.xm1.msky130_fd_pr__nfet_01v8[gds]
.endc
.end
EOF
    "$NGSPICE" -b "$TMP/smoke.spice" > "$TMP/smoke.log" 2>&1
    if grep -q "\[id\]" "$TMP/smoke.log"; then
        grep -E "^@m" "$TMP/smoke.log" | sed 's/^/       /'
        echo
        echo "       reference (W=5u L=0.5u VGS=VDS=0.9 VSB=0 tt 27C):"
        echo "         id  ~ 1.0095e-04    gm  ~ 6.6289e-04"
        echo "         vth ~ 6.2820e-01    cgg ~ 1.4053e-14"
        ok "ngspice evaluated the sky130 model"
    else
        bad "ngspice could not evaluate the model"
        tail -25 "$TMP/smoke.log" | sed 's/^/       /'
        FAILED=1
    fi
else
    warn "skipped -- need both ngspice and the model library"
fi

# --------------------------------------------------------------------------
hdr "verdict"
# --------------------------------------------------------------------------
if [ "$FAILED" -eq 0 ]; then
    echo "Everything the generator needs is present. Run:"
    echo
    echo "    export PDK_ROOT=$(dirname "${LIB%/libs.tech/ngspice/sky130.lib.spice}")"
    echo "    $PY techsweep_ngspice.py --config sky130_config.json --outdir luts"
    echo "    $PY check_lut.py luts/*.pkl --vds 0.9"
    echo
    echo "(techsweep_ngspice.py auto-discovers the model file; if it doesn't,"
    echo " set pdk.MODEL_FILE in sky130_config.json to:"
    echo "   $LIB )"
else
    echo "Some pieces are missing -- see the FAIL lines above."
    echo "setup_wsl.sh installs whatever you're short of."
fi
exit "$FAILED"
