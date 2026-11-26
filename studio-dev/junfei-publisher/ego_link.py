"""Desktop connection to the existing Ego space using the project's live config."""
import json
import os
import hashlib
from pathlib import Path
import sys
import uuid
import re
import runpy
from urllib.parse import urlsplit


def project_root():
    configured = os.environ.get('JUNFEI_PROJECT_ROOT', '').strip()
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute() or not (root / 'automation/config.json').is_file():
            raise ValueError('JUNFEI_PROJECT_ROOT 必须指向包含 automation/config.json 的完整项目绝对目录')
        return root.resolve()
    if getattr(sys, 'frozen', False):
        base = Path(sys._MEIPASS)
        pointer = base / 'project-root.txt'
        if pointer.is_file():
            return Path(pointer.read_text(encoding='utf-8').strip())
        for candidate in (base / 'portable-runtime', base):
            if (candidate / 'automation/config.json').is_file():
                return candidate
        raise ValueError('Portable project configuration was not found')
    return Path(__file__).resolve().parent.parent


def module_path(name):
    if os.environ.get('JUNFEI_PROJECT_ROOT', '').strip():
        return project_root() / 'junfei-publisher' / name
    if getattr(sys, 'frozen', False):
        nested = project_root() / 'junfei-publisher' / name
        if nested.is_file():
            return nested
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / name


def browser_config():
    return json.loads((project_root() / 'automation/config.json').read_text(encoding='utf-8'))['browser']


def ego_space(space_id):
    script = project_root() / 'automation/browser_space_lock.py'
    return runpy.run_path(str(script))['active_space'](space_id)


def ego_connection_command(*, config=None):
    config = browser_config() if config is None else config
    if config.get('engine') != 'ego-lite':
        raise ValueError('项目专属浏览器必须配置为ego-lite')
    space = config.get('task_space_id')
    if type(space) is not int or space < 0:
        raise ValueError('缺少已确认的ego工作区ID')
    cli = Path(config['cli'])
    if not cli.is_file():
        raise ValueError('未找到ego运行时，不会回退Edge')
    directory = Path(config['script_directory'])
    if not directory.is_absolute():
        raise ValueError('ego脚本目录必须为绝对路径')
    directory.mkdir(parents=True, exist_ok=True)
    entry = directory / ('desktop-connect-' + uuid.uuid4().hex + '.mjs')
    entry.write_text(f'const task = await taskSpace({space});\n'
                     'console.log(JSON.stringify({browser:"ego-lite",spaceId:task.spaceId,'
                     'tabs:(await task.tabs()).map(t=>({label:t.label,title:t.title,url:t.url}))},null,2));\n',encoding='utf-8')
    return [str(cli), 'nodejs', '-e', f"await import('{entry.as_uri()}')"]


def require_legacy_browser():
    config = browser_config()
    if config.get('engine') == 'ego-lite' or config.get('fallback_to_edge') is not True:
        raise ValueError('此按钮的页面执行器尚未迁移至ego。请由智能体在ego专属工作区继续；不会打开Edge。')


def parse_connection_result(result):
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'ego连接失败')
    # The Windows GUI runtime directs console.log to stderr when there is no
    # parent console. Accept only a complete, correctly shaped JSON response.
    try:
        state = json.loads(result.stdout.strip() or result.stderr.strip())
    except (json.JSONDecodeError, AttributeError):
        raise ValueError('ego未返回完整工作区状态') from None
    config = browser_config()
    if (not isinstance(state, dict) or state.get('browser') != 'ego-lite' or
            state.get('spaceId') != config['task_space_id'] or
            not isinstance(state.get('tabs'), list)):
        raise ValueError('ego连接结果与指定工作区不一致')
    return state


