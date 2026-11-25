// Run with node --test tests/test_live_avc_viewer.js.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8');
const viewer = app.slice(app.indexOf('let live=null'), app.indexOf('function livePoint('));

function fixture() {
 const elements = new Map(), calls = [];
 let clock = 0, status = {ack: 0}, sendInput = async () => {};
 const context = vm.createContext({
  $(id) {
   if (!elements.has(id)) {
    elements.set(id, {
     textContent: '',
     classList: {add() {}, remove() {}},
     removeAttribute() {},
     getContext() { return {drawImage() {}}; },
    });
   }
   return elements.get(id);
  },
  performance: {now: () => clock},
  AbortController,
  window: {webkit: {messageHandlers: {linkoUpdate: {}}}, VideoDecoder: {}, EncodedVideoChunk: {}, createImageBitmap: {}},
  selectedPeer: {id: 'peer'},
  remoteBusy: false,
  remotePending: null,
  remoteFrame: null,
  fetch: () => new Promise(() => {}),
  nonce: 'fixture',
  queueMicrotask,
  setTimeout() {},
  clearTimeout() {},
  async api(operation, body) {
   calls.push({operation, body});
   if (operation === 'live-open') return {id: 'fixture', live_avc: 1};
   if (operation === 'live-view') return status;
   if (operation === 'live-input') return sendInput(body);
   return {};
  },
 });
 vm.runInContext(viewer, context);
 const run = (code) => vm.runInContext(code, context);
 return {
  calls, run, elements,
  async start() {
   await run('startLive()');
   await new Promise(setImmediate); // Let the initial status poll finish before advancing the clock.
  },
  at: (time) => { clock = time; },
  poll: () => run('pollLive(live)'),
  draw: (captureAge) => run(
   `drawLiveMedia(live, {}, {seq:1,geometry:'0,0,1280,720',codec:'H.264'}, performance.now()-${captureAge}, 1280, 720)`),
  queue: () => run("queueLive({type:'move',x:1,y:1})"),
  fallback: () => calls.filter(({operation}) => operation === 'live-jpeg').length,
  inputs: () => calls.filter(({operation}) => operation === 'live-input').map(({body}) => body),
  onInput: (handler) => { sendInput = handler; },
  acknowledge: (ack) => { status = {ack}; },
  revoke: () => { status = {ack: 0, error: '授权已撤销'}; },
 };
}

test('a fresh first AVC draw after four seconds starts a separate playback gap timer', async () => {
 const viewer = fixture();
 await viewer.start();
 viewer.at(5200);
 await viewer.poll();
 viewer.queue();
 assert.equal(viewer.run('live.queue.length'), 0);
 assert.equal(viewer.fallback(), 0);

 viewer.draw(100);
 viewer.queue();
 assert.equal(viewer.run('live.queue.length'), 1);
 viewer.at(6600); // Frame age is exactly 1500 ms.
 viewer.queue();
 await viewer.run('pumpLive(live)');
 assert.equal(viewer.calls.some(({operation}) => operation === 'live-input'), false);
 assert.equal(viewer.run('live.queue.length'), 0);
 viewer.at(9200);
 await viewer.poll();
 assert.equal(viewer.fallback(), 0);
 viewer.at(9201);
 await viewer.poll();
 assert.equal(viewer.fallback(), 1);
 assert.equal(viewer.run('live.frame'), null);
});

test('a stale decode does not count as the first draw and initial waiting is bounded', async () => {
 const viewer = fixture();
 await viewer.start();
 viewer.at(6000);
 viewer.draw(1500);
 assert.equal(viewer.run('live.frame'), null);
 viewer.at(15000);
 await viewer.poll();
 assert.equal(viewer.fallback(), 0);
 viewer.at(15001);
 await viewer.poll();
 await viewer.poll();
 assert.equal(viewer.fallback(), 1);
});

test('authorization loss closes even after the startup deadline', async () => {
 const viewer = fixture();
 await viewer.start();
 viewer.at(15001);
 viewer.revoke();
 await viewer.poll();
 assert.equal(viewer.fallback(), 0);
 assert.equal(viewer.run('live'), null);
 assert.equal(viewer.calls.at(-1).operation, 'live-close');
 assert.match(viewer.elements.get('remote-state').textContent, /授权已撤销/);
});

test('input submits without timers, coalesces moves, and drains in order after acceptance', async () => {
 const viewer = fixture();
 let accept;
 viewer.onInput(({seq}) => seq === 1 ? new Promise((resolve) => { accept = resolve; }) : Promise.resolve());
 await viewer.start();
 viewer.draw(0);
 viewer.run(`
  queueLive({type:'move',x:1,y:1});
  queueLive({type:'move',x:2,y:2});
  queueLive({type:'down',x:2,y:2,button:0});
 `);
 await new Promise(setImmediate);
 viewer.run(`
  queueLive({type:'move',x:3,y:3});
  queueLive({type:'move',x:4,y:4});
  queueLive({type:'up',x:4,y:4,button:0});
  queueLive({type:'keydown',code:'KeyA'});
  queueLive({type:'keyup',code:'KeyA'});
 `);
 await new Promise(setImmediate);
 assert.equal(viewer.inputs().length, 1);
 accept();
 await new Promise(setImmediate);
 assert.deepEqual(JSON.parse(JSON.stringify(viewer.inputs().map(({seq, events}) => [seq, events]))), [
  [1, [{type:'move',x:2,y:2}, {type:'down',x:2,y:2,button:0}]],
  [2, [{type:'move',x:4,y:4}, {type:'up',x:4,y:4,button:0},
       {type:'keydown',code:'KeyA'}, {type:'keyup',code:'KeyA'}]],
 ]);
});

test('an acknowledgement reopens the send window without reviving input queued on a stale frame', async () => {
 const viewer = fixture();
 await viewer.start();
 viewer.draw(0);
 for (let i = 0; i < 6; i++) {
  viewer.queue();
  await new Promise(setImmediate);
 }
 viewer.queue();
 await new Promise(setImmediate);
 assert.equal(viewer.inputs().length, 6);
 viewer.acknowledge(1);
 await viewer.poll();
 assert.equal(viewer.inputs().at(-1).seq, 7);

 viewer.queue();
 viewer.at(1500);
 viewer.draw(0);
 viewer.acknowledge(2);
 await viewer.poll();
 assert.equal(viewer.inputs().length, 7);
 viewer.queue();
 await new Promise(setImmediate);
 assert.equal(viewer.inputs().at(-1).seq, 8);
});

test('uncertain submission halts other queued input and never retries', async () => {
 const viewer = fixture();
 let rejectInput;
 viewer.onInput(() => new Promise((_, reject) => { rejectInput = reject; }));
 await viewer.start();
 viewer.draw(0);
 viewer.queue();
 await new Promise(setImmediate);
 viewer.queue();
 rejectInput(new Error('connection lost'));
 await new Promise(setImmediate);
 viewer.acknowledge(1);
 await viewer.poll();
 viewer.queue();
 assert.equal(viewer.inputs().length, 1);
 assert.equal(viewer.run('live.queue.length'), 0);
 assert.equal(viewer.run('live.halt'), true);
 assert.equal(viewer.calls.filter(({operation}) => operation === 'live-close').length, 1);
});
