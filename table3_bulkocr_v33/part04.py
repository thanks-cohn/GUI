    if d.split()[0] not in _cols(c,t):c.execute(f'alter table {t} add column {d}')
def _match(p,works):
    best=None
    for w in works:
        try:_BP(p).resolve().relative_to(_BP(w.get('work_directory','')).resolve())
        except (ValueError,OSError):continue
        score=len(_BP(w['work_directory']).parts)
        if not best or score>best[0]:best=(score,w)
    return best[1] if best else None
def _augment(db,sql,rows,session,sid):
    c=_bsq.connect(db);c.row_factory=_bsq.Row
    try:
        tables={r[0] for r in c.execute("select name from sqlite_master where type='table'")}
        if 'ingest_work_queue' not in tables:raise ValueError('Export lacks ingest_work_queue')
        for d in ("ocr_session_id text","ocr_record_count integer not null default 0","ocr_status text","ocr_confidence real",
          "ocr_source_images_json text not null default '[]'","ocr_json_paths_json text not null default '[]'",
          "ocr_tags_json text not null default '[]'","ocr_characters_json text not null default '[]'",
          "ocr_fields_json text not null default '[]'","ocr_warnings_json text not null default '[]'",
          "ocr_errors_json text not null default '[]'","ocr_metadata_json text not null default '[]'",
          "combined_flat_tags_json text not null default '[]'"):_add(c,'ingest_work_queue',d)
        c.executescript('''create table if not exists ingest_ocr_records(
          id integer primary key,work_queue_id integer,session_id text not null,source_image_path text not null,
          relative_source_path text,ocr_json_path text,relative_ocr_json_path text,status text,confidence real,title text,creator text,
          tags_json text not null,characters_json text not null,fields_json text not null,work_json text not null,
          thumbnail_json text not null,warnings_json text not null,errors_json text not null,raw_payload_json text,matched_by text,created_at text not null);
          create index if not exists idx_ingest_ocr_work on ingest_ocr_records(work_queue_id);''')
        qc=_cols(c,'ingest_work_queue');qid=next((x for x in ('id','queue_id','work_queue_id') if x in qc),qc[0])
        works=[dict(x) for x in c.execute('select * from ingest_work_queue')];group={};unmatched=0
        for r in rows:
            w=_match(r['source_image_path'],works);wid=w.get(qid) if w else None
            if wid is None:unmatched+=1
            else:group.setdefault(wid,[]).append(r)
            c.execute('''insert into ingest_ocr_records(work_queue_id,session_id,source_image_path,relative_source_path,ocr_json_path,
              relative_ocr_json_path,status,confidence,title,creator,tags_json,characters_json,fields_json,work_json,thumbnail_json,
              warnings_json,errors_json,raw_payload_json,matched_by,created_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (wid,sid,r.get('source_image_path'),r.get('relative_source_path'),r.get('ocr_json_path'),r.get('relative_ocr_json_path'),
               r.get('status'),r.get('confidence'),r.get('title'),r.get('creator'),_bj.dumps(r.get('tags') or [],ensure_ascii=False),
               _bj.dumps(r.get('characters') or [],ensure_ascii=False),_bj.dumps(r.get('fields') or {},ensure_ascii=False),
               _bj.dumps(r.get('work') or {},ensure_ascii=False),_bj.dumps(r.get('thumbnail') or {},ensure_ascii=False),
               _bj.dumps(r.get('warnings') or [],ensure_ascii=False),_bj.dumps(r.get('errors') or [],ensure_ascii=False),
