import HelpTip from './HelpTip';
import {useEffect,useRef,useState} from 'react';
import SourceColourPicker,{type SourceColourAssociation} from './SourceColourPicker';
type Source={taskId:string;resultIndex:number;image:string;kind:string;templateStyle?:string;brief:string};
type Saved={payload:Record<string,unknown>;taskId?:string};
const DONE=new Set(['completed','needs_revision','failed','submission_unknown','interrupted']);
const KIND:Record<string,string>={white:'白底主图',seller:'细节图',ins:'种草图',model:'模特图'};
async function api(url:string,data?:unknown){const r=await fetch(url,data===undefined?undefined:{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});const body=await r.json();if(!r.ok)throw Object.assign(Error(body.error||`请求失败 ${r.status}`),{status:r.status});return body;}
export default function GeneratedColourVariant({productId,onBusyChange,onSubmitted}:{productId:string;onBusyChange:(busy:boolean)=>void;onSubmitted:()=>void}){
 const [sources,setSources]=useState<Source[]>([]),[selected,setSelected]=useState<Source>(),[reference,setReference]=useState<SourceColourAssociation|null>(null);
 const [brief,setBrief]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const [status,setStatus]=useState(''),[hasSaved,setHasSaved]=useState(false),[attempt,setAttempt]=useState(0);
 const saved=useRef<Saved|null>(null),key=`junfei-generated-variant:${productId}`;
 useEffect(()=>{onBusyChange(busy);return()=>onBusyChange(false)},[busy,onBusyChange]);
 useEffect(()=>{
  let alive=true;setSelected(undefined);setReference(null);setBrief('');setError('');setStatus('');saved.current=null;setHasSaved(false);
  try{const previous=sessionStorage.getItem(key);if(previous){saved.current=JSON.parse(previous);setHasSaved(true)}}catch{setError('无法读取本地接续记录，请先核对近期任务。')}
  const poll=async()=>{if(!saved.current?.taskId)return;try{const r=await api('/api/image-engine/task?id='+encodeURIComponent(saved.current.taskId));if(!alive)return;setStatus(r.task.status);if(['completed','needs_revision','failed'].includes(r.task.status)){saved.current=null;sessionStorage.removeItem(key);setHasSaved(false);onSubmitted()}}catch(e){if(alive)setError((e as Error).message)}};
  void poll();const timer=setInterval(poll,6000);return()=>{alive=false;clearInterval(timer)};
 },[productId,key]);
 useEffect(()=>{let alive=true;setSources([]);void api('/api/image-engine/sources?productId='+encodeURIComponent(productId)).then(r=>{if(alive)setSources(r.sources)}).catch(e=>{if(alive)setError(e.message)});return()=>{alive=false}},[productId,attempt]);
 const active=!!status&&!DONE.has(status);
 async function submit(){
  setError('');setBusy(true);
  try{
   let taskId=saved.current?.taskId;
   if(saved.current&&!taskId){
    const original=await api('/api/image-engine/request?'+new URLSearchParams({
     productId,requestId:String(saved.current.payload.requestId),
    }));
    taskId=original.task?.id;
   }
   if(!saved.current){
    if(!selected||!reference)throw Error('请选择已过检母版图和商品中已关联的真实目标颜色照片。');
    const payload={requestId:crypto.randomUUID(),productId,mode:'repair',references:[selected.image,reference.sourceUpload],sourceVariant:{receiptId:reference.receiptId},count:1,noText:false,editTask:'free',needOptimize:false,
     brief:brief.trim()||`制作 ${reference.color} 颜色变体。以当前商品已关联的真实同款色样为颜色依据，保留母版结构、原模板版式、人物和非目标内容，覆盖所有重复衣料与细节窗；只更新原文中对应的颜色名称。`,
     generatedColourVariant:{sourceTaskId:selected.taskId,sourceResultIndex:selected.resultIndex,targetColour:reference.color}};
    saved.current={payload};sessionStorage.setItem(key,JSON.stringify(saved.current));setHasSaved(true);
   }
   if(taskId){
    saved.current.taskId=taskId;sessionStorage.setItem(key,JSON.stringify(saved.current));
    const r=await api('/api/image-engine/task?id='+encodeURIComponent(taskId));setStatus(r.task.status);onSubmitted();return;
   }
   const r=await api('/api/image-engine/start',saved.current.payload);saved.current.taskId=r.task.id;sessionStorage.setItem(key,JSON.stringify(saved.current));setStatus(r.task.status);onSubmitted();
  }catch(e){const err=e as Error&{status?:number};if(err.status===400||err.status===404){saved.current=null;sessionStorage.removeItem(key);setHasSaved(false)}setError(err.message+(saved.current?'。原请求已保存，请沿原请求查询。':''))}finally{setBusy(false)}
 }
 return <section className="ie-variant" aria-label="生成颜色变体">
  <div className="wb-title-label"><h3>颜色变体</h3><HelpTip label="颜色变体">用 Codex Tools 按母版、同款真实色样和原模板制作变体。检查人物和细节窗中的颜色，尺码图沿用母版。</HelpTip></div>
  <label>选择已过检的母版图<select disabled={busy||hasSaved||active} value={selected?selected.taskId+':'+selected.resultIndex:''} onChange={e=>setSelected(sources.find(s=>s.taskId+':'+s.resultIndex===e.target.value))}>
   <option value="">选择母版图位</option>{sources.map(s=><option key={s.taskId+':'+s.resultIndex} value={s.taskId+':'+s.resultIndex}>{KIND[s.kind]||'商品图'}{s.templateStyle?' · '+s.templateStyle:''} · {s.brief.slice(0,45)} · {s.taskId.slice(0,6)}/{s.resultIndex+1}</option>)}
  </select></label>
  {!sources.length&&<p>暂无可用的已过检母版图，请先完成首色套图。尺码图和失败、未知任务不列入。</p>}
  {selected&&<img className="ie-variant-source" src={selected.image} alt="所选已过检母版图"/>}
  <SourceColourPicker productId={productId} value={reference} disabled={busy||hasSaved||active} onBusyChange={setBusy} onSelect={setReference}/>
  {reference&&<img className="ie-variant-source" src={reference.sourceUpload} alt={reference.color+'真实颜色照片'}/>}
  <label>补充要求<textarea value={brief} disabled={busy||hasSaved||active} rows={3} onChange={e=>setBrief(e.target.value)} placeholder="需要保留的画面内容或本色注意事项"/></label>
  <button className="ie-start" disabled={busy||(!hasSaved&&(!selected||!reference))||active} onClick={()=>void submit()}>{busy?'正在保存与提交…':active?'当前颜色图正在制作':hasSaved?'沿原请求查询 / 接续':'生成颜色变体并检查'}</button>
  <button disabled={busy||active} onClick={()=>{setAttempt(v=>v+1);onSubmitted()}}>刷新母版与任务</button>
  {status&&<p role="status">{status==='completed'?'颜色图已保存，请在下方近期结果查看。':status==='needs_revision'?'当前结果未通过检查，失败图与回执已保留。':'任务状态：'+status}</p>}
  {error&&<p role="alert" className="ie-error">{error}</p>}
 </section>;
}
