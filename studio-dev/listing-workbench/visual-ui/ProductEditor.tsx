import HelpTip from './HelpTip';
import { useEffect, useMemo, useRef, useState } from 'react';
import AboutOpenSource from './AboutOpenSource';
import { createRoot, type Root } from 'react-dom/client';
import { ArrowLeft, ArrowRight, Check, Download, Images, Search, Sparkles } from 'lucide-react';
import type { CreativeSets } from './types';
import KeywordTextarea from './KeywordTextarea';
import {keywordMetricSource,listingKeywords} from './keyword-marks';
import type {KeywordMetric} from './keyword-marks';
import './product-editor.css';
import ErpCollection from './ErpCollection';
import PublicationFlow from './PublicationFlow';
import CodexOperations from './CodexOperations';
import EngineDelivery from './EngineDelivery';
import {colourDeliveryProgress,masterDeliveryProgress,type DeliveryColours,type SelectedGalleryReadiness} from './delivery-readiness';

const steps = [
  ['product','素材与洞察'], ['copy','Listing 文案'], ['visual','商品作图'], ['delivery','交付'],
] as const;
type Step = typeof steps[number][0];
type Copy = Record<string, any>;
type Asset = {url:string; name:string; color?:string};
type InsightImage = {url:string;source:string;stale:boolean;revision?:string;
  observations?:{kind:string;text:string}[];unknowns?:string[];approved?:number[]};
type InsightReport = {images:InsightImage[];version:string};
type GenerationKeyword = {phrase:string;placement:string;metrics:KeywordMetric[]};
type AsinReference = {
  asin:string; market:string; title:string; bullets?:string[]; referenceImages?:string[]; capturedAt?:string;
  variants?:{asin:string;color:string;size:string}[];
  colors?:{color:string;mainImage?:{url:string;sourceAsin:string;sourceUrl:string;checkedAt:string}|null;issue?:string}[];
  insight?:{structured:{usp:string[];target_audience?:string;scenarios?:string[];keywords?:string[];
    image_strategy?:Record<string,string>;cosmo_score?:string|number;optimization_suggestions?:string[]};imageInsights:string};
};
type Product = Record<string, any> & {id:string; name:string; sku?:string};
type Editor = {
  product:Product; accountId:number; products:Product[]; market:string; step:Step; copy:Copy; pending?:Copy;
  identity:(product:Product)=>string;
  review:{copy?:boolean;images?:boolean}; completed?:boolean; online:boolean; busy:boolean;
  prepareCollection:()=>Promise<void>;
  savePublicationCommerce:(details:Record<string,unknown>)=>Promise<void>;
  notify:(message:string)=>void; goBack:()=>void; navigate:(id:string,step:Step)=>void;
  assets:()=>Asset[]; updateFacts:(values:Record<string,string>)=>void;
  updateCopy:(field:string,value:string,translation?:boolean)=>void; getCopy:()=>Copy;
  toggleLock:(field:string)=>Promise<Copy>; batchGenerate:()=>void;
  generate:()=>Promise<void>; progress:()=>void;
  insights:(action:'view'|'analyse'|'confirm',details?:Record<string,unknown>)=>Promise<InsightReport>;
  adopt:(candidate:Copy,base:string)=>void;
  rewrite:(field:string,value:string,mode:string)=>Promise<string>;
  translate:(field:string,value:string)=>Promise<string>;
  upload:(files:File[])=>Promise<void>; readAsin:(asin:string,site:string)=>Promise<AsinReference>;
  analyzeAsin:()=>Promise<AsinReference>;
  editFacts:()=>void; capture:()=>void; variants:()=>void; quotes:()=>void;
  history:()=>void; saveVersion:()=>void; listingSkills:()=>void;
  exportZip:()=>Promise<void>; openReview:()=>void; submit:()=>void;
  mountTools:(element:HTMLElement,kind:'research'|'visual'|'delivery')=>()=>void;
};

const fields = [
  ['title','产品标题'], ['highlights','商品亮点'], ['bullets','五点描述'],
  ['description','产品描述'], ['terms','后台搜索词'],
] as const;
const text = (value:unknown) => value == null ? '' : String(value);

function Tools({ editor, kind }: {editor:Editor;kind:'research'|'visual'|'delivery'}) {
  const ref=useRef<HTMLDivElement>(null);
  const [error,setError]=useState(''),[attempt,setAttempt]=useState(0);
  useEffect(()=>{
    setError('');
    try{return editor.mountTools(ref.current!,kind)}
    catch(error){setError((error as Error).message);}
  },[editor.product.id,kind,attempt]);
  return <><div className="wb-main pe-tools" ref={ref}/>{error&&<section className="pe-card" role="alert">
    <h2>工作区域加载失败</h2><p>{error}</p><button onClick={()=>setAttempt(value=>value+1)}>重新加载</button>
  </section>}</>;
}

function Section({ title, hint, actions, children }: {title:string;hint?:React.ReactNode;actions?:React.ReactNode;children?:React.ReactNode}) {
  return <section className="pe-card"><div className="pe-card-heading"><div className="wb-title-label"><h2>{title}</h2>{hint&&<HelpTip label={title}>{hint}</HelpTip>}</div>
    {actions&&<div className="pe-actions">{actions}</div>}</div>{children}</section>;
}

