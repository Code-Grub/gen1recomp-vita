-- PS Vita packaging shim (added by vita-probe/build_game_vpk.py; not part of
-- the engine repo).  Runs the engine's own conf.lua, packaged alongside as
-- conf_engine.lua, then adjusts it only on isage's LÖVE 11.4 Vita runtime.
-- Anywhere else it is a pass-through.
--
-- That runtime reports love._os == "Linux" on a 32-bit ARM LuaJIT whose
-- built-in package.path points at ux0:, which no real Linux install has.
-- VITA_SHIM_OFF=1 disables the Vita settings, for a desktop run of the
-- packaged game.love that should behave like the plain engine.

assert(love.filesystem.load("conf_engine.lua"))()

-- This file is only ever written into the Vita VPK, by build_game_vpk.py. It
-- is not in the engine and nothing else loads it, so "am I on a Vita" is not a
-- question that needs asking: the answer is yes by construction.
--
-- It used to sniff for love._os == "Linux", jit.arch == "arm" and "ux0:" in
-- package.path, and on hardware that came out FALSE - so none of this block
-- ran, and the window stayed at the engine's desktop default of 1024x768 on a
-- 960x544 panel. That is the cropping, and it also silenced the JIT tuning
-- below (hence the stalls) and the handheld profile. The "ux0:" test is the
-- part that broke: it matched isage's prebuilt LuaJIT, whose package.path was
-- compiled with PREFIX=ux0:/data/luajit, and our own build_luajit.sh does not
-- set PREFIX. A detection that silently degrades into "behave like a desktop"
-- was the wrong shape regardless; this cannot fail that way.
local isVita = os.getenv("VITA_SHIM_OFF") ~= "1"

