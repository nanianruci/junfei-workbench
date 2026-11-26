"""Adapter for the AGPL IvyeaOps VisualStep component and our shared backend."""
import base64
import copy
import hashlib
import ipaddress
import io
import json
import socket
import threading
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit
from urllib.request import Request,build_opener
import visual_studio as visual
import image_provider
import image_quality
from storyboard_variety import SCENE_FIELDS, gallery_issues, is_scene

LOCK=threading.Lock()
TERMINAL={'approved','awaiting_manual','needs_revision','failed','review_error','submit_unknown','interrupted'}

def artwork_copy(image):
    if image.get('shot_type')=='white_main' or image.get('text_on_image') is False:return {}
    return {k:str(image[k]).strip() for k in ('eyebrow','headline','subline','big_number','callout','supporting_text','proof') if image.get(k)}

def compile_native(project,index,identity):
    p=project['uiPlan'];i=p['images'][index]
    return identity+'\nFINISHED COMMERCIAL ARTWORK: '+i.get('render_prompt','')+'\nPRODUCT LOCK: '+p.get('product_lock','')+'\nSHARED STYLE: '+json.dumps(p.get('style',{}),ensure_ascii=False)+'\nCREATIVE BRIEF: '+p.get('creative_brief','')+'\nLAYOUT: '+i.get('layout_style','editorial')+'; exact text placement '+i.get('text_zone','top-left')+'; 8% safe margin. '+i.get('size','')+'\nEVIDENCE: '+i.get('evidence','')+'\nFINAL ARTWORK COPY: '+json.dumps(artwork_copy(i),ensure_ascii=False)+'\nRender each nonempty public string exactly once and character-for-character; no extra marketing text or claims. Use clear mobile-readable type. If copy object is empty render no added text, numbers, icons, diagrams or badges. Product identity and factual accuracy override styling. No fake measurements, accessories, certifications, comparisons, floating product, neon effects or invented UI. Preserve natural original product labels.'

def native(project,runs):
    if project.get('uiPlan'):p=copy.deepcopy(project['uiPlan'])
    else:
        plan=project['plan'];aplus=project.get('deliverable')=='aplus'
        p={'deliverable':project.get('deliverable','gallery'),'planner':'ai','style':{'direction':project.get('brief',''),'palette':', '.join(plan['palette']),'lighting':plan['lighting'],'type_system':plan['typography']},'product_profile':{'category_family':plan['audience'],'object_behavior':plan['identity']},'product_lock':plan['identity'],'story':'每张图片回答不同购买问题，产品外观保持一致','creative_brief':project.get('brief',''),'language':project.get('language','en'),'product_source_url':project['references'][0],'images':[]}
        roles=['white_main','lifestyle','hero_feature','specs','detail','in_box','hero_feature','trust']
        for i,s in enumerate(plan['slots']):
            p['images'].append({'slot':str(i),'role':visual.slot_name(project,i),'shot_type':('aplus_banner' if i==0 else 'hero_feature') if aplus else roles[i],'product_presence':'hero' if i==0 else 'supporting','layout':'poster_hero' if aplus else ('white_main' if i==0 else 'human_context' if i==1 else 'detail_macro' if i==4 else 'poster_hero'),'buyer_question':s['question'],'evidence':s['evidence'],'headline':s['headline'],'subline':s['caption'],'text_on_image':aplus or i!=0,'text_zone':'top-left','layout_style':'editorial','size':'1464x600' if aplus else '1600x1600','render_prompt':s['composition'],'product_source_url':project['references'][0],'acceptance_criteria':['产品结构、颜色与主体图一致','图上文案准确，无虚构卖点','构图、光线及色系与整套一致'],'versions':[]})
    p['_projectId']=project['id'];p['_confirmed']=project['confirmed']
    if project.get('sizeEvidence'):
        p['sizeEvidenceStatus']=project['sizeEvidence'].get('status')
        p['_sizeChartEvidence']=copy.deepcopy(project['sizeEvidence'])
    warnings=project['plan'].get('warnings',[])
    p['quality']={'ready':True,'issues':[{'code':'FACTS_TO_VERIFY','severity':'warning','message':w} for w in warnings]}
    analysis=project.get('analysis') or {}
    if analysis.get('productTruthSource'):
        p['productTruthSource']=analysis['productTruthSource']
    if analysis.get('layoutNote'):
        p['quality']['skipped_analyses']=[{'stage':'templates','code':'LAYOUT_ANALYSIS_SKIPPED','message':analysis['layoutNote']}]
    if 'referenceViews' in analysis:
        p['sourceAnalysis']={'views':analysis['referenceViews'],
                             'layoutCount':len(analysis.get('layoutTemplates',[]))}
    if project.get('uiPlan'):
        p['sceneDirectionFields']=SCENE_FIELDS
    if project.get('uiPlan') and project.get('deliverable','gallery')=='gallery':
        issues=gallery_issues(p['images'],shared_verified_angle=project.get('galleryDirection')=='apparel_v2_no_filler')
        if project.get('galleryDirection')=='apparel_v2_no_filler':
            from ivyea_pipeline import check_apparel_gallery
            try:check_apparel_gallery(p['images'],forbid_filler=True)
            except ValueError as error:issues.append(str(error))
        p['quality']['ready']=not issues
        p['quality']['issues'].extend({'code':'STORYBOARD_VARIETY','severity':'error','message':message} for message in issues)
    # Do not invent a quality score: only render fields supported by real review.
    from ivyea_pipeline import shot_source, size_chart_evidence
    for index,image in enumerate(p['images']):
        image['_isScene']=is_scene(image)
        if project.get('uiPlan'):
            source, observed, issue=shot_source(project,index)
            image['product_source_url']=source or ''
            if observed:
                image['sourceObservation']=observed
            if image.get('size_chart') and not size_chart_evidence(project.get('sizeEvidence')):
                issue=f'第 {index+1} 张缺少可用的供应商或同款竞品详细尺寸表。'
            if issue:
                image['sourceIssue']=issue
                p['quality']['ready']=False
                p['quality']['issues'].append({'code':'SOURCE_EVIDENCE','severity':'error','message':issue})
        history=[r for r in runs if r['projectId']==project['id'] and r['slot']==index]
        if not history:continue
        current=history[0];image['_runId']=current['id'];image['_status']=current['status']
        image['technicalReviewReady']=(current.get('technicalReview') or {}).get('ready') is True
        if current.get('image'):
            image['final_url']='/api/studio/image?runId='+current['id'];image['base_url']=image['final_url']
        qc=current.get('qc',[])
        if qc:
            q=visual._revision_review(current)
            image['render_qa']={'ready':q['pass'],'review_model':q.get('reviewModel','历史审核模型'),'issues':[{'code':'AI_VISUAL','severity':'error','message':issue} for issue in q['issues']]}
            if 'score' in q:
                image['render_qa']['score']=q['score']
            image['render_qa']['issues'].extend({'code':'COSMETIC_REVIEW','severity':'warning','message':message}
                                               for message in q.get('warnings',[]))
            if q.get('technical'):
                image['render_qa']['technical']=q['technical']
                image['render_qa']['issues'].extend(item for item in q['technical']['issues'] if item['severity']=='warning')
            if q.get('reviewMode')=='technical_only':
                image['render_qa']['review_model']='本地技术检查 · 外观、颜色和文字须人工核对'
                image['render_qa']['manual_visual_review_required']=q['pass']
        elif current['status'] in TERMINAL and current['status'] not in ('approved','awaiting_manual'):
            image['render_qa']={'ready':False,'issues':[{'code':'TASK_ERROR','severity':'error','message':current.get('error',current['status'])}]}
        image['human_reviewed']=current['status']=='approved';image['auto_retry_count']=current.get('attempt',0)
        image['versions']=[{'url':'/api/studio/image?runId='+r['id'],'created_at':str(r.get('approvedAt','')),
                            '_runId':r['id'],'provider':r.get('imageProvider'),'status':r['status']} for r in history[1:9] if r.get('image')]
    p['set_qa']=project.get('set_qa')
    return p

