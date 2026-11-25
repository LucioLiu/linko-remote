const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function canvas() {
  const canvas = {width:1280, height:720, pixel:0};
  const ctx = {
    drawImage(source) { canvas.pixel = source.pixel; },
    getImageData() { return {data: [canvas.pixel]}; },
  };
  canvas.getContext = () => ctx;
  return canvas;
}

async function main() {
  const events = [];
  const pending = [];
  const timers = new Map();
  const video = canvas();
  const live = {frameAt:0, frame:{}, jpeg:false, halt:false};
  let now = 300;
  let timerID = 0;
  const scope = vm.createContext({
    document:{createElement:canvas},
    performance:{now:()=>now},
    setTimeout:(callback, wait) => {
      timers.set(++timerID, {callback, wait});
      return timerID;
    },
    clearTimeout:(id)=>timers.delete(id),
    liveVideo:video,
    live,
    freshLiveInputFrame:(session)=>now-session.frameAt<1500,
    avcUnit:()=>({width:1280,height:720}),
    liveBytes:()=>new Uint8Array(),
    Blob,
    createImageBitmap:()=>new Promise(resolve=>pending.push(resolve)),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'overlay.js'),'utf8'), scope);
  const overlay = scope.makeOfficeOverlay((kind, report)=>events.push({kind, ...report}), ()=>{});
  const geometry = '0,0,1280,720';

  function receive(seq, epoch, patch) {
    const tile = {seq, geometry, width:1280, height:720, x:0,y:0,w:20,h:20, jpeg:''};
    overlay.received(live, {seq, geometry, officeOverlay:{epoch, tiles:patch?[tile]:[]}}, 0);
  }
  function draw(seq) {
    video.pixel=seq;
    overlay.baseDraw(live, {seq, geometry, codec:'H.264'},0);
  }
  async function finishDecode(pixel) {
    const bitmap = {width:20,height:20,pixel,closed:false,close(){this.closed=true;}};
    pending.shift()(bitmap);
    await new Promise(resolve=>setImmediate(resolve));
    return bitmap;
  }

  receive(1,1,true);
  draw(1);
  receive(2,1,false);
  draw(2);
  const late = await finishDecode(100);
  assert.equal(late.closed,true);
  assert.equal(video.pixel,2);
  assert.equal(events.at(-1).reason,'sequence');

  receive(3,1,true);
  draw(3);
  now=1600;
  const visible = await finishDecode(200);
  assert.equal(video.pixel,200);
  assert.equal(live.frameAt,0);
  assert.equal(events.at(-1).inputUnchanged,true);
  assert.equal(events.at(-1).inputWasFresh,false);
  assert.equal([...timers.values()][0].wait,900);

  receive(4,4,false);
  assert.equal(video.pixel,3);
  assert.equal(visible.closed,true);
  assert.equal(events.at(-1).reason,'source_change');
  assert.equal(events.at(-1).identical,true);
  draw(4);

  receive(5,4,true);
  draw(5);
  const expiring = await finishDecode(300);
  assert.equal(video.pixel,300);
  now=2500;
  [...timers.values()][0].callback();
  assert.equal(video.pixel,5);
  assert.equal(expiring.closed,true);
  assert.equal(events.at(-1).reason,'expired');

  now=700;
  receive(6,4,true);
  draw(6);
  live.jpeg=true;
  overlay.reset('fallback');
  const fallback = await finishDecode(500);
  assert.equal(fallback.closed,true);
  assert.equal(video.pixel,6);
  assert.equal(events.at(-1).reason,'identity');
  const checks = events.filter(e=>e.kind==='overlay_guard');
  assert.equal(checks.length,5);
  assert.ok(checks.every(e=>e.actual===e.expected));
  console.log('Overlay passed late decode, source change, expiry, fallback and input time checks');
}

main().catch(error=>{console.error(error); process.exitCode=1;});
