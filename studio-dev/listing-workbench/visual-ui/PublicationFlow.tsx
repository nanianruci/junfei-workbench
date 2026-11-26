import HelpTip from './HelpTip';
import {useEffect,useRef,useState} from 'react';

type Job={id:string;destinationId:string;status:string;phase:string;markets?:string[];onlineVerified?:boolean;stage?:string;error?:string;usPublishId?:string;ukDraftId?:string;ukPublishId?:string;variantCount?:number;
 observeSupported?:boolean;canObserveLatestReceipts?:boolean;observationOnly?:boolean;
 mainImageRepair?:{supported:boolean;canRepair:boolean;authorized:boolean;repairId?:string;checkpoint?:string;phase?:string;saveIntended?:boolean;retryIntended?:boolean;reason?:string;
  canCorrectSave?:boolean;authorizedCorrectiveSave?:boolean;correctiveId?:string;saveReceiptUnknown?:boolean;correctiveSaveReason?:string};
 usFailure?:{publishId?:string;failedCount?:number;totalCount?:number;errorCodes?:string[]};
 mediaRecovery?:{supported:boolean;canRetry:boolean;authorized:boolean;recoveryId?:string;reason?:string}};
type Target={id:string;store:string;target:{storeName?:string;shippingTemplate?:{name:string};brand?:string};ready:boolean;missing:string[];usReady:boolean;usMissing:string[]};
type View={targets:Target[];jobs:Job[];commerce:Record<string,any>;skuCount:number;policy?:{enabled:boolean}};
const active=(j?:Job)=>!!j&&['queued','running'].includes(j.status);

declare global {
 interface Window {
  confirmPublicationOperation:(accountId:number,action:'start'|'continue',request:Record<string,unknown>)=>Promise<{job:Job}|null>;
 }
}

