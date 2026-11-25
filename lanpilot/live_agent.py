"""Continuous desktop pump. A stalled network expires the native input lease."""
import json
import os
import http.client
import sys
import threading
import time

from .live import LIVE_WAIT_MS

FIRST_FRAME_TIMEOUT = 15
FRAME_GAP_TIMEOUT = 8


class LiveAgent:
    def __init__(self, client, factory, write):
        self.client=client;self.write=write
        self.hub=factory(client.config['endpoint'],client.config['credential'])
        self.frame_hub=factory(client.config['endpoint'],client.config['credential'])
        self.root=client.agent.workspace
        self.current=None;self.known=set();self.last_frame=0
        self.upload_base = None
        self.repeat_uploads = sys.platform == 'win32'
        self.error=''
        self.media_at=0
        self.lock=threading.RLock()

    def expire(self):
        with self.lock:
            if self.current:
                self.write(self.root/'live-control.json',{'id':self.current,'expires':0})
            self.current=None;self.known.clear();self.last_frame=0
            self.upload_base = None

    def read(self,name):
        try: return json.loads((self.root/name).read_text(encoding='utf-8-sig'))
        except (OSError,ValueError): return {}

    def start(self):
        threading.Thread(target=self.run,daemon=True).start()
        threading.Thread(target=self.frames,daemon=True).start()

    def frames(self):
        # A slow image upload must never hold the native keyboard lease or
        # delay key-up delivery. Only the newest captured frame is uploaded.
        while not self.client.stop_event.is_set():
            ident=self.current
            delay = .06
            try:
                if ident:
                    frame=self.read('live-frame.json')
                    if frame.get('id')==ident and frame.get('seq',0)>self.last_frame and time.time()-frame.get('created',0)<1:
                        started = time.monotonic()
                        self.upload_frame(ident, frame)
                        # Include upload time in the send interval. Slow links
                        # still send only the newest frame, with one in flight.
                        delay = max(.01, delay - (time.monotonic()-started))
            except PermissionError as error:
                self.end(ident,error)
            except (OSError, http.client.HTTPException):
                # The next iteration sends only the latest available image.
                # The input loop and its short native lease stay independent.
                pass
            except Exception as error:
                self.end(ident,error)
            self.client.stop_event.wait(delay)

    def upload_frame(self, ident, frame):
        with self.lock:
            if self.current != ident:
                return
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
                sessions=self.hub.call('live-agent-state')['sessions'] if not self.current else [{'id':self.current}]
                if not sessions:
                    self.client.stop_event.wait(.3);continue
                ident=sessions[0]['id']
                with self.lock:
                    if not self.client.incoming or self.client.stop_event.is_set():continue
                    starting = self.current!=ident
                    if starting:
                        self.expire();self.media_at=time.monotonic();self.current=ident
                    frame_timeout = FRAME_GAP_TIMEOUT if self.last_frame else FIRST_FRAME_TIMEOUT
                    timed_out = time.monotonic()-self.media_at>frame_timeout
                    reason = 'frame_gap' if self.last_frame else 'first_frame_timeout'
                if timed_out:
                    self.end(ident,'画面传输超时，已释放键鼠，请重新连接',reason)
                    continue
                ended=self.read('live-ended.json')
                if ended.get('id')==ident: raise RuntimeError(ended.get('error','连续桌面已停止'))
                receipt=self.read('live-receipt.json')
                body={'id':ident,'ack':receipt.get('seq',0) if receipt.get('id')==ident else 0}
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
                    self.write(self.root/'live-control.json',{'id':ident,'expires':time.time()+min(2.5,value['lease_ms']/1000)})
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
