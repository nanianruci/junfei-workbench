// Account-local configurable Images API, using the existing durable image jobs.
const renderBeforeOpenAIImages = renderSettings;
renderSettings = async function () {
 await renderBeforeOpenAIImages();
 if (view !== 'settings') return;
 const box = document.createElement('section');
 box.className = 'panel box';
 box.style.cssText = 'max-width:760px;margin-top:24px';
 box.innerHTML = `<div class="wb-title-label"><h2>OpenAI 兼容图片接口</h2>${workbenchHelp('配置本账号图片服务。使用 Codex Tools 时须启动本机反代，地址为 http://127.0.0.1:8787/v1，可用 gpt-image-2 和 Images edits。查询模型不会生图。','OpenAI 兼容图片接口')}</div>
 <label class="field"><span>Base URL</span><input id="customImageBase" type="url" placeholder="https://api.openai.com/v1" autocomplete="url"></label>
 <label class="field"><span>图片模型 ID</span><input id="customImageModel" placeholder="gpt-image-2"></label>
 <label class="field"><span>API Key（留空保留）</span><input id="customImageKey" type="password" autocomplete="off"></label>
 <div class="actions"><label>参考图接口 <select id="customImageProtocol"><option value="edits">Images edits（文件上传）</option><option value="reference_json">Images generations（image_urls）</option></select></label>
 <label>质量 <select id="customImageQuality"><option>low</option><option selected>medium</option><option>high</option><option>auto</option></select></label></div>
 <p id="customImageDimensions" class="sub" hidden></p>
 <div class="actions"><button id="saveCustomImages" class="btn primary">保存并设为当前生图服务</button><button id="testCustomImages" class="btn">查询可用模型（不生图）</button><button id="clearCustomImages" class="btn">清除本账号密钥</button></div>
 <p id="customImageStatus" role="status"></p>`;
 appendModelSettingsPanel('images',box);
 box.querySelectorAll('.field input').forEach(input => input.style.width = '100%');
 const el = id => box.querySelector('#'+id), status = el('customImageStatus');
 try {
  const d = await imageApi('settings');
  if (!box.isConnected) return;
  const c = d.openai || {};
  el('customImageBase').value = c.baseUrl || 'https://api.openai.com/v1';
  el('customImageModel').value = c.model || 'gpt-image-2';
  el('customImageProtocol').value = c.protocol || 'edits';
  el('customImageQuality').value = c.quality || 'medium';
  if (c.outputSize) {
   el('customImageDimensions').hidden = false;
   el('customImageDimensions').textContent = `出图尺寸 ${c.outputSize.replace('x','×')}；上游尺寸不一致时保留原图并等比适配。`;
  }
  status.textContent = `当前生图服务：${d.provider}；本账号兼容接口密钥${c.hasKey?'已配置':'未配置'}。`;
 } catch (error) { status.textContent = error.message; }
 const fields = () => ({provider:'openai', base_url:el('customImageBase').value,
  model:el('customImageModel').value, api_key:el('customImageKey').value,
  protocol:el('customImageProtocol').value, quality:el('customImageQuality').value});
 async function act(button, action) {
  button.disabled = true;
  try { await action(); } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
 }
 el('saveCustomImages').onclick = e => act(e.target, async () => {
  const d = await imageApi('settings', {...fields(), activate:true});
  el('customImageKey').value = '';
  status.textContent = `已保存；当前生图服务：${d.provider}。未提交生图。`;
 });
 el('testCustomImages').onclick = e => act(e.target, async () => {
  // Save the displayed endpoint/model without silently changing active provider.
  await imageApi('settings', fields());
  el('customImageKey').value = '';
  const d = await imageApi('test', {provider:'openai'});
  status.textContent = d.message+' 返回模型：'+d.models.join('、');
 });
 el('clearCustomImages').onclick = e => act(e.target, async () => {
  await imageApi('settings', {provider:'openai', clearKey:true});
  el('customImageKey').value = '';
  status.textContent = '本账号兼容接口密钥已清除；未切换到其他服务。';
 });
};
