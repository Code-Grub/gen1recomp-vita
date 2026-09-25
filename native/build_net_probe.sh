#!/bin/sh
# Build the HTTPS reachability probe VPK (native/net_probe.c).
#
# Standalone homebrew, not LOVE: the question it answers (can firmware sceSsl
# negotiate TLS with GitHub) does not need a Lua runtime, and a 37 KB VPK is a
# far cheaper hardware trip than a 32 MB game build.
#
# Needs VitaSDK. TMP/TEMP/TMPDIR are exported because MSYS2 strips them from
# recipe environments and arm-vita-eabi-gcc then cannot create temporaries.
set -e

VITASDK="${VITASDK:-/c/vitasdk}"
BIN="$VITASDK/bin"
OUT="${1:-../build/g1r-net-probe.vpk}"

export TMP="${TMP:-/tmp}" TEMP="${TEMP:-/tmp}" TMPDIR="${TMPDIR:-/tmp}"

cd "$(dirname "$0")"

# -g0 and --gc-sections keep the binary small. vita-elf-create corrupts its
# heap on large relocation counts (see TOOLCHAIN.md), so staying small is not
# only about size.
"$BIN/arm-vita-eabi-gcc" -Wl,-q -O2 -g0 \
	-ffunction-sections -fdata-sections -Wl,--gc-sections \
	net_probe.c -o net_probe.elf \
	-lSceHttp_stub -lSceSsl_stub -lSceNet_stub -lSceNetCtl_stub \
	-lSceSysmodule_stub -lSceRtc_stub -lSceLibKernel_stub

"$BIN/vita-elf-create" net_probe.elf net_probe.velf
"$BIN/vita-make-fself" net_probe.velf eboot.bin

# TITLE_ID is 4 letters + 5 digits, matching build_vpk.py's convention. A
# malformed id installs but can behave oddly, which would waste a trip.
mkdir -p vpk/sce_sys
"$BIN/vita-mksfoex" -s TITLE_ID=GNET00001 "gen1recomp net probe" \
	vpk/sce_sys/param.sfo
cp eboot.bin vpk/eboot.bin

mkdir -p "$(dirname "$OUT")"
(cd vpk && "$BIN/vita-pack-vpk" -s sce_sys/param.sfo -b eboot.bin "../$OUT")

echo "built $OUT"