if isVita then
  _G.POKEPORT_VITA = true


  -- The JIT is left ON here, which it could not be until two separate bugs
  -- were fixed in the runtime this VPK ships (see vita-probe/TOOLCHAIN.md):
  --
  --   1. LuaJIT needs its machine code within ARM branch range (~14 MB) of
  --      the interpreter, but sceKernelAllocMemBlockForVM takes no address
  --      and the kernel puts every block after the newlib heap - +139.6 MB
  --      with a 144 MB heap. love.cpp now reserves a 4 MB executable pool
  --      before the heap exists, which lands at +10.7 MB, and lj_mcode.c
  --      carves its areas out of that.
  --   2. vdpm's luajit was built with a host whose enums are 4 bytes while
  --      the ARM EABI makes them 1, so the VM read J->trace 4 bytes past
  --      where the library wrote it and entering a trace jumped through
  --      garbage. Rebuilt with build_luajit.sh, which verifies the offset.
  --
  -- Measured on hardware afterwards, 4 traces compiled and 0 aborted:
  -- a float loop went 96 ms -> 15 ms, and the loop shaped like the chip
  -- synth (bit ops + table reads) went 657 ms -> 35 ms.
  --
  -- This is the MAIN state only. LuaThread.cpp turns the JIT off in every
  -- worker state on this console: build_jitmt.py showed by elimination that
  -- one state compiling repeatedly is fine and three more running interpreted
  -- alongside it are fine, but the moment those states compile too the
  -- process dies - they share one mcode region, and the cache flush after
  -- writing a trace acts on a whole block. So the audio synth stays
  -- interpreted and does not get the 19x.

  -- Give the compiler room. LuaJIT's default budget is 512 KB of machine code
  -- (maxmcode) in 32 KB areas; when a program's live traces exceed that,
  -- lj_trace_flushall throws ALL of them away and every hot path has to be
  -- recorded and compiled again. On a codebase this size that cycle repeats,
  -- and each round is a visible stall - which is what "randomly freezes for a
  -- few seconds" is. The pool love.cpp reserves is 4 MB, so 2 MB of mcode in
  -- 128 KB areas fits with room to spare and stops the thrashing.
  -- maxtrace matters as much as maxmcode and was left at its default the
  -- first time: LuaJIT keeps at most 1000 traces, and exceeding EITHER limit
  -- calls lj_trace_flushall, which discards every trace so each hot path has
  -- to be recorded and compiled again. Raising only maxmcode made the stalls
  -- rarer without removing them, which is what a 1000-trace ceiling on a
  -- codebase this size looks like. maxsnap goes up for the same reason.
  -- Compile LESS, which is the opposite of what the earlier tuning did.
  --
  -- Measured with the frame log (ux0:data/g1r-frames.txt): every multi-second
  -- freeze lands exactly on LuaJIT taking new mcode areas - 6710 ms with +4
  -- areas, 5223 ms with +4, 4435 ms with +4, 3870 ms with +3 - while the
  -- sub-second blips take none. Startup is the same effect at scale: 34
  -- seconds for +25 areas. So the stalls are bursts of trace recording and
  -- assembly, done synchronously on the game thread, and this CPU is slow at
  -- it (~800 KB of machine code cost 34 s).
  --
  -- hotloop is how many iterations a loop runs before LuaJIT records it, and
  -- hotexit the same for a side exit. Raising both means only genuinely hot
  -- code is ever compiled: the synth and the render loops still get it, while
  -- the long tail of code that runs a few times stays interpreted and costs
  -- nothing to compile. That shrinks the bursts rather than rescheduling them.
  --
  -- Note this is the opposite direction from the maxtrace=8000 change that
  -- went badly: that REMOVED the ceiling on how much gets compiled.
  -- ...and raising them made it WORSE: same mcode areas, about double the
  -- time per area (startup 34 s -> 58 s, typical stall 4-6 s -> 8-12 s). The
  -- code is genuinely hot, so delaying compilation does not avoid it; it only
  -- defers each burst and lengthens it.
  --   pcall(jit.opt.start, "hotloop=150", "hotexit=20")
  --
  -- So the JIT is off. Its compile cost on this CPU is not worth its benefit
  -- HERE: the frame log ties every multi-second freeze to trace assembly, and
  -- desktop profiling of this game at Vita settings already showed Lua under
  -- 1 ms/frame with jit.off - the base game is not Lua-bound, so there is
  -- little for the compiler to win back. The measured 6.4x/18.8x wins were on
  -- synthetic loops; the one real beneficiary would be the audio synth, which
  -- runs in a worker state where the JIT has to stay off anyway (see below).
  --
  -- This is not "the JIT does not work" - it does, and getting there fixed two
  -- genuine upstream bugs. It is that ~800 KB of machine code costs this
  -- console 34 seconds to produce, and nothing in this game earns that back.
  -- If it is ever worth revisiting, start with LUAJIT_USE_SYSMALLOC, which
  -- lj_arch.h sets for PSP2: the compiler then allocates through newlib's
  -- malloc instead of LuaJIT's own dlmalloc, and that is a plausible reason
  -- assembly is this slow.
  -- JIT ON with one knob, because the voxel mods this port is being prepared
  -- for are the Lua-heavy workload the compiler would actually pay for. The
  -- base game is not (Lua under 1 ms/frame interpreted), so if this build
  -- stalls, turning the JIT off again costs the base game nothing.
  --
  -- maxmcode is the single change, and it is the one thing that directly
  -- targets what the frame log measured. LuaJIT holds at most maxmcode of
  -- machine code - 512 KB by default, which at the default 32 KB per area is
  -- 16 areas live. The log showed cumulative areas climbing 21 -> 45 across a
  -- session, i.e. the ceiling being hit and lj_trace_flushall discarding
  -- EVERYTHING several times, each followed by recompiling from scratch.
  -- Those are the 4-12 second stalls. Raising the ceiling makes the flush
  -- rare instead of periodic. 2 MB sits well inside the 4 MB pool.
  --
  -- This knob has never been tested on its own: the build that carried it
  -- also had maxtrace=8000, which removed the ceiling on how much gets
  -- compiled (the wrong direction), and instrumentation that was hammering
  -- the memory card. Both are gone, so this is a single-variable test.
  --
  -- It will not help the one-off startup cost (+25 areas, 34 s) - that is
  -- first-time compilation, not a flush - but that happens once.
  -- The compiler is off by default and opt-in per workload.
  --
  -- Why off: the frame log tied every multi-second stall to trace assembly,
  -- and the cost is the compiler itself, not anything fixable around it. Both
  -- candidates were measured and cleared - the mcode pool writes at 610 MB/s,
  -- the same as ordinary heap (so it is cached, not uncached), and newlib
  -- malloc runs 1.27 us per alloc/free pair, which would need millions of
  -- allocations to explain a 4.5 s stall. What is left is recording,
  -- optimising and register allocation on a 444 MHz in-order Cortex-A9, about
  -- 50x slower than a desktop at exactly the branchy pointer-chasing work a
  -- compiler is. Roughly what this CPU is, and not a bug. Tuning only moved
  -- the cost around: maxmcode=2048 never even reached a flush and stalled the
  -- same, and hotloop=150 made each burst longer by batching it.
  --
  -- Why opt-in rather than gone: traces compiled while the compiler was on
  -- KEEP RUNNING after it is switched off - only jit.flush() discards them.
  -- Verified on the desktop LuaJIT: 13.0 ms interpreted, 2.0 ms compiled,
  -- still 2.0 ms after jit.off(). So a workload that earns the compiler can
  -- pay for it once, deliberately, while nothing else in the engine ever
  -- does. That is the shape voxel mesh building wants: warm the builder at
  -- load time, then run compiled for the rest of the session.
  if type(jit) == "table" and jit.off then
    jit.off()
    if jit.flush then pcall(jit.flush) end

    -- Available to mods as POKEPORT_VITA_JIT.
    _G.POKEPORT_VITA_JIT = {
      -- warm(fn, ...) runs fn with the compiler on and switches it off again,
      -- keeping whatever it compiled. Call it once, on code you know is hot -
      -- a few representative mesh builds - not on a whole frame.
      warm = function(fn, ...)
        if type(fn) ~= "function" then return end
        jit.on()
        local ok, err = pcall(fn, ...)
        jit.off()
        if not ok then return nil, err end
        return true
      end,
      on = function() jit.on() end,
      off = function() jit.off() end,
    }
  end

  -- The rest stays off, because it has never actually run on hardware and the
  -- first build in which it did is the first that would not load a game.
  --
  -- The configuration that reached the overworld was the shim doing nothing
  -- at all, which means LuaJIT's defaults. Raising maxtrace from 1000 to 8000
  -- removes the flush that used to cap how much gets compiled, so the engine's
  -- one-shot loading code - thousands of cold paths that run once - keeps
  -- being recorded and compiled instead of being left to the interpreter.
  -- That is consistent with what the console shows: a long wait before the
  -- launcher, then a game that never finishes loading and never reaches sound.
  -- The stall numbers that motivated this were measured while the shim was
  -- inert, so they were never evidence for these values in the first place.
  --   pcall(jit.opt.start, "maxmcode=3072", "sizemcode=128",
  --         "maxtrace=8000", "maxsnap=2000")

  local engineConf = love.conf
  function love.conf(t)
    engineConf(t)
    -- t.version is deliberately left as the engine set it (11.5). Declaring
    -- "11.4" was meant to avoid a compatibility dialog, but it also puts LOVE
    -- into 11.4 compatibility semantics, and the runs that actually reached
    -- the overworld all asked for 11.5 - because the shim was inert. Only the
    -- window is changed here now; anything else is a second variable in a
    -- build whose job is to prove the window fix and nothing more.
    -- One fixed 960x544 panel.  Flags follow the engine's own console branch
    -- (the `nx` case in conf_engine.lua) rather than being invented here:
    -- explicit panel size, windowed, resizable, no highdpi.  resizable=true
    -- looks wrong for a panel that cannot be resized, but every other
    -- platform in the engine's conf sets it - the Switch comment explains
    -- that SDL only follows the real display when the window is resizable
    -- and not exclusive fullscreen, and a non-resizable window instead locks
    -- SDL to the aspect it was created with.  A Vita is the same shape of
    -- problem, so it gets the same answer.
    t.window.width = 960
    t.window.height = 544
    t.window.minwidth = 1
    t.window.minheight = 1
    t.window.fullscreen = false
    t.window.resizable = true
    t.window.highdpi = false
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
    -- POKEPORT_HANDHELD and POKEPORT_AUDIO_RATE are OFF for now, deliberately.
    --
    -- Until the detection bug above was fixed this whole block never ran, so
    -- neither has ever been exercised on hardware - and the first build in
    -- which they did take effect is the first build where the launcher works
    -- but starting a game gives a black screen. The engine still renders
    -- there (battle-FX shaders compile and map their uniforms) and the
    -- process does not fault, so something is being drawn and not shown.
    --
    -- The last configuration known to reach the overworld is "shim entirely
    -- off", which is also these two off. The window size and the JIT tuning
    -- are what this shim is actually needed for; the handheld profile
    -- (FrameCap pacing, pad launcher, on-screen keyboard) and the 22050 synth
    -- rate are improvements to re-introduce ONE at a time once the game runs
    -- again. POKEPORT_HANDHELD is the more suspect of the two: it routes the
    -- frame loop through FrameCap/PresentSync, which decides whether a frame
    -- is presented at all.
    --   POKEPORT_HANDHELD = "1",
    --   POKEPORT_AUDIO_RATE = AUDIO_RATE ~= "" and AUDIO_RATE or nil,
    -- No idle render governor here. The ARM handheld build drops to 6 fps
    -- after 10 s without input to save power, but menus and dialogue are
    -- exactly the static screens that triggers, and at 6 fps a button press
    -- takes up to ~170 ms to be noticed: on hardware it read as the whole
    -- game lagging.
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
