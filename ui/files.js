'use strict';

const fileJobs = new Map();
const fileActions = new Set();
const filePhases = {
 offered: '等待接收方确认',
 transferring: '正在传输',
 complete: '传输完成',
 declined: '接收方已拒绝',
 cancelled: '已取消',
 failed: '传输失败',
};
let fileRendering = '';
let filePeersRendering = '';
let fileChoosing = false;

function openFiles(peer) {
 $('files-nav').click();
 if (peer) $('file-peer').value = peer;
 updateFilePicker();
}

function fileSize(bytes) {
 if (bytes < 1024) return bytes + ' B';
 if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KiB';
 return (bytes / (1024 * 1024)).toFixed(1) + ' MiB';
}

function fileStatusText(item, receiving) {
 if (item.phase === 'complete') {
  if (!receiving) return '接收方已校验完整文件';
  return item.downloadable ? '已校验，可保存到本机' : '接收方已确认完成，本机暂无可保存的副本';
 }
 if (item.phase === 'failed') {
  const progress = '已确认 ' + fileSize(item.received) + ' / ' + fileSize(item.size);
  const retry = receiving ? '请让发送方重新发送' : '请重新选择原文件发送';
  return [item.error || filePhases.failed, progress, retry].join(' · ');
 }
 return item.error || (item.phase === 'offered' && receiving ? '等待你确认' : filePhases[item.phase]);
}

function fileFeedback(message, good = false) {
 const status = $('file-feedback');
 status.className = message ? 'notice' + (good ? ' good' : '') : 'hidden';
 status.textContent = message;
}

function unavailableFilePeer(peer) {
 if (!state?.connected) return '连接服务暂不可用';
 if (state.file_version !== 1) return '当前连接服务尚未支持文件传输';
 if (!peer) return '请先配对并选择接收电脑';
 if (!peer.online) return '对方电脑离线';
 if (!peer.file_transfer) return '对方版本暂不支持文件传输';
 return '';
}

function updateFilePicker() {
 const peer = state?.peers.find(device => device.id === $('file-peer').value);
 const reason = unavailableFilePeer(peer);
 $('file-input').disabled = Boolean(reason);
 $('file-send').disabled = Boolean(reason) || fileChoosing || !$('file-input').files.length;
 const max = state?.file_max_bytes;
 $('file-help').textContent = reason || '每份最多 ' + fileSize(max) + '，对方确认后开始传输。';
}

function renderFiles() {
 const files = state.files || [];
 const pending = files.filter(item => item.receiver === state.self.id && item.phase === 'offered').length;
 const badge = $('files-unread');
 badge.textContent = pending ? pending + ' 待接收' : '';
 badge.classList.toggle('hidden', !pending);
 if (pending) $('files-nav').setAttribute('aria-label', '文件传输，' + pending + ' 份待接收');
 else $('files-nav').removeAttribute('aria-label');
 const signature = JSON.stringify([state.connected, state.file_version, state.peers, files, [...fileActions]]);
 if (fileRendering === signature) return;
 fileRendering = signature;

 const peerSignature = JSON.stringify(state.peers.map(({id, name}) => [id, name]));
 if (filePeersRendering !== peerSignature) {
  filePeersRendering = peerSignature;
  const picker = $('file-peer'), previous = picker.value;
  picker.replaceChildren(new Option(state.peers.length ? '选择已配对电脑' : '还没有已配对的电脑', ''));
  for (const peer of state.peers) picker.add(new Option(peer.name + ' · ' + peer.id, peer.id));
  picker.value = state.peers.some(peer => peer.id === previous) ? previous : '';
 }
 updateFilePicker();

 const list = $('file-list');
 list.replaceChildren();
 if (!files.length) {
  list.append(textNode('div', '暂无文件传输', 'empty'));
  return;
 }
 for (const item of files) {
  const receiving = item.receiver === state.self.id;
  const peerId = receiving ? item.sender : item.receiver;
  const peer = state.peers.find(device => device.id === peerId);
  const row = textNode('div', '', 'device-row file-row');
  const meta = textNode('div', '', 'device-meta file-meta');
  const title = textNode('strong', item.name);
  title.title = item.name;
  const summary = (receiving ? '来自 ' : '发往 ') + (peer?.name || peerId) + ' · ' + fileSize(item.size);
  meta.append(title, textNode('small', summary));
  const status = fileStatusText(item, receiving);
  meta.append(textNode('small', status, 'file-status'));
  if (item.phase === 'transferring') {
   const progress = document.createElement('progress');
   progress.max = item.size || 1;
   progress.value = item.received;
   progress.setAttribute('aria-label', item.name + ' 的传输进度');
   meta.append(progress, textNode('small', fileSize(item.received) + ' / ' + fileSize(item.size)));
  }
  const buttons = textNode('div', '', 'row-actions');
  if (item.phase === 'offered' && receiving) {
   buttons.append(action('拒绝', () => changeFile(item, 'file-cancel')),
                  action('接收', () => changeFile(item, 'file-accept'), true));
  } else if (item.phase === 'offered' || item.phase === 'transferring') {
   buttons.append(action('取消', () => changeFile(item, 'file-cancel')));
  } else if (receiving && item.downloadable) {
   buttons.append(action('保存 / 下载', () => changeFile(item, 'file-download'), true));
  }
  for (const button of buttons.children) {
   button.disabled = fileActions.has(item.id) || (!state.connected && !item.downloadable);
  }
  row.append(meta, buttons);
  list.append(row);
 }
}

