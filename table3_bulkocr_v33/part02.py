        if any(x.startswith('.') or x=='export-for-ingest' for x in rel.parts):continue
        if _source_image(p):
            s=p.stat();side=p.with_name(p.stem+_BO_SIDE)
            out.append({'source_image_path':str(p.resolve()),'relative_source_path':rel.as_posix(),
                        'source_size_bytes':s.st_size,'source_mtime_ns':s.st_mtime_ns,
                        'ocr_json_path':str(side.resolve()),'ocr_json_existed_at_discovery':side.is_file()})
    return sorted(out,key=lambda r:r['relative_source_path'].casefold())
def _runner(gui):
    home=_BP.home();env=_bo.environ.get('EXTRACTED_DATA_HOME') or _bo.environ.get('BULKOCR_HOME')
    c=[]
    if env:c.append(_BP(env)/'batch_extract.py')
    c += [home/'dev/EXTRACTED-DATA/batch_extract.py',home/'EXTRACTED-DATA/batch_extract.py',
          home/'dev/BulkOCR/batch_extract.py',home/'BulkOCR/batch_extract.py']
    if gui:
        h=_BP(gui).resolve().parent;c += [h/'EXTRACTED-DATA/batch_extract.py',h.parent/'EXTRACTED-DATA/batch_extract.py']
    return next((p.resolve() for p in c if p.is_file()),None)
def _python(r):
    return str(next((p for p in (r.parent/'.venv/bin/python',r.parent/'.venv-paddle/bin/python') if p.is_file()),_BP(_bsy.executable)))
def _vals(v):
    out=[]
    if isinstance(v,str) and v.strip():out=[v.strip()]
    elif isinstance(v,(int,float)):out=[str(v)]
    elif isinstance(v,list):
        for x in v:
            if isinstance(x,dict):
                x=next((x[k] for k in ('value','name','text','normalized','raw_text') if k in x),x)
            out += _vals(x)
    elif isinstance(v,dict):
        for k in ('value','values','items','tags','characters','normalized','raw_text'):
            if k in v:out += _vals(v[k]);break
    seen=set();return [x for x in out if not (x.casefold() in seen or seen.add(x.casefold()))]
def _find(v,names):
    out=[]
    if isinstance(v,dict):
        for k,x in v.items():
            if str(k).casefold().replace('-','_').replace(' ','_') in names:out.append(x)
            out += _find(x,names)
    elif isinstance(v,list):
        for x in v:out += _find(x,names)
    return out
