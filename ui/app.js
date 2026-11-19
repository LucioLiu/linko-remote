'use strict';
const $ = id => document.getElementById(id);
const nonce = location.hash.slice(1) || sessionStorage.getItem('linko-session') || '';
if(nonce) sessionStorage.setItem('linko-session',nonce);
history.replaceState(null, '', '/');
let state = null, ownCode = null, activeApproval = null, selectedPeer = null, remoteFrame = null, remotePending = null, remoteBusy = false, lastStatePoll = false, closing = false;
async function api(operation, body = {}) {
 const response = await fetch('/api/' + operation, {method:'POST', headers:{'Content-Type':'application/json','X-Linko-Session':nonce}, body:JSON.stringify(body)});
 const value = await response.json();
 if (!value.ok) throw new Error(value.error || '请求没有完成');
 return value.value;
}
function note(message, good = false) { $('notice').textContent = message; $('notice').className = 'notice' + (good ? ' good' : ''); }
function platform(value) { return value === 'macos' ? 'macOS' : value === 'windows' ? 'Windows' : '电脑'; }
function textNode(tag, value, className = '') { const el = document.createElement(tag); el.textContent = value; el.className = className; return el; }
function action(label, handler, primary = false) { const el = textNode('button', label, 'button ' + (primary ? 'primary' : 'secondary')); el.onclick = () => Promise.resolve(handler()).catch(e => note(e.message)); return el; }
function renderDevices() {
 if (!state) return;
 const list = $('devices'), needle = $('device-search').value.trim().toLowerCase(); list.replaceChildren();
 const peers = state.peers.filter(peer => (peer.name + peer.id).toLowerCase().includes(needle));
 if (!peers.length) { const empty = textNode('div','','empty'); empty.append(textNode('b','还没有连接过的电脑'),textNode('span','输入对方连接码，开始第一次连接')); list.append(empty); return; }
 for (const peer of peers) {
  const row = textNode('div','','device-row'), icon = textNode('div','▣','device-icon'), meta = textNode('div','','device-meta');
  meta.append(textNode('strong',peer.name),textNode('small',platform(peer.platform) + ' · ' + peer.id + (peer.incoming ? ' · 正在连接本机' : '')));
  const status = textNode('div',peer.online ? '● 应用在线' : '○ 离线','device-status' + (peer.online ? ' online' : ''));
  const buttons = textNode('div','','row-actions');
  buttons.append(action(peer.can_control ? '打开桌面' : '请求连接',async () => { if(peer.can_control) openRemote(peer); else {await api('access',{peer:peer.id}); note('连接申请已发出，等待对方确认。',true);} },true));
  if(peer.can_control || peer.incoming) buttons.append(action('断开', async()=>{await api('disconnect',{peer:peer.id}); if(selectedPeer?.id===peer.id) closeRemote(); await refresh();}));
  row.append(icon,meta,status,buttons); list.append(row);
 }
}
function renderSSH() {
 const list = $('ssh-list'); list.replaceChildren();
 if(!state.ssh.length){list.append(textNode('div','还没有 SSH 连接，点击右上角添加。','empty')); return;}
 for(const item of state.ssh){const row=textNode('div','','device-row'),meta=textNode('div','','device-meta');meta.append(textNode('strong',item.name),textNode('small',(item.user?item.user+'@':'')+item.host+' · '+item.port)); row.append(textNode('div','›_','device-icon'),meta,action('打开终端',()=>api('ssh-open',{id:item.id}),true));list.append(row);}
}
async function refresh() {
 if(lastStatePoll || closing) return; lastStatePoll=true;
 try {
  state=await api('state'); $('local-name').textContent=state.self.name; $('local-id').textContent=state.self.id || '正在注册'; $('local-platform').textContent=platform(state.self.platform);
  $('incoming').checked=state.incoming; $('service-dot').className='status-dot'+(state.connected?' online':''); $('service-state').textContent=state.connected?'连接服务在线':'连接服务暂不可用'; $('version').textContent=state.version;
  $('generate').disabled=!state.incoming || !state.connected; $('settings-version').textContent=state.version; $('settings-network').textContent=state.connected?'已连接':state.error || '正在连接';
  const ready=state.permissions.screen && state.permissions.input;
  $('permission-text').textContent=ready?'本机屏幕与键鼠权限已就绪':'连接本机需要录屏与辅助功能权限'; $('settings-permissions').textContent=ready?'已就绪':'等待系统权限';
  $('screen-permission').disabled=state.permissions.screen; $('input-permission').disabled=state.permissions.input;
  renderDevices(); renderSSH(); renderUpdates();
  if(!activeApproval && state.approvals.length){activeApproval=state.approvals[0];$('approval-name').textContent=activeApproval.peer.name;$('approval-detail').textContent=platform(activeApproval.peer.platform)+' · '+activeApproval.peer.id;$('approval-dialog').showModal();}
  if(activeApproval && !state.approvals.some(x=>x.id===activeApproval.id)){ $('approval-dialog').close(); activeApproval=null; }
  if(selectedPeer && state.pending[selectedPeer.id] && !remotePending){remotePending=state.pending[selectedPeer.id];$('remote-state').textContent='上一操作结果未确认，请查询原结果';}
 } catch(e) {note(e.message);} finally {lastStatePoll=false;}
}
document.querySelectorAll('.nav').forEach(button=>button.onclick=()=>{document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x===button));document.querySelectorAll('.page').forEach(x=>x.classList.toggle('hidden',x.id!=='page-'+button.dataset.page));$('page-title').textContent=button.dataset.page==='home'?'让远方的电脑，就在手边':button.dataset.page==='ssh'?'连接你的电脑与服务器':'连接与权限，由你决定';});
$('refresh').onclick=refresh;$('device-search').oninput=renderDevices;
$('quit').onclick=async()=>{if(confirm('退出邻控并停止接受连接？')){try{closing=true;await api('shutdown');document.querySelectorAll('button,input').forEach(x=>x.disabled=true);note('邻控已退出，可以关闭这个窗口。',true);}catch(e){closing=false;note(e.message);}}};
$('incoming').onchange=async()=>{const enabled=$('incoming').checked;$('incoming').disabled=true;try{await api('availability',{enabled});if(!enabled){ownCode=null;$('own-code').textContent='•••• •••• ••••';$('copy-code').disabled=true;}await refresh();}catch(e){$('incoming').checked=!enabled;note(e.message);}finally{$('incoming').disabled=false;}};
$('generate').onclick=async()=>{try{ownCode=await api('code');$('own-code').textContent=ownCode.code.match(/.{1,4}/g).join(' ');$('copy-code').disabled=false;$('generate').textContent='刷新连接码';}catch(e){note(e.message);}};
async function copy(value){try{await navigator.clipboard.writeText(value);}catch{const field=document.createElement('textarea');field.value=value;document.body.append(field);field.select();document.execCommand('copy');field.remove();}note('连接码已复制，通过可信渠道发给对方。',true);}
$('copy-code').onclick=()=>ownCode && copy(ownCode.connection);
$('connect-form').onsubmit=async event=>{event.preventDefault();const code=$('connect-code').value.trim();if(!code.startsWith('linko2:') && !/^\d{12}$/.test(code.replace(/[\s-]/g,''))){note('请输入对方的 12 位连接码。');return;}$('connect-button').disabled=true;try{await api('connect',{code});note('连接申请已发出，请等待对方在邻控中确认。',true);$('connect-code').value='';}catch(e){note(e.message);}finally{$('connect-button').disabled=false;}};
$('rename').onclick=()=>{$('new-name').value=state.self.name;$('rename-dialog').showModal();};
$('rename-dialog').addEventListener('close',async()=>{if($('rename-dialog').returnValue==='save'){try{await api('name',{name:$('new-name').value});await refresh();}catch(e){note(e.message);}}});
for(const [id,kind] of [['screen-permission','screen'],['input-permission','input']])$(id).onclick=()=>api('permissions',{kind}).catch(e=>note(e.message));
$('add-ssh').onclick=()=>$('ssh-dialog').showModal();$('ssh-dialog').addEventListener('close',async()=>{if($('ssh-dialog').returnValue==='save'){try{const value=Object.fromEntries(new FormData($('ssh-dialog').querySelector('form')));await api('ssh-save',value);await refresh();}catch(e){note(e.message);}}});
async function approve(allow){if(!activeApproval)return;const id=activeApproval.id;try{await api('approve',{approval:id,allow});$('approval-dialog').close();activeApproval=null;await refresh();}catch(e){note(e.message);}}
$('approve').onclick=()=>approve(true);$('deny').onclick=()=>approve(false);
function openRemote(peer){selectedPeer=peer;remoteFrame=null;remotePending=state.pending[peer.id] || null;$('remote-name').textContent=peer.name;$('remote-image').removeAttribute('src');$('remote-placeholder').classList.remove('hidden');$('remote-state').textContent=remotePending?'存在未确认的操作，请查询原结果':'先取得画面，再进行操作';$('remote-dialog').showModal();if(!remotePending)perform({action:'observe'});}
function closeRemote(){$('remote-dialog').close();selectedPeer=null;remoteFrame=null;remotePending=null;}
$('remote-close').onclick=closeRemote;
function displayReceipt(value){
 if(value.state!=='complete'){$('remote-state').textContent='结果尚未确认，只查询原结果，不重复操作';return false;}
 remotePending=null;const receipt=value.result;remoteFrame=null;
 if(!receipt.ok){$('remote-state').textContent=(receipt.input_attempted?'操作已尝试，需重新取屏核对：':'操作未执行：')+receipt.error;return true;}
 const result=receipt.result || {}, image=result.jpeg_base64 || result.png_base64;
 if(image){$('remote-image').src='data:image/'+(result.jpeg_base64?'jpeg':'png')+';base64,'+image;$('remote-placeholder').classList.add('hidden');remoteFrame=result;}
 $('remote-state').textContent=receipt.input_attempted?'操作回执已收到，请核对画面':'画面已更新 · '+(result.title || '远程电脑');return true;
}
async function recoverPending(){if(!remotePending || remoteBusy)return;remoteBusy=true;try{displayReceipt(await api('recover',{id:remotePending}));}catch(e){$('remote-state').textContent=e.message+'；请保留原操作编号继续查询';}finally{remoteBusy=false;}}
async function perform(request){
 if(!selectedPeer || remoteBusy)return;if(remotePending){$('remote-state').textContent='上一操作未确认，请先查询原结果';return;}
 if(request.action!=='observe'){if(!remoteFrame){$('remote-state').textContent='请先取得新的画面';return;}request.frame_id=remoteFrame.frame_id;}
 remoteBusy=true;$('remote-state').textContent='正在等待回执…';
 try{const response=await api('remote',{peer:selectedPeer.id,request:{...request,image_format:'compact'}});remotePending=response.id;displayReceipt(response);}catch(e){$('remote-state').textContent=e.message;await refresh();}finally{remoteBusy=false;}
}
$('remote-refresh').onclick=()=>perform({action:'observe'});$('remote-recover').onclick=recoverPending;
$('remote-image').onclick=event=>{if(!remoteFrame)return;const rect=event.target.getBoundingClientRect();perform({action:'click',x:Math.min(remoteFrame.bounds[2]-1,Math.floor((event.clientX-rect.left)*remoteFrame.bounds[2]/rect.width)),y:Math.min(remoteFrame.bounds[3]-1,Math.floor((event.clientY-rect.top)*remoteFrame.bounds[3]/rect.height)),button:'left'});};
$('send-text').onclick=()=>{const value=$('remote-text').value;if(value)perform({action:'text',text:value});};document.querySelectorAll('.key').forEach(button=>button.onclick=()=>perform({action:'key',keys:[button.dataset.key]}));
setInterval(()=>{if(ownCode){const seconds=Math.max(0,Math.ceil(ownCode.expires-Date.now()/1000));$('code-hint').textContent=seconds?Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0')+' 后失效 · 对方输入后仍需你确认':'连接码已过期，请重新生成';if(!seconds)$('copy-code').disabled=true;}},1000);
setInterval(refresh,2500);setInterval(()=>{if(remotePending && $('remote-dialog').open)recoverPending();},2000);refresh();

function renderUpdates(){
 const update=state.updates;if(!update)return;
 $('auto-update-check').checked=update.auto_check;
 $('update-state').textContent=update.message;
 const working=['checking','downloading','installing'].includes(update.status);
 $('check-updates').disabled=working;
 $('download-update').classList.toggle('hidden',update.status!=='available');
 $('install-update').classList.toggle('hidden',update.status!=='ready');
 $('update-progress').classList.toggle('hidden',update.status!=='downloading');
 $('update-progress').value=update.progress||0;
 $('update-notes').textContent=update.notes||'';
 $('update-notes-box').classList.toggle('hidden',!update.notes);
 const announced=['available','downloading','ready'].includes(update.status);
 $('update-banner').classList.toggle('hidden',!announced);
 $('update-banner-text').textContent=announced?'邻控有新版本 '+update.latest+'，可在设置中更新。':'';
 $('update-last-result').textContent=update.last_result?.message||'';
}
$('show-updates').onclick=()=>document.querySelector('[data-page="settings"]').click();
$('check-updates').onclick=async()=>{try{await api('update-check');await refresh();}catch(e){note(e.message);}};
$('download-update').onclick=async()=>{try{await api('update-download');await refresh();}catch(e){note(e.message);}};
$('auto-update-check').onchange=async()=>{try{await api('update-preference',{enabled:$('auto-update-check').checked});await refresh();}catch(e){note(e.message);}};
$('install-update').onclick=async()=>{
 if(selectedPeer||remoteBusy||remotePending){note('请先结束远程操作并确认原结果，再安装更新。');return;}
 if(!confirm('安装更新会重新启动邻控。本机电脑名称、配对和 SSH 设置会保留。现在安装？'))return;
 try{await api('update-install');closing=true;document.querySelectorAll('button,input').forEach(x=>x.disabled=true);$('update-state').textContent='正在安装并重新启动，请稍候…';note('更新已开始；新版将自动打开。若此旧窗口未关闭，可以直接关闭。',true);if(window.webkit?.messageHandlers?.linkoUpdate)window.webkit.messageHandlers.linkoUpdate.postMessage('installing');else window.close();}catch(e){note(e.message);}
};
