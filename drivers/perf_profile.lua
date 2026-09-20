-- Where does the CPU go in the base game at the Vita's settings?
--
-- Runs the engine at the "low" performance tier (what the Vita resolves to),
-- 22050 Hz music, and samples with LuaJIT's profiler while walking the
-- overworld.  Reports self time per function, the hottest call stacks, and
-- the VM state split (interpreted / compiled / GC), which is what decides
-- whether the Vita's slower CPU can keep a frame budget.
--
--   VITA_SHIM_FORCE=1 VITA_SHIM_NOVSYNC=1 LOVE_GRAPHICS_USE_OPENGLES=1 \
--   PROFILE_SECONDS=20 [PROFILE_JIT_OFF=1] [PROFILE_MAP=VIRIDIAN_CITY] \
--   POKEPORT_DRIVER=<this> lovec-11.4 <game.love>          (cwd: engine root)

local U = dofile("tests/drivers/util.lua")
local SECONDS = tonumber(os.getenv("PROFILE_SECONDS")) or 20
local MAP = os.getenv("PROFILE_MAP") or "PALLET_TOWN"
local OUT = os.getenv("PROFILE_OUT")

if os.getenv("PROFILE_JIT_OFF") == "1" and jit then jit.off() end

local profile = require("jit.profile")

local selfTime, stacks, vmstates, total = {}, {}, {}, 0

local function bump(t, k, n)
  t[k] = (t[k] or 0) + n
end

local function sample(thread, count, vmstate)
  total = total + count
  bump(vmstates, vmstate, count)
  local ok, leaf = pcall(profile.dumpstack, thread, "F", 1)
  if ok then bump(selfTime, (leaf:gsub("%s+$", "")), count) end
  local ok2, stack = pcall(profile.dumpstack, thread, "F;", 8)
  if ok2 then bump(stacks, (stack:gsub("%s+$", "")), count) end
end

local function top(t, n)
  local rows = {}
  for k, v in pairs(t) do rows[#rows + 1] = { k, v } end
  table.sort(rows, function(a, b) return a[2] > b[2] end)
  local out = {}
  for i = 1, math.min(n, #rows) do out[i] = rows[i] end
  return out
end

local function report(lines)
  local text = table.concat(lines, "\n")
  print(text)
  if OUT then
    local f = io.open(OUT, "w")
    if f then f:write(text .. "\n") f:close() end
  end
end

return function(game)
  local Performance = require("src.core.Performance")
  local ChipSynth = require("src.core.ChipSynth")

  U.newGame(game)
  for _ = 1, 2000 do
    if game.overworld and game.stack:top() == game.overworld then break end
    U.tap(game, "a")
    U.wait(2)
  end
  U.teleport(game, MAP, 5, 6, "down")
  U.wait(120)                       -- let the map settle before sampling

  -- the tier the Vita resolves to (arm + "Linux"), applied the way the
  -- OPTIONS row would.  After newGame: starting a game reloads the save's
  -- options and would overwrite it.
  game.save.options.performance = "low"
  game:applyOptions(game.save.options)

  U.log(("profiling %s for %ds: tier %s, synth %d Hz, jit %s, love %s"):format(
    MAP, SECONDS, tostring(Performance.tier), ChipSynth.SAMPLE_RATE,
    tostring(jit and jit.status()), table.concat({ love.getVersion() }, ".", 1, 3)))

  profile.start("i2", sample)
  local t0 = love.timer.getTime()
  local frames, dirs, d = 0, { "down", "right", "up", "left" }, 1
  while love.timer.getTime() - t0 < SECONDS do
    for _ = 1, 24 do                -- walk a loop so map, sprites and audio all run
      table.insert(game.input.pressQueue, dirs[d])
      game.input.state[dirs[d]] = true
      frames = frames + 1
      coroutine.yield()
    end
    game.input.state[dirs[d]] = false
    d = d % #dirs + 1
  end
  profile.stop()
  local elapsed = love.timer.getTime() - t0

  local lines = {
    ("=== %s, %.1fs, %d frames (%.1f fps), %d samples"):format(
      MAP, elapsed, frames, frames / elapsed, total),
    "--- VM state (N native/JIT, I interpreted, C C-code, G GC, J compiler)",
  }
  for _, row in ipairs(top(vmstates, 8)) do
    lines[#lines + 1] = ("  %-4s %5.1f%%"):format(row[1], row[2] / total * 100)
  end
  lines[#lines + 1] = "--- self time by function"
  for _, row in ipairs(top(selfTime, 25)) do
    lines[#lines + 1] = ("  %5.1f%%  %s"):format(row[2] / total * 100, row[1])
  end
  lines[#lines + 1] = "--- hottest stacks (leaf first)"
  for _, row in ipairs(top(stacks, 12)) do
    lines[#lines + 1] = ("  %5.1f%%  %s"):format(row[2] / total * 100, row[1])
  end
  report(lines)
  U.log("DONE")
end