function ProductMaterials({editor}:{editor:Editor}) {
  const [report,setReport]=useState<InsightReport>();
  const [selected,setSelected]=useState('');
  const [checked,setChecked]=useState<number[]>([]);
  const [busy,setBusy]=useState<'upload'|'analyse'|'confirm'|''>('');
  const [error,setError]=useState('');
  const [uploadVersion,refreshMaterials]=useState(0);
  const [preview,setPreview]=useState('');
  const upload=useRef<HTMLInputElement>(null);
  const assets=new Map(editor.assets().map(asset=>[asset.url,asset]));
  const insightImages=new Map(report?.images.map(image=>[image.url,image]));
  const urls=new Set([...assets.keys(),...insightImages.keys()]);
  useEffect(()=>{
    setReport(undefined);
    setError('');
    if(!editor.online)return;
    let active=true;
    editor.insights('view').then(value=>{if(active)setReport(value)})
      .catch(reason=>{if(active)setError((reason as Error).message)});
    return()=>{active=false};
  },[editor.product.id,editor.online,editor.product.sourceUrl,editor.product.sourceImport,
    editor.product.editorAsinReference?.capturedAt,uploadVersion]);
  const image=insightImages.get(selected);
  useEffect(()=>setChecked(image?.approved||[]),[image]);

  async function addFiles(files:File[]) {
    if(!files.length||busy||!editor.online)return;
    setBusy('upload');
    try{await editor.upload(files)}catch(error){editor.notify((error as Error).message)}
    finally{setReport(undefined);setBusy('');refreshMaterials(n=>n+1)}
  }

  async function save(action:'analyse'|'confirm') {
    if(!image||busy||!editor.online)return;
    setBusy(action);setError('');
    try{
      const details=action==='analyse'?{url:image.url}:{url:image.url,revision:image.revision,indices:checked};
      const next=await editor.insights(action,details);
      setReport(next);
    }catch(reason){setError((reason as Error).message)}
    finally{setBusy('')}
  }
  return <Section title="产品素材库" hint="上传商品实拍图，或在图片卡片上勾选一张商品素材做 AI 洞察。"
    actions={<button onClick={()=>upload.current?.click()} disabled={!!busy||!editor.online}>上传素材</button>}>
    <input type="file" multiple accept="image/png,image/jpeg,image/webp" hidden ref={upload}
      onChange={e=>{void addFiles(Array.from(e.currentTarget.files||[]));e.currentTarget.value=''}}/>
    <button className="pe-drop" disabled={!!busy||!editor.online} onClick={()=>upload.current?.click()}
      onDragOver={e=>e.preventDefault()}
      onDrop={e=>{e.preventDefault();void addFiles(Array.from(e.dataTransfer.files))}}>
      <Images size={18}/>{busy==='upload'?'上传中…':'点击或拖入真实商品图片'}
    </button>
    <div className="pe-assets">{Array.from(urls,(url,index)=>{
      const asset=assets.get(url);
      const insight=insightImages.get(url);
      const name=asset?.name||insight?.source||'商品素材';
      const status=insight?.stale?'待重新分析':insight?.approved?.length?'已确认':insight?.revision?'待确认':'';
      return <article key={url} data-insight-selected={image?.url===url}>
        <button title="查看大图" onClick={()=>setPreview(url)}>
          <img src={url} alt={name+' '+(index+1)} loading="lazy" referrerPolicy="no-referrer"/>
        </button>
        {insight&&<div className="pe-asset-insight">
          <label><input type="checkbox" checked={image?.url===url} disabled={!!busy||!editor.online}
            aria-label={'选中素材 '+(index+1)+' 做 AI 洞察'}
            onChange={e=>setSelected(e.currentTarget.checked?url:'')}/>AI 洞察</label>
          {status&&<span>{status}</span>}
        </div>}
      </article>;
    })}</div>
    {preview&&<button className="pe-lightbox" aria-label="关闭图片预览" onClick={()=>setPreview('')}>
      <img src={preview} alt="素材大图" referrerPolicy="no-referrer"/>
    </button>}
    {error&&<p role="alert">{error}</p>}
    {!editor.online&&<p>连接工作台后可分析素材。</p>}
    {editor.online&&!report&&!error&&<p role="status">正在读取素材洞察…</p>}
    {report?.images.length===0&&<p>上传商品实拍图，或采集同款货源图片后，即可勾选做 AI 洞察。</p>}
    {report&&report.images.length>0&&<div className="pe-material-insights">
      <div className="pe-actions">
        <div className="wb-title-label"><h3>AI 素材洞察</h3><HelpTip label="素材洞察">勾选能够确认属于本款的特征，再保存供文案和分镜使用。</HelpTip></div>
        <button className="pe-primary" disabled={!image||!!busy||!editor.online}
          onClick={()=>void save('analyse')}>{busy==='analyse'?'分析中…':'分析选中图片'}</button>
      </div>
      {image&&<div className="pe-form">
        <img src={image.url} alt="当前分析的商品素材" loading="lazy" referrerPolicy="no-referrer"
          style={{maxWidth:240,maxHeight:240,objectFit:'contain'}}/>
        <div>
          {image.stale&&<p role="status">原图或商品资料已变化，请重新分析并确认。</p>}
          {image.observations?.map((item,i)=><label key={i} style={{display:'block',marginBottom:8}}>
            <input type="checkbox" checked={checked.includes(i)} disabled={!!busy||image.stale}
              onChange={e=>setChecked(e.currentTarget.checked?[...checked,i]:checked.filter(index=>index!==i))}/>
            {' '+item.kind+' · '+item.text}
          </label>)}
          {!!image.unknowns?.length&&<p>待核实 · {image.unknowns.join('、')}</p>}
          {!!image.observations?.length&&<button disabled={!!busy||image.stale||!editor.online}
            onClick={()=>void save('confirm')}>{busy==='confirm'?'保存中…':'保存已确认观察'}</button>}
        </div>
      </div>}
    </div>}
  </Section>;
}

