    def __init__(self,parent,db,out,exporter,status=None,gui=None):
        self.parent=parent;self.db=_BP(db).resolve();self.root=self.db.parent;self.out=_BP(out).resolve();self.exporter=exporter;self.status=status
        self.runner=_runner(gui);self.stage=0;self.busy=False;self.images=[];self.ocr=[];self.merged=[];self.result=None;self.q=_bq.Queue()
        self.w=tk.Toplevel(parent);self.w.title('Table 3 Export Studio · BulkOCR');self.w.transient(parent);self.w.geometry('900x620');self.w.minsize(760,520)
        self.w.configure(bg='#101719');self.w.protocol('WM_DELETE_WINDOW',self.cancel);self._ui();self._session();self.render();self.w.grab_set();self.w.after(100,self.drain)
    def _ui(self):
        h=tk.Frame(self.w,bg='#101719',padx=24,pady=18);h.pack(fill='x')
        tk.Label(h,text='EXPORT STUDIO',bg='#101719',fg='#8fe3ae',font=('TkDefaultFont',18,'bold')).pack(anchor='w')
        tk.Label(h,text='Recursive BulkOCR enrichment with reversible JSON/JSONL checkpoints',bg='#101719',fg='#d5e4df').pack(anchor='w')
        tk.Label(h,text=str(self.root),bg='#101719',fg='#8aa49b',font=('TkFixedFont',9),wraplength=840).pack(anchor='w',pady=(8,0))
        b=tk.Frame(self.w,bg='#101719',padx=24);b.pack(fill='both',expand=True);self.strip=tk.Frame(b,bg='#101719');self.strip.pack(fill='x')
        self.sl=[]
        for i,(n,_) in enumerate(self.stages):
            x=tk.Label(self.strip,text=f'{i+1}  {n}',bg='#1a2528',fg='#79928a',padx=9,pady=6,font=('TkDefaultFont',9,'bold'));x.pack(side='left',padx=(0,5));self.sl.append(x)
        card=tk.Frame(b,bg='#172124',highlightthickness=1,highlightbackground='#2a3b3f');card.pack(fill='both',expand=True,pady=(12,0))
        self.title=tk.Label(card,bg='#172124',fg='white',font=('TkDefaultFont',16,'bold'),anchor='w',padx=20,pady=14);self.title.pack(fill='x')
        self.desc=tk.Label(card,bg='#172124',fg='#b8cac4',anchor='w',padx=20);self.desc.pack(fill='x')
        self.run=tk.BooleanVar(value=True);self.over=tk.BooleanVar(value=False);opts=tk.Frame(card,bg='#172124',padx=20,pady=10);opts.pack(fill='x')
        self.rc=tk.Checkbutton(opts,text='Run BulkOCR recursively before export',variable=self.run,bg='#172124',fg='#d9e7e2',activebackground='#172124',selectcolor='#243438');self.rc.pack(anchor='w')
        self.oc=tk.Checkbutton(opts,text='Overwrite sidecars after backing them up',variable=self.over,bg='#172124',fg='#d9e7e2',activebackground='#172124',selectcolor='#243438');self.oc.pack(anchor='w')
        self.sum=tk.StringVar();tk.Label(card,textvariable=self.sum,bg='#172124',fg='#8fe3ae',anchor='w',padx=20,pady=6,font=('TkDefaultFont',10,'bold')).pack(fill='x')
        lf=tk.Frame(card,bg='#172124',padx=20,pady=(0,16));lf.pack(fill='both',expand=True);self.log=tk.Text(lf,bg='#0d1315',fg='#d9e7e2',font=('TkFixedFont',9),relief='flat',state='disabled',wrap='word',padx=10,pady=8);self.log.pack(fill='both',expand=True)
        f=tk.Frame(self.w,bg='#101719',padx=24,pady=14);f.pack(fill='x');ttk.Button(f,text='Cancel',command=self.cancel).pack(side='left');ttk.Button(f,text='Open Session',command=self.open).pack(side='left',padx=8)
        self.backb=ttk.Button(f,text='← Back',command=self.back);self.backb.pack(side='right',padx=8);self.next=tk.Button(f,text='Continue →',command=self.cont,bg='#237a3b',fg='white',font=('TkDefaultFont',10,'bold'),padx=16,pady=5);self.next.pack(side='right')
    def say(self,x):
        self.log.config(state='normal');self.log.insert('end',str(x).rstrip()+'\n');self.log.see('end');self.log.config(state='disabled')
        if self.status:self.status(str(x).rstrip())
    def _session(self):
        base=self.out/'sessions'/_safe(self.root.name);base.mkdir(parents=True,exist_ok=True);old=sorted([p for p in base.iterdir() if (p/'session.json').is_file()],reverse=True);resume=False
        if old:
            s=_load(old[0]/'session.json',{})
            if s.get('status') not in ('complete','cancelled'):
                resume=messagebox.askyesno('Resume export session?',f'Resume the unfinished session?\n\n{old[0]}',parent=self.w)
        if resume:
            self.dir=old[0];self.s=s;self.stage=int(s.get('current_stage',0));self.run.set(bool(s.get('run_bulkocr',True)));self.over.set(bool(s.get('overwrite_ocr',False)))
            self.images=_load(self.dir/'01-discovered-images.json',{}).get('images',[]);self.ocr=_load(self.dir/'02-bulkocr-results.json',{}).get('records',[]);self.merged=_load(self.dir/'03-merged-works.json',{}).get('works',[]);self.say(f'Resumed {self.dir}')
        else:
            sid=_bd.now().strftime('%Y%m%d-%H%M%S')+'-'+_safe(self.root.name);self.dir=base/sid;self.dir.mkdir();self.s={'schema_version':1,'workflow_version':_BO_VER,'session_id':sid,'created_at':_now(),'updated_at':_now(),'status':'in-progress','database_path':str(self.db),'run_directory':str(self.root),'export_root':str(self.out),'bulkocr_runner':str(self.runner) if self.runner else None,'run_bulkocr':True,'overwrite_ocr':False,'current_stage':0,'current_stage_name':'prepare','completed_stages':[]};_write(self.dir/'session.json',self.s);self.say(f'Created {self.dir}')
        self.say(f'BulkOCR runner: {self.runner or "not found"}')
    def render(self):
