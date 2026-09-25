# Chunked cache payloads for potato_voxel on PS Vita

Design, 2026-09-25. Targets a fork of `ShaneMcGovernIE/potato_voxel` at 1.9.4.

## Goal

Bound the peak memory of building and loading one voxel mesh slot, so a PS Vita
can build and read its own cache instead of needing a desktop bake copied onto
the card.

## Why: the measured problem

All figures from hardware and desktop runs on 2026-09-25. Evidence:
`docs/superpowers/evidence/` and `vita-probe/build/vita_prebuild_log*.txt`.

| Measurement | Value |
| --- | --- |
| Peak Lua heap, desktop bake of 444 jobs | **536 MB** |
| Live heap after a forced full GC, Vita | **109.9 MB** (only ~3 MB collectable) |
| GPU texture memory during the same build | **4.9 MB**, flat |
| Largest single baked file | **32.2 MB** (`VIRIDIAN_FOREST/shared/deco.bin`) |
| Whole baked cache | 493 MB over 222 maps, ~2.2 MB average |

The live-set measurement is the important one: the memory is not garbage, so GC
tuning cannot help, and raising the heap only moved the wall (47 MB peak at a
124 MB heap, 123.5 MB at a 200 MB heap, crashing either way). `texmem` staying at
4.9 MB says the builder is not handing geometry to the GPU as it goes.

### Exactly what holds the memory

`MeshCache.savePackedChunks` (`lib/MeshCache.lua:960`) is the whole story:

```lua
local raw, err = GeometryStream.toPayload(stream)  -- concatenates EVERY chunk
local bytes = packPayload(fp, raw)                 -- header + lz4/zstd: another copy
return writePayload(key, mkey, bytes, fp)          -- one whole-value writeBytes
```

So at the moment of writing a slot, three full representations coexist: the
stream's retained chunk strings, the concatenated `raw`, and the compressed
`bytes`. `saveTerrain` and `saveWater` follow the same shape via
`encodeIndexed`. For the largest slots that is comfortably the 110 MB we
measured.

**The read path has the same defect**, which we had not accounted for:
`readPayload` (`lib/MeshCache.lua:620`) does `readBytes(key)` for the ENTIRE
payload and then `unpackPayload` decompresses it. Loading `VIRIDIAN_FOREST`
means a 32.2 MB compressed string plus a larger decompressed buffer. **A desktop
bake copied to the card would therefore have risked failing at LOAD time on big
maps even if the transfer had succeeded.** Chunking has to apply in both
directions.

### Why whole-value writes exist

Not an accident. `lib/MeshCache.lua:595-599` records the invariant: the meta
record's presence is the commit marker, and "the engine's storage writes are
whole-value and crash-safe, so a meta record can never describe a torn payload."
Any design here must preserve that.

### What is already in our favour

The renderer needs no change, because it is already chunk-shaped:

- `lib/ChunkMesher.lua:94` slices deliberately: "a one-shot `newMesh(rows)` on a
  500k-vertex" mesh is what it exists to avoid. A slot is already **many**
  meshes.
- `lib/MeshRuntime.lua:80-105` builds one mesh per chunk from packed bytes via
  `GeometryStream.payloadVertexBytes(data.packed, info)`.
- `GeometryStream` is explicitly chunk-structured and bounded
  (`MAX_CHUNK_VERTICES = 16384`, `MAX_IN_FLIGHT_CHUNKS = 4`), and its header says
  the writer "keeps flat numeric arrays only until flush".

So only the storage granularity is wrong, not the geometry pipeline.

## Non-goals

- Reducing draw cost. Vertex counts, render distance and the quality ladder are
  a separate question, and draw-only cost has still never been measured on
  hardware because every run so far died while building.
- Changing the mesh format, the identity scheme or the compression codecs.
- Redistribution. potato_voxel ships **no license file**, so this is a personal
  fork or an upstream PR, not something to publish.

## Design: part-addressed payloads