def ego_draft_command(request_path, reconciliation_path=None, *, config=None):
    """Prepare one copy operation; the JS adapter owns field/page validation."""
    request = json.loads(Path(request_path).read_text(encoding='utf-8-sig'))
    if (not isinstance(request, dict) or not isinstance(request.get('patch'), dict)
            or not request['patch']
            or not set(request['patch']) <= {'title','highlights','bullets','description','terms','brand','manufacturer'}):
        raise ValueError('ego草稿入口仅支持文案及Generic品牌/制造商')
    if ({'brand','manufacturer'} & set(request['patch']) and
            (request['patch'].get('brand') != 'Generic' or request['patch'].get('manufacturer') != 'Generic')):
        raise ValueError('品牌及制造商仅允许同时设为Generic')
    # Identity + intended write, independent of filename/evidence/restarts.
    identity = {key: request.get(key) for key in ('draftId','store','erpSku','patch')}
    digest = hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    config = browser_config() if config is None else config
    if config.get('engine') != 'ego-lite' or type(config.get('task_space_id')) is not int:
        raise ValueError('缺少已配置的ego专属工作区')
    cli = Path(config['cli'])
    if not cli.is_file():
        raise ValueError('未找到ego运行时，不会回退Edge')
    records = project_root() / 'automation/data/ego-draft-executions'
    records.mkdir(parents=True,exist_ok=True)
    receipt = records / (digest + '.json')
    recovery = None
    if receipt.exists():
        if not reconciliation_path:
            raise ValueError(f'此草稿修改已有记录，先核对结果，不重复保存：{receipt}')
        previous_raw = receipt.read_bytes()
        previous = json.loads(previous_raw)
        reconciliation = json.loads(Path(reconciliation_path).read_text(encoding='utf-8'))
        if (previous.get('status') != 'stopped_before_save' or previous.get('attempt', 1) != 1
                or reconciliation.get('outcome') != 'confirmed_no_save'
                or not isinstance(reconciliation.get('persisted'), dict)
                or not reconciliation.get('source')
                or any(previous.get(k) != request.get(k) for k in ('draftId','store','erpSku','patch'))
                or any(reconciliation.get(k) != request.get(k) for k in ('draftId','store','erpSku'))):
            raise ValueError('仅允许已核实未保存的首次停止操作继续；未知/已保存结果不可重发')
        recovery = {'previousSha256': hashlib.sha256(previous_raw).hexdigest(),
                    'reconciliation': reconciliation, 'path': str(Path(reconciliation_path).resolve())}
    elif reconciliation_path:
        raise ValueError('没有原操作记录，不能作为恢复操作')
    directory = Path(config['script_directory'])
    if not directory.is_absolute():
        raise ValueError('ego脚本目录必须为绝对路径')
    directory.mkdir(parents=True,exist_ok=True)
    module = module_path('ego-existing-draft.mjs')
    entry = directory / ('desktop-draft-' + uuid.uuid4().hex + '.mjs')
    code = f'''const fs=await import('node:fs/promises');
const {{validateRequest,inspectDraft,updateEgoDraft}}=await import({json.dumps(module.as_uri())});
const request={json.dumps(request,ensure_ascii=False)};
validateRequest(request);
const path={json.dumps(str(receipt))};
const recovery={json.dumps(recovery,ensure_ascii=False)};
const ledger={{
 create:async record=>{{
  if(recovery){{
   const lock=await fs.open(path+'.reconcile-lock','wx');
   try{{
    const raw=await fs.readFile(path);const crypto=await import('node:crypto');
    if(crypto.createHash('sha256').update(raw).digest('hex')!==recovery.previousSha256)throw Error('原操作记录已变化，停止恢复');
    const previous=JSON.parse(raw.toString('utf8'));
    if(previous.status!=='stopped_before_save'||(previous.attempt||1)!==1)throw Error('原操作不可恢复');
    record.attempt=2;record.previousAttempts=[previous];record.reconciliation={{path:recovery.path,...recovery.reconciliation}};
    await ledger.write(record);
   }}finally{{await lock.close();await fs.unlink(path+'.reconcile-lock');}}
  }}else{{const file=await fs.open(path,'wx');try{{await file.writeFile(JSON.stringify(record,null,2),'utf8');await file.sync();}}finally{{await file.close();}}}}
 }},
 write:async record=>{{const temp=path+'.tmp';const file=await fs.open(temp,'w');try{{await file.writeFile(JSON.stringify(record,null,2),'utf8');await file.sync();}}finally{{await file.close();}}await fs.rename(temp,path);}}
}};
const task=await taskSpace({config['task_space_id']});
if(recovery){{
 const actual=await inspectDraft(task.page({json.dumps(config['pages']['erp'])}),request);
 for(const key of ['copy','price','quantity','shippingTemplate','handlingDays'])if(JSON.stringify(actual[key])!==JSON.stringify(recovery.reconciliation.persisted[key]))throw Error('草稿与未保存对账结果不同，停止恢复: '+key);
}}
const record=await updateEgoDraft(task.page({json.dumps(config['pages']['erp'])}),request,ledger);
console.log(JSON.stringify({{status:record.status,draftId:record.draftId,store:record.store,erpSku:record.erpSku,published:false,receipt:path}},null,2));
'''
    entry.write_text(code,encoding='utf-8')
    return [str(cli),'nodejs','-e',f"await import('{entry.as_uri()}')"]


