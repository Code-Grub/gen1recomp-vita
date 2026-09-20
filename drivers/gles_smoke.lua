-- Vita stand-in smoke run: LÖVE 11.4 + OpenGL ES (LOVE_GRAPHICS_USE_OPENGLES=1),
-- JIT off, real game.  Logs every shader the engine fails to compile, then
-- plays into a new game and walks Pallet Town with screenshots.
--
-- Run from C:/g2dev (util.lua is loaded relative to cwd):
--   POKEPORT_DRIVER=<this file> SHOT_DIR=<dir> lovec-11.4 .

if jit and os.getenv("SMOKE_JIT_OFF") then jit.off() end

local U = dofile("tests/drivers/util.lua")
local SHOT_DIR = assert(os.getenv("SHOT_DIR"), "set SHOT_DIR")

local newShader = love.graphics.newShader
local shaderCount, shaderFails = 0, 0
love.graphics.newShader = function(...)
  shaderCount = shaderCount + 1
  local ok, result = pcall(newShader, ...)
  if ok then return result end
  shaderFails = shaderFails + 1
  local where = debug.traceback("", 2):match("\n%s*([^\n]+)\n") or "?"
  U.log("SHADER FAIL at " .. where .. ": " .. tostring(result):gsub("%s+", " "):sub(1, 300))
  error(result, 2)
end

local function shot(game, name)
  local ok = U.shot(game, SHOT_DIR .. "/" .. name .. ".png")
  U.log("shot " .. name .. (ok and " ok" or " MISSING"))
end

return function(game)
  local name, version, vendor, device = love.graphics.getRendererInfo()
  U.log(("renderer: %s | %s | %s | %s"):format(name, version, vendor, device))
  U.log("love " .. table.concat({ love.getVersion() }, ".", 1, 3) .. ", jit " ..
    tostring(jit and jit.status()))
  U.log(("vita shim: %s, window %dx%d, synth rate %s"):format(tostring(_G.POKEPORT_VITA),
    love.graphics.getWidth(), love.graphics.getHeight(),
    tostring(require("src.core.ChipSynth").SAMPLE_RATE)))

  U.newGame(game)
  -- util's 400-tap budget ends mid-speech on this build; finish it here
  for _ = 1, 2000 do
    if game.overworld and game.stack:top() == game.overworld then break end
    U.tap(game, "a")
    U.wait(2)
  end
  U.wait(30)
  U.log("overworld reached: " .. tostring(game.overworld ~= nil and game.stack:top() == game.overworld))
  shot(game, "01_bedroom")

  -- down the stairs (top-right of the bedroom) and out the front door
  U.hold(game, "right", 40)
  U.hold(game, "up", 40)
  U.wait(60)
  shot(game, "02_downstairs")
  U.hold(game, "down", 120)
  U.hold(game, "left", 16)
  U.hold(game, "down", 60)
  U.wait(90)
  shot(game, "03_outside")
  U.hold(game, "right", 96)
  U.hold(game, "up", 64)
  U.wait(30)
  shot(game, "04_pallet_walk")

  U.tap(game, "start")
  U.wait(30)
  shot(game, "05_start_menu")
  U.tap(game, "b")
  U.wait(20)

  U.log(("shaders created: %d, failed: %d"):format(shaderCount, shaderFails))
  U.log("DONE")
end