function ProductStep({editor}:{editor:Editor}) {
  const p=editor.product;
  const [facts,setFacts]=useState<Record<string,string>>({});
  const [asinAction,setAsinAction]=useState<'capture'|'insight'|''>('');
  const [asinError,setAsinError]=useState('');
  const [site,setSite]=useState(p.editorAsinReference?.market||p.sourceImport?.market||editor.market);
  const [asin,setAsin]=useState(p.editorAsinReference?.asin||p.sourceImport?.asin||p.sourceImport?.amazon?.asin||'');
  const [asinReference,setAsinReference]=useState<AsinReference|undefined>(p.editorAsinReference);
  const amazon=asinReference||p.sourceImport?.amazon;
  const analysis=asinReference?.insight?.structured;
  const colors=asinReference?.colors||[];
  const variants=asinReference?.variants||[];
  const gallery=asinReference?.referenceImages||[];
  const checkedColors=colors.filter(color=>color.mainImage).length;
  const source=text(p.sourceUrl);
  const sourceLink=/^https?:\/\//i.test(source);
  const factsFields=[
    ['name','产品名称'],['targetAudience','目标受众'],['material','材质与成分'],
    ['style','款式'],['dimensions','尺寸与尺码证据'],['packageContents','包装内容'],
    ['features','核心卖点与场景'],['copyNotes','补充要求'],
  ];
  async function runAsin(action:'capture'|'insight') {
    setAsinAction(action);setAsinError('');
    try {
      const result=action==='capture'?await editor.readAsin(asin.trim().toUpperCase(),site):await editor.analyzeAsin();
      setAsinReference(result);
      editor.notify(action==='capture'?'已保存 ASIN 参考资料':'已保存 ASIN AI 洞察');
    } catch(error) {setAsinError((error as Error).message);}
    finally {setAsinAction('');}
  }
  return <>
    <Section title="ASIN 采集与洞察" hint="先采集 ASIN，再用原始套图和 Listing 文案运行 AI 洞察。"
      actions={<button onClick={editor.capture}>采集货源</button>}>
      <form className="pe-asin-form" onSubmit={e=>{e.preventDefault();void runAsin('capture');}}>
        <div className="pe-actions pe-asin-fields">
          <input aria-label="参考 ASIN" placeholder="填写 10 位 ASIN" value={asin} maxLength={10} required pattern="[A-Za-z0-9]{10}" onChange={e=>setAsin(e.currentTarget.value)}/>
          <select aria-label="ASIN 站点" value={site} onChange={e=>setSite(e.currentTarget.value)}>
            {['US','UK','JP','DE','FR','IT','ES','CA','IN','MX'].map(code=><option key={code}>{code}</option>)}
          </select>
        </div>
        <div className="pe-actions pe-asin-buttons">
          <button className="pe-primary" disabled={!editor.online||!!asinAction}>{asinAction==='capture'?'采集中…':'1. 采集 ASIN 资料'}</button>
          <button type="button" onClick={()=>void runAsin('insight')}
            disabled={!editor.online||!!asinAction||!asinReference?.capturedAt||!gallery.length||
              asin.trim().toUpperCase()!==asinReference.asin||site!==asinReference.market}>
            {asinAction==='insight'?'洞察运行中…':'2. 运行 AI 洞察'}</button>
        </div>
      </form>
      {asinError&&<p role="alert">{asinError}</p>}
      <div className="pe-asin-source">
        <div className="pe-source"><span>{source?'已绑定供应商':'待绑定供应商'}</span><span>参考 ASIN {amazon?.asin||p.sourceImport?.asin||p.asin||'待补充'}</span></div>
        {(amazon?.title||sourceLink)&&<div className="pe-asin-reference">
          {amazon?.title&&<span>{amazon.title}</span>}
          {sourceLink&&<a href={source} target="_blank" rel="noreferrer">打开货源原页</a>}
        </div>}
      </div>
      {Array.isArray(amazon?.bullets)&&amazon.bullets.length>0&&<details><summary>采集五点</summary><ol>{amazon.bullets.map((b:string,i:number)=><li key={i}>{b}</li>)}</ol></details>}
      {asinReference?.capturedAt&&<p role="status">套图 {gallery.length} 张 · 子体 {variants.length} 个 · 主图已核对 {checkedColors}/{colors.length} 色
        {!variants.length&&' · 未读取到颜色变体关系'}</p>}
      {gallery.length>0&&<details><summary>查看 AI 洞察参考套图（{gallery.length} 张）</summary><div className="pe-assets">
        {gallery.filter(url=>/^https:\/\//i.test(url)).map((url,index)=><a href={url} target="_blank" rel="noreferrer" key={url}>
          <img src={url} alt={'ASIN 参考图 '+(index+1)} loading="lazy" referrerPolicy="no-referrer"/></a>)}</div></details>}
      {colors.length>0&&<details><summary>Amazon 参考变体（{colors.length} 色，{variants.length} 个子 ASIN）</summary>
        {colors.map(({color,mainImage,issue})=><details key={color}><summary>{color} · {mainImage?'主图已核对':'主图待核对'}</summary>
          {mainImage&&/^https:\/\//i.test(mainImage.url)&&/^https:\/\//i.test(mainImage.sourceUrl)?
            <div className="pe-assets"><a href={mainImage.sourceUrl} target="_blank" rel="noreferrer">
              <img src={mainImage.url} alt={color+' 主图'} loading="lazy" referrerPolicy="no-referrer"/>
              来源 {mainImage.sourceAsin}</a></div>:<p>{issue||'暂无可核对的主图'}</p>}
          <table><thead><tr><th>原始尺码</th><th>子 ASIN</th><th>主图来源</th></tr></thead><tbody>
            {variants.filter(row=>row.color===color).map(row=><tr key={row.asin}><td>{row.size||'无尺码维度'}</td>
              <td>{row.asin}</td><td>{mainImage?(row.asin===mainImage.sourceAsin?'本子体已核对':'同色共用 '+mainImage.sourceAsin):'待核对'}</td></tr>)}
          </tbody></table></details>)}</details>}
      {analysis&&<details><summary>查看 AI洞察</summary>
        <ul>{analysis.usp.map((item,index)=><li key={index}>{text(item)}</li>)}</ul>
        {analysis.target_audience&&<p>目标受众 · {text(analysis.target_audience)}</p>}
        <pre>{JSON.stringify(analysis,null,2)}</pre>
        <details><summary>图片卖点清单</summary><pre>{asinReference?.insight?.imageInsights}</pre></details>
      </details>}
    </Section>
    <Tools editor={editor} kind="research"/>
    <Section title="产品信息" hint="这些资料会随产品保存，用于文案和视觉策划。"
      actions={<><button onClick={editor.variants}>查看变体</button><button onClick={editor.editFacts}>完整资料与变体</button>
        {Boolean(p.sourceImport?.supplier?.variants?.length)&&<button onClick={editor.quotes}>货源规格与报价</button>}</>}>
      <div className="pe-form">{factsFields.map(([key,label])=><label key={key}>{label}<textarea rows={key==='features'||key==='copyNotes'?3:2} maxLength={2000}
        value={facts[key]??text(p[key])} onBlur={()=>{if(key==='name'&&!facts.name?.trim())setFacts(old=>({...old,name:text(p.name)}))}}
        onChange={e=>{const value=e.currentTarget.value;setFacts(old=>({...old,[key]:value}));editor.updateFacts({[key]:value})}}/>
        </label>)}</div>
      <div className="pe-form">
        <label>刊登原产国<input aria-label="原产国代码" value="CN" readOnly/>
          <span>中国（CN）· 所有产品按工作台统一设置刊登。</span>
        </label>
        <label>刊登护理说明<input aria-label="刊登护理说明" value="Machine Wash" readOnly/>
          <span>机洗（Machine Wash）· 来源于用户统一设置，后续无需逐款填写。</span>
        </label>
      </div>
      {(p.countryOfOrigin||p.careInstructions||p.countryOfOriginEvidence)&&<details><summary>查看原始产地与护理资料</summary>
        <p>原资料保留用于追溯；新发布统一使用以上刊登选项。</p>
        {p.countryOfOrigin&&<p>原资料产地：{text(p.countryOfOrigin)}</p>}
        {p.countryOfOriginEvidence&&<p>原资料产地依据：{text(p.countryOfOriginEvidence)}</p>}
        {p.careInstructions&&<p>原资料护理：{text(p.careInstructions)}</p>}
      </details>}
    </Section>
    <ProductMaterials editor={editor}/>
  </>;
}

