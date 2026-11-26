"""Account-owned, versioned name mappings for a Codex-led store rotation.

Saving a setup never resolves identities, starts Codex or authorizes publishing.
"""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
import app_paths

class Conflict(ValueError):
    pass

def path():
    return app_paths.DATA/'store-rotation-setup.sqlite3'

def owner(user):
    if type(user)is not int or user<=0 or (app_paths.WORKER_USER and user!=app_paths.WORKER_USER):
        raise ValueError('任务配置不属于当前工作空间')
    return user

def read(user,setup_id=None):
    owner(user)
    if setup_id and not re.fullmatch(r'[a-f0-9]{32}',setup_id):raise ValueError('配置编号无效')
    if not path().is_file():return {'revision':0,'setup':None}
    with closing(sqlite3.connect(path().resolve().as_uri()+'?mode=ro',uri=True))as db:
        if setup_id:
            found=db.execute("SELECT body FROM setup_versions WHERE owner=? AND json_extract(body,'$.id')=?",(user,setup_id)).fetchone()
            if not found:raise ValueError('当前账号没有这份任务配置')
            setup=json.loads(found[0]);return {'revision':setup['revision'],'setup':setup}
        row=db.execute('SELECT revision,body FROM current_setup WHERE owner=?',(user,)).fetchone()
    return {'revision':row[0],'setup':json.loads(row[1])}if row else {'revision':0,'setup':None}

def name(value,label,required=True):
    if value=='' and not required:return ''
    if (not isinstance(value,str)or not value.strip()or len(value)>160
            or any(ord(char)<32 for char in value)):
        raise ValueError(label+'须为1–160字的实际名称')
    return value.strip()

def normalize(data):
    if not isinstance(data,dict)or set(data)!={'requestId','revision','markets','targets'}:
        raise ValueError('任务配置字段无效')
    if not re.fullmatch(r'[A-Za-z0-9-]{20,64}',str(data['requestId'])):
        raise ValueError('缺少有效请求编号')
    if type(data['revision'])is not int or data['revision']<0:raise ValueError('配置版本无效')
    markets=data['markets']
    if markets not in (['US'],['US','UK']):raise ValueError('请选择美国或美国＋英国')
    rows=data['targets']
    if not isinstance(rows,list)or not 1<=len(rows)<=20:raise ValueError('请填写1–20家店铺')
    targets=[];seen=set()
    for row in rows:
        if not isinstance(row,dict)or set(row)!={'ziniaoStoreName','erpAccount','usStoreName','ukStoreName'}:
            raise ValueError('店铺映射字段无效')
        clean={k:name(row[k],label,required=(k!='ukStoreName'or'UK'in markets))for k,label in
            [('ziniaoStoreName','紫鸟店铺名称'),('erpAccount','ERP账号'),('usStoreName','ERP美国店铺名称'),('ukStoreName','ERP英国店铺名称')]}
        if 'UK'not in markets:clean['ukStoreName']=''
        if 'UK'in markets and clean['usStoreName']==clean['ukStoreName']:raise ValueError('美国与英国ERP店铺名称不能相同')
        # A destination cannot be configured twice under two purple aliases.
        for market,key in [('US','usStoreName'),('UK','ukStoreName')]:
            if market not in markets:continue
            identity=(clean['erpAccount'],market,clean[key])
            if identity in seen:raise ValueError('同一ERP账号的相同目标店铺不能重复轮转')
            seen.add(identity)
        targets.append(clean)
    return {'markets':markets,'targets':targets}

def instruction(setup):
    lines=[f"读取当前工作台账号{setup['accountId']}的店铺轮转配置{setup['id']}（版本{setup['revision']}）。",
           f"读取入口：GET /api/agent/rotation-setup?setupId={setup['id']}",
           '从任务中心读取轮转检查点与原暂停状态；核对紫鸟及ERP身份，正式发布单独确认。',
           '按以下顺序轮转，每店每轮3款新商品，所有配置目标站点真实发布中才计入一款；失败或未知沿原ID对账。']
    for index,row in enumerate(setup['targets'],1):
        lines.append(f"{index}. 紫鸟：{row['ziniaoStoreName']}；ERP账号：{row['erpAccount']}；美国店铺：{row['usStoreName']}"+
                     (f"；英国店铺：{row['ukStoreName']}"if'UK'in setup['markets']else''))
    lines+=['名称为用户填写，尚未核实外部ID；不可当成已登录、已绑定或发布授权。',
            '按当前用户明确授权的范围执行；已有暂停不得仅因本配置自行解除。']
    return '\n'.join(lines)

def save(user,data):
    owner(user);clean=normalize(data)
    signature=hashlib.sha256(json.dumps({'revision':data['revision'],**clean},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    with closing(sqlite3.connect(path(),timeout=20))as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS setup_versions(owner INTEGER,request_id TEXT,signature TEXT,body TEXT,PRIMARY KEY(owner,request_id))')
        db.execute('CREATE TABLE IF NOT EXISTS current_setup(owner INTEGER PRIMARY KEY,revision INTEGER,body TEXT)')
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT signature,body FROM setup_versions WHERE owner=? AND request_id=?',(user,data['requestId'])).fetchone()
        if old:
            if old[0]!=signature:raise Conflict('请求编号已用于其他配置，请沿原请求核对')
            setup=json.loads(old[1]);current=db.execute('SELECT revision FROM current_setup WHERE owner=?',(user,)).fetchone()[0]
            return {'revision':setup['revision'],'currentRevision':current,'setup':setup,'replayed':True}
        current=db.execute('SELECT revision FROM current_setup WHERE owner=?',(user,)).fetchone()
        revision=current[0]if current else 0
        if data['revision']!=revision:raise Conflict('配置已在其他页面更新，请重新打开后核对；未覆盖原配置')
        setup={'id':uuid.uuid4().hex,'accountId':user,'revision':revision+1,'createdAt':time.time(),
               'productsPerStoreTurn':3,'productAssignment':'new_products_only',**clean,
               'identityStatus':'unverified_names','executionStatus':'awaiting_codex',
               'publishAuthorized':False,'codexStarted':False}
        setup['codexInstruction']=instruction(setup)
        raw=json.dumps(setup,ensure_ascii=False)
        db.execute('INSERT INTO setup_versions VALUES(?,?,?,?)',(user,data['requestId'],signature,raw))
        db.execute('INSERT OR REPLACE INTO current_setup VALUES(?,?,?)',(user,setup['revision'],raw))
    return {'revision':setup['revision'],'currentRevision':setup['revision'],'setup':setup,'replayed':False}