def ego_image_command(request_path, *, save_library=False):
    """Prepare journaled image transport; does not launch or take over a browser.

    Saving remains an explicitly selected, uncalibrated library-only operation.
    It never assigns variant images or declares a product ready/published.
    """
    request = json.loads(Path(request_path).read_text(encoding='utf-8-sig'))
    if (not isinstance(request, dict) or set(request) != {'draftId', 'store', 'erpSku', 'assets'}
            or not isinstance(request.get('draftId'), str) or not request['draftId'].isdigit()
            or any(not isinstance(request.get(k), str) or not request[k].strip() for k in ('store', 'erpSku'))
            or type(save_library) is not bool):
        raise ValueError('图片入口需要明确同一草稿ID、店铺、ERP SKU及图片，不能附加其他写入')
    assets = request['assets']
    if not isinstance(assets, list) or not 1 <= len(assets) <= 9:
        raise ValueError('图片必须为1至9张')
    hashes = []
    for a in assets:
        if (not isinstance(a, dict) or not isinstance(a.get('path'), str)
                or not Path(a['path']).is_absolute() or not Path(a['path']).is_file()
                or not re.fullmatch(r'[a-f0-9]{64}', a.get('sha256', ''))
                or a.get('purpose') not in ('calibration_only', 'reviewed_product_image')):
            raise ValueError('图片路径、哈希及用途不完整')
        user_default = False
        try:
            u = urlsplit(a.get('sourceUrl', ''))
            user_default = (a.get('rightsBasis') == 'user_default_supplier_use_not_supplier_license'
                and a.get('noUnauthorizedMarks') is True and u.scheme == 'https'
                and u.hostname == 'detail.1688.com' and u.port in (None, 443)
                and not u.username and not u.password and bool(re.fullmatch(r'/offer/\d+\.html', u.path)))
        except (ValueError, TypeError):
            pass
        if a['purpose'] == 'reviewed_product_image' and (a.get('reviewed') is not True
                or not (a.get('rightsVerified') is True or user_default) or not isinstance(a.get('evidence'), str)
                or not a['evidence'].strip()):
            raise ValueError('商品图片缺少审核和权利依据')
        if save_library and a['purpose'] != 'reviewed_product_image':
            raise ValueError('校准图不能保存为商品图库')
        hashes.append(a['sha256'])
    if len(set(hashes)) != len(hashes):
        raise ValueError('图片重复')
    config = browser_config()
    if (config.get('engine') != 'ego-lite' or type(config.get('task_space_id')) is not int
            or config['task_space_id'] < 0 or not Path(config.get('cli', '')).is_file()):
        raise ValueError('缺少已配置的ego专属工作区及运行时，不回退Edge')
    directory = Path(config['script_directory'])
    if not directory.is_absolute():
        raise ValueError('ego脚本目录必须为绝对路径')
    directory.mkdir(parents=True, exist_ok=True)
    # Filename, evidence, purpose and local path do not create another upload.
    identity = {k: request[k] for k in ('draftId', 'store', 'erpSku')}
    identity['assetHashes'] = sorted(hashes)
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    records = project_root() / 'automation/data/ego-image-executions'
    records.mkdir(parents=True, exist_ok=True)
    stage_path = records / (digest + '-stage.json')
    save_path = records / (digest + '-save.json')
    for other in records.glob('*-stage.json'):
        if other == stage_path:
            continue
        prior = json.loads(other.read_text(encoding='utf-8'))
        if (all(prior.get(k) == request[k] for k in ('draftId', 'store', 'erpSku'))
                and set(hashes) & {a.get('sha256') for a in prior.get('assets', [])}):
            raise ValueError(f'同一草稿的图片已有其他批次回执，先核对原上传，不拆分重传：{other}')
    if save_path.exists():
        raise ValueError(f'图片保存已有回执，先核对，不重复保存：{save_path}')
    if stage_path.exists():
        previous = json.loads(stage_path.read_text(encoding='utf-8'))
        if previous.get('status') != 'image_staged_not_saved':
            raise ValueError(f'上传原结果需核对，不重传：{stage_path}')
        if not save_library:
            raise ValueError(f'图片已经暂存，不重传：{stage_path}')
        if (any(previous.get(k) != request[k] for k in ('draftId', 'store', 'erpSku'))
                or [a.get('sha256') for a in previous.get('assets', [])] != hashes
                or any(a.get('purpose') != 'reviewed_product_image' for a in previous.get('assets', []))):
            raise ValueError('原暂存回执与商品图片不匹配，不能转换校准用途或重传')
    module = module_path('ego-draft-images.mjs')
    entry = directory / ('desktop-images-' + uuid.uuid4().hex + '.mjs')
    code = f'''const fs=await import('node:fs/promises');
const {{stageImageLibrary,saveStagedImageLibrary,validateAssets}}=await import({json.dumps(module.as_uri())});
const request={json.dumps(request,ensure_ascii=False)};
const saveLibrary={json.dumps(save_library)};
await validateAssets(request.assets);
const stagePath={json.dumps(str(stage_path))},savePath={json.dumps(str(save_path))};
const ledger=path=>({{
 create:async record=>{{const f=await fs.open(path,'wx');try{{await f.writeFile(JSON.stringify(record,null,2),'utf8');await f.sync();}}finally{{await f.close();}}}},
 write:async record=>{{const tmp=path+'.tmp';const f=await fs.open(tmp,'w');try{{await f.writeFile(JSON.stringify(record,null,2),'utf8');await f.sync();}}finally{{await f.close();}}await fs.rename(tmp,path);}}
}});
const task=await taskSpace({config['task_space_id']});
const page=task.page({json.dumps(config['pages']['erp'])});
const lockPath={json.dumps(str(project_root() / 'automation/data/ego-workbench.lock'))};
const lock=await fs.open(lockPath,'wx');
await lock.writeFile(JSON.stringify({{tool:'ego-image-library',draftId:request.draftId,stagePath,savePath,state:'started'}}),'utf8');
await lock.sync();
let completed=false;
try{{
 for(const name of await fs.readdir({json.dumps(str(records))})){{
  if(!name.endsWith('-stage.json'))continue;
  const other={json.dumps(str(records))}+'/'+name;
  if(name==={json.dumps(stage_path.name)})continue;
  const prior=JSON.parse(await fs.readFile(other,'utf8'));
  if(['draftId','store','erpSku'].every(k=>prior[k]===request[k])&&
   prior.assets?.some(a=>request.assets.some(b=>a.sha256===b.sha256)))throw Error('同一草稿图片已有其他上传批次，先核对，不拆分重传');
 }}
let staged;
try{{staged=JSON.parse(await fs.readFile(stagePath,'utf8'));}}catch(e){{if(e.code!=='ENOENT')throw e;}}
if(staged){{
 if(!saveLibrary||staged.status!=='image_staged_not_saved'||
  ['draftId','store','erpSku'].some(k=>staged[k]!==request[k])||
  JSON.stringify(staged.assets.map(a=>a.sha256))!==JSON.stringify(request.assets.map(a=>a.sha256))||
  staged.assets.some(a=>a.purpose!=='reviewed_product_image'))throw Error('原图片操作必须先核对，不重传或改变用途');
}}else staged=await stageImageLibrary(page,request,request.assets,ledger(stagePath));
const result=saveLibrary?await saveStagedImageLibrary(page,request,staged,ledger(savePath)):staged;
console.log(JSON.stringify({{status:result.status,stageReceipt:stagePath,saveReceipt:saveLibrary?savePath:null,
 saved:result.saved,published:false,variantImagesVerified:false,productionImageCompleted:false}},null,2));
completed=true;
}}finally{{await lock.close();if(completed)await fs.unlink(lockPath);}}
'''
    entry.write_text(code, encoding='utf-8')
    return [str(config['cli']), 'nodejs', '-e', f"await import('{entry.as_uri()}')"]
