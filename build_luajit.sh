#!/usr/bin/env bash
# Build LuaJIT 2.1 for the Vita with a host buildvm that agrees with the
# target about struct layout.
#
# vdpm's luajit package does not: its VM reads J->trace 4 bytes past where its
# C library writes it, so the interpreter is fine but entering a compiled
# trace jumps through garbage. See the comment on jit_State.postproc.
#
# LuaJIT's own Makefile cannot be used here because it needs a 32-bit host
# gcc, and this machine has no gcc at all. So the host half (minilua, dynasm,
# buildvm) is built with MSVC x86 and the target half with arm-vita-eabi-gcc.
# The flags below are the ones the Makefile would derive for this target;
# they are read back out of the target compiler rather than hardcoded.
#
#   ./build_luajit.sh          # build, verify, leave the archive in place
#   ./build_luajit.sh install  # also copy over the vdpm archive
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/vendor/luajit-vita-src/src"
# A Windows-style VITASDK (C:/vitasdk) is not a path this shell can prepend.
VITASDK="${VITASDK:-/c/vitasdk}"
case "$VITASDK" in [A-Za-z]:*) VITASDK="/$(echo "${VITASDK%%:*}" | tr 'A-Z' 'a-z')${VITASDK#*:}";; esac
export PATH="$VITASDK/bin:$PATH"
CC=arm-vita-eabi-gcc
VCVARS='C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvarsamd64_x86.bat'

TARGET_FLAGS="-O2 -marm -march=armv7-a -mtune=cortex-a9 -mfpu=neon -mfloat-abi=hard -fno-optimize-sibling-calls -ffunction-sections -fdata-sections"

cd "$SRC"

# --- derive the target's configuration, the way the Makefile does ----------
TESTARCH="$($CC $TARGET_FLAGS -E lj_arch.h -dM -I.)"
has() { grep -q "define $1 $2" <<<"$TESTARCH"; }

DASM_FLAGS="-D ENDIAN_LE"
has LJ_ARCH_BITS 64 && DASM_FLAGS="$DASM_FLAGS -D P64"
has LJ_HASJIT 1     && DASM_FLAGS="$DASM_FLAGS -D JIT"
has LJ_HASFFI 1     && DASM_FLAGS="$DASM_FLAGS -D FFI"
has LJ_DUALNUM 1    && DASM_FLAGS="$DASM_FLAGS -D DUALNUM"
HOST_ARCH="-DLUAJIT_TARGET=LUAJIT_ARCH_ARM -DLUAJIT_OS=LUAJIT_OS_PSP2 -DLUAJIT_USE_SYSMALLOC"
if has LJ_ARCH_HASFPU 1; then
  DASM_FLAGS="$DASM_FLAGS -D FPU"; HOST_ARCH="$HOST_ARCH -DLJ_ARCH_HASFPU=1"
else
  HOST_ARCH="$HOST_ARCH -DLJ_ARCH_HASFPU=0"
fi
if has LJ_ABI_SOFTFP 1; then
  HOST_ARCH="$HOST_ARCH -DLJ_ABI_SOFTFP=1"
else
  DASM_FLAGS="$DASM_FLAGS -D HFABI"; HOST_ARCH="$HOST_ARCH -DLJ_ABI_SOFTFP=0"
fi
if has LJ_NO_UNWIND 1; then
  DASM_FLAGS="$DASM_FLAGS -D NO_UNWIND"; HOST_ARCH="$HOST_ARCH -DLUAJIT_NO_UNWIND"
fi
VER="$(sed -n 's/.*define LJ_ARCH_VERSION \([0-9]*\).*/\1/p' <<<"$TESTARCH")"
DASM_FLAGS="$DASM_FLAGS -D VER=$VER"
echo "dasm flags : $DASM_FLAGS"
echo "host flags : $HOST_ARCH"

# Read the library list out of the Makefile rather than restating it: the
# order matters (it fixes the fast-function numbering in lj_ffdef.h) and the
# list grows between versions - lib_buffer.c is new in 2.1 and omitting it
# left every buffer symbol undefined.
LJLIB_C="$(sed -n '/^LJLIB_O=/,/^LJLIB_C=/p' Makefile | sed 's/^LJLIB_O=//; s/LJLIB_C=.*//; s/\\//g' | tr -s ' \t\n' ' ' | sed 's/\.o/.c/g; s/^ *//; s/ *$//')"
echo "libs       : $LJLIB_C"
for f in $LJLIB_C; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done