### Key shape

`…/<slot>/deco` becomes `…/<slot>/deco_0001`, `deco_0002`, and so on, keeping
the existing directory depth. A `deco/0001` sub-level would add a directory
level, and directory creation at depth 11 fails on the test console for reasons
still not understood (see Risks), so the flat suffix is deliberate.

Meta keys are unchanged: one meta record per slot, not per part.

### Write path

1. As `GeometryStream` flushes chunks, accumulate them until a part budget is
   reached (**1 MB target**, see Risks), then compress that part, `writeBytes` it,
   and release the buffer before continuing.
2. After the last part, write the meta record, extended with `parts` (count) and
   `partLens` (per-part raw and packed lengths).
3. **Meta last preserves the existing invariant.** Its presence still implies
   every part landed, so a torn write leaves parts with no meta, which reads as
   absent exactly as today.

Peak write memory becomes one part plus the stream's bounded in-flight chunks,
rather than three copies of a slot.

### Read path

1. Read the meta record. If `parts` is absent, treat the payload as a
   single-part legacy file so the code path is uniform.
2. For each part in order: `readBytes`, decompress, walk its chunk infos, build a
   mesh per chunk through the existing `MeshRuntime` path, then drop the part
   string before reading the next.

Peak read memory becomes one part plus its decompressed buffer.

### Format version

Bump `format`. It is already part of the cache identity
(`format|version|activeVersion|profile|dataKey|voidFill`), so existing caches are
rejected cleanly and rebuilt rather than misread.

## Consequence worth stating plainly

If peak build memory drops to a few MB, **the Vita can bake its own cache**. That
removes the 493 MB transfer, the USB or FTP question, and the depth-11 directory
problem, and it makes the 536 MB desktop figure irrelevant rather than
load-bearing. The desktop bake driver stays useful for producing a cache quickly,
but stops being a requirement.

## Risks and open questions

- **The producer must become resumable.** `savePackedChunks` is called once with
  a complete stream, so flushing parts mid-slot means changing where the stream
  is drained, not just how it is written. This is the largest unknown in the
  design and the first thing to prototype.
- **Part size is a guess.** 1 MB is chosen to sit well under the Vita's budget
  while keeping file count sane: the cache is already 4450 files, and 1 MB parts
  would add roughly 500 to 1000 more. Worth measuring both peak memory and load
  time at 512 KB, 1 MB and 4 MB rather than assuming.
- **Compression ratio may worsen.** lz4 and zstd do better on 32 MB than on 1 MB
  windows, so the cache may grow. Acceptable if memory drops, but measure it.
- **Directory creation at depth 11 fails on the test console**, cause unknown
  (not free space: 21 GB free). The flat `_0001` suffix avoids depending on it,
  but if the engine's `ensureParent` cannot create the existing
  `maps/<MAP>/<slot>/` level either, that is a separate blocker to settle first.
- **An engine-side `appendBytes` would be cleaner** than N part keys, and is
  plausibly upstreamable, since mod storage is whole-value only today
  (`write`/`read`/`writeBytes`/`readBytes`/`list`/`delete`). Part keys need no
  engine change, so start there and treat `appendBytes` as a follow-up.

## Verification

1. **Desktop first, with a memory assertion.** Re-run the bake driver and record
   peak `collectgarbage("count")`. The target is a peak that no longer scales
   with the largest slot. This is free and catches a wrong design before any
   hardware trip.
2. **Round-trip equality.** Bake a map before and after the change and assert the
   reconstructed vertex and index bytes are identical, so chunking is proven not
   to alter geometry.
3. **Load-path memory on the biggest map.** `VIRIDIAN_FOREST` at 32.2 MB is the
   worst case; measure peak heap loading it.
4. **Then hardware**, at the POTATO rung, with the heartbeat instrumentation
   already in place. Two questions: does a map build without dying, and what is
   the frame time once nothing is building. The second number is the one this
   whole effort has been unable to obtain.
