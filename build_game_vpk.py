"""Package the Gen1Recomp engine as a PS Vita VPK (isage's LÖVE 11.4 runtime).

    python build_game_vpk.py --engine C:/g2dev [--audio-rate 22050]

game.love holds the same file set as the desktop release (scripts/pack_love.sh)
plus the Vita conf.lua shim in vita/conf.lua; the engine's own conf.lua ships
as conf_engine.lua.  No ROM and no ROM-derived data goes in: the player copies
their .gb into the save folder and the engine imports it on first launch.

Output: build/gen1recomp-vita.vpk and build/gen1recomp-vita.love (the same
game.love, runnable on desktop LÖVE for testing).
"""

import argparse
import io
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from PIL import Image

import build_vpk as base

HERE = Path(__file__).resolve().parent
# LuaJIT v2.0 branch @ 3000f7c, built with src/msvcbuild.bat: its dump format
# (BCDUMP_VERSION 1) is the one the Vita runtime's LuaJIT 2.0.5 loads; 2.1's is not
LUAJIT20 = HERE / "vendor" / "luajit-2.0-src" / "LuaJIT-3000f7cdb1123f2a4d8b690d4292c541359c670c" / "src" / "luajit.exe"

TITLE_ID = "GENR00001"
TITLE = "Gen1Recomp"
STITLE = "Gen1Recomp"

# scripts/pack_love.sh, kept in step by hand -- and checked against it at build
# time by check_release_set(), because the hand-keeping failed once: the engine
# added tools/rom_manifest_firered.json and tools/rom_manifest_leafgreen.json
# and this list did not, so the VPK would have shipped without them.
INCLUDE = [
    "main.lua", "conf.lua", "src", "data", "assets", "tools/save-editor",
    "tools/rom_manifest.json", "tools/rom_manifest_blue.json",
    "tools/rom_manifest_yellow.json", "tools/rom_manifest_gold.json",
    "tools/rom_manifest_silver.json", "tools/rom_manifest_crystal.json",
    "tools/rom_manifest_firered.json", "tools/rom_manifest_leafgreen.json",
    "PATCH_NOTES.md",
    # Patch notes for the in-game updater, which reads it from the package
    # (src/update/PatchNotes.lua); pack_love.sh adds it when it exists.
    "mobile/ios/app-repo.json",
]
EXCLUDE_PREFIXES = ("data/generated/", "assets/generated/")
# pack_love.sh's own post-pack assertions, plus conf_engine.lua, which only
# this build produces.
REQUIRED = [
    "main.lua", "conf_engine.lua", "src/import/LauncherView.lua", "src/ui/kit/Kit.lua",
    "tools/save-editor/App.lua", "tools/save-editor/Kit.lua",
    "tools/save-editor/PadInput.lua", "tools/save-editor/panels/Party.lua",
    "tools/rom_manifest.json", "tools/rom_manifest_blue.json",
    "tools/rom_manifest_yellow.json", "tools/rom_manifest_gold.json",
    "tools/rom_manifest_silver.json", "tools/rom_manifest_crystal.json",
    "tools/rom_manifest_firered.json", "tools/rom_manifest_leafgreen.json",
]
ROM_SUFFIXES = (".gb", ".gbc", ".sav")


UTF8_ESCAPE = re.compile(r'(?<!\\)((?:\\\\)*)\\u\{([0-9A-Fa-f]{1,6})\}')
QUOTED = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')


def lua20_compatible(text):
    """Rewrite Lua 5.3 / LuaJIT 2.1 "\\u{XXXX}" escapes as the same UTF-8 bytes
    in "\\ddd" form, which LuaJIT 2.0 also reads.  Only inside ordinary quoted
    strings: long strings do not process escapes, so they are left alone."""
    def fix_string(m):
        def to_bytes(e):
            return e.group(1) + "".join(f"\\{b}" for b in chr(int(e.group(2), 16)).encode("utf-8"))
        return UTF8_ESCAPE.sub(to_bytes, m.group(0))
    return QUOTED.sub(fix_string, text) if "\\u{" in text else text


