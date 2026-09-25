"""Build a minimal VPK that records how far the LÖVE runtime gets on a Vita.

The full probe left no trace at all: black screen, back to the LiveArea, no
save directory, no SDL log. That is indistinguishable between "the eboot died
before Lua ran" and "Lua ran but the GPU driver refused a window". This writes
ux0:data/g1r-boot.txt as it passes each stage, so the last line names the
failure point.

    python build_bootprobe.py [--eboot vendor/love-vita/love.self]

Output: build/gen1recomp-bootprobe.vpk (a few hundred KB).
"""

import argparse
import io
import subprocess
import zipfile
from pathlib import Path

from PIL import Image

import build_vpk as base

HERE = Path(__file__).resolve().parent
TITLE_ID = "GNRB00001"
TITLE = "G1R Boot Probe"
STITLE = "G1R Boot"

# Stages are appended, flushed and closed one at a time: a crash cannot lose
# an already-written line, so the file's last entry is the last stage reached.
CONF_LUA = r"""
local function mark(stage)
  local f = io.open("ux0:data/g1r-boot.txt", "a")
  if f then
    f:write(stage, "\n")
    f:close()
  end
end
_G.G1R_MARK = mark

-- Truncate on a fresh boot so each run stands alone.
local fresh = io.open("ux0:data/g1r-boot.txt", "w")
if fresh then
  fresh:write("1 conf.lua entered\n")
  fresh:close()
end

mark("2 love._os = " .. tostring(love._os))
mark("3 jit = " .. tostring(type(jit) == "table" and jit.version or "absent")
  .. ", arch " .. tostring(type(jit) == "table" and jit.arch or "?")
  .. ", status " .. tostring(type(jit) == "table" and select(1, jit.status()) or "?")
  .. ", bit " .. tostring(pcall(require, "bit"))
  .. ", ffi " .. tostring(pcall(require, "ffi")))

-- LÖVE calls this on any error, including one thrown during love.init before
-- a window exists.  Writing it down is the only way to see why the GPU driver
-- refused, since nothing can be drawn and SDL logs nothing here.
-- What SDL will actually accept. By the time this runs love.window exists
-- (love.init loaded the modules; only setMode threw), so each combination can
-- be tried directly. "Could not set window mode" says nothing about WHICH
-- attribute the PowerVR driver rejected; this does.
local ATTEMPTS = {
  { "default 960x544", 960, 544, {} },
  { "fullscreen", 960, 544, { fullscreen = true } },
  { "fullscreen exclusive", 960, 544, { fullscreen = true, fullscreentype = "exclusive" } },
  { "no depth/stencil/msaa", 960, 544, { depth = 0, stencil = 0, msaa = 0 } },
  { "no vsync", 960, 544, { vsync = 0 } },
  { "not resizable, no highdpi", 960, 544, { resizable = false, highdpi = false } },
  { "640x480", 640, 480, {} },
  { "544p window 480x272", 480, 272, {} },
}

local function probeWindow()
  local okCount, count = pcall(love.window.getDisplayCount)
  mark("   displays: " .. tostring(okCount and count or "query failed"))
  local okModes, modes = pcall(love.window.getFullscreenModes, 1)
  if okModes and type(modes) == "table" then
    local list = {}
    for i, m in ipairs(modes) do
      list[#list + 1] = m.width .. "x" .. m.height
      if i >= 6 then break end
    end
    mark("   modes: " .. (#list > 0 and table.concat(list, ", ") or "none reported"))
  else
    mark("   modes: query failed " .. tostring(modes))
  end
  for _, a in ipairs(ATTEMPTS) do
    local ok, err = pcall(love.window.setMode, a[2], a[3], a[4])
    mark(("   setMode %-24s -> %s"):format(a[1], ok and "OK" or ("FAIL " .. tostring(err))))
    if ok then
      pcall(love.window.close)
    end
  end
end

local function checkGraphics()
  local ok, a, b, c, d = pcall(love.graphics.getRendererInfo)
  mark("7 renderer: " .. (ok and (tostring(a) .. " | " .. tostring(b) .. " | "
    .. tostring(c) .. " | " .. tostring(d)) or ("FAILED " .. tostring(a))))
  mark("8 window " .. tostring(love.graphics.getWidth()) .. "x"
    .. tostring(love.graphics.getHeight()))
  local okC, canvas = pcall(love.graphics.newCanvas, 160, 144)
  mark("9 canvas: " .. (okC and "ok" or ("FAILED " .. tostring(canvas))))
  local okS, shader = pcall(love.graphics.newShader,
    "vec4 effect(vec4 c, Image t, vec2 uv, vec2 s) { return Texel(t, uv) * c; }")
  mark("10 shader: " .. (okS and "ok" or ("FAILED " .. tostring(shader))))
  local okT = pcall(love.thread.newThread, "local c = ... c:push('ok')")
  mark("11 love.thread: " .. tostring(okT))
  local okA = pcall(love.audio.newQueueableSource, 22050, 16, 2, 8)
  mark("12 audio: " .. tostring(okA))
end

-- LÖVE's own window creation always fails on this driver, and a retry right
-- after it always works, so the run continues from here: create the window,
-- then drive a real render loop and report whether it can draw.
local function handler(msg)
  mark("!! ERROR: " .. tostring(msg))
  if not (love.window and love.graphics) then return function() return 1 end end

  -- Isolate what actually makes setMode succeed. The run that worked queried
  -- the display first and passed NO flags; the run that failed passed
  -- {vsync=1} and queried nothing. Try them in that order and record each.
  local made = false

  local ok1, r1 = pcall(love.window.setMode, 960, 544, {})
  mark("5.1 setMode {} before any query -> " .. (ok1 and tostring(r1) or ("threw " .. tostring(r1))))
  made = ok1 and r1 == true

  if not made then
    local okC, count = pcall(love.window.getDisplayCount)
    mark("5.2 getDisplayCount -> " .. tostring(okC and count or "failed"))
    local okM, modes = pcall(love.window.getFullscreenModes, 1)
    local desc = "failed"
    if okM and type(modes) == "table" then
      local list = {}
      for i, m in ipairs(modes) do list[#list + 1] = m.width .. "x" .. m.height if i >= 4 then break end end
      desc = table.concat(list, ", ")
    end
    mark("5.3 getFullscreenModes -> " .. desc)

    local ok2, r2 = pcall(love.window.setMode, 960, 544, {})
    mark("5.4 setMode {} after queries -> " .. (ok2 and tostring(r2) or ("threw " .. tostring(r2))))
    made = ok2 and r2 == true

    if not made then
      local ok3, r3 = pcall(love.window.setMode, 960, 544, { vsync = 1 })
      mark("5.5 setMode {vsync=1} after queries -> " .. (ok3 and tostring(r3) or ("threw " .. tostring(r3))))
      made = ok3 and r3 == true
    end
  end
  mark("5.9 window created: " .. tostring(made))
  if not made then
    mark("!! no window; not touching love.graphics (that crashed the console)")
    return function() return 1 end
  end

  pcall(love.window.setTitle, "G1R Boot Probe")
  pcall(checkGraphics)

  local frames = 0
  return function()
    frames = frames + 1
    if frames == 1 then mark("13 first frame drawn") end
    if frames >= 180 then
      mark("14 " .. frames .. " frames drawn, quitting cleanly")
      return 1
    end
    pcall(love.event.pump)
    for e in love.event.poll() do
      if e == "quit" then return 1 end
    end
    love.graphics.origin()
    love.graphics.clear(0.1, 0.2, 0.4)
    love.graphics.setColor(1, 1, 1)
    love.graphics.print("G1R boot probe: frame " .. frames, 40, 40)
    love.graphics.print("window created after the expected first failure", 40, 70)
    love.graphics.present()
    pcall(love.timer.sleep, 0.008)
  end
end
love.errorhandler = handler
love.errhand = handler

function love.conf(t)
  t.identity = "gen1recomp-bootprobe"
  t.modules.physics = false
  t.modules.video = false
  t.version = "@LOVE_VERSION@"
  t.window.title = "G1R Boot Probe"
  t.window.width = 960
  t.window.height = 544
  mark("4 love.conf ran (version " .. t.version .. ")")
end
"""

