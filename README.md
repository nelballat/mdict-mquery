# mdict-mquery

A high-performance Python library for reading and querying MDX/MDD dictionary files.

Forked from the original [mdict-query](https://github.com/mmjang/mdict-query) (2014, unmaintained) and rebuilt for modern Python with aggressive performance optimizations.

## Features

- **SQLite-backed lookups** — persistent, read-optimized SQLite connection for on-demand record lookups (immutable mode, mmap, no journaling)
- **Pickle cache** — pre-built key structures cached to disk; subsequent loads bypass SQLite key queries (~20x faster init vs baseline)
- **Pre-built key collections** — `key_set` (frozenset), `sorted_keys` (sorted list), `kanji_index` (CJK reverse index) built once at init
- **Block decompression cache** — LRU cache for decompressed record blocks (256 blocks)
- **Result cache** — LRU cache for lookup results (8,192 entries)
- **Persistent file handle** — single file handle with thread lock (no open/close per lookup)
- **Thread-safe** — safe for concurrent access from multiple threads
- **Zero RAM for record data** — record positions stay in SQLite (B-tree indexed), not loaded into memory
- **Python 3 only** — no legacy compatibility code

## Installation

```bash
pip install mdict-mquery
```

## Usage

```python
from mdict_mquery import IndexBuilder

# Load a dictionary
ib = IndexBuilder("path/to/dictionary.mdx")

# Look up a word (SQLite B-tree + block cache + result cache)
results = ib.mdx_lookup("食べる")
for entry in results:
    print(entry)

# Pre-built key collections (no copying overhead)
keys = ib.key_set          # frozenset — O(1) membership test
sorted_keys = ib.sorted_keys  # sorted list — binary search ready
kanji_idx = ib.kanji_index    # CJK char → list of keys with that kanji in 【...】

# Legacy API still works
keys_list = ib.get_mdx_keys()
print(f"{len(keys_list):,} entries")
```

## Benchmarks

Tested with 22 Japanese dictionaries (8.87 million keys total):

### Init Performance
| Scenario | Time | Notes |
|---|---|---|
| First run (build cache) | ~9s | Queries SQLite for sorted keys + saves pickle |
| Cached run | **~2.4s** | Loads sorted_keys + kanji_index from pickle |
| Original baseline | ~49s | Old _mem_index approach |

### Lookup Performance (デジタル大辞泉, 956K keys)
| Metric | Latency | Notes |
|---|---|---|
| Cold lookup | **0.18ms/word** | SQLite B-tree + block decompress |
| Warm lookup | **0.0003ms/word** | Result cache hit |
| Heavy (200 random) | **0.49ms/word** | Mixed block cache |

## Architecture

```
.mdx file ──→ .mdx.db (SQLite index, built once)
                  │
                  ├── .mdx.cache (pickle: sorted_keys + kanji_index)
                  │   └── Loaded at init for search/membership
                  │
                  └── Persistent SQLite connection (immutable, mmap'd)
                      └── Queried on-demand for mdx_lookup()
```

**Key insight**: The `.mdx.db` SQLite files already contain all record positions with a B-tree index. Loading them into a Python dict (`_mem_index`) was redundant — ~2GB of RAM for data that SQLite can query in 0.18ms. By keeping record data in SQLite and caching only the lightweight key structures, we get:

- **20x faster init** (2.4s vs 49s)
- **~200MB cache** (vs ~636MB with full _mem_index pickle)
- **Zero RAM for record positions** (SQLite handles it)
- **Same lookup speed** (0.18ms cold, microseconds warm)

### SQLite Optimizations (read-only)
- `immutable=1` — no file locking overhead
- `PRAGMA mmap_size=512MB` — memory-mapped I/O (zero-copy reads)
- `PRAGMA journal_mode=OFF` — no journaling
- `PRAGMA temp_store=MEMORY` — temp tables in RAM
- `PRAGMA cache_size=-64000` — 64MB page cache

## License

MIT
