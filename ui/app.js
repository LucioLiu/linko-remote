'use strict';
const $ = id => document.getElementById(id);
const nonce = location.hash.slice(1) || sessionStorage.getItem('linko-session') || '';
if(nonce) sessionStorage.setItem('linko-session',nonce);
history.replaceState(null, '', '/');
let state = null, ownCode = null, activeApproval = null, selectedPeer = null, remoteFrame = null, remotePending = null, remoteBusy = false, lastStatePoll = false, closing = false;
let changingAvailability = false;
let remotePendingRestored = false;
async function api(operation, body = {}) {
 const response = await fetch('/api/' + operation, {method:'POST', headers:{'Content-Type':'application/json','X-Linko-Session':nonce}, body:JSON.stringify(body)});
 const value = await response.json();
 if (!value.ok) throw new Error(value.error || '请求没有完成');
 return value.value;
}
function note(message, good = false) { $('notice').textContent = message; $('notice').className = 'notice' + (good ? ' good' : ''); }
function platform(value) { return {macos:'macOS',windows:'Windows',linux:'Linux'}[value] || '电脑'; }
function textNode(tag, value, className = '') { const el = document.createElement(tag); el.textContent = value; el.className = className; return el; }
function action(label, handler, primary = false) { const el = textNode('button', label, 'button ' + (primary ? 'primary' : 'secondary')); el.onclick = () => Promise.resolve(handler()).catch(e => note(e.message)); return el; }
function renderDevices() {
 if (!state) return;
 const list = $('devices'), needle = $('device-search').value.trim().toLowerCase();
 const waiting = new Set((state.outgoing || []).filter(item => item.state === 'pending').map(item => item.target));
 const signature=JSON.stringify([state.peers,needle,[...waiting],state.connected,state.pending,state.file_version]);if(renderDevices.signature===signature)return;renderDevices.signature=signature;list.replaceChildren();
 const peers = state.peers.filter(peer => (peer.name + peer.id).toLowerCase().includes(needle));
 if (!peers.length) { const empty = textNode('div','','empty'); empty.append(textNode('b','还没有连接过的电脑'),textNode('span','输入对方连接码，开始第一次连接')); list.append(empty); return; }
 for (const peer of peers) {
  const row = textNode('div','','device-row'), icon = textNode('div','▣','device-icon'), meta = textNode('div','','device-meta');
  meta.append(textNode('strong',peer.name),textNode('small',platform(peer.platform) + ' · ' + peer.id + (peer.trusted_incoming ? ' · 已长期授权' : peer.incoming ? ' · 已授权连接本机' : '')));
  const online = state.connected && peer.online;
  const paused = peer.accepting_connections === false;
  const initiatorOnly = peer.accepts_desktop === false;
  const statusLabel = initiatorOnly ? '● 在线 · 可发起连接' : paused ? '● 在线 · 暂停接收' : '● 应用在线';
  const status = textNode('div',online ? statusLabel : state.connected ? '○ 离线' : '○ 状态待更新','device-status' + (online ? ' online' : ''));
  const buttons = textNode('div','','row-actions');
  const pending = !peer.can_control && waiting.has(peer.id);
  const awaitingReceipt = Boolean(state.pending[peer.id]);
  const connect = action(awaitingReceipt ? '查询原结果' : initiatorOnly ? '可发起连接' : peer.can_control ? '打开桌面' : pending ? '等待对方同意' : '请求连接',async () => {
   if(awaitingReceipt || peer.can_control){openRemote(peer);return;}
   connect.disabled=true;
   try{await api('access',{peer:peer.id});note('请对方打开邻控，在两分钟内同意。',true);await refresh();}
   finally{connect.disabled=false;}
  },true);
  connect.disabled=!state.connected || (!awaitingReceipt && (!online || paused || pending || initiatorOnly));
  buttons.append(connect);
  if (typeof openFiles === 'function') buttons.append(action('传文件', () => openFiles(peer.id)));
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
  if(state.portable){
   document.title=$('app-name').textContent='邻控试用版';
   $('ssh-nav').classList.add('hidden');
   $('quit').textContent='退出试用版并停止接受连接';
  }
  if(!changingAvailability)$('incoming').checked=state.incoming;
  $('service-dot').className='status-dot'+(state.connected?' online':'');
  $('service-state').textContent=state.connected?'连接服务在线':state.error?'连接服务暂不可用':state.self.id?'正在恢复连接服务':'正在连接服务';
  $('version').textContent=state.version;
  $('generate').disabled=!state.incoming || !state.connected; $('settings-version').textContent=state.version; $('settings-network').textContent=state.connected?'已连接':state.error || '正在连接';
  const ready=state.permissions.screen && state.permissions.input;
  const controllerOnly=state.supports_incoming===false;
  $('receive-row').classList.toggle('hidden',controllerOnly); $('code-area').classList.toggle('hidden',controllerOnly);
  $('permission-text').textContent=controllerOnly?'Linux 候选版支持发起远程桌面和 SSH 连接':state.pause_pending?'本机已暂停接收，等待与连接服务同步':state.agent_error || (ready?'本机屏幕与键鼠权限已就绪':'连接本机需要录屏与辅助功能权限');
  $('settings-permissions').textContent=controllerOnly?'可发起连接':state.agent_error || (ready?'已就绪':'等待系统权限');
  $('screen-permission').classList.toggle('hidden',controllerOnly); $('input-permission').classList.toggle('hidden',controllerOnly);
  $('screen-permission').disabled=state.permissions.screen; $('input-permission').disabled=state.permissions.input;
  syncInviteActions(); renderDevices(); renderSSH(); renderUpdates(); if (typeof renderFiles === 'function') renderFiles();
  const approvals=state.connected && state.incoming ? state.approvals : [];
  if(!activeApproval && approvals.length){activeApproval=approvals[0];$('approval-name').textContent=activeApproval.peer.name;$('approval-detail').textContent=platform(activeApproval.peer.platform)+' · '+activeApproval.peer.id;$('approval-permission').textContent=state.supports_long_term_trust
   ? '允许本次连接后，对方可在一小时内操作本机。长期授权可在设备列表撤销。'
   : '允许后，对方可在一小时内查看本机屏幕并操作键鼠。';
  $('approve-persistent').classList.toggle('hidden',!state.supports_long_term_trust);
  $('approval-dialog').showModal();}
  if(activeApproval && !approvals.some(x=>x.id===activeApproval.id)){ $('approval-dialog').close(); activeApproval=null; }
  if(selectedPeer && state.pending[selectedPeer.id] && !remotePending){remotePending=state.pending[selectedPeer.id];remotePendingRestored=true;$('remote-state').textContent='上一操作结果未确认，请查询原结果';}
 } catch(e) {note(e.message);} finally {lastStatePoll=false;}
}
document.querySelectorAll('.nav').forEach(button=>button.onclick=()=>{document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x===button));document.querySelectorAll('.page').forEach(x=>x.classList.toggle('hidden',x.id!=='page-'+button.dataset.page));$('page-title').textContent=button.dataset.page==='home'?'让远方的电脑，就在手边':button.dataset.page==='files'?'文件传输':button.dataset.page==='ssh'?'连接你的电脑与服务器':'连接与权限，由你决定';});
$('refresh').onclick=refresh;$('device-search').oninput=renderDevices;
$('quit').onclick=async()=>{if(confirm('退出邻控并停止接受连接？')){try{closing=true;await api('shutdown');document.querySelectorAll('button,input').forEach(x=>x.disabled=true);note('邻控已退出，可以关闭这个窗口。',true);}catch(e){closing=false;note(e.message);}}};
$('incoming').onchange=async()=>{
 const enabled=$('incoming').checked;
 changingAvailability=true;$('incoming').disabled=true;
 try{
  const result=await api('availability',{enabled});
  if(!result.incoming){ownCode=null;syncInviteActions();}
  if(result.sync_pending)note('本机已暂停接收，联网后会同步。',true);
 }catch(e){note(e.message);}
 finally{changingAvailability=false;$('incoming').disabled=false;await refresh();}
};
function connectionCode(value){
 if(typeof value!=='string')return '';
 const code=value.trim();
 if(/^linko2:[a-zA-Z0-9_-]{1,2000}$/.test(code))return code;
 const digits=code.replace(/[\s-]/g,'');
 return /^\d{12}$/.test(digits)?digits:'';
}
function readInvitation(text, now=Date.now()/1000){
 let value;
 try{value=JSON.parse(text);}catch{throw new Error('邀请文件内容无效。');}
 if(!value || value.kind!=='linko-invitation' || value.version!==1 || !connectionCode(value.code) || !Number.isFinite(value.expires)){
  throw new Error('邀请文件内容无效。');
 }
 if(value.expires<=now)throw new Error('邀请已过期，请对方重新生成。');
 return connectionCode(value.code);
}
function syncInviteActions(){
 const seconds=ownCode?Math.max(0,Math.ceil(ownCode.expires-Date.now()/1000)):0;
 const canShare=Boolean(seconds && state?.connected && state.incoming);
 $('copy-code').disabled=!canShare; $('save-invite').disabled=!canShare;
 $('generate').textContent=canShare?'刷新连接码':'生成连接码';
 $('own-code').textContent=canShare?ownCode.code.match(/.{1,4}/g).join(' '):'•••• •••• ••••';
 $('code-hint').textContent=canShare?Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0')+' 后失效 · 对方发起后需你确认':ownCode && !seconds?'连接码已过期，请重新生成':'允许连接后，生成 10 分钟有效的连接码';
}
$('generate').onclick=async()=>{try{ownCode=await api('code');syncInviteActions();}catch(e){note(e.message);}};
async function copy(value){
 try{await navigator.clipboard.writeText(value);}
 catch{
  const field=document.createElement('textarea');field.value=value;document.body.append(field);field.select();
  try{if(!document.execCommand('copy'))throw new Error('复制失败，请手动选中连接码。');}
  finally{field.remove();}
 }
 note('连接码已复制。',true);
}
$('copy-code').onclick=()=>{if(ownCode && ownCode.expires>Date.now()/1000)copy(ownCode.connection || ownCode.code).catch(e=>note(e.message));};
$('save-invite').onclick=async()=>{
 if(!ownCode || ownCode.expires<=Date.now()/1000)return;
 const contents=JSON.stringify({kind:'linko-invitation',version:1,code:ownCode.connection || ownCode.code,expires:ownCode.expires});
 const nativeSave=window.webkit?.messageHandlers?.linkoInvite;
 if(nativeSave){
  try{if(await nativeSave.postMessage(contents))note('邀请文件已保存。',true);}
  catch(e){note(e.message || '邀请文件保存失败。');}
  return;
 }
 const url=URL.createObjectURL(new Blob([contents],{type:'application/json'}));
 const link=document.createElement('a');link.href=url;link.download='邻控邀请.linko';
 document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
};
$('open-invite').onclick=()=>$('invite-file').click();
$('invite-file').onchange=async()=>{
 const input=$('invite-file'), file=input.files[0];input.value='';
 if(!file)return;
 try{
  if(file.size>4096)throw new Error('邀请文件内容无效。');
  $('connect-code').value=readInvitation(await file.text());
  $('connect-button').focus();note('邀请已读取。点击「发起连接」后，等待对方确认。',true);
 }catch(e){note(e.message);}
};
$('connect-form').onsubmit=async event=>{
 event.preventDefault();const code=connectionCode($('connect-code').value);
 if(!code){note('请输入有效的邻控连接码，或打开邀请文件。');return;}
 $('connect-button').disabled=true;
 try{await api('connect',{code});note('连接申请已发出，请等待对方在邻控中确认。',true);$('connect-code').value='';}
 catch(e){note(e.message);}finally{$('connect-button').disabled=false;}
};
$('rename').onclick=()=>{$('new-name').value=state.self.name;$('rename-dialog').showModal();};
$('rename-dialog').addEventListener('close',async()=>{if($('rename-dialog').returnValue==='save'){try{await api('name',{name:$('new-name').value});await refresh();}catch(e){note(e.message);}}});
for(const [id,kind] of [['screen-permission','screen'],['input-permission','input']])$(id).onclick=()=>api('permissions',{kind}).catch(e=>note(e.message));
$('add-ssh').onclick=()=>$('ssh-dialog').showModal();$('ssh-dialog').addEventListener('close',async()=>{if($('ssh-dialog').returnValue==='save'){try{const value=Object.fromEntries(new FormData($('ssh-dialog').querySelector('form')));await api('ssh-save',value);await refresh();}catch(e){note(e.message);}}});
async function approve(allow,persistent=false){if(!activeApproval)return;const id=activeApproval.id;try{await api('approve',{approval:id,allow,...(persistent?{persistent:true}:{})});$('approval-dialog').close();activeApproval=null;await refresh();}catch(e){note(e.message);}}
$('approve').onclick=()=>approve(true);
$('approve-persistent').onclick=()=>approve(true,true);
$('deny').onclick=()=>approve(false);
function openRemote(peer){
 selectedPeer=peer;remoteFrame=null;remotePending=state.pending[peer.id] || null;
 remotePendingRestored=Boolean(remotePending);
 $('remote-name').textContent=peer.name;$('remote-image').removeAttribute('src');
 $('remote-mode').hidden=!peer.live;
 $('remote-placeholder').classList.remove('hidden');
 $('remote-state').textContent=remotePending?'存在未确认的操作，请查询原结果':'先取得画面，再进行操作';
 $('remote-dialog').showModal();
 if(!remotePending)perform({action:'observe'});
}
function closeRemote(){stopLive().catch(e=>note(e.message));$('remote-dialog').close();selectedPeer=null;remoteFrame=null;remotePending=null;}
$('remote-close').onclick=closeRemote;
function displayReceipt(value, restored=false){
 if(value.state!=='complete'){$('remote-state').textContent='结果尚未确认，只查询原结果，不重复操作';return false;}
 remotePending=null;const receipt=value.result;remoteFrame=null;
 if(!receipt.ok){
  const message={
   SCREEN_CAPTURE_TIMED_OUT:'取屏超时，可以重新取画面',
   STATUS_TIMED_OUT:'设备状态查询超时，可以重新查询',
   EXPIRED_OR_ACCESS_REVOKED_BEFORE_DELIVERY:'请求过期或授权已撤销，操作未交付',
  }[receipt.error];
  $('remote-state').textContent=message || (receipt.input_attempted?'操作已尝试，需重新取屏核对：':'操作未执行：')+receipt.error;
  return true;
 }
 const result=receipt.result || {}, image=result.jpeg_base64 || result.png_base64;
 if(image){$('remote-image').src='data:image/'+(result.jpeg_base64?'jpeg':'png')+';base64,'+image;$('remote-placeholder').classList.add('hidden');}
 if(!image){$('remote-state').textContent='原操作回执已保留，请重新取得画面';return true;}
 if(restored){$('remote-state').textContent='已显示原回执画面，连接可用时请重新取屏';return true;}
 remoteFrame=result;
 $('remote-state').textContent=receipt.input_attempted?'操作回执已收到，请核对画面':'画面已更新 · '+(result.title || '远程电脑');return true;
}
async function recoverPending(){
 if(!remotePending || remoteBusy)return;
 const peer=selectedPeer, id=remotePending;
 remoteBusy=true;
 try{
  const value=await api('recover',{id});
  if(selectedPeer===peer && remotePending===id)displayReceipt(value,remotePendingRestored);
 }catch(e){
  if(selectedPeer===peer && remotePending===id)$('remote-state').textContent=e.message+'；请保留原操作编号继续查询';
 }finally{remoteBusy=false;}
}
async function perform(request){
 if(!selectedPeer || remoteBusy || liveOpening || liveClosing)return;if(remotePending){$('remote-state').textContent='上一操作未确认，请先查询原结果';return;}
 if(request.action!=='observe'){if(!remoteFrame){$('remote-state').textContent='请先取得新的画面';return;}request.frame_id=remoteFrame.frame_id;}
 const peerId=selectedPeer.id;remoteBusy=true;$('remote-state').textContent='正在等待回执…';
 try{const response=await api('remote',{peer:selectedPeer.id,request:{...request,image_format:'compact'}});if(selectedPeer?.id===peerId && !live){remotePending=response.id;remotePendingRestored=false;displayReceipt(response);}}catch(e){$('remote-state').textContent=e.message;await refresh();}finally{remoteBusy=false;}
}
$('remote-refresh').onclick=()=>live?restartLive():perform({action:'observe'});$('remote-recover').onclick=recoverPending;
$('remote-image').onclick=event=>{if(live || !remoteFrame)return;const rect=event.target.getBoundingClientRect();perform({action:'click',x:Math.min(remoteFrame.bounds[2]-1,Math.floor((event.clientX-rect.left)*remoteFrame.bounds[2]/rect.width)),y:Math.min(remoteFrame.bounds[3]-1,Math.floor((event.clientY-rect.top)*remoteFrame.bounds[3]/rect.height)),button:'left'});};
$('send-text').onclick=()=>{const value=$('remote-text').value;if(value){if(live){if(value.length>500){$('remote-state').textContent='连续桌面每次最多发送 500 字，请分段发送';return;}queueLive({type:'text',text:value});}else perform({action:'text',text:value});}};document.querySelectorAll('.key').forEach(button=>button.onclick=()=>{if(live){const code={ENTER:'Enter',TAB:'Tab',ESC:'Escape',BACKSPACE:'Backspace'}[button.dataset.key];queueLive({type:'keydown',code});queueLive({type:'keyup',code});}else perform({action:'key',keys:[button.dataset.key]});});
setInterval(syncInviteActions,1000);
setInterval(refresh,2500);setInterval(()=>{if(remotePending && $('remote-dialog').open)recoverPending();},2000);refresh();

