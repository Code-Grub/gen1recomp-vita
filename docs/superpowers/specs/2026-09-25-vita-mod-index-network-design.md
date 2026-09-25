# Mod index networking on PS Vita

Design, 2026-09-25.

## Goal

Make the in-game mod index work on the Vita build: browse
`gen1recomp-mod-index` and install a mod from it, over Wi-Fi, with no PC
involved.

Today every attempt fails with `no network transport on this platform`.

## Non-goals

- Voxel mod performance. This spec only makes mods *installable*. Whether a
  voxel mod then runs at a playable frame rate is a separate, already-parked
  question (4-10x vertex gap, measured on desktop).
- Save sync and signed requests (`love.system.httpRequest`). Not needed by the
  index; see Phase 2.
- Conditional GET. The bridge contract has no ETag hook, so an index refresh
  re-downloads in full. Acceptable at the current index size.
- Restoring the launcher's network UI on other platforms. Nothing outside the
  Vita changes behaviour.

## Background: why there is no transport

Everything funnels through one gate:

```
HostShell.canFetch() = haveCurl() or haveBridge()
```

`haveCurl()` is false on the Vita: the desktop transport is `io.popen` plus a
`curl` binary (`src/core/HostShell.lua:572`), and the console has neither
process spawning nor curl.

`haveBridge()` (`src/core/HostShell.lua:463`) is the second, non-curl transport,
added for mobile. Its own comment names our exact situation:

