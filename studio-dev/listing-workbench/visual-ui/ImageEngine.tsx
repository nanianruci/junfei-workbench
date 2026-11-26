import HelpTip from './HelpTip';
import { useEffect, useRef, useState } from "react";
import "./image-engine.css";
import { OfficialTemplates, OfficialColourControls } from "./OfficialTemplates";
import { type ColourResources, type ColourSettings } from "./OfficialColour";
import { ApparelSettings, defaultApparelConfig, apparelShotOrder, type ApparelConfig } from "./ApparelSettings";
import RepairTemplateInput, { type RepairNode } from "./RepairTemplateInput";
import GeneratedColourVariant from "./GeneratedColourVariant";
import SourceColourPicker, {type SourceColourAssociation} from './SourceColourPicker';
import { runImageTool, runAutomaticColour } from './ImageTools';

type Result = { image: string; raw: string; rawModelOutput?: string; index: number; templateExecution?: { templateId: string; artworkReferenceSubmitted: boolean; completeOriginalPromptSubmitted: boolean; mode: string }; shot?: { apparelKind?: string }; prompt?: string; references?: string[]; stats?: { branch?: string; renderer?: string; passes?: number; outsideChangedPixels: number }; review?: { blocking?: boolean; issues?: string[]; warnings?: string[] } };
type Task = { id: string; status: string; productName: string; error?: string; request: { mode: string; brief: string; references: string[]; sourceVariant?: {color:string}; generatedColourVariant?: { targetColour: string; template?: { id:string;url:string;prompt:string } } }; results: Result[]; lifestyleSetReview?: { blocking: boolean; issues: string[]; warnings: string[] } };
const STATUS: Record<string, string> = { queued: "排队中", planning: "分析与编排", processing: "选区换色", submitting: "正在提交", generating: "生成中", reviewing: "检查成图", completed: "已检查并保存", needs_revision: "未通过检查", failed: "处理未完成", submission_unknown: "提交待核对", interrupted: "原任务待接续核对" };
const DONE = new Set(["completed", "needs_revision", "failed", "submission_unknown", "interrupted"]);
class HttpError extends Error { constructor(message: string, public status: number) { super(message); } }

async function api(url: string, init?: RequestInit) {
  const r = await fetch(url, init); const body = await r.json();
  if (!r.ok) throw new HttpError(body.error || `请求失败 ${r.status}`, r.status);
  return body;
}
async function upload(blob: Blob) {
  return (await api("/api/upload", { method: "POST", body: blob })).url as string;
}