def get_view(product):
    import product_insights
    import generated_gallery
    data=visual.list_projects(product['id']);latest={}
    for p in data['projects']:
        kind=p.get('deliverable','gallery')
        if kind not in latest:latest[kind]=p
    sets={kind:native(p,data['runs']) for kind,p in latest.items()}
    with closing(visual.db()) as c:
        c.execute('CREATE TABLE IF NOT EXISTS studio_tasks (id TEXT PRIMARY KEY, product_id TEXT, body TEXT, created REAL)');c.commit()
        tasks=[json.loads(r[0]) for r in c.execute('SELECT body FROM studio_tasks WHERE product_id=? ORDER BY created DESC LIMIT 20',(product['id'],))]
    jobs={}
    latest_created=max((p['created'] for p in latest.values()),default=0)
    for task in tasks:
        # Finished tasks from an older project must not masquerade as this plan's progress.
        if (task.get('status')!='running' and task.get('created',0)<latest_created
                and not any(task.get('projectId')==project['id'] for project in latest.values())):continue
        if task['kind'] not in jobs:jobs[task['kind']]=task
    renders={}
    for kind,p in latest.items():
        for r in data['runs']:
            if r['projectId']!=p['id'] or kind+':'+str(r['slot']) in renders:continue
            active=r['status'] in ('submitting','generating','reviewing','repairing')
            renders[kind+':'+str(r['slot'])]={'id':r['id'],'kind':'render','status':'running' if active else 'failed' if r['status'] not in ('approved','awaiting_manual') else 'done','message':{'generating':'图片模型生成中','reviewing':'AI视觉质检中','repairing':'正在按质检意见重画一次'}.get(r['status'],r.get('error',r['status'])),'progress':0.4 if active else 1,'total':1,'done_count':0 if active else 1,'error':r.get('error','')}
    insights=product_insights.report(product,verify_local=False)
    selected_gallery=generated_gallery.selection_summary(product,(latest.get('gallery') or {}).get('id'))
    return {'sets':sets,'jobs':jobs,'renderJobs':renders,'materialInsightVersion':insights['version'],
            'selectedGallery':selected_gallery,
            'automaticProductSource':True}