def engine_files(engine):
    """(path, arcname) for the release file set; sources LuaJIT 2.0 cannot
    read are replaced by a rewritten copy under build/lua20/."""
    for f, rel in _release_files(engine):
        if rel.endswith(".lua"):
            text = f.read_text(encoding="utf-8")
            fixed = lua20_compatible(text)
            if fixed != text:
                out = base.BUILD / "lua20" / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(fixed, encoding="utf-8", newline="")
                print(f"rewrote \\u{{}} escapes for LuaJIT 2.0: {rel}")
                f = out
        yield f, rel


PACK_SCRIPT = "scripts/pack_love.sh"


def check_release_set(engine):
    """Fail if scripts/pack_love.sh packs something INCLUDE does not.

    The desktop release's file set lives in that script and INCLUDE is a
    hand-copy of it, so the two drift silently: the engine adds a file, this
    build keeps working, and the VPK ships without it.  Only paths the
    checkout actually has count, which skips CI-only names like
    build-info.json that the script zips from elsewhere.
    """
    script = engine / PACK_SCRIPT
    if not script.is_file():
        print(f"warning: no {PACK_SCRIPT}; release file set unchecked")
        return
    # Line continuations first: the main zip spans six lines.
    text = script.read_text(encoding="utf-8").replace("\\\n", " ")
    packed = []
    main = re.search(r'zip\s+-q\s+-9\s+-r\s+"\$OUTPUT"(.*?)\s-x\s', text)
    if main:
        packed += main.group(1).split()
    packed += [t.rstrip(")") for t in re.findall(r'zip\s+-q\s+"\$OUTPUT"\s+(\S+)', text)]
    unpacked = sorted({p for p in packed if p not in INCLUDE and (engine / p).exists()})
    if unpacked:
        raise SystemExit(
            f"{PACK_SCRIPT} packs files INCLUDE does not: {', '.join(unpacked)}\n"
            "Add them to INCLUDE, and to REQUIRED if pack_love.sh asserts them.")
    stale = [p for p in INCLUDE if p not in packed]
    if stale:
        print(f"warning: INCLUDE has entries {PACK_SCRIPT} no longer packs: {', '.join(stale)}")


def _release_files(engine):
    for entry in INCLUDE:
        path = engine / entry
        if path.is_file():
            yield path, entry
        elif path.is_dir():
            for f in sorted(path.rglob("*")):
                rel = f.relative_to(engine).as_posix()
                if f.is_file() and not rel.startswith(EXCLUDE_PREFIXES) \
                        and f.name != ".DS_Store":
                    yield f, rel


def precompile(engine, luajit):
    """Compile every engine .lua to LuaJIT 2.0 bytecode; returns {arcname: path}."""
    out = base.BUILD / "bytecode"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    listing = base.BUILD / "bytecode-list.txt"
    with open(listing, "w", encoding="utf-8", newline="\n") as fh:
        for f, rel in engine_files(engine):
            if rel.endswith(".lua"):
                arc = "conf_engine.lua" if rel == "conf.lua" else rel
                (out / arc).parent.mkdir(parents=True, exist_ok=True)
                fh.write(f"{arc}\t{f.resolve()}\n")
    run = subprocess.run([str(luajit), str(HERE / "tools" / "bcdump.lua"), str(listing), str(out)],
                         capture_output=True, text=True)
    if run.returncode != 0:
        raise SystemExit(f"bytecode compile failed (exit {run.returncode}):\n{run.stdout}{run.stderr}")
    print(run.stdout.strip())
    return {p.relative_to(out).as_posix(): p for p in out.rglob("*.lua")}


def build_love(engine, audio_rate, compiled):
    buf = io.BytesIO()
    names = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f, rel in engine_files(engine):
            arc = "conf_engine.lua" if rel == "conf.lua" else rel
            z.write(compiled.get(arc, f), arc)
            names.append(arc)
        shim = (HERE / "vita" / "conf.lua").read_text(encoding="utf-8")
        z.writestr("conf.lua", shim.replace("@AUDIO_RATE@", audio_rate))
        names.append("conf.lua")
    for req in REQUIRED:
        if req not in names:
            raise SystemExit(f"game.love is missing {req}")
    leaked = [n for n in names if n.lower().endswith(ROM_SUFFIXES) or n.startswith(EXCLUDE_PREFIXES)]
    if leaked:
        raise SystemExit(f"game.love would contain ROM data: {leaked[:5]}")
    return buf.getvalue(), len(names)


