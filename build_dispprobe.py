"""Build a VPK that shows where the Vita actually puts pixels.

The game renders into a canvas and letterboxes it; on hardware the top of the
screen is cut off. This draws a ruler-like pattern twice - once straight to the
screen, once through a canvas the same way the engine does - so the two can be
compared by eye. Whatever is missing or shifted names the cause: a viewport
offset, a vertical flip, or a size LOVE and vitaGL disagree about.

    python build_dispprobe.py --eboot vendor/love-vita/love.self
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
  t.identity = "gen1recomp-dispprobe"
  t.window.title = "G1R Display Probe"
  t.window.width = 960
  t.window.height = 544
  t.modules.physics = false
  t.modules.video = false
  t.version = "11.4"
end
"""

MAIN_LUA = r"""
-- Two modes, toggled with X (or any button): direct drawing, then the same
-- pattern through a canvas. A report is written to ux0:data/g1r-display.txt.
local mode = 1          -- 1 = direct, 2 = via canvas
local canvas
local font

local function report()
  local f = io.open("ux0:data/g1r-display.txt", "w")
  if not f then return end
  local w, h = love.graphics.getDimensions()
  local pw, ph = love.graphics.getPixelDimensions()
  f:write(("getDimensions      %d x %d\n"):format(w, h))
  f:write(("getPixelDimensions %d x %d\n"):format(pw, ph))
  f:write(("getDPIScale        %s\n"):format(tostring(love.graphics.getDPIScale())))
  local ok, dw, dh = pcall(love.window.getMode)
  f:write(("window.getMode     %s %s\n"):format(tostring(dw), tostring(dh)))
  local name, ver, vendor, dev = love.graphics.getRendererInfo()
  f:write(("renderer           %s | %s | %s | %s\n"):format(name, ver, vendor, dev))
  f:write(("canvas 320x182 ok  %s\n"):format(tostring(canvas ~= nil)))
  f:close()
end

function love.load()
  font = love.graphics.newFont(16)
  love.graphics.setFont(font)
  local ok, c = pcall(love.graphics.newCanvas, 320, 182)
  if ok then canvas = c end
  report()
end

-- The pattern: a 4px border on all four edges, horizontal rules every 32px
-- labelled with their y coordinate, and corner tags. Anything cropped or
-- shifted is obvious from which labels are visible.
local function pattern(w, h, label)
  love.graphics.clear(0.06, 0.09, 0.16)
  love.graphics.setColor(1, 0.85, 0.2)
  love.graphics.rectangle("fill", 0, 0, w, 4)          -- top edge
  love.graphics.rectangle("fill", 0, h - 4, w, 4)      -- bottom edge
  love.graphics.rectangle("fill", 0, 0, 4, h)          -- left edge
  love.graphics.rectangle("fill", w - 4, 0, 4, h)      -- right edge

  love.graphics.setColor(0.4, 0.8, 1)
  for y = 0, h - 1, 32 do
    love.graphics.rectangle("fill", 0, y, w, 1)
    love.graphics.print(tostring(y), 8, y + 2)
  end

  love.graphics.setColor(1, 1, 1)
  love.graphics.print("TOP-LEFT 0,0", 12, 8)
  love.graphics.print("BOTTOM-RIGHT", w - 130, h - 24)
  love.graphics.print(label, w / 2 - 90, h / 2 - 10)
  love.graphics.print(("%dx%d"):format(w, h), w / 2 - 30, h / 2 + 14)
end

function love.draw()
  if mode == 1 or not canvas then
    pattern(love.graphics.getWidth(), love.graphics.getHeight(), "DIRECT TO SCREEN")
  else
    love.graphics.setCanvas(canvas)
    pattern(320, 182, "VIA CANVAS")
    love.graphics.setCanvas()
    love.graphics.clear(0, 0, 0)
    love.graphics.setColor(1, 1, 1)
    -- the engine's own arrangement: integer scale, centred
    love.graphics.draw(canvas, 0, 0, 0, 3, 3)
  end
end

function love.keypressed() mode = mode == 1 and 2 or 1 end
function love.gamepadpressed(_, b)
  if b == "start" then love.event.quit() else mode = mode == 1 and 2 or 1 end
end
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
        "GNRD00001", "G1R Display Probe", "G1R Display", 12
    icon = Image.open(Path(args.engine) / "ports" / "switch" / "assets" / "icon.jpg").convert("RGB")

    base.BUILD.mkdir(parents=True, exist_ok=True)
    vpk = base.BUILD / "gen1recomp-dispprobe.vpk"
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
