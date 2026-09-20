"""Build the Gen1Recomp Vita probe VPK without VitaSDK.

A VPK is a zip: eboot.bin, sce_sys/param.sfo, sce_sys icons/LiveArea, plus
whatever the app loads from app0:.  This script fetches isage's prebuilt
LÖVE 11.4 eboot and the PowerVR/gl4es user modules its SDL build loads from
app0:module (all pinned by SHA-256), extracts every shader source the
engine ships into shaders.lua, and zips the probe as app0:game.love (the
path the eboot hard-codes).

    python build_vpk.py --engine C:/g2dev

Output: build/gen1recomp-vita-probe.vpk and build/game/ (runnable on
desktop LÖVE for a baseline: lovec build/game).
"""

import argparse
import hashlib
import io
import re
import struct
import subprocess
import urllib.request
import zipfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
BUILD = HERE / "build"

TITLE_ID = "GNRP00001"  # 4 letters + 5 digits; probe only, the game gets its own
# param.sfo ATTRIBUTE2: 12 asks the kernel for the extended memory mode that
# big Vita ports use (Rinnegatamante's CMake sets it the same way). The LÖVE
# runtime asks for a 200 MB newlib heap, which the default pool cannot give.
ATTRIBUTE2 = 0
TITLE = "Gen1Recomp Vita Probe"
STITLE = "G1R Probe"

DOWNLOADS = {
    "eboot.bin": (
        "https://github.com/isage/love/releases/download/11.4-vita/eboot.bin",
        "d2e21d574f113ee92afb51638900db0bc304c1e0b5e420922be8de62efbdeb31",
    ),
    "pvr_psp2.zip": (
        "https://github.com/GrapheneCt/PVR_PSP2/releases/download/v3.9/PSVita_Release.zip",
        "ed69be89f21c4894e8009a8c3567c89b1778c8db0beb3c2f4ea134adab4c494f",
    ),
    "gl4es4vita.zip": (
        "https://github.com/SonicMastr/gl4es4vita/releases/download/v1.1.4-vita/PSVita_Release.zip",
        "aa0444263d5ac7006c11174034f85651fbad0aec358c0a16b525335ad9fe3953",
    ),
}

# What SDL_vitagles_pvr.c loads from app0:module (and libGL for gl4es).
MODULES = {
    "pvr_psp2.zip": [
        "libgpu_es4_ext.suprx",
        "libIMGEGL.suprx",
        "libGLESv1_CM.suprx",
        "libGLESv2.suprx",
        "libpvrPSP2_WSEGL.suprx",
    ],
    "gl4es4vita.zip": ["libGL.suprx"],
}


def fetch(name):
    url, sha = DOWNLOADS[name]
    path = VENDOR / name
    if not path.exists():
        VENDOR.mkdir(parents=True, exist_ok=True)
        print(f"downloading {name}")
        with urllib.request.urlopen(url) as resp:
            path.write_bytes(resp.read())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sha:
        raise SystemExit(f"{name}: sha256 {digest} != pinned {sha}")
    return path


# ---------------------------------------------------------------------------
# param.sfo (same keys and defaults vita-mksfoex writes)
# ---------------------------------------------------------------------------

def build_sfo():
    entries = [
        ("APP_VER", "01.00", 8),
        ("ATTRIBUTE", 0x8000, None),
        ("ATTRIBUTE2", ATTRIBUTE2, None),
        ("ATTRIBUTE_MINOR", 0x10, None),
        ("BOOT_FILE", "", 32),
        ("CATEGORY", "gd", 4),
        ("CONTENT_ID", "", 48),
        ("EBOOT_APP_MEMSIZE", 0, None),
        ("EBOOT_ATTRIBUTE", 0, None),
        ("EBOOT_PHY_MEMSIZE", 0, None),
        ("LAREA_TYPE", 0, None),
        ("NP_COMMUNICATION_ID", "", 16),
        ("PARENTAL_LEVEL", 0, None),
        ("PSP2_DISP_VER", "00.000", 8),
        ("PSP2_SYSTEM_VER", 0, None),
        ("STITLE", STITLE, 52),
        ("TITLE", TITLE, 128),
        ("TITLE_ID", TITLE_ID, 12),
        ("VERSION", "00.00", 8),
    ]
    assert [e[0] for e in entries] == sorted(e[0] for e in entries)

    keys = b""
    data = b""
    index = b""
    for key, value, max_len in entries:
        key_off = len(keys)
        keys += key.encode() + b"\0"
        data_off = len(data)
        if isinstance(value, int):
            raw = struct.pack("<I", value)
            fmt, length, max_len = 0x0404, 4, 4
        else:
            raw = value.encode("utf-8") + b"\0"
            fmt, length = 0x0204, len(raw)
            assert length <= max_len, key
            raw = raw.ljust(max_len, b"\0")
        data += raw
        index += struct.pack("<HHIII", key_off, fmt, length, max_len, data_off)
    keys = keys.ljust((len(keys) + 3) & ~3, b"\0")
    key_start = 20 + len(index)
    data_start = key_start + len(keys)
    header = struct.pack("<4sIIII", b"\0PSF", 0x0101, key_start, data_start, len(entries))
    return header + index + keys + data


# ---------------------------------------------------------------------------
# Icons (LiveArea wants 8-bit indexed PNGs)
# ---------------------------------------------------------------------------

