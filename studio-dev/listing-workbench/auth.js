// Session bootstrap gates the account workspace and keeps secrets scoped to its owner.
let signedUser=null;
let localWorkbenchMode=false;
const sessionFetch=window.fetch.bind(window);
window.fetch=async function(input,options){
 const path=typeof input==='string'?input:input.url;
 if(signedUser&&path.startsWith('/api/')&&!path.startsWith('/api/auth/')){const headers=new Headers(options?.headers||(typeof input!=='string'?input.headers:undefined));if(!headers.has('X-Workspace-Account'))headers.set('X-Workspace-Account',String(signedUser.id));options={...options,headers};}
 const r=await sessionFetch(input,options);
 if(!localWorkbenchMode&&(r.status===401||r.status===409&&r.headers.get('Content-Type')?.includes('application/json')&&(await r.clone().json()).accountChanged)&&signedUser&&path.includes('/api/')){
  signedUser=null;serverReady=false;clearTimeout(imagePollTimer);localStorage.removeItem('listing-studio-v1');document.getElementById('accountPanel')?.remove();loginScreen();document.getElementById('authMessage').textContent='登录已过期或账号已变化，请重新登录。';
 }
 return r;
};
const authStyle=document.createElement('style');authStyle.textContent=`body.auth-pending .app,body.auth-pending #modal{display:none}#loginScreen{min-height:100vh;display:grid;place-items:center;background:#f1f4f9;padding:28px}#loginCard{width:min(440px,100%);padding:36px;background:white;border:1px solid #e1e7ef;border-radius:20px;box-shadow:0 20px 70px #203f6a12}#loginCard img{width:210px;display:block;margin-bottom:28px}#loginCard label{display:block;margin:18px 0}#loginCard input{display:block;width:100%;padding:12px;margin-top:7px;border:1px solid #ced8e5;border-radius:8px}#loginCard button{margin-top:12px}#authMessage{line-height:1.7;color:#9b3f36}#accountPanel{max-width:760px;width:90vw;padding:28px;border:0;border-radius:16px}#accountPanel::backdrop{background:#152c4866}#accountPanel td{padding:10px;border-bottom:1px solid #e5eaf2}`;document.head.append(authStyle);
async function accountApi(path,data){const r=await fetch('/api/auth/'+path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const d=await r.json();if(!r.ok)throw Error(d.error||'账号请求失败');return d}
function accountHeader(){
 if(!signedUser)return;
 const header=document.querySelector('header .user');
 const status=document.getElementById('serverStatus');
 header.replaceChildren();
 if(status)header.append(status);
 const button=(label,action)=>{const b=document.createElement('button');b.className='btn';b.textContent=label;b.onclick=action;header.append(b)};
 if(localWorkbenchMode){
  const crumb=document.querySelector('header .crumb');
  if(crumb?.firstChild?.nodeType===Node.TEXT_NODE)crumb.firstChild.nodeValue='本机工作空间 ';
  button('采集插件',showLocalCapturePanel);
  return;
 }
 const name=document.createElement('span');
 name.textContent=signedUser.name+' · '+(signedUser.role==='admin'?'管理员':'员工');
 header.append(name);
 button('账号与插件',showAccountPanel);
 button('退出登录',async()=>{try{await flushWorkspace();await accountApi('logout',{});localStorage.removeItem('listing-studio-v1');location.reload()}catch(e){toast(e.message)}});
}
const renderBeforeAccounts=render;render=function(){renderBeforeAccounts();accountHeader()};
function showLocalCapturePanel(){
 document.getElementById('accountPanel')?.remove();
 const dialog=document.createElement('dialog');
 dialog.id='accountPanel';
 dialog.innerHTML='<div class="wb-title-label"><h2>采集插件</h2>'+workbenchHelp('在 EGO 的扩展管理页加载解压后的 extension 文件夹。打开插件，输入本机地址和配对码，采集结果就会保存到工作台。','采集插件')+'</div><p><a class="btn primary" href="/api/auth/extension-download" download>下载商品采集插件</a></p><label class="field">本机地址<input id="captureAddress" readonly></label><button class="btn" id="createCaptureCode" type="button">生成配对码</button><div id="captureCodeLabel" hidden><label class="field">配对码<input id="localCaptureCode" readonly></label></div><p id="localCaptureStatus" role="status"></p><button class="btn" id="closeCapturePanel" type="button">关闭</button>';
 document.body.append(dialog);
 dialog.querySelector('#captureAddress').value='http://127.0.0.1:'+location.port;
 dialog.querySelector('#closeCapturePanel').onclick=()=>dialog.close();
 dialog.addEventListener('close',()=>dialog.remove(),{once:true});
 const create=dialog.querySelector('#createCaptureCode');
 create.onclick=async()=>{
  create.disabled=true;
  const status=dialog.querySelector('#localCaptureStatus');
  try{
   const result=await accountApi('capture-token',{});
   dialog.querySelector('#captureCodeLabel').hidden=false;
   const code=dialog.querySelector('#localCaptureCode');
   code.value=result.token;
   code.select();
   status.textContent=result.message;
  }catch(error){status.textContent=error.message}
  finally{create.disabled=false}
 };
 dialog.showModal();
}
async function showAccountPanel(){document.getElementById('accountPanel')?.remove();const dialog=document.createElement('dialog');dialog.id='accountPanel';dialog.innerHTML='<div class="wb-title-label"><h2>账号与采集插件</h2>'+workbenchHelp('产品、素材、任务和模型配置按账号保存。新员工注册后需管理员批准。','账号与采集插件')+'</div><p><a class="btn primary" href="/api/auth/extension-download" download>下载商品采集插件 v0.7.2</a></p><details><summary>插件安装与采集教程</summary><ol style="line-height:1.9"><li>下载并解压ZIP，保留 extension 文件夹。</li><li>Chrome打开 chrome://extensions；Edge打开 edge://extensions。开启开发者模式，点击“加载已解压的扩展程序”，选择包含 manifest.json 的 extension 文件夹。</li><li>插件地址默认 http://127.0.0.1:18765 ，输入自己的员工账号和密码，点击“登录并绑定账号”，允许连接工作台。登录后显示当前员工，无需复制配对码。</li><li>找货源：先打开 Amazon 商品详情页，点插件“从当前 Amazon 商品图搜货源”。参考图保存到下载目录 AmazonImageSearch，并打开1688；在1688打开图片搜索入口后，可点插件把参考图带入上传框；完成页面验证后人工筛选。</li><li>采集供应商：打开候选1688商品详情页，展开属性和图片，选择站点，点插件“采集到工作台”，成功后回产品档案刷新。相似图片不等于同款或权利已核实。</li><li>切换员工请先在插件退出，再登录其他账号。密码不会保存到插件；完整说明在压缩包 README.md 中。</li></ol></details><div id="staffAccounts"></div><button class="btn" id="closeAccounts">关闭</button>';document.body.append(dialog);dialog.querySelector('#closeAccounts').onclick=()=>dialog.close();dialog.showModal();if(signedUser.role==='admin'){
try{
const d=await accountApi('users');const area=dialog.querySelector('#staffAccounts');
area.innerHTML='<h3>员工审核与账号管理</h3><p class="sub">删除后立即禁止登录和插件采集。产品与模型配置保留，可恢复账号。</p><div id="currentAccounts"></div><details><summary>已删除账号</summary><div id="deletedAccounts"></div></details>';
const renderAccounts=(items,target)=>{
const table=document.createElement('table');table.style.cssText='width:100%;min-width:0;table-layout:fixed';table.innerHTML='<thead><tr><th>账号</th><th>姓名</th><th>状态</th><th>操作</th></tr></thead><tbody></tbody>';target.append(table);
if(!items.length){target.textContent='暂无账号';return}
for(const u of items){const tr=document.createElement('tr');
for(const text of [u.username,u.name,({active:'已启用',pending:'待审核',disabled:'已停用',deleted:'已删除'})[u.status]||u.status]){const td=document.createElement('td');td.textContent=text;td.style.overflowWrap='anywhere';tr.append(td)}
const td=document.createElement('td');
const actionButton=(label,payload,confirmText)=>{const btn=document.createElement('button');btn.className='btn';btn.textContent=label;btn.style.margin='3px';if(['delete','purge'].includes(payload.action))btn.style.color='#b91c1c';btn.onclick=async()=>{if(confirmText&&!confirm(confirmText))return;if(payload.action==='purge'){const name=prompt('此操作无法恢复，会清除该员工的产品、图片、生成记录和模型配置。请输入账号 '+u.username+' 确认：');if(name!==u.username){if(name!==null)toast('账号名称不一致，已取消');return}payload={...payload,confirmUsername:name};}btn.disabled=true;try{const result=await accountApi('users',{id:u.id,...payload});toast(result.message);dialog.close();showAccountPanel()}catch(e){toast(e.message);btn.disabled=false}};td.append(btn)};
if(u.role==='staff'){
if(u.status==='deleted'){actionButton('恢复账号',{action:'restore'});actionButton('彻底删除',{action:'purge'});}
else{actionButton(u.status==='active'?'停用':'通过 / 启用',{status:u.status==='active'?'disabled':'active'});actionButton('删除账号',{action:'delete'},'确定删除账号 '+u.username+'（'+u.name+'）？登录和插件采集立即失效，产品与配置保留，可恢复。')}
}else td.textContent='管理员';tr.append(td);table.querySelector('tbody').append(tr)}
};
renderAccounts(d.users.filter(u=>u.status!=='deleted'),area.querySelector('#currentAccounts'));
renderAccounts(d.users.filter(u=>u.status==='deleted'),area.querySelector('#deletedAccounts'));
}catch(e){toast(e.message)}
}}

function loginLogoSource(){return new URL('assets/junfei-logo.png',location.href).href}
function localWorkbenchEntry(){
 clearAccountDialogs();
 document.body.classList.add('auth-pending');
 document.getElementById('loginScreen')?.remove();
 const screen=document.createElement('section');screen.id='loginScreen';
 screen.innerHTML=`<div id="loginCard"><img src="${loginLogoSource()}" alt="钧飞跨境"><h2>打开商品内容工作台</h2><p>进入本机工作台，继续使用已保存的产品资料和模型配置。</p><a class="btn primary" id="openLocalWorkbench" href="http://127.0.0.1:18775/" style="display:inline-block;margin-top:24px;text-decoration:none">进入工作台</a></div>`;
 document.body.append(screen);
}
function clearAccountDialogs(){
 const modal=document.getElementById('modal');
 if(modal){if(modal.open)modal.close();modal.replaceChildren();}
 document.getElementById('accountPanel')?.remove();
}
function loginScreen(setup=false,register=false){clearAccountDialogs();document.body.classList.add('auth-pending');document.getElementById('loginScreen')?.remove();const screen=document.createElement('section');screen.id='loginScreen';screen.innerHTML=`<form id="loginCard"><img src="${loginLogoSource()}" alt="钧飞跨境"><h2>${setup?'创建首位管理员':register?'员工注册':'登录商品内容工作台'}</h2><p>${setup?'首次使用，请设置管理员账号。':register?'注册后由管理员审核开通。':'登录后继续编辑 Listing 和制作产品图。'}</p>${setup||register?'<label>姓名<input name="name" maxlength="50" required autocomplete="name"></label>':''}<label>账号<input name="username" required minlength="3" maxlength="40" pattern="[A-Za-z0-9_.-]+" autocomplete="username" placeholder="英文字母、数字、点、横线或下划线"></label><label>密码<input name="password" type="password" required minlength="12" maxlength="128" autocomplete="${setup||register?'new-password':'current-password'}" placeholder="至少12位"></label><button class="btn primary" type="submit">${setup?'创建管理员':register?'提交注册':'登录'}</button>${setup?'':'<button class="btn" type="button" id="switchLogin">'+(register?'返回登录':'注册员工账号')+'</button>'}<p id="authMessage" role="status"></p></form>`;document.body.append(screen);screen.querySelector('#switchLogin')?.addEventListener('click',()=>loginScreen(false,!register));screen.querySelector('form').onsubmit=async e=>{e.preventDefault();const button=screen.querySelector('[type=submit]');button.disabled=true;try{const d=await accountApi(setup?'setup':register?'register':'login',Object.fromEntries(new FormData(e.target)));if(setup||register){loginScreen();document.getElementById('authMessage').textContent=d.message}else{signedUser=d.user;if(!await initServer())throw Error('工作台加载失败，请刷新重试');screen.remove();document.body.classList.remove('auth-pending');accountHeader()}}catch(error){document.getElementById('authMessage').textContent=error.message}finally{button.disabled=false}}}
(async()=>{
 if(document.readyState==='loading')await new Promise(resolve=>document.addEventListener('DOMContentLoaded',resolve,{once:true}));
 if(location.protocol==='file:'){localWorkbenchEntry();return}
 try{
  const status=await accountApi('status');
  localWorkbenchMode=status.localMode===true;
  if(status.user){
   signedUser=status.user;
   if(!await initServer())throw Error('工作台加载失败，请刷新重试');
   document.body.classList.remove('auth-pending');
   accountHeader();
  }else{
   loginScreen(status.setup&&!status.localSetupOnly);
   if(status.setup&&status.localSetupOnly)document.getElementById('authMessage').textContent='请先让管理员在主机 http://127.0.0.1:18765 创建管理员账号，再进行员工注册。';
  }
 }catch(error){
  if(localWorkbenchMode){
   const message=document.createElement('p');message.id='authMessage';message.setAttribute('role','alert');
   message.textContent='本机工作台加载失败，请刷新页面。';document.body.append(message);
  }else{
   loginScreen();
   document.getElementById('authMessage').textContent='本机服务未连接，请启动服务后刷新。';
  }
 }
})();

const workbenchStyle=document.createElement('link');workbenchStyle.rel='stylesheet';workbenchStyle.href=new URL('ui-polish.css?v=20261108',location.href).href;document.head.append(workbenchStyle);

