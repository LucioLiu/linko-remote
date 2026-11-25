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
 let clock = 0, status = {ack: 0};
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
  setTimeout() {},
  clearTimeout() {},
  async api(operation, body) {
   calls.push({operation, body});
   if (operation === 'live-open') return {id: 'fixture', live_avc: 1};
   if (operation === 'live-view') return status;
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