def indexed_png(img, size, background=(0, 0, 0)):
    canvas = Image.new("RGB", size, background)
    fitted = img.copy()
    fitted.thumbnail(size, Image.LANCZOS)
    canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    out = io.BytesIO()
    canvas.convert("P", palette=Image.ADAPTIVE, colors=256).save(out, "PNG")
    return out.getvalue()


LIVEAREA_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<livearea style="a1" format-ver="01.00" content-rev="1">
  <livearea-background>
    <image>bg.png</image>
  </livearea-background>
  <gate>
    <startup-image>startup.png</startup-image>
  </gate>
</livearea>
"""


# ---------------------------------------------------------------------------
# Engine shaders -> shaders.lua
# ---------------------------------------------------------------------------

LONG_STRING = re.compile(r"\[(=*)\[(.*?)\]\1\]", re.S)


def extract_shaders(engine):
    found, seen = [], set()
    for path in sorted((engine / "src").rglob("*.lua")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in LONG_STRING.finditer(text):
            if text[max(0, m.start() - 2):m.start()] == "--":
                continue  # long comment
            code = m.group(2)
            if "vec4" not in code or not re.search(r"\b(effect|position)\s*\(", code):
                continue
            if code in seen:
                continue
            seen.add(code)
            line = text.count("\n", 0, m.start()) + 1
            found.append((f"{path.relative_to(engine / 'src').as_posix()}:{line}", code))
    return found


def shaders_lua(shaders):
    out = ["-- Generated by build_vpk.py from the engine's src/. Do not edit.", "return {"]
    for name, code in shaders:
        level = 1
        while f"]{'=' * level}]" in code:
            level += 1
        eq = "=" * level
        out.append(f'  {{ name = "{name}", code = [{eq}[{code}]{eq}] }},')
    out.append("}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------

def bytecode_chunks(engine, count=80):
    """Compile `count` engine modules to LuaJIT 2.0 bytecode for the probe."""
    import build_game_vpk as game

    if not game.LUAJIT20.exists():
        print("no LuaJIT 2.0 host built; probe ships without the bytecode test")
        return {}
    sources = sorted((engine / "src").rglob("*.lua"))[:count]
    out = BUILD / "probe-bytecode"
    out.mkdir(parents=True, exist_ok=True)
    listing = BUILD / "probe-bytecode-list.txt"
    with open(listing, "w", encoding="utf-8", newline="\n") as fh:
        for i, src in enumerate(sources):
            fh.write(f"{i:03d}.luac\t{src.resolve()}\n")
    run = subprocess.run([str(game.LUAJIT20), str(HERE / "tools" / "bcdump.lua"),
                          str(listing), str(out)], capture_output=True, text=True)
    if run.returncode != 0:
        raise SystemExit(f"probe bytecode compile failed:\n{run.stdout}{run.stderr}")
    return {p.name: p.read_bytes() for p in sorted(out.glob("*.luac"))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="C:/g2dev", help="Gen1Recomp engine checkout")
    args = ap.parse_args()
    engine = Path(args.engine)

    eboot = fetch("eboot.bin")
    module_zips = {name: fetch(name) for name in MODULES}

    shaders = extract_shaders(engine)
    print(f"extracted {len(shaders)} engine shaders")

    game_dir = BUILD / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    for src in ("main.lua", "conf.lua"):
        (game_dir / src).write_bytes((HERE / "probe" / src).read_bytes())
    (game_dir / "shaders.lua").write_text(shaders_lua(shaders), encoding="utf-8")

    # Real engine chunks as LuaJIT 2.0 bytecode, for the probe's GC stress
    # test: loading bytecode while the collector runs crashes LuaJIT on the
    # desktop (2.0 and 2.1), and that decides whether the game build can
    # ship precompiled. Skipped when no 2.0 host has been built.
    chunks = bytecode_chunks(engine)

    bc_dir = game_dir / "bc"
    if bc_dir.exists():
        for stale in bc_dir.iterdir():
            stale.unlink()
    elif chunks:
        bc_dir.mkdir()
    for name, data in chunks.items():
        (bc_dir / name).write_bytes(data)

    love_bytes = io.BytesIO()
    with zipfile.ZipFile(love_bytes, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(game_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(game_dir).as_posix())

    icon_src = Image.open(engine / "ports" / "switch" / "assets" / "icon.jpg").convert("RGB")

    vpk = BUILD / "gen1recomp-vita-probe.vpk"
    with zipfile.ZipFile(vpk, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(eboot, "eboot.bin")
        z.writestr("sce_sys/param.sfo", build_sfo())
        z.writestr("sce_sys/icon0.png", indexed_png(icon_src, (128, 128)))
        z.writestr("sce_sys/livearea/contents/bg.png", indexed_png(icon_src, (840, 500)))
        z.writestr("sce_sys/livearea/contents/startup.png", indexed_png(icon_src, (280, 158)))
        z.writestr("sce_sys/livearea/contents/template.xml", LIVEAREA_TEMPLATE)
        for zip_name, names in MODULES.items():
            with zipfile.ZipFile(module_zips[zip_name]) as mz:
                for n in names:
                    z.writestr(f"module/{n}", mz.read(n))
        z.writestr("game.love", love_bytes.getvalue())
    print(f"wrote {vpk} ({vpk.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
