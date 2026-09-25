-- Bake potato_voxel's map mesh cache on the DESKTOP, so the Vita never builds.
--
-- Why this exists. Measured on hardware 2026-09-25: the on-device build holds a
-- growing LIVE working set -- 109.9 MB still live after a forced full GC, only
-- ~3 MB collectable -- while GPU texture memory stayed at 4.9 MB the whole time.
-- So the finished mesh is small and it is the BUILD that does not fit. Raising
-- the heap from 124 to 200 MB only moved the wall (47 MB -> 123.5 MB peak).
-- Desktop has the RAM to do the building once; the console should only load.
--
-- What it does: new game, reach the overworld, switch the voxel pipeline on so
-- the mod initialises its cache machinery, then drive CachePrebuild to
-- completion, logging progress. The cache lands in the desktop save's
-- mod_storage.
--
-- The cache is playthrough-SCOPED but not playthrough-DEPENDENT: the identity is
-- `format|version|activeVersion|profile|dataKey|voidFill` (lib/CacheIdentity.lua)
-- and carries no playthrough id, so the baked directory can simply be renamed to
-- the console's id when copying. Verified beforehand: `profile` is
-- BrickProfile.brick, an unconditional true, so it is "b" on both; and `dataKey`
-- derives from the ROM-decoded tilesets, with vendor/red.gb hashing
-- ea9bcae617fdf159b045185467ae58b2e4a48b9a, matching the console's
-- romSources.red.sha1.
--
-- Run from the ENGINE ROOT:
--   POKEPORT_DRIVER=<abs path to this file> \
--   BAKE_LEVEL=1 BAKE_MAX_FRAMES=400000 \
--   lovec .
--
-- BAKE_LEVEL is the voxel pipeline rung to switch on (1 is what the console
-- runs). BAKE_MAX_FRAMES bounds the run so a stalled bake cannot hang forever.

local U = dofile("tests/drivers/util.lua")

local LEVEL = tonumber(os.getenv("BAKE_LEVEL") or "1")
local MAX_FRAMES = tonumber(os.getenv("BAKE_MAX_FRAMES") or "400000")
local MOD_ID = os.getenv("BAKE_MOD_ID") or "potato_voxel"
-- Report at most this often, so the log stays readable on a 444-job run.
local REPORT_EVERY = tonumber(os.getenv("BAKE_REPORT_EVERY") or "600")

local function clock()
  return (love.timer and love.timer.getTime and love.timer.getTime()) or os.clock()
end

-- Flushed file log, in addition to U.log.
--
-- The first attempt at this bake was killed mid-run and left /tmp/bake.log
-- completely EMPTY -- not even the opening line -- because print() buffers and
-- the buffer died with the process. A long run that leaves no evidence when it
-- is interrupted is worthless, so every line is written and flushed here too.
-- Drivers are loaded with plain loadfile(), not sandboxed mod code, so io is
-- available.
-- Relative to the engine root the driver runs from; override with BAKE_LOG.
local logFile = io.open(os.getenv("BAKE_LOG") or "bake-progress.txt", "w")

local function say(fmt, ...)
  local text = select("#", ...) > 0 and string.format(fmt, ...) or fmt
  U.log(text)
  if logFile then
    logFile:write(text, "\n")
    logFile:flush()
  end
end

local function heapMB()
  return collectgarbage("count") / 1024
end

