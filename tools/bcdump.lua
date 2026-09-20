-- Precompile engine sources to LuaJIT 2.0 bytecode (dump format 1, what the
-- Vita runtime's LuaJIT 2.0.5 loads).  Run by a LuaJIT 2.0 host binary:
--
--   luajit bcdump.lua <listfile> <outdir>
--
-- listfile: one "relative/path.lua<TAB>absolute source path" per line.
-- Each chunk is compiled under "@relative/path.lua", the name LÖVE itself
-- gives it, keeps its debug info (line numbers in error messages), and is
-- loaded back before it is written so a bad dump fails the build here.

-- Compiling hundreds of files in one loop lets the JIT trace this loop, and
-- traced string.dump then fails intermittently ("unable to dump given
-- function") or crashes the host outright (0xC0000005).  Interpreted, it is
-- reliable and still fast -- and this runs once per build.
jit.off()

local list, outdir = assert(arg[1], "listfile"), assert(arg[2], "outdir")
assert(jit and jit.version:match("^LuaJIT 2%.0"), "needs a LuaJIT 2.0 host, got " .. tostring(jit and jit.version))

local count, inBytes, outBytes = 0, 0, 0
for line in io.lines(list) do
  local rel, src = line:match("^([^\t]+)\t(.+)$")
  local f = assert(io.open(src, "rb"))
  local text = f:read("*a")
  f:close()
  local chunk, err = loadstring(text, "@" .. rel)
  if not chunk then
    io.stderr:write("COMPILE FAIL " .. rel .. ": " .. tostring(err) .. "\n")
    os.exit(1)
  end
  local bc = string.dump(chunk)
  assert(bc:byte(4) == 1, rel .. ": dump format is not 1")
  assert(bc:byte(5) % 4 < 2, rel .. ": dump is stripped")      -- BCDUMP_F_STRIP = 0x02
  assert(loadstring(bc, "@" .. rel), rel .. ": dump does not load back")
  -- the caller creates the directory tree: one os.execute("mkdir") per file
  -- spawned 500+ processes and crashed the host (0xC0000005)
  local o = assert(io.open(outdir .. "/" .. rel, "wb"))
  o:write(bc)
  o:close()
  count, inBytes, outBytes = count + 1, inBytes + #text, outBytes + #bc
end
print(("compiled %d files, %.1f MB source -> %.1f MB bytecode (%s)"):format(
  count, inBytes / 1e6, outBytes / 1e6, jit.version))
