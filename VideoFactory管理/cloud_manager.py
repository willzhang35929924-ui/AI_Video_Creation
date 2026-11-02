import hashlib, json, queue, sys, threading, time, tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from urllib.parse import urlparse
import cloud_backend as core
SECRET_STORE = 'Windows 凭据管理器' if sys.platform=='win32' else 'macOS 钥匙串'

LINKFOX_DEFAULTS={'kind':'linkfox','submit_url':'https://ai-api.linkfox.com/image/v3/make/salesVideo','poll_url':'https://ai-api.linkfox.com/image/v2/make/info','poll_method':'POST','id_path':'data.id','status_path':'data.status','result_path':'data.resultList.0.url','success_values':'3','failure_values':'4'}
GENERIC_DEFAULTS={'auth_header':'Authorization','auth_prefix':'Bearer ','poll_method':'GET','submit_template':core.DEFAULT_TEMPLATE,'poll_template':{},'id_path':'id','status_path':'status','result_path':'output_url','success_values':'completed,succeeded','failure_values':'failed,cancelled','test_method':'GET','test_body':{},'test_success_path':'','test_success_value':''}

def connection_text(result):
    if not isinstance(result,dict) or not result.get('state'): return '连接状态：未测试'
    names={'connected':'连接成功','failed':'连接失败','unconfigured':'测试接口未配置'}
    state=names.get(result['state'],'未测试')
    when=time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(result.get('checked_at',0))) if result.get('checked_at') else ''
    return '连接状态：'+state+('（'+when+'）' if when else '')+'。'+result.get('message','')