MAIN_LUA = r"""
local mark = _G.G1R_MARK or function() end
mark("5 main.lua loaded")

local frames = 0


function love.load()
  mark("6 love.load entered")
  local ok, a, b, c, d = pcall(love.graphics.getRendererInfo)
  mark("7 renderer: " .. (ok and (tostring(a) .. " | " .. tostring(b) .. " | "
    .. tostring(c) .. " | " .. tostring(d)) or ("FAILED " .. tostring(a))))
  mark("8 window " .. tostring(love.graphics.getWidth()) .. "x"
    .. tostring(love.graphics.getHeight()))
  local okC, canvas = pcall(love.graphics.newCanvas, 160, 144)
  mark("9 canvas: " .. (okC and "ok" or ("FAILED " .. tostring(canvas))))
  local okS, shader = pcall(love.graphics.newShader,
    "vec4 effect(vec4 c, Image t, vec2 uv, vec2 s) { return Texel(t, uv) * c; }")
  mark("10 shader: " .. (okS and "ok" or ("FAILED " .. tostring(shader))))
  local okT, thread = pcall(love.thread.newThread, "local c = ... c:push('ok')")
  mark("11 love.thread: " .. (okT and "created" or ("FAILED " .. tostring(thread))))
  local okA, src = pcall(love.audio.newQueueableSource, 22050, 16, 2, 8)
  mark("12 audio: " .. (okA and "queueable source ok" or ("FAILED " .. tostring(src))))
end

function love.draw()
  frames = frames + 1
  if frames == 1 then mark("13 first frame drawn") end
  if frames == 120 then
    mark("14 120 frames drawn, quitting cleanly")
    love.event.quit()
  end
  love.graphics.clear(0.1, 0.2, 0.4)
  love.graphics.setColor(1, 1, 1)
  love.graphics.print("G1R boot probe: frame " .. frames, 40, 40)
  love.graphics.print("writing ux0:data/g1r-boot.txt", 40, 70)
end

function love.keypressed() love.event.quit() end
function love.gamepadpressed() love.event.quit() end
"""


