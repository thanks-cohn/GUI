    def merge(self):
        db,sql,count=self.exporter(self.db,self.out);self.merged,counts=_augment(_BP(db),_BP(sql),self.ocr,self.dir,self.s['session_id']);_write(self.dir/'03-merged-works.json',{'schema_version':1,'generated_at':_now(),'counts':counts,'works':self.merged});_write_lines(self.dir/'03-merged-works.jsonl',self.merged);plan={'schema_version':1,'generated_at':_now(),'session_id':self.s['session_id'],'sqlite_path':str(db),'sql_path':str(sql),'work_count':count,'counts':counts};_write(self.dir/'04-export-plan.json',plan);_write_lines(self.dir/'04-export-plan.jsonl',[plan]);_bs.copy2(db,self.out/'latest.sqlite3');_bs.copy2(sql,self.out/'latest.sql');(self.out/'latest-session.txt').write_text(str(self.dir)+'\n',encoding='utf-8');(self.out/'latest.txt').write_text(f'sqlite={db}\nsql={sql}\nsession={self.dir}\n',encoding='utf-8');self.s['status']='complete';_checkpoint(self.dir,self.s,5,'export',{'sqlite_path':str(db),'sql_path':str(sql),'work_count':count,'merge_counts':counts,'status':'complete'});self.stage=5;self.q.put(('log',f'Enriched export complete: {db}'))
    def back(self):
        if self.stage and not self.busy:self.stage-=1;self.s.update(current_stage=self.stage,current_stage_name=self.stages[self.stage][0].casefold());_write(self.dir/'session.json',self.s);self.say(f'Back to {self.stages[self.stage][0]} checkpoint.');self.render()
    def drain(self):
        try:
            while True:
                k,v=self.q.get_nowait()
                if k=='log':self.say(v)
                elif k=='done':self.busy=False;self.w.config(cursor='');self.render();
                elif k=='err':self.busy=False;self.w.config(cursor='');self.say(v[1]);messagebox.showerror('Export Studio error',str(v[0]),parent=self.w);self.render()
        except _bq.Empty:pass
        if self.w.winfo_exists():self.w.after(100,self.drain)
    def open(self):
        try:_bsp.Popen(['xdg-open',str(self.dir)] if _bsy.platform.startswith('linux') else ['open',str(self.dir)])
        except OSError as e:messagebox.showerror('Could not open folder',str(e),parent=self.w)
    def cancel(self):
        if self.busy:return
        if messagebox.askyesno('Close Export Studio?','Close now? Completed stages are saved and resumable.',parent=self.w):
            if self.s.get('status')!='complete':self.s.update(status='paused',updated_at=_now());_write(self.dir/'session.json',self.s)
            self.w.destroy()
    def show(self):self.parent.wait_window(self.w);return self.result


def run_table3_bulkocr_export_studio(parent,database_path,export_root,export_callable,status_callback=None,gui_file=None):
    return _BOStudio(parent,database_path,export_root,export_callable,status_callback,gui_file).show()
