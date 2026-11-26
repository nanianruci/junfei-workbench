const beforeSellerSpriteSettings=renderSettings;
renderSettings=async function(){
  await beforeSellerSpriteSettings();
  if(view!=='settings'||document.getElementById('sellerSpriteSettings'))return;
  const panel=document.createElement('section');
  const local=typeof localWorkbenchMode!=='undefined'&&localWorkbenchMode;
  panel.id='sellerSpriteSettings';panel.className='panel box';panel.style.cssText='max-width:760px;margin-top:22px';
  panel.innerHTML=`<div class="wb-title-label"><h2>卖家精灵 MCP${local?'':' · 全员共用'}</h2>${workbenchHelp('连接官方 MCP 获取 ASIN 详情、关键词、月搜索量和自然排名。相同查询共享原始数据缓存，详情保留 7 天，关键词和评论保留 24 小时。AI 分析仍单独调用模型。','卖家精灵 MCP')}</div>
    <p style="margin-top:12px"><a href="https://www.sellersprite.com/v3/mcp-manager?tab=keys" target="_blank" rel="noopener">打开 MCP 密钥管理</a> · <a href="https://www.sellersprite.com/v3/knowledge/feature/mcp-installation-tutorial" target="_blank" rel="noopener">官方接入教程</a></p>
    <form id="sellerSpriteForm"><label class="field"><span>MCP 密钥 / 快速集成 URL</span><div style="display:flex;gap:8px"><input id="sellerSpriteKey" type="password" autocomplete="off" placeholder="粘贴密钥或官方快速集成 URL；留空保留已保存配置" style="flex:1;min-width:0"><button type="button" class="btn" id="sellerSpriteEye" aria-label="显示卖家精灵密钥">显示</button></div></label>
    <p id="sellerSpriteState" role="status" style="margin:14px 0">正在读取配置…</p>
    <div class="actions"><button class="btn primary" id="sellerSpriteSave">保存配置</button><button type="button" class="btn" id="sellerSpriteTest">测试连接并获取工具</button><button type="button" class="btn" id="sellerSpriteClear">清除密钥</button></div></form>
    <p id="sellerSpriteResult" role="status" style="margin-top:14px"></p>`;
  appendModelSettingsPanel('seller',panel);
  const key=panel.querySelector('#sellerSpriteKey'),status=panel.querySelector('#sellerSpriteState'),result=panel.querySelector('#sellerSpriteResult');
  const request=async(path,data)=>{const r=await fetch(path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d};
  try{const config=await request('/api/sellersprite/settings');status.textContent=local?(config.hasKey?'✓ MCP 密钥已保存':'请填写卖家精灵 MCP 密钥'):(config.hasKey?'✓ 共用 MCP 密钥已保存，所有账号可使用':'尚未配置共用 MCP，请管理员配置');if(!config.canManage){key.closest('label').hidden=true;for(const id of ['sellerSpriteSave','sellerSpriteClear'])panel.querySelector('#'+id).hidden=true;panel.querySelector('#sellerSpriteForm').onsubmit=e=>e.preventDefault();key.disabled=true;panel.querySelector('#sellerSpriteEye').disabled=true}}catch(e){status.textContent=e.message}
  panel.querySelector('#sellerSpriteEye').onclick=e=>{key.type=key.type==='password'?'text':'password';e.target.textContent=key.type==='password'?'显示':'隐藏';e.target.setAttribute('aria-label',key.type==='password'?'显示卖家精灵密钥':'隐藏卖家精灵密钥')};
  key.oninput=()=>result.textContent='';
  const save=async clear=>{const b=panel.querySelector('#sellerSpriteSave');b.disabled=true;try{const d=await request('/api/sellersprite/settings',{api_key:clear?'':key.value,clearKey:clear});if(clear)key.value='';status.textContent=d.hasKey?'✓ MCP 密钥已加密保存，重启自动加载':'已清除 MCP 密钥';result.textContent='';toast('卖家精灵配置已保存')}catch(e){result.textContent='✕ '+e.message}finally{b.disabled=false}};
  panel.querySelector('#sellerSpriteForm').onsubmit=e=>{e.preventDefault();save(false)};
  panel.querySelector('#sellerSpriteClear').onclick=()=>save(true);
  panel.querySelector('#sellerSpriteTest').onclick=async e=>{const button=e.target,value=key.value;button.disabled=true;result.textContent='正在连接 MCP 并读取工具列表…';try{const d=await request('/api/sellersprite/test',{api_key:value});if(key.value!==value)return;result.textContent=d.message+' 共 '+d.tools.length+' 个工具。'+(d.supported?'已发现 ASIN 详情和关键词反查工具。':'当前工具列表缺少 ASIN 详情或关键词反查，请核对权限。')+(value?' 输入的新密钥需点击保存后才用于竞品读取。':'');result.style.color=d.supported?'#176b50':'#a27829'}catch(e){result.textContent='✕ '+e.message;result.style.color='#ad6452'}finally{button.disabled=false}};
};

// Saved credentials stay out of normal settings responses; reveal only on an eye click.
const beforePersistentSecretDisplay=renderSettings;
renderSettings=async function(){
 await beforePersistentSecretDisplay();
 if(view!=='settings')return;
 for(const [id,service,statusId] of [['apiKey','deepseek','keyState'],['imageKey','apimart','imageKeyStatus'],['sellerSpriteKey','sellersprite','sellerSpriteState']]){
  const input=document.getElementById(id),status=document.getElementById(statusId);if(!input||!status||input.disabled)continue;
  const eye=id==='sellerSpriteKey'?document.getElementById('sellerSpriteEye'):input.parentElement.querySelector('button');
  const update=()=>{const saved=/已配置|已保存|已加密保存/.test(status.textContent)&&!/尚未|已清除/.test(status.textContent);input.dataset.saved=String(saved);input.placeholder=saved?'•••••••• 已保存（点击眼睛查看）':'填写密钥';if(!saved){input.value='';input.type='password'}if(service==='deepseek'&&status.textContent.includes('不回传浏览器'))status.textContent='✓ 密钥已配置（点击眼睛可查看）'};
  const observer=new MutationObserver(update);observer.observe(status,{childList:true,characterData:true,subtree:true});update();
  if(!eye)continue;
  eye.onclick=async()=>{const show=input.type==='password';eye.disabled=true;try{if(show&&!input.value&&input.dataset.saved==='true'){const r=await fetch('/api/credentials/reveal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service})});const d=await r.json();if(!r.ok)throw Error(d.error||'查看密钥失败');if(!input.isConnected)return;input.value=d.key}input.type=show?'text':'password';eye.setAttribute('aria-pressed',String(show));eye.setAttribute('aria-label',show?'隐藏密钥':'显示密钥');eye.title=show?'隐藏密钥':'显示密钥';if(service==='sellersprite')eye.textContent=show?'隐藏':'显示'}catch(e){toast(e.message)}finally{eye.disabled=false}};
 }
};