function renderUpdates(){
 const supported=state.supports_updates!==false;
 document.querySelector('.update-card').classList.toggle('hidden',!supported);
 if(!supported){$('update-banner').classList.add('hidden');return;}
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
const liveVideo=$('remote-video');
let liveClosing=null;
const liveFrameLimit=1500;
const liveAvcFirstFrameTimeout=15000;
const liveAvcFrameGapTimeout=4000;
function stopLive(){
 const prior=live;live=null;
 if(prior){
  clearTimeout(prior.timer);
  prior.mediaAbort?.abort();
  closeLiveDecoder(prior);
  liveImage.removeAttribute('src');remoteFrame=null;
  liveVideo.classList.add('hidden');
  liveVideo.width=liveVideo.height=1;
  $('remote-placeholder').classList.remove('hidden');
  liveClosing=api('live-close',{id:prior.id}).finally(()=>{liveClosing=null;});
 }
 $('remote-mode').textContent='连续桌面';$('remote-refresh').textContent='取新画面';$('remote-recover').hidden=false;
 return liveClosing || Promise.resolve();
}
async function startLive(){
 if(!selectedPeer || liveOpening || remoteBusy || liveClosing)return;if(remotePending){$('remote-state').textContent='请先查询上一操作结果';return;}
 liveOpening=true;const peer=selectedPeer;
 const acceptAvc=Boolean(window.webkit?.messageHandlers?.linkoUpdate &&
   window.VideoDecoder && window.EncodedVideoChunk && window.createImageBitmap);
 try{const value=await api('live-open',{peer:peer.id,...(acceptAvc?{viewer_transport:'stream',accept_avc:1}:{})});if(selectedPeer!==peer){await api('live-close',{id:value.id});return;}
  remoteFrame=null;
  const canvas=acceptAvc && value.live_avc===1;
  live={id:value.id,seq:0,ack:0,queue:[],sending:false,frame:null,frameAt:0,halt:false,
   canvas,openedAt:performance.now(),mediaAt:null,pendingFrames:new Map()};
  if(canvas){
   liveImage.removeAttribute('src');
   live.mediaAbort=new AbortController();
   watchLiveMedia(live);
  }else liveImage.src='/live-feed/'+value.id+'?session='+encodeURIComponent(nonce);
  $('remote-mode').textContent='逐步操作';$('remote-refresh').textContent='重新连接';$('remote-recover').hidden=true;$('remote-state').textContent='正在建立连续画面…';pollLive(live);
 }catch(e){$('remote-state').textContent=e.message;}finally{liveOpening=false;}
}
function closeLiveDecoder(session){
 const decoder=session.decoder;
 session.decoder=null;session.pendingFrames.clear();session.avcSequence=null;
 if(decoder && decoder.state!=='closed')decoder.close();
}
function freshLiveInputFrame(session){
 return session.frame && performance.now()-session.frameAt<liveFrameLimit;
}
function presentLiveFrame(session, frame, capturedAt){
 if(!freshLiveInputFrame(session) ||
    session.frame.geometry!==frame.geometry || session.frame.codec!==frame.codec)
  session.queue=[];
 session.frame=frame;
 session.frameAt=capturedAt;
 $('remote-placeholder').classList.add('hidden');
}
function drawLiveMedia(session, pixels, frame, capturedAt, width, height){
 if(live!==session || performance.now()-capturedAt>=liveFrameLimit)return;
 if(liveVideo.width!==width || liveVideo.height!==height){
  liveVideo.width=width;liveVideo.height=height;
 }
 session.canvasContext ||= liveVideo.getContext('2d',{alpha:false});
 session.canvasContext.drawImage(pixels,0,0,width,height);
 liveVideo.classList.remove('hidden');
 session.mediaAt=performance.now();
 presentLiveFrame(session,frame,capturedAt);
}
async function requestLiveJpeg(session){
 if(live!==session || session.jpeg)return;
 session.jpeg=true;
 closeLiveDecoder(session);
 session.frame=null;session.frameAt=0;session.queue=[];
 if(!session.halt)$('remote-state').textContent='正在切换到 JPEG 画面，键鼠已暂停';
 await api('live-jpeg',{id:session.id});
}
function failLiveMedia(session, error){
 if(live!==session)return;
 session.halt=true;session.queue=[];
 $('remote-state').textContent='画面连接中断，已停止键鼠：'+error.message;
 stopLive().catch(showLiveCloseError);
}
function liveBytes(encoded){
 if(Uint8Array.fromBase64)return Uint8Array.fromBase64(encoded);
 const binary=atob(encoded), bytes=new Uint8Array(binary.length);
 for(let i=0;i<bytes.length;i++)bytes[i]=binary.charCodeAt(i);
 return bytes;
}
function avcUnit(frame){
 const bytes=liveBytes(frame.avc_base64);
 const header=new DataView(bytes.buffer);
 if(bytes.length<=48 || bytes.length>48+2*1024*1024 || header.getUint32(0)!==0x4c504131 ||
    header.getUint32(4,true)!==bytes.length-48 || !Number.isSafeInteger(frame.seq) ||
    frame.seq<=0 || header.getBigUint64(8,true)!==BigInt(frame.seq) ||
    typeof frame.idr!=='boolean' || header.getUint32(28,true)!==Number(frame.idr))
  throw new Error('无效的 H.264 帧头');
 const width=header.getUint16(24,true),height=header.getUint16(26,true);
 const dimensions=[header.getInt32(32,true),header.getInt32(36,true),
  header.getUint32(40,true),header.getUint32(44,true)];
 if(width<2 || height<2 || width>1920 || height>1080 || width%2 || height%2 ||
    width>dimensions[2] || height>dimensions[3] || dimensions.join(',')!==frame.geometry)
  throw new Error('无效的 H.264 画面尺寸');
 return {data:bytes.subarray(48),width,height};
}
function avcCodec(data){
 for(let i=2;i+4<data.length;i++){
  if(data[i-2]===0 && data[i-1]===0 && data[i]===1 && (data[i+1]&31)===7)
   return 'avc1.'+Array.from(data.subarray(i+2,i+5),byte=>byte.toString(16).padStart(2,'0')).join('');
 }
 throw new Error('H.264 关键帧缺少 SPS');
}
function decodeLiveAvc(session, frame, capturedAt){
 const {data,width,height}=avcUnit(frame);
 const format=`${frame.geometry}:${width}x${height}`;
 if(session.decoder && (frame.seq!==session.avcSequence+1 || format!==session.avcFormat ||
    session.pendingFrames.size>=8)){
  closeLiveDecoder(session);
  session.frame=null;session.queue=[];
 }
 if(!session.decoder){
  if(!frame.idr)return;
  const decoder=new VideoDecoder({
   output(pixels){
    try{
     if(session.decoder!==decoder)return;
     const record=session.pendingFrames.get(pixels.timestamp);
     session.pendingFrames.delete(pixels.timestamp);
     if(record && pixels.displayWidth===record.width && pixels.displayHeight===record.height)
      drawLiveMedia(session,pixels,record.frame,record.capturedAt,record.width,record.height);
    }catch(e){requestLiveJpeg(session).catch(error=>failLiveMedia(session,error));}
    finally{pixels.close();}
   },
   error(){
    if(session.decoder===decoder)requestLiveJpeg(session).catch(error=>failLiveMedia(session,error));
   },
  });
  session.decoder=decoder;session.avcFormat=format;
  decoder.configure({codec:avcCodec(data),optimizeForLatency:true});
 }
 session.avcSequence=frame.seq;
 session.pendingFrames.set(frame.seq,{frame:{seq:frame.seq,geometry:frame.geometry,codec:'H.264'},
  capturedAt,width,height});
 session.decoder.decode(new EncodedVideoChunk({type:frame.idr?'key':'delta',timestamp:frame.seq,data}));
}
async function receiveLiveMedia(session){
 const started=performance.now();
 const response=await fetch('/live-media/'+session.id,{
  headers:{'X-Linko-Session':nonce},signal:session.mediaAbort.signal,
 });
 if(!response.ok || !response.body)throw new Error('无法读取本地画面');
 const reader=response.body.getReader(), decoder=new TextDecoder();
 let buffered='';
 try{
  while(live===session){
   const {value,done}=await reader.read();
   if(done)break;
   buffered+=decoder.decode(value,{stream:true});
   let end;
   while(live===session && (end=buffered.indexOf('\n'))!==-1){
    if(end>2_800_000)throw new Error('画面消息过大');
    const line=buffered.slice(0,end);
    buffered=buffered.slice(end+1);
    if(!line)continue;
    const {frame,frame_age_ms,sent_ms}=JSON.parse(line);
    const now=performance.now(), capturedAt=now-frame_age_ms-Math.max(0,now-started-sent_ms);
    if(!Number.isFinite(capturedAt) || now-capturedAt>=liveFrameLimit)continue;
    if(frame.avc_base64){
     if(!session.jpeg){
      try{decodeLiveAvc(session,frame,capturedAt);}
      catch(e){await requestLiveJpeg(session);}
     }
    }else{
     session.jpeg=true;
     if(session.decoder){
      closeLiveDecoder(session);
      session.frame=null;session.queue=[];
     }
     const bytes=liveBytes(frame.jpeg_base64);
     const bitmap=await createImageBitmap(new Blob([bytes],{type:'image/jpeg'}));
     try{drawLiveMedia(session,bitmap,{seq:frame.seq,geometry:frame.geometry,codec:'JPEG'},
      capturedAt,bitmap.width,bitmap.height);}
     finally{bitmap.close();}
    }
   }
   if(buffered.length>2_800_000)throw new Error('画面消息过大');
  }
 }finally{await reader.cancel().catch(()=>{});}
 if(live===session)throw new Error('本地画面已结束');
}
async function watchLiveMedia(session){
 try{await receiveLiveMedia(session);}
 catch(e){failLiveMedia(session,e);}
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
  const now=performance.now(), frame=value.frame;
  if(session.canvas){
   // Wait for a frame actually drawn on the canvas before applying the playback gap timeout.
   const waitingForFirstFrame=session.mediaAt===null;
   const elapsed=now-(waitingForFirstFrame?session.openedAt:session.mediaAt);
   const timeout=waitingForFirstFrame?liveAvcFirstFrameTimeout:liveAvcFrameGapTimeout;
   if(!session.jpeg && elapsed>timeout)await requestLiveJpeg(session);
  }else if(frame && (!session.frame || frame.seq!==session.frame.seq)){
   presentLiveFrame(session,frame,now-value.frame_age_ms);
  }
  scheduleLiveInput(session);
  if(!session.halt){
   if(session.frame && now-session.frameAt<liveFrameLimit){
    const codec=session.frame.codec?' · '+session.frame.codec:'';
    $('remote-state').textContent='连续桌面'+codec+' · '+Number(value.fps || 0).toFixed(1)+' 帧/秒 · 点击画面使用键鼠';
   }else if(now-(session.mediaAt ?? session.openedAt)>liveFrameLimit){
    $('remote-state').textContent='正在等待新画面，键鼠已暂停';
   }
  }
 }catch(e){if(live===session){session.halt=true;session.queue=[];$('remote-state').textContent='连接中断，已停止键鼠：'+e.message;stopLive().catch(showLiveCloseError);}}
 if(live===session)session.timer=setTimeout(()=>pollLive(session),80);
}
function queueLive(event){
 const s=live;if(!s || s.halt)return;
 if(!freshLiveInputFrame(s)){s.queue=[];return;}
 if(s.queue.length>=128){s.halt=true;s.queue=[];$('remote-state').textContent='网络拥堵，已停止输入，请重新连接画面';stopLive();return;}
 if(event.type==='move' && s.queue.at(-1)?.type==='move')s.queue[s.queue.length-1]=event;else s.queue.push(event);
 scheduleLiveInput(s);
}
function scheduleLiveInput(session){
 if(live!==session || session.halt || !session.queue.length)return;
 if(!freshLiveInputFrame(session)){session.queue=[];return;}
 if(session.inputScheduled || session.sending || session.seq-session.ack>=6)return;
 // Combine events queued in the same turn without waiting for a background timer.
 session.inputScheduled=true;
 queueMicrotask(()=>{
  session.inputScheduled=false;
  pumpLive(session);
 });
}
async function pumpLive(session){
 if(live!==session || session.halt)return;
 if(!freshLiveInputFrame(session)){session.queue=[];return;}
 if(!session.queue.length || session.seq-session.ack>=6 || session.sending)return;
 session.sending=true;const events=session.queue.splice(0,64),seq=++session.seq;
 try{await api('live-input',{id:session.id,seq,geometry:session.frame.geometry,events});}
 catch(e){if(live===session){session.halt=true;session.queue=[];$('remote-state').textContent='键鼠结果未确认，已停止输入：'+e.message;api('live-close',{id:session.id}).catch(()=>{});}}
 finally{
  session.sending=false;
  scheduleLiveInput(session);
 }
}
function livePoint(e){const r=e.currentTarget.getBoundingClientRect();return {x:Math.max(0,Math.min(65535,Math.round((e.clientX-r.left)/r.width*65535))),y:Math.max(0,Math.min(65535,Math.round((e.clientY-r.top)/r.height*65535)))};}
const liveCodes=new Set(["KeyA", "KeyB", "KeyC", "KeyD", "KeyE", "KeyF", "KeyG", "KeyH", "KeyI", "KeyJ", "KeyK", "KeyL", "KeyM", "KeyN", "KeyO", "KeyP", "KeyQ", "KeyR", "KeyS", "KeyT", "KeyU", "KeyV", "KeyW", "KeyX", "KeyY", "KeyZ", "Digit0", "Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "Digit6", "Digit7", "Digit8", "Digit9", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End", "PageUp", "PageDown", "ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight", "AltLeft", "AltRight", "MetaLeft", "MetaRight", "Minus", "Equal", "BracketLeft", "BracketRight", "Backslash", "Semicolon", "Quote", "Comma", "Period", "Slash", "Backquote"]);
function releaseLiveInput(){const s=live;if(!s)return;if(s.halt)return;if(s.frame && performance.now()-s.frameAt<liveFrameLimit)queueLive({type:'release'});else api('live-close',{id:s.id}).catch(()=>{});}
for(const surface of [liveImage,liveVideo]){
 surface.addEventListener('pointermove',e=>{if(live)queueLive({type:'move',...livePoint(e)});});
 surface.addEventListener('pointerdown',e=>{if(!live)return;e.preventDefault();surface.focus();surface.setPointerCapture(e.pointerId);queueLive({type:'down',button:e.button,...livePoint(e)});});
 surface.addEventListener('pointerup',e=>{if(!live)return;e.preventDefault();queueLive({type:'up',button:e.button,...livePoint(e)});if(surface.hasPointerCapture(e.pointerId))surface.releasePointerCapture(e.pointerId);});
 surface.addEventListener('pointercancel',()=>queueLive({type:'release'}));
 surface.addEventListener('contextmenu',e=>{if(live)e.preventDefault();});
 surface.addEventListener('wheel',e=>{if(live){e.preventDefault();queueLive({type:'wheel',amount:e.deltaY>0?-1:1});}},{passive:false});
 for(const type of ['keydown','keyup'])surface.addEventListener(type,e=>{if(!live || e.isComposing || !liveCodes.has(e.code))return;e.preventDefault();queueLive({type,code:e.code});});
 surface.addEventListener('compositionend',e=>{if(live && e.data)queueLive({type:'text',text:e.data.slice(0,500)});});
 surface.addEventListener('blur',releaseLiveInput);
 surface.addEventListener('paste',e=>{if(live){e.preventDefault();const text=e.clipboardData?.getData('text/plain') || '';if(text.length>500){$('remote-state').textContent='每次最多粘贴 500 字，请分段发送';return;}if(text)queueLive({type:'text',text});}});
}
window.addEventListener('blur',releaseLiveInput);
document.addEventListener('visibilitychange',()=>{if(document.hidden && live)releaseLiveInput();});
$('remote-dialog').addEventListener('cancel',()=>closeRemote());
