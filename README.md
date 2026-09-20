# Gen1Recomp on PS Vita (experimental)

Runs the Gen1Recomp engine on isage's LÖVE 11.4 Vita port
(https://github.com/isage/love, release `11.4-vita`). No VitaSDK needed.

## Build

```
python build_game_vpk.py --engine C:/g2dev     # the game  -> build/gen1recomp-vita.vpk
python build_vpk.py --engine C:/g2dev          # the probe -> build/gen1recomp-vita-probe.vpk
```

The eboot and the GPU driver modules are downloaded into `vendor/` and checked
against pinned SHA-256s. `game.love` contains the desktop release's file set
(`scripts/pack_love.sh`) plus the `vita/conf.lua` shim, which only activates on
the Vita runtime and sets LÖVE 11.4, a fixed 960x544 window, and the engine's
own handheld profile: `POKEPORT_HANDHELD` (pad-driven launcher, in-game ROM
browser, on-screen keyboard, handheld frame pacing), 22050 Hz music
(`--audio-rate ''` keeps 44100), the idle render governor (6 fps after 10 s
idle) and no Discord presence. Same knobs `build-linux-arm-sbc.sh` exports
for the ARM handheld build. No ROM or ROM-derived data is packaged.

Measured at those settings (desktop, `low` tier): the base game submits ~25
draws + ~15 sprite batches, 4 canvas and 4 shader switches per frame, and
renders into a 320x182 canvas, not the full 960x544 -- so neither draw-call
count nor fill rate should bind on the Vita. Lua CPU is under 1 ms/frame
even with the JIT off, so the base game's frame cost is not Lua-bound
either. The audio synth and startup parsing are the two real CPU costs.

### LuaJIT 2.0 (the Vita runtime is 2.0.5, desktop LÖVE is 2.1)

- `\u{XXXX}` string escapes are 2.1-only. The build rewrites them to the same
  UTF-8 bytes (`build/lua20/`); today that is one line in
  `src/ui/gen2/BattleState.lua`, which otherwise fails to load on the Vita
  and takes every Gen 2 battle down with it. (Fixed upstream in this
  checkout by commit 5732d36c; the rewrite still covers older engines.)
- `--bytecode` ships precompiled chunks instead of source, which skips
  parsing ~11 MB at startup (141 ms -> 14 ms on a desktop, tens of times
  more on the Vita's CPU). **Off by default**: loading bytecode while the
  collector runs crashes LuaJIT intermittently on the desktop -- source +
  GC survived 4/4 runs, bytecode + GC crashed 3/4 under 2.0 and 1/4 under
  2.1, so it is an upstream bug, not a 2.0 one. The probe VPK carries 80
  real engine chunks and loads them with the GC stepping; if that section
  finishes on hardware, this flag is safe to use.
  Needs a LuaJIT 2.0 host: `vendor/luajit-2.0-src/.../src/msvcbuild.bat
  static` from a VS x64 prompt (LuaJIT `v2.0` branch @ 3000f7c; dump format
  1, same as 2.0.5). Desktop LÖVE (2.1) cannot load those chunks, so test
  desktop builds without the flag.

### Building a LuaJIT 2.1 runtime

isage's prebuilt eboot links LuaJIT **2.0.5**. `TOOLCHAIN.md` documents the
VitaSDK setup here and rebuilds the same LÖVE against LuaJIT **2.1.0-beta3**
(what the engine and desktop LÖVE 11.4 target), then:

```
python build_game_vpk.py --engine C:/g2dev --eboot vendor/love-vita/love.self
```

## Install

1. HENkaku settings: turn on **Enable Unsafe Homebrew** (the eboot requires it).
   Recommended: a per-app clock profile (e.g. the PSVshell plugin) at 444 MHz
   CPU / 222 MHz GPU. The eboot never raises the clocks itself, and apps
   otherwise run at the 333 MHz default.
2. Install `gen1recomp-vita.vpk` with VitaShell.
3. Copy your own `.gb`/`.gbc` dump to `ux0:data/love2d/love/pokemon-love2d/`
   (create the folder if the game has not been launched yet).
4. Launch Gen1Recomp and press Choose on the cart: the file picker is
   unavailable on the Vita, so the importer falls back to that folder.

Crash logs: `ux0:data/SDL_Log.txt`.

## Status

- Verified on desktop LÖVE 11.4 in OpenGL ES mode with the shim forced on
  (`VITA_SHIM_FORCE=1`): boots, imports, reaches the overworld; all 11 engine
  shaders compile as GLSL ES 1.00.
- Vita3K cannot run it: the PowerVR driver (`libgpu_es4_ext.suprx`) needs
  kernel functions the emulator does not implement.
- Not yet run on real hardware. Run the probe VPK first; it reports whether
  the GPU driver works, whether the JIT is on, and frame times.
