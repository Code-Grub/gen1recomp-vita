-- Voxel-mod smoke run under Vita-like conditions: the packaged game.love with
-- the Vita shim forced on, LÖVE 11.4, OpenGL ES.  Enforces the SGX543 limits
-- the desktop GPU does not have (4096px textures), logs every canvas format
-- and large mesh the mod asks for, catches shader failures, and times frames
-- so the heavy ones (map mesh builds) stand out.
--
--   VITA_SHIM_FORCE=1 LOVE_GRAPHICS_USE_OPENGLES=1 POKEPORT_DRIVER=<this>
--   SHOT_DIR=<dir> lovec-11.4 build/gen1recomp-vita.love      (cwd: engine root)

local U = dofile("tests/drivers/util.lua")
local SHOT_DIR = assert(os.getenv("SHOT_DIR"), "set SHOT_DIR")
local MAX_TEX = 4096 -- PVR_PSP2 GLES2_MAX_TEXTURE_SIZE on SGX543

local g = love.graphics
local stats = { shaders = 0, shaderFails = 0, canvases = {}, bigMeshes = 0, maxVerts = 0, texFails = 0 }

local function caller()
  return (debug.traceback("", 3):match("\n%s*([^\n]+)\n") or "?"):gsub("^%s+", "")
end

local function guardSize(kind, w, h)
  if type(w) == "number" and type(h) == "number" and (w > MAX_TEX or h > MAX_TEX) then
    stats.texFails = stats.texFails + 1
    U.log(("VITA LIMIT %s %dx%d > %d at %s"):format(kind, w, h, MAX_TEX, caller()))
  end
end

local newShader = g.newShader
g.newShader = function(...)
  stats.shaders = stats.shaders + 1
  local ok, r = pcall(newShader, ...)
  if ok then return r end
  stats.shaderFails = stats.shaderFails + 1
  U.log("SHADER FAIL at " .. caller() .. ": " .. tostring(r):gsub("%s+", " "):sub(1, 300))
  error(r, 2)
end

local newCanvas = g.newCanvas
g.newCanvas = function(w, h, settings, ...)
  guardSize("canvas", w, h)
  local fmt = type(settings) == "table" and (settings.format or "normal") or "normal"
  if type(settings) == "table" and settings.readable then fmt = fmt .. "+readable" end
  if type(settings) == "table" and (settings.msaa or 0) > 0 then fmt = fmt .. "+msaa" .. settings.msaa end
  stats.canvases[fmt] = (stats.canvases[fmt] or 0) + 1
  return newCanvas(w, h, settings, ...)
end

local newImage = g.newImage
g.newImage = function(src, ...)
  local img = newImage(src, ...)
  guardSize("image", img:getWidth(), img:getHeight())
  return img
end

local TYPE_BYTES = { float = 4, byte = 1, unorm8 = 1, unorm16 = 2, snorm8 = 1, snorm16 = 2,
  int8 = 1, uint8 = 1, int16 = 2, uint16 = 2, int32 = 4, uint32 = 4 }
local meshBytes = setmetatable({}, { __mode = "k" }) -- live meshes -> vertex buffer bytes

local newMesh = g.newMesh
g.newMesh = function(a, b, ...)
  local mesh = newMesh(a, b, ...)
  local n = mesh:getVertexCount()
  if n > stats.maxVerts then stats.maxVerts = n end
  if n > 65535 then stats.bigMeshes = stats.bigMeshes + 1 end
  local stride = 0
  for _, attr in ipairs(mesh:getVertexFormat()) do
    stride = stride + (TYPE_BYTES[attr[2]] or 4) * (attr[3] or 1)
  end
  meshBytes[mesh] = n * stride
  return mesh
end

-- vertices submitted per frame: what the Vita's GPU would have to process
local frameVerts, drawVerts = 0, {}
local draw = g.draw
g.draw = function(obj, ...)
  if type(obj) == "userdata" and obj.typeOf and obj:typeOf("Mesh") then
    local _, count = obj:getDrawRange()
    frameVerts = frameVerts + (count or obj:getVertexCount())
  end
  return draw(obj, ...)
