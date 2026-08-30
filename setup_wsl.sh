#!/usr/bin/env bash
#
# setup_wsl.sh -- install everything the sky130 gm/ID LUT generator needs,
#                 natively in WSL, without Docker.
#
# The generator only needs ngspice, numpy, and the sky130A ngspice models.
# It does NOT need xschem, magic, klayout or anything else from the aicex
# container -- so there is no reason to wait for Docker to work.
#
#   bash setup_wsl.sh
#
# Afterwards, in any new shell:
#
#   source ~/.gmid-sky130-env
#   cd /path/to/gmid-sky130
#   python3 techsweep_ngspice.py --config sky130_config.json --outdir luts
#
set -euo pipefail

VENV="$HOME/.venv-gmid"
ENVFILE="$HOME/.gmid-sky130-env"
PDK_ROOT_DEFAULT="$HOME/.ciel"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
say "1/5  System packages (ngspice + python venv support)"
# --------------------------------------------------------------------------
sudo apt-get update
sudo apt-get install -y ngspice python3-venv python3-pip

ngspice -v 2>&1 | head -2 || die "ngspice did not install correctly"

# --------------------------------------------------------------------------
say "2/5  Python environment at $VENV"
# --------------------------------------------------------------------------
# A venv sidesteps PEP 668 ("externally-managed-environment"), which is what
# recent Ubuntu throws at 'pip install --user'.
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade --quiet pip
"$VENV/bin/pip" install --upgrade --quiet numpy matplotlib ciel

# --------------------------------------------------------------------------
say "3/5  Downloading the sky130A PDK"
# --------------------------------------------------------------------------
echo "This pulls a prebuilt open_pdks sky130 from the FOSSi Foundation's"
echo "ciel-releases. Expect a few GB; make sure WSL has the disk space."
echo

export PDK_ROOT="${PDK_ROOT:-$PDK_ROOT_DEFAULT}"

LIB="$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice"
if [ -f "$LIB" ]; then
    echo "Already present: $LIB  -- skipping download."
else
    # ls-remote prints one version hash per line, newest first, when piped.
    VERSION="$("$VENV/bin/ciel" ls-remote --pdk-family sky130 | head -1)"
    [ -n "$VERSION" ] || die "Could not get a sky130 version list from ciel. \
Check your network, then run: $VENV/bin/ciel ls-remote --pdk-family sky130"
    echo "Newest available build: $VERSION"
    "$VENV/bin/ciel" enable --pdk-family sky130 --pdk-root "$PDK_ROOT" "$VERSION"
fi

[ -f "$LIB" ] || die "Expected the model library at:
  $LIB
but it is not there. Check what ciel installed:
  ls $PDK_ROOT/sky130A/libs.tech/ngspice/"

echo "Model library: $LIB"

# --------------------------------------------------------------------------
say "4/5  Writing $ENVFILE"
# --------------------------------------------------------------------------
cat > "$ENVFILE" <<EOF
# Sourced to work with the sky130 gm/ID LUT generator.
export PDK_ROOT="$PDK_ROOT"
export PDK=sky130A
export PATH="$VENV/bin:\$PATH"
EOF
echo "Wrote $ENVFILE"
echo "Add it to your shell startup if you like:"
echo "    echo 'source $ENVFILE' >> ~/.bashrc"

# --------------------------------------------------------------------------
say "5/5  Smoke test"
# --------------------------------------------------------------------------
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
.endc
.end
EOF

if ngspice -b "$TMP/smoke.spice" 2>&1 | tee "$TMP/smoke.log" | grep -q "\[id\]"; then
    grep -E "^@m" "$TMP/smoke.log" || true
    echo
    echo "Expected roughly: id ~ 100 uA, gm ~ 660 uA/V, vth ~ 0.63 V, cgg ~ 14 fF"
    echo "(W = 5 um, L = 0.5 um, VGS = VDS = 0.9 V, VSB = 0, tt, 27 C)"
else
    tail -30 "$TMP/smoke.log"
    die "ngspice could not evaluate the sky130 model. See the output above."
fi

say "Done"
cat <<EOF
Next:

    source $ENVFILE
    cd /path/to/gmid-sky130
    python3 techsweep_ngspice.py --config sky130_config.json --outdir luts
    python3 check_lut.py luts/*.pkl --vds 0.9

Roughly 10 s per device; five devices in under a minute.
EOF
