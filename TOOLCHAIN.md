# Building a LÖVE runtime for the Vita (LuaJIT 2.1)

isage's prebuilt `11.4-vita` eboot links **LuaJIT 2.0.5** (hyln9/vita-luajit).
This rebuilds the same engine against **LuaJIT 2.1.0-beta3**, the version the
Gen1Recomp engine (and desktop LÖVE 11.4) actually targets, which removes the
`\u{}` rewrite and the bytecode-format split, and should be faster.

Everything below is already done on this machine; it is written down so the
build can be reproduced or repaired.

## Toolchain (once)

| Piece | Where |
|---|---|
| VitaSDK (GCC 15.2.0, arm-vita-eabi) | `C:/vitasdk`, from `vitasdk/autobuilds` release `sdk-snapshot-20260914.739.1`, asset `vitasdk-x86_64-w64-mingw32-*.tar.bz2` (SHA-256 checked against `SHA256SUMS`) |
| `vdpm` package manager | `vendor/vdpm/bin/vdpm.exe`, same release; `vdpm refresh 2026.08` selects the supported channel |
| Vita libraries | `vdpm install luajit sdl2 freetype openal-soft libmodplug mpg123 libvorbis libogg libtheora zlib libpng physfs` (LuaJIT lands as 2.1.0-beta3) |
| MSYS2 build tools | `pacman -S make autoconf automake libtool pkgconf patch` (LÖVE's build is autotools; Git Bash has no `make`) |
| PowerVR driver headers + stubs | PVR_PSP2 v3.9 source `include/{EGL,GLES,GLES2,KHR,gpu_es4}` and `vitasdk_stubs.zip` -> `C:/vitasdk/arm-vita-eabi/{include,lib}` |
| gl4es headers + stub | gl4es4vita `v1.1.4-vita` `include.zip` + `vitasdk_stubs.zip` -> same place |
| SDL2 with the GL backend | SDL2 2.32.8 rebuilt with `-DVIDEO_VITA_PVR=ON` (the vdpm package is built **without** it, so it has no GL at all) and installed over the package |

### Gotchas hit here

- Several vdpm packages ship `.pc` files with a Linux CI prefix
  (`/usr/local/vitasdk/...`) or a literal `$VITASDK`; rewrite `prefix=` in
  `C:/vitasdk/arm-vita-eabi/lib/pkgconfig/*.pc` to `C:/vitasdk/arm-vita-eabi`.
  FreeType also `Requires: libpng`, but only `libpng16.pc` is installed - copy
  it to `libpng.pc`.
- `aclocal` needs SDL2's `sdl2.m4` (from the SDL2 source tree) in
  `/c/msys64/usr/share/aclocal/`, or `platform/vita/automagic` dies on
  `AM_PATH_SDL2`.
- This shell sets `NoDefaultCurrentDirectoryInExePath=1`; a `.bat` that runs
  MSVC builds has to clear it or `cmd` cannot find programs in the cwd.

## The packaging wall: vita-elf-create crashes on big binaries

`vita-elf-create` dies with a heap corruption (`0xC0000374`, exit 127 under
bash) partway through module-info creation. It is **size dependent**, not a
bad relocation or a bad tool build:

| binary | relocations | text | result |
|---|---|---|---|
| `-O2 -g` then `strip --strip-debug` | 192,569 | 0x6afc7c | crashes |
| `-Os -g0` | 160,900 | 0x61631c | converts fine |

Ruled out: tool version (2026 and 2022 builds crash identically), the strip
flag, weak vs regular PowerVR stubs, TLS (none), GOT/PIC relocations (none),
`sceLibcHeapSize`-style symbols, section count (54), and C++ exceptions --
small C++ binaries with the same relocation types convert fine. Upstream has
a similar open report (vitasdk/vita-toolchain#274, godot).

So: **build with `-g0` and keep the binary small** (`-ffunction-sections
-fdata-sections -Wl,--gc-sections`). If a build starts crashing the tool
again, shrink it rather than hunting relocations. The shipped build
(`-O2` + NEON tuning + section GC) lands at 184,126 relocations and converts,
so the wall sits between 184k and 192k on this binary.

## Flags worth copying from Rinnegatamante's ports

His Vita ports (DaedalusX64-vitaGL, lpp-vita) build with:

    -march=armv7-a -mtune=cortex-a9 -mfpu=neon -mfloat-abi=hard
    -fno-optimize-sibling-calls -fno-lto

and set `ATTRIBUTE2=12` in `param.sfo` (`VITA_MKSFOEX_FLAGS "-d ATTRIBUTE2=12"`),
which asks for the extended memory mode -- relevant here because the runtime
requests a 200 MB newlib heap. `build_game_vpk.py --extended-memory` does the
same. He also uses `-ffast-math`, which is deliberately **not** copied: it
changes float semantics across a whole game engine for unclear gain.

## Build

```sh
git clone --depth 1 https://github.com/isage/love vendor/love-vita
cd vendor/love-vita && platform/vita/automagic          # under MSYS2 bash
FLAGS='-O2 -g0 -march=armv7-a -mtune=cortex-a9 -mfpu=neon -mfloat-abi=hard
       -fno-optimize-sibling-calls -ffunction-sections -fdata-sections'
./configure --host=arm-vita-eabi --prefix=$VITASDK/arm-vita-eabi/ \
  --disable-shared --enable-static \
  --with-lua=luajit --with-luaversion=2.1 \
  --disable-library-enet --disable-library-luasocket \
  CFLAGS="$FLAGS" CXXFLAGS="$FLAGS" LDFLAGS='-Wl,--gc-sections'
make -j12
vita-elf-create -s ./src/love ./love.velf && vita-make-fself ./love.velf ./love.self
```

Two build-order traps: libtool records link flags inside `src/liblove.la`, so
after changing `.pc` files delete `src/liblove.la src/.libs/liblove.*` or the
stale flags come back; and `-lpthread` must appear exactly once (it arrives
via LÖVE's own check, and `openal.pc` used to add `-pthread` as well, which
made every pthread symbol a duplicate definition).

`PKG_CONFIG_PATH` and `PKG_CONFIG_LIBDIR` must both point at
`C:/vitasdk/arm-vita-eabi/lib/pkgconfig`, and `C:/vitasdk/bin` must be on PATH.

The resulting `love.self` replaces `eboot.bin` in the VPK
(`build_game_vpk.py --eboot <path>`).
