# mdict-mquery

A high-performance Python library for reading and querying MDX/MDD dictionary files.

Forked from the original [mdict-query](https://github.com/mmjang/mdict-query) (2014, unmaintained) and rebuilt for modern Python with aggressive performance optimizations.

## Features

- **In-memory index** — entire dictionary index loaded into a Python dict at init for O(1) key lookups (no per-query SQLite overhead)
- **Block decompression cache** — LRU cache for decompressed record blocks (256 blocks)
- **Result cache** — LRU cache for lookup results (8,192 entries)
- **Persistent file handle** — single file handle with thread lock (no open/close per lookup)
- **Thread-safe** — safe for concurrent access from multiple threads
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

# Look up a word
results = ib.mdx_lookup("食べる")
for entry in results:
    print(entry)

# Get all keys
keys = ib.get_mdx_keys()
print(f"{len(keys):,} entries")
```

## Benchmarks

Tested with 22 Japanese dictionaries (8.87 million keys total):

| Metric | mdict-mquery | Original mdict-query | Speedup |
|---|---|---|---|
| Cold lookups (660) | 216ms | ~35,000ms | **162x** |
| Warm lookups (660) | 0.1ms | ~35,000ms | **261,389x** |
| Realistic (4,400) | 359ms | ~35,000ms | **~97x** |
| Per-lookup (warm) | 0.2μs | ~53ms | at hardware floor |

## How It Works

The original `mdict-query` opened a new SQLite connection for every single lookup, resulting in ~25,000 connect/execute/close cycles per run. This library eliminates that by:

1. Loading the entire SQLite index into a Python dict at init time
2. Keeping the MDX file open with a persistent, thread-locked file handle
3. Caching decompressed record blocks in an OrderedDict LRU (avoids re-decompressing the same zlib blocks)
4. Caching final lookup results in a separate OrderedDict LRU

After the one-time init cost (~28s for 22 dictionaries), individual lookups run at microsecond scale.

## License

MIT