> UWP is listed because Xbox has no curl and no way to spawn one
> (`Platform.canSpawnProcess` is false there), so the bridge is its only
> possible transport (#876).

So the bridge is the sanctioned path, not a workaround. Two entry points exist:

| Function | Signature | Used by |
| --- | --- | --- |
| `love.system.httpDownload` | `(url, absPath, userAgent, accept)` -> truthy | index fetch, zip download |
| `love.system.httpRequest` | `(url, method, fields, body, userAgent)` -> envelope | signed save-sync only |

`HostShell.httpGet` is implemented *on top of* `httpDownload` (fetch to
`http_fetch.tmp` in the save dir, read, remove; `src/core/HostShell.lua:618`).

**Therefore one C function, `httpDownload`, is enough to light up index
browsing and mod installation.** That is Phase 1.

### The gate cannot see this console

`haveBridge()` requires `love.system.getOS()` to be `Android`, `iOS` or `UWP`.
Our eboot defines `LOVE_LINUX`, so `getOS()` returns `"Linux"`, which is
indistinguishable from desktop Linux where the gate must stay false. A small
engine-side change is therefore unavoidable.

That change is smaller than it first appears, because **the engine already has
a capability-probe convention for exactly this** and `haveBridge()` is the
outlier. `src/core/Platform.lua:15` computes

```lua
local nativeHttp = love and love.system
  and type(love.system.httpDownload) == "function"
```

with no OS allowlist at all, and feeds it into
`canFetchRemote = (not nx and not uwp) or nativeHttp`. So the fix is to align
`haveBridge()` with `Platform.lua` rather than to invent a new marker.

Note that `NX` is absent from the allowlist too, so a Switch build that grew a
bridge would still be refused by `HostShell`. Aligning the two helps that port
as well, which strengthens the case for sending this upstream.

### A second, independent bug: the two gates disagree

On this console `getOS()` is `"Linux"`, so `nx` and `uwp` are both false and
`Platform.canFetchRemote()` returns **true**, because the expression assumes
"not a console" implies "desktop, therefore curl". Meanwhile
`HostShell.canFetch()` returns **false**. The launcher consequently offers the
mod catalog and then fails every fetch with
`no network transport on this platform`.

This is worth fixing on its own merits, independently of whether the transport
below ever ships: today the Vita build advertises a capability it does not
have. Fix by making `canFetchRemote` depend on an actual transport rather than
on not being a known console.

### Why a Lua-side monkey patch cannot work

Patching `HostShell` from `vita/conf.lua` would appear to work in the launcher
and fail on every real download. `src/net/fetch_worker.lua` runs in a fresh
`love.thread` and pulls `HostShell` in through its own `loadModule`, so a
main-thread patch never reaches the workers that perform the fetches. Rejected.

## What the other console ports do

Worth knowing before committing to a TLS stack: **no gen1recomp console port has
in-app HTTPS today.** We would be first.

**Switch (NX)**, the only shipped console port (runtime
`retronx-team/love-nx`), does not network from inside LOVE at all:

- remote mod browsing is off by policy: "Remote **FIND MODS** / GitHub download
  stays **off** on Switch" (`docs/switch-install.md:172`)
- the LOVE self-updater is disabled (`networkValidated == false`)
- OTA runs in a **separate native launcher NRO** built on libnx plus curl,
  outside LOVE. It fetches the install zip, verifies SHA-256 against
  `sha256sums.txt`, replaces both NROs, then `envSetNextLoad`s into the game
  (`src/update/SwitchOta.lua`)
- mods install from a **zip inbox** at `<savedir>/imports/mods/`, followed by
  MODS then Scan again. This is engine code, not Switch-specific:
  `MODS_INBOX_DIR = "imports/mods"` (`src/import/RomImporter.lua:451`), the
  rescan at `:767`, and an explicit branch at `:2429` reading "NX: no
  HostShell/desktop picker, rescan imports/mods/ inbox instead"

**Xbox (UWP)** has no transport either. `Platform.lua:36`: "The UWP LOVE backend
does not export that bridge yet, so this still resolves false on Xbox and the
launcher still says so."

**Android and iOS** are the bridge precedent this design follows: Android via a
GameActivity JNI `HttpsURLConnection` bridge, iOS via
`GRPickerBridge.httpDownload` over `URLSession`, both surfaced as
`love.system.httpDownload` (`docs/updater.md:171-177`).

### Alternative considered: a separate native process

The Switch model (do the networking in a companion homebrew that hands off to
the game) is proven and avoids TLS inside LOVE entirely. Rejected here for two
reasons. First, it cannot serve the actual goal: a separate updater can fetch a
zip, but browsing an index and installing from it is an in-game UI flow, and
that UI reaches the network through `HostShell`. Second, its main advantage does
not apply to us. The Switch port consumes a pinned upstream runtime it does not
build, so patching LOVE was not an option there; we already build and patch our
own runtime, and already carry native patches under `vita-probe/patches/`. The
marginal cost of adding one more is low.

### The inbox is a free fallback, and it is not a substitute

The `imports/mods/` inbox costs nothing on this port: it is plain engine code
with no NX hardware dependency, and the save directory is already the writable
root. It gives mod installation with no C written, and it is what the shipped
console port does.

It does not give index browsing, which is the goal here, so it does not replace
this work. It is valuable as a de-risking step: it proves install-and-enable
works on this console before TLS is in the picture, which separates two failure
modes that would otherwise be diagnosed together. Hence probe step 0 below.

## Security posture

The relevant fact, verified in the engine source: **mod zips are never
digest-checked.** `installFromRelease` calls `ModUpdate.downloadZip`, whose only
validation is `info.size ~= 0` (`src/mods/ModUpdate.lua`). The index format does
carry a `sha256` pin (`src/mods/ModIndex.lua:253`) but no mod code path consumes
it; only `RomImporter` verifies digests, and only for carts.

A mod is executable Lua. So on any platform, TLS is the only thing standing
between the index and arbitrary code execution. Disabling certificate
verification outright would remove that entirely.

The SDK makes a better posture nearly free. `psp2/net/http.h` exposes:

- `sceHttpsLoadCert(caCertNum, caList, cert, privKey)`: load **our own** CA
  list, instead of depending on the firmware's ageing store
- `sceHttpsDisableOption(flags)` with per-check granularity:
  `SERVER_VERIFY`, `CN_CHECK`, `NOT_AFTER_CHECK`, `NOT_BEFORE_CHECK`,
  `KNOWN_CA_CHECK`
- `sceHttpsSetSslCallback(id, cb, userArg)` where
  `SceHttpsCallback(verifyEsrr, sslCert[], certNum, userArg)` receives a bitmask
  of which checks failed

Adopted posture:

1. **Ship our own roots.** Embed the DER roots behind
   `*.github.io`, `raw.githubusercontent.com`, `codeload.github.com` and
   `objects.githubusercontent.com` (DigiCert Global Root G2/G3 and the Sectigo
   root currently in those chains), and install them with `sceHttpsLoadCert`.
   A few KB. Signature and chain verification stay real.
2. **Relax only the clock-dependent checks.** Disable `NOT_BEFORE_CHECK` and
   `NOT_AFTER_CHECK`. This is deliberate: the test console has been off its
   charger for extended periods, so its RTC is expected to be wrong, and a
   skewed clock would otherwise reject a perfectly good certificate. Wrong
   dates are the failure mode we cannot fix from inside the app; a forged
   signature is one we can still catch.
3. **Keep `SERVER_VERIFY` on** and use the callback only to accept a chain whose
   *sole* complaint is a date bit. If `verifyEsrr` reports anything else, reject.
4. **Pin roots, not leaves.** GitHub rotates leaf and intermediate
   certificates; pinning either would break the console on a schedule we do not
   control. Root SPKI is stable for years.

Note for implementation: `sslCert[]` arrives as `void *const[]`, and this SDK
ships no `psp2/net/ssl.h` (only `libSceSsl_stub.a`). So the certificates are
opaque without hand-declared prototypes. `verifyEsrr` is the part we rely on;
do not design around inspecting the chain.

Residual risk, accepted and recorded: if a root we ship is ever compromised, or
GitHub moves to a chain rooted outside our embedded set, index fetches fail
closed (no transport) rather than silently accepting a forgery. Failing closed
is the intended behaviour.

## Design

Three pieces.

### 1. Native transport in the LOVE fork

New source in the `love-vita` checkout, exposed through the `love.system`
module so that `require("love.system")` inside a fetch worker thread sees it.
Delivered as a patch under `vita-probe/patches/`, alongside the existing
`love-vita-psp2.patch`, because `vendor/` is gitignored.

Responsibilities:

- one-time global init, behind a once-guard (see Threading):
  `sceSysmoduleLoadModule` for `NET`, `HTTP`, `SSL`; `sceNetInit` with its
  memory pool; `sceNetCtlInit`; `sceSslInit`; `sceHttpInit`; then
  `sceHttpsLoadCert` and the option flags from Security posture
- `httpDownload(url, absPath, userAgent, accept)`:
  create template (`sceHttpCreateTemplate`), enable
  `sceHttpSetAutoRedirect` with a bounded hop count, create connection and
  request, `sceHttpSendRequest`, check `sceHttpGetStatusCode` is 2xx, then
  stream `sceHttpReadData` to `absPath` in fixed-size chunks
- write to a temporary path and rename on success, so a truncated transfer
  never leaves a file that passes `downloadZip`'s non-empty check
- free every handle on every path, including errors

Return contract: truthy on success. On failure return falsy plus a message; the
engine already surfaces it as a failed fetch.

### 2. The engine gate

Two small changes to engine Lua, both narrowing gates to the capability rather
than to a platform name:

- `src/core/HostShell.lua`, `haveBridge()` and `haveRequestBridge()`: accept the
  transport when the function exists, matching what `Platform.lua:15` already
  does, instead of requiring an OS-name match. `getOS()` keeps returning
  `"Linux"`, so `Performance.detect()` continues to select the `low` tier and
  every other Linux-conditioned path is untouched.
- `src/core/Platform.lua`, `canFetchRemote`: stop inferring a transport from not
  being a known console, so the launcher cannot advertise a catalog it cannot
  reach. See "the two gates disagree" above.

Both are candidates to send upstream. The `#876` comment shows the maintainers
already wanted a curl-less console to reach the bridge, and the second is a bug
on any platform that is neither desktop nor a recognised console.

The changes ship the same way the `\u{}` rewrite does, through
`build_game_vpk.py`, so the engine checkout at `C:/g2dev` stays pristine.

### 3. Packaging

No new packaging path. The index install flow writes through `CacheFs` into the
save directory, which is already the writable root on this console.

## Threading model

`fetch_worker.lua` runs `httpDownload` from a background `love.thread`, three
workers in the pool. Consequences, and they are the main correctness risk:

- global init must happen exactly once across all threads. Guard with a mutex
  and a done flag; do not rely on the main thread having gone first, because
  `Fetch` starts workers lazily.
- template, connection and request objects are created, used and freed entirely
  within the calling thread. Nothing is shared between workers.
- `sceHttpInit` pool size must cover three concurrent transfers.
- `sceNetInit`'s pool is allocated once and never freed for the process
  lifetime.

If threads turn out not to work on this runtime, `Fetch` degrades to
`background threads unavailable` (`src/net/Fetch.lua:48`) and the whole feature
is dead regardless. Verify this first; see Verification.

## Error mapping

`sceHttp` returns numeric errors that mean nothing to a player. Map to the
shapes the launcher already renders:

| Condition | Surfaced as |
| --- | --- |
| no Wi-Fi association | `no network connection` |
| DNS or connect failure | `could not reach <host>` |
| TLS verification failure | `certificate rejected for <host>` |
| non-2xx status | existing `fetchError(url, status, body)` shape |
| short read or rename failure | `download failed` |

Include the raw `sce` error code in the log line but not in the player-facing
string.

## Verification

Vita3K cannot run this runtime at all (its GL stack fails module start), so
every check below is on hardware, a PCH-1000 over VitaShell FTP.

Ordered so that the cheapest disqualifying result comes first:

0. **The inbox, no build required.** FTP a small non-voxel mod zip into
   `<savedir>/imports/mods/`, then MODS and Scan again in the launcher. Costs
   one FTP transfer and no code. Proves install-and-enable works on this console
   before any transport exists, so a later network failure cannot be confused
   with a broken install path. If this fails, fix it first: every remote install
   ends in the same `installZip` call.

1. **Threads: already answered, no trip needed.** `patches/README.md` records
   that `modules/thread/LuaThread.cpp` needed `jit.off()` in every worker state,
   "measured by elimination: one state compiling repeatedly is fine, three more
   running interpreted alongside it are fine, but the moment those states compile
   too the process dies." Three concurrent worker Lua states on hardware means
   `love.thread` works on this port. Note the consequence for this design: the
   fetch workers will run interpreted, which is fine because they block on
   `sceHttp` rather than on Lua.
2. **Connectivity and TLS. Built: `native/net_probe.c`,
   `native/build_net_probe.sh`, output `build/g1r-net-probe.vpk` (37 KB).**
   Standalone homebrew rather than a LOVE build, because the question needs no
   Lua runtime and a 37 KB VPK is a far cheaper trip than a 32 MB game build.
   It writes `ux0:data/g1r-net.txt` and climbs a ladder that separates the two
   failure modes leading to opposite decisions: a plain-HTTP control, then HTTPS
   with firmware defaults, then with only the date checks disabled, then with
   server verification off, then the same against `raw.githubusercontent.com`.
   Passing only the last rung means the handshake is fine and just chain
   validation failed, so shipping our own roots works. Failing the last rung
   means `sceSsl` cannot reach GitHub and this design is dead. The report ends
   with a written key for reading exactly that.

   The feed probed is
   `https://bryanthaboi.github.io/gen1recomp-mod-index/data/index.json` plus the
   `raw.githubusercontent.com` equivalent. Point it at the `Code-Grub` fork's
   Pages URL instead by editing `URL_PAGES` and rebuilding. The report is
   written from C because Lua file writes do not work inside the running game on
   this port, only in standalone probes.

3. **Clock skew.** Folded into step 2: the probe reports the console's UTC clock
   before any request, so a date-check failure can be attributed immediately.
4. **Concurrency.** Three simultaneous downloads, matching the worker pool.
   Deferred until the transport exists; it cannot be probed standalone.
5. **End to end in game.** Add the index in the launcher, browse it, install one
   small mod, confirm it appears in the mod list after a relaunch. Use a small
   non-voxel mod for this, not a voxel mod: it separates "install works" from
   "voxel mods are playable".

Keep the existing rule of one change per hardware test.

## Risks and open questions

- **Firmware TLS versus GitHub.** The largest unknown. GitHub requires TLS 1.2
  with modern cipher suites. If firmware `sceSsl` cannot negotiate, no amount of
  certificate work helps, and the fallback is a different stack (vdpm `curl`
  plus `mbedtls`, neither currently installed in this VitaSDK) or an HTTP
  mirror. Probe step 2 settles it, and it is the reason step 2 comes early.
- ~~Threads on this runtime.~~ Resolved before probing: three concurrent worker
  Lua states are already measured on hardware (`patches/README.md`, the
  `LuaThread.cpp` entry). The fetch pool will run interpreted, which is fine for
  I/O-bound work.
- **Index size.** 157 mods today with no conditional GET, so every refresh is a
  full download. Measure it; if it is large, revisit.
- **Root set drift.** Shipping roots means a maintenance obligation. Record
  which roots are embedded and why, so a future failure is diagnosable.

## Phase 2, not now

`love.system.httpRequest` for `canHttpRequest()`, which unlocks signed requests
and save sync. Same transport, plus method and header support and the
`STATUS <n>\n<body>` envelope. Deliberately deferred: the index does not need
it, and Phase 1 is the thing worth proving on hardware first.