function CopyStep({editor}:{editor:Editor}) {
  const [copy,setCopy]=useState(editor.copy);
  const [revision,setRevision]=useState<Copy>();
  const [base,setBase]=useState('');
  const [busy,setBusy]=useState('');
  const [showTranslation,setShowTranslation]=useState(true);
  const [insightVersion,setInsightVersion]=useState<string>();
  useEffect(()=>{
    if(!editor.online)return;
    let active=true;
    editor.insights('view').then(value=>{if(active)setInsightVersion(value.version)}).catch(()=>{});
    return()=>{active=false};
  },[editor.product.id,editor.online,editor.pending]);
  const copyRef=useRef(copy);copyRef.current=copy;
  useEffect(()=>{setCopy(editor.copy);editor.progress()},[editor.copy,editor.pending]);
  const locked=(field:string)=>Array.isArray(copy.lockedFields)&&copy.lockedFields.includes(field);
  const setField=(key:string,value:string,translation=false)=>{
    if(locked(key))return;
    setCopy(previous=>translation?{...previous,translations:{...previous.translations,[key]:value}}:
      {...previous,[key]:value,translations:{...previous.translations,[key]:''}});
    editor.updateCopy(key,value,translation);
  };
  const limits:Record<string,number>={title:75,highlights:125};
  const measure=(value:string)=>Array.from(value).length;
  const keywords=(editor.product.competitorResearch?.[editor.market]?.keywords||[]).filter((word:any)=>word.selected!==false);
  const keywordReport=editor.product.competitorResearch?.[editor.market];
  const keywordData=listingKeywords(keywordReport,copy.generationKeywordInput);
  const keywordSignature=JSON.stringify(keywordData);
  const markedKeywords=useMemo(()=>keywordData,[keywordSignature]);
  const propose=(candidate:Copy)=>{setBase(JSON.stringify(editor.getCopy()));setRevision(candidate)};
  async function assist(key:string,mode:string) {
    if(locked(key))return;
    const value=text(copy[key]);if(!value.trim())return editor.notify('请先填写原文');
    const translated=text(copy.translations?.[key]);
    setBusy(key);
    try{
      const result=mode==='translate'?await editor.translate(key,value):await editor.rewrite(key,value,mode);
      const latest=copyRef.current;
      if(latest.lockedFields?.includes(key)||text(latest[key])!==value||text(latest.translations?.[key])!==translated)return editor.notify('内容或锁定状态已变化，请重新操作');
      if(mode==='translate')setField(key,result,true);
      else propose({...editor.copy,...latest,[key]:result,translations:{...latest.translations,[key]:''},zh:'',contentAudit:undefined});
    }catch(error){editor.notify((error as Error).message)}finally{setBusy('')}
  }
  return <>
    <Section title="文案编辑" hint={<>文案自动保存。{markedKeywords.length>0&&<> <span className="pe-keyword-legend exact">黄色实线</span>表示完整词组，<span className="pe-keyword-legend variant">蓝色虚线</span>表示自然变体。悬停在关键词上可查看原词与卖家精灵数据。</>}</>}
      actions={<><button onClick={editor.listingSkills}>文案技能</button><button onClick={editor.history}>版本对比</button><button onClick={editor.saveVersion}>保存版本</button>
        <button disabled={!editor.online} onClick={editor.batchGenerate}>批量生成</button>
        <button id="editor-generate" className="pe-primary" disabled={!editor.online||editor.busy||busy==='generate'} onClick={async()=>{setBusy('generate');try{await editor.generate()}finally{setBusy('')}}}><Sparkles size={15}/>{editor.busy||busy==='generate'?'生成中…':editor.copy.title?'重新生成':'生成文案'}</button></>}>
      <div id="listingGenerationProgress" className="pe-copy-progress"/>
      <div className="pe-actions pe-copy-actions">
        <div id="listingGenerationActions"/>
        <button onClick={()=>setShowTranslation(!showTranslation)}>{showTranslation?'隐藏中文':'显示中文'}</button>
        <a href={'/api/listing/preview?productId='+encodeURIComponent(editor.product.id)} target="_blank" rel="noreferrer">预览完整 Listing</a></div>
    </Section>
    {insightVersion!==undefined&&[copy,editor.pending].some(draft=>
      typeof draft?.materialInsightVersion==='string'&&draft.materialInsightVersion!==insightVersion)&&
      <p role="status">已确认的素材洞察有更新，请重新核对现有文案和待采用草稿。</p>}
    {editor.pending&&<Section title="新生成草稿" hint="核对新旧文案后选择采用。" actions={<button className="pe-primary" onClick={()=>propose(editor.pending!)}>对比草稿</button>}/>}
    {revision&&<Section title="对比文案" actions={<><button onClick={()=>setRevision(undefined)}>关闭对比</button><button className="pe-primary"
      onClick={()=>{editor.adopt(revision,base);setRevision(undefined)}}>采用新稿</button></>}>
      {fields.filter(([key])=>!locked(key)&&(text(copy[key])!==text(revision[key])||text(copy.translations?.[key])!==text(revision.translations?.[key]))).map(([key,label])=><div key={key}><h3>{label}</h3><div className="pe-form"><pre>{text(copy[key])||'（当前为空）'}</pre><pre>{text(revision[key])||'（新稿为空）'}</pre></div></div>)}
      {fields.some(([key])=>locked(key))&&<p>采用时保留已锁定字段及其中文翻译。</p>}
    </Section>}
    {fields.map(([key,label])=>{
      const value=text(copy[key]),translation=text(copy.translations?.[key]);
      const list=value.split(/\r?\n/),translated=translation.split(/\r?\n/);
      const count=key==='bullets'?Math.max(5,list.length,translated.length):1;
      const hint=key==='bullets'?'生成一套五点，每条 250–500 字符。':
        key==='terms'?`${measure(value)} 字符（200–250） · ${new TextEncoder().encode(value).length} / 249 字节`:
        key==='title'?`${measure(value)} 字符 · 要求 60–75 字符（含空格）`:
        limits[key]?`${measure(value)} / ${limits[key]} 字符`:undefined;
      return <Section key={key} title={label} hint={hint}
        actions={<><button aria-pressed={locked(key)} disabled={!editor.online||Boolean(busy)} onClick={async()=>{
            setBusy('lock');try{setCopy(await editor.toggleLock(key));setRevision(undefined)}catch(error){editor.notify((error as Error).message)}finally{setBusy('')}
          }}>{locked(key)?'已锁定 · 解锁':'锁定字段'}</button>
          <button disabled={!value} onClick={()=>void navigator.clipboard.writeText(value).then(()=>editor.notify('已复制')).catch(()=>editor.notify('复制失败'))}>复制原文</button>
          <button disabled={locked(key)||!editor.online||Boolean(busy)||!value} onClick={()=>void assist(key,'translate')}>AI 翻译</button>
          <select aria-label={label+'改写方式'} value="" disabled={locked(key)||!editor.online||Boolean(busy)||!value} onChange={e=>void assist(key,e.currentTarget.value)}>
            <option value="">AI 改写</option><option value="natural">更自然</option><option value="concise">更精简</option><option value="keywords">融入已选关键词</option></select></>}>
        {Array.from({length:count},(_,i)=><div className={'pe-form'+(showTranslation?'':' pe-one')} key={i}>
          <label>{count>1?'第 '+(i+1)+' 点 · ':''}原文{key==='bullets'&&`（${measure((list[i]||'').trim())} 字符）`}<KeywordTextarea rows={key==='description'?8:key==='bullets'?4:2} value={count>1?list[i]||'':value}
            keywords={markedKeywords} market={editor.market}
            data-copy-field={key} aria-invalid={key==='title'&&(measure(value)<60||measure(value)>75)||undefined} readOnly={locked(key)||busy==='lock'}
            onChange={e=>{const next=e.currentTarget.value;if(key!=='bullets')setField(key,next);else setField(key,Array.from({length:count},(_,n)=>n===i?next.replace(/\r?\n/g,' '):list[n]||'').join('\n'))}}/></label>
          {showTranslation&&<label>{count>1?'第 '+(i+1)+' 点 · ':''}中文翻译<textarea rows={key==='description'?8:key==='bullets'?4:2}
            readOnly={locked(key)||busy==='lock'} value={count>1?translated[i]||'':translation} onChange={e=>{const next=e.currentTarget.value;if(key!=='bullets')setField(key,next,true);else setField(key,Array.from({length:count},(_,n)=>n===i?next.replace(/\r?\n/g,' '):translated[n]||'').join('\n'),true)}}/></label>}
        </div>)}
        {key==='title'&&(measure(value)<60||measure(value)>75)&&<p role="status" className="pe-field-error">{measure(value)<60?`还需 ${60-measure(value)} 个字符`:`超出 ${measure(value)-75} 个字符`}；可继续编辑并自动保存草稿。</p>}
      </Section>;
    })}
    {(copy.warnings||copy.editorialPlan||copy.alternatives||keywords.length||copy.generationKeywordInput) ? <Section title="写作依据与候选">
      {copy.warnings&&<pre>{copy.warnings}</pre>}{copy.editorialPlan&&<details><summary>写作策略</summary><pre>{copy.editorialPlan}</pre></details>}
      {copy.generationKeywordInput&&<details><summary>本次实际入模的卖家精灵关键词（{copy.generationKeywordInput.keywords.length} 个）</summary>
        <ul>{(copy.generationKeywordInput.keywords as GenerationKeyword[]).map(row=><li key={row.phrase}>
          {row.phrase} · {row.placement} · {row.metrics.map(keywordMetricSource).join('、')}
        </li>)}</ul></details>}
      {(copy.alternatives?.titles||[]).map((title:string,i:number)=><p key={i}>{title} <button disabled={locked('title')} onClick={()=>propose({...editor.copy,...copy,title,zh:'',contentAudit:undefined,translations:{...copy.translations,title:''}})}>对比采用</button></p>)}
      {(['bulletsConversion','bulletsQuestions'] as const).map(key=>Array.isArray(copy.alternatives?.[key])&&<details key={key}><summary>{key==='bulletsConversion'?'购买决策五点':'买家问答五点'}</summary><pre>{copy.alternatives[key].join('\n')}</pre>
        <button disabled={locked('bullets')} onClick={()=>propose({...editor.copy,...copy,bullets:copy.alternatives[key].join('\n'),zh:'',contentAudit:undefined,translations:{...copy.translations,bullets:''}})}>对比采用</button></details>)}
      {keywords.length>0&&<details><summary>已选关键词与文案位置</summary><div className="pe-tags">{keywords.map((word:any,i:number)=>{
        const phrase=text(word.phrase);if(!phrase)return null;
        const match=new RegExp('(^|[^\\p{L}\\p{N}])'+phrase.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'(?=$|[^\\p{L}\\p{N}])','iu');
        const matches=fields.filter(([key])=>match.test(text(copy[key])));
        return <button key={i} disabled={!matches.length} onClick={()=>Array.from(document.querySelectorAll<HTMLTextAreaElement>('[data-copy-field]')).find(area=>match.test(area.value))?.focus()}>
          {phrase} · {matches.map(([,label])=>label).join('、')||'未使用'}</button>;
      })}</div></details>}
    </Section>:null}
  </>;
}

