"""Local Windows manager for the Docker Compose deployment."""
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk
import webbrowser

ROOT=Path(__file__).resolve().parent
PROJECT=ROOT.parent

def docker(args, *, timeout=60, stdin=None):
    command=['docker','compose',*args]
    try:
        result=subprocess.run(command,cwd=str(PROJECT),input=stdin,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
    except FileNotFoundError as exc: raise RuntimeError('找不到 Docker。请安装并启动 Docker Desktop。') from exc
    except subprocess.TimeoutExpired as exc: raise RuntimeError('Docker 操作超时，结果可能不完整。请检查 Docker Desktop 和项目状态。') from exc
    output=result.stdout.decode('utf-8',errors='replace')
    if result.returncode: raise RuntimeError('Docker 操作失败：\n'+output[-5000:])
    return output

def check_engine():
    if os.environ.get('DOCKER_HOST') or os.environ.get('DOCKER_CONTEXT'):
        raise RuntimeError('检测到 DOCKER_HOST 或 DOCKER_CONTEXT，无法确认目标为本机。请先清除这两个环境变量，再在 Docker Desktop 选择本机上下文。')
    try:
        context=subprocess.run(['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],cwd=str(PROJECT),capture_output=True,timeout=20)
    except (FileNotFoundError,subprocess.TimeoutExpired) as exc: raise RuntimeError('无法确认 Docker 上下文。') from exc
    endpoint=context.stdout.decode('utf-8',errors='replace').strip().lower()
    if context.returncode or not endpoint.startswith('npipe:////./pipe/'):
        raise RuntimeError('当前 Docker 上下文不是本机 Windows 命名管道；请在 Docker Desktop 选择本机 Linux 引擎。')
    try: p=subprocess.run(['docker','info','--format','{{.OSType}}'],cwd=str(PROJECT),capture_output=True,timeout=25)
    except (FileNotFoundError,subprocess.TimeoutExpired) as exc: raise RuntimeError('Docker Desktop 未安装、未启动或响应超时。') from exc
    if p.returncode: raise RuntimeError('Docker 引擎未就绪。请启动 Docker Desktop 后重试。')
    if p.stdout.decode('utf-8',errors='replace').strip()!='linux': raise RuntimeError('请将 Docker Desktop 切换为 Linux 容器。')

def clean_host_data():
    def link(path):
        return path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction())
    folder=ROOT/'云端视频'
    if link(folder): raise RuntimeError('云端视频目录是链接，拒绝清理。')
    if folder.exists():
        if not folder.is_dir(): raise RuntimeError('云端视频路径不是目录。')
        for item in folder.iterdir():
            if link(item) or not item.is_dir(): item.unlink()
            else: shutil.rmtree(item)
        if any(folder.iterdir()): raise RuntimeError('云端视频目录仍有文件。')
    profiles=ROOT/'云端配置'
    if link(profiles): raise RuntimeError('云端配置目录是链接，拒绝清理历史。')
    if profiles.exists():
        if not profiles.is_dir(): raise RuntimeError('云端配置路径不是目录。')
        for d in profiles.iterdir():
            if link(d) or not d.is_dir() or not re.fullmatch(r'[0-9a-f]{32}',d.name): raise RuntimeError('存在无法识别的云端配置目录：'+str(d))
            history=d/'history.json'
            if link(history): raise RuntimeError('任务历史是链接：'+str(history))
            if history.exists(): history.unlink()
            if history.exists(): raise RuntimeError('任务历史未清理：'+str(history))

def execute(action):
    if action in ('start','stop','status','clear'): check_engine()
    if action=='start':
        result=docker(['up','-d','--wait','--wait-timeout','180'],timeout=300)
        return result+'\n项目已启动。默认网页：http://127.0.0.1:8080 （自定义端口请查看 compose 配置）。'
    if action=='stop': return docker(['stop','-t','10'],timeout=60)+'\n本项目已停止；Docker Desktop 仍运行。'
    if action=='status': return docker(['ps','-a'],timeout=45)
    if action=='clear':
        docker(['stop','-t','10'],timeout=60)
        script=(ROOT/'cleanup.py').read_bytes()
        result=docker(['run','--rm','--no-deps','--pull','never','-T','--entrypoint','python','backend','-'],timeout=300,stdin=script)
        try: clean_host_data()
        except Exception as exc: raise RuntimeError('数据库清理已执行，但本机文件清理未完成：'+str(exc)) from exc
        return result+'\n云端任务历史和本机云端视频已清理；配置与系统凭据保留。项目保持关闭。'
    raise ValueError('未知操作')

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('VideoFactory Windows 管理'); self.geometry('800x590'); self.minsize(650,480)
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.busy=False; self.events=queue.Queue(); self.columnconfigure(0,weight=1); self.rowconfigure(2,weight=1)
        ttk.Label(self,text='VideoFactory Windows 管理',font=('Microsoft YaHei UI',16,'bold')).grid(row=0,column=0,pady=12)
        bar=ttk.Frame(self);bar.grid(row=1,column=0,padx=8,pady=8)
        self.buttons=[]
        for label,action in [('一键开启','start'),('一键关闭','stop'),('查看状态','status'),('云端配置与视频','cloud'),('清空项目与任务','clear')]:
            b=ttk.Button(bar,text=label,command=lambda a=action:self.run(a));b.pack(side='left',padx=4);self.buttons.append(b)
        self.log=scrolledtext.ScrolledText(self,wrap='word',state='disabled');self.log.grid(row=2,column=0,sticky='nsew',padx=12,pady=8)
        self.state=tk.StringVar(value='请先启动 Docker Desktop，确认使用 Linux 容器。');ttk.Label(self,textvariable=self.state,wraplength=750).grid(row=3,column=0,sticky='w',padx=12,pady=8)
        self.after(150,self.pump)
    def logline(self,text):
        self.log.configure(state='normal');self.log.insert('end',text+'\n');self.log.see('end');self.log.configure(state='disabled')
    def run(self,action):
        if self.busy:return
        if action=='cloud':
            try: subprocess.Popen([sys.executable,'-B',str(ROOT/'cloud_manager.py')],cwd=str(ROOT))
            except OSError as e: messagebox.showerror('无法打开',str(e))
            return
        if action=='clear':
            answer=simpledialog.askstring('清空项目与任务','将删除本项目的项目、任务、素材、数据库备份、本机云端任务历史和视频；保留全局配置及密钥。云端、其他项目、导出文件、快照和备份不在范围。输入“清空”继续：',parent=self)
            if answer!='清空': return
            if not messagebox.askyesno('最后确认','此操作不可撤销。\n无法保证 SSD、快照和备份上的数据不可恢复。确定执行？',icon='warning'): return
        self.busy=True
        for b in self.buttons:b.config(state='disabled')
        self.state.set('正在处理…');self.logline('执行操作：'+action)
        def worker():
            try:self.events.put(('ok',action,execute(action)))
            except Exception as e:self.events.put(('error',action,str(e)))
        threading.Thread(target=worker,daemon=True).start()
    def pump(self):
        try:
            while True:
                kind,action,text=self.events.get_nowait();self.busy=False
                for b in self.buttons:b.config(state='normal')
                self.logline(text);self.state.set('已完成' if kind=='ok' else '未完成，请查看上方信息')
                if kind=='ok' and action=='start':webbrowser.open('http://127.0.0.1:8080')
                if kind=='error':messagebox.showerror('操作未完成',text)
        except queue.Empty: pass
        self.after(150,self.pump)
    def close(self):
        if self.busy:
            messagebox.showwarning('操作进行中','请等待当前操作完成，避免留下不完整状态。'); return
        self.destroy()

if __name__=='__main__':
    if not (PROJECT/'compose.yaml').is_file(): raise SystemExit('未找到项目 compose.yaml；请把管理程序放在项目目录内。')
    App().mainloop()
