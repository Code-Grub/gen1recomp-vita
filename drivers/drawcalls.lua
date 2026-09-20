-- Per-frame GPU submission cost of the base game at the Vita's settings.
--
-- Draw calls, canvas switches and shader switches are what the Vita's SGX
-- driver charges CPU for, and they are the same numbers on any machine --
-- unlike frame times, which say nothing here about a 444 MHz ARM.  Reports
-- per-frame medians and the call sites responsible.
--
--   VITA_SHIM_FORCE=1 LOVE_GRAPHICS_USE_OPENGLES=1 POKEPORT_DRIVER=<this>
--   [COUNT_MAP=VIRIDIAN_CITY] [COUNT_SECONDS=10] lovec-11.4 <game.love>

local U = dofile("tests/drivers/util.lua")
local MAP = os.getenv("COUNT_MAP") or "PALLET_TOWN"
local SECONDS = tonumber(os.getenv("COUNT_SECONDS")) or 10
local OUT = os.getenv("COUNT_OUT")

local g = love.graphics
local frame = { draws = 0, batches = 0, prints = 0, shapes = 0, canvas = 0, shader = 0 }
local perFrame, sites, counting = {}, {}, false

local function note(kind)
  if not counting then return end
  frame[kind] = frame[kind] + 1
  if kind == "draws" then
    local info = debug.getinfo(3, "Sl")
    if info then
      local at = ("%s:%d"):format(info.short_src:gsub(".*[/\\]", ""), info.currentline)
      sites[at] = (sites[at] or 0) + 1
    end
  end
end

local canvasSizes = {}
local rawNewCanvas = g.newCanvas
g.newCanvas = function(w, h, settings, ...)
  local c = rawNewCanvas(w, h, settings, ...)
  local key = ("%dx%d"):format(c:getWidth(), c:getHeight())
  canvasSizes[key] = (canvasSizes[key] or 0) + 1
  return c
end

local raw = { draw = g.draw, print = g.print, printf = g.printf, rectangle = g.rectangle,
              line = g.line, circle = g.circle, setCanvas = g.setCanvas, setShader = g.setShader }
g.draw = function(obj, ...)
  note(type(obj) == "userdata" and obj.typeOf and obj:typeOf("SpriteBatch") and "batches" or "draws")
  return raw.draw(obj, ...)
end
g.print = function(...) note("prints") return raw.print(...) end
g.printf = function(...) note("prints") return raw.printf(...) end
g.rectangle = function(...) note("shapes") return raw.rectangle(...) end
g.line = function(...) note("shapes") return raw.line(...) end
g.circle = function(...) note("shapes") return raw.circle(...) end
g.setCanvas = function(...) note("canvas") return raw.setCanvas(...) end
g.setShader = function(...) note("shader") return raw.setShader(...) end

local loveDraw = love.draw
love.draw = function(...)
  for k in pairs(frame) do frame[k] = 0 end
  loveDraw(...)
  if counting then
    perFrame[#perFrame + 1] = { draws = frame.draws, batches = frame.batches,
      prints = frame.prints, shapes = frame.shapes, canvas = frame.canvas, shader = frame.shader }
  end
end

local function median(key)
  local vals = {}
  for _, f in ipairs(perFrame) do vals[#vals + 1] = f[key] end
  table.sort(vals)
  if #vals == 0 then return 0, 0 end
  return vals[math.ceil(#vals / 2)], vals[#vals]
end

return function(game)
  U.newGame(game)
  for _ = 1, 2000 do
    if game.overworld and game.stack:top() == game.overworld then break end
    U.tap(game, "a")
    U.wait(2)
  end
  U.teleport(game, MAP, 5, 6, "down")
  U.wait(60)
  game.save.options.performance = "low"
  game:applyOptions(game.save.options)
  U.wait(30)

  counting = true
  local t0, dirs, d = love.timer.getTime(), { "down", "right", "up", "left" }, 1
  while love.timer.getTime() - t0 < SECONDS do
    for _ = 1, 24 do
      table.insert(game.input.pressQueue, dirs[d])
      game.input.state[dirs[d]] = true
      coroutine.yield()
    end
    game.input.state[dirs[d]] = false
    d = d % #dirs + 1
  end
  counting = false

  local lines = { ("=== %s, %d rendered frames, tier %s"):format(
    MAP, #perFrame, tostring(require("src.core.Performance").tier)) }
  for _, key in ipairs({ "draws", "batches", "prints", "shapes", "canvas", "shader" }) do
    local med, max = median(key)
    lines[#lines + 1] = ("  %-9s median %5d  max %5d  per frame"):format(key, med, max)
  end
  local sizes = {}
  for k, v in pairs(canvasSizes) do sizes[#sizes + 1] = k .. " x" .. v end
  table.sort(sizes)
  lines[#lines + 1] = "--- canvases created: " .. table.concat(sizes, ", ")
  lines[#lines + 1] = "--- draw call sites (share of all draws)"
  local rows, total = {}, 0
  for at, n in pairs(sites) do rows[#rows + 1] = { at, n } total = total + n end
  table.sort(rows, function(a, b) return a[2] > b[2] end)
  for i = 1, math.min(12, #rows) do
    lines[#lines + 1] = ("  %5.1f%%  %6d  %s"):format(rows[i][2] / total * 100, rows[i][2], rows[i][1])
  end
  local text = table.concat(lines, "\n")
  print(text)
  if OUT then local f = io.open(OUT, "w") if f then f:write(text .. "\n") f:close() end end
  U.log("DONE")
end
