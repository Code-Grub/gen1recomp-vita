-- PS Vita packaging shim (added by vita-probe/build_game_vpk.py; not part of
-- the engine repo).  Runs the engine's own conf.lua, packaged alongside as
-- conf_engine.lua, then adjusts it only on isage's LÖVE 11.4 Vita runtime.
-- Anywhere else it is a pass-through.
--
-- That runtime reports love._os == "Linux" on a 32-bit ARM LuaJIT whose
-- built-in package.path points at ux0:, which no real Linux install has.
-- VITA_SHIM_FORCE=1 applies the Vita settings on desktop for testing.

assert(love.filesystem.load("conf_engine.lua"))()

local isVita = os.getenv("VITA_SHIM_FORCE") == "1"
  or (love._os == "Linux" and type(jit) == "table" and jit.arch == "arm"
      and package.path:find("ux0:", 1, true) ~= nil)

if isVita then
  _G.POKEPORT_VITA = true

  local engineConf = love.conf
  function love.conf(t)
    engineConf(t)
    -- The runtime is LÖVE 11.4; asking for 11.5 raises a compatibility
    -- dialog before the game starts.  The engine runs on 11.4 unchanged.
    t.version = "11.4"
    -- One fixed 960x544 panel: nothing to resize.  The engine letterboxes
    -- the 160x144 screen into whatever size the window ends up.
    t.window.width = 960
    t.window.height = 544
    t.window.minwidth = 1
    t.window.minheight = 1
    t.window.resizable = false
    -- desktop measurement only: uncapped frames show the real frame cost
    if os.getenv("VITA_SHIM_NOVSYNC") == "1" then t.window.vsync = 0 end
  end

  -- Music is synthesized in Lua and its cost scales linearly with the sample
  -- rate.  The PortMaster handheld builds run at 22050 for the same reason
  -- (ChipSynth.lua reads this on load); an explicit value still wins.
  local AUDIO_RATE = "@AUDIO_RATE@"
  -- The Vita reports itself as desktop Linux (isage's port defines
  -- LOVE_LINUX), so none of the engine's handheld paths would fire on their
  -- own.  These are the same knobs build-linux-arm-sbc.sh exports for the
  -- ARM handheld build, which is the closest device class this engine
  -- already supports.
  local DEFAULTS = {
    -- Pad-driven launcher, the in-game ROM file browser instead of a
    -- desktop file dialog that cannot exist here, the on-screen keyboard
    -- for naming (a Vita has no keyboard), and handheld frame pacing.
    POKEPORT_HANDHELD = "1",
    -- Synthesis cost scales with the rate and the Game Boy's own audio
    -- sits well below 11 kHz; the audio worker is the dominant CPU cost
    -- while music plays.
    POKEPORT_AUDIO_RATE = AUDIO_RATE ~= "" and AUDIO_RATE or nil,
    -- Idle-power governor: after 10 s without input on a static screen,
    -- presentation drops to 6 fps.  Game logic and audio stay full speed.
    POKEPORT_IDLE_AFTER = "10",
    POKEPORT_IDLE_FPS = "6",
    -- No Discord client can exist on a Vita; without this the presence
    -- module retries its socket connect every 30 seconds for nothing.
    POKEPORT_NO_DISCORD = "1",
  }
  local getenv = os.getenv
  os.getenv = function(name)
    local value = getenv(name)
    if value == nil then return DEFAULTS[name] end
    return value
  end
end
