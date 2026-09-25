"""Build a VPK that finds out WHICH part of the JIT kills the process.

With the mcode pool in place traces finally compile, and the first JIT-enabled
loop crashed. The core dump put the faulting PC at lj_BC_JLOOP+8, the
`ldr TRACE:RC, [CARG1, RC, lsl #2]` that indexes J->trace to enter a trace,
with CARG1 = 1 - so jit_State itself was already corrupt by then. That leaves
three candidates, and nothing in the dump separates them:

  1. recording a trace corrupts memory,
  2. executing compiled code corrupts memory,
  3. the probe's own jit.attach callback (which only ever ran on aborts
     before, because nothing used to compile) is what breaks.

So this runs the stages in order, cheapest first, and appends a marker line to
ux0:data/g1r-jitrun.txt after each one, closing the file every time. Whatever
the last line is names the stage that died. No jit.attach until the end, so a
crash before it clears the callback of blame.

    python build_jitrun.py --eboot vendor/love-vita/love.self
"""

import argparse
import io
import zipfile
from pathlib import Path

from PIL import Image

import build_vpk as base
import build_bootprobe as boot

HERE = Path(__file__).resolve().parent

CONF_LUA = r"""
function love.conf(t)
  t.identity = "gen1recomp-jitrun"
  t.window.title = "G1R JIT Run"
  t.window.width = 960
  t.window.height = 544
  t.modules.physics = false
  t.modules.video = false
  t.version = "11.4"
end
"""

