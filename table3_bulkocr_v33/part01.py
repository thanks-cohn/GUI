# Reversible Table 3 -> BulkOCR -> ingest export studio (injected by pairing_gui.py).
import json as _bj
import os as _bo
import queue as _bq
import shutil as _bs
import sqlite3 as _bsq
import subprocess as _bsp
import sys as _bsy
import threading as _bt
import traceback as _btr
from datetime import datetime as _bd, timezone as _btz
from pathlib import Path as _BP

_BO_EXTS={'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff'}
_BO_SIDE='-EXTRACTED-DATA.json'
_BO_VER='33.0'


def _now(): return _bd.now(_btz.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def _safe(s):
    v=''.join(c if c.isalnum() or c in '-_.' else '_' for c in str(s)); return v.strip('_.') or 'run'
def _load(p,d=None):
    try:return _bj.loads(_BP(p).read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError):return d
def _write(p,v):
    p=_BP(p);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_name(f'.{p.name}.{_bo.getpid()}.tmp')
    t.write_text(_bj.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');_bo.replace(t,p)
def _write_lines(p,rows):
    p=_BP(p);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_name(f'.{p.name}.{_bo.getpid()}.tmp')
    with t.open('w',encoding='utf-8') as f:
        for r in rows:f.write(_bj.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n')
        f.flush();_bo.fsync(f.fileno())
    _bo.replace(t,p)
def _source_image(p):
    return p.is_file() and p.suffix.casefold() in _BO_EXTS and not p.stem.casefold().endswith(('-thumbnail','-extracted-thumbnail'))
def _discover(root):
    out=[]
    for p in root.rglob('*'):
        try:rel=p.relative_to(root)
        except ValueError:continue
