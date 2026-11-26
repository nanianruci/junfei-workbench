import HelpTip from './HelpTip';
import { useEffect, useState } from "react";
import type { ColourResources, ColourSettings, Parameter } from "./OfficialColour";
import "./official-templates.css";
type Item = { id: string; name: string; style?: string; width?: number; height?: number; image?: string; promptLength: number; needOptimize?: boolean };
async function get(url: string) { const r = await fetch(url); const b = await r.json(); if (!r.ok) throw new Error(b.error || "读取原模板失败"); return b; }
export function OfficialTemplates({ kind, selected, onSelect, title, description, disabled = false }: { kind: "layout" | "repair"; selected: string[]; onSelect: (id: string) => void; title?: string; description?: string; disabled?: boolean }) {
  const [items, setItems] = useState<Item[]>([]), [error, setError] = useState(""), [search, setSearch] = useState(""), [group, setGroup] = useState(""), [page, setPage] = useState(0), [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  useEffect(() => { setError(""); setPage(0); void get("/api/image-templates/catalog?kind=" + kind).then(r => setItems(r.items)).catch(e => setError(String(e))); }, [kind]);
  const filtered = items.filter(i => (!group || i.style?.startsWith(group)) && (i.name + i.id + (i.style || "")).toLowerCase().includes(search.toLowerCase()));
  return <details className="official-library" open>
    <summary>{title || (kind === "layout" ? "官方版式库 · 240 张原配图与原提示词" : "官方修图模板 · 24 项完整配置")} {description&&<HelpTip label="原模板">{description}</HelpTip>}</summary>
    <div className="ot-toolbar"><input aria-label="搜索官方模板" placeholder="搜索编号或名称" value={search} onChange={e => { setSearch(e.target.value); setPage(0); }} />
      {kind === "layout" && <select aria-label="版式分组" value={group} onChange={e => { setGroup(e.target.value); setPage(0); }}><option value="">全部分组</option>{["C", "L", "A", "M"].map(g => <option key={g}>{g}</option>)}</select>}
      <span>{filtered.length} 项 · 已选 {selected.length}</span></div>
    {error && <p role="alert">{error}</p>}
    <div className="ot-grid">{filtered.slice(page * 18, page * 18 + 18).map(i => <article key={i.id} className={selected.includes(i.id) ? "is-selected" : ""}>
      {i.image && <img src={i.image + "&preview=1"} alt={i.name + " 官方原配图"} loading="lazy" />}
      <strong>{i.name}</strong><small>{i.width} × {i.height} · 原文 {i.promptLength} 字{i.needOptimize ? " · 含原优化指令" : ""}</small>
      <div><button type="button" disabled={disabled} onClick={() => onSelect(i.id)}>{selected.includes(i.id) ? "取消选用" : "选用"}</button>
        <button type="button" onClick={() => void get("/api/image-templates/item?kind=" + kind + "&id=" + i.id).then(setDetail).catch(e => setError(String(e)))}>查看原文</button></div>
    </article>)}</div>
    <div className="ot-toolbar"><button type="button" disabled={page === 0} onClick={() => setPage(p => p - 1)}>上一页</button><span>{page + 1} / {Math.max(1, Math.ceil(filtered.length / 18))}</span><button type="button" disabled={(page + 1) * 18 >= filtered.length} onClick={() => setPage(p => p + 1)}>下一页</button></div>
    {detail && <div className="ot-detail" role="dialog" aria-label="官方模板原文"><header><strong>{String(detail.name)} · ID {String(detail.id)}</strong><button type="button" onClick={() => setDetail(null)}>关闭</button></header>
      <a href={"/api/image-templates/image?kind=" + kind + "&id=" + detail.id} target="_blank" rel="noreferrer">打开完整原配图</a>
      {kind === "layout" ? <pre>{(detail.modelPrompts as string[]).join("\n\n")}</pre> : <pre>{JSON.stringify(detail.config, null, 2)}</pre>}
      <details><summary>完整原始记录与来源</summary><pre>{JSON.stringify(detail.original, null, 2)}</pre></details>
    </div>}
  </details>;
}
export function OfficialColourControls({ resources, settings, onChange }: { resources: ColourResources | null; settings: ColourSettings; onChange: (v: ColourSettings) => void }) {
  if (!resources) return <p>正在读取原始换色配置…</p>;
  function set(index: string, key: string, value: unknown) { onChange({ ...settings, [index]: { ...settings[index], [key]: value } }); }
  function control(index: string, p: Parameter) {
    const value = settings[index]?.[p.key] ?? p.default;
    if (["clothingMainColor", "targetColor", "L_min_orig", "L_max_orig", "bSetMainColor"].includes(p.key)) return <code title="原始默认值；执行时由原图选区及目标色注入">{JSON.stringify(value)}</code>;
    if (p.type === "int") return <input type="checkbox" checked={Number(value) !== 0} onChange={e => set(index, p.key, e.target.checked ? 1 : 0)} />;
    if (p.type === "float") return <input type="number" min={p.min} max={p.max} step="any" value={Number(value)} onChange={e => set(index, p.key, +e.target.value)} />;
    return <textarea aria-label={p.name} rows={2} defaultValue={JSON.stringify(value)} onBlur={e => { try { set(index, p.key, JSON.parse(e.target.value)); e.target.setCustomValidity(""); } catch { e.target.setCustomValidity("请填写有效JSON数组"); e.target.reportValidity(); } }} />;
  }
  return <details className="official-colour-controls"><summary>官方原始换色滤镜 · 参数与源码 <HelpTip label="换色滤镜">使用原始 GLSL 链，保留选区外像素与透明度。可展开查看原配置、参数范围、默认值和执行顺序。</HelpTip></summary>
    {resources.config.filters.map(f => <details key={f.index}><summary>{f.index}. {f.name} · {f.fs}</summary>
      {f.params.map(p => <label key={p.key}><span>{p.name} <small>{p.min !== undefined ? p.min + "–" + p.max : ""}</small></span>{control(f.index, p)}</label>)}
      <details><summary>原始着色器</summary><pre>{resources.shaders[f.fs]}</pre></details>
    </details>)}
    <details><summary>完整原始配置</summary><pre>{JSON.stringify(resources.config, null, 2)}</pre></details>
  </details>;
}
