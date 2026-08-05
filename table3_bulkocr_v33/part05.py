               _bj.dumps(r.get('raw_payload'),ensure_ascii=False) if r.get('raw_payload') is not None else None,
               'longest-work-directory-prefix' if wid is not None else 'unmatched',_now()))
        merged=[]
        for w in works:
            wid=w.get(qid);rr=group.get(wid,[]);st=[str(x.get('status') or 'unknown') for x in rr]
            conf=[float(x['confidence']) for x in rr if isinstance(x.get('confidence'),(int,float))]
            tags=_unique(*(x.get('tags') for x in rr));chars=_unique(*(x.get('characters') for x in rr));combined=_unique(_jlist(w.get('flat_tags_json')),tags)
            status='complete' if rr and all(x=='complete' for x in st) else 'partial' if rr and any(x=='complete' for x in st) else 'failed' if rr else 'not-run'
            vals=(sid,len(rr),status,sum(conf)/len(conf) if conf else None,
              _bj.dumps([x.get('source_image_path') for x in rr],ensure_ascii=False),_bj.dumps([x.get('ocr_json_path') for x in rr],ensure_ascii=False),
              _bj.dumps(tags,ensure_ascii=False),_bj.dumps(chars,ensure_ascii=False),_bj.dumps([x.get('fields') or {} for x in rr],ensure_ascii=False),
              _bj.dumps([y for x in rr for y in (x.get('warnings') or [])],ensure_ascii=False),
              _bj.dumps([y for x in rr for y in (x.get('errors') or [])],ensure_ascii=False),
              _bj.dumps([x.get('raw_payload') for x in rr if x.get('raw_payload') is not None],ensure_ascii=False),_bj.dumps(combined,ensure_ascii=False),wid)
            c.execute(f'''update ingest_work_queue set ocr_session_id=?,ocr_record_count=?,ocr_status=?,ocr_confidence=?,
              ocr_source_images_json=?,ocr_json_paths_json=?,ocr_tags_json=?,ocr_characters_json=?,ocr_fields_json=?,
              ocr_warnings_json=?,ocr_errors_json=?,ocr_metadata_json=?,combined_flat_tags_json=? where {qid}=?''',vals)
            merged.append({'queue_id':wid,'work_directory':w.get('work_directory'),'title':w.get('title') or w.get('display_title'),
              'slug_suggestion':w.get('slug_suggestion'),'source_url':w.get('source_url'),'ocr_record_count':len(rr),'ocr_status':status,
              'ocr_confidence':sum(conf)/len(conf) if conf else None,'ocr_tags':tags,'combined_flat_tags':combined,'ocr_characters':chars,
              'ocr_source_images':[x.get('source_image_path') for x in rr],'ocr_json_paths':[x.get('ocr_json_path') for x in rr]})
        if 'export_metadata' in tables and {'key','value'}<=set(_cols(c,'export_metadata')):
            for k,v in [('bulkocr_workflow_version',_BO_VER),('bulkocr_session_id',sid),('bulkocr_session_directory',str(session)),
                        ('bulkocr_record_count',str(len(rows))),('bulkocr_unmatched_record_count',str(unmatched))]:
                c.execute('insert or replace into export_metadata(key,value) values(?,?)',(k,v))
        c.commit();_BP(sql).write_text('\n'.join(c.iterdump())+'\n',encoding='utf-8')
        return merged,{'ocr_records':len(rows),'matched_ocr_records':len(rows)-unmatched,'unmatched_ocr_records':unmatched,
                       'works':len(works),'works_with_ocr':sum(bool(x) for x in group.values())}
    finally:c.close()
def _checkpoint(d,s,stage,name,extra=None):
    s.update({'current_stage':stage,'current_stage_name':name,'updated_at':_now()});s.setdefault('completed_stages',[])
    if name not in s['completed_stages']:s['completed_stages'].append(name)
    if extra:s.update(extra)
    _write(d/'session.json',s);_write(d/'current-stage.json',{'schema_version':1,'session_id':s['session_id'],'stage':stage,
      'stage_name':name,'completed_stages':s['completed_stages'],'updated_at':s['updated_at']})


class _BOStudio:
    stages=[('Prepare','Choose BulkOCR behavior.'),('Discover','Recursively inventory source images.'),('BulkOCR','Run or reuse sidecars.'),
            ('Collect','Freeze JSON and JSONL evidence.'),('Merge','Combine OCR with Table 3.'),('Export','Finish the ingest handoff.')]
