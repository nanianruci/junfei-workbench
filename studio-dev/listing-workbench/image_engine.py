"""Account-scoped independent image studio: plan, compose, edit, recolour, review.

Model transport, quota reservations, raw receipts and unknown-submit recovery
remain owned by image_provider. This module never holds or copies API keys.
"""
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from PIL import Image, ImageDraw, ImageOps
import app_paths
import image_engine_prompts as prompts

DB = app_paths.DATA / 'image_engine.sqlite3'
LOCK = threading.RLock()
ACTIVE = set()
TERMINAL = {'completed','needs_revision','failed','submission_unknown','interrupted'}
MODES = {'suite','repair','colour'}


def db():
    c = sqlite3.connect(DB, timeout=20)
    c.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, product_id TEXT, request_id TEXT UNIQUE, body TEXT, updated REAL)')
    c.commit()
    return c


def save(task):
    task['updated'] = time.time()
    if task['id'] in ACTIVE:
        task['leaseUntil'] = task['updated'] + 600
    with LOCK, closing(db()) as c, c:
        c.execute('INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?)',
                  (task['id'],task['productId'],task['requestId'],json.dumps(task,ensure_ascii=False),task['updated']))
    return task


def get(task_id):
    with closing(db()) as c:
        row=c.execute('SELECT body FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not row:
        raise ValueError('图片任务不存在于当前账号')
    return json.loads(row[0])


def by_request(product_id, request_id):
    if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9-]{20,64}', request_id):
        raise ValueError('原图片请求编号无效')
    with closing(db()) as connection:
        row = connection.execute('SELECT body FROM tasks WHERE product_id=? AND request_id=?',
                                 (product_id, request_id)).fetchone()
    return json.loads(row[0]) if row else None


def list_tasks(product_id, *, limit=30, completed_only=False):
    with closing(db()) as c:
        return [json.loads(row[0]) for row in c.execute(
            """SELECT body FROM tasks WHERE product_id=?
               AND (?=0 OR json_extract(body,'$.status')='completed')
               ORDER BY updated DESC LIMIT ?""",
            (product_id, int(completed_only), -1 if limit is None else limit))]


def coordination_tasks(product_id):
    """Include every original task that can block a new suite, even beyond the recent history."""
    with closing(db()) as connection:
        rows = connection.execute("""
            WITH recent AS (
                SELECT id FROM tasks WHERE product_id=? ORDER BY updated DESC, id LIMIT 30
            )
            SELECT body FROM tasks WHERE product_id=? AND (
                id IN (SELECT id FROM recent)
                OR COALESCE(json_extract(body,'$.status'),'') NOT IN ('failed','needs_revision','completed')
                OR (json_extract(body,'$.status')='completed' AND json_extract(body,'$.request.mode')='suite'
                    AND json_extract(body,'$.request.count')=7)
            ) ORDER BY updated DESC, id
        """, (product_id, product_id))
        return [json.loads(row[0]) for row in rows]


def asset(url):
    if not isinstance(url,str) or not re.fullmatch(r'/uploads/[a-f0-9]{32}\.(png|jpg|webp)',url):
        raise ValueError('请选择当前账号已上传的图片')
    path=app_paths.DATA/url.lstrip('/')
    if path.is_symlink() or path.resolve().parent!=(app_paths.DATA/'uploads').resolve() or not path.is_file():
        raise ValueError('图片不属于当前账号或文件已不存在')
    raw=path.read_bytes()
    if len(raw)>15*1024*1024:
        raise ValueError('图片超过15MB')
    with Image.open(io.BytesIO(raw)) as image:
        if image.width*image.height>40_000_000:
            raise ValueError('图片超过4000万像素')
        image=ImageOps.exif_transpose(image).copy()
    return image, hashlib.sha256(raw).hexdigest()


def cache(image):
    import openai_image_provider as images
    b=io.BytesIO();image.save(b,'PNG')
    return images.cache_image(b.getvalue())


def require_codex():
    import image_provider as images
    import openai_image_provider as custom
    import provider
    settings=provider.text_settings()
    config=custom.public()
    if (images.active_provider()!='openai' or config['baseUrl']!=custom.CODEX_TOOLS_BASE
            or provider.text_provider_name(settings)!='Codex Tools' or not settings.get('api_key')):
        raise ValueError('请在当前账号把分析与图片接口均设为 Codex Tools；本引擎不会切到其他服务')
    return config


def json_model(instruction,context,refs,purpose):
    require_codex()
    import visual_studio as visual
    return visual.model_json(instruction,context,[visual.encode_reference(r) for r in refs],purpose=purpose)


def normalize_shots(value,count,reference_count,no_text):
    # Preserve a complete textual style returned by vision as its named
    # photography field; this is lossless wire normalization, not QA approval.
    if (isinstance(value,dict) and isinstance(value.get('style'),str)
            and value['style'].strip() and len(value['style'])<=6000):
        value['style']={'photography':value['style']}
    if not isinstance(value,dict) or not value.get('identity') or not isinstance(value.get('style'),dict):
        raise ValueError('策划未返回商品身份与统一风格')
    shots=value.get('shots')
    if not isinstance(shots,list) or len(shots)!=count:
        raise ValueError('策划张数不完整，未用模板填充')
    for index,shot in enumerate(shots):
        if not isinstance(shot,dict) or shot.get('layout') not in ('main','scene','detail','feature'):
            raise ValueError('策划版式无效')
        if type(shot.get('sourceIndex')) is not int or not 0<=shot['sourceIndex']<reference_count:
            raise ValueError('分镜未绑定具体商品图')
        # Accept the named richer wire shape without changing coordinates or
        # inventing evidence; the saved original proposal remains auditable.
        if isinstance(shot.get('scene'),dict):
            scene=shot['scene']
            if set(scene)-{'composition','templateArtwork','templateId','templateOriginalPrompt'} or not isinstance(scene.get('composition'),str):
                raise ValueError('结构化场景缺少明确版式说明')
            shot['sceneDetails']=json.loads(json.dumps(scene));shot['scene']=scene['composition']
        if isinstance(shot.get('sourceCrop'),dict):
            crop=shot['sourceCrop']
            if set(crop)!={'x','y','width','height'}:raise ValueError('细节坐标字段无效')
            shot['sourceCrop']=[crop[k] for k in ('x','y','width','height')]
        # Vision planners may separate observed facts and inferences. Preserve
        # that typed evidence rather than discarding it or making another call.
        evidence=shot.get('evidence')
        if isinstance(evidence,dict):
            evidence=json.loads(json.dumps(evidence))
            details=evidence.pop('detailCrops',None)
            if details is not None:
                if not isinstance(details,list) or not 1<=len(details)<=6:raise ValueError('结构细节证据格式无效')
                for detail in details:
                    if (not isinstance(detail,dict) or set(detail)!={'feature','sourceIndex','sourceCrop'}
                            or not isinstance(detail['feature'],str) or not detail['feature'].strip()
                            or type(detail['sourceIndex'])is not int or not 0<=detail['sourceIndex']<reference_count):
                        raise ValueError('结构细节证据未绑定真实来源')
                    crop=detail['sourceCrop']
                    if isinstance(crop,dict)and set(crop)=={'x','y','width','height'}:crop=[crop[k]for k in ('x','y','width','height')]
                    if (not isinstance(crop,list)or len(crop)!=4 or any(type(v)not in(int,float)or not 0<=v<=1 for v in crop)
                            or crop[2]<=0 or crop[3]<=0 or crop[0]+crop[2]>1.001 or crop[1]+crop[3]>1.001):
                        raise ValueError('结构细节证据坐标超出原片')
                    detail['sourceCrop']=crop
                shot['detailEvidence']=details
            if (not evidence or any(not isinstance(k,str) or not isinstance(v,(str,list))
                    or (isinstance(v,list) and any(not isinstance(item,str) for item in v))
                    for k,v in evidence.items()) or not any(evidence.values())):
                raise ValueError('结构化可见证据必须包含真实文字条目')
            shot['evidenceDetails']=json.loads(json.dumps(evidence))
            shot['evidence']=json.dumps(evidence,ensure_ascii=False)
        for key in ('evidence','question','scene','camera'):
            if not isinstance(shot.get(key),str) or not shot[key].strip() or len(shot[key])>2000:
                raise ValueError('每张图必须有独立问题、可见证据和镜头设计')
        explicit_text_box='textBox' in shot
        for key in ('subjectBox','textBox'):
            box=shot.get(key,[0,0,1,1] if key=='subjectBox' else [0,0,0,0]);shot[key]=box
            if (not isinstance(box,list) or len(box)!=4 or any(type(v) not in (int,float) or not 0<=v<=1 for v in box)
                    or (key=='subjectBox' and (box[2]<=0 or box[3]<=0))
                    or box[0]+box[2]>1.001 or box[1]+box[3]>1.001):
                raise ValueError(f'第{index+1}张的{key}版式坐标无效')
        if not isinstance(shot.get('copy'),list) or len(shot['copy'])>4 or any(not isinstance(s,str) or len(s)>120 for s in shot['copy']):
            raise ValueError('图上文字格式无效')
        if no_text or shot['layout']=='main':shot['copy']=[]
        if explicit_text_box and shot['copy'] and (shot['textBox'][2]<=0 or shot['textBox'][3]<=0):
            raise ValueError('有文案的图位必须保留有效文字区域')
        if shot['layout']=='detail' and shot.get('sourceCrop') is not None:
            crop=shot.get('sourceCrop')
            if (not isinstance(crop,list) or len(crop)!=4 or any(type(v) not in (int,float) or not 0<=v<=1 for v in crop)
                    or crop[2]<=0 or crop[3]<=0 or crop[0]+crop[2]>1.001 or crop[1]+crop[3]>1.001):
                raise ValueError('细节图需要在原片内明确选择真实裁切区域')
        shot['index']=index
    return value


