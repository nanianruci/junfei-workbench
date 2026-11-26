import HelpTip from './HelpTip';
import {useState} from 'react';
import ImageEngine from './ImageEngine';
import LegacyVisualStep from './LegacyVisualStep';
import type {ListingState} from './bridge';

export default function ImageWorkflow({state}:{state:ListingState}){
 const [aplusOpen,setAplusOpen]=useState(false);
 const history=state.creativeSets.gallery?.images||[];
 const running=[...Object.values(state.jobs),...Object.values(state.renderJobs)].some(job=>job?.status==='running');
 return <section className="image-workflow">
  <div className="image-workflow-heading wb-title-label"><h2>商品作图</h2>
   <HelpTip label="商品作图">先完成首色套图（白底 1 张、细节 2 张、种草 3 张、尺码 1 张），再用过检母版和同款真实色样制作变体。细节和种草各用原模板，三张种草图应有明显差异。所有颜色共用有真实依据的尺码图。逐张核对后保存，局部修改可用智能修图。</HelpTip>
  </div>
  <ImageEngine productId={state.project.id}/>
  <details className="image-workflow-compat"><summary>历史分镜与成图 · {history.length}个图位</summary>
   {running&&<p role="status">原分镜任务仍在后台运行，任务及回执继续保留。</p>}
   {history.length?<div className="ie-results">{history.map((image,index)=><figure key={index}>{image.final_url?<a href={image.final_url} target="_blank" rel="noreferrer"><img src={image.final_url} alt={`历史图位 ${index+1}`} loading="lazy"/></a>:<p>图位{index+1} · 暂无成图</p>}<figcaption>{index+1} · {image.role||image.shot_type||'历史图位'}{image.final_url&&<a href={image.final_url} download>保存原成图</a>}</figcaption><small>{image.render_qa?.ready?'原检查通过':'保留原检查状态'}</small></figure>)}</div>:<p>暂无历史商品分镜。</p>}
  </details>
  <details className="image-workflow-compat" onToggle={event=>setAplusOpen(event.currentTarget.open)}><summary>A+ 内容制作</summary>{aplusOpen&&<LegacyVisualStep state={state}/>}</details>
 </section>;
}