def task_save(task, *, handoff=None):
    with closing(visual.db()) as c,c:
        c.execute('CREATE TABLE IF NOT EXISTS studio_tasks (id TEXT PRIMARY KEY, product_id TEXT, body TEXT, created REAL)')
        c.execute('INSERT OR REPLACE INTO studio_tasks VALUES (?,?,?,?)',(task['id'],task['productId'],json.dumps(task,ensure_ascii=False),task['created']))
        if handoff:
            import codex_handoff
            codex_handoff.record(c,handoff,task['id'],'queued')
    return task

def task_new(product,kind,total=1,*,handoff=None):
    task={'id':uuid.uuid4().hex,'productId':product['id'],'kind':kind,'status':'running','stage':'starting','message':'任务已提交到后台','progress':0,'done_count':0,'total':total,'created':time.time()}
    task_save(task,handoff=handoff);return task

def latest_project(pid,kind):
    return next((p for p in visual.list_projects(pid)['projects'] if p.get('deliverable','gallery')==kind),None)

def start_plan(data,product,listing):
    import product_insights
    from ivyea_pipeline import captured_asin_truth
    refs=data.get('references')
    if refs is None:
        if not product_insights.source_images(product) and not captured_asin_truth(product):
            raise ValueError('请先上传商品实拍图，或采集本款 ASIN 主图')
    elif not isinstance(refs,list) or not 1<=len(refs)<=4:
        raise ValueError('请提供 1–4 张同款参考图')
    count=data.get('target_count',7);kind=data.get('deliverable','gallery')
    if kind not in ('gallery','aplus') or type(count) is not int or count not in ([4,5,6] if kind=='aplus' else [5,6,7,8]):raise ValueError('不支持的张数或交付类型')
    brief=visual.bounded(data.get('brief',''),3500);language=visual.bounded(data.get('language','en'),30);tone=visual.bounded(data.get('visual_tone','natural'),100)
    with LOCK:
        if any(t.get('status')=='running' for t in get_view(product)['jobs'].values()):raise ValueError('当前产品有任务进行中，请完成后再重新策划')
        task=task_new(product,'plan')
    def work():
        try:
            def progress(stage,message,fraction):
                task.update(stage=stage,message=message,progress=fraction)
                task_save(task)
            p=visual.plan({'references':refs,'brief':brief,'visual_tone':tone,'count':count,'deliverable':kind,'language':language},product,listing,progress)
            task.update(status='done',projectId=p['id'],progress=1,done_count=1,message='分镜策划完成，请选卡片查看并修改')
        except Exception as e:task.update(status='failed',error=str(e),message='策划失败；没有用通用模板冒充AI策划')
        task_save(task)
    threading.Thread(target=work,daemon=True).start();return task