def blueprint(shot):
    image=Image.new('RGB',(800,800),'#f5f4f0');d=ImageDraw.Draw(image)
    def box(key,colour):
        x,y,w,h=shot[key];d.rectangle((int(x*800),int(y*800),int((x+w)*800)-1,int((y+h)*800)-1),fill=colour)
    box('subjectBox','#a9b9b0')
    if shot['copy']:box('textBox','#d9c9aa')
    return cache(image)


def frozen_plan_recovery(recipe,parent,product_id):
    """Reuse a fully returned plan only after a known pre-image wire error."""
    previous=parent.get('request',{})
    allowed=('结构化可见证据必须包含真实文字条目','每张图必须有独立问题、可见证据和镜头设计','细节图需要在原片内明确选择真实裁切区域')
    style_wire=(parent.get('error')=='策划未返回商品身份与统一风格'
        and previous.get('apparelSuite',{}).get('artDirection')=='template-led-v1'
        and isinstance(parent.get('plannerProposal'),dict)
        and isinstance(parent['plannerProposal'].get('style'),str)
        and 0<len(parent['plannerProposal']['style'].strip())<=6000)
    if (parent.get('productId')!=product_id or parent.get('status')!='failed' or (parent.get('error')not in allowed and not style_wire)
            or parent.get('results') or parent.get('pending') or parent.get('providerJobId')
            or parent.get('backgroundPlates') or not isinstance(parent.get('plannerProposal'),dict)
            or recipe.get('mode')!='suite' or previous.get('mode')!='suite'):
        raise ValueError('仅可复用已明确失败且尚未提交图片的完整策划')
    fields=('brief','count','noText','referenceSha256','preserveSourcePixels','fidelityPolicy','templateExecutionPolicy')
    if any(recipe.get(k)!=previous.get(k) for k in fields):raise ValueError('原策划输入已改变，不能复用')
    for key in ('officialTemplates','apparelSuite'):
        def identity(value):
            if isinstance(value,list):return [identity(v)for v in value]
            if isinstance(value,dict):return {k:identity(v)for k,v in value.items()if k!='url'}
            return value
        if identity(recipe.get(key))!=identity(previous.get(key)):raise ValueError('原策划模板或参考角色已改变')
    proposal=json.loads(json.dumps(parent['plannerProposal']))
    return {'parentTaskId':parent['id'],'proposal':proposal,
        'proposalSha256':hashlib.sha256(json.dumps(proposal,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        'originalError':parent['error'],'paidImageSubmissionReplayed':False}


def frozen_generation_recovery(recipe,parent,product_id):
    import image_provider as images
    known='衣料边界与独立衣料证据仍冲突，不能自动换色'
    if (parent.get('status')!='failed' or parent.get('error')!=known or not parent.get('results')
            or not parent.get('plan') or parent.get('attempts') or parent.get('automaticCorrections')):
        raise ValueError('仅可接续明确的模板原生输出本地校色失败')
    # Apply the same complete source/template/brief binding as plan recovery.
    shell={**parent,'error':'结构化可见证据必须包含真实文字条目','results':[],'pending':None,
           'providerJobId':None,'plannerProposal':parent['plan']}
    frozen_plan_recovery(recipe,shell,product_id)
    outputs=json.loads(json.dumps(parent['results']))
    for index,row in enumerate(outputs):
        if row.get('index')!=index or not row.get('providerJobId'):raise ValueError('原模型成图顺序不完整')
        if index<len(outputs)-1 and (row.get('review',{}).get('blocking')is not False or row.get('review',{}).get('identityPreserved')is not True):
            raise ValueError('早期成图未完成检查，不能跳过')
        snapshot=images.poll({'id':row['providerJobId']})
        raw=row.get('rawModelOutput')or row['raw']
        assets=[a.get('sourceAsset')or a for a in snapshot.get('assets',[])]
        if (snapshot.get('status')!='completed' or raw not in [a.get('url')for a in assets]+snapshot.get('images',[])
                or asset(raw)[1]!=row.get('rawSha256')):
            raise ValueError('原图片任务或输出字节未核实，不能接续')
    return {'parentTaskId':parent['id'],'plan':json.loads(json.dumps(parent['plan'])),'results':outputs,
        'originalError':parent['error'],'providerCompletionsVerified':True,'paidImageSubmissionReplayed':False}


def default_art_direction(data,product_id):
    """New creative default must never reinterpret a saved or recovered intent."""
    with closing(db()) as connection:
        row=connection.execute('SELECT body FROM tasks WHERE request_id=?',
            (data.get('requestId'),)).fetchone()
    parent=json.loads(row[0]) if row else None
    if parent is None:
        ids=[data.get(k) for k in ('reusePlanTaskId','reuseGeneratedTaskId','reusePlateTaskId') if data.get(k)]
        if len(ids)>1:raise ValueError('只能沿一份原任务恢复，不混用冻结策略')
        if ids:parent=get(ids[0])
    if parent is not None:
        if parent.get('productId')!=product_id:raise ValueError('原任务不属于当前商品')
        return parent.get('request',{}).get('apparelSuite',{}).get('artDirection','legacy-v1')
    return 'legacy-v1' if data.get('preserveSourcePixels') is True else 'template-led-v1'


def start(data,product,*,handoff=None):
    mode=data.get('mode')
    if mode not in MODES:raise ValueError('请选择套图、修图或换色')
    request_id=data.get('requestId')
    if not isinstance(request_id,str) or not re.fullmatch('[a-f0-9-]{20,64}',request_id):
        raise ValueError('缺少有效请求编号')
    refs=data.get('references')
    if not isinstance(refs,list) or not 1<=len(refs)<=3:raise ValueError('请选择1–3张同款商品图')
    hashes=[asset(ref)[1] for ref in refs]
    brief=data.get('brief','')
    if not isinstance(brief,str) or not 1<=len(brief.strip())<=6000:raise ValueError('请填写1–6000字的具体要求')
    count=data.get('count',3) if mode=='suite' else 1
    if type(count) is not int or not 1<=count<=8:raise ValueError('自由套图张数为1–8张；正式刊登仍使用正式套图流程')
    edit_task=data.get('editTask','free')
    if mode=='repair' and not data.get('repairTemplateId'):prompts.compile_edit(edit_task,brief)
    if data.get('repairTemplateId'):
        import official_image_templates as library
        library.get(data['repairTemplateId'],'repair')
    options={k:data[k] for k in ('mask','protected','target','referencePhoto','referenceMask','polish','autoMask','renderedImage','colourRenderer','shaderReceipt','originalSource','qualityCheckId','selectionTaskId') if k in data}
    if mode=='colour':
        if not re.fullmatch('#[a-fA-F0-9]{6}',str(options.get('target',''))):raise ValueError('请提供目标色RGB，不按颜色名称猜测')
        if options.get('autoMask') is True:
            import garment_auto
            kind=garment_auto.category(product)
            if kind=='auto':raise ValueError('当前商品未识别为服装，请明确选择要换色的商品部件')
            options['garmentKind']=kind
            options.pop('mask',None)
        else:
            source,_=asset(refs[0]);mask,_=asset(options.get('mask'))
            if source.size!=mask.size:raise ValueError('换色选区必须与商品图尺寸相同')
        if options.get('protected'):asset(options['protected'])
        if options.get('referencePhoto'):
            asset(options['referencePhoto']);asset(options.get('referenceMask'))
    if mode=='colour' and options.get('colourRenderer')=='official-glsl':
        from official_colour_result import validate
        rendered,_=asset(options.get('renderedImage'))
        validate(asset(refs[0])[0],asset(options.get('mask'))[0],rendered,options.get('shaderReceipt'))
    if mode!='colour' or options.get('polish') is True:require_codex()
    recipe={'mode':mode,'references':refs,'referenceSha256':hashes,'brief':brief,'count':count,
            'noText':data.get('noText',True) is not False,'editTask':edit_task,'options':options}
    if data.get('sourceVariant') is not None:
        import image_source_variants
        if mode == 'repair' and data.get('generatedColourVariant') is not None:
            if len(refs) != 2:
                raise ValueError('颜色变体需要原母版和一张真实同款目标照片')
            variant_source = refs[1]
        else:
            variant_source = options.get('originalSource') if mode == 'colour' else refs[0]
        recipe['sourceVariant']=image_source_variants.freeze(product,data['sourceVariant'],
            variant_source,asset,
            normalized_source=refs[0] if mode=='colour' else None)
    # Freeze every ancillary file too: a later browser upload cannot change a
    # mask/reference behind a saved job without detection.
    recipe['assetSha256']={url:asset(url)[1] for key,url in options.items()
                          if key in ('mask','protected','referencePhoto','referenceMask','renderedImage','originalSource')}
    if mode in ('suite','repair'):
        import garment_auto
        kind=garment_auto.category(product)
        if kind!='auto':recipe['fidelityPolicy']={'version':'source-chroma-and-fibres-v1','garmentKind':kind}
        if data.get('preserveSourcePixels') is True:
            if kind=='auto':raise ValueError('原片穿着图层模式目前仅支持已识别的服装')
            recipe['preserveSourcePixels']=True
            if data.get('neutralBackground') is True:recipe['neutralBackground']=True
    if mode=='suite':
        import official_image_templates as library
        recipe['templateExecutionPolicy']='official-artwork-v1'
        config=__import__('copy').deepcopy(data.get('apparelSuite') or {})
        if not isinstance(config,dict):raise ValueError('服装编排配置无效')
        point_ids=config.get('pointTemplateIds')
        if point_ids is not None and data.get('layoutTemplateIds')is not None:
            raise ValueError('卖点排版和自由逐图排版不能同时指定')
        if kind!='auto'and data.get('layoutTemplateIds')is None:
            config['templatePolicy']='point-only-v1'
        elif(kind!='auto'and data.get('layoutTemplateIds')is not None and not config.get('counts')
                and not any(data.get(k)for k in ('reusePlanTaskId','reuseGeneratedTaskId','reusePlateTaskId'))):
            config['counts']={'seller':count}
        if config.get('templatePolicy')=='point-only-v1':
            seller_count=(config.get('counts') or {}).get('seller',max(0,count-2))
            if type(seller_count)is not int or not 0<=seller_count<=8:raise ValueError('卖点图数量须为0–8整数')
            if point_ids is not None and (not isinstance(point_ids,list)or(point_ids and len(point_ids)!=seller_count)):
                raise ValueError('每个卖点模板生成一张，已选模板数须等于卖点图数量')
            recipe['officialTemplates']=(library.assignments(product,len(point_ids)if point_ids else seller_count,
                requested=point_ids or None)if seller_count else[])
            seller_templates=list(recipe['officialTemplates'])
            if config.get('useLifestyleTemplates') is True and (config.get('counts') or {}).get('ins'):
                from lifestyle_variety import choose_template_ids
                ins_count=config['counts']['ins'];ids=choose_template_ids(library,ins_count,config.get('lifestyleTemplateIds'),[t['id'] for t in seller_templates])
                lifestyle_templates=library.assignments(product,ins_count,requested=ids)
                config['lifestyleTemplateIds']=ids
                recipe['officialTemplates']+=lifestyle_templates
            recipe['pointStyles']=[{'id':t['id'],'style':t.get('style'),
                'imageUrl':t.get('originalSourceUrl')or t['url'],'materialSource':2,'tabType':'1'}
                for t in seller_templates]
        else:recipe['officialTemplates']=library.assignments(product,count,requested=data.get('layoutTemplateIds'))
        if kind!='auto':
            import apparel_suite
            import copy
            if 'artDirection' not in config:
                config['artDirection']=default_art_direction(data,product['id'])
            if (config.get('counts') or {}).get('size'):
                with closing(db()) as connection:
                    saved=connection.execute('SELECT body FROM tasks WHERE request_id=?',(data['requestId'],)).fetchone()
                saved_request=json.loads(saved[0])['request'] if saved else None
                parents=[data[k] for k in ('reusePlanTaskId','reuseGeneratedTaskId','reusePlateTaskId') if data.get(k)]
                if saved_request is None and parents:saved_request=get(parents[0])['request']
                if saved_request is None:
                    recipe['sizeRenderingPolicy']='verified-typeset-v1'
                    recipe['sizeLayoutCompiler']='layout-contract-v2'
                elif saved_request.get('sizeRenderingPolicy'):recipe['sizeRenderingPolicy']=saved_request['sizeRenderingPolicy']
                if saved_request and saved_request.get('sizeLayoutCompiler'):recipe['sizeLayoutCompiler']=saved_request['sizeLayoutCompiler']
            if (config.get('counts') or {}).get('size') and not config.get('sizeChart'):
                from shared_size_chart import from_product
                with closing(db()) as connection:
                    prior=connection.execute('SELECT body FROM tasks WHERE request_id=?',(data['requestId'],)).fetchone()
                frozen=json.loads(prior[0])['request'].get('apparelSuite',{}) if prior else {}
                config['sizeChart']=frozen.get('sizeChart') or from_product(product)
            if (config.get('counts') or {}).get('size'):
                import copy
                chart=copy.deepcopy(config.get('sizeChart'))
                if config.get('sizeChartStyle') is not None:
                    if config['sizeChartStyle'] not in ('classic-v1','editorial-split-v1'):raise ValueError('尺码版式无效')
                    chart['style']=config['sizeChartStyle']
                preview_id=config.get('sizePreviewTaskId')
                if preview_id:
                    preview_task=get(preview_id);index=config.get('sizePreviewResultIndex',0)
                    matches=[r for r in preview_task.get('results',[]) if r['index']==index]
                    if preview_task['productId']!=product['id'] or preview_task['status']!='completed' or len(matches)!=1:
                        raise ValueError('尺码预览须为当前商品已完成的白底候选')
                    preview=matches[0]
                    if preview.get('shot',{}).get('apparelKind')!='white' or preview.get('review',{}).get('blocking')is not False or preview.get('review',{}).get('identityPreserved')is not True:
                        raise ValueError('尺码预览没有已通过检查的同款白底图')
                    _,digest=asset(preview['image'])
                    chart['preview']={'url':preview['image'],'sha256':digest,'sourceTaskId':preview_id,'resultIndex':index,'purpose':'approved visual preview only; size values from sourceEvidence'}
                    recipe['assetSha256'][preview['image']]=digest
                config['sizeChart']=chart
            provider_config=require_codex()
            # Freeze the actual configured Codex canvas, rather than copying
            # LinkFox's portrait default into a square-only transport.
            native_canvas=(provider_config or {}).get('outputSize') or '1600x1600'
            width,height=map(int,native_canvas.split('x'))
            config.setdefault('targetWidth',width);config.setdefault('targetHeight',height)
            config.setdefault('aspectRatio','1:1')
            if (config['targetWidth'],config['targetHeight'],config['aspectRatio'])!=(width,height,'1:1'):
                raise ValueError('服装编排尺寸必须匹配当前Codex Tools实际配置；不会静默补白或拉伸')
            consistency=config.get('consistency',{})
            if not isinstance(consistency,dict):raise ValueError('模特场景配置无效')
            for key in ('modelReference','sceneReference','sceneMask'):
                value=consistency.get(key)
                if value is not None:
                    url=value if isinstance(value,str) else value.get('url') if isinstance(value,dict) else None
                    _,digest=asset(url);consistency[key]={'url':url,'sha256':digest}
                    recipe['assetSha256'][url]=digest
            config['consistency']=consistency
            config['sourcePixelMode']=recipe.get('preserveSourcePixels') is True
            recipe['apparelSuite']=apparel_suite.freeze_request(config,refs,
                reference_sha256=hashes,templates=recipe['officialTemplates'],count=count,
                no_text=recipe['noText'],brief=brief)
            if config.get('useLifestyleTemplates') is True and (config.get('counts') or {}).get('ins',0)>1:
                recipe['lifestyleVarietyPolicy']='distinct-template-presentation-v1'
                with closing(db()) as connection:
                    existing=connection.execute('SELECT body FROM tasks WHERE request_id=?',(data['requestId'],)).fetchone()
                original=json.loads(existing[0])['request'] if existing else None
                if original is None:recipe['lifestylePromptPolicy']='explicit-presentation-v1'
                elif original.get('lifestylePromptPolicy'):recipe['lifestylePromptPolicy']=original['lifestylePromptPolicy']
        if data.get('reusePlateTaskId'):
            parent=get(data['reusePlateTaskId'])
            if (not recipe.get('preserveSourcePixels') or not parent['request'].get('preserveSourcePixels')
                    or parent['productId']!=product['id'] or parent['request'].get('count')!=count
                    or parent['request'].get('noText')!=recipe['noText']
                    or [r['id'] for r in parent['request']['officialTemplates']]!=[r['id'] for r in recipe['officialTemplates']]
                    or parent['status'] not in TERMINAL or parent['status'] in ('interrupted','submission_unknown')
                    or parent['request']['references']!=refs or parent['request']['referenceSha256']!=hashes
                    or not parent.get('plan')
                    or {p.get('index') for p in parent.get('backgroundPlates',[])}!={s['index'] for s in parent.get('plan',{}).get('shots',[]) if s['layout']!='main' and not (s['layout']=='detail' and not s.get('copy') and parent['request'].get('apparelSuite'))}):
                raise ValueError('只能复用当前账号、同款同原片、全部已返回的原模板背景；未知提交不能复用或重发')
            import image_source_layers
            approved=image_source_layers.reusable_plates(parent)
            recipe['reusedPlates']={'parentTaskId':parent['id'],'plan':parent['plan'],'plates':approved}
            for plate in approved:recipe['assetSha256'][plate['raw']]=asset(plate['raw'])[1]
        if data.get('reusePlanTaskId'):
            recipe['reusedPlan']=frozen_plan_recovery(recipe,get(data['reusePlanTaskId']),product['id'])
        if data.get('reuseGeneratedTaskId'):
            parent=get(data['reuseGeneratedTaskId'])
            recipe['reusedGeneration']=frozen_generation_recovery(recipe,parent,product['id'])
            # Canonicalise to the exact original references after SHA equality,
            # so a known completed provider request is never reinterpreted.
            recipe['references']=json.loads(json.dumps(parent['request']['references']))
            recipe['apparelSuite']=json.loads(json.dumps(parent['request']['apparelSuite']))
        if config.get('reuseLifestyleTaskId'):
            if any(data.get(k) for k in ('reusePlanTaskId','reuseGeneratedTaskId','reusePlateTaskId')):
                raise ValueError('种草字段修复不能混用其他接续入口')
            from lifestyle_variety import prepare_recovery
            import image_provider as images
            parent=get(config['reuseLifestyleTaskId'])
            recipe['lifestyleRecovery']=prepare_recovery(recipe,parent,product['id'],asset,lambda job:images.poll({'id':job}))
            recipe['references']=json.loads(json.dumps(parent['request']['references']))
            recipe['apparelSuite']=json.loads(json.dumps(parent['request']['apparelSuite']))
            for url,digest in parent['request'].get('assetSha256',{}).items():
                if asset(url)[1]!=digest:raise ValueError('原种草输入文件已变化')
                recipe['assetSha256'][url]=digest
        overrides=data.get('sourceLayerOverrides')
        if overrides is not None:
            if not recipe.get('preserveSourcePixels') or not isinstance(overrides,dict):raise ValueError('原片图层修订格式无效')
            import image_source_layers
            for slot,layers in overrides.items():
                if not isinstance(slot,str) or not slot.isdigit() or not 0<=int(slot)<count:raise ValueError('图层修订图位无效')
                image_source_layers.validate_layers({'sourceLayers':layers})
            recipe['sourceLayerOverrides']=overrides
    if mode=='repair' and data.get('repairTemplateId'):
        recipe['repairTemplateId']=data['repairTemplateId']
    if mode=='repair':
        import repair_prompt_pipeline
        import provider
        requested=data.get('needOptimize',False)
        if type(requested) is not bool:raise ValueError('修图扩写开关无效')
        objects=repair_prompt_pipeline.normalize_objects(data.get('repairObjects',[{'type':'text','text':brief}]))
        if repair_prompt_pipeline.objects_prompt(objects).strip()!=brief.strip():raise ValueError('修图输入节点与当前要求不一致')
        settings=provider.text_settings()
        recipe.update(repairObjects=objects,repairRequestedOptimize=requested,
            repairSize=repair_prompt_pipeline._size(data.get('repairSize','')),
            repairCapabilities={'builtinProviderExpansion':False,'localTextExpansion':True,
                'source':{'kind':'local_text_optimizer','provider':'Codex Tools',
                          'model':settings.get('model',''),'baseUrl':settings.get('base_url','')}})
    if data.get('generatedColourVariant') is not None:
        import generated_colour_variant as variants
        import official_image_templates as library
        recipe['generatedColourVariant']=variants.freeze(data['generatedColourVariant'],recipe,product['id'],get,asset,library.bind)
        bound=recipe['generatedColourVariant'].get('template')
        if bound:recipe['assetSha256'][bound['url']]=bound['sha256']
    if mode=='repair'and data.get('reusePlateTaskId'):
        import image_source_layers
        parent=get(data['reusePlateTaskId']);previous=parent['request']
        fields=('mode','references','referenceSha256','brief','repairTemplateId','repairObjects','repairRequestedOptimize','repairSize','neutralBackground')
        if (parent['productId']!=product['id']or not recipe.get('preserveSourcePixels')or not previous.get('preserveSourcePixels')
                or any(previous.get(k)!=recipe.get(k)for k in fields)):raise ValueError('修图背景只可供同商品、同原片、同要求和原模板的工具修复复用')
        plate=image_source_layers.reusable_repair_plate(parent)
        expert=parent.get('officialRepair',{});compiled=expert.get('compiledPrompt')
        if not compiled or hashlib.sha256(compiled.encode()).hexdigest()!=expert.get('compiledPromptSha256'):raise ValueError('原修图扩写回执不完整，未冒充复用')
        recipe.update(reusedRepairPlate=plate,reusedRepairPrompt=expert,reusedRepairTaskId=parent['id'])
        recipe['assetSha256'][plate['raw']]=asset(plate['raw'])[1]
    signature=hashlib.sha256(json.dumps(recipe,sort_keys=True).encode()).hexdigest()
    with LOCK, closing(db()) as c, c:
        import codex_handoff
        c.execute('BEGIN IMMEDIATE')
        previous=c.execute('SELECT body FROM tasks WHERE request_id=?',(request_id,)).fetchone()
        if previous:
            old=json.loads(previous[0])
            if old['productId']!=product['id'] or old['signature']!=signature:raise ValueError('请求编号已用于不同输入')
            codex_handoff.record(c, handoff, old['id'], old['status'])
            return old
        if handoff and c.execute(
                """SELECT 1 FROM tasks WHERE product_id=?
                   AND json_extract(body,'$.status') NOT IN ('completed','failed','needs_revision') LIMIT 1""",
                (product['id'],)).fetchone():
            raise ValueError('本款已有图片任务待完成或对账，请核对原任务')
        task={'id':uuid.uuid4().hex,'productId':product['id'],'productName':product.get('name',''),
              'requestId':request_id,'signature':signature,'version':prompts.VERSION,'status':'queued',
              'created':time.time(),'updated':time.time(),'request':recipe,'results':[],'humanReviewed':False,
              'usage':'creative_candidate','skuBinding':None}
        c.execute('INSERT INTO tasks VALUES(?,?,?,?,?)',
                  (task['id'], product['id'], request_id, json.dumps(task, ensure_ascii=False), task['created']))
        codex_handoff.record(c, handoff, task['id'], 'queued')
    launch(task['id'])
    return get(task['id'])


def authorize(task_id,data):
    """Only the server worker can link an exact saved intent to paid submit."""
    task=get(task_id)
    if task.get('status')!='submitting' or task.get('pending')!=data:
        raise ValueError('图片请求与当前持久任务意图不一致')
    return {'imageEngineTaskId':task_id}


def assert_assets(task):
    request=task['request']
    for ref,sha in zip(request['references'],request['referenceSha256']):
        if asset(ref)[1]!=sha:raise ValueError('商品图已改变，原任务未重发')
    for ref,sha in request.get('assetSha256',{}).items():
        if asset(ref)[1]!=sha:raise ValueError('选区或参考图已改变，原任务未重发')
    for template in request.get('officialTemplates',[]):
        if asset(template['url'])[1]!=template['sha256']:raise ValueError('原始模板配图已改变，原任务未重发')


def paid_image(task,prompt,refs,scope=None):
    import image_provider as images
    previous=task['request'].get('reusedGeneration',{}).get('results',[])
    if scope is None and len(task['results'])<len(previous):
        result=json.loads(json.dumps(previous[len(task['results'])]))
        if prompt!=result.get('prompt') or refs!=result.get('references'):
            raise ValueError('原成图的提示词或实际参考顺序已改变，未重发生图')
        result['reusedFromTaskId']=task['request']['reusedGeneration']['parentTaskId']
        return result
    config=require_codex()
    assert_assets(task)
    data={'productId':task['productId'],'slot':'image-engine-'+task['id']+'-'+str(len(task['results']))+('-'+scope if scope else ''),
          'model':config['model'],'prompt':prompt,'references':refs,'size':'1:1','resolution':'1600',
          'expectedReferenceSha256':[asset(r)[1] for r in refs]}
    task.update(status='submitting',pending=data);save(task)
    # The provider layer persists quota + intent before networking. If this call
    # is interrupted, the saved submission must be reconciled, never replayed.
    job=images.submit(data,image_engine_task_id=task['id'],expected_provider='openai')
    task.update(status='generating',providerJobId=job['id']);save(task)
    while job['status'] not in ('completed','failed','cancelled','submission_unknown'):
        time.sleep(8)
        job=images.poll({'id':job['id']})
    if job['status']!='completed' or not job.get('images'):
        raise ValueError(job.get('error') or '模型任务未返回成图')
    first=(job.get('assets') or [{}])[0]
    raw=first.get('sourceAsset') or first
    raw_url=raw.get('url') or job['images'][0]
    original,sha=asset(raw_url)
    # Do not rescue the wrong aspect by adding white bars or cropping the item.
    # Leave the original available for inspection; a mismatch fails delivery.
    aspect_ok=abs(original.width/original.height-1)<.01
    delivered=cache(original.resize((1600,1600),Image.Resampling.LANCZOS)) if aspect_ok else raw
    return {'index':len(task['results']),'providerJobId':job['id'],'raw':raw_url,'rawSha256':sha,
            'image':delivered.get('url',raw_url),'width':original.width,'height':original.height,
            'aspectValid':aspect_ok,'prompt':prompt,'references':refs,'responseReceipt':job.get('responseReceipt')}


def review(task,result,source):
    task['status']='reviewing';save(task)
    if task['request'].get('generatedColourVariant'):
        import generated_colour_variant as variants
        image_sha = asset(result['image'])[1]
        result['review']=variants.inspect(task['request'],result,source,json_model)
        if asset(result['image'])[1] != image_sha:
            raise ValueError('质检时成图文件发生变化')
        result['imageSha256'] = image_sha
        result['productionMethod']='codex-generated-colour-variant'
        result['generatedColourVariant']={k:v for k,v in task['request']['generatedColourVariant'].items() if k!='template'}
        return
    template=result.get('shot',{}).get('officialTemplate')
    check_template=bool(template and task['request'].get('templateExecutionPolicy')=='official-artwork-v1'
                        and not task['request'].get('preserveSourcePixels'))
    template_refs=[template['url']] if check_template else []
    recipe=task['request'].get('apparelSuite',{})
    native_kinds=recipe.get('templatePolicy')=='point-only-v1'
    model_slot=native_kinds and result.get('shot',{}).get('apparelKind')=='model'
    extra_roles=[r for r in result.get('shot',{}).get('apparelReferenceRoles',[])if model_slot and r.get('purpose')in(
        'selected consistent model identity only','selected scene/background reference only')]
    require_model=model_slot and bool(recipe.get('consistency',{}).get('modelReference'))
    require_scene=model_slot and recipe.get('consistency',{}).get('exactScene')is True
    consistency_rule=(' For independent ins lifestyle photography the adult wearer, pose and scene may change; '
        'garment construction/colour remain exact product truth. 'if native_kinds else '')
    if model_slot:
        consistency_rule+=' Additional selected person/scene references are consistency truth, not garment-design truth. '
        if require_model:consistency_rule+='Return modelConsistencyPreserved:bool; a different selected model identity is blocking. '
        if require_scene:consistency_rule+='Return sceneConsistencyPreserved:bool; failure to follow the exact selected scene is blocking. '
    template_rule=(' The LAST reference is the assigned ORIGINAL TEMPLATE ARTWORK, not a product truth image. '
        'Also return templateFollowed:bool and templateComparison:str. Compare its distinctive composition, '
        'photo grouping, type hierarchy and relative visual emphasis to the output, adapting portrait to square. '
        'A single enlarged source crop or generic product photo that discards the assigned design is blocking. '
        'Authorised exact text replaces example text; current garment facts and neutral product colour take priority. '
        'Do not require copying the template product, claims, measurements or brand.' if check_template else '')
    image_sha = asset(result['image'])[1] if task['request']['mode'] == 'suite' else None
    result['review']=json_model(
        'Compare input source and output pixels. Return {blocking:bool,issues:[str],warnings:[str],'
        'identityPreserved:bool,composition:str,colour:str,edges:str,text:str}. Be concrete; '
        'wrong parts, distorted structure, significant colour spill or wrong text are blocking. '
        'Minor antialiasing is a warning. Do not claim human review. Evaluate at full view and thumbnail. '
        'Blueprint geometry is guidance, not visual content to copy. When an extra construction detail reference is supplied, '
        'compare lower-edge fibre count, irregular strand spacing, length, hem seam, gaps and cut shape. '
        'Do not accept a uniform drawn comb, merged fringe curtain, extra fringe rows or clipped hems. '
        'Judge final colour against the source; intentional light and shadow do not justify orange or red recolouring.'+consistency_rule+template_rule,
        {'mode':task['request']['mode'],'brief':task['request']['brief'],'shot':result.get('shot'),
         'expectedCopy':result.get('shot',{}).get('copy',[]),
         'originalPixelGeometry':result.get('pixelGeometry'),
         'verifiedSizeData':recipe.get('sizeChart') if result.get('shot',{}).get('apparelKind')=='size' else None,
         'sharedAcrossAllVariants':result.get('sharedAcrossAllVariants',False),
         'colourPreprocessReceipt':result.get('fidelity'),
         'selectedPersonAndSceneRoles':extra_roles,
         'sourcePixelMode':task['request'].get('preserveSourcePixels',False),
         'assignedOriginalTemplate':({'id':template['id'],'prompt':template['prompt'],'sha256':template['sha256']} if check_template else None),
         'referenceOrder':(['exact_original_crop','delivered_output','full_original'] if result.get('reviewSource') else ['full_original','delivered_output'])},
        [result.get('reviewSource') or source,result['image']]+([source] if result.get('reviewSource') else [])+([result['shot']['constructionAnchor']['url']] if result.get('shot',{}).get('constructionAnchor') else [])+[r['url']for r in extra_roles]+template_refs,
        'independent_image_engine_review')
    if image_sha is not None:
        if asset(result['image'])[1] != image_sha:
            raise ValueError('质检时成图文件发生变化')
        result['imageSha256'] = image_sha
    if type(result['review'].get('blocking')) is not bool or type(result['review'].get('identityPreserved')) is not bool:
        raise ValueError('质检未返回明确结论；保留成图，未自动通过')
    if check_template:
        if type(result['review'].get('templateFollowed')) is not bool:
            raise ValueError('模板执行检查未返回明确结论；保留成图，未自动通过')
        if not result['review']['templateFollowed']:
            result['review']['blocking']=True
            result['review'].setdefault('issues',[]).append('未执行所选官方模板的实际版式')
    for required,key,label in [(require_model,'modelConsistencyPreserved','指定模特'),(require_scene,'sceneConsistencyPreserved','指定场景')]:
        if required and type(result['review'].get(key))is not bool:raise ValueError(label+'一致性检查未返回明确结论')
        if required and result['review'][key]is False:
            result['review']['blocking']=True;result['review'].setdefault('issues',[]).append('未遵循'+label)


def automatic_image_correction(task,result,prompt,refs,source,*,fidelity_source=None):
    """One tool-owned correction; original attempts and reference intent survive."""
    checked=result.get('review',{})
    if not checked.get('blocking')and checked.get('identityPreserved')is True:return result
    if result.get('automaticAttempt',1)>=2 or any(r.get('slot')==result['index']for r in task.get('automaticCorrections',[])):return result
    # Original-pixel modes cannot silently redraw a product to clear a gate.
    if task['request'].get('preserveSourcePixels')or result.get('productionMethod')=='original-pixel-crop':return result
    if not result.get('providerJobId'):return result
    failed=json.loads(json.dumps(result));failed['candidateRole']='superseded_automatic_attempt'
    task.setdefault('attempts',[]).append(failed)
    task['results']=[r for r in task['results']if r is not result]
    task.setdefault('automaticCorrections',[]).append({'slot':result['index'],'reason':checked,'maximumRetries':1,'sourceReferencesUnchanged':True})
    save(task)
    correction=(prompt+'\n\n[AUTOMATIC QUALITY CORRECTION; SAME SOURCE TRUTH]\n'
        'The prior candidate failed the following actual checks. Correct only these defects while preserving '
        'all source-visible product structures, original user intent, garment colour and reference roles. '
        'Do not invent missing facts, replace the product or add text to solve a defect. Return one finished image.\n'
        +json.dumps(checked,ensure_ascii=False))
    retry=paid_image(task,correction,refs,scope='automatic-quality-correction-1')
    retry['automaticAttempt']=2;retry['derivedFromFailedProviderJobId']=failed['providerJobId']
    if result.get('shot'):retry['shot']=result['shot']
    if result.get('templateExecution'):retry['templateExecution']=result['templateExecution']
    task['results'].append(retry);save(task)
    if fidelity_source:
        import image_fidelity as fidelity
        truth,source_mask,source_receipt=fidelity_source
        generated,_=asset(retry['raw']);result_mask,result_receipt=fidelity.selection(generated,task['request']['fidelityPolicy']['garmentKind'])
        corrected,colour_receipt=fidelity.calibrate(truth,generated,source_mask,result_mask)
        native=cache(corrected);delivered=cache(corrected.resize((1600,1600),Image.Resampling.LANCZOS))if retry['aspectValid']else native
        retry.update(rawModelOutput=retry['raw'],raw=native['url'],image=delivered['url'],
            fidelity={'colour':colour_receipt,'sourceMask':cache(source_mask),'resultMask':cache(result_mask),'sourceSelection':source_receipt,'resultSelection':result_receipt})
        save(task)
    review(task,retry,source);save(task)
    return retry


def process(task_id):
    task=get(task_id)
    try:
        req=task['request'];assert_assets(task)
        task['status']='planning' if req['mode']=='suite' else 'processing';save(task)
        if req['mode']=='suite':
            official=req.get('officialTemplates')
            if not official and not(req.get('apparelSuite',{}).get('templatePolicy')=='point-only-v1'
                    and req['apparelSuite']['counts']['seller']==0):
                raise ValueError('新套图必须使用已核验官方配图与原文，不能回退简化蓝图')
            planning_refs=req['references']+[r['url'] for r in official]
            planning_context={'count':req['count'],'brief':req['brief'],'noText':req['noText'],
                'productReferenceCount':len(req['references']),
                'officialTemplateReferences':[{'id':r['id'],'style':r['style'],'referenceImageIndex':len(req['references'])+i+1,
                    'originalPrompt':r['prompt']} for i,r in enumerate(official)]}
            if req.get('sourceVariant'):
                planning_context['sourceColour']={k:req['sourceVariant'][k]for k in('color','children','referenceType')}
            direction=prompts.director_for(req)
            if req.get('apparelSuite'):
                import apparel_suite
                planning_context.update(apparel_suite.planning_context(req['apparelSuite']))
            if req.get('lifestyleVarietyPolicy'):
                from lifestyle_variety import DIRECTOR_RULE
                direction+='\n'+DIRECTOR_RULE
            direction+='\n[SHOT WIRE CONTRACT] Every shot evidence MUST be a nonempty plain string describing source-visible facts; keep optional detail crop records outside that string. copy MUST be a JSON list of zero to FOUR plain strings, each at most 120 characters. If the layout has more text zones, leave optional captions empty instead of returning a fifth string. No rich text objects, nested copy maps or protocol instructions in public copy. These schema constraints do not waive garment truth or composition checks.'
            if req.get('preserveSourcePixels'):
                planning_context['preserveSourcePixels']=True
                direction+='\nORIGINAL-PIXEL MODE: AI draws only a background and optional exact type, then real source photo layers are composited. For every NON-main shot return sourceLayers (1–4 layers), each {kind:wearer|detail,box:[x,y,width,height],crop:[x,y,width,height],shape:rectangle|ellipse}. Coordinates normalized to 0..1. The crop is a real region of this shot source photo, never an imagined detail. wearer retains the actual photographed person/product with background removed and aspect preserved; detail is a real photo crop filling the box. Infer these layer positions from the assigned original template artwork, not a generic layout grid. Keep fine hems fully visible on full-product cards. Vary full outfit, real pocket and hem detail placement to answer distinct buyer questions. Main needs no sourceLayers. No pose changes, new angles or generated product surfaces are available in this mode.'
            plan=(json.loads(json.dumps(req['lifestyleRecovery']['plan'])) if req.get('lifestyleRecovery') else
                  json.loads(json.dumps(req['reusedGeneration']['plan'])) if req.get('reusedGeneration') else
                  json.loads(json.dumps(req['reusedPlan']['proposal'])) if req.get('reusedPlan') else
                  json.loads(json.dumps(req['reusedPlates']['plan'])) if req.get('reusedPlates') else
                  __import__('shared_size_chart').size_plan(req['apparelSuite']) if req.get('sizeLayoutCompiler')=='layout-contract-v2' and req['apparelSuite']['counts']['size']==req['count'] else
                  json_model(direction,planning_context,planning_refs,'official_original_template_suite_plan'))
            task['plannerProposal']=plan;save(task)
            for slot,layers in req.get('sourceLayerOverrides',{}).items():plan['shots'][int(slot)]['sourceLayers']=layers
            # A recovered generation plan already passed the full normalizer
            # before its original provider submissions. Reinterpreting injected
            # default textBox as a user-authored box is not idempotent.
            task['plan']=(json.loads(json.dumps(plan)) if req.get('reusedGeneration') or req.get('lifestyleRecovery') else
                normalize_shots(json.loads(json.dumps(plan)),req['count'],len(req['references']),req['noText']));save(task)
            if req.get('apparelSuite'):
                task['plan']=apparel_suite.normalize_plan(task['plan'],req['apparelSuite']);save(task)
            if req.get('sizeLayoutCompiler')=='layout-contract-v2':
                from shared_size_chart import layout_contract
                for card in task['plan']['shots']:
                    if card.get('apparelKind')=='size':
                        card['sizeLayoutPlannerOriginal']={k:card.get(k) for k in ('scene','camera')}
                        card['scene']=layout_contract(req['apparelSuite']['sizeChart'])
                        card['camera']='Use the frozen preview and exact source measurements; no product redraw.'
                save(task)
            plan=task['plan']
            if req.get('lifestyleVarietyPolicy'):
                from lifestyle_variety import validate_plan
                validate_plan(plan)
            recovery=req.get('lifestyleRecovery')
            if recovery:
                task['attempts']=json.loads(json.dumps(recovery['attempts']))
                task['automaticCorrections']=json.loads(json.dumps(recovery['automaticCorrections']))
                for index in recovery['revisionSlots']:
                    task['attempts'].append({**json.loads(json.dumps(recovery['results'][index])),
                        'candidateRole':'superseded_lifestyle_set_attempt'})
                    task['automaticCorrections'].append({'slot':index,'reason':recovery['originalSetReview'],
                        'maximumRetries':1,'sourceReferencesUnchanged':True,'cause':recovery['reason']})
                save(task)
            template_index=0
            for shot in plan['shots']:
                template=(shot.get('officialTemplate') if req.get('apparelSuite') else
                          None if shot['layout']=='main' else official[template_index%len(official)])
                if template:template_index+=1
                source=req['references'][shot['sourceIndex']]
                render_prompt=None;retry_refs=None;fidelity_source=None
                if template:shot['officialTemplate']=template
                save(task)
                if recovery and shot['index'] not in recovery['revisionSlots']:
                    kept=json.loads(json.dumps(recovery['results'][shot['index']]))
                    if asset(kept['image'])[1]!=kept['imageSha256']:raise ValueError('复用种草图片已改变')
                    kept['reusedFromTaskId']=recovery['parentTaskId'];task['results'].append(kept);save(task);continue
                previous=req.get('reusedGeneration',{}).get('results',[])
                if shot['index']<len(previous) and previous[shot['index']].get('review',{}).get('blocking')is False:
                    kept=json.loads(json.dumps(previous[shot['index']]))
                    kept['reusedFromTaskId']=req['reusedGeneration']['parentTaskId'];task['results'].append(kept);save(task);continue
                if shot.get('apparelKind')=='size' and req.get('sizeRenderingPolicy')=='verified-typeset-v1':
                    from shared_size_chart import render_candidate
                    chart=req['apparelSuite']['sizeChart'];preview=chart.get('preview')
                    display=asset(source)[0]
                    if preview:
                        display,digest=asset(preview['url'])
                        if digest!=preview['sha256']:raise ValueError('尺码图已冻结预览内容变化')
                    result=render_candidate(chart,display,shot['index'],cache)
                elif req.get('preserveSourcePixels') and (not (req.get('apparelSuite') and shot['layout']=='detail' and not shot['copy'])
                        or __import__('image_source_layers').tiled_details(shot)):
                    import image_source_layers
                    result=image_source_layers.render(task,plan,shot,source,template,paid_image)
                elif shot['layout']=='detail' and not shot['copy'] and (not template or req.get('preserveSourcePixels')):
                    original,sha=asset(source)
                    x,y,w,h=shot['sourceCrop'];box=(round(x*original.width),round(y*original.height),round((x+w)*original.width),round((y+h)*original.height))
                    cropped=original.crop(box)
                    # A detail may crop intentionally, but may never stretch.
                    edge=min(cropped.size);left=(cropped.width-edge)//2;top=(cropped.height-edge)//2
                    cropped=cropped.crop((left,top,left+edge,top+edge))
                    raw=cache(cropped);delivered=cache(cropped.resize((1600,1600),Image.Resampling.LANCZOS))
                    effective_box=(box[0]+left,box[1]+top,box[0]+left+edge,box[1]+top+edge)
                    result={'index':len(task['results']),'image':delivered['url'],'raw':raw['url'],'reviewSource':raw['url'],
                            'sourceSha256':sha,'sourceCropPixels':list(effective_box),'requestedSourceCropPixels':list(box),'productionMethod':'original-pixel-crop',
                            'pixelGeometry':{'method':'square_subcrop_then_uniform_lanczos','sourceBox':list(effective_box),
                                'sourceSize':[edge,edge],'outputSize':[1600,1600],'scaleX':1600/edge,'scaleY':1600/edge,'stretched':False},
                            'officialTemplate':template,
                            'aspectValid':True,'warnings':['原片细节分辨率有限，放大不增加真实细节'] if edge<800 else []}
                else:
                    refs=[source]+([template['url']] if template else [])
                    if req.get('fidelityPolicy'):
                        import image_fidelity as fidelity
                        truth,_=asset(source)
                        source_mask,mask_receipt=fidelity.selection(truth,req['fidelityPolicy']['garmentKind'])
                        anchor,anchor_box=fidelity.construction_anchor(truth,source_mask)
                        prior_anchor=shot.get('constructionAnchor') if req.get('reusedGeneration') and shot['index']<len(previous) else None
                        if prior_anchor:
                            prior_pixels,_=asset(prior_anchor['url'])
                            if prior_pixels.convert('RGB').tobytes()!=anchor.convert('RGB').tobytes() or prior_pixels.size!=anchor.size:
                                raise ValueError('原生图结构参考像素已改变，未重发生图')
                            anchor_asset=prior_anchor
                        else:anchor_asset=cache(anchor)
                        refs.append(anchor_asset['url'])
                        shot['constructionAnchor']={'url':anchor_asset['url'],'sha256':anchor_asset['sha256'],'sourcePixelBox':list(anchor_box)}
                        save(task)
                    if req.get('apparelSuite'):
                        records=apparel_suite.render_references(req['apparelSuite'],shot,
                            construction_anchor=shot.get('constructionAnchor'))
                        shot['apparelReferenceRoles']=records;refs=[r['url'] for r in records];save(task)
                    render_prompt=prompts.compile_shot(plan,shot,req['brief'],no_text=req['noText']);retry_refs=list(refs)
                    result=paid_image(task,render_prompt,refs)
                    if req.get('fidelityPolicy'):
                        # Keep the returned provider image even if local masking
                        # or calibration subsequently fails.
                        task['results'].append(result);save(task)
                        generated,_=asset(result['raw'])
                        try:
                            generated_mask,gen_receipt=fidelity.selection(generated,req['fidelityPolicy']['garmentKind'])
                            corrected,colour_receipt=fidelity.calibrate(truth,generated,source_mask,generated_mask)
                            native=cache(corrected);final=cache(corrected.resize((1600,1600),Image.Resampling.LANCZOS)) if result['aspectValid'] else native
                            result.update(rawModelOutput=result['raw'],raw=native['url'],image=final['url'],
                                fidelity={'colour':colour_receipt,'sourceMask':cache(source_mask),'resultMask':cache(generated_mask),
                                          'sourceSelection':mask_receipt,'resultSelection':gen_receipt})
                        except ValueError as error:
                            if (not template or req.get('templateExecutionPolicy')!='official-artwork-v1'
                                    or str(error)!='衣料边界与独立衣料证据仍冲突，不能自动换色'):raise
                            # This is an optional colour transformation, not a
                            # visual verdict. Never apply a conflicting mask to
                            # a multi-panel poster; retain native pixels and
                            # require the same independent colour/identity QA.
                            result['fidelity']={'colourCorrectionApplied':False,'nativePixelsRetained':True,
                                'localCorrectionError':str(error),'reviewRequired':True,'qualityApproved':False}
                        save(task)
                        fidelity_source=(truth,source_mask,mask_receipt)
                result['shot']=shot
                if recovery:result['automaticAttempt']=2
                if template:
                    result['templateExecution']={'policy':req.get('templateExecutionPolicy'),
                        'mode':'source-pixel-layout' if req.get('preserveSourcePixels') else 'official-template-generation',
                        'templateId':template['id'],'imageSha256':template['sha256'],'promptSha256':template['promptSha256'],
                        'artworkReferenceSubmitted':bool(render_prompt and template['url'] in (retry_refs or [])),
                        'completeOriginalPromptSubmitted':bool(render_prompt and template['prompt'] in render_prompt)}
                if not any(item is result for item in task['results']):task['results'].append(result)
                save(task)
                review(task,result,source);save(task)
                if render_prompt:automatic_image_correction(task,result,render_prompt,retry_refs,source,fidelity_source=fidelity_source)
        elif req['mode']=='repair':
            repair_refs=req['references']
            if req.get('generatedColourVariant'):
                import generated_colour_variant as variants
                prompt=variants.compile_prompt(req);repair_refs=variants.references(req)
                task['officialRepair']={'compiledPrompt':prompt,'generatedColourVariant':req['generatedColourVariant'],'needOptimize':False};save(task)
            elif req.get('reusedRepairPrompt'):
                task['officialRepair']=req['reusedRepairPrompt'];task['reusedPromptFromTaskId']=req['reusedRepairTaskId']
                prompt=task['officialRepair']['compiledPrompt'];save(task)
            elif req.get('repairTemplateId'):
                import official_image_templates as library
                prompt,expert=library.expert_prompt(req['repairTemplateId'],req['brief'],
                    [__import__('visual_studio').encode_reference(r) for r in req['references']],
                    req.get('repairSize',''),objects=req.get('repairObjects'),
                    need_optimize=req.get('repairRequestedOptimize',False),image_capabilities=req.get('repairCapabilities'))
                task['officialRepair']=expert;save(task)
            else:
                import repair_prompt_pipeline
                prompt,expert=repair_prompt_pipeline.compile_repair(
                    {'id':None,'name':req['editTask'],'config':{'prompt':'','systemPrompt':prompts.EDIT_TASKS[req['editTask']]}},
                    req['brief'],[__import__('visual_studio').encode_reference(r) for r in req['references']],
                    req.get('repairSize',''),objects=req.get('repairObjects'),
                    need_optimize=req.get('repairRequestedOptimize',False),image_capabilities=req.get('repairCapabilities'))
                task['officialRepair']=expert;save(task)
            if req.get('preserveSourcePixels'):
                import image_source_layers
                result=image_source_layers.repair(task,prompt,req['references'],paid_image)
            else:
                result=paid_image(task,prompt,repair_refs)
            task['results'].append(result);save(task);review(task,result,req['references'][0])
            if not req.get('preserveSourcePixels'):automatic_image_correction(task,result,prompt,repair_refs,req['references'][0])
        else:
            from colour_transfer import transfer
            opt=req['options'];source,_=asset(req['references'][0])
            if opt.get('autoMask') is True:
                import garment_colour_mask
                protection=asset(opt['protected'])[0] if opt.get('protected') else Image.new('L',source.size)
                mask,receipt=garment_colour_mask.prepare(source,opt['garmentKind'],protected=protection)
                mask_url=cache(mask)['url']
                task['selection']={'mask':mask_url,'receipt':receipt};save(task)
            else:
                mask_url=opt['mask'];mask,_=asset(mask_url)
            target=tuple(int(opt['target'][i:i+2],16) for i in (1,3,5))
            if opt.get('colourRenderer')=='official-glsl':
                from official_colour_result import validate
                output,_=asset(opt['renderedImage'])
                stats=validate(source,mask,output,opt['shaderReceipt'])
                task['originalShaderReceipt']=opt['shaderReceipt']
            else:
                output,stats=transfer(source,mask,target,
                    protected=asset(opt['protected'])[0] if opt.get('protected') else None,
                    reference=asset(opt['referencePhoto'])[0] if opt.get('referencePhoto') else None,
                    reference_mask=asset(opt['referenceMask'])[0] if opt.get('referenceMask') else None)
            local=({'url':opt['renderedImage'],'sha256':asset(opt['renderedImage'])[1]} if opt.get('colourRenderer')=='official-glsl' else cache(output))
            result={'index':0,'image':local['url'],'raw':local['url'],'sha256':local['sha256'],
                    'stats':stats,'aspectValid':True}
            task['results'].append(result);save(task)
            task['status']='reviewing';save(task)
            import image_tools
            verified=image_tools.colour_review(task['productId'],req['references'][0],mask_url,local['url'],opt['target'],
                quality_check_id=opt.get('qualityCheckId'),selection_task_id=opt.get('selectionTaskId'),product_name=task.get('productName',''))
            task['colourVerification']=verified
            result['review']={**verified,'blocking':verified.get('blocking',True),'identityPreserved':verified.get('identityPreserved',False)}
            save(task)
            if opt.get('polish') is True and verified.get('passed') is not True:
                task['polishSkipped']={'reason':'初始换色未通过，未付费修整不合格选区或颜色','issues':verified.get('issues',[])};save(task)
            if opt.get('polish') is True and verified.get('passed') is True:
                refs=[local['url'],req['references'][0],mask_url]
                polished=paid_image(task,prompts.compile_edit('colour-polish',req['brief']),refs)
                # Native mask support is unverified. Composite only the explicit
                # selection back to the original, so model drift cannot escape it.
                generated,_=asset(polished['raw'])
                if generated.size!=source.size:generated=generated.resize(source.size,Image.Resampling.LANCZOS)
                effective=mask.convert('L')
                if opt.get('protected'):
                    from PIL import ImageChops
                    effective=ImageChops.subtract(effective,asset(opt['protected'])[0].convert('L'))
                merged=Image.composite(generated.convert('RGB'),source.convert('RGB'),effective)
                if 'A' in source.getbands():merged.putalpha(source.getchannel('A'))
                polished['image']=cache(merged)['url'];polished['localBase']=local['url']
                polished['rawModelOutput']=polished['raw'];polished['raw']=polished['image']
                polished['aspectValid']=True
                task['results'].append(polished);save(task)
                # This is a new image. The initial GPU approval cannot approve
                # a generated repair, nor can ordinary source-hue QC judge it.
                final_check=image_tools.colour_review(task['productId'],req['references'][0],mask_url,polished['image'],opt['target'],
                    selection_task_id=opt.get('selectionTaskId'),product_name=task.get('productName',''))
                polished['review']=final_check;task['polishColourVerification']=final_check;save(task)
        if req.get('lifestyleVarietyPolicy'):
            from lifestyle_variety import review_set
            task['status']='reviewing';save(task)
            task['lifestyleSetReview']=review_set(task,json_model);save(task)
        blocking=bool(task.get('lifestyleSetReview',{}).get('blocking')) or any(not r.get('aspectValid',True) or r.get('review',{}).get('blocking') or not r.get('review',{}).get('identityPreserved',False) for r in task['results'])
        task.update(status='needs_revision' if blocking else 'completed',completed=time.time())
        task.pop('pending',None);save(task)
    except Exception as error:
        # Preserve actual outputs and provider handles, and never retry a paid
        # call merely because a response or review failed.
        task.update(status='submission_unknown' if task.get('status')=='submitting' else 'failed',error=str(error)[:2000])
        save(task)
    finally:
        with LOCK:ACTIVE.discard(task_id)


def launch(task_id):
    with LOCK:
        if task_id in ACTIVE:return
        ACTIVE.add(task_id)
        threading.Thread(target=process,args=(task_id,),daemon=True,name='image-engine-'+task_id[:8]).start()


def poll(task_id):
    """Read/reconcile an existing task only; never starts another generation."""
    task=get(task_id)
    if task_id not in ACTIVE and task.get('leaseUntil',0)<time.time() and (
            task['status'] not in TERMINAL or task['status'] in ('interrupted','submission_unknown')):
        import image_provider
        handle=task.get('providerJobId') if task.get('status')=='generating' else None
        if not handle and task.get('pending'):
            with closing(image_provider.storage()) as c:
                row=c.execute('SELECT id FROM submission_attempts WHERE json_extract(body,\'$.imageEngineTaskId\')=? AND slot=? ORDER BY rowid DESC LIMIT 1',
                              (task['id'],task['pending'].get('slot'))).fetchone()
            if row:handle=row[0]
        if handle:
            snapshot=image_provider.poll({'id':handle})
            task['providerSnapshot']=snapshot
            if snapshot.get('status')=='completed' and snapshot.get('images'):
                if not any(r.get('providerJobId')==snapshot['id'] for r in task['results']):
                    first=(snapshot.get('assets') or [{}])[0];raw=first.get('sourceAsset') or first
                    task['results'].append({'index':len(task['results']),'providerJobId':snapshot['id'],
                        'image':snapshot['images'][0],'raw':raw.get('url') or snapshot['images'][0],
                        'recovered':True,'reviewPending':True})
        task.update(status='interrupted',error='工作进程曾中断；已查询并保存原任务可恢复结果，未重新生图；剩余图位与质检需要明确的后续任务')
        save(task)
    return task