function DeliveryStep({editor,sets,error,selectedGallery}:{editor:Editor;sets:CreativeSets;error:string;selectedGallery?:SelectedGalleryReadiness}) {
  const [exporting,setExporting]=useState(false);
  const [erpSourceReadyKey,setErpSourceReadyKey]=useState('');
  const [engineGalleryReady,setEngineGalleryReady]=useState(false);
  const gallery=sets.gallery;
  const colourScope=JSON.stringify([editor.accountId,editor.product.id,editor.product.masterColor,editor.product.generatedGallerySelection,gallery?._projectId]);
  const [colourState,setColourState]=useState<{scope:string;data?:DeliveryColours;error?:string}>({scope:''});
  const [colourAttempt,retryColours]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    setColourState({scope:colourScope});
    if(!editor.online)return()=>controller.abort();
    const load=async()=>{
      try{
        const response=await fetch('/api/colours/sets?productId='+encodeURIComponent(editor.product.id),{signal:controller.signal,headers:{'X-Workspace-Account':String(editor.accountId)}});
        const data=await response.json();if(!response.ok)throw Error(data.error||'变体套图读取失败');
        if(!Array.isArray(data.sets)||!Array.isArray(data.colors)||typeof data.masterColorKey!=='string')throw Error('变体套图记录不完整');
        if(!controller.signal.aborted)setColourState({scope:colourScope,data});
      }catch(error){if(!controller.signal.aborted)setColourState({scope:colourScope,error:(error as Error).message});}
      finally{if(!controller.signal.aborted)timer=setTimeout(load,5000);}
    };
    void load();return()=>{controller.abort();clearTimeout(timer);};
  },[colourScope,editor.online,colourAttempt]);
  const currentColours=colourState.scope===colourScope?colourState:undefined;
  const colours=colourDeliveryProgress(currentColours?.data);
  const master=masterDeliveryProgress(editor.product,gallery,selectedGallery);
  const collectionReady=engineGalleryReady||!error&&master.ready&&colours.ready;
  const collectionBlockReason=error?'母版记录读取失败，请刷新后重试。':!master.ready?master.reason:currentColours?.error?'变体套图读取失败：'+currentColours.error:colours.reason;
  const finals=Object.entries(sets).flatMap(([kind,plan])=>(plan?.images||[]).flatMap((image,i)=>image.final_url?[{image,i,kind}]:[]));
  const fullText=fields.filter(([key])=>editor.copy[key]).map(([key,label])=>label+'\n'+editor.copy[key]).join('\n\n');
  const checks=[
    ['文案已填写',Boolean(editor.copy.title&&editor.copy.bullets&&editor.copy.description)],
    ['正式图库来源已保存',engineGalleryReady||Boolean(gallery?.images?.length)],
    [master.imageLabel,engineGalleryReady||master.imagesReady],
    [master.setLabel,engineGalleryReady||master.setReady],['变体套图检查与 SKU 关联',engineGalleryReady||colours.ready],['已交 ERP 发布',editor.completed===true],
  ] as const;
  const saveText=()=>{
    const url=URL.createObjectURL(new Blob([fullText],{type:'text/plain;charset=utf-8'})),anchor=document.createElement('a');
    anchor.href=url;anchor.download=(editor.product.sku||editor.product.id)+'-Listing.txt';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
  };
  return <>
    <EngineDelivery key={editor.accountId+':'+editor.product.id} productId={editor.product.id}
      accountId={editor.accountId} online={editor.online} onSelection={setEngineGalleryReady}/>
    <ErpCollection key={editor.accountId+':'+editor.product.id+':'+editor.market} productId={editor.product.id} accountId={editor.accountId} market={editor.market} copy={editor.copy} online={editor.online} beforeStart={editor.prepareCollection} deliveryReady={collectionReady} deliveryBlockReason={collectionBlockReason} onVerified={setErpSourceReadyKey}/>
    {editor.market==='US'&&<PublicationFlow key={editor.accountId+':'+editor.product.id} productId={editor.product.id} accountId={editor.accountId} online={editor.online} beforeStart={editor.prepareCollection} saveCommerce={editor.savePublicationCommerce} sourceReadyKey={erpSourceReadyKey}/>} 
    <Section title="自动处理状态" hint="文案和套图完成后交接 ERP，发布受理后档案显示已完成。"
      actions={<><button id="wbExport" disabled={!editor.online||!editor.copy.title||exporting}
        onClick={async()=>{setExporting(true);try{await editor.exportZip()}finally{setExporting(false)}}}><Download size={15}/>{exporting?'正在打包…':'下载可选备份 ZIP'}</button></>}>
      {error&&<p role="alert">{error}</p>}<div className="pe-tags">{checks.map(([label,ok])=><span className={ok?'pe-ok':''} key={label}>{ok?'✓':'○'} {label}</span>)}</div>
      <div aria-label="变体套图交付进度" role="status">
        {currentColours?.data?<><p>当前母版的变体套图：{colours.totalColors} 色 · {colours.totalImages} 张；已出图 {colours.images}/{colours.totalImages}，检查通过 {colours.checked}/{colours.totalImages}，已关联 {colours.linked}/{colours.totalColors} 色。</p>
          <ul>{colours.rows.map(row=><li key={row.key}>{row.color} · 已出图 {row.images}/{row.total} · 检查通过 {row.checked}/{row.total} · {row.state}</li>)}</ul></>:<p>{currentColours?.error?'变体套图读取失败：'+currentColours.error:colours.reason}</p>}
        {!collectionReady&&<p>{collectionBlockReason}</p>}
        <button onClick={()=>retryColours(value=>value+1)} disabled={!editor.online}>刷新套图检查状态</button>
      </div>
    </Section>
    <Section title="Listing 完整文案" actions={<><button disabled={!fullText} onClick={()=>void navigator.clipboard.writeText(fullText).then(()=>editor.notify('已复制')).catch(()=>editor.notify('复制失败'))}>复制全部</button><button disabled={!fullText} onClick={saveText}>下载 TXT</button></>}>
      <pre>{fullText||'请先编辑 Listing 文案。'}</pre>
    </Section>
    <Section title="成图墙" hint="点击图片查看原图。"><div className="pe-assets">
      {finals.map(({image,i,kind})=><article key={kind+i}><a href={image.final_url!} target="_blank" rel="noreferrer"><img src={image.final_url!} alt={image.role||'成图 '+(i+1)} loading="lazy"/></a>
        <div><span>{kind==='gallery'?'套图':'A+'} {i+1} · 生成结果</span><a href={image.final_url!} download>下载</a></div></article>)}
      </div>{!finals.length&&<p>暂无成图，可在商品作图中制作。</p>}
    </Section>
    <Section title="导出设置"><label>母版颜色<input defaultValue={editor.product.masterColor||''} maxLength={100} onChange={e=>editor.updateFacts({masterColor:e.currentTarget.value.trim()})}/></label></Section>
    <Tools editor={editor} kind="delivery"/>
  </>;
}

