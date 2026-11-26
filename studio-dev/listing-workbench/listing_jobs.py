"""Account-owned Listing jobs that survive a closed browser without paid replay.

Final results and workspace replacement share one SQLite transaction. A private
atomic result receipt also covers interruption between the model response and
that transaction. Reading/recovering a job never calls the model again.
"""
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

import app_paths
import listing_locks
import provider

DB=app_paths.DATA/'studio.sqlite3'
LOCK=threading.RLock()
ACTIVE=set()
RUNTIME=uuid.uuid4().hex
THREAD_FACTORY=threading.Thread
UNRESOLVED={'queued','running','ready','needs_review'}
FIELDS=listing_locks.FIELDS


def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':')).encode('utf-8')).hexdigest()


@contextmanager
def database():
    DB.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB,timeout=30)
    conn.row_factory=sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS listing_generation_jobs(
        id TEXT PRIMARY KEY,owner INTEGER NOT NULL,product_id TEXT NOT NULL,
        market TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,
        updated REAL NOT NULL,request_id TEXT NOT NULL,body TEXT NOT NULL,
        UNIQUE(owner,request_id))''')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def account(owner):
    if type(owner) is not int or owner<=0 or app_paths.WORKER_USER and owner!=app_paths.WORKER_USER:
        raise ValueError('文案任务账号无效')


def workspace(conn):
    row=conn.execute('SELECT revision,body FROM workspace WHERE id=1').fetchone()
    if row is None:raise ValueError('当前账号工作空间尚无资料')
    return row['revision'],json.loads(row['body'])


def product_for(state,pid):
    found=[p for p in state.get('products',[]) if p.get('id')==pid]
    if len(found)!=1:raise ValueError('当前账号没有该产品，或产品编号重复')
    return found[0]


def input_snapshot(state,product,market):
    # Exclude only our own progress/history writes. Other product facts, pending
    # drafts, the current copy (including locks) and reviews remain protected.
    facts={k:v for k,v in product.items() if k not in
           ('listingGeneration','listingGenerationHistory','copyHistory')}
    return {'product':copy.deepcopy(facts),'copy':copy.deepcopy(state.get('copy',{}).get(product['id']+'-'+market)),
            'review':copy.deepcopy(state.get('reviews',{}).get(product['id']))}


def owned(conn,owner,jid):
    row=conn.execute('SELECT body FROM listing_generation_jobs WHERE id=? AND owner=?',(jid,owner)).fetchone()
    if row is None:raise ValueError('当前账号没有此文案任务')
    return json.loads(row['body'])


def save(conn,job):
    job['updatedAt']=time.time()
    conn.execute('UPDATE listing_generation_jobs SET status=?,updated=?,body=? WHERE id=? AND owner=?',
        (job['status'],job['updatedAt'],json.dumps(job,ensure_ascii=False),job['id'],job['owner']))


def result_path(job):
    if not re.fullmatch(r'[a-f0-9]{32}',job['id']):raise ValueError('文案任务编号无效')
    return DB.parent/'listing-generation-results'/(job['id']+'.json')


def save_result_receipt(job,result):
    path=result_path(job)
    path.parent.mkdir(parents=True,exist_ok=True)
    record={'version':1,'jobId':job['id'],'owner':job['owner'],'productId':job['productId'],
        'market':job['market'],'inputDigest':job['inputDigest'],'result':result,'savedAt':stamp()}
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(record,stream,ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_result_receipt(job):
    path=result_path(job)
    if not path.is_file():return None
    try:
        record=json.loads(path.read_text(encoding='utf-8'))
        if (record.get('version')!=1 or record.get('jobId')!=job['id'] or record.get('owner')!=job['owner']
                or record.get('productId')!=job['productId'] or record.get('market')!=job['market']
                or record.get('inputDigest')!=job['inputDigest']):return None
        result=record.get('result')
        if not isinstance(result,dict) or not isinstance(result.get('copy'),dict):return None
        return result
    except (OSError,ValueError,TypeError):return None


def public(job,*,include_result=False,reused=False):
    result={key:job[key] for key in ('id','productId','market','status','stage','message','createdAt',
        'updatedAt','error','workspaceRevision','appliedAt','responseReceipt') if key in job}
    result.update(resultAvailable=isinstance(job.get('result'),dict),applied=job['status']=='applied',reused=reused)
    if isinstance(job.get('reviewRecovery'), dict):
        recovery = job['reviewRecovery']
        result['reviewRecovery'] = {key:copy.deepcopy(recovery[key]) for key in
            ('id','scope','phase','authorizedAt','originalError','receipts','stageResponses','error','continuation') if key in recovery}
    if isinstance(job.get('formatRecovery'), dict):
        recovery = job['formatRecovery']
        result['formatRecovery'] = {key:copy.deepcopy(recovery[key]) for key in
            ('id','scope','phase','authorizedAt','originalError','receipts','stageResponses','error') if key in recovery}
    if isinstance(job.get('exhaustedContinuation'), dict):
        continuation = job['exhaustedContinuation']
        result['exhaustedContinuation'] = {key:copy.deepcopy(continuation[key]) for key in
            ('id','scope','phase','requestId','authorizedAt','originalError','receipts','stageResponses',
             'error','pendingOperations','limits','bulletsSha256','bulletSha256s') if key in continuation}
    if include_result and result['resultAvailable']:
        result['result']=copy.deepcopy(job['result'])
        if job['status']=='applied' and isinstance(job.get('appliedCopy'),dict):
            result['result']['copy']=copy.deepcopy(job['appliedCopy'])
    return result


def write_outcome(conn,state,revision,job,*,initial=False):
    try:product=product_for(state,job['productId'])
    except ValueError:return revision
    current=(product.get('listingGeneration') or {}).get(job['market'])
    if not initial and (not isinstance(current,dict) or current.get('jobId')!=job['id']):return revision
    if initial and current:
        product.setdefault('listingGenerationHistory',[]).append({'market':job['market'],'date':stamp(),
            'source':'后台文案任务接续前记录','outcome':copy.deepcopy(current)})
    status='completed' if job['status']=='applied' else 'failed' if job['status']=='failed' else \
        'uncertain' if job['status'] in ('ready','needs_review') else 'running'
    product.setdefault('listingGeneration',{})[job['market']]={'status':status,'jobId':job['id'],
        'message':job['message'],'at':stamp(),'resultAvailable':isinstance(job.get('result'),dict)}
    revision+=1
    conn.execute('UPDATE workspace SET revision=?,body=? WHERE id=1',
        (revision,json.dumps(state,ensure_ascii=False)))
    job['workspaceRevision']=revision
    return revision


def apply_result(conn,job):
    if job['status']=='applied':return
    if not isinstance(job.get('result'),dict):raise ValueError('此任务尚无可恢复的完成稿；请先核对原模型回执，不要重复生成')
    revision,state=workspace(conn)
    try:
        product=product_for(state,job['productId'])
        matches=digest(input_snapshot(state,product,job['market']))==job['inputDigest']
    except ValueError:
        product=None;matches=False
    if not matches:
        job.update(status='ready',stage='完成稿已保存，等待核对',
            message='生成期间产品资料、文案、字段锁或审核状态已变化，未覆盖当前内容；完成稿可查看和下载')
        write_outcome(conn,state,revision,job)
        save(conn,job)
        return
    key=job['productId']+'-'+job['market']
    previous=copy.deepcopy(state)
    current=state.setdefault('copy',{}).get(key) or {}
    candidate=job['result'].get('copy')
    if not isinstance(candidate,dict) or not isinstance(candidate.get('title'),str) or not candidate['title'].strip():
        raise ValueError('文案任务缺少有效完成稿，未替换当前内容')
    if key in state['copy']:
        product.setdefault('copyHistory',[]).append({'market':job['market'],'date':stamp(),
            'source':'生成替换前自动备份','copy':copy.deepcopy(state['copy'][key]),'jobId':job['id']})
    state['copy'][key]=listing_locks.merge(current,candidate)
    if isinstance(product.get('pendingCopy'),dict):product['pendingCopy'].pop(job['market'],None)
    product['status']='制作中'
    state.setdefault('reviews',{}).setdefault(job['productId'],{}).update(copy=False,note='生成文案已更新，待审核')
    listing_locks.validate_state(previous,state)
    job.pop('error',None)
    job.update(status='applied',stage='已更新当前文案',message='已更新当前 Listing 文案',appliedAt=stamp(),
               appliedCopy=copy.deepcopy(state['copy'][key]))
    revision=write_outcome(conn,state,revision,job)
    # If a stale client removed only our progress marker, the same input may
    # still match. Persist the replacement and restore its authoritative job ID.
    if job['id']!=(product.get('listingGeneration',{}).get(job['market']) or {}).get('jobId'):
        revision=write_outcome(conn,state,revision,job,initial=True)
    job['workspaceRevision']=revision
    save(conn,job)


def safe_apply_result(conn,job):
    """Replacement and its job outcome must commit together, or neither does."""
    previous=copy.deepcopy(job)
    conn.execute('SAVEPOINT listing_apply')
    try:
        apply_result(conn,job)
    except Exception as error:
        conn.execute('ROLLBACK TO listing_apply')
        conn.execute('RELEASE listing_apply')
        job.clear();job.update(previous)
        job.update(status='ready',stage='完成稿已保存，替换待处理',
            message='完成稿已保存，当前内容未覆盖；请查看任务结果',error=str(error)[:2000])
        revision,state=workspace(conn)
        write_outcome(conn,state,revision,job)
        save(conn,job)
    else:
        conn.execute('RELEASE listing_apply')


def recover(conn,job):
    if job['status'] not in ('queued','running','needs_review') or job.get('result'):
        return
    if job['id'] in ACTIVE and job.get('runOwner')==RUNTIME:
        return
    result=load_result_receipt(job)
    if result is not None:
        if isinstance(job.get('exhaustedContinuation'),dict):
            job['exhaustedContinuation'].update(phase='completed',reconciledFromResultReceiptAt=stamp())
        job.update(status='ready',result=result,responseReceipt=result.get('responseReceipt'),
            stage='已找回完成稿',message='已从持久回执找回完成稿')
        safe_apply_result(conn,job)
    elif job['status'] in ('queued','running'):
        if isinstance(job.get('reviewRecovery'),dict):
            job['reviewRecovery'].update(phase='interrupted_unknown',interruptedAt=stamp())
        if isinstance(job.get('exhaustedContinuation'),dict):
            job['exhaustedContinuation'].update(phase='interrupted_unknown',interruptedAt=stamp())
        job.update(status='needs_review',stage='执行中断，结果待核对',
            message='上次生成进程已中断，未自动重复请求模型；请先核对原模型回执')
        revision,state=workspace(conn)
        write_outcome(conn,state,revision,job)
        save(conn,job)


def start(owner,data,*,handoff=None):
    import codex_handoff
    account(owner)
    if not isinstance(data,dict) or set(data)-{'productId','market','requestId','expectedRevision'}:
        raise ValueError('文案任务参数无效')
    if 'expectedRevision' in data and (type(data['expectedRevision']) is not int or data['expectedRevision']<0):
        raise ValueError('工作空间版本须为非负整数')
    pid=data.get('productId');market=data.get('market')
    if not isinstance(pid,str) or not pid or market not in provider.MARKET_LANGUAGES:
        raise ValueError('请选择有效产品和站点')
    request=data.get('requestId') or uuid.uuid4().hex
    if not isinstance(request,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',request):
        raise ValueError('文案请求编号无效')
    with LOCK,database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing=conn.execute('SELECT body FROM listing_generation_jobs WHERE owner=? AND request_id=?',(owner,request)).fetchone()
        if existing:
            job=json.loads(existing['body'])
            if job['productId']!=pid or job['market']!=market:raise ValueError('此请求编号属于其他产品或站点')
            recover(conn,job)
            codex_handoff.record(conn,handoff,job['id'],'reused')
            return public(job,include_result=True,reused=True)
        existing=conn.execute('SELECT body FROM listing_generation_jobs WHERE owner=? AND product_id=? AND market=? '
            "AND status IN ('queued','running','ready','needs_review') ORDER BY created DESC,id DESC LIMIT 1",(owner,pid,market)).fetchone()
        if existing:
            job=json.loads(existing['body']);recover(conn,job)
            codex_handoff.record(conn,handoff,job['id'],'reused')
            return public(job,include_result=True,reused=True)
        revision,state=workspace(conn)
        if data.get('expectedRevision') is not None and data['expectedRevision']!=revision:
            raise ValueError('工作空间版本已变化，请保存并刷新当前资料后再生成')
        product=product_for(state,pid)
        import discovery_intake
        discovery_intake.assert_web_available(DB.parent, owner, pid)
        # Research intake and standalone copy have different evidence requirements.
        from codex_operations import selection_required
        if selection_required(owner, product, market):
            raise ValueError('本款来自选品流程，请在任务中心沿原选品任务接续或核对文案')
        previous=(product.get('listingGeneration') or {}).get(market) or {}
        if previous.get('status')=='uncertain':
            raise ValueError('上次文案生成结果未知，请先核对原模型回执，不能重复付费生成')
        if not product.get('name') or not any(isinstance(product.get(k),str) and product[k].strip()
                for k in ('material','dimensions','features','style')):
            raise ValueError('尚无可用产品信息，请先采集货源资料或填写产品特征')
        if len(listing_locks.locked(state.get('copy',{}).get(pid+'-'+market)))==len(FIELDS):
            raise ValueError('全部文案字段已锁定，未调用模型')
        settings=provider.text_settings()
        if not settings.get('api_key') or not settings.get('model'):
            raise ValueError('请先在模型设置中配置文案服务密钥和模型 ID')
        snapshot=input_snapshot(state,product,market)
        job={'id':uuid.uuid4().hex,'owner':owner,'productId':pid,'market':market,'status':'queued',
            'stage':'已排队，关闭页面后继续','message':'文案任务已保存，关闭页面后仍会继续',
            'createdAt':time.time(),'updatedAt':time.time(),'requestId':request,'runOwner':RUNTIME,
            'inputDigest':digest(snapshot),'inputSnapshot':snapshot,'sourceRevision':revision}
        conn.execute('INSERT INTO listing_generation_jobs VALUES(?,?,?,?,?,?,?,?,?)',
            (job['id'],owner,pid,market,job['status'],job['createdAt'],job['updatedAt'],request,json.dumps(job,ensure_ascii=False)))
        codex_handoff.record(conn,handoff,job['id'],'queued')
        write_outcome(conn,state,revision,job,initial=True)
        save(conn,job)
        ACTIVE.add(job['id'])
    try:
        THREAD_FACTORY(target=execute,args=(owner,job['id']),daemon=True,name='listing-'+job['id'][:8]).start()
    except Exception:
        ACTIVE.discard(job['id'])
        with LOCK,database() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current=owned(conn,owner,job['id']);recover(conn,current);job=current
    return public(job,reused=False)


def review_recovery_bundle(conn, job, receipts):
    """Bind complete account-local legacy responses to one failed task before any new model call."""
    if not isinstance(receipts,dict) or set(receipts)!={'initial','polish','review'}:
        raise ValueError('请提供原初稿、润色和审校三份回执及SHA256')
    original_error=(job.get('reviewRecovery') or {}).get('originalError',job.get('error',''))
    initial_match=re.findall(r'初稿回执 ([a-f0-9]{32})',original_error)
    if len(set(initial_match))!=1 or '五点语义审校标记的问题无法与原文核对' not in original_error:
        raise ValueError('原任务不是已返回的审校引用错误，不能重发未知模型阶段')
    folder=(DB.parent/'listing-responses').resolve()
    records={};times=[]
    for key in ('initial','polish','review'):
        ref=receipts[key]
        if (not isinstance(ref,dict) or set(ref)!={'id','sha256'}
                or not re.fullmatch(r'[a-f0-9]{32}',str(ref.get('id','')))
                or not re.fullmatch(r'[a-f0-9]{64}',str(ref.get('sha256','')))):
            raise ValueError('原回执编号或SHA256无效')
        path=(folder/(ref['id']+'.json')).resolve()
        if path.parent!=folder or not path.is_file() or path.stat().st_size>2*1024*1024:
            raise ValueError('原回执不属于当前账号的安全响应目录')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=ref['sha256']:
            raise ValueError('原回执SHA256变化，不能继续')
        record=json.loads(raw.decode('utf-8'))
        if record.get('market')!=job['market']:
            raise ValueError('原模型回执站点不匹配')
        expected={'initial':None,'polish':'listing_unslop','review':'listing_bullet_review'}[key]
        if record.get('purpose')!=expected:
            raise ValueError('原回执阶段类型不匹配')
        try:
            at=datetime.fromisoformat(record['createdAt'].replace('Z','+00:00'))
            if at.tzinfo is None:raise ValueError()
            times.append(at.timestamp())
        except (ValueError,TypeError,KeyError,AttributeError):
            raise ValueError('原回执没有有效时间') from None
        provider.response_json(record.get('response'))
        records[key]=record
    if receipts['initial']['id']!=initial_match[0] or len({v['id'] for v in receipts.values()})!=3:
        raise ValueError('初稿回执不属于原失败任务，或阶段回执重复')
    end=(job.get('reviewRecovery') or {}).get('originalFailureUpdatedAt',job['updatedAt'])
    if not (job['createdAt']-1<=times[0]<=times[1]<=times[2]<=end+2):
        raise ValueError('三份原回执时间序与原任务不符')
    # This recovery starts at the first returned semantic review. A later repair
    # response would require its own original checkpoint, never replay the polish.
    selected_ids={item['id'] for item in receipts.values()}
    for path in folder.glob('*.json'):
        if path.stem in selected_ids or not times[1]-2<=path.stat().st_mtime<=end+3:
            continue
        try:
            extra=json.loads(path.read_text(encoding='utf-8'))
            extra_at=datetime.fromisoformat(extra.get('createdAt','').replace('Z','+00:00')).timestamp()
        except (OSError,ValueError,TypeError,AttributeError):
            continue
        if (times[1]<=extra_at<=end+2 and extra.get('market')==job['market']
                and extra.get('purpose') in ('listing_unslop','listing_bullet_review','listing_bullet_repair')):
            raise ValueError('原任务已有其他润色或修订阶段回执，不能从旧阶段重放')
    identity={'jobId':job['id'],'owner':job['owner'],'productId':job['productId'],'inputDigest':job['inputDigest']}
    lineage=[record.get('listingTask') for record in records.values()]
    if any(lineage):
        if any(not isinstance(item,dict) or any(item.get(k)!=v for k,v in identity.items()) for item in lineage):
            raise ValueError('原模型回执任务身份不匹配')
        binding='persisted_task_lineage'
    else:
        # Old responses lack request lineage. Explicit hash reconciliation is allowed
        # only in an exclusive account-local task window; never invent that lineage.
        overlap=conn.execute('SELECT id FROM listing_generation_jobs WHERE owner=? AND id<>? '
            'AND created<=? AND updated>=? LIMIT 1',(job['owner'],job['id'],times[2]+1,times[0]-1)).fetchone()
        if overlap is not None:
            raise ValueError('旧回执时间窗有其他文案任务，无法唯一对账')
        binding='explicit_legacy_hash_reconciliation_exclusive_task_window'
    product=job['inputSnapshot']['product'];market=job['market']
    research=(product.get('competitorResearch') or {}).get(market) or {}
    if research.get('source')=='sellersprite':
        import ivyea_listing_trial
        research={'keywords':ivyea_listing_trial.keyword_input_report(product,market,required=False)['keywords']}
    plan=provider.selected_keyword_plan(research,provider.listing_brand_policy.excluded_brand_terms(product))[:45]
    draft=provider.response_json(records['initial']['response'])
    polished=provider.merge_polished_listing(draft,provider.response_json(records['polish']['response']))
    bullets=provider.normalize_bullet_lines(polished.get('bullets'))
    if not isinstance(bullets,str) or len(bullets.splitlines())!=5:
        raise ValueError('原润色回执没有完整五点，不能局部恢复')
    hits={}
    for hit in provider.draft_bullet_keyword_hits(draft.get('bullets'),plan)+provider.draft_bullet_keyword_hits(bullets,plan):
        hits.setdefault(hit['phrase'],set()).update(hit['bullets'])
    hit_rows=[{'phrase':phrase,'bullets':sorted(values)} for phrase,values in hits.items()]
    valid,invalid=provider.bullet_review_findings(provider.response_json(records['review']['response']),bullets,hit_rows)
    if not invalid:
        raise ValueError('原审校没有已返回的无效引用，不重复审校')
    return records,{'binding':binding,'validIssueCount':len(valid),'invalidIssueCount':len(invalid),
                    'validIssues':valid,'invalidIssues':invalid,'draftKeywordHits':hit_rows,'polishedBullets':bullets}


def resume_review(owner,data):
    account(owner)
    if (not isinstance(data,dict) or set(data)!={'id','requestId','receipts'}
            or not isinstance(data.get('requestId'),str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',data['requestId'])):
        raise ValueError('局部审校恢复参数无效')
    with LOCK,database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        job=owned(conn,owner,data['id']);recover(conn,job)
        if job.get('reviewRecovery'):
            return public(job,include_result=True,reused=True)
        if job['status']!='needs_review' or job.get('result') or result_path(job).exists():
            raise ValueError('只允许恢复原任务已返回的无效审校引用')
        revision,state=workspace(conn)
        product=product_for(state,job['productId'])
        if digest(input_snapshot(state,product,job['market']))!=job['inputDigest']:
            raise ValueError('原产品资料、文案或锁定已变化，未调用模型')
        _,diagnostic=review_recovery_bundle(conn,job,data['receipts'])
        job['reviewRecovery']={'id':uuid.uuid4().hex,'scope':'resume_original_invalid_bullet_review',
            'requestId':data['requestId'],'authorizedAt':stamp(),'originalError':job.get('error',''),
            'originalFailureUpdatedAt':job['updatedAt'],'inputDigest':job['inputDigest'],
            'receipts':copy.deepcopy(data['receipts']),'diagnostic':diagnostic,'phase':'queued','stageResponses':[]}
        job.update(status='queued',runOwner=RUNTIME,stage='原审校局部恢复已排队',
                   message='复用原初稿和润色，仅核对已返回的无效引用，再完成原检查')
        write_outcome(conn,state,revision,job);save(conn,job);ACTIVE.add(job['id'])
    try:
        THREAD_FACTORY(target=execute_review_recovery,args=(owner,job['id']),daemon=True,
                       name='listing-review-'+job['id'][:8]).start()
    except Exception as error:
        ACTIVE.discard(job['id'])
        with LOCK,database() as conn:
            current=owned(conn,owner,job['id']);current['reviewRecovery'].update(phase='dispatch_failed',error=str(error)[:2000])
            current.update(status='needs_review',stage='原审校恢复未启动',message='恢复意图已保留，未重复调用模型')
            save(conn,current);job=current
    return public(job,reused=False)


def execute_review_recovery(owner,jid):
    def checkpoint(phase,details=None):
        with LOCK,database() as conn:
            conn.execute('BEGIN IMMEDIATE');current=owned(conn,owner,jid);recovery=current['reviewRecovery']
            recovery['phase']=phase
            recovery.setdefault('history',[]).append({'phase':phase,'at':stamp(),'details':copy.deepcopy(details or {})})
            save(conn,current)
    def response_saved(receipt_id,record):
        with LOCK,database() as conn:
            conn.execute('BEGIN IMMEDIATE');current=owned(conn,owner,jid)
            path=DB.parent/'listing-responses'/(receipt_id+'.json')
            current['reviewRecovery']['stageResponses'].append({'receiptId':receipt_id,'purpose':record.get('purpose'),
                'createdAt':record['createdAt'],'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            save(conn,current)
    try:
        with LOCK,database() as conn:
            conn.execute('BEGIN IMMEDIATE');job=owned(conn,owner,jid)
            recovery=job.get('reviewRecovery') or {}
            if (job['status']!='queued' or job.get('runOwner')!=RUNTIME
                    or recovery.get('phase') not in ('queued','queued_returned_continuation')):return
            revision,state=workspace(conn);product=product_for(state,job['productId'])
            if digest(input_snapshot(state,product,job['market']))!=job['inputDigest']:
                raise ValueError('恢复执行前资料已变化，未调用模型')
            records,_=review_recovery_bundle(conn,job,recovery['receipts'])
            returned=None
            if recovery.get('phase')=='queued_returned_continuation':
                returned,_=returned_review_bundle(conn,job,recovery['continuation']['returnedReceipt'])
                recovery['continuation']['intent']={'armedAt':stamp(),'requestId':recovery['continuation']['requestId']}
                recovery['phase']='returned_continuation_armed'
            else:
                recovery.update(phase='intent_armed',intent={'armedAt':stamp(),'requestId':recovery['requestId']})
            job.update(status='running',stage='仅恢复原审校与后续检查',message='原初稿和润色已复用，不重新生成')
            save(conn,job)
        identity={'jobId':jid,'owner':owner,'productId':job['productId'],'inputDigest':job['inputDigest'],
                  'reviewRecoveryId':recovery['id']}
        with provider.listing_response_scope(identity,response_saved):
            result=provider.resume_listing_review(copy.deepcopy(job['inputSnapshot']['product']),job['market'],
                initial_raw=records['initial']['response'],polished_raw=records['polish']['response'],
                old_review=records['review']['response'],receipt_id=recovery['receipts']['initial']['id'],checkpoint=checkpoint,
                **({'returned_review':returned['response']} if returned else {}))
        if not isinstance(result,dict) or not isinstance(result.get('copy'),dict):
            raise ValueError('局部恢复未产生通过原检查的完成稿')
        result['copy'].setdefault('generationEngine',{})['reviewRecovery']={'id':recovery['id'],
            'initialReceipt':recovery['receipts']['initial']['id'],'polishReceipt':recovery['receipts']['polish']['id'],
            'originalReviewReceipt':recovery['receipts']['review']['id'],'scope':recovery['scope']}
        if recovery.get('continuation'):
            result['copy']['generationEngine']['reviewRecovery']['returnedContinuationId']=recovery['continuation']['id']
        save_result_receipt(job,result)
        with LOCK,database() as conn:
            conn.execute('BEGIN IMMEDIATE');current=owned(conn,owner,jid)
            current['reviewRecovery'].update(phase='completed',completedAt=stamp())
            current.update(status='ready',result=result,responseReceipt=result.get('responseReceipt'),
                stage='原完成稿已接续检查',message='局部恢复完成，正在核对原资料版本并应用')
            safe_apply_result(conn,current)
    except Exception as error:
        with LOCK,database() as conn:
            conn.execute('BEGIN IMMEDIATE');current=owned(conn,owner,jid)
            recovery=current.get('reviewRecovery')
            if isinstance(recovery,dict):recovery.update(phase='needs_reconciliation',error=str(error)[:2000])
            result=load_result_receipt(current)
            if result is not None:
                current.update(status='ready',result=result,responseReceipt=result.get('responseReceipt'))
                safe_apply_result(conn,current)
            elif current['status']!='applied':
                current.update(status='needs_review',stage='原审校恢复待核对',
                    message='局部恢复回执已保留，不自动再次调用模型',error=str(error)[:2000])
                revision,state=workspace(conn);write_outcome(conn,state,revision,current);save(conn,current)
    finally:
        ACTIVE.discard(jid)


def returned_review_bundle(conn,job,ref):
    """A returned response, not a new re-review: exact lineage and no later paid stage."""
    recovery=job.get('reviewRecovery') or {}
    stages=recovery.get('stageResponses') or []
    if (not isinstance(ref,dict) or set(ref)!={'id','sha256'} or len(stages)!=1
            or stages[0].get('receiptId')!=ref.get('id') or stages[0].get('sha256')!=ref.get('sha256')
            or stages[0].get('purpose')!='listing_bullet_review_recovery'
            or not re.fullmatch(r'[a-f0-9]{32}',str(ref.get('id','')))
            or not re.fullmatch(r'[a-f0-9]{64}',str(ref.get('sha256','')))):
        raise ValueError('只能接续同一次已返回的局部重审回执')
    if any(row.get('phase')=='targeted_repair_intent' for row in recovery.get('history',[])):
        raise ValueError('后续修订已有提交意图，只读对账，不能重放')
    path=DB.parent/'listing-responses'/(ref['id']+'.json')
    if not path.is_file() or path.stat().st_size>2*1024*1024:
        raise ValueError('已返回重审回执不存在或过大')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=ref['sha256']:
        raise ValueError('已返回重审回执SHA256不符')
    record=json.loads(raw.decode('utf-8'));lineage=record.get('listingTask') or {}
    identity={'jobId':job['id'],'owner':job['owner'],'productId':job['productId'],
              'inputDigest':job['inputDigest'],'reviewRecoveryId':recovery.get('id')}
    if (record.get('market')!=job['market'] or record.get('purpose')!='listing_bullet_review_recovery'
            or any(lineage.get(key)!=value for key,value in identity.items())):
        raise ValueError('已返回重审回执不属于原任务和恢复意图')
    returned=provider.response_json(record['response'])
    if not any(row.get('phase')=='re_review_returned' and row.get('details',{}).get('response')==returned
               for row in recovery.get('history',[])):
        raise ValueError('原后台没有记录该完整重审返回，不重发未知阶段')
    records,proof=review_recovery_bundle(conn,job,recovery['receipts']);dispositions=[]
    def collect(phase,details):
        dispositions.extend(details.get('deterministicDispositions',[]))
    issues=provider.reconcile_returned_bullet_review(provider.response_json(records['review']['response']),
        returned,proof['polishedBullets'],proof['draftKeywordHits'],checkpoint=collect)
    if not dispositions:
        raise ValueError('没有可机械反证的丢词判断，不扩大原恢复范围')
    return record,{'issues':issues,'deterministicDispositions':dispositions,'paidReReviewCount':1}


def continue_returned_review(owner,data):
    account(owner)
    if (not isinstance(data,dict) or set(data)!={'id','reviewRecoveryId','requestId','returnedReceipt'}
            or not isinstance(data.get('requestId'),str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',data['requestId'])):
        raise ValueError('已返回审校接续参数无效')
    with LOCK,database() as conn:
        conn.execute('BEGIN IMMEDIATE');job=owned(conn,owner,data['id']);recover(conn,job)
        recovery=job.get('reviewRecovery') or {}
        if recovery.get('id')!=data['reviewRecoveryId']:
            raise ValueError('恢复意图不属于原任务')
        if recovery.get('continuation'):
            return public(job,include_result=True,reused=True)
        if (job['status']!='needs_review' or recovery.get('phase')!='needs_reconciliation'
                or recovery.get('error')!='局部重审仍含无法对应原文的问题；未再次调用模型'
                or job.get('result') or result_path(job).exists()):
            raise ValueError('只允许接续已完整返回且机械反证明确的审校阶段')
        revision,state=workspace(conn);product=product_for(state,job['productId'])
        if digest(input_snapshot(state,product,job['market']))!=job['inputDigest']:
            raise ValueError('原资料或文案已变化，未调用模型')
        _,diagnostic=returned_review_bundle(conn,job,data['returnedReceipt'])
        recovery['continuation']={'id':uuid.uuid4().hex,'scope':'continue_known_returned_review',
            'requestId':data['requestId'],'authorizedAt':stamp(),'originalError':recovery['error'],
            'returnedReceipt':copy.deepcopy(data['returnedReceipt']),'diagnostic':diagnostic}
        recovery['phase']='queued_returned_continuation'
        job.update(status='queued',runOwner=RUNTIME,stage='接续已返回审校后的原检查',
                   message='已保存丢词判断反证；继续修订有效问题，不再次重审或重新生成')
        write_outcome(conn,state,revision,job);save(conn,job);ACTIVE.add(job['id'])
    try:
        THREAD_FACTORY(target=execute_review_recovery,args=(owner,job['id']),daemon=True,
                       name='listing-returned-'+job['id'][:8]).start()
    except Exception as error:
        ACTIVE.discard(job['id'])
        with LOCK,database() as conn:
            current=owned(conn,owner,job['id']);current['reviewRecovery']['continuation']['error']=str(error)[:2000]
            current.update(status='needs_review',stage='已返回审校接续未启动',message='接续意图已保留，未自动重发')
            save(conn,current);job=current
    return public(job,reused=False)


def exhausted_receipt_bundle(job, refs, *, target_bullets=None):
    """Strict new scope: nine complete task-bound responses, never a legacy fallback."""
    if not isinstance(refs, list) or len(refs) != 9:
        raise ValueError('次数耗尽接续必须提供完整九份原回执及SHA256')
    folder = (DB.parent / 'listing-responses').resolve()
    identity = {'jobId':job['id'], 'owner':job['owner'], 'productId':job['productId'],
                'inputDigest':job['inputDigest']}
    records, times, ids = [], [], set()
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {'id', 'sha256'}
                or not re.fullmatch(r'[a-f0-9]{32}', str(ref.get('id', '')))
                or not re.fullmatch(r'[a-f0-9]{64}', str(ref.get('sha256', '')))
                or ref['id'] in ids):
            raise ValueError('原九份回执编号、顺序或SHA256无效')
        path = (folder / (ref['id'] + '.json')).resolve()
        if path.parent != folder or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('原回执不属于当前账号的安全目录')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise ValueError('原九份回执SHA256变化，未调用模型')
        try:
            record = json.loads(raw.decode('utf-8'))
            at = datetime.fromisoformat(record['createdAt'].replace('Z', '+00:00'))
            if at.tzinfo is None:
                raise ValueError()
            choice = record['response']['choices'][0]
            lineage = record.get('listingTask') or {}
            if (record.get('market') != job['market'] or choice.get('finish_reason') != 'stop'
                    or any(lineage.get(key) != value for key,value in identity.items())
                    or lineage.get('exhaustedContinuationId') or lineage.get('reviewRecoveryId')):
                raise ValueError()
            provider.response_json(record['response'])
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise ValueError('原九份回执站点、完整返回或任务身份不匹配') from None
        times.append(at.timestamp());records.append(record);ids.add(ref['id'])
    continuation = job.get('exhaustedContinuation') or {}
    original_error = continuation.get('originalError', job.get('error', ''))
    end = continuation.get('originalFailureUpdatedAt', job['updatedAt'])
    initial_ids = re.findall(r'初稿回执 ([a-f0-9]{32})', original_error)
    if ('五点未通过检查，已达到自动返修次数' not in original_error
            or initial_ids != [refs[0]['id']]
            or not job['createdAt'] - 1 <= times[0] <= times[-1] <= end + 2
            or times != sorted(times)):
        raise ValueError('原错误、初稿身份或九份回执时序不符')
    response_ids = [record['response'].get('id') for record in records]
    if not all(isinstance(value, str) and value for value in response_ids) or len(set(response_ids)) != 9:
        raise ValueError('原模型响应身份缺失或重复')
    routing = {(record.get('provider'), record['response'].get('model')) for record in records}
    if len(routing) != 1 or not all(isinstance(value, str) and value for value in next(iter(routing))):
        raise ValueError('原九份回执模型身份不一致')
    # Another returned stage from this exact original job invalidates the start
    # point. Existing responses from THIS continuation are separately accounted.
    allowed_new = {row['receiptId'] for row in continuation.get('stageResponses', [])}
    for path in folder.glob('*.json'):
        if path.stem in ids or path.stem in allowed_new:
            continue
        try:
            other = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            continue
        if (other.get('listingTask') or {}).get('jobId') == job['id']:
            raise ValueError('原任务存在未纳入冻结的后续模型回执，不能从旧阶段重放')
    frozen = provider.reconstruct_exhausted_listing(job['inputSnapshot']['product'], job['market'], records,
                                                   target_bullets=target_bullets)
    if frozen['lastIssues'][0]['reason'] not in original_error:
        raise ValueError('末次有效问题与原失败错误不一致')
    return records, frozen, next(iter(routing))


def continue_exhausted_review(owner, data):
    account(owner)
    required = {'id', 'requestId', 'inputDigest', 'receipts', 'bulletsSha256', 'targetBullets'}
    if (not isinstance(data, dict) or set(data) != required
            or not re.fullmatch(r'[a-f0-9]{32}', str(data.get('id', '')))
            or not isinstance(data.get('requestId'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', data['requestId'])
            or not re.fullmatch(r'[a-f0-9]{64}', str(data.get('inputDigest', '')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(data.get('bulletsSha256', '')))):
        raise ValueError('原稿指定五点有界接续参数无效')
    targets = provider.exhausted_targets(data.get('targetBullets'))
    with LOCK, database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        job = owned(conn, owner, data['id']);recover(conn, job)
        if job.get('exhaustedContinuation'):
            return public(job, include_result=True, reused=True)
        if (job['status'] != 'needs_review' or job.get('result') or result_path(job).exists()
                or job.get('reviewRecovery') or job['inputDigest'] != data['inputDigest']):
            raise ValueError('只允许原任务已完整返回且三次返修耗尽后的独立接续')
        revision, state = workspace(conn);product = product_for(state, job['productId'])
        if digest(input_snapshot(state, product, job['market'])) != job['inputDigest']:
            raise ValueError('原产品、文案或锁定已变化，未调用模型')
        records, frozen, old_routing = exhausted_receipt_bundle(job, data['receipts'], target_bullets=targets)
        if frozen['bulletsSha256'] != data['bulletsSha256']:
            raise ValueError('已审查原最终五点SHA256不符')
        model_identity = provider.exhausted_model_identity(provider.text_settings())
        if (model_identity['provider'], model_identity['model']) != old_routing:
            raise ValueError('当前模型与原已返回稿不同，未扩大接续授权')
        job['exhaustedContinuation'] = {'id':uuid.uuid4().hex,
            'scope':('continue_original_exhausted_bullet3_once' if targets == [3]
                     else 'continue_original_exhausted_target_bullets_once'), 'requestId':data['requestId'],
            'authorizedAt':stamp(), 'originalError':job.get('error', ''),
            'originalFailureUpdatedAt':job['updatedAt'], 'originalStatus':job['status'],
            'inputDigest':job['inputDigest'], 'receipts':copy.deepcopy(data['receipts']),
            'bulletsSha256':frozen['bulletsSha256'], 'bulletSha256s':frozen['bulletSha256s'],
            'frozen':frozen, 'frozenSha256':digest(frozen), 'modelIdentity':model_identity,
            'limits':{'targetBullets':targets, 'repairCalls':1, 'freshReviewCalls':1,
                      'initialCalls':0, 'polishCalls':0},
            'phase':'queued', 'intents':{}, 'stageResponses':[], 'history':[]}
        job.update(status='queued', runOwner=RUNTIME, stage='原稿指定五点有界接续已排队',
                   message='保留原九份回执，仅修订已冻结问题五点一次并重新审校一次')
        write_outcome(conn, state, revision, job);save(conn, job);ACTIVE.add(job['id'])
    try:
        THREAD_FACTORY(target=execute_exhausted_review, args=(owner, job['id']), daemon=True,
                       name='listing-exhausted-' + job['id'][:8]).start()
    except Exception as error:
        ACTIVE.discard(job['id'])
        with LOCK, database() as conn:
            current = owned(conn, owner, job['id'])
            current['exhaustedContinuation'].update(phase='dispatch_failed', error=str(error)[:2000])
            current.update(status='needs_review', stage='原稿接续未启动', message='原接续意图已保留，未自动重发')
            save(conn, current);job=current
    return public(job, reused=False)


def execute_exhausted_review(owner, jid):
    try:
        with LOCK, database() as conn:
            conn.execute('BEGIN IMMEDIATE');job = owned(conn, owner, jid)
            continuation = job.get('exhaustedContinuation') or {}
            if (job['status'] != 'queued' or job.get('runOwner') != RUNTIME
                    or continuation.get('phase') != 'queued' or continuation.get('intents')):
                return
            records, rebuilt, _ = exhausted_receipt_bundle(job, continuation['receipts'],
                                      target_bullets=continuation['limits']['targetBullets'])
            if digest(rebuilt) != continuation['frozenSha256']:
                raise ValueError('原稿严格重建或写作规则变化，未调用模型')
            continuation.update(phase='starting', startedAt=stamp())
            job.update(status='running', stage='原稿指定五点接续检查中', message='仅执行已冻结的一次返修和一次新审校')
            save(conn, job)
        continuation_id = continuation['id']

        def checkpoint(phase, details):
            with LOCK, database() as conn:
                conn.execute('BEGIN IMMEDIATE');current = owned(conn, owner, jid)
                scope = current['exhaustedContinuation']
                if (scope['id'] != continuation_id or current['status'] != 'running'
                        or current.get('runOwner') != RUNTIME):
                    raise ValueError('原接续执行身份变化，停止外部请求')
                revision, state = workspace(conn);product = product_for(state, current['productId'])
                if digest(input_snapshot(state, product, current['market'])) != current['inputDigest']:
                    raise ValueError('原资料、文案或锁定在接续中变化，未继续提交')
                if provider.exhausted_model_identity(provider.text_settings()) != scope['modelIdentity']:
                    raise ValueError('原接续模型或凭据身份变化，未继续提交')
                for ref in scope['receipts']:
                    path = DB.parent / 'listing-responses' / (ref['id'] + '.json')
                    if hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']:
                        raise ValueError('原九份回执在接续中变化，未继续提交')
                operation, event = phase.split('_', 1)
                if operation not in ('repair', 'review') or event not in ('intent', 'returned'):
                    raise ValueError('接续检查点无效')
                expected = {'repair_intent':'starting', 'repair_returned':'repair_intent',
                            'review_intent':'repair_returned', 'review_returned':'review_intent'}
                if scope['phase'] != expected[phase]:
                    raise ValueError('接续阶段已经执行或未知，禁止重放')
                if event == 'intent':
                    if operation in scope['intents'] or details.get('modelIdentity') != scope['modelIdentity']:
                        raise ValueError('同一次接续请求已有意图，不重新提交')
                    scope['intents'][operation] = {'at':stamp(), **copy.deepcopy(details)}
                else:
                    returned = [row for row in scope['stageResponses'] if row['operation'] == operation]
                    if len(returned) != 1 or returned[0]['receiptId'] != details.get('receiptId'):
                        raise ValueError('模型完整返回尚未持久核对，不继续下一阶段')
                scope['phase'] = phase
                scope['history'].append({'phase':phase, 'at':stamp(), 'details':copy.deepcopy(details)})
                save(conn, current)

        def response_saved(receipt_id, record):
            purpose_to_operation = {'listing_bullet_exhausted_repair':'repair',
                                    'listing_bullet_exhausted_review':'review'}
            operation = purpose_to_operation.get(record.get('purpose'))
            with LOCK, database() as conn:
                conn.execute('BEGIN IMMEDIATE');current = owned(conn, owner, jid)
                scope = current['exhaustedContinuation'];lineage = record.get('listingTask') or {}
                expected = {'jobId':jid, 'owner':owner, 'productId':current['productId'],
                            'inputDigest':current['inputDigest'], 'exhaustedContinuationId':continuation_id}
                if (not operation or scope['id'] != continuation_id or operation not in scope['intents']
                        or scope['phase'] != operation + '_intent'
                        or any(lineage.get(key) != value for key,value in expected.items())
                        or any(row['operation'] == operation for row in scope['stageResponses'])):
                    raise ValueError('接续模型响应身份或一次调用范围不符')
                path = DB.parent / 'listing-responses' / (receipt_id + '.json')
                scope['stageResponses'].append({'operation':operation, 'receiptId':receipt_id,
                    'purpose':record['purpose'], 'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                    'createdAt':record['createdAt'], 'responseId':record.get('response', {}).get('id')})
                save(conn, current)

        identity = {'jobId':jid, 'owner':owner, 'productId':job['productId'], 'inputDigest':job['inputDigest'],
                    'exhaustedContinuationId':continuation_id}
        with provider.listing_response_scope(identity, response_saved):
            result = provider.continue_exhausted_listing(copy.deepcopy(job['inputSnapshot']['product']), job['market'],
                frozen=copy.deepcopy(continuation['frozen']), initial_raw=records[0]['response'],
                initial_receipt_id=continuation['receipts'][0]['id'], continuation_id=continuation_id,
                model_identity=continuation['modelIdentity'], checkpoint=checkpoint)
        if not isinstance(result, dict) or not isinstance(result.get('copy'), dict):
            raise ValueError('接续没有返回通过原检查的完成稿')
        save_result_receipt(job, result)
        with LOCK, database() as conn:
            conn.execute('BEGIN IMMEDIATE');current = owned(conn, owner, jid)
            if current['status'] == 'applied':
                return
            current['exhaustedContinuation'].update(phase='completed', completedAt=stamp())
            current.update(status='ready', result=result, responseReceipt=result.get('responseReceipt'),
                           stage='原稿接续完成，核对替换条件', message='完成稿已保存，按原输入和锁定条件应用')
            safe_apply_result(conn, current)
    except Exception as error:
        with LOCK, database() as conn:
            conn.execute('BEGIN IMMEDIATE');current = owned(conn, owner, jid)
            if current['status'] == 'applied':
                return
            scope = current.get('exhaustedContinuation')
            if isinstance(scope, dict):
                returned = {row['operation'] for row in scope.get('stageResponses', [])}
                pending = [operation for operation in scope.get('intents', {}) if operation not in returned]
                scope.update(phase='needs_reconciliation' if pending else 'failed_checks',
                             error=str(error)[:2000], pendingOperations=pending)
            result = load_result_receipt(current)
            if result is not None:
                current.update(status='ready', result=result, responseReceipt=result.get('responseReceipt'))
                safe_apply_result(conn, current)
            elif current['status'] != 'applied':
                current.update(status='needs_review', stage='原稿有界接续待核对',
                    message='接续回执及原意图已保留，未重新生成或继续循环', error=str(error)[:2000])
                revision, state = workspace(conn);write_outcome(conn, state, revision, current);save(conn, current)
    finally:
        ACTIVE.discard(jid)


def execute(owner,jid):
    try:
        with LOCK,database() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job=owned(conn,owner,jid)
            if job['status']!='queued' or job.get('runOwner')!=RUNTIME:return
            job.update(status='running',stage='初稿、润色与校验中',message='文案生成中，关闭页面后仍会继续')
            save(conn,job)
        product=copy.deepcopy(job['inputSnapshot']['product'])
        with provider.listing_response_scope({'jobId':jid,'owner':owner,'productId':job['productId'],
                                             'inputDigest':job['inputDigest']}):
            result=provider.generate_listing(product,job['market'],evidence_product=copy.deepcopy(product))
        if not isinstance(result,dict) or not isinstance(result.get('copy'),dict):
            raise ValueError('模型没有返回完成稿，请核对原始响应')
        save_result_receipt(job,result)
        with LOCK,database() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current=owned(conn,owner,jid)
            if current['status']=='applied':return
            current.update(status='ready',result=result,responseReceipt=result.get('responseReceipt'),
                stage='完成稿已保存',message='完成稿已保存，正在核对替换条件')
            safe_apply_result(conn,current)
    except Exception as error:
        with LOCK,database() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job=owned(conn,owner,jid)
            if job['status']!='applied':
                result=load_result_receipt(job)
                if result is not None:
                    job.update(status='ready',result=result,responseReceipt=result.get('responseReceipt'),
                        stage='完成稿已保存，等待核对',message='完成稿已保留，未自动重新生成',error=str(error)[:2000])
                else:
                    job.update(status='needs_review',stage='生成结果待核对',
                        message='生成未正常返回完成稿，已保留任务；请先核对模型回执，不会自动重发',error=str(error)[:2000])
                revision,state=workspace(conn)
                write_outcome(conn,state,revision,job)
                save(conn,job)
    finally:
        ACTIVE.discard(jid)


def status(owner,jid):
    account(owner)
    with LOCK,database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        job=owned(conn,owner,jid);recover(conn,job)
        return public(job,include_result=True)


def list_jobs(owner,productId='',market=''):
    account(owner)
    if market and market not in provider.MARKET_LANGUAGES:raise ValueError('文案任务站点无效')
    where='owner=?';values=[owner]
    if productId:where+=' AND product_id=?';values.append(productId)
    if market:where+=' AND market=?';values.append(market)
    with LOCK,database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        recent=conn.execute('SELECT body FROM listing_generation_jobs WHERE '+where+
            ' ORDER BY created DESC,id DESC LIMIT 100',values).fetchall()
        pending=conn.execute('SELECT body FROM listing_generation_jobs WHERE '+where+
            " AND status IN ('queued','running','ready','needs_review')",values).fetchall()
        by_id={}
        for row in [*recent,*pending]:
            job=json.loads(row['body']);by_id[job['id']]=job
        jobs=sorted(by_id.values(),key=lambda j:(j['createdAt'],j['id']),reverse=True)
        for job in jobs:recover(conn,job)
        return [public(job) for job in jobs]


def apply(owner,jid):
    account(owner)
    with LOCK,database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        job=owned(conn,owner,jid);recover(conn,job)
        if job['status']!='applied':
            if not isinstance(job.get('result'),dict):
                raise ValueError('此任务尚无可恢复的完成稿；请先核对原模型回执，不要重复生成')
            safe_apply_result(conn,job)
        return public(job,include_result=True)
