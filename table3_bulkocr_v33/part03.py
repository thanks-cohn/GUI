def _get(v,*path):
    for k in path:
        if not isinstance(v,dict):return None
        v=v.get(k)
    return v
def _first(p,paths):
    for path in paths:
        x=_vals(_get(p,*path))
        if x:return x[0]
def _record(r,root):
    side=_BP(r['ocr_json_path']);p=_load(side)
    if not isinstance(p,dict):return {**r,'sidecar_exists':side.is_file(),'status':'missing-or-invalid-json','confidence':None,
        'title':None,'creator':None,'tags':[],'characters':[],'fields':{},'work':{},'thumbnail':{},
        'warnings':[],'errors':['BulkOCR sidecar missing or invalid'],'raw_payload':None}
    tags=[];chars=[]
    for x in _find(p,{'tags','tag_items','tag_chips'}):tags += _vals(x)
    for x in _find(p,{'characters','character_items','character_chips'}):chars += _vals(x)
    return {**r,'sidecar_exists':True,'status':str(_get(p,'extraction','status') or p.get('status') or 'unknown'),
      'confidence':_get(p,'extraction','overall_confidence') if isinstance(_get(p,'extraction','overall_confidence'),(int,float)) else None,
      'title':_first(p,[('work','title'),('fields','title'),('title',)]),
      'creator':_first(p,[('work','creator'),('work','author'),('fields','creator'),('fields','author'),('creator',),('author',)]),
      'tags':_vals(tags),'characters':_vals(chars),'fields':p.get('fields') if isinstance(p.get('fields'),dict) else {},
      'work':p.get('work') if isinstance(p.get('work'),dict) else {},'thumbnail':p.get('thumbnail') if isinstance(p.get('thumbnail'),dict) else {},
      'warnings':p.get('warnings') if isinstance(p.get('warnings'),list) else [],'errors':p.get('errors') if isinstance(p.get('errors'),list) else [],
      'raw_payload':p,'relative_ocr_json_path':side.relative_to(root).as_posix() if root in side.parents else side.name,'collected_at':_now()}
def _unique(*groups):
    out=[];seen=set()
    for g in groups:
        for x in g if isinstance(g,list) else []:
            x=' '.join(str(x or '').split());k=x.casefold()
            if x and k not in seen:seen.add(k);out.append(x)
    return out
def _jlist(v):
    if isinstance(v,list):return v
    if isinstance(v,str):
        try:v=_bj.loads(v)
        except ValueError:return []
    return v if isinstance(v,list) else []
def _cols(c,t):return [r[1] for r in c.execute(f'pragma table_info({t})')]
def _add(c,t,d):