function Workbench({editor}:{editor:Editor}) {
  const [query,setQuery]=useState('');
  const [sets,setSets]=useState<CreativeSets>({});
  const [selectedGallery,setSelectedGallery]=useState<SelectedGalleryReadiness>();
  const [visualBusy,setVisualBusy]=useState(false);
  const [error,setError]=useState('');
  const p=editor.product;
  useEffect(()=>{
    if(!editor.online)return;
    const controller=new AbortController();
    const load=async()=>{
      try{
        const response=await fetch('/api/studio/view?productId='+encodeURIComponent(p.id),{signal:controller.signal,headers:{'X-Workspace-Account':String(editor.accountId)}});
        const result=await response.json();if(!response.ok)throw Error(result.error||'视觉记录读取失败');
        if(controller.signal.aborted)return;
        setSets(result.sets||{});setSelectedGallery(result.selectedGallery||undefined);setError('');
        setVisualBusy([...Object.values(result.jobs||{}),...Object.values(result.renderJobs||{})].some((job:any)=>job.status==='running'));
      }catch(error){if(!controller.signal.aborted)setError((error as Error).message)}
    };
    void load();const timer=setInterval(load,5000);
    return ()=>{controller.abort();clearInterval(timer)};
  },[editor.online,p.id,editor.accountId]);
  const match=query.trim().toLowerCase();
  const products=editor.products.filter(item=>[item.name,item.sku,item.id,item.asin,item.sourceImport?.asin,
    item.sourceImport?.amazon?.asin,item.editorAsinReference?.asin].some(value=>text(value).toLowerCase().includes(match)));
  const done={product:Boolean(p.material||p.features||p.sourceUrl),copy:Boolean(editor.copy.title),visual:sets.gallery?.set_qa?.ready===true,delivery:editor.completed===true};
  const index=steps.findIndex(([key])=>key===editor.step);
  return <div className="pe-shell">
    <div className="pe-heading"><div><button className="pe-back" onClick={editor.goBack}><ArrowLeft size={16}/> 产品档案</button>
      <h1>Listing 工作台</h1><p>{p.name}</p></div><div className="pe-context product-identity">{editor.identity(p)}</div></div>
    <CodexOperations key={editor.accountId+':'+p.id+':'+editor.market} productId={p.id} market={editor.market}
      accountId={editor.accountId} online={editor.online} beforeApproval={editor.prepareCollection}
      openTasks={()=>{window.location.href='/tasks';}}/>
    <div className="pe-layout"><aside className="pe-rail"><h2>产品项目</h2>
      <label className="pe-search"><input aria-label="搜索产品项目" placeholder="搜索名称、SKU 或 ASIN" value={query} onChange={e=>setQuery(e.currentTarget.value)}/><Search size={16} aria-hidden="true"/></label>
      <div className="pe-projects">{products.map(item=><button key={item.id} className="pe-project-button" aria-current={item.id===p.id?'true':undefined}
        onClick={()=>editor.navigate(item.id,'product')}><strong>{item.name}</strong><span className="product-identity">{editor.identity(item)}</span></button>)}
        {!products.length&&<p>没有匹配的产品。</p>}</div></aside>
      <div className="pe-main"><nav aria-label="Listing 工作流程" className="pe-tabs">{steps.map(([key,label],i)=><button key={key} data-wb-step={key}
        aria-current={key===editor.step?'step':undefined} onClick={()=>editor.navigate(p.id,key)}><i>{i+1}</i>{label}
        {(key==='visual'&&visualBusy||key==='copy'&&editor.busy)?<span>进行中</span>:done[key]&&<Check size={13}/>}</button>)}</nav>
        <div className="pe-step" key={editor.step}>
          {editor.step==='product'&&<ProductStep editor={editor}/>}
          {editor.step==='copy'&&<CopyStep editor={editor}/>}
          {editor.step==='visual'&&<Tools editor={editor} kind="visual"/>}
          {editor.step==='delivery'&&<DeliveryStep key={editor.accountId+':'+editor.product.id+':'+editor.market} editor={editor} sets={sets} error={error} selectedGallery={selectedGallery}/>}
        </div>
        {index<3&&<button className="pe-next" onClick={()=>editor.navigate(p.id,steps[index+1][0])}>下一步 · {steps[index+1][1]} <ArrowRight size={15}/></button>}
      </div></div>
    <footer><AboutOpenSource/></footer>
  </div>;
}

let root:Root|null=null;
let container:HTMLElement|null=null;
declare global {interface Window {JunfeiProductEditor:{mount:(node:HTMLElement,editor:Editor)=>void;unmount:()=>void}}}
window.JunfeiProductEditor={
  mount(node,editor){
    if(container!==node){root?.unmount();root=createRoot(node);container=node}
    root!.render(<Workbench key={editor.accountId+':'+editor.product.id} editor={editor}/>);
  },
  unmount(){root?.unmount();root=null;container=null},
};