MAIN_LUA = r"""
local LOG = "ux0:data/g1r-jitrun.txt"
local lines = {}
-- Reopen and rewrite every time: if the next stage takes the process down,
-- whatever reached the card is still on the card.
local function say(s)
  lines[#lines + 1] = s
  local f = io.open(LOG, "w")
  if f then f:write(table.concat(lines, "\n"), "\n") f:close() end
end

-- Plain number crunching: no bit ops, no table reads, no upvalues. The
-- simplest thing LuaJIT will compile, so a crash here is not about some
-- exotic instruction.
local function numeric(n)
  local acc = 0.0
  for i = 1, n do
    acc = acc + i * 0.5
    if acc > 1e12 then acc = acc - 1e12 end
  end
  return acc
end

-- What the chip synth actually does: bit ops plus table indexing.
local bit = require("bit")
local function mixed(n)
  local band, bxor, rshift, lshift, bor = bit.band, bit.bxor, bit.rshift, bit.lshift, bit.bor
  local floor = math.floor
  local wave = {}
  for i = 0, 31 do wave[i] = (i % 16) / 15 end
  local lfsr, acc, ph = 0x7fff, 0, 0
  for _ = 1, n do
    ph = ph + 0.0371
    if ph >= 32 then ph = ph - 32 end
    local fb = band(bxor(lfsr, rshift(lfsr, 1)), 1)
    lfsr = bor(rshift(lfsr, 1), lshift(fb, 14))
    acc = acc + wave[floor(ph)] * 0.25 + band(lfsr, 1) * 0.1
  end
  return acc
end

local done = false

local function ms(t0)
  return (love.timer.getTime() - t0) * 1000
end

function love.load()
  say("stage 0: booted, jit.status=" .. tostring(jit.status()))
  say("stage 0: " .. tostring(jit.version) .. " on " .. tostring(jit.arch))

  -- Interpreter baseline. This already worked before the pool existed, so if
  -- it dies now the pool broke something unrelated to compiling.
  jit.off()
  jit.flush()
  local t0 = love.timer.getTime()
  local a = numeric(200000)
  say(("stage 1: interpreter numeric  %6.0f ms  (acc %.3f)"):format(ms(t0), a))

  t0 = love.timer.getTime()
  a = mixed(200000)
  say(("stage 2: interpreter mixed    %6.0f ms  (acc %.3f)"):format(ms(t0), a))

  -- Record traces but never enter one: hotloop is how many iterations a loop
  -- runs before LuaJIT records it, and 2 iterations past that is enough to
  -- compile without looping back into the finished trace many times.
  say("stage 3: jit.on, recording only (about to compile a trace)")
  jit.on()
  jit.flush()
  pcall(jit.opt.start, "hotloop=4")
  a = numeric(8)
  say(("stage 3: survived recording   (acc %.3f)"):format(a))

  -- Now actually run in compiled code, a little at a time, so the marker
  -- says how far it got rather than just "it died".
  for _, n in ipairs({ 100, 1000, 10000, 200000 }) do
    say(("stage 4: entering compiled numeric n=%d"):format(n))
    t0 = love.timer.getTime()
    a = numeric(n)
    say(("stage 4: numeric n=%-6d       %6.0f ms  (acc %.3f)"):format(n, ms(t0), a))
  end

  say("stage 5: entering compiled mixed")
  t0 = love.timer.getTime()
  a = mixed(200000)
  say(("stage 5: mixed                %6.0f ms  (acc %.3f)"):format(ms(t0), a))

  -- Only now bring in the callback the old probe used, to see whether it is
  -- the thing that breaks once traces really do compile.
  say("stage 6: attaching trace callback")
  local starts, stops, aborts = 0, 0, 0
  local ok = pcall(jit.attach, function(what)
    if what == "start" then starts = starts + 1
    elseif what == "stop" then stops = stops + 1
    elseif what == "abort" then aborts = aborts + 1 end
  end, "trace")
  say("stage 6: attach " .. (ok and "ok" or "FAILED"))
  jit.flush()
  a = mixed(200000)
  say(("stage 6: with callback  starts %d stops %d aborts %d"):format(starts, stops, aborts))

  say("ALL STAGES PASSED")
  done = true
end

function love.draw()
  love.graphics.clear(0.05, 0.1, 0.2)
  love.graphics.setColor(1, 1, 1)
  love.graphics.print(done and "done - see ux0:data/g1r-jitrun.txt" or "measuring...", 40, 30)
  for i, line in ipairs(lines) do
    love.graphics.print(line, 40, 55 + i * 18)
  end
end

function love.keypressed() love.event.quit() end
function love.gamepadpressed() love.event.quit() end
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eboot", default="vendor/love-vita/love.self")
    ap.add_argument("--engine", default="C:/g2dev")
    args = ap.parse_args()

    eboot = Path(args.eboot)
    if not eboot.is_file():
        raise SystemExit(f"no such runtime: {eboot}")
    module_zips = {name: base.fetch(name) for name in base.MODULES}

    sources = {"conf.lua": CONF_LUA, "main.lua": MAIN_LUA}
    boot.check_syntax(sources)

    love_bytes = io.BytesIO()
    with zipfile.ZipFile(love_bytes, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in sources.items():
            z.writestr(name, text)

    # Its own title ID and its own marker file: two probes sharing either one
    # produced an ambiguous "it worked" here once already.
    base.TITLE_ID, base.TITLE, base.STITLE, base.ATTRIBUTE2 = \
        "GNRR00001", "G1R JIT Run", "G1R JIT Run", 12
    icon = Image.open(Path(args.engine) / "ports" / "switch" / "assets" / "icon.jpg").convert("RGB")

    base.BUILD.mkdir(parents=True, exist_ok=True)
    vpk = base.BUILD / "gen1recomp-jitrun.vpk"
    with zipfile.ZipFile(vpk, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(eboot, "eboot.bin")
        z.writestr("sce_sys/param.sfo", base.build_sfo())
        z.writestr("sce_sys/icon0.png", base.indexed_png(icon, (128, 128)))
        z.writestr("sce_sys/livearea/contents/bg.png", base.indexed_png(icon, (840, 500)))
        z.writestr("sce_sys/livearea/contents/startup.png", base.indexed_png(icon, (280, 158)))
        z.writestr("sce_sys/livearea/contents/template.xml", base.LIVEAREA_TEMPLATE)
        for zip_name, names in base.MODULES.items():
            with zipfile.ZipFile(module_zips[zip_name]) as mz:
                for n in names:
                    z.writestr(f"module/{n}", mz.read(n))
        z.writestr("game.love", love_bytes.getvalue())
    print(f"wrote {vpk} ({vpk.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