export default function PublicationFlow({productId,accountId,online,beforeStart,saveCommerce,sourceReadyKey}:{
 productId:string;accountId:number;online:boolean;beforeStart:()=>Promise<void>;
 saveCommerce:(details:Record<string,unknown>)=>Promise<void>;
 sourceReadyKey?:string;
}){
 const [data,setData]=useState<View>(),[selected,select]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const [markets,setMarkets]=useState<'US'|'US-UK'>('US');
 const [store,setStore]=useState(''),[shipping,setShipping]=useState(''),[brand,setBrand]=useState('');
 const [price,setPrice]=useState(''),[quantity,setQuantity]=useState(''),[handling,setHandling]=useState(''),[preserve,setPreserve]=useState(false),[consent,setConsent]=useState(false);
 const sending=useRef(false),mounted=useRef(true);
 const api=async(path:string,body?:unknown,signal?:AbortSignal)=>{
  const r=await fetch('/api/publication/'+path,{signal,headers:{'Content-Type':'application/json','X-Workspace-Account':String(accountId)},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});
  const result=await r.json();if(!r.ok)throw Error(result.error||'发布流程请求失败');return result;
 };
 const load=async(signal?:AbortSignal)=>{
  const result:View=await api('view?productId='+encodeURIComponent(productId),undefined,signal);
  if(signal?.aborted||!mounted.current)return;
  setData(result);select(old=>result.targets.some(t=>t.id===old)?old:result.targets[0]?.id||'');return result;
 };
 useEffect(()=>{
  mounted.current=true;const controller=new AbortController();setError('');setData(undefined);select('');setConsent(false);
  void load(controller.signal).then(result=>{if(!result)return;setMarkets(result.policy?.enabled?'US-UK':'US');setPrice(result.commerce.price||'');setQuantity(String(result.commerce.quantity??''));setHandling(String(result.commerce.handlingDays??''));setPreserve(result.commerce.inventoryPolicy==='preserve_source');}).catch(e=>{if(!controller.signal.aborted)setError(e.message)});
  return()=>{mounted.current=false;controller.abort();};
 },[productId,accountId]);
 const target=data?.targets.find(t=>t.id===selected),job=data?.jobs.find(j=>j.destinationId===selected);
 const usOnly=job ? job.markets?.length===1 : markets==='US';
 const ready=usOnly?target?.usReady:target?.ready;
 const continuing=job?.status==='needs_reconciliation';
 const retryAvailable=continuing&&job?.mediaRecovery?.supported===true&&job.mediaRecovery.canRetry===true&&!job.mainImageRepair?.authorized;
 const mainRepairAvailable=continuing&&job?.mainImageRepair?.supported===true&&job.mainImageRepair.canRepair===true&&!job.mainImageRepair.authorized;
 const correctiveSaveAvailable=continuing&&job?.mainImageRepair?.supported===true&&job.mainImageRepair.canCorrectSave===true&&!job.mainImageRepair.authorizedCorrectiveSave;
 const mainRepairResolved=['retry_request_accepted','retry_unknown','saved_no_retry_needed','already_correct','source_no_longer_failed','corrective_save_verified'].includes(job?.mainImageRepair?.phase||'');
 const correctiveSavePending=job?.mainImageRepair?.saveReceiptUnknown===true&&!mainRepairResolved&&!job?.stage?.startsWith('uk_');
 const continuingUk=continuing&&!!job?.ukDraftId&&!job.usFailure&&!!job.stage?.startsWith('uk_');
 const mainCheckpoint=mainRepairResolved?(job?.mainImageRepair?.phase==='source_no_longer_failed'?'原状态已恢复，未重复保存或重试':job?.mainImageRepair?.phase==='already_correct'?'原主图字段已核对，无需重复保存或重试':'原主图字段已保存并核对，继续核对原任务回执'):job?.mainImageRepair?.checkpoint||'沿原任务核对保存与重试结果';
 const showMainRepair=!!job?.mainImageRepair?.authorized||(job?.usFailure?.failedCount===1&&job.usFailure.errorCodes?.length===1&&job.usFailure.errorCodes[0]==='18320');
 useEffect(()=>{setStore(target?.target.storeName||'');setShipping(target?.target.shippingTemplate?.name||'');setBrand(target?.target.brand||'');setConsent(false);},[selected,target?.target.storeName,target?.target.shippingTemplate?.name]);
 useEffect(()=>{
  if(!active(job))return;const controller=new AbortController();
  const poll=()=>{void api('status?id='+encodeURIComponent(job!.id),undefined,controller.signal).then(result=>{if(!controller.signal.aborted)setData(old=>old?{...old,jobs:old.jobs.map(j=>j.id===result.job.id?result.job:j)}:old)}).catch(e=>{if(!controller.signal.aborted)setError(e.message)});};
  const timer=setInterval(poll,2000);return()=>{controller.abort();clearInterval(timer)};
 },[job?.id,job?.status,accountId]);
 useEffect(()=>{
  if(!sourceReadyKey||!data)return;const controller=new AbortController();
  // A verified collection changes source readiness, not the publication intent.
  // Keep the original jobs and all editable fields if a publish is in flight.
  void api('view?productId='+encodeURIComponent(productId),undefined,controller.signal).then((result:View)=>{
   if(!controller.signal.aborted&&mounted.current)setData(old=>old?{...old,targets:result.targets,skuCount:result.skuCount}:result);
  }).catch(e=>{if(!controller.signal.aborted)setError(old=>old||e.message)});
  return()=>controller.abort();
 },[sourceReadyKey,productId,accountId,Boolean(data)]);
 const run=async(action:()=>Promise<void>)=>{
  if(sending.current)return;sending.current=true;setBusy(true);setError('');
  try{await action()}catch(e){if(mounted.current)setError((e as Error).message)}finally{sending.current=false;if(mounted.current)setBusy(false)}
 };
 const submit=(action?:'retryMediaFailures'|'repairMainImage'|'retryMainImageSave')=>run(async()=>{
  if(action==='retryMediaFailures'&&(!online||busy||active(job)||!retryAvailable))throw Error('原图片失败任务尚未具备恢复条件，请先核对原回执');
  if(action==='repairMainImage'&&(!online||busy||active(job)||!mainRepairAvailable))throw Error('原主图字段尚未具备修复条件，请先核对原回执');
  if(action==='retryMainImageSave'&&(!online||busy||active(job)||!correctiveSaveAvailable))throw Error('尚未具备一次明确重新保存的条件，请先核对原记录');
  await beforeStart();
  if(!mounted.current)return;
  const result=await window.confirmPublicationOperation(accountId,continuing?'continue':'start',
    continuing?{id:job!.id,confirmed:true,...(action?{[action]:true}:{})}:{productId,destinationId:selected,confirmed:true,markets:usOnly?['US']:['US','UK']});
  if(result&&mounted.current){setData(old=>old?{...old,jobs:[...old.jobs.filter(j=>j.destinationId!==selected),result.job]}:old);setConsent(false);}
 });
 const observeLatest=()=>run(async()=>{
  if(!online||busy||active(job)||!job?.observeSupported||!job.canObserveLatestReceipts)throw Error('目标站点原发布回执尚未齐全，请先核对原任务');
  const result=await api('observe',{id:job.id});
  if(mounted.current)setData(old=>old?{...old,jobs:[...old.jobs.filter(j=>j.destinationId!==selected),result.job]}:old);
 });
 return <section className="pe-card" aria-label="正式发布">
  <div className="pe-card-heading"><div className="wb-title-label"><h2>亚马逊正式发布</h2><HelpTip label="正式发布">美英联动时，美国取得发布中任务号后，才复制到英国，恢复原父子 SKU，核对 GBP 售价和运输模板，再提交英国发布。任务回执可在任务中心查看。</HelpTip></div></div>
  {!data?<p role="status">{error||'正在读取发布配置与回执…'}</p>:<>
   <label>美国来源店铺 <select value={selected} disabled={busy||active(job)} onChange={e=>select(e.target.value)}>
    {!data.targets.length&&<option value="">请先配置美国ERP采集目标</option>}{data.targets.map(t=><option key={t.id} value={t.id}>{t.store}</option>)}
   </select></label>
   {!job&&<label>发布站点 <select value={markets} disabled={busy} onChange={e=>{setMarkets(e.target.value as 'US'|'US-UK');setConsent(false)}}>
    <option value="US">仅美国</option><option value="US-UK" disabled={!data.policy?.enabled}>美国并接续英国</option>
   </select></label>}
   {!usOnly&&<details><summary>英国店铺与经营资料 <HelpTip label="经营资料">店铺设置按账号保存。售价、库存和处理时间只用于当前产品。运行中的任务继续使用原快照。</HelpTip></summary>
    <div className="pe-actions"><label>英国ERP店铺<input value={store} maxLength={200} onChange={e=>setStore(e.target.value)}/></label><label>英国运输模板<input value={shipping} maxLength={200} onChange={e=>setShipping(e.target.value)}/></label><label>适用品牌（可空）<input value={brand} maxLength={100} onChange={e=>setBrand(e.target.value)}/></label>
    <button disabled={!selected||!store||!shipping||busy||active(job)} onClick={()=>void run(async()=>{await api('settings',{sourceDestinationId:selected,storeName:store,shippingTemplate:shipping,brand});await load();})}>保存店铺设置</button></div>
    <div className="pe-actions"><label>本款统一售价 GBP<input type="number" min="0.01" step="0.01" value={price} onChange={e=>setPrice(e.target.value)}/></label><label>每子SKU库存<input type="number" min="0" step="1" disabled={preserve} value={quantity} onChange={e=>setQuantity(e.target.value)}/></label><label>处理时间（天）<input type="number" min="0" max="30" step="1" value={handling} onChange={e=>setHandling(e.target.value)}/></label></div>
    <label><input type="checkbox" checked={preserve} onChange={e=>setPreserve(e.target.checked)}/> 库存沿用原美国草稿</label>
    <button disabled={busy||active(job)||!price||(!preserve&&!quantity)||!handling} onClick={()=>void run(async()=>{
     await saveCommerce({...data.commerce,price,variantPrices:{},handlingDays:handling,...(preserve?{quantity:undefined,variantQuantities:{},inventoryPolicy:'preserve_source'}:{quantity,variantQuantities:{},inventoryPolicy:undefined})});await load();
    })}>保存本款英国经营资料</button>
    {Object.keys(data.commerce.variantPrices||{}).length>0&&<p>已保存逐 SKU GBP 售价。填写统一售价并保存将覆盖该价格表，旧值保留在经营资料历史。</p>}
   </details>}
   {(usOnly?target?.usMissing:target?.missing)?.map((m,i)=><p key={i} role="status">待补齐：{m}</p>)}
   {job&&<div className="notice" role="status"><strong>{job.phase}</strong><p>美国发布任务：{job.usPublishId||'未取得'}{!usOnly&&<> · 英国草稿：{job.ukDraftId||'未取得'} · 英国发布任务：{job.ukPublishId||'未取得'}</>}</p>{job.error&&<p>{job.error}</p>}{job.status==='submitted'&&!job.usFailure&&<p>{job.onlineVerified?'所有子 SKU 在售已核验。':'目标站点已受理，待核实亚马逊在售。'}</p>}</div>}
   {job?.mainImageRepair?.authorized&&<p role="status">主图字段修复检查点：{mainCheckpoint}。</p>}
   {correctiveSavePending&&<div className="notice" role="status">
    <p>旧保存回执仍未知，最近回读仍为原资料。再次核对原记录、确认资料未变后，仅对同一记录保存原主图字段一次；保存回读通过后才重试原父任务，结果未知不循环。</p>
    <button id="wbRetryOriginalMainImageSave" disabled={!online||!correctiveSaveAvailable||busy||active(job)} onClick={()=>void submit('retryMainImageSave')}>核对原记录并重新保存主图字段</button>
    {job?.mainImageRepair?.correctiveSaveReason&&<p>{job.mainImageRepair.correctiveSaveReason}</p>}
    {job?.mainImageRepair?.authorizedCorrectiveSave&&<p>本次重新保存授权已冻结；继续只沿同一检查点核对，保留旧意图与未知回执。</p>}
   </div>}
   {job?.usFailure&&<div className="notice" role="alert"><strong>美国原任务后续失败：{job.usFailure.failedCount??'待核对'}/{job.usFailure.totalCount??job.variantCount??'待核对'} 个子 SKU</strong>
    <p>当前任务尚未完成。核对图片后会重新处理原美国父任务的全部 {job.usFailure.totalCount??job.variantCount??''} 个子 SKU，保留原任务及父子 SKU。</p>
    {!!job.usFailure.errorCodes?.length&&<p>图片错误代码：{job.usFailure.errorCodes.join('、')}</p>}
    {showMainRepair&&<><button id="wbRepairOriginalMainImage" disabled={!online||!mainRepairAvailable||busy||active(job)} onClick={()=>void submit('repairMainImage')}>补齐原主图字段并重试原任务</button>
     <p>保留原图、原 SKU 和商品资料，仅补齐失败子 SKU 的原主图字段；保存回读后重试原父任务全部 {job.usFailure.totalCount??job.variantCount??''} 个子 SKU。</p>
     {job.mainImageRepair?.reason&&<p>{job.mainImageRepair.reason}</p>}
     {!job.mainImageRepair?.supported&&<p>当前服务尚未提供原主图字段修复，请先刷新核对。</p>}</>}
    <button id="wbRetryUsMediaFailures" disabled={!online||!retryAvailable||busy||active(job)} onClick={()=>void submit('retryMediaFailures')}>核对图片并重试原美国任务</button>
    {job.mediaRecovery?.reason&&<p>{job.mediaRecovery.reason}</p>}
    {!job.mediaRecovery?.supported&&<p>当前服务尚未提供原任务图片恢复，请先刷新核对。</p>}
   </div>}
   {job?.usPublishId&&(usOnly||job?.ukPublishId)&&<div className="pe-actions"><button id="wbObservePublicationReceipts"
    disabled={!online||busy||active(job)||!job.observeSupported||!job.canObserveLatestReceipts}
    onClick={()=>void observeLatest()}>核对原发布回执</button><span>只读取原任务最新状态，不复制、不改图、不重发。</span></div>}
   {job?.status!=='submitted'&&<>{!job&&<label><input type="checkbox" checked={consent} disabled={busy} onChange={e=>setConsent(e.target.checked)}/> {usOnly?'发布本款到美国，保持原父子 SKU。':'发布本款到美国并接续英国，保持原父子 SKU。'}</label>}
   {continuing&&!job?.mediaRecovery?.authorized&&!job?.mainImageRepair?.authorized&&<p>继续仅核对原回执；图片失败重试使用上方的明确恢复入口。</p>}
   <div className="pe-actions"><button className="pe-primary" disabled={!online||!selected||(!ready&&!job)||(!job&&!consent)||busy||active(job)} onClick={()=>void submit()}>{busy?'正在提交…':active(job)?'后台执行中':continuing?(continuingUk?'核对原英国草稿并继续':mainRepairResolved?'核对原回执并继续':correctiveSavePending&&job?.mainImageRepair?.authorizedCorrectiveSave?'继续原主图重新保存检查点':job?.mainImageRepair?.authorized?'继续原主图修复检查点':job?.mediaRecovery?.authorized?'继续原图片恢复检查点':'核对原回执并继续'):usOnly?'发布美国':'发布美国并接续英国'}</button><button disabled={busy} onClick={()=>void run(async()=>{await load()})}>刷新状态</button></div></>}
   {error&&<p role="alert">{error}</p>}
  </>}
 </section>;
}
