"""Account-owned finished-page collection, with one durable draft per destination.

No ZIP is read or built. Credentials stay in the ERP browser/plugin. An unknown
external result is reconciled by its existing draft ID, never by creating again.
"""
import hashlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tempfile
import uuid
import os
from contextlib import contextmanager
from pathlib import Path
from PIL import Image
import app_paths
import studio_bridge
import generated_gallery
import colour_sets
import colour_master
import variant_colours
import delivery_export
import erp_collection_inputs as inputs
import image_composition
from ivyea_pipeline import required_size_chart

ROOT=app_paths.DATA/'erp-collection'
PROJECT=Path(__file__).resolve().parent.parent
FIELDS=('title','highlights','bullets','description','terms')
ACTIVE=set()
LOCK=threading.Lock()
RUNTIME=uuid.uuid4().hex


@contextmanager
def connect():
    ROOT.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(ROOT/'jobs.sqlite3',timeout=30)
    c.row_factory=sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
        product_id TEXT NOT NULL,destination_id TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,
        UNIQUE(account_id,product_id,destination_id))''')
    try:yield c
    finally:c.close()


def destinations(user):
    path=app_paths.DATA/'erp-collection-settings.json'
    if not path.is_file():return []
    data=json.loads(path.read_text(encoding='utf-8'))
    return [d for d in data.get('destinations',[]) if d.get('accountId')==user and d.get('enabled') is True]


def product_for(workspace,pid):
    found=[p for p in workspace.get('products',[]) if p.get('id')==pid]
    if len(found)!=1:raise ValueError('当前账号产品不存在或编号重复')
    return found[0]


def public(job):
    job=dict(job)
    if job.get('status') in ('queued','running') and job.get('id') not in ACTIVE and not process_alive(job.get('browserPid')):
        if job.get('runOwner')!=RUNTIME or not process_alive(job.get('ownerPid')):
            job.update(status='needs_reconciliation',phase='执行已中断，点击继续从原回执对账')
    if job.get('status') in ('queued','running') and re.fullmatch(r'[a-f0-9]{32}',str(job.get('id',''))):
        receipt=ROOT/job['id']/'receipt.json'
        if receipt.is_file():
            try:
                r=json.loads(receipt.read_text(encoding='utf-8'))
                if r.get('accountId')==job.get('accountId') and r.get('productId')==job.get('productId'):
                    job.update(phase=r.get('phase') or job['phase'],stages=r.get('stages',{}),draftId=r.get('draftId'),draftUrl=r.get('draftUrl'),collectionId=r.get('collection',{}).get('id'))
            except (ValueError,OSError):pass
    return {k:job.get(k) for k in ('id','productId','destinationId','store','marketplace','status','phase','engine',
        'error','createdAt','updatedAt','draftId','draftUrl','collectionId','collectionUrl','nativeJoined',
        'handoffMode','parentSku','variantCount','imageCount','verifiedAt','published','reused','warnings','stages') if k in job}


def verified_result(job,record,snapshot):
    collection=record.get('collection',{})
    if (record.get('status')!='saved_reopened_verified' or record.get('snapshotSha256')!=snapshot.get('sha256')
            or record.get('published') is not False or collection.get('status')!='list_verified'
            or not re.fullmatch(r'\d+',str(collection.get('id','')))
            or collection.get('row',{}).get('sourceMatch') is not True
            or len(record.get('variantChecks',[]))!=len(snapshot['goodsItems'])):
        raise ValueError('采集列表与草稿回读证据未齐全，不能标记完成')
    joined='amazon' in collection['row'].get('join_platform',[])
    binding=snapshot.get('destination',{}).get('nativeCollectionId')
    transfer=record.get('nativeTransfer',{})
    identity=transfer.get('identityProof',{})
    reconciled=(transfer.get('reconciled') is True and identity.get('sourceMatch') is True
                and identity.get('collectionId')==collection['id'] and str(identity.get('id'))==str(record['draftId'])
                and set(identity.get('skus',[]))=={v['sku'] for v in snapshot['goodsItems']})
    native=(joined and transfer.get('status')=='native_join_verified'
            and transfer.get('collectionId')==collection['id'] and str(transfer.get('draftId'))==str(record['draftId'])
            and ((transfer.get('sent')==1 and transfer.get('response',{}).get('body',{}).get('code')==0) or reconciled))
    if snapshot.get('schema')=='finished-page-collection-v2' and not native:raise ValueError('必须完成ERP原生转入和唯一草稿核对')
    if binding and (str(binding)!=collection['id'] or not native):
        raise ValueError('ERP原生转入身份或加入位置尚未核对，不能标记完成')
    if binding or snapshot.get('schema')=='finished-page-collection-v2':
        images=record.get('imageChecks',[])
        if (len(images)!=len(snapshot.get('assets',[])) or not images
                or {i.get('sha256') for i in images}!={a.get('sha256') for a in snapshot['assets']}
                or any(i.get('remote',{}).get('sha256')!=i.get('sha256') or not i.get('remote',{}).get('bytes',0)>0 for i in images)):
            raise ValueError('成品图片当前可用性与文件哈希未核对，不能标记完成')
    job.update(collectionId=collection['id'],collectionUrl='https://www.junfeikuajing.com/#/product/collect',
        draftId=record['draftId'],draftUrl=record['draftUrl'],nativeJoined=native,
        handoffMode='native_transfer' if native else 'reuse_existing_draft',status='verified',
        phase='ERP原生采集与目标店铺草稿回读通过' if native else '采集记录与既有草稿回读通过',verifiedAt=time.time(),warnings=record.get('warnings',[]),stages=record.get('stages',{}))


def owned(user,jid):
    with connect() as c:
        row=c.execute('SELECT body FROM jobs WHERE id=? AND account_id=?',(jid,user)).fetchone()
    if not row:raise ValueError('当前账号找不到此采集记录')
    return json.loads(row[0])


def persist(job):
    job['updatedAt']=time.time()
    with connect() as c,c:
        c.execute('UPDATE jobs SET status=?,body=? WHERE id=? AND account_id=?',
            (job['status'],json.dumps(job,ensure_ascii=False),job['id'],job['accountId']))


def eligible_variant_rows(product):
    """Use original SKU mappings, restricted to the explicit production ASINs.

    The audit/export helper intentionally includes excluded observations. Scope
    its input here, before price validation, without changing those records.
    An explicitly empty eligible list must stay empty.
    """
    source=product.get('sourceImport') or {}
    amazon=source.get('amazon') or {}
    observed=amazon.get('variants')
    eligible=amazon.get('listingEligibleVariants')
    scoped=product
    if isinstance(observed,list) and isinstance(eligible,list):
        asins={row.get('asin') for row in eligible if isinstance(row,dict) and row.get('asin')}
        scoped={**product,'sourceImport':{**source,'amazon':{**amazon,'variants':[
            row for row in observed if isinstance(row,dict) and row.get('asin') in asins]}}}
    return list(delivery_export.variant_rows(scoped,product.get('sku','')))


def view(user,workspace,pid):
    p=product_for(workspace,pid)
    ds=destinations(user)
    with connect() as c:
        rows=c.execute('SELECT body FROM jobs WHERE account_id=? AND product_id=?',(user,pid)).fetchall()
    issues=[]
    variant_rows=eligible_variant_rows(p)
    from creative_skills import select
    import image_engine_delivery
    if select(p)['category']=='apparel' and image_engine_delivery.selection(p['id']) is None:
        gallery=studio_bridge.get_view(p).get('sets',{}).get('gallery',{})
        try:
            required_size_chart(p,gallery,(row[3] for row in variant_rows))
        except ValueError as error:
            issues.extend({'destinationId':d['id'],'message':str(error)} for d in ds)
    for d in ds:
        try:
            prior=next((json.loads(r[0]) for r in rows if json.loads(r[0])['destinationId']==d['id']),None)
            old=ROOT/prior['id']/'collection.json' if prior else None
            snapshot=json.loads(old.read_text(encoding='utf-8')) if old and old.is_file() else None
            commerce=inputs.commerce(user,p,d,variant_rows,snapshot)
            inputs.transport(user,p,commerce)
        except (ValueError,KeyError,OSError) as error:issues.append({'destinationId':d['id'],'message':str(error)})
    return {'destinations':[{k:d[k] for k in ('id','name','store','marketplace','quantity','handlingDays') if k in d} for d in ds],
            'jobs':[public(json.loads(r[0])) for r in rows], 'parentSku':p.get('sku'),
            'variantCount':len(p.get('sourceImport',{}).get('amazon',{}).get('listingEligibleVariants'))
                if isinstance(p.get('sourceImport',{}).get('amazon',{}).get('listingEligibleVariants'),list)
                else len(p.get('sourceImport',{}).get('amazon',{}).get('variants') or []),
            'mode':'draft','exportRequired':False,'issues':issues,'workflowVersion':2}


def finished_snapshot(user,p,listing,destination,folder,prior=None):
    """Read selected live image records directly, preserving review provenance."""
    if not p.get('sku') or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',p['sku']):raise ValueError('产品缺少有效父SKU')
    variant_rows=eligible_variant_rows(p)
    destination=inputs.commerce(user,p,destination,variant_rows,prior)
    if any(not listing.get(k) for k in ('title','bullets','description','terms')):raise ValueError('请先保存完整Listing文案')
    bullets=listing['bullets']
    if isinstance(bullets,str):bullets=[s.strip() for s in bullets.splitlines() if s.strip()]
    if len(bullets)!=5:raise ValueError('文案须有五条独立五点')
    for key,limit in (('title',75),('highlights',125),('description',2000)):
        if len(str(listing.get(key,'')))>limit:raise ValueError('钧飞字段 '+key+' 超过 '+str(limit)+' 字符，请先在Listing编辑器调整')
    if any(len(b)>500 for b in bullets) or len(str(listing.get('terms','')).encode('utf-8'))>249:
        raise ValueError('钧飞五点或搜索词超过字段限制，请先调整已保存文案')
    groups=variant_colours.catalog(p)
    if not groups or any(not g['ready'] for g in groups.values()):raise ValueError('颜色尺码与货源对应资料未完成')
    import image_engine_delivery
    selected = image_engine_delivery.galleries(p)
    plan = {}
    master = variant_colours.resolve(groups, p.get('masterColor'))
    if selected is not None:
        chart_hash = selected[master][6][1]['sha256']
        plan = {'images': [{'shot_type': 'detail' if i in (1, 2) else None} for i in range(7)]}
    else:
        view=studio_bridge.get_view(p);plan=view.get('sets',{}).get('gallery',{})
        chart_hash=required_size_chart(p,plan,(row[3] for row in variant_rows))
        master=variant_colours.resolve(groups,p.get('masterColor'))
        if not variant_colours.has_reference(groups.get(master),plan.get('product_source_url') or ((plan.get('images') or [{}])[0].get('product_source_url'))):
            raise ValueError('母版与本款真实颜色参考不匹配')
        selected={};project=plan.get('_projectId')
        generated=generated_gallery.export_images(p,project)
        if generated is not None:selected[master]=[(raw,info) for raw,info in generated]
        else:
            images=[]
            for slot,image in enumerate(plan.get('images',[])):
                if not image.get('_runId') or image.get('_status') not in ('approved','awaiting_manual') or image.get('render_qa',{}).get('ready') is False:
                    raise ValueError('母版图位未通过草稿检查')
                raw,mime=studio_bridge.result_image(image['_runId'])
                if chart_hash and slot==6 and hashlib.sha256(raw).hexdigest()!=chart_hash:
                    raise ValueError('待补尺码：第7张尺码图文件与校对记录不一致')
                images.append((raw,{'slot':slot,'mime':mime,'runId':image['_runId'],'humanReviewed':image.get('human_reviewed') is True,'status':image['_status']}))
            selected[master]=images
        for v in colour_sets.list_sets(p['id'])['sets']:
            if generated is not None and v.get('masterId')!=colour_master.selection_id(p):continue
            if generated is None and project and v.get('masterId')!=project:continue
            key=v.get('colorKey') or variant_colours.resolve(groups,v.get('color'))
            if key not in groups or key==master:continue
            if v.get('validated') is not True or not variant_colours.has_reference(groups[key],v.get('reference')):continue
            rows=[]
            for item in sorted(v.get('items',[]),key=lambda i:i['slot']):
                if not item.get('image') or item.get('status') not in ('approved','awaiting_manual') or (item.get('qc') or {}).get('pass') is not True:
                    raise ValueError('换色图位未通过草稿检查：'+groups[key]['color'])
                if chart_hash and item['slot']==6:
                    source=selected[master][6][1]
                    native_match=source.get('runId') and item.get('baseRun')==source['runId']
                    generated_match=(source.get('jobId') and item.get('sourceJobId')==source['jobId']
                                     and item.get('sourceImageIndex')==source['imageIndex'])
                    if not (native_match or generated_match):
                        raise ValueError('待补尺码：第7张尺码图与当前已校对母版未关联：'+groups[key]['color'])
                raw,mime=colour_sets.image_bytes(v['id'],item['slot'])
                rows.append((raw,{'slot':item['slot'],'mime':mime,'setId':v['id'],'humanReviewed':v.get('manual') is True and item['status']=='approved','status':item['status'],'warnings':(item.get('qc') or {}).get('warnings',[])}))
            if key in selected:raise ValueError('同色有多套成品，请先明确保留版本')
            selected[key]=rows
    if set(selected)!=set(groups):raise ValueError('拟交付颜色缺少完整成品套图')
    assets=[];by_colour={};folder.mkdir(parents=True,exist_ok=True)
    for key,rows in selected.items():
        if [info['slot'] for _,info in rows]!=list(range(7)):raise ValueError('每色必须按顺序保留完整7张成品图')
        indices=[]
        for raw,info in rows:
            sha=hashlib.sha256(raw).hexdigest()
            composition=image_composition.require(raw,slot=info['slot'],product=p,
                                                  detail=bool(info['slot']<len(plan.get('images',[])) and plan['images'][info['slot']].get('shot_type')=='detail'),
                                                  size_chart=bool(chart_hash and info['slot']==6))
            with Image.open(io.BytesIO(raw)) as im:
                width,height=im.size;fmt=im.format
                if fmt not in ('PNG','JPEG') or min(width,height)<1600 or len(raw)>=5*1024*1024:raise ValueError('成品图片需PNG/JPEG、至少1600像素且小于5MB')
                im.verify()
            name=sha+('.png' if fmt=='PNG' else '.jpg');file=folder/name
            if not file.exists():file.write_bytes(raw)
            indices.append(len(assets));assets.append({'path':str(file),'sha256':sha,'width':width,'height':height,'color':groups[key]['color'],**info,
                                                       'compositionCheck':composition})
        by_colour[key]=indices
    variants=[]
    for sku,color,english,size,source,status,asin in variant_rows:
        key=variant_colours.resolve(groups,color)
        if key not in by_colour:continue
        variants.append({'sku':sku,'color':color,'size':size,'sourceAsin':asin,'imageIndices':by_colour[key],
                         'price':destination['variantPrices'][sku],'quantity':str(destination['quantity'])})
    expected={(c['asin'],c['sku']) for g in groups.values() for c in g['children']}
    if (len(variants)!=sum(len(g['children']) for g in groups.values())
            or len({v['sku'] for v in variants})!=len(variants)
            or {(v['sourceAsin'],v['sku']) for v in variants}!=expected):
        raise ValueError('子SKU集合或颜色尺码映射不一致')
    goods={k:listing.get(k,'') for k in FIELDS};goods['bullets']=bullets
    goods.update(parentSku=p['sku'],sourceUrl=p.get('sourceUrl'),brand=p.get('brand') or 'Generic',manufacturer=p.get('manufacturer') or 'Generic')
    result={'schema':'finished-page-collection-v2','accountId':user,'productId':p['id'],'mode':'draft','goods':goods,'goodsItems':variants,'assets':assets,
            'store':destination['store'],'marketplace':destination['marketplace'],'masterColor':p.get('masterColor'),'handlingDays':str(destination['handlingDays']),
            'reviewScope':'draft_only','humanReviewed':all(a.get('humanReviewed') is True for a in assets),'exportRequired':False,
            'categoryHints':inputs.category_hints(p),'pricingBasis':destination['pricingBasis']}
    fingerprint={**result,'assets':[{k:v for k,v in a.items() if k!='path'} for a in assets]}
    result['sha256']=hashlib.sha256(json.dumps(fingerprint,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    result['destination']=inputs.transport(user,p,destination)|{'workbenchUrl':inputs.source_url(user,p['id'],destination)}
    return result


def review_packet(snapshot):
    """Expose only business fields, resolving each SKU's ordered local image identities."""
    variants={}
    for row in snapshot['goodsItems']:
        images=[{k:snapshot['assets'][i].get(k) for k in
                 ('slot','sha256','color','runId','jobId','setId','imageIndex','status','humanReviewed')}
                for i in row['imageIndices']]
        variants[row['sku']]={**{k:v for k,v in row.items() if k!='imageIndices'},'images':images}
    dest=snapshot['destination']
    return {'contentDigest':inputs.content_digest(snapshot),'goods':snapshot['goods'],'variants':variants,
            'store':snapshot['store'],'marketplace':snapshot['marketplace'],'handlingDays':snapshot['handlingDays'],
            'purchaseCny':dest.get('purchaseCny'),'currency':dest.get('currency') or
            inputs.MARKETS[snapshot['marketplace']][0], 'pricingSource':(snapshot.get('pricingBasis') or {}).get('source')}


def review_digest(packet):
    return hashlib.sha256(json.dumps(packet,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def preflight(user,product,listing,destination,prior):
    if prior and prior.get('draftId') and not (ROOT/prior['id']/'receipt.json').is_file():
        raise ValueError('缺少原草稿回执，请人工对账')
    old_file=ROOT/prior['id']/'collection.json' if prior else None
    original=json.loads(old_file.read_text(encoding='utf-8')) if old_file and old_file.is_file() else None
    with tempfile.TemporaryDirectory(prefix='junfei-erp-review-') as folder:
        current=finished_snapshot(user,product,listing,destination,Path(folder),original)
    if original and inputs.content_digest(current)!=inputs.content_digest(original):
        raise ValueError('当前成品与原采集快照不同，请核对原草稿')
    # A resumed worker uses the original packet. Check current price and artwork
    # identities as well as the business-content comparison used by older jobs.
    if original:
        original['destination']={**current['destination'],**original.get('destination',{})}
        frozen=review_packet(original)
        fresh=review_packet(current)
        if any(frozen[k]!=fresh[k] for k in ('purchaseCny','currency','variants')):
            raise ValueError('原草稿价格或图位证据与当前成品不同，请核对原回执')
        return frozen
    return review_packet(current)


def start(user,workspace,data,*,reviewed_digest=None,handoff=None):
    if not isinstance(data,dict) or set(data)-{'productId','destinationId','expectedListing'}:raise ValueError('采集参数无效')
    p=product_for(workspace,data.get('productId'))
    ds=[d for d in destinations(user) if d['id']==data.get('destinationId')]
    if len(ds)!=1:raise ValueError('请先配置并选择本账号的ERP草稿目标')
    d=ds[0];listing=workspace.get('copy',{}).get(p['id']+'-'+d['marketplace'],{})
    if data.get('expectedListing') is not None and (not isinstance(data['expectedListing'],dict) or any(data['expectedListing'].get(k,'')!=listing.get(k,'') for k in FIELDS)):raise ValueError('当前页面文案与已保存内容不同，请保存后再采集')
    launch=None
    with LOCK,connect() as c,c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT body FROM jobs WHERE account_id=? AND product_id=? AND destination_id=?',(user,p['id'],d['id'])).fetchone()
        if row:
            prior=json.loads(row[0]);receipt=ROOT/prior['id']/'receipt.json'
            live=public(prior)
            if live['status'] in ('queued','running'):
                if handoff:raise ValueError('原草稿任务已在执行，请核对原任务')
                return {**live,'reused':True}
            snapshot_file=ROOT/prior['id']/'collection.json'
            if snapshot_file.is_file():
                original=json.loads(snapshot_file.read_text(encoding='utf-8'))
                current=finished_snapshot(user,p,listing,d,ROOT/prior['id']/'media',original)
                if inputs.content_digest(current)!=inputs.content_digest(original):
                    raise ValueError('当前成品与原采集版本不同；已保留原记录，请先对账更新，不能复用旧成功回执')
            if reviewed_digest and (live['status']!='needs_reconciliation' or
                    review_digest(preflight(user,p,listing,d,live))!=reviewed_digest):
                raise ValueError('原草稿或审核版本已变化')
            if prior['status']=='verified' and prior.get('engine')=='native-v2':return {**public(prior),'reused':True}
            if prior.get('draftId') and not receipt.is_file():
                if handoff:raise ValueError('缺少原草稿回执，请人工对账')
                return {**live,'reused':True}
            job=prior;job.setdefault('history',[]).append({'error':job.pop('error',None),'resumedAt':time.time()})
            job.update(status='queued',phase='等待浏览器，从持久回执对账续跑',runOwner=RUNTIME,ownerPid=os.getpid(),engine='native-v2')
            c.execute('UPDATE jobs SET status=?,body=? WHERE id=?',('queued',json.dumps(job,ensure_ascii=False),job['id']))
        else:
            job={'id':uuid.uuid4().hex,'accountId':user,'productId':p['id'],'destinationId':d['id'],'store':d['store'],'marketplace':d['marketplace'],
                 'parentSku':p['sku'],'status':'queued','phase':'读取当前成品','createdAt':time.time(),'updatedAt':time.time(),'published':False,
                 'runOwner':RUNTIME,'ownerPid':os.getpid(),'engine':'native-v2'}
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(job['id'],user,p['id'],d['id'],job['status'],json.dumps(job,ensure_ascii=False)))
        if handoff:
            import codex_handoff
            codex_handoff.record(c,handoff,job['id'],'queued')
        if reviewed_digest:
            job['reviewedDigest']=reviewed_digest
            c.execute('UPDATE jobs SET body=? WHERE id=?',(json.dumps(job,ensure_ascii=False),job['id']))
        ACTIVE.add(job['id']);launch=(job,p,listing,d)
    # Commit the identity before starting a worker or any external operation.
    threading.Thread(target=execute,args=launch,daemon=True).start()
    return {**public(job),'reused':bool(row)}


def browser_preflight_failure(script,output,binding):
    """Prove this attempt stopped in a known guard before its business import.

    Error wording alone is not evidence: require the runner's immutable receipt,
    matching script hash/binding, and a stack frame on the actual bootstrap line.
    """
    try:
        script=Path(script).resolve(strict=True)
        digest=hashlib.sha256(script.read_bytes()).hexdigest()
        key=hashlib.sha256((str(script)+':'+digest).encode('utf-8')).hexdigest()
        receipt=PROJECT/'automation/data/ego-executions'/(key+'.json')
        record=json.loads(receipt.read_text(encoding='utf-8'))
        if (record.get('script')!=str(script) or record.get('sha256')!=digest
                or record.get('browserBinding')!=binding or record.get('exitCode') in (None,0)):
            return None
        entry=Path(record['entry']).resolve(strict=True)
        if not re.fullmatch(r'round-[a-f0-9]{32}\.mjs',entry.name):return None
        lines=entry.read_text(encoding='utf-8').splitlines()
        imported='await import('+json.dumps(script.as_uri())+');'
        import_line=lines.index(imported)+1
        text=output.decode('utf-8',errors='replace') if isinstance(output,bytes) else str(output)
        frames=re.findall(re.escape(entry.as_uri())+r':(\d+):\d+',text)
        known=('专属采集浏览器连接已变化，请重新连接','浏览器已交还或中断，请完成验证后继续',
               '专属工作区归属不符，停止操作','专属工作区名称不符，停止操作','专属ERP页面不唯一，请重新连接')
        for number in frames:
            line=int(number)
            if not 1<=line<import_line:continue
            source=lines[line-1]
            for message in known:
                if message in text and message in source and re.search(r'\bthrow\s+(?:new\s+)?Error\(',source):
                    return {'message':message,'entry':str(entry),'line':line,'executionReceipt':str(receipt)}
    except (OSError,ValueError,KeyError,TypeError):
        pass
    return None


def execute(job,p,listing,d):
    try:
        folder=ROOT/job['id'];folder.mkdir(parents=True,exist_ok=True)
        job['status']='running';persist(job)
        snapshot_file=folder/'collection.json'
        if snapshot_file.is_file():
            snapshot=json.loads(snapshot_file.read_text(encoding='utf-8'))
            # Add store-level transport settings to old receipts without changing
            # their original content hash, source URL, draft ID or image mapping.
            commerce=inputs.commerce(job['accountId'],p,d,eligible_variant_rows(p),snapshot)
            snapshot['destination']={**inputs.transport(job['accountId'],p,commerce),**snapshot.get('destination',{})}
        else:snapshot=finished_snapshot(job['accountId'],p,listing,d,folder/'media')
        if job.get('reviewedDigest'):
            if review_digest(review_packet(snapshot))!=job['reviewedDigest']:
                raise ValueError('草稿成品与已批准的逐项证据不同，请重新核对')
            for asset in snapshot['assets']:
                if hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest()!=asset['sha256']:
                    raise ValueError('原成品图片字节与审核记录不符')
        snapshot['destination'].update(destinationId=d['id'],workflowVersion=2,categoryHints=inputs.category_hints(p))
        snapshot_file.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
        job.update(variantCount=len(snapshot['goodsItems']),imageCount=len(snapshot['assets']),phase='等待专属浏览器');persist(job)
        receipt=folder/'receipt.json';script=folder/'collect.mjs'
        module=PROJECT/'junfei-publisher/ego-collection-workflow.mjs'
        script.write_text('const fs=await import("node:fs/promises");\n'+
            'const input=JSON.parse(await fs.readFile('+json.dumps(str(snapshot_file))+',"utf8"));\n'+
            'const {runCollectionWorkflow}=await import('+json.dumps(module.as_uri())+');\n'+
            'await runCollectionWorkflow(globalThis.amazonTask.page('+json.dumps(d['page'])+'),input,'+json.dumps(str(receipt))+');\n',encoding='utf-8')
        with browser_lease(d['browserBinding']):
            job['phase']='核对店铺和插件登录';persist(job)
            login=[sys.executable,'-X','utf8',str(PROJECT/'automation/ziniao_plugin.py'),'--store-id',str(d['storeId']),'--store-name',d['storeName'],
                   '--market',d['marketplace'],'--seller',d['seller'],'--account',d['erpAccount'],'--ensure-site','--receipt',str(folder/'login.json')]
            r=subprocess.run(login,capture_output=True,timeout=150,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if r.returncode:raise ValueError('店铺或插件登录未通过；完成登录后点击继续，保留原回执')
            command=[sys.executable,'-X','utf8',str(PROJECT/'automation/ego_browser.py'),'run',str(script),'--timeout','1200','--binding',d['browserBinding'],'--application-job']
            # Each resume gets a fresh local script name, while sharing the same
            # external receipt. The transport replay barrier remains enabled.
            attempt=folder/('run-'+uuid.uuid4().hex+'.mjs');attempt.write_bytes(script.read_bytes());command[command.index(str(script))]=str(attempt)
            job['phase']='查重、采集与原生草稿交接';persist(job)
            before_receipt=hashlib.sha256(receipt.read_bytes()).hexdigest() if receipt.is_file() else None
            proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            job['browserPid']=proc.pid;persist(job)
            try:stdout,stderr=proc.communicate(timeout=1220)
            except subprocess.TimeoutExpired:
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True)
                else:proc.kill()
                stdout,stderr=proc.communicate();raise ValueError('执行超时；保留原提交回执，继续时先对账，禁止重建草稿')
            finally:
                after_receipt=hashlib.sha256(receipt.read_bytes()).hexdigest() if receipt.is_file() else None
                job['lastBrowserAttempt']={'script':attempt.name,'receiptBeforeSha256':before_receipt,
                    'receiptAfterSha256':after_receipt,'receiptUpdated':before_receipt!=after_receipt,
                    'returnCode':proc.returncode}
            (folder/(attempt.stem+'.log')).write_bytes(stdout+stderr)
        if proc.returncode:
            preflight=browser_preflight_failure(attempt,stdout+stderr,d['browserBinding'])
            if preflight:
                raise ValueError('采集连接预检失败：'+preflight['message']+'；本次执行已在业务脚本导入前停止，原任务及既有回执保留，请核对连接后从原检查点继续')
            if before_receipt==after_receipt:
                output=(stdout+stderr).decode('utf-8',errors='replace')
                failure='本轮 CLI bootstrap 连接失败' if 'Failed to connect to ego_cli bootstrap' in output else '本轮浏览器执行器非零退出（'+str(proc.returncode)+'）'
                raise ValueError(failure+'，原业务回执未更新；原任务及采集/草稿检查点保留，请核对本轮执行日志后沿原检查点继续')
        if not receipt.is_file():raise ValueError('采集执行未返回回执，结果待核对；不会重新建草稿')
        record=json.loads(receipt.read_text(encoding='utf-8'))
        job.update(draftId=record.get('draftId'),draftUrl=record.get('draftUrl'),collectionId=record.get('collection',{}).get('id'))
        if proc.returncode or record.get('status')!='saved_reopened_verified' or record.get('snapshotSha256')!=snapshot['sha256'] or record.get('published') is not False:
            raise ValueError(record.get('error') or '草稿回读未完成，保留原记录，不重复提交')
        verified_result(job,record,snapshot);persist(job)
    except Exception as error:
        job.update(status='needs_reconciliation',phase='已保留检查点，可继续核对',error=str(error));persist(job)
    finally:
        job.pop('browserPid',None);persist(job)
        with LOCK:ACTIVE.discard(job['id'])


def process_alive(pid):
    if not isinstance(pid,int) or pid<=0:return False
    if os.name=='nt':
        import ctypes
        kernel=ctypes.windll.kernel32;kernel.OpenProcess.restype=ctypes.c_void_p
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return False
        code=ctypes.c_ulong()
        try:return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle),ctypes.byref(code))) and code.value==259
        finally:kernel.CloseHandle(ctypes.c_void_p(handle))
    try:os.kill(pid,0);return True
    except OSError:return False


@contextmanager
def browser_lease(binding):
    config=json.loads((PROJECT/'automation/config.json').read_text(encoding='utf-8'))['browser']['task_bindings'][binding]
    key=str(config['task_space_id']);folder=PROJECT/'automation/data/erp-collection-locks';folder.mkdir(parents=True,exist_ok=True)
    file=(folder/(hashlib.sha256(key.encode()).hexdigest()+'.lock')).open('a+b')
    if file.seek(0,2)==0:file.write(b'0');file.flush()
    start=time.monotonic();locked=False
    try:
        while not locked:
            try:
                file.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                locked=True
            except OSError:
                if time.monotonic()-start>120:raise ValueError('专属ERP浏览器正在处理另一款，请稍后继续')
                time.sleep(.25)
        yield
    finally:
        if locked:
            file.seek(0)
            if os.name=='nt':msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(file.fileno(),fcntl.LOCK_UN)
        file.close()