class ProfileDialog(tk.Toplevel):
    def __init__(self,parent,on_save):
        super().__init__(parent); self.title('添加云端配置'); self.geometry('800x740'); self.minsize(650,580); self.on_save=on_save
        self.transient(parent); self.grab_set(); self.columnconfigure(1,weight=1); self.rowconfigure(8,weight=1)
        self.results=queue.Queue(); self.testing=False; self.test_result=None; self.test_fingerprint=None
        self.name=tk.StringVar(); self.kind=tk.StringVar(value='LinkFox 带货视频'); self.model=tk.StringVar(value='minimax-h3-lite'); self.submit=tk.StringVar(value=LINKFOX_DEFAULTS['submit_url']); self.poll=tk.StringVar(value=LINKFOX_DEFAULTS['poll_url']); self.key=tk.StringVar()
        self.test_url=tk.StringVar(value='https://ai-api.linkfox.com/v1/userPlan/info'); self.test_status=tk.StringVar(value='连接状态：未测试')
        fields=[('配置名称',self.name),('模型名称',self.model),('提交 API URL',self.submit),('查询 API URL',self.poll),('API Key',self.key)]
        ttk.Label(self,text='协议类型').grid(row=0,column=0,sticky='w',padx=12,pady=5)
        combo=ttk.Combobox(self,textvariable=self.kind,values=('LinkFox 带货视频','自定义异步 JSON'),state='readonly'); combo.grid(row=0,column=1,sticky='ew',padx=12,pady=5); combo.bind('<<ComboboxSelected>>',self.kind_changed)
        for i,(label,var) in enumerate(fields,1):
            ttk.Label(self,text=label).grid(row=i,column=0,sticky='w',padx=12,pady=5)
            ttk.Entry(self,textvariable=var,show='•' if label=='API Key' else '').grid(row=i,column=1,sticky='ew',padx=12,pady=5)
        ttk.Label(self,text='只读测试 URL').grid(row=6,column=0,sticky='w',padx=12,pady=5)
        self.test_entry=ttk.Entry(self,textvariable=self.test_url,state='readonly'); self.test_entry.grid(row=6,column=1,sticky='ew',padx=12,pady=5)
        ttk.Label(self,text='自定义协议参数',anchor='nw').grid(row=7,column=0,sticky='nw',padx=12,pady=5)
        self.advanced=scrolledtext.ScrolledText(self,height=8,wrap='word'); self.advanced.grid(row=7,column=1,rowspan=2,sticky='nsew',padx=12,pady=5)
        self.hint=ttk.Label(self,text='自定义平台请填写只读测试 URL 和高级参数中的成功字段；不要使用会创建任务的接口。',wraplength=650); self.hint.grid(row=9,column=0,columnspan=2,sticky='w',padx=12,pady=6)
        ttk.Label(self,textvariable=self.test_status,wraplength=700).grid(row=10,column=0,columnspan=2,sticky='w',padx=12,pady=4)
        buttons=ttk.Frame(self); buttons.grid(row=11,column=0,columnspan=2,pady=8)
        self.test_btn=ttk.Button(buttons,text='测试连接',command=self.start_test); self.test_btn.pack(side='left',padx=8)
        self.save_btn=ttk.Button(buttons,text='保存配置',command=self.save); self.save_btn.pack(side='left',padx=8)
        ttk.Button(buttons,text='取消',command=self.destroy).pack(side='left',padx=8)
        self.advanced.insert('1.0',json.dumps(GENERIC_DEFAULTS,ensure_ascii=False,indent=2)); self.advanced.configure(state='disabled')
        self.advanced.edit_modified(False); self.advanced.bind('<<Modified>>',self.advanced_changed)
        for var in (self.name,self.kind,self.model,self.submit,self.poll,self.key,self.test_url): var.trace_add('write',self.changed)
        self.after(100,self.poll_test)
    def changed(self,*_):
        self.test_result=None; self.test_fingerprint=None
        if not self.testing: self.test_status.set('连接状态：未测试（配置已更改）')
        if self.kind.get().startswith('LinkFox'):
            parsed=urlparse(self.submit.get().strip())
            auto=parsed.scheme+'://'+parsed.netloc+'/v1/userPlan/info' if parsed.scheme and parsed.netloc else ''
            if self.test_url.get()!=auto: self.test_url.set(auto)
    def advanced_changed(self,event=None):
        if self.advanced.edit_modified(): self.advanced.edit_modified(False); self.changed()
    def kind_changed(self,event=None):
        generic=self.kind.get().startswith('自定义')
        self.advanced.configure(state='normal' if generic else 'disabled')
        self.test_entry.configure(state='normal' if generic else 'readonly')
        self.submit.set('' if generic else LINKFOX_DEFAULTS['submit_url']); self.poll.set('' if generic else LINKFOX_DEFAULTS['poll_url']); self.model.set('' if generic else 'minimax-h3-lite')
        if generic: self.test_url.set('')
    def collect(self):
        p={'name':self.name.get().strip(),'model':self.model.get().strip(),'submit_url':self.submit.get().strip(),'poll_url':self.poll.get().strip()}
        if self.kind.get().startswith('LinkFox'):
            p.update(LINKFOX_DEFAULTS); p['submit_url']=self.submit.get().strip(); p['poll_url']=self.poll.get().strip()
        else:
            advanced=json.loads(self.advanced.get('1.0','end')); p.update(advanced); p['kind']='generic'
            p.update(name=self.name.get().strip(),model=self.model.get().strip(),submit_url=self.submit.get().strip(),poll_url=self.poll.get().strip(),test_url=self.test_url.get().strip())
        return core.validate(p)
    @staticmethod
    def fingerprint(profile,key):
        return hashlib.sha256((json.dumps(profile,sort_keys=True,ensure_ascii=False)+'\0'+key).encode()).hexdigest()
    def start_test(self):
        try:
            p=self.collect(); key=self.key.get().strip()
            if not key: raise ValueError('请填写 API Key。')
            core.test_settings(p)
        except Exception as exc: self.test_status.set('连接状态：未测试。'+str(exc)); return
        fingerprint=self.fingerprint(p,key); self.testing=True; self.test_result=None; self.test_fingerprint=None
        self.test_btn.config(state='disabled'); self.save_btn.config(state='disabled'); self.test_status.set('连接状态：测试中…')
        def worker():
            try: result=core.probe_connection(p,key)
            except Exception: result={'state':'failed','message':'测试时发生错误，请检查网络和配置。','checked_at':int(time.time())}
            self.results.put((fingerprint,result))
        threading.Thread(target=worker,daemon=True).start()
    def poll_test(self):
        try:
            fingerprint,result=self.results.get_nowait(); self.testing=False
            self.test_btn.config(state='normal'); self.save_btn.config(state='normal')
            try: current=self.fingerprint(self.collect(),self.key.get().strip())
            except Exception: current=''
            if fingerprint==current:
                self.test_result=result; self.test_fingerprint=fingerprint; self.test_status.set(connection_text(result))
            else: self.test_status.set('连接状态：未测试（测试期间配置已更改）')
        except queue.Empty: pass
        self.after(100,self.poll_test)
    def save(self):
        try:
            p=self.collect()
            if self.test_result and self.test_fingerprint==self.fingerprint(p,self.key.get().strip()): p['connection']=self.test_result
            result=core.create_profile(p,self.key.get()); self.key.set(''); self.on_save(result['id']); self.destroy()
        except Exception as exc: messagebox.showerror('无法保存配置',str(exc),parent=self)

