"""钧飞ERP独立刊登窗口。浏览器步骤由本地 recipe.json 提供。"""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from materials import inspect_folder
from contextlib import nullcontext
from ego_link import browser_config, ego_connection_command, ego_draft_command, ego_space, require_legacy_browser, parse_connection_result

ROOT = Path(__file__).resolve().parent


def node_executable():
    """Prefer the bundled runtime so the standalone EXE does not need system Node."""
    bundled = Path(getattr(sys, '_MEIPASS', ROOT)) / 'node.exe'
    return str(bundled) if bundled.is_file() else shutil.which('node')


class Publisher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('钧飞 ERP · 亚马逊批量刊登')
        self.geometry('1000x720')
        self.minsize(850, 600)
        self.events = queue.Queue()
        self.process = None
        self.busy = False
        self.login_waiting = False
        self.output_tail = ''
        self.last_error = ''
        self.material_folder = ''
        self.source = tk.StringVar()
        self.recipe = tk.StringVar(value=str(ROOT / 'recipe.json'))
        self.status = tk.StringVar(value='请选择产品 JSON；当前页面适配尚未校准')
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TButton', padding=8)
        body = ttk.Frame(self, padding=22)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='钧飞 ERP 上架工作台', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
        ttk.Label(body, text='本地资料 → 变体与图片预检 → 保存草稿 → 提交发布', padding=(0, 8)).pack(anchor='w')
        ttk.Button(body, text='导入产品文件夹（Word / TXT / 图片）', command=self.import_folder).pack(anchor='w', pady=5)
        ttk.Button(body, text='连接 ego · 专属工作区', command=self.connect_brain).pack(anchor='w', pady=5)
        ttk.Button(body, text='修改已有草稿（不发布）', command=self.update_existing_draft).pack(anchor='w', pady=5)
        for label, variable, command in [('产品资料 JSON', self.source, self.pick_source), ('ERP 页面配置', self.recipe, self.pick_recipe)]:
            row = ttk.Frame(body)
            row.pack(fill='x', pady=5)
            ttk.Label(row, text=label, width=16).pack(side='left')
            ttk.Entry(row, textvariable=variable).pack(side='left', fill='x', expand=True)
            ttk.Button(row, text='选择文件', command=command).pack(side='left', padx=(8, 0))
        self.table = ttk.Treeview(body, columns=('sku', 'store', 'market', 'variants'), show='headings', height=7)
        for name, title in [('sku', '父 SKU'), ('store', '店铺'), ('market', '站点'), ('variants', '变体数')]:
            self.table.heading(name, text=title)
            self.table.column(name, width=180)
        self.table.pack(fill='x', pady=12)
        row = ttk.Frame(body)
        row.pack(fill='x')
        for title, command in [('安装依赖', self.install), ('本地预检', lambda: self.run('check')), ('批量保存草稿', lambda: self.run('draft')), ('批量提交发布', lambda: self.run('publish'))]:
            ttk.Button(row, text=title, command=command).pack(side='left', padx=(0, 8))
        ttk.Button(row, text='登录完成，继续', command=self.login_done).pack(side='left')
        ttk.Label(body, textvariable=self.status, wraplength=920, padding=(0, 12)).pack(anchor='w')
        self.log = tk.Text(body, height=13, wrap='word', font=('Consolas', 10), state='disabled')
        self.log.pack(fill='both', expand=True)
        ttk.Label(body, text='提交成功仅表示 ERP 接收；亚马逊在线状态需另行核实。关闭窗口会中断当前批次。').pack(anchor='w', pady=(8, 0))
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(100, self.poll)

    def import_folder(self):
        folder = filedialog.askdirectory(title='选择单个产品资料文件夹')
        if not folder:
            return
        try:
            result = inspect_folder(folder)
            self.material_folder = folder
            window = tk.Toplevel(self)
            window.title('产品原始资料 · 请核对颜色、尺码和图片映射')
            window.geometry('900x650')
            text = tk.Text(window, wrap='word', font=('Microsoft YaHei UI', 10))
            text.pack(fill='both', expand=True, padx=15, pady=15)
            sections = [f'目录：{folder}', f'文档 {len(result["documents"])} 份；图片 {len(result["images"])} 张', '以下为原始内容，尚未自动转换为可发布产品。颜色配图、库存、售价、类目和店铺仍须明确。']
            for doc in result['documents']:
                sections.extend(['\n── ' + doc['file'] + ' ──', doc['text']])
            sections.extend(['\n图片清单：', *result['images'], '\n读取问题：', *result['errors']])
            text.insert('1.0', '\n'.join(sections))
            text.configure(state='disabled')
            def save():
                name = filedialog.asksaveasfilename(parent=window, defaultextension='.json', initialfile='资料读取清单.json')
                if name:
                    Path(name).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            ttk.Button(window, text='保存读取清单', command=save).pack(pady=(0, 12))
            self.status.set(f'已读取 {len(result["documents"])} 份文档、{len(result["images"])} 张图片；等待资料映射')
            if result['errors']:
                messagebox.showwarning('部分资料读取失败', '\n'.join(result['errors']), parent=window)
        except Exception as error:
            messagebox.showerror('资料读取失败', str(error))

    def connect_brain(self):
        try:
            config = browser_config()
            self.start(ego_connection_command(config=config), space_id=config['task_space_id'])
        except Exception as error:
            messagebox.showerror('ego连接未完成', str(error))

    def pick_source(self):
        name = filedialog.askopenfilename(filetypes=[('产品 JSON', '*.json')])
        if not name:
            return
        try:
            products = json.loads(Path(name).read_text(encoding='utf-8-sig'))
            if not isinstance(products, list) or not products or not all(isinstance(p, dict) for p in products):
                raise ValueError('产品文件必须为非空对象数组')
            self.table.delete(*self.table.get_children())
            for p in products:
                self.table.insert('', 'end', values=(p.get('parentSku', ''), p.get('store', ''), p.get('marketplace', ''), len(p.get('variants', []))))
            self.source.set(name)
            self.status.set(f'已读取 {len(products)} 个产品；请先运行本地预检')
        except Exception as error:
            messagebox.showerror('读取失败', str(error))

    def pick_recipe(self):
        name = filedialog.askopenfilename(filetypes=[('页面配置 JSON', '*.json')])
        if name:
            self.recipe.set(name)

    def install(self):
        if (Path(getattr(sys, '_MEIPASS', ROOT)) / 'node_modules' / 'playwright').is_dir():
            messagebox.showinfo('依赖已内置', '此桌面版已包含刊登运行依赖。')
            return
        npm = shutil.which('npm.cmd') or shutil.which('npm')
        if not npm:
            messagebox.showerror('缺少依赖', '请先安装 Node.js 22 或更新版本')
            return
        self.start([npm, 'install'])

    def update_existing_draft(self):
        """Run the Ego copy adapter; this path cannot publish."""
        if self.busy:
            messagebox.showinfo('正在执行', '请等待当前任务完成')
            return
        name = filedialog.askopenfilename(title='选择已有草稿修改请求', filetypes=[('JSON', '*.json')])
        if not name:
            return
        try:
            request = json.loads(Path(name).read_text(encoding='utf-8-sig'))
            if not isinstance(request, dict) or not str(request.get('draftId', '')).isdigit():
                raise ValueError('请求必须包含明确的草稿ID')
            store, sku, patch = request.get('store'), request.get('erpSku'), request.get('patch')
            if not isinstance(store, str) or not store.strip() or not isinstance(sku, str) or not sku.strip():
                raise ValueError('请求必须包含列表核对用的店铺与ERP SKU')
            if not isinstance(patch, dict) or not patch:
                raise ValueError('请求必须至少包含一个明确修改字段')
            config = browser_config()
            command = ego_draft_command(name, config=config)
            summary = json.dumps(patch, ensure_ascii=False)
            if not messagebox.askyesno('确认修改已有草稿',
                    f'在ego专属工作区修改此钧飞草稿并保存。\n\n草稿ID：{request["draftId"]}\n店铺：{store}\nERP SKU：{sku}\n修改字段：{summary}\n\n继续执行并重开核对？'):
                return
            self.start(command, space_id=config['task_space_id'])
            self.status.set('ego核对草稿身份、保存指定字段并重开验证中')
        except Exception as error:
            messagebox.showerror('无法执行草稿修改', str(error))

    def run(self, mode):
        if mode != 'check':
            try:
                require_legacy_browser()
            except Exception as error:
                messagebox.showinfo('使用ego专属工作区', str(error))
                return
        if self.busy:
            messagebox.showinfo('正在执行', '请等待当前任务完成')
            return
        source = Path(self.source.get())
        if not source.is_file():
            messagebox.showerror('缺少产品文件', '请选择真实产品 JSON，图片路径相对该文件所在目录')
            return
        node = node_executable()
        if not node:
            messagebox.showerror('缺少依赖', '请先安装 Node.js')
            return
        command = [node, str(ROOT / 'listing.mjs'), '--products', str(source), '--output', str(source.parent / 'junfei-output')]
        if mode != 'check':
            try:
                raw = Path(self.recipe.get()).read_text(encoding='utf-8-sig')
                recipe = json.loads(raw)
                if recipe.get('calibrated') is not True or 'REPLACE_' in raw:
                    raise ValueError('钧飞 ERP 页面配置尚未校准，暂不能执行草稿保存或发布。可先使用本地预检。')
            except Exception as error:
                messagebox.showerror('页面适配未就绪', str(error))
                return
            command += ['--recipe', self.recipe.get(), '--run']
            if mode == 'publish':
                if not messagebox.askyesno('批量提交', '将对所选文件中的产品创建草稿并提交发布。请确认店铺、站点、价格及图片均已核对。继续？'):
                    return
                command += ['--publish']
        self.start(command)

    def start(self, command, *, space_id=None):
        if self.busy:
            return
        self.busy = True
        self.output_tail = ''
        self.last_error = ''
        self.status.set('执行中…')
        def work():
            try:
                env = dict(os.environ, FORCE_COLOR='0')
                with (ego_space(space_id) if space_id is not None else nullcontext()):
                    self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env, creationflags=subprocess.CREATE_NO_WINDOW)
                    # 登录提示无换行，因此逐字符转发。
                    while True:
                        char = self.process.stdout.read(1)
                        if not char:
                            break
                        self.events.put(('text', char))
                    self.events.put(('done', self.process.wait()))
            except Exception as error:
                self.events.put(('text', f'\n执行失败：{error}\n'))
                self.events.put(('done', -1))
        threading.Thread(target=work, daemon=True).start()

    def login_done(self):
        if self.process and self.process.poll() is None and self.login_waiting:
            self.process.stdin.write('\n')
            self.process.stdin.flush()
            self.login_waiting = False

    def poll(self):
        chunk = ''
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == 'text':
                chunk += value
            else:
                self.busy = False
                self.login_waiting = False
                self.status.set('任务完成；请查看输出及实际 ERP 状态' if value == 0 else '任务停止；请查看错误，核对已有草稿，避免重复提交')
                if value != 0:
                    self.after_idle(lambda: messagebox.showerror('需要处理的问题', '当前任务遇到错误，窗口将保持打开。\n请核对下方日志；如涉及保存或发布，先核对 ERP 实际状态，再继续。\n\n' + self.output_tail[-1200:]))
        if chunk:
            self.output_tail = (self.output_tail + chunk)[-5000:]
            if 'BRAIN_PROBLEM' in self.output_tail:
                detail = self.output_tail.split('BRAIN_PROBLEM', 1)[1]
                if '\n' in detail:
                    message = detail.split('\n', 1)[0]
                    self.status.set('正在自动诊断页面定位问题；执行器保持连接')
                    if not any(word in message for word in ('定位', 'locator.', 'Timeout')):
                        self.after_idle(lambda m=message: messagebox.showwarning('需要你处理的问题', m + '\n\n执行器保持连接。'))
                    self.output_tail = self.output_tail.split('BRAIN_PROBLEM', 1)[0] + detail.split('\n', 1)[1]
            self.log.configure(state='normal')
            self.log.insert('end', chunk)
            self.log.see('end')
            if '然后按回车开始执行：' in self.output_tail and self.busy:
                self.login_waiting = True
                self.output_tail = self.output_tail.replace('然后按回车开始执行：', '[等待用户确认登录]')
            self.log.configure(state='disabled')
        self.after(100, self.poll)

    def close(self):
        if self.busy:
            if not messagebox.askyesno('停止当前批次', '正在执行。关闭后需人工核对 ERP 中的草稿和提交状态，再决定是否继续。确定关闭？'):
                return
            if self.process and self.process.poll() is None:
                self.process.terminate()
        self.destroy()


if __name__ == '__main__':
    if '--ego-check-output' in sys.argv:
        target = Path(sys.argv[sys.argv.index('--ego-check-output') + 1])
        try:
            config = browser_config()
            with ego_space(config['task_space_id']):
                result = subprocess.run(ego_connection_command(config=config), capture_output=True,
                                        text=True, encoding='utf-8', timeout=30,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
            state = parse_connection_result(result)
            report = {'connected': True, 'browser': state['browser'],
                      'spaceId': state['spaceId'],
                      'tabs': [{'label': t.get('label'), 'title': t.get('title')} for t in state['tabs']]}
        except Exception as error:
            report = {'connected': False, 'error': str(error)}
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        raise SystemExit(0 if report['connected'] else 1)
    app = Publisher()
    if '--folder' in sys.argv:
        app.material_folder = sys.argv[sys.argv.index('--folder') + 1]
    if '--brain' in sys.argv:
        app.after(300, app.connect_brain)
    app.mainloop()