def persist(data,product):
    p=data.get('plan');old=visual.get_project(data.get('projectId'))
    if old['productId']!=product['id'] or not isinstance(p,dict):raise ValueError('方案与产品不匹配')
    # Optimistic conflict detection, including two employees editing different cards.
    latest=latest_project(product['id'],old.get('deliverable','gallery'))
    if not latest or latest['id']!=old['id']:raise ValueError('另一页面已修改方案，请刷新后重试')
    existing=native(old,visual.list_projects(product['id'])['runs'])
    new_images=p.get('images',[])
    if not isinstance(new_images,list) or len(new_images)!=len(old['plan']['slots']):raise ValueError('图片分镜数量不一致')
    fields=('role','buyer_question','evidence','headline','eyebrow','subline','big_number','callout','supporting_text','proof','text_zone','text_pos','layout_style','layout','render_prompt','size','template_url','scene','visual_concept','camera_direction')
    updated=copy.deepcopy(existing)
    changed=[];approvals=[];restores=[]
    if any(r['projectId']==old['id'] and r['status'] not in TERMINAL for r in visual.list_projects(product['id'])['runs']):raise ValueError('图片正在生成，请完成后再修改分镜或恢复版本')
    for index,(candidate,current) in enumerate(zip(new_images,existing['images'])):
        clean=copy.deepcopy(current)
        desired=candidate.get('final_url')
        if desired and desired!=current.get('final_url'):
            version=next((v for v in current.get('versions',[]) if v.get('url')==desired),None)
            if not version or not version.get('_runId'):raise ValueError('历史图片版本不存在')
            original=visual.get_run(version['_runId'])
            owner=visual.get_project(original['projectId'])
            if owner['productId']!=product['id'] or original['slot']!=index:raise ValueError('历史版本与产品或分镜不匹配')
            restores.append((index,original))
        for key in fields:
            if key in candidate:clean[key]=visual.bounded(candidate[key] or '',20000 if key=='render_prompt' else 1000)
        if candidate.get('shot_type',current.get('shot_type')) != current.get('shot_type'):
            if (index==0 or current.get('size_chart') or old.get('deliverable','gallery')!='gallery'
                    or candidate.get('shot_type') not in ('detail','lifestyle','trust','hero_feature')):
                raise ValueError('仅可调整普通辅助图的展示类型，不能改变主图或尺码表身份')
            clean['shot_type']=candidate['shot_type']
        if 'scene_direction' in candidate:
            direction=candidate['scene_direction']
            if not isinstance(direction,dict) or any(
                key not in SCENE_FIELDS or not isinstance(value,str)
                or (value and value not in SCENE_FIELDS[key][1]) for key,value in direction.items()
            ):
                raise ValueError(f'第 {index+1} 张场景方向字段无效')
            clean['scene_direction']={key:value for key,value in direction.items() if value}
            if clean['scene_direction']!=current.get('scene_direction') and clean.get('camera_direction')==current.get('camera_direction'):
                clean.pop('camera_direction',None)
        clean['text_on_image']=candidate.get('text_on_image',True) is True
        if clean['shot_type']=='white_main':
            for key in artwork_copy({**clean,'shot_type':'other'}):clean[key]=''
            clean['text_on_image']=False
        edited=any(clean.get(k)!=current.get(k) for k in fields+('text_on_image','scene_direction','shot_type'))
        if edited:changed.append(index)
        if candidate.get('human_reviewed') and not current.get('human_reviewed'):approvals.append(current.get('_runId'))
        if current.get('human_reviewed') and not candidate.get('human_reviewed') and current.get('_runId'):
            r=visual.get_run(current['_runId']);r.update(status='awaiting_manual',manual=False);visual.save_run(r)
        updated['images'][index]=clean
    if restores and changed:raise ValueError('请先保存文案修改，再恢复历史图片')
    if not changed:
        for index,r in restores:
            r={**r,'id':uuid.uuid4().hex,'projectId':old['id'],'manual':False,'status':'awaiting_manual' if r.get('qc') and r['qc'][-1].get('pass') else 'needs_revision'}
            visual.save_run(r)
        if restores:
            old['set_qa']=None;visual.save_project(old)
        for rid in approvals:
            if not rid:raise ValueError('图片任务不存在')
            visual.approve({'runId':rid,'checked':True})
        return {'message':'审核状态已保存'}
    for i in changed:
        im=updated['images'][i]
        if im.get('final_url'):
            im['versions']=[*(im.get('versions',[])),{'url':im['final_url'],'_runId':im.get('_runId')}][-8:]
        for key in ('final_url','base_url','render_qa','human_reviewed','_runId','_status'):im.pop(key,None)
    pid=uuid.uuid4().hex
    for key in ('_projectId','_confirmed','sceneDirectionFields','sourceAnalysis','quality'):
        updated.pop(key,None)
    for image in updated['images']:
        image.pop('sourceObservation',None)
        image.pop('sourceIssue',None)
    updated['set_qa']=None
    new={**old,'id':pid,'parent':old['id'],'created':time.time(),'confirmed':False,'uiPlan':updated,'set_qa':None}
    for index in changed:
        visual.slot_references(new,index, allow_missing_evidence=True)
    for index,image in enumerate(updated['images']):
        slot=new['plan']['slots'][index]
        composition=image.get('render_prompt') or image.get('camera_direction') or image.get('scene') or visual.compile_prompt(new,index)
        slot.update(question=image.get('buyer_question',''),evidence=image.get('evidence',''),headline=image.get('headline','') if image.get('text_on_image') else '',caption=image.get('subline','') if image.get('text_on_image') else '',composition=composition)
    # Preserve unaffected image records in the new version; only changed cards need redrawing.
    visual.save_project(new)
    for im in updated['images']:
        rid=im.get('_runId')
        if not rid:continue
        r=visual.get_run(rid)
        if r['status'] not in TERMINAL:continue
        r={**r,'id':uuid.uuid4().hex,'projectId':pid};visual.save_run(r)
    return {'message':'新版本已保存，修改的图片需重新生成','id':pid}