async function changeFile(item, operation) {
 if (fileActions.has(item.id)) return;
 fileActions.add(item.id);
 renderFiles();
 try {
  if (operation === 'file-cancel' && fileJobs.has(item.id)) fileJobs.get(item.id).cancelled = true;
  const result = await api(operation, {id:item.id});
  if (operation === 'file-download') {
   if (!/^\/file-download\/[A-Za-z0-9_-]{43}$/.test(result.url)) throw new Error('下载链接无效');
   const link = document.createElement('a');
   link.href = result.url;
   link.download = item.name;
   document.body.append(link);
   link.click();
   link.remove();
  }
  if (operation === 'file-accept') fileFeedback('已确认接收，传输完成后可选择保存。', true);
 } catch (error) {
  fileFeedback(error.message);
 } finally {
  fileActions.delete(item.id);
  await refresh();
  renderFiles();
 }
}

const fileWait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function sendFile(item, file, chunkSize) {
 const job = {cancelled: false};
 fileJobs.set(item.id, job);
 try {
  let current = item;
  while (!job.cancelled && current.phase === 'offered') {
   await fileWait(900);
   if (job.cancelled) return;
   current = await api('file-status', {id:item.id});
  }
  if (job.cancelled || current.phase !== 'transferring') return;
  for (let offset = 0; offset < file.size; offset += chunkSize) {
   if (job.cancelled) return;
   const bytes = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
   let raw = '';
   for (let start = 0; start < bytes.length; start += 8192) {
    raw += String.fromCharCode(...bytes.subarray(start, start + 8192));
   }
   const body = {id:item.id, offset, data:btoa(raw)};
   while (!job.cancelled) {
    if ((await api('file-push', body)).accepted) break;
    await fileWait(100);
   }
  }
  if (!job.cancelled) await api('file-seal', {id:item.id});
 } catch (error) {
  if (!job.cancelled) {
   let status;
   try { status = await api('file-status', {id:item.id}); } catch {}
   if (!status || status.phase === 'offered' || status.phase === 'transferring') {
    fileFeedback(item.name + ' · ' + error.message);
    await api('file-cancel', {id:item.id, failed:true}).catch(() => {});
   }
  }
 } finally {
  fileJobs.delete(item.id);
  await refresh();
 }
}

$('file-peer').onchange = updateFilePicker;
$('file-input').onchange = updateFilePicker;
$('file-form').onsubmit = async event => {
 event.preventDefault();
 if (fileChoosing || $('file-send').disabled) return;
 const file = $('file-input').files[0];
 const peer = $('file-peer').value;
 const max = state.file_max_bytes;
 if (file.size > max) {
  fileFeedback('所选文件超过 ' + fileSize(max) + '。');
  return;
 }
 fileChoosing = true;
 updateFilePicker();
 try {
  const item = await api('file-offer', {peer, name:file.name, size:file.size});
  $('file-input').value = '';
  fileFeedback('文件请求已发出，请等待对方在邻控中确认。', true);
  sendFile(item, file, state.file_chunk_bytes);
  await refresh();
 } catch (error) {
  fileFeedback(error.message);
 } finally {
  fileChoosing = false;
  updateFilePicker();
 }
};

window.addEventListener('pagehide', () => {
 for (const id of fileJobs.keys()) {
  fetch('/api/file-cancel', {
   method:'POST', keepalive:true,
   headers:{'Content-Type':'application/json','X-Linko-Session':nonce},
   body:JSON.stringify({id}),
  }).catch(() => {});
 }
});
