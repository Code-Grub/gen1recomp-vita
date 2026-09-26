# Draft issue for bryanthaboi/gen1recomp

Posted as https://github.com/bryanthaboi/gen1recomp/issues/2490 on 2026-09-25.
Kept here as the record of what was sent.

---

**Title:** PS Vita port is playable: guidance wanted on upstreaming it

---

I have a PS Vita port of the engine running on hardware and would like to know
whether you want it in the project, and in what shape, before I build any CI
scaffolding you might want structured differently.

Repo: https://github.com/Code-Grub/gen1recomp-vita (scripts, patches and docs
only; no engine code, no ROM data)

## State

Engine **v0.3.14** boots and plays on a real PCH-1000 at the panel's native
960x544, Gen 1 and Gen 2. Gen 3 is present in v0.3.14 but untested here.

The port ships its own LOVE runtime rather than a prebuilt one: LOVE 11.x built
from the `love-vita` tree with SDL2's PVR backend plus gl4es, packaged as a VPK.
The GPU user modules are fetched at build time with pinned SHA-256, the same way
`scripts/switch/love-nx-11.5-nx1.sha256` pins yours. No ROM or ROM-derived data
is in the repo or in a built VPK; the player supplies their own.

Mods install from a zip inbox at `imports/mods/`, which is the path
`RomImporter` already implements for NX, so that part needed no new engine code.

## Two LuaJIT fixes that may be worth having regardless

These are in `patches/` with a README explaining each hunk, and both are upstream
bugs rather than port glue:

1. **Executable memory placement.** LuaJIT on ARM needs mcode within
   `LJ_TARGET_JUMPRANGE`, and reaches it by passing an address hint.
   `sceKernelAllocMemBlockForVM` takes only a name and a size, so the hint
   cannot be honoured and every trace aborted with `MCODEAL`: measured 5404
   traces started, 0 compiled, while leaving the compiler nominally on cost
   about 5x because it retried forever. Fixed by reserving a VM pool before
   the newlib heap and carving mcode areas from it, which also fixes the
   kernel's 1 MB floor on VM blocks against LuaJIT's 32 KB `sizemcode`.

2. **A miscompiled LuaJIT package.** With placement fixed, traces compiled and
   the process then died. From a core dump: faulting PC `lj_BC_JLOOP+8`, the
   `ldr` that enters a trace, with `CARG1 = 1`. Cause is that the assembled VM
   and the C library disagree about `sizeof(jit_State)` by 4 bytes, so
   `GG_DISP2J` is -2476 in the VM against -2480 in the library and `J->trace`
   reads `freetrace`. `offsetof(jit_State, trace)` agrees at 268 and all three
   `DISPATCH_GL` offsets match, which is why the interpreter and the whole game
   were flawless and only JIT entry broke.

Happy to send these as their own PR whether or not the port is wanted.

## Known limitations, stated plainly

- **The JIT compiler ships off.** It compiles correctly, but trace assembly on a
  444 MHz in-order Cortex-A9 is too slow to do in bursts: every multi-second
  stall landed on LuaJIT taking new mcode areas, about 28 KB of machine code per
  4.5 s. Flush storms, an uncached pool, newlib malloc and
  `sceKernelSyncVMDomain` were each ruled out by measurement. Traces already
  compiled keep running after `jit.off()`, so there is a `warm(fn, ...)` opt-in
  for code that wants to pay once at load.
- **No networking.** The mod index, mod downloads and the self-updater do not
  work. `HostShell`'s desktop transport shells out to curl, which the console
  cannot run, and the firmware's own TLS cannot reach GitHub: it has no ECDSA
  support at all (measured: `ecc256`/`ecc384` fail the handshake while
  `rsa2048`/`rsa4096`/`sha512`/`cbc`/`3des` and TLS 1.2 all succeed), and GitHub
  Pages serves ECDSA certificates. `api.github.com` and `codeload.github.com`
  reach certificate evaluation and fail only on `UNKNOWN_CA`.
- **Voxel mods do not work.** Their mesh builder holds a growing live working
  set: 109.9 MB still live after a forced full GC on hardware with only ~3 MB
  collectable, and ~536 MB peak for a full desktop bake, against a 200 MB heap.
- **One device, and no emulator.** Vita3K cannot run this runtime at all: its GL
  stack fails `module_start`, so hardware is the only way to test.
- Bytecode precompiling exists but is off, because loading LuaJIT bytecode while
  the collector runs crashes intermittently on both 2.0 and 2.1.

## Why I am asking rather than shipping

I published a release from my own repo, then read `LICENSE.MD` properly and took
it straight back down. Additional Term 2 makes the Launcher proprietary and
permits distributing it with the program "only in unmodified official releases
from BOIS CLUB GAMES, LLC", and a built VPK necessarily contains
`LauncherView.lua`, `LauncherSettings.lua`, `OnlinePanel.lua`,
`CartLabelArt.lua`, `CartShape.lua`, `LauncherMods.lua`, `RomImporter.lua`,
`assets/launcher/` and `assets/labels/`. So a third-party Vita build cannot be
distributed as a playable artifact at all, which is exactly why upstreaming is
the only sensible route.

## Questions

1. Do you want a Vita port in the project?
2. Should it mirror the Switch layout: `scripts/build_vita.sh` plus
   `scripts/vita/`, `docs/vita-build.md` and `docs/vita-install.md`, and a
   `vita:` job in `release.yml`?
3. CI: the Switch job runs on a self-hosted macOS runner for devkitPro. A Vita
   job needs VitaSDK. Worth noting that `vita-elf-create` corrupts its heap on
   large binaries in a size-dependent way, so the build has to keep the binary
   small with `-g0` and `--gc-sections`. Is a hosted runner acceptable, or would
   you want this path-gated rather than hard-failing like Switch?
4. `Platform.lua`: this port deliberately reports `getOS() == "Linux"`, which is
   load-bearing because `Performance.detect()` uses it to pick the `low` tier.
   Would you prefer a `vita` arm, or to leave it reporting Linux?
5. Would you take the two LuaJIT patches separately if the port itself is not
   something you want to carry?

Either answer is useful. If you would rather not take it, I will keep the repo
source-only so people build their own VPK from their own checkout, and add the
Term 1 credit to its README.
