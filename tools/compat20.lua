-- List every engine file LuaJIT 2.0 cannot compile: luajit compat20.lua <listfile>
local fails = 0
for line in io.lines(arg[1]) do
  local rel, src = line:match("^([^\t]+)\t(.+)$")
  local f = assert(io.open(src, "rb")); local t = f:read("*a"); f:close()
  local ok, err = loadstring(t, "@" .. rel)
  if not ok then fails = fails + 1; print(err) end
end
print(fails .. " files fail to compile under " .. jit.version)
