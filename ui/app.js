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
 const list = $('devices'), needle = $('device-search').value.trim().toLowerCase();
 const waiting = new Set((state.outgoing || []).filter(item => item.state === 'pending').map(item => item.target));
 const signature=JSON.stringify([state.peers,needle,[...waiting]]);if(renderDevices.signature===signature)return;renderDevices.signature=signature;list.replaceChildren();
 const peers = state.peers.filter(peer => (peer.name + peer.id).toLowerCase().includes(needle));
 if (!peers.length) { const empty = textNode('div','','empty'); empty.append(textNode('b','还没有连接过的电脑'),textNode('span','输入对方连接码，开始第一次连接')); list.append(empty); return; }
 for (const peer of peers) {
  const row = textNode('div','','device-row'), icon = textNode('div','▣','device-icon'), meta = textNode('div','','device-meta');
  meta.append(textNode('strong',peer.name),textNode('small',platform(peer.platform) + ' · ' + peer.id + (peer.trusted_incoming ? ' · 已长期授权' : peer.incoming ? ' · 已授权连接本机' : '')));
  const status = textNode('div',peer.online ? '● 应用在线' : '○ 离线','device-status' + (peer.online ? ' online' : ''));
  const buttons = textNode('div','','row-actions');
  const pending = !peer.can_control && waiting.has(peer.id);
  const connect = action(peer.can_control ? '打开桌面' : pending ? '等待对方同意' : '请求连接',async () => {
   if(peer.can_control){openRemote(peer);return;}
   connect.disabled=true;
   try{await api('access',{peer:peer.id});note('请对方打开邻控，在两分钟内同意。',true);await refresh();}
   finally{connect.disabled=false;}
  },true);
  connect.disabled=pending;
  buttons.append(connect);
  if(peer.can_control || peer.incoming || peer.trusted_incoming) buttons.append(action(peer.trusted_incoming ? '撤销信任并断开' : '断开', async()=>{await api('disconnect',{peer:peer.id}); if(selectedPeer?.id===peer.id) closeRemote(); await refresh();}));
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
  if(!activeApproval && state.approvals.length){activeApproval=state.approvals[0];$('approval-name').textContent=activeApproval.peer.name;$('approval-detail').textContent=platform(activeApproval.peer.platform)+' · '+activeApproval.peer.id;$('approval-permission').textContent=state.supports_long_term_trust
   ? '允许后，只要本机开启接收连接，这台电脑就可以查看屏幕并操作键鼠。授权不会到期，可在设备列表撤销。'
   : '允许后，对方可在一小时内查看本机屏幕并操作键鼠。';
  $('approve').textContent=state.supports_long_term_trust?'允许长期连接':'允许本次连接';
  $('approval-dialog').showModal();}
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
async function approve(allow,persistent=false){if(!activeApproval)return;const id=activeApproval.id;try{await api('approve',{approval:id,allow,...(persistent?{persistent:true}:{})});$('approval-dialog').close();activeApproval=null;await refresh();}catch(e){note(e.message);}}
$('approve').onclick=()=>approve(true,state.supports_long_term_trust===true);
$('deny').onclick=()=>approve(false);
function openRemote(peer){
 selectedPeer=peer;remoteFrame=null;remotePending=state.pending[peer.id] || null;
 $('remote-name').textContent=peer.name;$('remote-image').removeAttribute('src');
 $('remote-mode').hidden=!peer.live;
 $('remote-placeholder').classList.remove('hidden');
 $('remote-state').textContent=remotePending?'存在未确认的操作，请查询原结果':'先取得画面，再进行操作';
 $('remote-dialog').showModal();
 if(!remotePending)perform({action:'observe'});
}
function closeRemote(){stopLive().catch(e=>note(e.message));$('remote-dialog').close();selectedPeer=null;remoteFrame=null;remotePending=null;}
$('remote-close').onclick=closeRemote;
function displayReceipt(value){
 if(value.state!=='complete'){$('remote-state').textContent='结果尚未确认，只查询原结果，不重复操作';return false;}
 remotePending=null;const receipt=value.result;remoteFrame=null;
 if(!receipt.ok){$('remote-state').textContent=(receipt.input_attempted?'操作已尝试，需重新取屏核对：':'操作未执行：')+receipt.error;return true;}
 const result=receipt.result || {}, image=result.jpeg_base64 || result.png_base64;
 if(image){$('remote-image').src='data:image/'+(result.jpeg_base64?'jpeg':'png')+';base64,'+image;$('remote-placeholder').classList.add('hidden');remoteFrame=result;}
 if(!image){$('remote-state').textContent='原操作回执已保留，请重新取得画面';return true;}
 $('remote-state').textContent=receipt.input_attempted?'操作回执已收到，请核对画面':'画面已更新 · '+(result.title || '远程电脑');return true;
}
async function recoverPending(){if(!remotePending || remoteBusy)return;remoteBusy=true;try{displayReceipt(await api('recover',{id:remotePending}));}catch(e){$('remote-state').textContent=e.message+'；请保留原操作编号继续查询';}finally{remoteBusy=false;}}
async function perform(request){
 if(!selectedPeer || remoteBusy || liveOpening || liveClosing)return;if(remotePending){$('remote-state').textContent='上一操作未确认，请先查询原结果';return;}
 if(request.action!=='observe'){if(!remoteFrame){$('remote-state').textContent='请先取得新的画面';return;}request.frame_id=remoteFrame.frame_id;}
 const peerId=selectedPeer.id;remoteBusy=true;$('remote-state').textContent='正在等待回执…';
 try{const response=await api('remote',{peer:selectedPeer.id,request:{...request,image_format:'compact'}});if(selectedPeer?.id===peerId && !live){remotePending=response.id;displayReceipt(response);}}catch(e){$('remote-state').textContent=e.message;await refresh();}finally{remoteBusy=false;}
}
$('remote-refresh').onclick=()=>live?restartLive():perform({action:'observe'});$('remote-recover').onclick=recoverPending;
$('remote-image').onclick=event=>{if(live || !remoteFrame)return;const rect=event.target.getBoundingClientRect();perform({action:'click',x:Math.min(remoteFrame.bounds[2]-1,Math.floor((event.clientX-rect.left)*remoteFrame.bounds[2]/rect.width)),y:Math.min(remoteFrame.bounds[3]-1,Math.floor((event.clientY-rect.top)*remoteFrame.bounds[3]/rect.height)),button:'left'});};
$('send-text').onclick=()=>{const value=$('remote-text').value;if(value){if(live){if(value.length>500){$('remote-state').textContent='连续桌面每次最多发送 500 字，请分段发送';return;}queueLive({type:'text',text:value});}else perform({action:'text',text:value});}};document.querySelectorAll('.key').forEach(button=>button.onclick=()=>{if(live){const code={ENTER:'Enter',TAB:'Tab',ESC:'Escape',BACKSPACE:'Backspace'}[button.dataset.key];queueLive({type:'keydown',code});queueLive({type:'keyup',code});}else perform({action:'key',keys:[button.dataset.key]});});
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

// Human live desktop: bounded latest-frame display and ordered input batches.
// No input HTTP request is retried. A missing acknowledgement pauses control.
let live=null,liveOpening=false;
const liveImage=$('remote-image');
let liveClosing=null;
function stopLive(){
 const prior=live;live=null;
 if(prior){
  clearTimeout(prior.timer);clearTimeout(prior.inputTimer);
  liveImage.removeAttribute('src');remoteFrame=null;
  $('remote-placeholder').classList.remove('hidden');
  liveClosing=api('live-close',{id:prior.id}).finally(()=>{liveClosing=null;});
 }
 $('remote-mode').textContent='连续桌面';$('remote-refresh').textContent='取新画面';$('remote-recover').hidden=false;
 return liveClosing || Promise.resolve();
}
async function startLive(){
 if(!selectedPeer || liveOpening || remoteBusy || liveClosing)return;if(remotePending){$('remote-state').textContent='请先查询上一操作结果';return;}
 liveOpening=true;const peer=selectedPeer;
 try{const value=await api('live-open',{peer:peer.id});if(selectedPeer!==peer){await api('live-close',{id:value.id});return;}
  remoteFrame=null;
  liveImage.src='/live-feed/'+value.id+'?session='+encodeURIComponent(nonce);
  live={id:value.id,seq:0,ack:0,queue:[],sending:false,frame:null,arrivals:[],frameAt:0,halt:false};
  $('remote-mode').textContent='逐步操作';$('remote-refresh').textContent='重新连接';$('remote-recover').hidden=true;$('remote-state').textContent='正在建立连续画面…';pollLive(live);pumpLive(live);
 }catch(e){$('remote-state').textContent=e.message;}finally{liveOpening=false;}
}
async function restartLive(){
 const peer=selectedPeer;
 try{
  await stopLive();
  if(selectedPeer===peer)await startLive();
 }catch(e){showLiveCloseError(e);}
}
function showLiveCloseError(e){$('remote-state').textContent='连续桌面关闭结果待确认：'+e.message;}
$('remote-mode').onclick=async()=>{
 if(!live){await startLive();return;}
 const peer=selectedPeer;
 try{
  await stopLive();
  if(selectedPeer===peer)await perform({action:'observe'});
 }catch(e){showLiveCloseError(e);}
};
async function pollLive(session){
 if(live!==session)return;
 try{
  const value=await api('live-view',{id:session.id});if(live!==session)return;session.ack=value.ack;
  if(value.error){
   session.halt=true;session.queue=[];
   const uncertain=session.seq!==session.ack;
   $('remote-state').textContent=value.error+(uncertain?' · 有键鼠尚未确认，请先核对画面':' · 可以重新连接或取新画面');
   await stopLive().catch(showLiveCloseError);
   return;
  }
  const frame=value.frame;
  if(frame && (!session.frame || frame.seq!==session.frame.seq)){
   const now=performance.now();session.arrivals=session.arrivals.filter(t=>now-t<2000);session.arrivals.push(now);session.frame=frame;session.frameAt=now-value.frame_age_ms;
   $('remote-placeholder').classList.add('hidden');
   if(!session.halt)$('remote-state').textContent='连续桌面 · '+Number(value.fps || 0).toFixed(1)+' 帧/秒 · 点击画面使用键鼠';
  }else if(performance.now()-session.frameAt>2000 && !session.halt){$('remote-state').textContent='正在等待新画面，键鼠已暂停';}
 }catch(e){if(live===session){session.halt=true;session.queue=[];$('remote-state').textContent='连接中断，已停止键鼠：'+e.message;stopLive().catch(showLiveCloseError);}}
 if(live===session)session.timer=setTimeout(()=>pollLive(session),80);
}
function queueLive(event){
 const s=live;if(!s || s.halt || !s.frame || performance.now()-s.frameAt>1500)return;
 if(s.queue.length>=128){s.halt=true;s.queue=[];$('remote-state').textContent='网络拥堵，已停止输入，请重新连接画面';stopLive();return;}
 if(event.type==='move' && s.queue.at(-1)?.type==='move')s.queue[s.queue.length-1]=event;else s.queue.push(event);
}
async function pumpLive(session){
 if(live!==session)return;
 if(!session.halt && session.frame && performance.now()-session.frameAt>1500)session.queue=[];
 if(!session.halt && session.queue.length && session.seq-session.ack<6 && !session.sending){
  session.sending=true;const events=session.queue.splice(0,64),seq=++session.seq;
  try{await api('live-input',{id:session.id,seq,geometry:session.frame.geometry,events});}
  catch(e){if(live===session){session.halt=true;session.queue=[];$('remote-state').textContent='键鼠结果未确认，已停止输入：'+e.message;api('live-close',{id:session.id}).catch(()=>{});}}
  finally{session.sending=false;}
 }
 if(live===session)session.inputTimer=setTimeout(()=>pumpLive(session),30);
}
function livePoint(e){const r=liveImage.getBoundingClientRect();return {x:Math.max(0,Math.min(65535,Math.round((e.clientX-r.left)/r.width*65535))),y:Math.max(0,Math.min(65535,Math.round((e.clientY-r.top)/r.height*65535)))};}
liveImage.addEventListener('pointermove',e=>{if(live)queueLive({type:'move',...livePoint(e)});});
liveImage.addEventListener('pointerdown',e=>{if(!live)return;e.preventDefault();liveImage.focus();liveImage.setPointerCapture(e.pointerId);queueLive({type:'down',button:e.button,...livePoint(e)});});
liveImage.addEventListener('pointerup',e=>{if(!live)return;e.preventDefault();queueLive({type:'up',button:e.button,...livePoint(e)});if(liveImage.hasPointerCapture(e.pointerId))liveImage.releasePointerCapture(e.pointerId);});
liveImage.addEventListener('pointercancel',()=>queueLive({type:'release'}));
liveImage.addEventListener('contextmenu',e=>{if(live)e.preventDefault();});
liveImage.addEventListener('wheel',e=>{if(live){e.preventDefault();queueLive({type:'wheel',amount:e.deltaY>0?-1:1});}},{passive:false});
const liveCodes=new Set(["KeyA", "KeyB", "KeyC", "KeyD", "KeyE", "KeyF", "KeyG", "KeyH", "KeyI", "KeyJ", "KeyK", "KeyL", "KeyM", "KeyN", "KeyO", "KeyP", "KeyQ", "KeyR", "KeyS", "KeyT", "KeyU", "KeyV", "KeyW", "KeyX", "KeyY", "KeyZ", "Digit0", "Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "Digit6", "Digit7", "Digit8", "Digit9", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End", "PageUp", "PageDown", "ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight", "AltLeft", "AltRight", "MetaLeft", "MetaRight", "Minus", "Equal", "BracketLeft", "BracketRight", "Backslash", "Semicolon", "Quote", "Comma", "Period", "Slash", "Backquote"]);
for(const type of ['keydown','keyup'])liveImage.addEventListener(type,e=>{if(!live || e.isComposing || !liveCodes.has(e.code))return;e.preventDefault();queueLive({type,code:e.code});});
liveImage.addEventListener('compositionend',e=>{if(live && e.data)queueLive({type:'text',text:e.data.slice(0,500)});});
function releaseLiveInput(){const s=live;if(!s)return;if(s.halt)return;if(s.frame && performance.now()-s.frameAt<=1500)queueLive({type:'release'});else api('live-close',{id:s.id}).catch(()=>{});}
liveImage.addEventListener('blur',releaseLiveInput);
window.addEventListener('blur',releaseLiveInput);
document.addEventListener('visibilitychange',()=>{if(document.hidden && live)releaseLiveInput();});
$('remote-dialog').addEventListener('cancel',()=>closeRemote());

liveImage.addEventListener('paste',e=>{if(live){e.preventDefault();const text=e.clipboardData?.getData('text/plain') || '';if(text.length>500){$('remote-state').textContent='每次最多粘贴 500 字，请分段发送';return;}if(text)queueLive({type:'text',text});}});
