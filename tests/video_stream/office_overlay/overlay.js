// Loaded only by the offscreen test harness. Patches never present a live frame.
function makeOfficeOverlay(report, sample) {
  const displayTTL = 2500;
  let current, latest, saved, timer;
  const tiles = new Map();
  const base = document.createElement('canvas');
  const baseContext = base.getContext('2d', {alpha:false, willReadFrequently:true});

  function dropTiles() {
    for (const bitmap of tiles.values()) bitmap.close();
    tiles.clear();
  }

  function restore(reason) {
    clearTimeout(timer);
    const hadTiles = tiles.size > 0;
    dropTiles();
    if (!saved || !hadTiles || !current) return;
    const context = liveVideo.getContext('2d');
    context.drawImage(base, 0, 0);
    saved = false;
    const before = baseContext.getImageData(0, 0, base.width, base.height).data;
    const after = context.getImageData(0, 0, base.width, base.height).data;
    report('overlay_clear', {reason, seq:current.seq,
      identical:before.every((byte, i) => byte === after[i])});
    sample(current.frame);
  }

  function matches(left, right) {
    return left && right && left.session === right.session &&
      left.epoch === right.epoch && left.geometry === right.geometry &&
      left.width === right.width && left.height === right.height;
  }

  function received(session, frame, capturedAt) {
    const patch = frame.officeOverlay;
    if (!patch || !Number.isSafeInteger(patch.epoch) || patch.epoch <= 0 ||
        patch.epoch > frame.seq || !Array.isArray(patch.tiles) || patch.tiles.length > 1) {
      reset('unmarked');
      return;
    }
    const {width, height} = avcUnit(frame);
    const next = {session, seq:frame.seq, epoch:patch.epoch, geometry:frame.geometry,
                  width, height, capturedAt, patches:patch.tiles};
    if (latest?.session === session && next.seq <= latest.seq) return;
    if (!matches(latest, next)) restore('source_change');
    latest = next;
    records.set(frame.seq, next);
    if (records.size > 40) records.delete(records.keys().next().value);
  }

  function paint(context) {
    for (const [position, bitmap] of tiles) {
      const [x, y] = position.split(',').map(Number);
      context.drawImage(bitmap, x, y);
    }
  }

  const records = new Map();
  function baseDraw(session, frame, capturedAt) {
    clearTimeout(timer);
    saved = false;
    const record = frame.codec === 'H.264' ? records.get(frame.seq) : undefined;
    if (!matches(record, current) || !matches(record, latest)) dropTiles();
    current = record && { ...record, frame };
    if (!matches(current, latest)) return;
    if (tiles.size) redrawBase();
    if (current.patches.length) {
      for (const patch of current.patches) receiveTile(current, patch);
    }
    function redrawBase() {
      base.width = liveVideo.width;
      base.height = liveVideo.height;
      baseContext.drawImage(liveVideo, 0, 0);
      saved = true;
      paint(liveVideo.getContext('2d'));
      timer = setTimeout(() => restore('expired'), Math.max(0, capturedAt + displayTTL - performance.now()));
    }
  }

  function rejection(anchor, patch, now) {
    if (live !== anchor.session || anchor.session.jpeg || anchor.session.halt ||
        !matches(anchor, current) || !matches(anchor, latest))
      return 'identity';
    if (patch.geometry !== anchor.geometry || patch.width !== anchor.width || patch.height !== anchor.height)
      return 'geometry';
    if (patch.seq !== anchor.seq || current.seq !== anchor.seq)
      return 'sequence';
    if (now - anchor.capturedAt >= displayTTL) return 'expired';
    if (typeof patch.jpeg !== 'string' || patch.jpeg.length > 96 * 1024) return 'payload';
    if (![patch.x,patch.y,patch.w,patch.h].every(Number.isSafeInteger) ||
        patch.x < 0 || patch.y < 0 || patch.w <= 0 || patch.h <= 0 ||
        patch.x + patch.w > anchor.width || patch.y + patch.h > anchor.height)
      return 'bounds';
    return null;
  }

  function faultChecks(anchor, patch) {
    const now = performance.now();
    const faults = [
      ['geometry', anchor, {...patch, geometry:'1,0,1280,720'}, now],
      ['sequence', anchor, {...patch, seq:patch.seq-1}, now],
      ['expired', anchor, patch, anchor.capturedAt+displayTTL],
      ['identity', {...anchor, epoch:anchor.epoch+1}, patch, now],
      ['bounds', anchor, {...patch, x:anchor.width}, now],
    ];
    for (const [expected, target, data, time] of faults)
      report('overlay_guard', {expected, actual:rejection(target, data, time)});
  }

  let checked = false;
  async function receiveTile(anchor, patch) {
    if (!patch || typeof patch !== 'object') return;
    let bitmap;
    try {
      let reason = rejection(anchor, patch, performance.now());
      if (!reason) {
        bitmap = await createImageBitmap(new Blob([liveBytes(patch.jpeg)], {type:'image/jpeg'}));
        reason = rejection(anchor, patch, performance.now());
        if (!reason && (bitmap.width !== patch.w || bitmap.height !== patch.h)) reason = 'dimensions';
      }
      if (reason) { report('overlay_reject', {seq:anchor.seq, reason}); return; }
      if (!checked) { checked = true; faultChecks(anchor, patch); }
      const frameBefore = live.frame, ageBefore = live.frameAt, leaseBefore = freshLiveInputFrame(live);
      if (!saved) {
        base.width = liveVideo.width;
        base.height = liveVideo.height;
        baseContext.drawImage(liveVideo, 0, 0);
        saved = true;
      }
      const key = [patch.x, patch.y].join(',');
      tiles.get(key)?.close();
      tiles.set(key, bitmap);
      bitmap = null;
      paint(liveVideo.getContext('2d'));
      clearTimeout(timer);
      timer = setTimeout(() => restore('expired'),
                         Math.max(0, current.capturedAt + displayTTL - performance.now()));
      report('overlay_apply', {seq:anchor.seq, epoch:anchor.epoch, rect:[patch.x,patch.y,patch.w,patch.h],
        inputUnchanged:live.frame === frameBefore && live.frameAt === ageBefore,
        inputWasFresh:leaseBefore, tileCount:tiles.size});
      sample(current.frame);
    } catch (error) {
      report('overlay_reject', {seq:anchor.seq, reason:String(error)});
    } finally {
      bitmap?.close();
    }
  }

  function reset(reason) {
    restore(reason);
    current = latest = undefined;
    records.clear();
  }

  return {received, baseDraw, reset, count:() => tiles.size};
}