def render(data,product,listing=None,*,codex_target=None,handoff=None):
    import product_insights
    kind=data.get('deliverable','gallery');p=latest_project(product['id'],kind)
    if not p:raise ValueError('请先策划套图')
    if data.get('projectId') and data['projectId']!=p['id']:
        raise ValueError('分镜版本已更新，请刷新并使用最新方案生成')
    analysis=p.get('analysis') or {}
    if 'productFactsFingerprint' in analysis and product_insights.fingerprint(product)!=analysis['productFactsFingerprint']:
        raise ValueError('分镜引用的产品资料已变化，请重新策划并核对分镜')
    if 'materialInsightVersion' in analysis and product_insights.context(product).get('version','')!=analysis['materialInsightVersion']:
        raise ValueError('已确认的素材洞察已变化，请重新策划并核对分镜')
    listing_fields=('title','bullets','description')
    # A plan created with no copy has no Listing-derived text to invalidate.
    fact_only=product_insights.digest(dict.fromkeys(listing_fields,''))
    if listing is not None and 'listingFingerprint' in analysis and analysis['listingFingerprint'] not in (
        fact_only,product_insights.digest({key:listing.get(key,'') for key in listing_fields})
    ):
        raise ValueError('套图引用的 Listing 文案已变化，请重新策划并核对分镜')
    visual.require_ai_storyboard(p,product['id'])
    # User confirmation at the UI explicitly confirms current facts/copy and paid rendering.
    if data.get('confirmed') is not True:raise ValueError('请先确认分镜与付费生成')
    indices=data.get('indices')
    if not isinstance(indices,list) or not indices or any(type(i) is not int or not 0<=i<len(p['plan']['slots']) for i in indices):raise ValueError('分镜序号不正确')
    config=data.get('config',{})
    settings=image_provider.public()
    if config.get('model') not in settings['models']:raise ValueError('请选择当前服务支持的图片模型')
    if config.get('size') not in ('1:1','4:5','3:4','16:9') or config.get('resolution') not in ('1K','2K','4K'):raise ValueError('图片比例或分辨率不支持')
    config={key:config[key] for key in ('model','size','resolution')}
    with LOCK:
        if codex_target is not None:
            from listing_jobs import digest
            if (digest(latest_project(product['id'],kind)) != codex_target['projectDigest']
                    or settings['activeProvider'] != codex_target['provider']):
                raise ValueError('分镜或图像服务已变化，请重新核对')
            if any(r.get('projectId')==p['id'] and r.get('slot') in indices
                   for r in visual.list_projects(product['id'])['runs']):
                raise ValueError('该图位已有原任务，请先核对')
        tasks=get_view(product)['jobs']
        if any(tasks.get(k,{}).get('status')=='running' for k in ('plan','render_set','render_image')):raise ValueError('图片正在制作，请等待当前批次完成')
        for index in indices:
            visual.check_render_prompt(p,index)
        p['confirmed']=True;p['set_qa']=None;visual.save_project(p)
        task=task_new(product,'render_set' if len(indices)>1 else 'render_image',len(indices),handoff=handoff)
    def work():
        failures=[]
        try:
            for position,index in enumerate(indices):
                task.update(stage='card-'+str(index),message='正在生成第'+str(index+1)+'张并检查外观与文字',progress=position/len(indices));task_save(task)
                r=visual.generate({'id':p['id'],'slot':index,'autoRepair':data.get('autoRepair') is True,**config},
                                  expected_provider=settings['activeProvider'], only_new=codex_target is not None,
                                  expected_project_digest=digest(p) if codex_target is not None else None)
                while True:
                    r=visual.get_run(r['id'])
                    if r['status'] in TERMINAL:break
                    time.sleep(4)
                if r['status'] in ('submit_unknown','interrupted'):
                    raise ValueError(f"第{index+1}张提交状态未确认。已保留任务，后续图片未提交。")
                if r['status'] not in ('approved','awaiting_manual'):
                    failures.append(f"第{index+1}张：{r.get('error') or r['status']}")
                task.update(done_count=position+1,progress=(position+1)/len(indices),failures=failures);task_save(task)
            if failures:
                task.update(status='failed',error='；'.join(failures),message='已完成逐张处理，请查看需返修的图位')
            else:
                task.update(status='done',message='图片生成与单图质检完成，请人工核对')
                images=native(p,visual.list_projects(product['id'])['runs'])['images']
                if data.get('reviewCompleteSet') is True and all(i.get('final_url') and (i.get('render_qa') or {}).get('ready') for i in images):
                    review_set({'deliverable':kind},product)
        except Exception as e:task.update(status='failed',error=str(e))
        task_save(task)
    threading.Thread(target=work,daemon=True).start();return task

