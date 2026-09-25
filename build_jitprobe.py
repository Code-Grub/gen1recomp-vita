"""Build a VPK that answers one question: is LuaJIT actually compiling traces?

On hardware jit.status() reports true, yet Lua runs ~250x slower than a
desktop with the JIT off - as if every trace were being thrown away. LuaJIT
aborts traces silently when it cannot get executable memory (LJ_TRERR_MCODEAL),
which looks exactly like this. This probe attaches to LuaJIT's trace events,
counts starts/stops/aborts with their reasons, and times the same loop with
the JIT on and off. If the two timings match, the JIT is doing nothing.

    python build_jitprobe.py --eboot vendor/love-vita/love.self
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
  t.identity = "gen1recomp-jitprobe"
  t.window.title = "G1R JIT Probe"
  t.window.width = 960
  t.window.height = 544
  t.modules.physics = false
  t.modules.video = false
  t.version = "11.4"
end
"""

MAIN_LUA = r"""
local out = {}
local function say(s)
  out[#out + 1] = s
  local f = io.open("ux0:data/g1r-jit.txt", "w")
  if f then f:write(table.concat(out, "\n"), "\n") f:close() end
end

-- The same shape of loop as the chip synth: float math plus bit ops.
local bit = require("bit")
local function work(n)
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

local phase, done = 0, false

local starts, stops, aborts, reasons = 0, 0, 0, {}

function love.load()
  say("jit.status: " .. tostring(jit.status()))
  local st = { jit.status() }
  say("jit flags: " .. table.concat(st, " ", 2))
  say("jit.version: " .. tostring(jit.version))
  say("jit.arch/os: " .. tostring(jit.arch) .. " / " .. tostring(jit.os))
  for _, m in ipairs({ "jit.util", "jit.v", "jit.dump", "jit.opt" }) do
    local ok, mod = pcall(require, m)
    say(("module %-9s %s"):format(m, ok and "available" or ("missing (" .. tostring(mod):sub(1, 50) .. ")")))
  end

  -- Count what the compiler does. "abort" carries the reason as the error
  -- message in LuaJIT's own trace callback.
  starts, stops, aborts, reasons = 0, 0, 0, {}
  local okattach = pcall(jit.attach, function(what, tr, func, pc, otr, oex)
    if what == "start" then starts = starts + 1
    elseif what == "stop" then stops = stops + 1
    elseif what == "abort" then
      aborts = aborts + 1
      local info = ""
      local okutil, util = pcall(require, "jit.util")
      if okutil and util.traceinfo then
        local ti = util.traceinfo(tr)
        if ti then info = " link=" .. tostring(ti.link) end
      end
      local key = tostring(otr) .. "/" .. tostring(oex) .. info
      reasons[key] = (reasons[key] or 0) + 1
    end
  end, "trace")
  say("jit.attach: " .. (okattach and "ok" or "FAILED"))

  -- The runtime now reserves a 4 MB executable pool before the newlib heap
  -- exists, so it lands inside ARM branch range, and lj_mcode.c carves areas
  -- out of it instead of asking the kernel for a block per area. That removes
  -- both old failures at once: the out-of-range placement and the kernel's
  -- refusal to grant VM blocks under 1 MB. So LuaJIT's own defaults should
  -- work now, and the sweep just checks the pool takes larger areas too
  -- without running past its 4 MB.
  local CONFIGS = {
    { "love default", nil, nil },
    { "sizemcode=64  max=1024", 64, 1024 },
    { "sizemcode=256 max=2048", 256, 2048 },
    { "sizemcode=1024 max=3072", 1024, 3072 },
    { "pool exhaustion (max=8192 > 4 MB)", 1024, 8192 },
  }

  local base_nojit
  jit.off()
  jit.flush()
  local t1 = love.timer.getTime()
  work(200000)
  base_nojit = (love.timer.getTime() - t1) * 1000
  say(("baseline WITHOUT jit: %.0f ms"):format(base_nojit))
  jit.on()

  for _, cfg in ipairs(CONFIGS) do
    starts, stops, aborts, reasons = 0, 0, 0, {}
    if cfg[2] then
      pcall(jit.opt.start, "sizemcode=" .. cfg[2], "maxmcode=" .. cfg[3])
    end
    jit.flush()
    local t0 = love.timer.getTime()
    work(200000)
    local ms = (love.timer.getTime() - t0) * 1000
    say(("%-32s %6.0f ms  traces %d/%d compiled, %d aborted (%.2fx of no-jit)"):format(
      cfg[1], ms, stops, starts, aborts, ms / base_nojit))
    local shown = 0
    for reason, count in pairs(reasons) do
      shown = shown + 1
      if shown <= 2 then say(("     abort x%d: err %s"):format(count, reason)) end
    end
  end

  say("done")
  done = true
end

function love.draw()
  love.graphics.clear(0.05, 0.1, 0.2)
  love.graphics.setColor(1, 1, 1)
  love.graphics.print(done and "JIT probe done - see ux0:data/g1r-jit.txt" or "measuring...", 40, 40)
  for i, line in ipairs(out) do
    love.graphics.print(line, 40, 70 + i * 18)
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

    base.TITLE_ID, base.TITLE, base.STITLE, base.ATTRIBUTE2 = \
        "GNRJ00001", "G1R JIT Probe", "G1R JIT", 12
    icon = Image.open(Path(args.engine) / "ports" / "switch" / "assets" / "icon.jpg").convert("RGB")

    base.BUILD.mkdir(parents=True, exist_ok=True)
    vpk = base.BUILD / "gen1recomp-jitprobe.vpk"
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
