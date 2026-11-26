import HelpTip from './HelpTip';
import {useEffect, useState} from 'react';

type Candidate = {id: string; images: string[]; usesMaster: boolean; slotCandidates: Record<string,{id:string;image:string}[]>};
type Group = {key: string; color: string; master: boolean; candidates: Candidate[]};
type Choice = {suiteTaskId:string;slotTaskIds:Record<string,string>};
type View = {revision: number; groups: Group[]; selection?: {versionId?:string;taskIds: Record<string, string|Choice>}; reason?: string};

export default function EngineDelivery({productId, accountId, online, onSelection}: {
  productId: string; accountId: number; online: boolean; onSelection: (ready: boolean) => void;
}) {
  const [view, setView] = useState<View>();
  const [chosen, setChosen] = useState<Record<string, Choice>>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const url = '/api/image-engine/delivery';
  const headers = {'Content-Type': 'application/json', 'X-Workspace-Account': String(accountId)};

  async function load(signal?: AbortSignal) {
    const response = await fetch(url + '?productId=' + encodeURIComponent(productId), {headers, signal});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '作图交付记录读取失败');
    if (signal?.aborted) return;
    setView(data);
    const saved:Record<string,Choice>={};
    for(const [key,value] of Object.entries((data.selection?.taskIds||{}) as Record<string,string|Choice>))
      saved[key]=typeof value==='string'?{suiteTaskId:value,slotTaskIds:{}}:value;
    setChosen(saved);
    onSelection(Boolean(data.selection && !data.reason));
  }

  useEffect(() => {
    const controller = new AbortController();
    if (online) void load(controller.signal).catch(err => {
      if (!controller.signal.aborted) setError(err.message);
    });
    return () => controller.abort();
  }, [productId, accountId, online]);

  async function approve() {
    if (!view || !confirmed || busy) return;
    setBusy(true);
    setError('');
    try {
      const response = await fetch(url, {method: 'POST', headers, body: JSON.stringify({
        productId, expectedRevision: view.revision, taskIds: chosen, confirmed: true,
      })});
      const data = await response.json();
      if (!response.ok) throw Error(data.error || '正式图库确认失败');
      setView({...view, selection: data.selection});
      onSelection(true);
      setConfirmed(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!online || (!view?.groups.length && !view?.selection && !error)) return null;
  const masterChoice = view?.groups.find(group => group.master);
  const masterId = masterChoice && chosen[masterChoice.key]?.suiteTaskId;
  const complete = Boolean(view?.groups.length && view.groups.every(group => {
    const choice = chosen[group.key];
    const candidate = group.candidates.find(item => item.id === choice?.suiteTaskId);
    if (!candidate) return false;
    const validSlot = (slot:string, taskId:string) =>
      candidate.slotCandidates[slot]?.some(item => item.id === taskId);
    return Object.entries(choice.slotTaskIds).every(([slot, id]) => validSlot(slot, id))
      && (!candidate.usesMaster || candidate.id === masterId
        && Array.from({length:6}, (_, index) => String(index)).every(slot => validSlot(slot, choice.slotTaskIds[slot])));
  }));
  return <details className="pe-card">
    <summary>确认作图成品用于正式交付 <HelpTip label="图库交付">逐色核对已通过原生质检的七图、真实颜色和子 SKU。所有颜色共用母版的尺码图。</HelpTip></summary>
    {view?.selection && <p role="status">已保存正式图库确认记录{view.selection.versionId?' · '+view.selection.versionId.slice(0,12):''}</p>}
    {view?.reason && <p role="status">{view.reason}</p>}
    {view?.groups.map(group => {
      const selection=chosen[group.key]||{suiteTaskId:'',slotTaskIds:{}};
      const candidate = group.candidates.find(item => item.id === selection.suiteTaskId);
      return <div key={group.key}>
        <label>{group.color}{group.master?'（母版）':''} <select value={selection.suiteTaskId} disabled={busy}
          onChange={event => {setChosen({...chosen, [group.key]: {suiteTaskId:event.target.value,slotTaskIds:{}}});setConfirmed(false)}}>
          <option value="">选择已质检套图</option>
          {group.candidates.map(item => <option key={item.id} value={item.id}
            disabled={item.usesMaster && item.id !== masterId}>
            {item.usesMaster?'沿选定母版组装六图 · ':'独立套图 · '}{item.id.slice(0, 12)}
          </option>)}
        </select></label>
        {candidate && <div className="pe-assets">{candidate.images.slice(0, group.master?7:6).map((image, i) => {
          const slot=String(i),derived=candidate.slotCandidates[slot]||[],selected=selection.slotTaskIds[slot];
          const actual=derived.find(item=>item.id===selected)?.image || (candidate.usesMaster ? '' : image);
          return <figure key={i}>{actual && <a href={actual} target="_blank" rel="noreferrer"><img src={actual} alt={group.color+' 图 '+(i+1)} loading="lazy"/></a>}
            {(candidate.usesMaster||derived.length>0)&&<label>图位{i+1}<select value={selected||''} disabled={busy||!derived.length}
              onChange={event=>{const slots={...selection.slotTaskIds};if(event.target.value)slots[slot]=event.target.value;else delete slots[slot];
                setChosen({...chosen,[group.key]:{...selection,slotTaskIds:slots}});setConfirmed(false)}}>
              <option value="">{candidate.usesMaster?'选择过检颜色变体':'使用套图原图'}</option>{derived.map(item=><option key={item.id} value={item.id}>使用过检颜色变体 · {item.id.slice(0,8)}</option>)}
            </select></label>}</figure>;
        })}</div>}
      </div>;
    })}
    <label><input type="checkbox" disabled={!complete || busy} checked={confirmed}
      onChange={event => setConfirmed(event.target.checked)}/> 我已核对所选颜色和正式图序</label>
    <div className="pe-actions"><button className="pe-primary" disabled={!confirmed || !complete || busy}
      onClick={() => void approve()}>确认正式图库</button>
    <button disabled={busy} onClick={() => {setConfirmed(false);setError('');void load().catch(err => setError(err.message))}}>刷新候选</button></div>
    {error && <p role="alert">{error}</p>}
  </details>;
}