def engine_commit(engine):
    try:
        return subprocess.run(["git", "-C", str(engine), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="C:/g2dev", help="Gen1Recomp engine checkout")
    ap.add_argument("--audio-rate", default="22050",
                    help="music synth rate on the Vita; '' keeps the engine default (44100)")
    ap.add_argument("--extended-memory", action="store_true",
                    help="set param.sfo ATTRIBUTE2=12 (the extended memory mode big Vita "
                         "ports use). The runtime asks for a 200 MB heap; the default "
                         "pool is smaller. Untested on hardware.")
    ap.add_argument("--eboot", default=None,
                    help="LÖVE runtime to package (default: isage's prebuilt 11.4-vita "
                         "eboot, LuaJIT 2.0.5). Pass vendor/love-vita/love.self to use "
                         "the locally built LuaJIT 2.1 runtime -- see TOOLCHAIN.md")
    ap.add_argument("--luajit", default=str(LUAJIT20),
                    help="LuaJIT 2.0 host binary for precompiling (see README)")
    ap.add_argument("--bytecode", action="store_true",
                    help="ship LuaJIT 2.0 bytecode instead of source: saves seconds of "
                         "startup parsing, but loading bytecode while the collector runs "
                         "crashes LuaJIT intermittently (2.0 and 2.1 both) -- run the "
                         "probe's bytecode test on real hardware before trusting it")
    args = ap.parse_args()
    engine = Path(args.engine)
    check_release_set(engine)

    eboot = Path(args.eboot) if args.eboot else base.fetch("eboot.bin")
    if not eboot.is_file():
        raise SystemExit(f"no such runtime: {eboot}")
    module_zips = {name: base.fetch(name) for name in base.MODULES}

    compiled = precompile(engine, Path(args.luajit)) if args.bytecode else {}
    love_bytes, count = build_love(engine, args.audio_rate, compiled)
    base.BUILD.mkdir(parents=True, exist_ok=True)
    love_out = base.BUILD / "gen1recomp-vita.love"
    love_out.write_bytes(love_bytes)

    base.TITLE_ID, base.TITLE, base.STITLE = TITLE_ID, TITLE, STITLE
    if args.extended_memory:
        base.ATTRIBUTE2 = 12
    icon_src = Image.open(engine / "ports" / "switch" / "assets" / "icon.jpg").convert("RGB")

    vpk = base.BUILD / "gen1recomp-vita.vpk"
    with zipfile.ZipFile(vpk, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(eboot, "eboot.bin")
        z.writestr("sce_sys/param.sfo", base.build_sfo())
        z.writestr("sce_sys/icon0.png", base.indexed_png(icon_src, (128, 128)))
        z.writestr("sce_sys/livearea/contents/bg.png", base.indexed_png(icon_src, (840, 500)))
        z.writestr("sce_sys/livearea/contents/startup.png", base.indexed_png(icon_src, (280, 158)))
        z.writestr("sce_sys/livearea/contents/template.xml", base.LIVEAREA_TEMPLATE)
        for zip_name, names in base.MODULES.items():
            with zipfile.ZipFile(module_zips[zip_name]) as mz:
                for n in names:
                    z.writestr(f"module/{n}", mz.read(n))
        # stored, not deflated: LÖVE mounts it in place and reads it at runtime
        z.writestr(zipfile.ZipInfo("game.love"), love_bytes, compress_type=zipfile.ZIP_STORED)

    print(f"engine {engine} @ {engine_commit(engine)}: {count} files, "
          f"game.love {len(love_bytes) / 1e6:.1f} MB, audio rate {args.audio_rate or 'engine default'}, "
          f"runtime {eboot.name} ({eboot.stat().st_size / 1e6:.1f} MB)")
    print(f"wrote {vpk} ({vpk.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