def check_syntax(sources):
    """Compile the generated Lua before it can reach a console.

    These sources are built by string substitution, and a probe whose conf.lua
    does not parse fails exactly like the bug it is meant to diagnose: black
    screen, no output. One such build already shipped.
    """
    import build_game_vpk as game

    luajit = game.LUAJIT20 if game.LUAJIT20.exists() else None
    if luajit is None:
        print("no LuaJIT host found; skipping the syntax check")
        return
    base.BUILD.mkdir(parents=True, exist_ok=True)
    for name, text in sources.items():
        tmp = base.BUILD / f"_syntax_{name}"
        tmp.write_text(text, encoding="utf-8")
        # loadfile, not -b: bytecode mode loads jit/*.lua from whatever LuaJIT
        # is on PATH and dies with "core/library version mismatch".
        run = subprocess.run([str(luajit), "-e", f"assert(loadfile([[{tmp}]]))"],
                             capture_output=True, text=True)
        if run.returncode != 0:
            raise SystemExit(f"{name} does not parse:\n{run.stderr.strip()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eboot", default=None,
                    help="runtime to package (default: isage's prebuilt 11.4-vita eboot)")
    ap.add_argument("--engine", default="C:/g2dev", help="only used for the icon")
    ap.add_argument("--extended-memory", action="store_true",
                    help="param.sfo ATTRIBUTE2=12: does the GPU driver fail for lack of memory?")
    ap.add_argument("--title-id", default=TITLE_ID, help="so variants can coexist on the device")
    ap.add_argument("--out", default="gen1recomp-bootprobe.vpk")
    args = ap.parse_args()

    eboot = Path(args.eboot) if args.eboot else base.fetch("eboot.bin")
    if not eboot.is_file():
        raise SystemExit(f"no such runtime: {eboot}")
    module_zips = {name: base.fetch(name) for name in base.MODULES}

    sources = {"conf.lua": CONF_LUA.replace("@LOVE_VERSION@", "11.4"), "main.lua": MAIN_LUA}
    check_syntax(sources)

    love_bytes = io.BytesIO()
    with zipfile.ZipFile(love_bytes, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in sources.items():
            z.writestr(name, text)

    base.TITLE_ID = args.title_id
    base.TITLE = TITLE + (" ExtMem" if args.extended_memory else "")
    base.STITLE = STITLE + (" X" if args.extended_memory else "")
    if args.extended_memory:
        base.ATTRIBUTE2 = 12
    icon = Image.open(Path(args.engine) / "ports" / "switch" / "assets" / "icon.jpg").convert("RGB")

    base.BUILD.mkdir(parents=True, exist_ok=True)
    vpk = base.BUILD / args.out
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
    print(f"wrote {vpk} ({vpk.stat().st_size / 1e6:.1f} MB), runtime {eboot.name}")


if __name__ == "__main__":
    main()
