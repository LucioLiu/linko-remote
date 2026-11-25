"""Continuous desktop pump. A stalled network expires the native input lease."""
import json
import os
import http.client
import sys
import threading
import time

from .live import AVC_BATCH_BYTES, AVC_BATCH_FRAMES, AVC_MAX_ENCODED, LIVE_WAIT_MS

FIRST_FRAME_TIMEOUT = 15
FRAME_GAP_TIMEOUT = 8
AVC_FALLBACK_TIMEOUT = 4


def windows_tick_ms():
    import ctypes
    tick = ctypes.windll.kernel32.GetTickCount64
    tick.restype = ctypes.c_uint64
    return tick()


class LiveAgent:
    def __init__(self, client, factory, write):
        self.client=client;self.write=write
        self.hub=factory(client.config['endpoint'],client.config['credential'])
        self.frame_hub=factory(client.config['endpoint'],client.config['credential'])
        self.root=client.agent.workspace
        self.current=None;self.known=set();self.last_frame=0
        self.upload_base = None
        self.repeat_uploads = sys.platform == 'win32'
        self.avc_enabled = False
        self.reset_avc()
        self.error=''
        self.media_at=0
        self.lock=threading.RLock()

    def reset_avc(self):
        self.avc_active = False
        self.avc_batch = False
        self.avc_mailbox = {}
        self.avc_seen = 0
        self.avc_seq = 0
        self.avc_key = 0
        self.avc_waiting_key = False
        self.avc_lease = 0

    def request_avc_key(self):
        if not self.avc_waiting_key:
            self.avc_waiting_key = True
            self.avc_key += 1

    def expire(self):
        with self.lock:
            if self.current:
                self.write(self.root/'live-control.json',{'id':self.current,'expires':0})
            self.current=None;self.known.clear();self.last_frame=0
            self.upload_base = None
            self.reset_avc()

    def read(self,name):
        try: return json.loads((self.root/name).read_text(encoding='utf-8-sig'))
        except (OSError,ValueError): return {}

    def start(self):
        threading.Thread(target=self.run,daemon=True).start()
        threading.Thread(target=self.frames,daemon=True).start()

    def frames(self):
        # The input lease and receipts run on their own connection. The
        # Windows mailbox collects new captures while this thread sends media.
        while not self.client.stop_event.is_set():
            ident = self.current
            delay = .06
            started = time.monotonic()
            try:
                if ident:
                    if self.avc_batch:
                        delay = .01
                        self.upload_avc(ident, self.read_avc_mailbox(ident))
                    frame = self.read('live-frame.json')
                    if frame.get('id') == ident:
                        if 'avc_base64' in frame and not self.avc_batch:
                            delay = .02
                            self.upload_avc(ident, [frame])
                        elif ('jpeg_base64' in frame and frame['seq'] > self.last_frame and
                              time.time() - frame.get('created', 0) < 1):
                            self.upload_frame(ident, frame)
            except PermissionError as error:
                self.end(ident, error)
            except (OSError, http.client.HTTPException):
                pass
            except Exception as error:
                self.end(ident, error)
            self.client.stop_event.wait(max(.005, delay - (time.monotonic() - started)))

    def read_avc_mailbox(self, ident):
        slots = self.avc_mailbox
        available = []
        for slot in range(AVC_BATCH_FRAMES):
            name = f'live-avc-{slot}.json'
            try:
                stat = (self.root / name).stat()
            except OSError:
                continue
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if slot not in slots or slots[slot][0] != stamp:
                if stat.st_size > AVC_MAX_ENCODED + 512:
                    continue
                frame = self.read(name)
                if not frame:
                    continue  # Retry a transient mailbox sharing failure.
                slots[slot] = stamp, frame
            frame = slots[slot][1]
            if frame.get('id') == ident and frame['seq'] > self.avc_seen:
                available.append(frame)
        return sorted(available, key=lambda frame: frame['seq'])

    def upload_avc(self, ident, frames):
        if not frames:
            return
        with self.lock:
            if (self.current != ident or not self.avc_active or not self.client.incoming or
                    self.client.stop_event.is_set() or time.monotonic() >= self.avc_lease):
                return
            batch = self.avc_batch
            payloads = []
            last_seq = self.avc_seq
            size = 0
            now_tick = windows_tick_ms()
            for frame in frames:
                seq = frame['seq']
                if seq <= self.avc_seen:
                    continue
                encoded = frame['avc_base64']
                if len(payloads) == AVC_BATCH_FRAMES or size + len(encoded) > AVC_BATCH_BYTES:
                    break
                self.avc_seen = seq
                age = now_tick - frame['capture_tick']
                if not 0 <= age < 1000:
                    self.request_avc_key()
                    continue
                if frame['idr']:
                    self.avc_waiting_key = False
                elif self.avc_waiting_key or not last_seq or seq != last_seq + 1:
                    self.request_avc_key()
                    continue
                payloads.append({'avc_base64': encoded, 'capture_age_ms': age})
                size += len(encoded)
                last_seq = seq
                if not batch:
                    break
            if not payloads:
                return

        operation = 'live-avc-batch' if batch else 'live-frame'
        body = {'frames': payloads} if batch else {'frame': payloads[0]}
        try:
            value = self.frame_hub.call(operation, dict(body, id=ident))
        except PermissionError as error:
            if getattr(error, 'status', None) != 400:
                raise
            with self.lock:
                if self.current == ident:
                    self.avc_active = self.avc_batch = False
            return
        except (OSError, http.client.HTTPException):
            with self.lock:
                if self.current == ident and self.avc_active:
                    self.request_avc_key()
            return

        with self.lock:
            if self.current != ident:
                return
            if not value['active']:
                raise RuntimeError('连续画面已停止')
            if not self.avc_active:
                return
            if value.get('frame_seq') == last_seq:
                self.avc_seq = last_seq
                self.media_at = time.monotonic()
            if value.get('live_avc', 1) != 1:
                self.avc_active = self.avc_batch = False
            elif value.get('need_idr') or value.get('frame_seq') != last_seq:
                self.request_avc_key()

    def upload_frame(self, ident, frame):
        with self.lock:
            if self.current != ident:
                return
            # The worker has switched codecs for the rest of this session.
            self.avc_active = self.avc_batch = False
            base = self.upload_base
            # A lost response leaves the relay's current base uncertain.
            self.upload_base = None

        payload = {k: frame[k] for k in ('seq', 'geometry', 'jpeg_base64')}
        if (base and base['geometry'] == payload['geometry'] and
                base['jpeg_base64'] == payload['jpeg_base64']):
            del payload['jpeg_base64']
            payload['repeat_after'] = base['seq']

        value = self.frame_hub.call('live-frame', {'id': ident, 'frame': payload})
        with self.lock:
            if self.current != ident:
                return
            if not value['active']:
                raise RuntimeError('连续画面已停止')
            # Older relays omit frame_seq and always receive complete JPEGs.
            if 'frame_seq' in value:
                if value['frame_seq'] != frame['seq']:
                    return
                if self.repeat_uploads:
                    self.upload_base = frame
            elif 'repeat_after' in payload:
                return
            self.last_frame = frame['seq']
            self.media_at = time.monotonic()

    def end(self,ident,error,reason=None):
        with self.lock:
            if self.current!=ident:return
            self.error=str(error)[:180]
            self.expire()
        if ident:
            try:self.hub.call('live-close',{'id':ident,'reason':reason} if reason else {'id':ident})
            except Exception:pass

    def accept(self, ident, batch, roundtrip):
        seq=batch['seq']
        if seq in self.known: return False
        self.known.add(seq)
        marker=self.root/f'{ident}-{seq:010d}.live-accepted'
        try:
            fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        except FileExistsError: return False
        batch=dict(batch)
        remaining=batch.pop('valid_for_ms')/1000-roundtrip
        self.write(self.root/(f'{ident}-{seq:010d}.live-input.json'),dict(batch,id=ident,expires=time.time()+min(1.5,remaining)))
        return True

    def run(self):
        while not self.client.stop_event.is_set():
            try:
                if not self.client.incoming:
                    self.expire();self.client.stop_event.wait(.3);continue
                if self.current:
                    sessions = [{'id': self.current}]
                else:
                    options = {'accept_avc_batch': 1} if self.avc_enabled else {}
                    sessions = self.hub.call('live-agent-state', options)['sessions']
                if not sessions:
                    self.client.stop_event.wait(.3);continue
                ident=sessions[0]['id']
                with self.lock:
                    if not self.client.incoming or self.client.stop_event.is_set():continue
                    starting = self.current!=ident
                    if starting:
                        self.expire();self.media_at=time.monotonic();self.current=ident
                    if self.avc_active and time.monotonic() - self.media_at > AVC_FALLBACK_TIMEOUT:
                        self.avc_active = self.avc_batch = False
                    has_frame = self.last_frame or self.avc_seq
                    frame_timeout = FRAME_GAP_TIMEOUT if has_frame else FIRST_FRAME_TIMEOUT
                    timed_out = time.monotonic() - self.media_at > frame_timeout
                    reason = 'frame_gap' if has_frame else 'first_frame_timeout'
                if timed_out:
                    self.end(ident,'画面传输超时，已释放键鼠，请重新连接',reason)
                    continue
                ended=self.read('live-ended.json')
                if ended.get('id')==ident: raise RuntimeError(ended.get('error','连续桌面已停止'))
                receipt=self.read('live-receipt.json')
                body={'id':ident,'ack':receipt.get('seq',0) if receipt.get('id')==ident else 0}
                if (starting and sessions[0].get('avc_batch') == 1) or self.avc_batch:
                    body['accept_avc_batch'] = 1
                # The native worker needs its first lease before it can capture.
                if not starting: body['wait_ms']=LIVE_WAIT_MS
                if receipt.get('id')==ident and receipt.get('error'): body['error']=receipt['error']
                # No input is ever submitted again if this exchange loses its response.
                started=time.monotonic()
                value=self.hub.call('live-exchange',body)
                relay_wait = value.get('waited_ms', 0) / 1000
                with self.lock:
                    if self.current!=ident or not self.client.incoming or self.client.stop_event.is_set():continue
                    if not value['active']: raise RuntimeError('远程输入已停止')
                    lease = min(2.5, value['lease_ms'] / 1000)
                    if starting:
                        self.avc_active = self.avc_enabled and sessions[0].get('live_avc') == 1
                        self.avc_batch = sessions[0].get('avc_batch') == 1
                    self.avc_active = self.avc_active and value.get('live_avc') == 1
                    self.avc_batch = self.avc_active and self.avc_batch and value.get('avc_batch') == 1
                    control = {'id': ident, 'expires': time.time() + lease}
                    if self.avc_active:
                        self.avc_lease = time.monotonic() + lease
                        control.update(live_avc=1, avc_key=self.avc_key)
                        if self.avc_batch:
                            control['avc_batch'] = 1
                    self.write(self.root/'live-control.json', control)
                    for batch in value['inputs']:
                        transit = max(0, time.monotonic() - started - relay_wait)
                        self.accept(ident, batch, transit)
                    self.error=''
                if value['inputs'] or 'waited_ms' not in value:
                    self.client.stop_event.wait(.06)
            except Exception as error:
                self.end(self.current,error)
                self.client.stop_event.wait(.5)
        self.expire()