def review_contact_sheet(images):
    from PIL import Image,ImageDraw,ImageOps
    tile=420
    columns=min(3,len(images))
    rows=(len(images)+columns-1)//columns
    sheet=Image.new('RGB',(columns*tile,rows*tile),'#E9EAEC')
    draw=ImageDraw.Draw(sheet)
    for index,raw in enumerate(images):
        x,y=(index%columns)*tile,(index//columns)*tile
        with Image.open(io.BytesIO(raw)) as image:
            fitted=ImageOps.contain(image.convert('RGB'),(tile-16,tile-40),Image.Resampling.LANCZOS)
        sheet.paste(fitted,(x+(tile-fitted.width)//2,y+32+(tile-40-fitted.height)//2))
        draw.rectangle((x+8,y+4,x+78,y+27),fill='#172135')
        draw.text((x+16,y+9),f'SLOT {index+1}',fill='white')
    output=io.BytesIO()
    sheet.save(output,'JPEG',quality=91)
    return output.getvalue()


def verified_set_references(project,images):
    """Provide the actual, hash-bound alternate view used by a current card."""
    if project.get('galleryDirection')!='apparel_v2_no_filler':return [],[]
    from ivyea_pipeline import shot_source
    primary=project['references'][0];hashes=(project.get('analysis') or {}).get('referenceHashes',{})
    encoded=[];records=[];positions={}
    for index,_ in enumerate(images):
        ref,observed,issue=shot_source(project,index)
        if issue:raise ValueError(issue)
        if ref==primary or not observed or observed.get('identity')!='same_sku':continue
        if ref in positions:
            records[positions[ref]]['slotNumbers'].append(index+1);continue
        expected=hashes.get(ref)
        if not expected:raise ValueError('整套复核缺少该图位实拍的已绑定哈希')
        source=visual.encode_reference(ref)
        raw=base64.b64decode(source.split(',',1)[1],validate=True)
        if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('整套复核的已核实实拍内容已变化')
        positions[ref]=len(records)
        records.append({'inputImageNumber':len(encoded)+3,'slotNumbers':[index+1],
                        'observedAngle':observed.get('angle'),'visibleConstruction':observed.get('visible',[]),
                        'sourceSha256':expected})
        encoded.append(source)
    return encoded,records


def review_set(data,product):
    p=latest_project(product['id'],data.get('deliverable','gallery'))
    if not p:raise ValueError('请先生成套图')
    images=native(p,visual.list_projects(product['id'])['runs'])['images']
    if not images or any(not i.get('final_url') or not i.get('render_qa',{}).get('ready') for i in images):raise ValueError('请先完成所有图片并通过单图AI质检')
    pixels=[]
    for image in images:
        # Review cached pixels for remote results too: provider links expire and
        # model-side URL downloads can fail even when the local image is intact.
        raw,_=result_image(image['_runId'])
        pixels.append(raw)
    task=task_new(product,'review_set')
    def work():
        try:
            technical_issues=image_quality.duplicate_issues(pixels)
            if p.get('deliverable','gallery')=='gallery':
                for index, raw in enumerate(pixels,1):
                    technical_issues.extend(
                        {**issue,'message':f"第 {index} 张：{issue['message']}"}
                        for issue in __import__('image_composition').for_project(raw,p,index-1)['issues']
                        if issue['severity']=='error'
                    )
                    technical_issues.extend(
                        {**issue,'message':f"第 {index} 张：{issue['message']}"}
                        for issue in image_quality.inspect(raw,main=index==1,full_bleed=index!=1,
                            size_chart=bool(index<=len((p.get('uiPlan') or {}).get('images',[])) and p['uiPlan']['images'][index-1].get('size_chart')))['issues']
                        if issue['severity']=='error'
                    )
            local=any(visual.get_run(image['_runId']).get('imageProvider')==image_provider.local_images.PROVIDER
                      for image in images)
            instruction=(
                'Review the numbered contact sheet as ONE Amazon gallery. The separate first image is the product source. '
                'The second image contains the whole generated set. Use its printed SLOT labels to identify tiles and match '
                'them to imageNumber in the plan. Do not count the reference as a tile. The slot labels are review overlays '
                'outside the artwork and must not be judged as added text. '
                'Check product identity, color, construction and visible hardware across the set and against the source. '
                'Check coherent lighting, color direction and any typography. Use each slot’s buyer question to assess whether '
                'it adds genuinely distinct information. For worn apparel compare body posture, hand position and framing between '
                'wearer shots. A repeated wearer pose or an inset reused to fill detail slots fails when it duplicates the same view. '
                'Return JSON {"ready":bool,"issues":[{"code":"...","severity":"error or warning","message":"Chinese specific issue with slot numbers"}]}. '
                'Product identity uncertainty must be an error. Optional style deviations are warnings. Do not infer exact hex colors. '
                'Report material mismatch, contradictory facts, missing or unreadable required copy, and unusable or repetitive '
                'composition as errors. No fabricated pixel scores.'
                ' A generate_once card permits a generated wearer and removal of unrelated styling props. '
                'Do not require pixel-identical person, hands or unchanged jewelry merely because the product source is a worn photo. '
                'Compare actual garment neckline, seams, cuffs, sleeve proportions, silhouette, texture and visible construction; '
                'any real change to garment details or removal of occlusion that exposes unverified cloth still fails. '
                'A source_layout card preserves its source product pixels and may use a planned crop. '
            )
            context={'productFacts':p.get('productFacts',{}),'identity':p['plan']['identity'],
                     'style':p.get('uiPlan',{}).get('style',{}),
                     'slots':[{'imageNumber':n+1,'role':im.get('role'),'question':im.get('buyer_question'),
                               'evidence':im.get('evidence'),'expectedCopy':artwork_copy(im),'layout':im.get('layout'),
                               'productPresence':im.get('product_presence'),'plannedCamera':im.get('camera_direction'),
                               'productionMethod':im.get('production_method'),'actualArtworkMode':visual.get_run(im['_runId']).get('mode'),
                               'plannedShot':im.get('scene_direction')}
                              for n,im in enumerate(images)],'deliverable':p.get('deliverable','gallery')}
            if p.get('deliverable')=='aplus':
                instruction+=(
                    ' A+ editorial design is required: inspect layout variety, relative type scale, '
                    'photo emphasis and executed shared typography. Report repeated oversized headings, '
                    'the same image-and-headline split across three or more modules, and a size guide '
                    'with no actual measurements as errors. A required composition or hierarchy that '
                    'was not executed is an error even if product identity and spelling are correct.'
                )
            fact_rule=visual.PRODUCT_FACT_QC+' For set review, report unsupported factual claims as severity=error and ready=false.'
            if p.get('galleryDirection')=='apparel_v2_no_filler':
                fact_rule+=visual.APPAREL_NO_FILLER_QC
            chart=p.get('sizeEvidence')
            if chart and any(im.get('size_chart') for im in images):
                context['sizeChartReviewReference']={key:chart.get(key) for key in ('sourceKind','sourceAsin','sourcePage','units','columns','rows','status')}
                fact_rule+=(
                    ' Compare every table cell and unit to sizeChartReviewReference. Its sourceKind and status '
                    'record whether the table came from the supplier or a linked competitor. Preserve that '
                    'source distinction. Reject changed numbers, missing rows, wrong units and unsupported fit claims.'
                )
                fact_rule+=(
                    ' A measurement module deliberately reuses a verified product photograph as context for its '
                    'table. Do not fail this module merely because that source pose appears elsewhere when the '
                    'measurement table is readable and provides distinct sourced information. Check identity, '
                    'every measurement and units, rather than demanding an invented new wearing pose.'
                )
            source_references=[];source_reference_records=[]
            if local or technical_issues:
                issues=list(technical_issues)
                reviewed=all(image.get('human_reviewed') for image in images)
                if local and not reviewed:
                    issues.append({'code':'MANUAL_REVIEW','severity':'warning','message':'本地套图的商品外观和文字须逐张人工核对'})
                result={'ready':local and reviewed and not technical_issues,'issues':issues,
                        'reviewInput':'saved_image_files','semanticReview':'manual_confirmed' if local and reviewed else 'pending_manual'}
            else:
                contact='data:image/jpeg;base64,'+base64.b64encode(review_contact_sheet(pixels)).decode('ascii')
                source_references,source_reference_records=verified_set_references(p,images)
                if source_reference_records:
                    context['additionalProductReferences']=source_reference_records
                    instruction+=(' Additional product reference images are the actual verified same-SKU photographs '
                                  'used by the indicated slotNumbers, not generated artwork. Match them by '
                                  'inputImageNumber in additionalProductReferences. The first source remains the '
                                  'canonical identity and color; these photographs additionally establish only their '
                                  'actually visible alternate views and construction. Check those views against the '
                                  'appropriate source before calling them invented. Hidden or contradictory details still fail.')
                supplements=[]
                if p.get('deliverable','gallery')=='gallery':
                    # A contact sheet establishes variety, but downsamples the
                    # very pocket, seam and table details each card must prove.
                    # Supply every saved gallery artwork at its actual size;
                    # metadata roles must not hide a close feature from review.
                    indices=range(len(images))
                    for n in indices:
                        context.setdefault('supplementalImages',[]).append({'inputImageNumber':len(source_references)+len(supplements)+3,'slotNumber':n+1,
                            'sourceSha256':hashlib.sha256(pixels[n]).hexdigest()})
                        supplements.append('data:image/png;base64,'+base64.b64encode(pixels[n]).decode('ascii'))
                    instruction+=(' Supplemental images are the same saved artwork pixels at higher resolution; '
                                  'match them using supplementalImages. Check alleged missing parts there before failing. '
                                  'Evaluate each slot at its planned subject scale. A deliberately distant environmental '
                                  'view establishes silhouette and context; do not demand close-up stitching or pocket-edge '
                                  'resolution, and do not infer an unresolved part is absent. A visible contradiction, '
                                  'unexecuted planned shot or incorrect required feature still fails.')
                result=visual.model_json(instruction+fact_rule,context,[visual.encode_reference(p['references'][0]),contact,*source_references,*supplements])
                if type(result.get('ready')) is not bool or not isinstance(result.get('issues'),list):raise ValueError('整套复核格式异常')
                result['modelReady']=result['ready']
                result['reviewInput']='numbered_contact_sheet'
                if context.get('supplementalImages'):
                    result['supplementalArtworkReferences']=context['supplementalImages']
                warnings_only=bool(result['issues']) and all(i.get('severity')=='warning' for i in result['issues'])
                result['ready']=(result['ready'] or warnings_only) and not any(i.get('severity')!='warning' for i in result['issues'])
            result['imageHashes']=[hashlib.sha256(raw).hexdigest() for raw in pixels]
            if source_reference_records:result['additionalSourceReferences']=source_reference_records
            current=visual.get_project(p['id'])
            current_images=native(current,visual.list_projects(product['id'])['runs'])['images']
            if [i.get('_runId') for i in current_images]!=[i.get('_runId') for i in images]:
                raise ValueError('复核期间图片已更新，旧复核未采用，请重新复核整套')
            if current.get('set_qa'):
                current.setdefault('set_qa_history',[]).append(current['set_qa'])
            current['set_qa']=result;visual.save_project(current)
            task.update(status='done',message='整套复核完成',progress=1,done_count=1)
        except Exception as e:task.update(status='failed',error=str(e))
        task_save(task)
    threading.Thread(target=work,daemon=True).start();return task

def result_image(run_id):
    r=visual.get_run(run_id);url=r.get('image')
    if not url:raise ValueError('此任务没有成图')
    cache=image_provider.ROOT/'studio-results';cache.mkdir(exist_ok=True)
    path=cache/(run_id+'.image') if url.startswith('local:') else cache/(run_id+'-'+hashlib.sha256(url.encode()).hexdigest()[:16]+'.image')
    if url.startswith('local:') and not path.is_file():
        import re
        source_id=url.removeprefix('local:')
        if not re.fullmatch('[a-f0-9]{32}',source_id):raise ValueError('本地成图标识不正确')
        owner=visual.get_run(source_id)
        if owner.get('productId')!=r.get('productId'):raise ValueError('本地成图不属于当前产品')
        source_path=cache/(source_id+'.image')
        if not source_path.is_file():raise ValueError('原图排版文件已不存在，请重新制作')
        path=source_path
    if path.is_file():raw=path.read_bytes()
    elif url.startswith('/uploads/'):
        raw=image_provider.local_image(url)
    else:
        u=urlsplit(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.port not in (443,None):raise ValueError('成图地址不安全')
        addresses=socket.getaddrinfo(u.hostname,443,type=socket.SOCK_STREAM)
        if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise ValueError('成图地址不得指向内部网络')
        with build_opener(image_provider.NoImageRedirect()).open(Request(url,headers={'User-Agent':'JunfeiListing/1.0'}),timeout=40) as response:raw=response.read(25*1024*1024+1)
        if len(raw)>25*1024*1024:raise ValueError('成图文件超过25MB')
    mime='image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if raw.startswith(b'\xff\xd8\xff') else 'image/webp' if raw[:4]==b'RIFF' and raw[8:12]==b'WEBP' else None
    if not mime:raise ValueError('生成链接未返回有效PNG/JPG/WebP图片')
    if not path.exists():
        tmp=path.with_suffix('.tmp-'+uuid.uuid4().hex);tmp.write_bytes(raw);tmp.replace(path)
    return raw,mime

def export_psd(run_id):
    from PIL import Image
    from psd_writer import PsdLayer, build_psd

    run=visual.get_run(run_id)
    project=visual.get_project(run['projectId'])
    raw,_=result_image(run_id)
    base=Image.open(io.BytesIO(raw)).convert('RGBA')
    index=run['slot']
    layers=[PsdLayer(f'成图 · {visual.slot_name(project,index)}',base,ascii='final')]
    references=run.get('references')
    if references is None:
        try:
            references=visual.slot_references(project,index,allow_missing_evidence=True)
        except (KeyError,ValueError):
            references=[]
    if references:
        try:
            source_raw=base64.b64decode(visual.encode_reference(references[0]).split(',',1)[1],validate=True)
            source_hash=(project.get('analysis') or {}).get('referenceHashes',{}).get(references[0])
            if not source_hash or hashlib.sha256(source_raw).hexdigest()==source_hash:
                source=Image.open(io.BytesIO(source_raw)).convert('RGBA')
                source.thumbnail(base.size,Image.Resampling.LANCZOS)
                offset=((base.width-source.width)//2,(base.height-source.height)//2)
                layers.append(PsdLayer('产品真值（默认隐藏）',source,offset=offset,
                                       visible=False,ascii='product-source'))
        except (OSError,ValueError):
            pass
    suffix='' if run.get('manual') else '_DRAFT'
    return build_psd(base.size,layers),f"{project.get('deliverable','gallery')}_{index+1:02d}{suffix}.psd"

def restart_tasks():
    with closing(visual.db()) as c,c:
        c.execute('CREATE TABLE IF NOT EXISTS studio_tasks (id TEXT PRIMARY KEY, product_id TEXT, body TEXT, created REAL)')
        for rid,raw in c.execute('SELECT id,body FROM studio_tasks').fetchall():
            t=json.loads(raw)
            if t['status']=='running':
                t.update(status='failed',error='服务已重启，批次已暂停。已提交图片仍在后台质检，请核对结果后只补缺失，避免重复计费。')
                c.execute('UPDATE studio_tasks SET body=? WHERE id=?',(json.dumps(t,ensure_ascii=False),rid))

def source_archive():
    """Corresponding source for the combined deployed application, excluding user data."""
    import io,zipfile
    from pathlib import Path
    root=Path(__file__).parent;buffer=io.BytesIO()
    packaged=root/'corresponding-source.zip'
    if packaged.is_file():return packaged.read_bytes()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for file in root.iterdir():
            if file.is_file() and file.suffix in ('.py','.js','.cjs','.html','.txt','.md','.ps1') and not file.name.endswith('.log'):
                archive.write(file,file.name)
        for name in ('visual-ui','extension','cloud'):
            for file in (root/name).rglob('*'):
                if not file.is_file() or any(p in ('node_modules','dist','__pycache__') for p in file.relative_to(root/name).parts):continue
                archive.write(file,str(file.relative_to(root)))
        audit=root/'docs'/'IVYEA-IMAGE-AUDIT-20261104.md'
        if audit.is_file():archive.write(audit,str(audit.relative_to(root)))
    return buffer.getvalue()