# --- host half: minilua, dynasm, buildvm (MSVC x86) ------------------------
cat > build_host.bat <<BAT
@echo off
rem NoDefaultCurrentDirectoryInExePath=1 is set on this machine, which stops
rem cmd running minilua.exe from the current directory. Clear it first.
set NoDefaultCurrentDirectoryInExePath=
call "$VCVARS" >nul || exit /b 1
cl /nologo /c /MD /O2 /W0 /D_CRT_SECURE_NO_DEPRECATE host\\minilua.c /Fo:minilua.obj || exit /b 1
link /nologo /out:minilua.exe minilua.obj || exit /b 1
minilua ..\\dynasm\\dynasm.lua $DASM_FLAGS -o host\\buildvm_arch.h vm_arm.dasc || exit /b 1
cl /nologo /c /MD /O2 /W0 /D_CRT_SECURE_NO_DEPRECATE /I . /I ..\\dynasm $HOST_ARCH host\\buildvm*.c || exit /b 1
link /nologo /out:buildvm.exe buildvm*.obj || exit /b 1
BAT
cmd //c "$(cygpath -w "$SRC/build_host.bat")" || { echo "HOST BUILD FAILED"; exit 1; }
[ -f buildvm.exe ] || { echo "no buildvm.exe"; exit 1; }

# --- generate the VM and the tables ---------------------------------------
./buildvm.exe -m elfasm  -o lj_vm.S
./buildvm.exe -m bcdef   -o lj_bcdef.h   $LJLIB_C
./buildvm.exe -m ffdef   -o lj_ffdef.h   $LJLIB_C
./buildvm.exe -m libdef  -o lj_libdef.h  $LJLIB_C
./buildvm.exe -m recdef  -o lj_recdef.h  $LJLIB_C
./buildvm.exe -m vmdef   -o jit/vmdef.lua $LJLIB_C
./buildvm.exe -m folddef -o lj_folddef.h lj_opt_fold.c

# --- target half -----------------------------------------------------------
rm -f ./*.o libluajit.a
for f in $(ls lj_*.c lib_*.c | grep -v '^lj_mcode_'); do
  $CC -c $TARGET_FLAGS -I. -o "${f%.c}.o" "$f"
done
$CC -c $TARGET_FLAGS -I. -o lj_vm.o lj_vm.S
arm-vita-eabi-ar rcus libluajit.a ./*.o
echo "built $(ls -la libluajit.a | awk '{print $5}') bytes"

# --- verify the VM and the library agree, before this ever reaches a Vita --
echo
echo "=== offset check ==="
cat > /tmp/ljofs.c <<'EOF'
#include "lj_obj.h"
#include "lj_dispatch.h"
#include "lj_jit.h"
char want[(GG_DISP2J + (int)offsetof(jit_State, trace)) + 0x2000];
EOF
$CC -c $TARGET_FLAGS -I. -o /tmp/ljofs.o /tmp/ljofs.c
WANT=$(arm-vita-eabi-nm --print-size /tmp/ljofs.o | awk '{print strtonum("0x"$2)-8192}')
# lj_BC_JLOOP's first instruction is ldr CARG1,[DISPATCH,#DISPATCH_J(trace)];
# a negative-offset ARM ldr encodes as e51 7 0 NNN.
GOT=$(arm-vita-eabi-objdump -d --start-address=$(arm-vita-eabi-nm libluajit.a \
        | awk '/T lj_BC_JLOOP/{print "0x"$1; exit}') libluajit.a 2>/dev/null \
      | grep -oE 'e517[0-9a-f]{4}' | head -1 | sed 's/^e517//')
# Not $(( 0x$GOT )): a leading zero makes bash read it as octal and fail.
GOT=$(python -c "import sys; print(-int(sys.argv[1],16))" "$GOT")
echo "  library expects DISPATCH_J(trace) = $WANT"
echo "  VM encodes      DISPATCH_J(trace) = $GOT"
if [ "$WANT" = "$GOT" ]; then
  echo "  MATCH - the VM and the library agree"
else
  echo "  MISMATCH - do not install this build"; exit 1
fi

if [ "${1:-}" = "install" ]; then
  DEST="$VITASDK/arm-vita-eabi/lib/libluajit-5.1.a"
  [ -f "$DEST.orig-vdpm" ] || cp "$DEST" "$DEST.orig-vdpm"
  cp libluajit.a "$DEST"
  echo "installed over $DEST (stock kept as .orig-vdpm)"
fi