return function(game)
  say(("bake driver: love %s, mod %s, level %d, frame cap %d"):format(
    table.concat({ love.getVersion() }, ".", 1, 3), MOD_ID, LEVEL, MAX_FRAMES))

  U.newGame(game)
  for _ = 1, 2000 do
    if game.overworld and game.stack:top() == game.overworld then break end
    U.tap(game, "a")
    U.wait(2)
  end
  local reached = game.overworld ~= nil and game.stack:top() == game.overworld
  say("overworld reached: " .. tostring(reached))
  if not reached then
    say("BAKE FAILED: never reached the overworld")
    return
  end

  -- The mod only spins up its cache machinery once its pipeline is live, which
  -- is the same gate the console needed (options.pipelines.voxel, level 0 = OFF).
  local Pipelines = require("src.render.Pipelines")
  local okLevel, errLevel = pcall(Pipelines.setLevel, "voxel", LEVEL)
  say(("voxel level -> %d (%s): %s"):format(LEVEL,
    tostring(Pipelines.levelLabel and Pipelines.levelLabel("voxel", LEVEL)),
    okLevel and "ok" or tostring(errLevel)))
  U.wait(30)
  say("voxel level now: " .. tostring(Pipelines.level and Pipelines.level("voxel")))

  local exports = (game.mods and game.mods.exports) or {}
  local ex = exports[MOD_ID]
  if not (ex and ex.lib) then
    local names = {}
    for id in pairs(exports) do names[#names + 1] = id end
    table.sort(names)
    say("BAKE FAILED: no exports.lib for " .. MOD_ID
          .. "; exports present: " .. table.concat(names, ", "))
    return
  end

  local okReq, Prebuild = pcall(ex.lib.require, "CachePrebuild")
  if not (okReq and type(Prebuild) == "table") then
    say("BAKE FAILED: could not require CachePrebuild: " .. tostring(Prebuild))
    return
  end

  if Prebuild.available and not Prebuild.available() then
    say("BAKE FAILED: CachePrebuild.available() is false (no MeshCache?)")
    return
  end
  if Prebuild.isReady and Prebuild.isReady() then
    say("cache already READY before starting; nothing to bake")
    return
  end

  local okStart, started = pcall(Prebuild.start, game)
  say(("CachePrebuild.start -> %s%s"):format(tostring(started),
    okStart and "" or (" (threw: " .. tostring(started) .. ")")))
  if not (okStart and started) then
    say("BAKE FAILED: prebuild did not start")
    return
  end

  local t0 = clock()
  local frames = 0
  local lastDone, lastChange = -1, 0
  local stalled = false

  while frames < MAX_FRAMES do
    coroutine.yield()
    frames = frames + 1

    local done, total, running = Prebuild.progress()
    done, total = tonumber(done) or 0, tonumber(total) or 0

    if done ~= lastDone then
      lastDone, lastChange = done, frames
    end

    if frames % REPORT_EVERY == 0 then
      local elapsed = clock() - t0
      local rate = done > 0 and (elapsed / done) or 0
      say(("bake %d/%d jobs, %.0f s elapsed, %.2f s/job, eta %.0f s, heap %.0f MB, frame %d")
        :format(done, total, elapsed, rate,
                (total > done and rate > 0) and rate * (total - done) or 0,
                heapMB(), frames))
    end

    if Prebuild.isReady and Prebuild.isReady() then
      say(("BAKE COMPLETE: %d/%d jobs in %.0f s (%d frames)"):format(
        done, total, clock() - t0, frames))
      break
    end
    if not running then
      say(("bake stopped running at %d/%d after %.0f s; isReady=%s"):format(
        done, total, clock() - t0, tostring(Prebuild.isReady and Prebuild.isReady())))
      break
    end
    -- 60 s of wall time with no job completing means something is wedged;
    -- better to report it than to burn the whole frame budget.
    if frames - lastChange > 3600 then
      say(("BAKE STALLED: no progress for %d frames at %d/%d"):format(
        frames - lastChange, done, total))
      stalled = true
      break
    end
  end

  if frames >= MAX_FRAMES then
    say(("BAKE HIT FRAME CAP at %d frames, %d/%d jobs"):format(
      frames, lastDone, select(2, Prebuild.progress()) or 0))
  end

  local metrics = Prebuild.metrics and Prebuild.metrics() or {}
  local keys = {}
  for k in pairs(metrics) do keys[#keys + 1] = k end
  table.sort(keys)
  local parts = {}
  for _, k in ipairs(keys) do
    parts[#parts + 1] = k .. "=" .. tostring(metrics[k])
  end
  say("prebuild metrics: " .. table.concat(parts, " "))
  say("failed jobs: " .. tostring(Prebuild._workerFailures and Prebuild._workerFailures()))
  say("save dir: " .. tostring(love.filesystem.getSaveDirectory()))
  say(stalled and "DONE (stalled)" or "DONE")
  if logFile then logFile:close() end
end
