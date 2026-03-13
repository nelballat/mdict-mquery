# -*- coding: utf-8 -*-
# mdict_mquery — Modern MDX/MDD dictionary file lookup library
# Forked from mdict-query by mmjang (https://github.com/mmjang/mdict-query)
# Based on readmdict by Xiaoqiang Wang

from .readmdict import MDX, MDD
from struct import pack, unpack
from io import BytesIO
from collections import OrderedDict
import re
import os
import sqlite3
import json
import zlib
import threading

try:
    import lzo
except ImportError:
    lzo = None

# Maximum number of decompressed record blocks to keep in memory
_BLOCK_CACHE_MAX = 256
# Maximum number of lookup results to keep in memory
_RESULT_CACHE_MAX = 8192


class IndexBuilder:
    def __init__(self, fname, encoding="", passcode=None, force_rebuild=False, enable_history=False, sql_index=True, check=False):
        self._mdx_file = fname
        self._mdd_file = ""
        self._encoding = ''
        self._stylesheet = {}
        self._title = ''
        self._version = ''
        self._description = ''
        self._sql_index = sql_index
        self._check = check
        _filename, _file_extension = os.path.splitext(fname)
        assert(_file_extension == '.mdx')
        assert(os.path.isfile(fname))
        self._mdx_db = _filename + ".mdx.db"
        if force_rebuild:
            self._make_mdx_index(self._mdx_db)
            if os.path.isfile(_filename + '.mdd'):
                self._mdd_file = _filename + ".mdd"
                self._mdd_db = _filename + ".mdd.db"
                self._make_mdd_index(self._mdd_db)

        if os.path.isfile(self._mdx_db):
            conn = sqlite3.connect(self._mdx_db)
            cursor = conn.execute("SELECT * FROM META WHERE key = \"version\"")
            for cc in cursor:
                self._version = cc[1]
            if not self._version:
                conn.close()
                self._make_mdx_index(self._mdx_db)
                if os.path.isfile(_filename + '.mdd'):
                    self._mdd_file = _filename + ".mdd"
                    self._mdd_db = _filename + ".mdd.db"
                    self._make_mdd_index(self._mdd_db)
                return None
            cursor = conn.execute("SELECT * FROM META WHERE key = \"encoding\"")
            for cc in cursor:
                self._encoding = cc[1]
            cursor = conn.execute("SELECT * FROM META WHERE key = \"stylesheet\"")
            for cc in cursor:
                self._stylesheet = json.loads(cc[1])
            cursor = conn.execute("SELECT * FROM META WHERE key = \"title\"")
            for cc in cursor:
                self._title = cc[1]
            cursor = conn.execute("SELECT * FROM META WHERE key = \"description\"")
            for cc in cursor:
                self._description = cc[1]
            conn.close()
        else:
            self._make_mdx_index(self._mdx_db)

        if os.path.isfile(_filename + ".mdd"):
            self._mdd_file = _filename + ".mdd"
            self._mdd_db = _filename + ".mdd.db"
            if not os.path.isfile(self._mdd_db):
                self._make_mdd_index(self._mdd_db)

        # Persistent SQLite connection for on-demand record lookups (replaces _mem_index)
        # Optimized for read-only: immutable (no locking), mmap (zero-copy), no journal
        if os.path.isfile(self._mdx_db):
            uri = 'file:' + self._mdx_db.replace('\\', '/') + '?immutable=1'
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.execute("PRAGMA mmap_size=536870912")
            self._conn.execute("PRAGMA journal_mode=OFF")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.execute("PRAGMA cache_size=-64000")
        else:
            self._conn = None

        # Load key structures: cache path (pickle) or slow path (SQLite query)
        self._cache_file = _filename + ".mdx.cache"
        _cache_hit = (
            not force_rebuild
            and os.path.isfile(self._cache_file)
            and os.path.isfile(self._mdx_db)
            and os.path.getmtime(self._cache_file) > os.path.getmtime(self._mdx_db)
        )

        if _cache_hit:
            import pickle
            with open(self._cache_file, 'rb') as f:
                _cached = pickle.load(f)
            self.sorted_keys = _cached['sk']
            self.kanji_index = _cached['kx']
            self.key_set = frozenset(self.sorted_keys)
        else:
            # Build sorted_keys from SQLite (B-tree gives us sorted order for free)
            if self._conn:
                self.sorted_keys = [row[0] for row in self._conn.execute("SELECT DISTINCT key_text FROM MDX_INDEX ORDER BY key_text")]
            else:
                self.sorted_keys = []
            self.key_set = frozenset(self.sorted_keys)

            # Kanji reverse index: CJK character → list of keys containing it in 【...】
            self.kanji_index = {}
            for k in self.sorted_keys:
                if '【' in k:
                    bp = k.find('【')
                    ep = k.find('】', bp)
                    if ep > bp:
                        for c in k[bp+1:ep]:
                            if '\u4E00' <= c <= '\u9FFF':
                                if c in self.kanji_index:
                                    self.kanji_index[c].append(k)
                                else:
                                    self.kanji_index[c] = [k]

            # Save lightweight cache (keys + kanji index only, no record data)
            if self.sorted_keys:
                import pickle
                with open(self._cache_file, 'wb') as f:
                    pickle.dump({'sk': self.sorted_keys, 'kx': self.kanji_index},
                                f, protocol=pickle.HIGHEST_PROTOCOL)

        self._keys_cache = tuple(self.sorted_keys)

        # Persistent file handle for MDX reads (avoids open/close per lookup)
        self._mdx_fh = open(self._mdx_file, 'rb') if os.path.isfile(self._mdx_file) else None
        self._fh_lock = threading.Lock()

        # Decompressed record block cache keyed by file_pos (LRU via OrderedDict)
        self._block_cache = OrderedDict()

        # Lookup result cache keyed by keyword (LRU via OrderedDict)
        self._result_cache = OrderedDict()

    def __del__(self):
        if hasattr(self, '_mdx_fh') and self._mdx_fh:
            self._mdx_fh.close()
        if hasattr(self, '_conn') and self._conn:
            self._conn.close()

    def _replace_stylesheet(self, txt):
        txt_list = re.split(r'`\d+`', txt)
        txt_tag = re.findall(r'`\d+`', txt)
        txt_styled = txt_list[0]
        for j, p in enumerate(txt_list[1:]):
            style = self._stylesheet[txt_tag[j][1:-1]]
            if p and p[-1] == '\n':
                txt_styled = txt_styled + style[0] + p.rstrip() + style[1] + '\r\n'
            else:
                txt_styled = txt_styled + style[0] + p + style[1]
        return txt_styled

    def make_sqlite(self):
        sqlite_file = self._mdx_file + '.sqlite.db'
        if os.path.exists(sqlite_file):
            os.remove(sqlite_file)
        mdx = MDX(self._mdx_file)
        conn = sqlite3.connect(sqlite_file)
        cursor = conn.cursor()
        cursor.execute(
            ''' CREATE TABLE MDX_DICT
                (key text not null,
                value text
                )''')
        aeiou = 'āáǎàĀÁǍÀēéěèêềếĒÉĚÈÊỀẾīíǐìÍǏÌōóǒòŌÓǑÒūúǔùŪÚǓÙǖǘǚǜǕǗǙǛḾǹňŃŇ'
        pattern = r"`\d+`|[（\(]?['a-z%s]*[%s]['a-z%s]*[\)）]?" % (aeiou, aeiou, aeiou)
        tuple_list = [(key.decode(), re.sub(pattern, '', value.decode()))
            for key, value in mdx.items()]
        cursor.executemany('INSERT INTO MDX_DICT VALUES (?,?)', tuple_list)
        returned_index = mdx.get_index(check_block=self._check)
        meta = returned_index['meta']
        cursor.execute('''CREATE TABLE META (key text, value text)''')
        cursor.executemany(
            'INSERT INTO META VALUES (?,?)',
            [('encoding', meta['encoding']),
             ('stylesheet', meta['stylesheet']),
             ('title', meta['title']),
             ('description', meta['description']),
             ('version', version)])
        if self._sql_index:
            cursor.execute('''CREATE INDEX key_index ON MDX_DICT (key)''')
        conn.commit()
        conn.close()

    def _make_mdx_index(self, db_name):
        if os.path.exists(db_name):
            os.remove(db_name)
        mdx = MDX(self._mdx_file)
        self._mdx_db = db_name
        returned_index = mdx.get_index(check_block=self._check)
        index_list = returned_index['index_dict_list']
        conn = sqlite3.connect(db_name)
        c = conn.cursor()
        c.execute(
            ''' CREATE TABLE MDX_INDEX
               (key_text text not null,
                file_pos integer,
                compressed_size integer,
                decompressed_size integer,
                record_block_type integer,
                record_start integer,
                record_end integer,
                offset integer
                )''')
        tuple_list = [
            (item['key_text'], item['file_pos'], item['compressed_size'],
             item['decompressed_size'], item['record_block_type'],
             item['record_start'], item['record_end'], item['offset'])
            for item in index_list]
        c.executemany('INSERT INTO MDX_INDEX VALUES (?,?,?,?,?,?,?,?)', tuple_list)
        meta = returned_index['meta']
        c.execute('''CREATE TABLE META (key text, value text)''')
        c.executemany(
            'INSERT INTO META VALUES (?,?)',
            [('encoding', meta['encoding']),
             ('stylesheet', meta['stylesheet']),
             ('title', meta['title']),
             ('description', meta['description']),
             ('version', version)])
        if self._sql_index:
            c.execute('''CREATE INDEX key_index ON MDX_INDEX (key_text)''')
        conn.commit()
        conn.close()
        self._encoding = meta['encoding']
        self._stylesheet = json.loads(meta['stylesheet'])
        self._title = meta['title']
        self._description = meta['description']

    def _make_mdd_index(self, db_name):
        if os.path.exists(db_name):
            os.remove(db_name)
        mdd = MDD(self._mdd_file)
        self._mdd_db = db_name
        index_list = mdd.get_index(check_block=self._check)
        conn = sqlite3.connect(db_name)
        c = conn.cursor()
        c.execute(
            ''' CREATE TABLE MDX_INDEX
               (key_text text not null unique,
                file_pos integer,
                compressed_size integer,
                decompressed_size integer,
                record_block_type integer,
                record_start integer,
                record_end integer,
                offset integer
                )''')
        tuple_list = [
            (item['key_text'], item['file_pos'], item['compressed_size'],
             item['decompressed_size'], item['record_block_type'],
             item['record_start'], item['record_end'], item['offset'])
            for item in index_list]
        c.executemany('INSERT INTO MDX_INDEX VALUES (?,?,?,?,?,?,?,?)', tuple_list)
        if self._sql_index:
            c.execute('''CREATE UNIQUE INDEX key_index ON MDX_INDEX (key_text)''')
        conn.commit()
        conn.close()

    def _get_block(self, fmdx, file_pos, compressed_size, decompressed_size, record_block_type):
        """Read and decompress a record block, with caching."""
        if file_pos in self._block_cache:
            return self._block_cache[file_pos]
        with self._fh_lock:
            fmdx.seek(file_pos)
            record_block_compressed = fmdx.read(compressed_size)
        if record_block_type == 0:
            _record_block = record_block_compressed[8:]
        elif record_block_type == 1:
            if lzo is None:
                return b''
            _record_block = lzo.decompress(record_block_compressed[8:], initSize=decompressed_size, blockSize=1308672)
        elif record_block_type == 2:
            _record_block = zlib.decompress(record_block_compressed[8:])
        else:
            _record_block = record_block_compressed[8:]
        # Evict oldest block if cache is full
        if len(self._block_cache) >= _BLOCK_CACHE_MAX:
            self._block_cache.popitem(last=False)
        self._block_cache[file_pos] = _record_block
        return _record_block

    @staticmethod
    def get_data_by_index(fmdx, index):
        """Read and decompress a single record from an open file handle (static, no caching)."""
        fmdx.seek(index['file_pos'])
        record_block_compressed = fmdx.read(index['compressed_size'])
        record_block_type = index['record_block_type']
        decompressed_size = index['decompressed_size']
        if record_block_type == 0:
            _record_block = record_block_compressed[8:]
        elif record_block_type == 1:
            if lzo is None:
                return b''
            _record_block = lzo.decompress(record_block_compressed[8:], initSize=decompressed_size, blockSize=1308672)
        elif record_block_type == 2:
            _record_block = zlib.decompress(record_block_compressed[8:])
        data = _record_block[index['record_start'] - index['offset']:index['record_end'] - index['offset']]
        return data

    def get_mdx_by_index(self, fmdx, index):
        """Decode a single MDX record from an open file handle (static path, no caching)."""
        data = self.get_data_by_index(fmdx, index)
        record = data.decode(self._encoding, errors='ignore').strip('\x00').encode('utf-8')
        if self._stylesheet:
            record = self._replace_stylesheet(record)
        record = record.decode('utf-8')
        return record

    def _get_record_fast(self, rec_tuple):
        """Decode a record from a preloaded index tuple using cached blocks and file handle."""
        file_pos, compressed_size, decompressed_size, record_block_type, record_start, record_end, offset = rec_tuple
        block = self._get_block(self._mdx_fh, file_pos, compressed_size, decompressed_size, record_block_type)
        data = block[record_start - offset:record_end - offset]
        record = data.decode(self._encoding, errors='ignore').strip('\x00').encode('utf-8')
        if self._stylesheet:
            record = self._replace_stylesheet(record)
        return record.decode('utf-8')

    @staticmethod
    def lookup_indexes(db, keyword, ignorecase=None):
        """Query SQLite index for a keyword (used by mdd_lookup and legacy callers)."""
        indexes = []
        if ignorecase:
            sql = 'SELECT * FROM MDX_INDEX WHERE lower(key_text) = lower("{}")'.format(keyword)
        else:
            sql = 'SELECT * FROM MDX_INDEX WHERE key_text = "{}"'.format(keyword)
        conn = sqlite3.connect(db)
        cursor = conn.execute(sql)
        for result in cursor:
            index = {}
            index['file_pos'] = result[1]
            index['compressed_size'] = result[2]
            index['decompressed_size'] = result[3]
            index['record_block_type'] = result[4]
            index['record_start'] = result[5]
            index['record_end'] = result[6]
            index['offset'] = result[7]
            indexes.append(index)
        conn.close()
        return indexes

    def mdx_lookup(self, keyword, ignorecase=None):
        """Look up a keyword via persistent SQLite connection + block/result cache."""
        if keyword in self._result_cache:
            return self._result_cache[keyword]
        if not self._conn:
            return []
        if ignorecase:
            recs = self._conn.execute(
                "SELECT file_pos, compressed_size, decompressed_size, record_block_type, record_start, record_end, offset FROM MDX_INDEX WHERE key_text = ? COLLATE NOCASE", (keyword,)
            ).fetchall()
        else:
            recs = self._conn.execute(
                "SELECT file_pos, compressed_size, decompressed_size, record_block_type, record_start, record_end, offset FROM MDX_INDEX WHERE key_text = ?", (keyword,)
            ).fetchall()
        if not recs:
            self._result_cache[keyword] = []
            return []
        results = [self._get_record_fast(rec) for rec in recs]
        if len(self._result_cache) >= _RESULT_CACHE_MAX:
            self._result_cache.popitem(last=False)
        self._result_cache[keyword] = results
        return results

    def mdd_lookup(self, keyword, ignorecase=None):
        """Look up a keyword in the MDD resource file (uses SQLite, no in-memory index)."""
        lookup_result_list = []
        indexes = self.lookup_indexes(self._mdd_db, keyword, ignorecase)
        with open(self._mdd_file, 'rb') as mdd_file:
            for index in indexes:
                lookup_result_list.append(self.get_mdd_by_index(mdd_file, index))
        return lookup_result_list

    def get_mdd_by_index(self, fmdx, index):
        return self.get_data_by_index(fmdx, index)

    @staticmethod
    def get_keys(db, query=''):
        """Get all keys from the SQLite index, optionally filtered by a query pattern."""
        if not db:
            return []
        if query:
            if '*' in query:
                query = query.replace('*', '%')
            else:
                query = query + '%'
            sql = 'SELECT key_text FROM MDX_INDEX WHERE key_text LIKE \"' + query + '\"'
        else:
            sql = 'SELECT key_text FROM MDX_INDEX'
        conn = sqlite3.connect(db)
        cursor = conn.execute(sql)
        keys = [item[0] for item in cursor]
        conn.close()
        return keys

    def get_mdd_keys(self, query=''):
        return self.get_keys(self._mdd_db, query)

    def get_mdx_keys(self, query=''):
        """Return all MDX keys. Uses pre-cached tuple when no query filter is given."""
        if not query and self._keys_cache:
            return list(self._keys_cache)
        return self.get_keys(self._mdx_db, query)
