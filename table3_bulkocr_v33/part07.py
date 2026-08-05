        n,d=self.stages[self.stage];self.title.config(text=f'{self.stage+1}. {n}');self.desc.config(text=d)
        for i,x in enumerate(self.sl):x.config(bg='#214732' if i<self.stage else '#237a3b' if i==self.stage else '#1a2528',fg='#aef0c4' if i<self.stage else 'white' if i==self.stage else '#79928a')
        self.rc.config(state='normal' if self.stage==0 and not self.busy else 'disabled');self.oc.config(state='normal' if self.stage==0 and self.run.get() and not self.busy else 'disabled');self.backb.config(state='normal' if self.stage and not self.busy else 'disabled');self.next.config(state='normal' if not self.busy else 'disabled')
        sums=[f'BulkOCR: {self.runner or "not found"}',f'Discovered images: {len(self.images)}',f'Mode: {"recursive OCR" if self.run.get() else "reuse sidecars"}',f'OCR records: {len(self.ocr)}',f'Merged works: {len(self.merged)}',f'Works ready: {self.s.get("work_count",0)}'];self.sum.set(sums[self.stage]);self.next.config(text=['Begin →','Discover Images','Run BulkOCR' if self.run.get() else 'Reuse Sidecars','Collect Results','Merge & Export','Finish'][self.stage])
    def bg(self,fn):
        self.busy=True;self.render();self.w.config(cursor='watch')
        def go():
            try:fn();self.q.put(('done',None))
            except BaseException as e:self.q.put(('err',(e,_btr.format_exc())))
        _bt.Thread(target=go,daemon=True).start()
    def cont(self):
        if self.stage==0:
            if self.run.get() and not self.runner:messagebox.showwarning('BulkOCR not found','Set EXTRACTED_DATA_HOME or clone EXTRACTED-DATA under ~/dev, or uncheck BulkOCR.',parent=self.w);return
            self.s.update(run_bulkocr=self.run.get(),overwrite_ocr=self.over.get());_checkpoint(self.dir,self.s,0,'prepare');self.stage=1;self.render()
        elif self.stage==1:self.bg(self.discover)
        elif self.stage==2:self.bg(self.bulk)
        elif self.stage==3:self.bg(self.collect)
        elif self.stage==4:self.bg(self.merge)
        elif self.stage==5:self.result=(_BP(self.s['sqlite_path']),_BP(self.s['sql_path']),int(self.s['work_count']),self.dir);self.w.destroy()
    def discover(self):
        self.images=_discover(self.root);_write(self.dir/'01-discovered-images.json',{'schema_version':1,'generated_at':_now(),'run_directory':str(self.root),'recursive':True,'image_count':len(self.images),'images':self.images});_write_lines(self.dir/'01-discovered-images.jsonl',self.images);_checkpoint(self.dir,self.s,1,'discover',{'image_count':len(self.images)});self.q.put(('log',f'Discovered {len(self.images)} images.'));self.stage=2
    def bulk(self):
        if not self.images:self.discover()
        if self.run.get():
            if self.over.get():
                backups=[]
                for r in self.images:
                    p=_BP(r['ocr_json_path'])
                    if p.is_file():
                        rel=p.relative_to(self.root);dst=self.dir/'pre-ocr-backup'/rel;dst.parent.mkdir(parents=True,exist_ok=True);_bs.copy2(p,dst);backups.append({'original_path':str(p),'backup_path':str(dst),'relative_path':rel.as_posix()})
                _write(self.dir/'00-pre-ocr-backup.json',{'schema_version':1,'created_at':_now(),'count':len(backups),'backups':backups});_write_lines(self.dir/'00-pre-ocr-backup.jsonl',backups);self.q.put(('log',f'Backed up {len(backups)} sidecars.'))
            cmd=[_python(self.runner),str(self.runner),str(self.root),'--recursive','--ocr-engine','auto','--no-extract-thumbnail','--non-interactive']+(['--overwrite'] if self.over.get() else [])
            info={'schema_version':1,'started_at':_now(),'cwd':str(self.runner.parent),'command':cmd};_write(self.dir/'01-bulkocr-command.json',info);p=_bsp.Popen(cmd,cwd=self.runner.parent,stdout=_bsp.PIPE,stderr=_bsp.STDOUT,text=True,bufsize=1);lines=[]
            for line in p.stdout:lines.append(line);self.q.put(('log',line.rstrip()))
            rc=p.wait();(self.dir/'01-bulkocr-output.log').write_text(''.join(lines),encoding='utf-8');info.update(finished_at=_now(),return_code=rc);_write(self.dir/'01-bulkocr-command.json',info)
            if rc not in (0,2):raise RuntimeError(f'BulkOCR exited with {rc}')
        else:self.q.put(('log','BulkOCR skipped; reusing sidecars.'))
        _checkpoint(self.dir,self.s,2,'bulkocr');self.stage=3
    def collect(self):
        self.ocr=[_record(r,self.root) for r in self.images];ok=sum(x.get('status')=='complete' for x in self.ocr);_write(self.dir/'02-bulkocr-results.json',{'schema_version':1,'generated_at':_now(),'record_count':len(self.ocr),'complete_count':ok,'records':self.ocr});_write_lines(self.dir/'02-bulkocr-results.jsonl',self.ocr);_checkpoint(self.dir,self.s,3,'collect',{'ocr_record_count':len(self.ocr),'ocr_complete_count':ok});self.q.put(('log',f'Frozen {len(self.ocr)} OCR records.'));self.stage=4