export default function ImageEngine({ productId }: { productId: string }) {
  const [mode, setMode] = useState("suite");
  const [variantBusy,setVariantBusy]=useState(false);
  const [showAllHistory,setShowAllHistory]=useState(false);
  function changeMode(value:string){setMode(value);try{sessionStorage.setItem(`junfei-image-mode:${productId}`,value)}catch{/* optional preference */}}

  const [layoutIds, setLayoutIds] = useState<string[]>([]);
  const [templateTarget, setTemplateTarget] = useState<"seller" | "ins">("seller");
  const [repairTemplateId, setRepairTemplateId] = useState("");
  const [colourResources, setColourResources] = useState<ColourResources | null>(null);
  const [colourSettings, setColourSettings] = useState<ColourSettings>({ "2": { sigma: 0 }, "3": { sigma: 0 }, "4": { enable: 1 } });
  useEffect(() => { void api("/api/image-templates/colour").then(setColourResources).catch(e => setError(String(e))); }, []);
  const [source, setSource] = useState("");
  const [sourceVariant,setSourceVariant]=useState<SourceColourAssociation|null>(null);
  const sourceEpoch=useRef(0);
  const [brief, setBrief] = useState("");
  const [count, setCount] = useState(7);
  const [editTask, setEditTask] = useState("background");
  const [target, setTarget] = useState("#3868a0");
  const [noText, setNoText] = useState(false);
  const [preserveSourcePixels, setPreserveSourcePixels] = useState(false);
  const [neutralBackground, setNeutralBackground] = useState(true);
  const [apparelConfig, setApparelConfig] = useState<ApparelConfig | null>(defaultApparelConfig);
  const [needOptimize, setNeedOptimize] = useState(false);
  const [repairObjects, setRepairObjects] = useState<RepairNode[] | null>(null);
  const [preparedMask, setPreparedMask] = useState<{ source: string; mask: string; normalized: string; taskId: string } | null>(null);
  const [targetReference, setTargetReference] = useState('');
  const [busyLabel, setBusyLabel] = useState('正在保存请求…');
  const [polish, setPolish] = useState(false);
  const [matchTargetSample, setMatchTargetSample] = useState(true);
  const [autoMask, setAutoMask] = useState(true);
  const [brush, setBrush] = useState(35);
  const [erase, setErase] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [pending, setPending] = useState(false);
  const canvas = useRef<HTMLCanvasElement>(null);
  const drawing = useRef(false);
  const request = useRef<Record<string, unknown> | null>(null);
  const key = `junfei-image-engine-pending:${productId}`;

  useEffect(() => {
    try{const value=sessionStorage.getItem(`junfei-image-mode:${productId}`);setMode(value&&['suite','variant','repair','colour'].includes(value)?value:'suite')}catch{setMode('suite')}
    setShowAllHistory(false);sourceEpoch.current++;setSourceVariant(null);
    setSource(""); setTargetReference(''); setPreparedMask(null); setError(""); request.current = null; setPending(false);
    try { const saved = sessionStorage.getItem(key); if (saved) { request.current = JSON.parse(saved); setPending(true); } } catch { /* unavailable storage */ }
    let alive = true;
    const refresh = async () => {
      try {
        const r = await api(`/api/image-engine/tasks?productId=${encodeURIComponent(productId)}`);
        if (alive) setTasks(r.tasks);
        for (const task of r.tasks as Task[]) if (!DONE.has(task.status)) await api(`/api/image-engine/task?id=${task.id}`);
      } catch (e) { if (alive) setError(String(e instanceof Error ? e.message : e)); }
    };
    void refresh(); const timer = setInterval(refresh, 6000);
    return () => { alive = false; clearInterval(timer); };
  }, [productId, key]);

  function loadMask(url: string) {
    const picture = new Image(); picture.onload = () => {
      const c = canvas.current; if (!c) return;
      if (picture.width !== c.width || picture.height !== c.height) { setError("选区图必须与商品图尺寸一致"); return; }
      c.getContext("2d")?.drawImage(picture, 0, 0);
    }; picture.src = url;
  }
  async function prepareSelection() {
    setError(""); setBusy(true);
    try {
      if (!source) throw new Error("请先上传商品原图");
      const normalized = await api("/api/image-templates/prepare-source", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ productId, source }) });
      const selected = await runImageTool(productId, 'selection', { source: normalized.source.url }, setBusyLabel);
      setPreparedMask({ source, normalized: normalized.source.url, mask: selected.result.mask.url, taskId: selected.id });
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  function paint(e: React.PointerEvent<HTMLCanvasElement>) {
    if (!drawing.current) return;
    const c = e.currentTarget, rect = c.getBoundingClientRect(), ctx = c.getContext("2d"); if (!ctx) return;
    ctx.fillStyle = erase ? "black" : "white";
    ctx.beginPath(); ctx.arc((e.clientX - rect.left) * c.width / rect.width, (e.clientY - rect.top) * c.height / rect.height, brush * c.width / rect.width / 2, 0, Math.PI * 2); ctx.fill();
  }
  async function submit() {
    setError(""); setBusy(true);
    try {
      let payload = request.current;
      let originalTask: Task | undefined;
      if (payload) {
        const query = new URLSearchParams({productId, requestId: String(payload.requestId)});
        const result = await api('/api/image-engine/request?' + query);
        originalTask = result.task || undefined;
      }
      if (!payload) {
        if (!source || !brief.trim()) throw new Error("先上传商品图并填写具体要求");
        let mask: string | undefined; let referenceSource = source;
        let renderedImage: string | undefined; let shaderReceipt: unknown; let effectiveTarget = target;
        let qualityCheckId: string | undefined; let selectionTaskId: string | undefined;
        if (mode === "colour" && !autoMask) {
          const c = canvas.current; if (!c) throw new Error("商品图尚未加载");
          const ctx = c.getContext("2d"); const pixels = ctx?.getImageData(0, 0, c.width, c.height).data;
          if (!pixels || !pixels.some((v, i) => i % 4 === 0 && v > 240)) throw new Error("请涂选需要换色的部件，背景、皮肤、五金和文字留黑");
          const blob = await new Promise<Blob>((resolve, reject) => c.toBlob(b => b ? resolve(b) : reject(new Error("选区保存失败")), "image/png"));
          mask = await upload(blob);
        }
        if (mode === "colour") {
          if (preparedMask?.source === source && autoMask) { referenceSource = preparedMask.normalized; mask = preparedMask.mask; selectionTaskId = preparedMask.taskId; }
          else {
            const normalized = await api("/api/image-templates/prepare-source", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ productId, source }) });
            referenceSource = normalized.source.url;
            if (autoMask) { const prepared = await runImageTool(productId, 'selection', { source: referenceSource }, setBusyLabel); mask = prepared.result.mask.url; selectionTaskId = prepared.id; }
          }
          if (!mask || !colourResources) throw new Error("原始滤镜与商品选区尚未就绪");
          let normalizedReference: string | undefined;
          if (targetReference) {
            const normalized = await api('/api/image-templates/prepare-source', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ productId, source: targetReference }) });
            normalizedReference = normalized.source.url;
            const sample = await runImageTool(productId, 'reference_sample', { source: normalizedReference, role: 'target_colour_reference' }, setBusyLabel);
            effectiveTarget = sample.result.target; setTarget(effectiveTarget);
          }
          const rendered = await runAutomaticColour({ productId, source: referenceSource, mask, selectionTaskId, target: effectiveTarget, reference: normalizedReference,
            autoMask, matchTarget: matchTargetSample, settings: colourSettings, resources: colourResources, progress: setBusyLabel });
          ({ mask, renderedImage, shaderReceipt, qualityCheckId, selectionTaskId } = rendered);
        }
        payload = { requestId: crypto.randomUUID(), productId, mode, sourceVariant: sourceVariant ? {receiptId:sourceVariant.receiptId} : undefined, references: [referenceSource], originalSource: mode === "colour" ? source : undefined, brief, count, editTask, target: effectiveTarget, mask, noText, polish, qualityCheckId, selectionTaskId,
          autoMask: mode === "colour" ? false : autoMask, colourRenderer: mode === "colour" ? "official-glsl" : undefined, renderedImage, shaderReceipt,
          preserveSourcePixels: mode !== "colour" ? preserveSourcePixels : undefined,
          neutralBackground: mode !== "colour" ? neutralBackground : undefined,
          apparelSuite: mode === "suite" && apparelConfig ? { ...apparelConfig, shotOrder: apparelShotOrder(apparelConfig.counts), artDirection: preserveSourcePixels ? "legacy-v1" : "template-led-v1" } : undefined,
          needOptimize: mode === "repair" ? needOptimize : undefined,
          repairObjects: mode === "repair" ? repairObjects || [{ type: "text", text: brief }] : undefined,
          layoutTemplateIds: mode === "suite" && !apparelConfig && layoutIds.length ? layoutIds : undefined,
          repairTemplateId: mode === "repair" && repairTemplateId ? repairTemplateId : undefined };
        request.current = payload; sessionStorage.setItem(key, JSON.stringify(payload)); setPending(true);
      }
      const task: Task = originalTask || (await api("/api/image-engine/start", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
      })).task;
      setTasks(old => [task, ...old.filter(item => item.id !== task.id)]);
      request.current = null; sessionStorage.removeItem(key); setPending(false);
    } catch (e) {
      if (e instanceof HttpError && [400, 404].includes(e.status)) { request.current = null; sessionStorage.removeItem(key); setPending(false); }
      setError(`${e instanceof Error ? e.message : e}${request.current ? "。沿原请求查询，勿另开重复任务。" : ""}`);
    }
    finally { setBusy(false); }
  }

  const visibleTasks=showAllHistory?tasks:tasks.filter(task=>task.status==='completed'||!DONE.has(task.status)||['submission_unknown','interrupted'].includes(task.status)).slice(0,8);
  const refreshHistory=()=>{void api(`/api/image-engine/tasks?productId=${encodeURIComponent(productId)}`).then(r=>setTasks(r.tasks)).catch(e=>setError(e.message))};
  return <section className="image-engine">
    <div className="wb-title-label"><h3>制作与结果</h3><HelpTip label="制作与结果">使用当前账号的 Codex Tools。原模板、真实商品依据、原始输出与检查记录随任务保存。</HelpTip></div>
    <div className="ie-modes" role="group" aria-label="图片创作类型">
      {[["suite", "首色套图"], ["variant", "颜色变体"], ["repair", "智能修图"]].map(([v,title])=><button key={v} aria-pressed={mode===v} disabled={busy||pending||variantBusy} className={mode===v?"selected":""} onClick={()=>changeMode(v)}>{title}</button>)}
    </div>
    <details className="ie-advanced"><summary>高级像素工具</summary><div className="wb-title-label"><button disabled={busy||pending||variantBusy} className={mode==='colour'?'selected':''} onClick={()=>changeMode('colour')}>本地精确选区换色</button><HelpTip label="像素换色">适合需要保留选区外原像素的部件换色。制作模板拼版时使用颜色变体。</HelpTip></div></details>
    {mode==='variant'?<GeneratedColourVariant productId={productId} onBusyChange={setVariantBusy} onSubmitted={refreshHistory}/>:<>
    <div className="ie-workarea">
      <div>
        <SourceColourPicker productId={productId} value={sourceVariant} disabled={busy||pending} onBusyChange={setBusy} onSelect={value=>{sourceEpoch.current++;setSourceVariant(value);setSource(value?.sourceUpload||'');setPreparedMask(null);setError('')}}/>
        <label>商品原图（也可自行上传）<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy || pending} onChange={async e => {
          const file = e.target.files?.[0]; if (!file) return;
          const token=++sourceEpoch.current;setBusy(true);setError("");
          try{const url=await upload(file);if(token===sourceEpoch.current){setSource(url);setSourceVariant(null);setPreparedMask(null)}}
          catch(x){if(token===sourceEpoch.current)setError(String(x))}finally{if(token===sourceEpoch.current)setBusy(false)}
        }} /></label>
        {source && <div className="ie-canvas">
          <img src={source} alt="本次商品原图" onLoad={e => {
            const img = e.currentTarget, c = canvas.current; if (c) { c.width = img.naturalWidth; c.height = img.naturalHeight; const ctx = c.getContext("2d"); if (ctx) { ctx.fillStyle = "black"; ctx.fillRect(0, 0, c.width, c.height); } }
          }} />
          <canvas ref={canvas} style={{ visibility: mode === "colour" && !autoMask ? "visible" : "hidden" }} aria-label="换色选区画布"
            onPointerDown={e => { e.currentTarget.setPointerCapture(e.pointerId); drawing.current = true; paint(e); }}
            onPointerMove={paint} onPointerUp={() => { drawing.current = false; }} onPointerCancel={() => { drawing.current = false; }} />
        </div>}
        {mode === "colour" && <label className="ie-check"><input type="checkbox" checked={autoMask} onChange={e => setAutoMask(e.target.checked)} />自动识别当前服装部件选区（其他商品请手动选区）</label>}
        {mode === "colour" && <label className="ie-check"><input type="checkbox" checked={matchTargetSample} disabled={busy || pending} onChange={e => setMatchTargetSample(e.target.checked)} />匹配目标色样（按原片实测主色校准明暗）</label>}
        {mode === "colour" && autoMask && <button disabled={busy || pending || !source} onClick={() => void prepareSelection()}>查看自动选区</button>}
        {mode === "colour" && preparedMask?.source === source && <div className="ie-mask-preview"><img src={preparedMask.mask} alt="本次换色选区：白色换色，黑色保护" /><p>白色换色，黑色保护；检查流苏间隙、手部和靴子。</p><button disabled={busy || pending} onClick={() => { setAutoMask(false); loadMask(preparedMask.mask); }}>在这张选区上调整</button></div>}
        {mode === "colour" && !autoMask && <div className="ie-selection">
          <p>白色涂选要换色的部件，黑色保留原图；避开皮肤、文字、透明窗与五金。</p>
          <label>画笔大小<input type="range" min="4" max="100" value={brush} onChange={e => setBrush(+e.target.value)} /></label>
          <button onClick={() => setErase(!erase)}>{erase ? "切换到涂选" : "切换到擦除"}</button>
          <label>或导入精确黑白选区<input type="file" accept="image/png" onChange={e => { const f = e.target.files?.[0]; if (f) { const u = URL.createObjectURL(f); loadMask(u); } }} /></label>
        </div>}
      </div>
      <div className="ie-settings">
        <label>本次要求<textarea rows={4} value={brief} disabled={busy || pending} onChange={e => { setBrief(e.target.value); setRepairObjects(null); }} placeholder="说明画面用途、要保留的结构，以及具体需要调整的地方" /></label>
        {mode === "suite" && <>
          <label>自由套图张数<input type="number" min="1" max="8" value={count} disabled={!!apparelConfig || busy || pending} onChange={e => setCount(+e.target.value)} /></label>
          <ApparelSettings value={apparelConfig} sourcePixelMode={preserveSourcePixels} disabled={busy || pending} upload={upload} onChange={value => { setApparelConfig(value); if (value) setCount(Object.values(value.counts).reduce((a, b) => a + b, 0)); }} />
          <label className="ie-check"><input type="checkbox" checked={noText} onChange={e => setNoText(e.target.checked)} />不添加图上文案</label>
          <details className="ie-advanced"><summary>高级 · 保留原片姿态</summary><label className="ie-check"><input type="checkbox" checked={preserveSourcePixels} disabled={busy || pending} onChange={e => setPreserveSourcePixels(e.target.checked)} />锁定原片商品与细节</label>
          {preserveSourcePixels && <p>按官方原版式制作背景与排版，商品及细节使用原片图层；保留原拍摄姿态。</p>}
          {preserveSourcePixels && <label className="ie-check"><input type="checkbox" checked={neutralBackground} onChange={e => setNeutralBackground(e.target.checked)} />中和背景偏暖，商品使用原片颜色</label>}
          </details>
        </>}
        {mode === "repair" && !repairTemplateId && <label>修图任务<select value={editTask} onChange={e => setEditTask(e.target.value)}>
          <option value="background">换背景</option><option value="cleanup">清理干扰物</option><option value="relight">光影调整</option><option value="detail">细节与清晰度</option><option value="free">按要求编辑</option>
        </select></label>}
        {mode === "repair" && <label className="ie-check"><input type="checkbox" checked={needOptimize} disabled={busy || pending} onChange={e => setNeedOptimize(e.target.checked)} />用 Codex Tools 扩写提示词</label>}
        {mode === "repair" && <label className="ie-check"><input type="checkbox" checked={preserveSourcePixels} disabled={busy || pending} onChange={e => setPreserveSourcePixels(e.target.checked)} />保留商品原片，只修背景（保留原姿态）</label>}
        {mode === "repair" && preserveSourcePixels && <p>商品、搭配和细流苏使用原片像素；不重新打光商品或生成新细节。</p>}
        {mode === "repair" && repairTemplateId && <RepairTemplateInput id={repairTemplateId} disabled={busy || pending} onChange={(text, nodes) => { setBrief(text); setRepairObjects(nodes); }} />}
        {mode === "colour" && <>
          <label>同款目标色参考图（自动取色）<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy || pending} onChange={async e => {
            const file = e.target.files?.[0]; if (!file) return;
            setBusy(true); setError(''); try { setTargetReference(await upload(file)); } catch (error) { setError(String(error)); } finally { setBusy(false); }
          }} /></label>
          <div className="wb-title-label"><label>目标色<input type="color" value={target} onChange={e => setTarget(e.target.value)} /><span>{target.toUpperCase()}</span></label><HelpTip label="目标色">按真实目标商品取色，本地预览保留选区外像素。</HelpTip></div>
          {targetReference && <p>本次将从参考图的实际衣料取色。</p>}
          <label className="ie-check"><input type="checkbox" checked={polish} onChange={e => setPolish(e.target.checked)} />追加 Codex Tools 局部光影修整</label>
        </>}
        <button className="ie-start" disabled={busy || (!pending && (!source || !brief.trim()))} onClick={() => void submit()}>{busy ? busyLabel : pending ? "沿原请求查询 / 接续" : mode === "colour" && !polish ? "换色并自动检查" : "开始制作"}</button>
        {error && <p role="alert" className="ie-error">{error}</p>}
      </div>
    </div>
    {mode === "suite" && apparelConfig && <div className="ie-modes" role="group" aria-label="模板用途">{([["seller", "细节图模板"], ["ins", "种草图模板"]] as const).map(([key, label]) => <button key={key} disabled={busy || pending} className={templateTarget === key ? "selected" : ""} onClick={() => setTemplateTarget(key)}>{label}</button>)}</div>}
    {mode === "suite" && <OfficialTemplates kind="layout" selected={apparelConfig ? (templateTarget === "ins" ? apparelConfig.lifestyleTemplateIds || [] : apparelConfig.pointTemplateIds || []) : layoutIds}
      title={apparelConfig ? (templateTarget === "ins" ? "种草图模板 · 每张使用不同原版式" : "细节图排版 · 每种模板生成一张") : "自由模板创作 · 240 张官方原配图与原文"}
      description={apparelConfig ? "细节图和种草图分别绑定原模板配图与完整原文。种草默认选不同版式并检查人物/场景差异；白底、尺码图不借用这些排版。" : "按所选原模板逐张创作。上方可切换为按服装图种制作。"}
      disabled={busy || pending} onSelect={id => {
        if (apparelConfig) {
          const old = (templateTarget === "ins" ? apparelConfig.lifestyleTemplateIds : apparelConfig.pointTemplateIds) || [];
          const next = old.includes(id) ? old.filter(v => v !== id) : [...old, id];
          const others = Object.entries(apparelConfig.counts).reduce((sum, [key, value]) => sum + (key === templateTarget ? 0 : value), 0);
          if (next.length + others > 8) { setError("本次最多8张，请先减少其他图种数量。"); return; }
          const value = { ...apparelConfig, templatePolicy: "point-only-v1" as const, ...(templateTarget === "ins" ? { useLifestyleTemplates: true, lifestyleTemplateIds: next } : { pointTemplateIds: next }), counts: { ...apparelConfig.counts, [templateTarget]: next.length || apparelConfig.counts[templateTarget] } };
          setApparelConfig(value); setCount(Object.values(value.counts).reduce((a, b) => a + b, 0));
        } else setLayoutIds(old => { const next = old.includes(id) ? old.filter(v => v !== id) : old.length < 8 ? [...old, id] : old; if (next.length) setCount(next.length); return next; });
      }} />}
    {mode === "repair" && <OfficialTemplates kind="repair" selected={repairTemplateId ? [repairTemplateId] : []} onSelect={id => setRepairTemplateId(old => old === id ? "" : id)} />}
    {mode === "colour" && <details className="ie-advanced"><summary>原始滤镜参数</summary><OfficialColourControls resources={colourResources} settings={colourSettings} onChange={setColourSettings}/></details>}
    </>}
    <div className="ie-history-heading"><h3>近期结果</h3><button onClick={()=>setShowAllHistory(value=>!value)}>{showAllHistory?'仅看当前结果':`展开近期全部任务（${tasks.length}）`}</button></div>
    <div className="ie-history">{visibleTasks.map(task => <article key={task.id}>
      <header><strong>{task.request.generatedColourVariant?"颜色变体":({ suite: "套图", colour: "本地选区换色", repair: "修图" } as Record<string, string>)[task.request.mode]}</strong><span>{STATUS[task.status] || task.status}</span><small>{task.id.slice(0, 8)}</small></header>
      {task.request.sourceVariant&&<p>来源颜色：{task.request.sourceVariant.color}</p>}
      <details><summary>查看本次要求</summary><p>{task.request.brief}</p></details>{task.error && <p className="ie-error">{task.error}</p>}
      {task.lifestyleSetReview?.issues.map((issue, i) => <p key={"set-issue-" + i} className="ie-error">整组种草图：{issue}</p>)}
      {task.lifestyleSetReview?.warnings.map((warning, i) => <p key={"set-warning-" + i}>整组提醒：{warning}</p>)}
      <div className="ie-results">{task.results.map(r => <figure key={r.index}>
        <a href={r.image} target="_blank" rel="noreferrer"><img src={r.image} alt={`候选成图 ${r.index + 1}`} loading="lazy" /></a>
        <figcaption><a href={r.image} download>保存成图</a><a href={r.rawModelOutput || r.raw} target="_blank" rel="noreferrer">查看原始输出</a></figcaption>
        {r.templateExecution && <small>{r.templateExecution.artworkReferenceSubmitted && r.templateExecution.completeOriginalPromptSubmitted ? "原模板配图与原文已用于生图" : "原片图层版式合成"} · <a href={"/api/image-templates/image?kind=layout&id=" + r.templateExecution.templateId} target="_blank" rel="noreferrer">查看对应原模板</a></small>}
        {task.request.generatedColourVariant&&<small>颜色变体 · {task.request.generatedColourVariant.targetColour}{task.request.generatedColourVariant.template&&r.references?.includes(task.request.generatedColourVariant.template.url)&&r.prompt?.includes(task.request.generatedColourVariant.template.prompt)?' · 原模板配图与完整原文已用于生成':''}</small>}
        {!r.templateExecution && r.shot?.apparelKind === "ins" && <small>独立种草摄影</small>}
        {r.review?.issues?.map((issue, i) => <p key={i} className="ie-error">{issue}</p>)}
        {r.review?.warnings?.map((warning, i) => <p key={i}>提醒：{warning}</p>)}
        {r.stats && <small>{r.stats.renderer === "official_original_glsl" ? "官方原始GLSL · " + r.stats.passes + "级滤镜" : r.stats.branch || "本地换色"} · 选区外改变 {r.stats.outsideChangedPixels} 像素</small>}
      </figure>)}</div>
    </article>)}</div>
  </section>;
}
