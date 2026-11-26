import HelpTip from './HelpTip';
import {useEffect,useState,useRef} from 'react';
import {newCollectionBlocked} from './delivery-readiness';

type Job={id:string;destinationId:string;marketplace:string;status:string;phase:string;store:string;error?:string;draftUrl?:string;draftId?:string;collectionId?:string;collectionUrl?:string;nativeJoined?:boolean;handoffMode?:string;variantCount?:number;imageCount?:number;verifiedAt?:number;reused?:boolean;stages?:Record<string,string>};
type Destination={id:string;name:string;store:string;marketplace:string;price:string};
const running=(job?:Job)=>!!job&&['queued','running'].includes(job.status);

export default function ErpCollection({productId,accountId,market,copy,online,beforeStart,deliveryReady,deliveryBlockReason,onVerified}:{productId:string;accountId:number;market:string;copy:Record<string,unknown>;online:boolean;beforeStart:()=>Promise<void>;deliveryReady:boolean;deliveryBlockReason:string;onVerified?:(id:string)=>void}){
  const [destinations,setDestinations]=useState<Destination[]>([]),[selected,select]=useState('');
  const [job,setJob]=useState<Job>(),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [jobs,setJobs]=useState<Job[]>([]),[issues,setIssues]=useState<{destinationId:string;message:string}[]>([]);const sending=useRef(false);
  const [pollError,setPollError]=useState('');
  const remember=(value:Job)=>{setJob(value);setJobs(old=>[...old.filter(j=>j.destinationId!==value.destinationId),value]);};
  useEffect(()=>{
    const controller=new AbortController();setLoading(true);setError('');setPollError('');setJob(undefined);
    fetch('/api/erp-collection/view?productId='+encodeURIComponent(productId),{signal:controller.signal,headers:{'X-Workspace-Account':String(accountId)}})
      .then(async r=>{const data=await r.json();if(!r.ok)throw Error(data.error||'采集记录读取失败');return data;})
      .then(data=>{if(controller.signal.aborted)return;const targets=data.destinations.filter((d:Destination)=>d.marketplace===market);const first=targets[0]?.id||'';setDestinations(targets);select(first);setJobs(data.jobs);setIssues(data.issues||[]);setJob(data.jobs.find((j:Job)=>j.destinationId===first));})
      .catch(e=>{if(!controller.signal.aborted)setError(e.message);}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[productId,accountId,market]);
  useEffect(()=>{
    setPollError('');
    if(!job?.id||job.status==='verified')return;
    const controller=new AbortController();
    const poll=async()=>{try{const r=await fetch('/api/erp-collection/status?id='+encodeURIComponent(job.id),{signal:controller.signal,headers:{'X-Workspace-Account':String(accountId)}});const data=await r.json();if(!r.ok)throw Error(data.error||'采集状态读取失败');if(!controller.signal.aborted){remember(data.job);setPollError('');}}catch(e){if(!controller.signal.aborted)setPollError((e as Error).message);}};
    const timer=setInterval(poll,1500);return()=>{controller.abort();clearInterval(timer);};
  },[job?.id,job?.status,accountId,productId]);
  useEffect(()=>{if(job?.status==='verified')onVerified?.(job.id);},[job?.id,job?.status,onVerified]);
  const collect=async()=>{
    if(sending.current||running(job)||newCollectionBlocked(deliveryReady,job))return;sending.current=true;
    setBusy(true);setError('');
    try{
      if(job?.status==='verified'){
        const r=await fetch('/api/erp-collection/status?id='+encodeURIComponent(job.id),{headers:{'X-Workspace-Account':String(accountId)}});
        const data=await r.json();if(!r.ok)throw Error(data.error||'采集回执读取失败');remember(data.job);return;
      }
      await beforeStart();
      const expectedListing=Object.fromEntries(['title','highlights','bullets','description','terms'].map(k=>[k,copy[k]||'']));
      const r=await fetch('/api/erp-collection/start',{method:'POST',headers:{'Content-Type':'application/json','X-Workspace-Account':String(accountId)},body:JSON.stringify({productId,destinationId:selected,expectedListing})});
      const data=await r.json();if(!r.ok)throw Error(data.error||'采集未启动');remember(data.job);setIssues(old=>old.filter(i=>i.destinationId!==selected));
    }catch(e){setError((e as Error).message);}finally{sending.current=false;setBusy(false);}
  };
  const safeUrl=job?.draftUrl&&/^https:\/\/www\.junfeikuajing\.com\/#\/amazon\/product\/\d+\?type=draft$/.test(job.draftUrl)?job.draftUrl:undefined;
  return <section className="pe-card" aria-label="成品采集到钧飞ERP">
    <div className="pe-card-heading"><div className="wb-title-label"><h2>一键采集到钧飞 ERP</h2><HelpTip label="ERP 采集">成品进入采集列表，再自动转入所选店铺草稿并重开核对。无需下载或逐款绑定。</HelpTip></div></div>
    {loading?<p role="status">正在读取采集目标与回执…</p>:<>
      <div className="pe-actions"><label>目标店铺 <select aria-label="ERP采集目标店铺" value={selected} onChange={e=>{select(e.target.value);setJob(jobs.find(j=>j.destinationId===e.target.value));}} disabled={busy||running(job)}>
        {!destinations.length&&<option value="">尚未配置采集目标</option>}{destinations.map(d=><option key={d.id} value={d.id}>{d.name||d.store||d.id} · {d.marketplace}</option>)}
      </select></label><button id="wbCollectErp" className="pe-primary" disabled={!online||!selected||(!copy.title&&job?.status!=='verified')||busy||running(job)||newCollectionBlocked(deliveryReady,job)} onClick={collect}>
        {busy?'正在读取或提交采集…':running(job)?job!.phase:job?.status==='verified'?'刷新已有采集回执':job?.status==='needs_reconciliation'?'从检查点继续':'一键采集到 ERP'}</button>
        {job?.collectionId&&job.collectionUrl==='https://www.junfeikuajing.com/#/product/collect'&&<a href={job.collectionUrl} target="_blank" rel="noreferrer">打开采集列表 · {job.collectionId}</a>}
        {safeUrl&&<a href={safeUrl} target="_blank" rel="noreferrer">打开 ERP 草稿 {job?.draftId}</a>}
      </div>
      {newCollectionBlocked(deliveryReady,job)&&<p role="status">{deliveryBlockReason||'当前成品尚未完整通过检查，暂不能新建 ERP 采集。'}</p>}
      {!destinations.length&&<p>管理员配置本账号的店铺与专属浏览器后，即可使用页面采集。</p>}
      {issues.filter(i=>i.destinationId===selected).map((i,index)=><p role="alert" key={index}>{i.message}</p>)}
      {job&&<div role="status" className="notice"><strong>{job.status==='verified'?(job.nativeJoined?'ERP 原生采集与目标店铺草稿已核对':'采集记录与既有草稿已核对'):job.phase}</strong>
        <p>{job.store}{job.variantCount?` · ${job.variantCount} 个子 SKU`:''}{job.imageCount?` · ${job.imageCount} 张成品图，每色 7 张`:''}</p>
        {job.stages&&Object.keys(job.stages).length>0&&<ol className="pe-erp-stages">{Object.entries({dedupe:'SKU查重',images:'图片上传',collection:'采集列表',native:'原生转入',draft:'草稿保存',verification:'重开核对'}).map(([key,label])=><li key={key} data-state={job.stages?.[key]||'pending'}>{label} · {job.stages?.[key]==='completed'?'完成':job.stages?.[key]==='running'?'处理中':job.status==='verified'&&!job.stages?.[key]?'未记录':'待执行'}</li>)}</ol>}
        {job.status==='verified'&&<p>颜色、尺码及逐 SKU 图片顺序已核对。重复点击复用本条回执，不创建新草稿。</p>}
        {job.status==='verified'&&job.nativeJoined&&<p>ERP 采集列表「加入位置」：亚马逊草稿箱。仅保存草稿，未发布。</p>}
        {job.status==='verified'&&job.handoffMode==='reuse_existing_draft'&&!job.nativeJoined&&<p>本次保留既有草稿；ERP 采集列表的「加入位置」仍为「未加入」，尚未完成 ERP 原生转草稿关联。请勿再次使用「加入到」创建重复草稿。</p>}
        {job.reused&&<p>已定位原采集记录。</p>}{job.error&&<p role="alert">{job.error}</p>}
      </div>}
    </>}{error&&<p role="alert">{error}</p>}{!error&&pollError&&<p role="alert">{pollError}</p>}
  </section>;
}
