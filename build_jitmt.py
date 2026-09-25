"""Build a VPK that runs the JIT from several Lua states at once.

The single-threaded jitrun probe passes every stage, but the full game dies
once it leaves the launcher - which is exactly when the audio workers start.
LOVE gives every love.thread its own Lua state, so each one has its own
jit_State compiling its own traces into the one shared mcode pool, through a
VM-domain protection call that acts on the whole process. That is the
condition this reproduces, in something that takes seconds to run instead of
a whole game boot.

Each stage appends to ux0:data/g1r-jitmt.txt and closes the file, so whatever
reached the card names the stage that died.

    python build_jitmt.py --eboot vendor/love-vita/love.self
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
  t.identity = "gen1recomp-jitmt"
  t.window.title = "G1R JIT MT"
  t.window.width = 960
  t.window.height = 544
  t.modules.physics = false
  t.modules.video = false
  t.version = "11.4"
end
"""

# Runs in its own Lua state, with its own jit_State. Deliberately the same
# shape as the chip synth: bit ops plus table reads, the thing the audio
# worker actually does.
WORKER_LUA = r"""
local id, usejit = ...
if not usejit and jit and jit.off then jit.off() end
local bit = require("bit")
local chan = love.thread.getChannel("g1r_jitmt")
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
local acc = 0
for round = 1, 6 do
  if usejit and jit and jit.flush then pcall(jit.flush) end
  acc = acc + mixed(60000)
end
chan:push(("worker %d done jit=%s acc=%.3f"):format(id, tostring(jit and jit.status()), acc))
"""

MAIN_LUA = r"""
local LOG = "ux0:data/g1r-jitmt.txt"
local lines = {}
-- Append, never rewrite. The previous version reopened the file with "w" and
-- wrote the whole log back every time, so a crash anywhere between the
-- truncate and the close left a 0-byte file and lost every line that had
-- already been proven. Appending means whatever reached the card stays there.
do
  local f = io.open(LOG, "w")
  if f then f:close() end
end
local function say(s)
  lines[#lines + 1] = s
  local f = io.open(LOG, "a")
  if f then f:write(s, "\n") f:close() end
end

local WORKER = [==[@WORKER@]==]
local done = false

local function mixed(n)
  local bit = require("bit")
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

-- Three sub-experiments, each naming one factor, cheapest first. Whichever
-- stage the log stops in is the factor that matters:
--   A  main thread alone, twice as many rounds as it ever survived
--      -> if this dies, the trigger is repeated compilation, not threads
--   B  three worker states running interpreted, main compiling
--      -> if this dies, merely having other threads live is enough
--   C  three worker states compiling too
--      -> if only this dies, concurrent compilation is the trigger
local function runWorkers(tag, usejit)
  local threads = {}
  for i = 1, 3 do
    threads[i] = love.thread.newThread(WORKER)
    threads[i]:start(i, usejit)
  end
  say(tag .. ": 3 workers started (jit=" .. tostring(usejit) .. ")")
  for round = 1, 6 do
    if jit and jit.flush then pcall(jit.flush) end
    mixed(60000)
    say(("%s: main round %d ok"):format(tag, round))
  end
  for i = 1, 3 do
    threads[i]:wait()
    local err = threads[i]:getError()
    if err then say(("%s: worker %d ERROR %s"):format(tag, i, tostring(err))) end
  end
  local chan = love.thread.getChannel("g1r_jitmt")
  while true do
    local m = chan:pop()
    if not m then break end
    say(tag .. ": " .. m)
  end
  say(tag .. ": COMPLETE")
end

function love.load()
  say("stage 0: jit=" .. tostring(jit and jit.status()))

  -- Exactly what jitrun stage 5 did, and it passed on this same runtime. If
  -- this fails now, the runtime regressed and nothing below it means anything.
  say("R: regression check - one hot loop, no flush")
  local t0 = love.timer.getTime()
  local ra = mixed(200000)
  say(("R: mixed(200000) %5.0f ms  (acc %.3f)"):format(
    (love.timer.getTime() - t0) * 1000, ra))
  say("R: COMPLETE")

  say("A: main alone, 12 rounds")
  for round = 1, 12 do
    if jit and jit.flush then pcall(jit.flush) end
    local a = mixed(60000)
    say(("A: round %2d ok  (acc %.3f)"):format(round, a))
  end
  say("A: COMPLETE")

  runWorkers("B", false)
  runWorkers("C", true)

  say("ALL STAGES PASSED")
  done = true
end

function love.draw()
  love.graphics.clear(0.05, 0.1, 0.2)
  love.graphics.setColor(1, 1, 1)
  love.graphics.print(done and "done - ux0:data/g1r-jitmt.txt" or "running...", 40, 20)
  for i = math.max(1, #lines - 24), #lines do
    love.graphics.print(lines[i], 40, 40 + (i - math.max(1, #lines - 24)) * 18)
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

    # The worker source is embedded in a long-bracket string, so it must not
    # contain the closing delimiter.
    assert "]==]" not in WORKER_LUA
    main_lua = MAIN_LUA.replace("@WORKER@", WORKER_LUA)
    sources = {"conf.lua": CONF_LUA, "main.lua": main_lua}
    boot.check_syntax(sources)

    love_bytes = io.BytesIO()
    with zipfile.ZipFile(love_bytes, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in sources.items():
            z.writestr(name, text)

    base.TITLE_ID, base.TITLE, base.STITLE, base.ATTRIBUTE2 = \
        "GNRM00001", "G1R JIT MT", "G1R JIT MT", 12
    icon = Image.open(Path(args.engine) / "ports" / "switch" / "assets" / "icon.jpg").convert("RGB")

    base.BUILD.mkdir(parents=True, exist_ok=True)
    vpk = base.BUILD / "gen1recomp-jitmt.vpk"
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