class TestSettingsDialog(tk.Toplevel):
    def __init__(self,parent,profile,on_save):
        super().__init__(parent); self.title('设置只读测试接口'); self.geometry('760x420'); self.minsize(650,350)
        self.transient(parent); self.grab_set(); self.profile=profile; self.on_save=on_save; self.columnconfigure(1,weight=1); self.rowconfigure(4,weight=1)
        self.url=tk.StringVar(value=profile.get('test_url','')); self.method=tk.StringVar(value=profile.get('test_method','GET'))
        self.path=tk.StringVar(value=profile.get('test_success_path','')); self.value=tk.StringVar(value=profile.get('test_success_value',''))
        for row,(label,var) in enumerate((('只读测试 URL',self.url),('成功字段路径',self.path),('成功字段期望值',self.value))):
            ttk.Label(self,text=label).grid(row=row,column=0,padx=12,pady=7,sticky='w'); ttk.Entry(self,textvariable=var).grid(row=row,column=1,padx=12,pady=7,sticky='ew')
        ttk.Label(self,text='请求方法').grid(row=3,column=0,padx=12,pady=7,sticky='w')
        ttk.Combobox(self,textvariable=self.method,values=('GET','POST'),state='readonly').grid(row=3,column=1,padx=12,pady=7,sticky='w')
        ttk.Label(self,text='POST 请求体 JSON').grid(row=4,column=0,padx=12,pady=7,sticky='nw')
        self.body=scrolledtext.ScrolledText(self,height=5,wrap='word'); self.body.grid(row=4,column=1,padx=12,pady=7,sticky='nsew'); self.body.insert('1.0',json.dumps(profile.get('test_body',{}),ensure_ascii=False,indent=2))
        ttk.Label(self,text='只填写供应商明确说明为只读的接口。测试不会使用视频提交地址。',wraplength=700).grid(row=5,column=0,columnspan=2,padx=12,pady=5,sticky='w')
        buttons=ttk.Frame(self); buttons.grid(row=6,column=0,columnspan=2,pady=8); ttk.Button(buttons,text='保存',command=self.save).pack(side='left',padx=8); ttk.Button(buttons,text='取消',command=self.destroy).pack(side='left',padx=8)
    def save(self):
        try:
            changes={'test_url':self.url.get().strip(),'test_method':self.method.get(),'test_success_path':self.path.get().strip(),
                     'test_success_value':self.value.get().strip(),'test_body':json.loads(self.body.get('1.0','end')),'connection':None}
            core.test_settings({**self.profile,**changes})
            updated=core.update_profile(self.profile['id'],changes); self.on_save(updated['id']); self.destroy()
        except Exception as exc: messagebox.showerror('无法保存测试接口',str(exc),parent=self)

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('云端视频配置与生成'); self.geometry('880x710'); self.minsize(720,600)
        self.protocol('WM_DELETE_WINDOW',self.close); self.busy=False; self.msg=queue.Queue(); self.profiles=[]; self.current_id=None
        self.columnconfigure(0,weight=1); self.rowconfigure(2,weight=1)
        top=ttk.LabelFrame(self,text='云端 API 配置'); top.grid(row=0,column=0,sticky='ew',padx=14,pady=10); top.columnconfigure(0,weight=1)
        self.profile_var=tk.StringVar(); self.combo=ttk.Combobox(top,textvariable=self.profile_var,state='readonly'); self.combo.grid(row=0,column=0,sticky='ew',padx=8,pady=8); self.combo.bind('<<ComboboxSelected>>',self.selected)
        self.add=ttk.Button(top,text='添加新配置',command=lambda:ProfileDialog(self,self.refresh)); self.add.grid(row=0,column=1,padx=4)
        self.delete=ttk.Button(top,text='删除所选配置',command=self.remove); self.delete.grid(row=0,column=2,padx=4)
        self.details=ttk.Label(top,text='尚无配置，请点击“添加新配置”。',wraplength=790); self.details.grid(row=1,column=0,columnspan=3,sticky='w',padx=8,pady=(0,8))
        self.connection_status=tk.StringVar(value='连接状态：未测试'); ttk.Label(top,textvariable=self.connection_status,wraplength=570).grid(row=2,column=0,sticky='w',padx=8,pady=(0,8))
        self.test_connection=ttk.Button(top,text='测试连接',command=self.test_selected); self.test_connection.grid(row=2,column=1,padx=4,pady=(0,8))
        self.test_settings=ttk.Button(top,text='设置测试接口',command=self.configure_test); self.test_settings.grid(row=2,column=2,padx=4,pady=(0,8))
        form=ttk.LabelFrame(self,text='视频内容'); form.grid(row=1,column=0,sticky='ew',padx=14,pady=4); form.columnconfigure(1,weight=1)
        self.image=tk.StringVar(value=core.DEFAULT_IMAGE); self.duration=tk.StringVar(value='5'); self.resolution=tk.StringVar(value='768P'); self.ratio=tk.StringVar(value='9:16')
        ttk.Label(form,text='参考图片 URL').grid(row=0,column=0,sticky='w',padx=8,pady=5); ttk.Entry(form,textvariable=self.image).grid(row=0,column=1,columnspan=5,sticky='ew',padx=8,pady=5)
        for i,(label,var,values) in enumerate([('秒数',self.duration,('5','10','15')),('分辨率',self.resolution,('768P','1080P','720P','480P')),('比例',self.ratio,('9:16','16:9','1:1','3:4','4:3','adaptive'))]):
            ttk.Label(form,text=label).grid(row=1,column=i*2,sticky='w',padx=8); ttk.Combobox(form,textvariable=var,values=values,width=10).grid(row=1,column=i*2+1,sticky='w',pady=5)
        body=ttk.LabelFrame(self,text='剧本 / 视频提示词'); body.grid(row=2,column=0,sticky='nsew',padx=14,pady=4); body.columnconfigure(0,weight=1); body.rowconfigure(0,weight=1)
        self.prompt=tk.Text(body,wrap='word',height=5); self.prompt.grid(row=0,column=0,sticky='nsew',padx=8,pady=8); self.prompt.insert('1.0',core.DEFAULT_PROMPT)
        bottom=ttk.LabelFrame(self,text='历史任务与操作'); bottom.grid(row=3,column=0,sticky='ew',padx=14,pady=7); bottom.columnconfigure(1,weight=1)
        ttk.Label(bottom,text='历史任务').grid(row=0,column=0,padx=8,pady=5)
        self.task=tk.StringVar(); self.tasks=ttk.Combobox(bottom,textvariable=self.task,state='normal'); self.tasks.grid(row=0,column=1,sticky='ew',padx=8,pady=5)
        ttk.Label(bottom,text='选择任务可恢复查询，不会重新提交。').grid(row=1,column=0,columnspan=3,sticky='w',padx=8)
        btn=ttk.Frame(bottom); btn.grid(row=2,column=0,columnspan=3,pady=8)
        self.submit=ttk.Button(btn,text='使用所选配置生成',command=lambda:self.run(False)); self.submit.pack(side='left',padx=8)
        self.resume=ttk.Button(btn,text='查询并下载历史任务',command=lambda:self.run(True)); self.resume.pack(side='left',padx=8)
        self.status=tk.StringVar(value='就绪'); ttk.Label(self,textvariable=self.status,wraplength=820).grid(row=4,column=0,sticky='w',padx=16,pady=(2,12))
        self.refresh(); self.after(150,self.pump)
    def refresh(self,select=None):
        self.profiles=core.list_profiles(); labels=[p['name']+'  ['+('LinkFox' if p['kind']=='linkfox' else '自定义')+']  '+p['id'][:6] for p in self.profiles]
        self.combo['values']=labels
        if self.profiles:
            idx=next((i for i,p in enumerate(self.profiles) if p['id']==(select or self.current_id)),0); self.combo.current(idx); self.selected()
        else:
            self.current_id=None; self.combo.set(''); self.details.config(text='尚无配置，请点击“添加新配置”。'); self.connection_status.set('连接状态：未测试'); self.tasks['values']=[]; self.task.set('')
    def current(self):
        return next((p for p in self.profiles if p['id']==self.current_id),None)
    def selected(self,event=None):
        i=self.combo.current()
        if i<0 or i>=len(self.profiles): return
        p=self.profiles[i]; self.current_id=p['id']; self.details.config(text='模型：'+p['model']+'　提交地址：'+p['submit_url']+'\nKey 已保存在 '+SECRET_STORE+'；请求前请核对地址和价格。')
        self.connection_status.set(connection_text(p.get('connection')))
        self.test_settings.config(state='normal' if p['kind']=='generic' and not self.busy else 'disabled')
        try:
            history=core.get_history(p['id']); self.tasks['values']=[str(x['task_id']) for x in history]; self.task.set('')
        except Exception as exc: self.tasks['values']=[]; self.task.set(''); self.status.set('历史读取失败：'+str(exc))
    def remove(self):
        p=self.current()
        if self.busy: messagebox.showwarning('任务进行中','请等待本次查询或生成完成后再删除。'); return
        if not p: return
        if not messagebox.askyesno('确认删除','删除配置“'+p['name']+'”、其本地任务记录和'+SECRET_STORE+'中的密钥？\n已生成的视频文件及供应商云端记录会保留。',icon='warning'): return
        try:
            core.remove_profile(p['id']); self.current_id=None; self.refresh(); self.status.set('配置、任务记录和系统凭据已删除。')
        except Exception as exc: messagebox.showerror('删除未完成',str(exc))
    def configure_test(self):
        p=self.current()
        if p and p['kind']=='generic' and not self.busy: TestSettingsDialog(self,p,self.refresh)
    def test_selected(self):
        p=self.current()
        if self.busy or not p:
            if not p: messagebox.showwarning('请选择配置','请先添加并选择云端配置。')
            return
        try: core.test_settings(p)
        except Exception as exc: self.connection_status.set('连接状态：未测试。'+str(exc)); return
        self.busy=True; self.set_buttons(False); self.connection_status.set('连接状态：测试中…')
        def worker():
            try:
                try:
                    key=core.Keychain().get(p['id']); result=core.probe_connection(p,key)
                except Exception:
                    result={'state':'failed','message':'无法读取系统凭据或执行测试。','checked_at':int(time.time())}
                core.update_profile(p['id'],{'connection':result}); self.msg.put(('test_done',result))
            except Exception as exc: self.msg.put(('test_error','测试或状态保存失败：'+str(exc)))
        threading.Thread(target=worker,daemon=True).start()
    def run(self,resume):
        p=self.current()
        if self.busy or not p:
            if not p: messagebox.showwarning('请选择配置','请先添加并选择云端配置。')
            return
        task=self.task.get().strip() if resume else ''
        if resume and not task: messagebox.showwarning('缺少任务编号','请选择或输入已有任务编号。'); return
        try:
            duration=int(self.duration.get()); assert 1<=duration<=120
        except Exception: messagebox.showwarning('参数错误','时长必须是 1 至 120 秒的整数。'); return
        prompt=self.prompt.get('1.0','end').strip(); image=self.image.get().strip()
        if not resume and (not prompt or (p['kind']=='linkfox' and not image)): messagebox.showwarning('参数错误','请填写脚本；LinkFox 还需要公网参考图片 URL。'); return
        if not resume and p['kind']=='linkfox' and p['model'].lower()=='minimax-h3-lite' and (duration not in (5,10,15) or self.resolution.get() not in ('768P','1080P')):
            messagebox.showwarning('参数错误','LinkFox 当前预设支持 5/10/15 秒与 768P/1080P；具体请以模型文档为准。'); return
        if not resume and not messagebox.askyesno('确认云端生成','使用“'+p['name']+'”提交一次任务，可能产生费用。\n请先核对模型和供应商价格。继续？'): return
        self.busy=True; self.set_buttons(False); self.status.set('正在连接并处理，请勿重复提交…')
        # Snapshot Tk variables before leaving the UI thread.
        resolution,ratio=self.resolution.get(),self.ratio.get()
        def safe_worker():
            try:
                key=core.Keychain().get(p['id']); result=core.generate(p,key,prompt,image,duration,resolution,ratio,task,lambda text:self.msg.put(('status',text))); self.msg.put(('done',result))
            except Exception as exc: self.msg.put(('error',str(exc)))
        threading.Thread(target=safe_worker,daemon=True).start()
    def set_buttons(self,enabled):
        for button in (self.submit,self.resume,self.add,self.delete,self.test_connection): button.config(state='normal' if enabled else 'disabled')
        p=self.current(); self.test_settings.config(state='normal' if enabled and p and p['kind']=='generic' else 'disabled')
        self.combo.config(state='readonly' if enabled else 'disabled')
    def pump(self):
        try:
            while True:
                kind,data=self.msg.get_nowait()
                if kind=='status': self.status.set(data)
                else:
                    self.busy=False; self.set_buttons(True); self.refresh(self.current_id)
                    if kind=='test_done': self.connection_status.set(connection_text(data)); self.status.set('只读连接测试已完成。')
                    elif kind=='test_error': self.connection_status.set('连接状态：测试失败。'+data); self.status.set('测试未完成。')
                    elif kind=='done': self.status.set('完成：'+data[0]); messagebox.showinfo('视频已保存',data[0])
                    else: self.status.set('未完成：'+data); messagebox.showerror('操作未完成',data)
        except queue.Empty: pass
        self.after(150,self.pump)
    def close(self):
        if self.busy:
            messagebox.showwarning('任务进行中','请等待当前任务结束；关闭程序可能使任务状态未知。'); return
        self.destroy()

if __name__=='__main__': App().mainloop()
