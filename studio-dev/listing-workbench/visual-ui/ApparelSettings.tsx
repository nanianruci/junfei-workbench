import HelpTip from './HelpTip';
export type ApparelConfig = {
  counts: Record<"white" | "model" | "ins" | "seller" | "size", number>;
  consistency: { modelReference?: string; sceneReference?: string; exactScene?: boolean; instructions?: string };
  templatePolicy?: "point-only-v1";
  pointTemplateIds?: string[];
  useLifestyleTemplates?: boolean;
  lifestyleTemplateIds?: string[];
  artDirection?: "legacy-v1" | "template-led-v1";
  sizeChartStyle?: "editorial-split-v1" | "classic-v1";
};

export function defaultApparelConfig(): ApparelConfig {
  return { counts: { white: 1, model: 0, ins: 3, seller: 2, size: 1 }, consistency: {}, templatePolicy: "point-only-v1", pointTemplateIds: [], useLifestyleTemplates: true, lifestyleTemplateIds: [], artDirection: "template-led-v1", sizeChartStyle: "editorial-split-v1" };
}

export function apparelShotOrder(counts: ApparelConfig["counts"]) {
  return (["white", "seller", "model", "ins", "size"] as const).flatMap(kind => Array.from({ length: counts[kind] || 0 }, () => kind));
}

export function ApparelSettings({ value, onChange, upload, disabled, sourcePixelMode = false }: {
  value: ApparelConfig | null; onChange: (value: ApparelConfig | null) => void;
  upload: (file: File) => Promise<string>; disabled: boolean; sourcePixelMode?: boolean;
}) {
  const [error, setError] = useState("");
  async function selectReference(file: File, key: "modelReference" | "sceneReference") {
    if (!value) return;
    try { setError(""); const url = await upload(file); onChange({ ...value, consistency: { ...value.consistency, [key]: url } }); }
    catch (e) { setError(String(e)); }
  }
  return <div className="ie-apparel">
    <div className="wb-title-label"><label className="ie-check"><input type="checkbox" disabled={disabled} checked={!!value} onChange={e => onChange(e.target.checked ? defaultApparelConfig() : null)} />按服装图种制作</label><HelpTip label="服装图种">关闭后可自由选模板逐张制作。开启后，细节和种草各选几种原模板就生成几张，白底与尺码图独立。尺码图使用档案中的真实尺寸，缺少依据时停止生成。{sourcePixelMode&&" 需要改变拍摄姿态时，先关闭原片锁定。"}</HelpTip></div>
    {value && <>
      <div className="ie-counts">{([["white", "白底图"], ["model", "同模特图"], ["ins", "种草图"], ["seller", "细节图"], ["size", "尺码图"]] as const).map(([key, name]) => <label key={key}>{name}<input aria-label={name + "数量"} type="number" min="0" max="8" disabled={disabled || ((key === "seller" && !!value.pointTemplateIds?.length) || (key === "ins" && !!value.lifestyleTemplateIds?.length))} value={value.counts[key]} onChange={e => onChange({ ...value, counts: { ...value.counts, [key]: Number(e.target.value) } })} /></label>)}</div>
      {value.counts.size > 0 && <label>尺码图版式<select disabled={disabled} value={value.sizeChartStyle || "editorial-split-v1"} onChange={e => onChange({ ...value, sizeChartStyle: e.target.value as ApparelConfig["sizeChartStyle"] })}><option value="editorial-split-v1">时装目录 · 大商品与清晰表格</option><option value="classic-v1">简洁数据表</option></select></label>}
      {value.counts.model > 0 && <>
        <label>模特参考图{value.consistency.modelReference && <span>已上传</span>}<input type="file" accept="image/png,image/jpeg,image/webp" disabled={disabled} onChange={e => { const file = e.target.files?.[0]; if (file) void selectReference(file, "modelReference"); }} /></label>
        <label>场景参考图{value.consistency.sceneReference && <span>已上传</span>}<input type="file" accept="image/png,image/jpeg,image/webp" disabled={disabled} onChange={e => { const file = e.target.files?.[0]; if (file) void selectReference(file, "sceneReference"); }} /></label>
        <label className="ie-check"><input type="checkbox" disabled={disabled || !value.consistency.sceneReference} checked={!!value.consistency.exactScene} onChange={e => onChange({ ...value, consistency: { ...value.consistency, exactScene: e.target.checked } })} />遵循指定场景</label>
        <label>模特与场景要求<input disabled={disabled} value={value.consistency.instructions || ""} onChange={e => onChange({ ...value, consistency: { ...value.consistency, instructions: e.target.value } })} /></label>
      </>}
    </>}{error && <p role="alert">{error}</p>}
  </div>;
}
import { useState } from "react";