end
-- CPU time in Lua update + draw submission (excludes present / GPU wait):
-- the part of a frame that scales with the Vita's much slower CPU
local cpuMs, updateStart = {}, nil
local loveDraw = love.draw
love.draw = function(...)
  frameVerts = 0
  local t0 = love.timer.getTime()
  loveDraw(...)
  local drawMs = (love.timer.getTime() - t0) * 1000
  drawVerts[#drawVerts + 1] = frameVerts
  cpuMs[#cpuMs + 1] = drawMs + (updateStart or 0)
  updateStart = nil
end
local loveUpdate = love.update
love.update = function(...)
  local t0 = love.timer.getTime()
  loveUpdate(...)
  updateStart = (love.timer.getTime() - t0) * 1000
end
local function liveMeshMB()
  collectgarbage("collect")
  local total = 0
  for _, bytes in pairs(meshBytes) do total = total + bytes end
  return total / 1048576
end

-- frame timing: wall time between driver resumes = one full game frame
local frameTimes, last = {}, nil
local function tick()
  local t = love.timer.getTime()
  if last then frameTimes[#frameTimes + 1] = t - last end
  last = t
end
local function wait(n) for _ = 1, n do tick(); U.wait(1) end end
local function hold(game, btn, n)
  for _ = 1, n do
    tick()
    table.insert(game.input.pressQueue, btn)
    game.input.state[btn] = true
    coroutine.yield()
  end
  game.input.state[btn] = false
end
local function report(label)
  table.sort(frameTimes)
  local n = #frameTimes
  if n == 0 then return end
  local sum = 0
  for _, v in ipairs(frameTimes) do sum = sum + v end
  table.sort(drawVerts)
  local dv = #drawVerts
  U.log(("%s: %d frames, median %.1f ms, p95 %.1f ms, worst %.1f ms, total %.2f s; " ..
    "mesh vertices drawn/frame median %d max %d; live mesh buffers %.0f MB"):format(
    label, n, frameTimes[math.ceil(n / 2)] * 1000, frameTimes[math.ceil(n * 0.95)] * 1000,
    frameTimes[n] * 1000, sum, dv > 0 and drawVerts[math.ceil(dv / 2)] or 0,
    dv > 0 and drawVerts[dv] or 0, liveMeshMB()))
  table.sort(cpuMs)
  local c = #cpuMs
  if c > 0 then
    U.log(("%s: Lua update+draw CPU median %.2f ms, p95 %.2f ms"):format(
      label, cpuMs[math.ceil(c / 2)], cpuMs[math.ceil(c * 0.95)]))
  end
  frameTimes, last, drawVerts, cpuMs = {}, nil, {}, {}
end

local function shot(game, name)
  U.log("shot " .. name .. (U.shot(game, SHOT_DIR .. "/" .. name .. ".png") and " ok" or " MISSING"))
end

return function(game)
  local name, version = g.getRendererInfo()
  U.log(("renderer: %s | %s; love %s; shim %s; window %dx%d"):format(name, version,
    table.concat({ love.getVersion() }, ".", 1, 3), tostring(_G.POKEPORT_VITA), g.getWidth(), g.getHeight()))
  local mods = {}
  for id in pairs((game.mods and game.mods.exports) or {}) do mods[#mods + 1] = id end
  table.sort(mods)
  U.log("mods with exports: " .. table.concat(mods, ", "))

  U.newGame(game)
  for _ = 1, 2000 do
    if game.overworld and game.stack:top() == game.overworld then break end
    U.tap(game, "a")
    U.wait(2)
  end
  U.log("overworld reached: " .. tostring(game.overworld ~= nil and game.stack:top() == game.overworld))
  -- VOXEL_LEVEL=n turns on a mod's "voxel" render pipeline at rung n
  -- (potato: 1 HIGH, 2 MEDIUM, 3 LOW, 4 POTATO)
  local level = tonumber(os.getenv("VOXEL_LEVEL"))
  if level then
    local Pipelines = require("src.render.Pipelines")
    local ok, err = pcall(Pipelines.setLevel, "voxel", level)
    U.log(("voxel level -> %d (%s): %s"):format(level,
      tostring(Pipelines.levelLabel("voxel", level)), ok and "ok" or tostring(err)))
    wait(10)
    U.log("voxel level now: " .. tostring(Pipelines.level("voxel")))
  end
  wait(30)
  report("bedroom")
  shot(game, "01_bedroom")

  U.teleport(game, "PALLET_TOWN", 5, 6, "down")
  wait(120)
  report("pallet: teleport + first 120 frames")
  shot(game, "02_pallet")
  hold(game, "down", 48); hold(game, "right", 64); hold(game, "up", 96)
  report("pallet: walking")
  shot(game, "03_pallet_walk")

  U.teleport(game, "VIRIDIAN_CITY", 23, 26, "up")
  wait(120)
  report("viridian: teleport + first 120 frames")
  shot(game, "04_viridian")
  hold(game, "up", 64); hold(game, "left", 64)
  report("viridian: walking")
  shot(game, "05_viridian_walk")

  local fmts = {}
  for k, v in pairs(stats.canvases) do fmts[#fmts + 1] = k .. " x" .. v end
  table.sort(fmts)
  U.log("canvas formats: " .. table.concat(fmts, ", "))
  U.log(("shaders %d (failed %d); texture limit hits %d; largest mesh %d vertices; meshes > 65535: %d"):format(
    stats.shaders, stats.shaderFails, stats.texFails, stats.maxVerts, stats.bigMeshes))
  U.log("DONE")
end
