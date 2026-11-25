"""Latest-frame cache independent of browser background timer throttling."""
import threading
import time

from .live import LIVE_WAIT_MS
from .live_delta import DeltaError, decode_delta


class LiveView:
    def __init__(self, hub, identifier, stop_event, *, stream_version=None):
        self.hub=hub;self.id=identifier;self.client_stop=stop_event
        self.stop=threading.Event();self.changed=threading.Condition()
        self.value={'id':identifier,'frame':None,'ack':0,'error':''}
        self.arrivals=[];self.updated=0;self.ui_seen=time.monotonic()
        self.thread=None
        self.stream_version = stream_version
        self.stream = None

    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def publish(self, value, roundtrip=0):
        now=time.monotonic()
        with self.changed:
            previous=self.value.get('frame');frame=value.get('frame')
            if frame and ('delta_after' in frame or 'delta_base64' in frame):
                if (not previous or type(frame.get('delta_after')) is not int or
                        previous['seq'] != frame['delta_after'] or
                        type(frame.get('seq')) is not int or frame['seq'] <= previous['seq'] or
                        frame.get('geometry') != previous['geometry'] or
                        'repeat_after' in frame or 'jpeg_base64' in frame or value.get('unchanged')):
                    raise DeltaError('画面差分缺少对应的上一帧')
                jpeg = decode_delta(previous['jpeg_base64'], frame.get('delta_base64'))
                frame = {'seq': frame['seq'], 'geometry': frame['geometry'], 'jpeg_base64': jpeg}
                value = dict(value, frame=frame)
            if frame and 'repeat_after' in frame:
                if (not previous or previous['seq'] != frame['repeat_after'] or
                        previous['geometry'] != frame['geometry']):
                    raise ValueError('重复画面缺少对应的上一帧')
                frame = dict(previous, seq=frame['seq'])
                value = dict(value, frame=frame)
            if value.get('unchanged'):
                value=dict(value,frame=previous)
            if frame and (not previous or previous['seq']!=frame['seq']):
                self.arrivals=[t for t in self.arrivals if now-t<2];self.arrivals.append(now)
                self.updated=now-value.get('frame_age_ms',0)/1000-roundtrip
            self.value=value;self.changed.notify_all()

    def snapshot(self, touch=True):
        if touch:self.ui_seen=time.monotonic()
        with self.changed:
            result=dict(self.value);frame=result.get('frame')
            if frame: result['frame']={k:v for k,v in frame.items() if k!='jpeg_base64'}
            result['fps']=round(len([t for t in self.arrivals if time.monotonic()-t<2])/2,1)
            result['frame_age_ms']=round((time.monotonic()-self.updated)*1000) if self.updated else 999999
            if result['frame_age_ms']>1500:result['frame']=None
            return result

    def active(self):
        return not self.stop.is_set() and not self.client_stop.is_set() and time.monotonic() - self.ui_seen < 12

    def run_stream(self):
        factory = getattr(self.hub, 'live_stream', None)
        if self.stream_version != 1 or factory is None:
            return False
        with self.changed:
            if not self.active():
                return True
            stream = self.stream = factory(self.id)
        try:
            return stream.run(self.active, self.publish)
        finally:
            with self.changed:
                self.stream = None

    def run(self):
        resync = False
        try:
            if self.run_stream():
                return
            while self.active():
                frame = None if resync else self.value.get('frame')
                started=time.monotonic()
                value=self.hub.call('live-view',{'id':self.id,'after':frame['seq'] if frame else None,
                                                 'after_geometry':frame['geometry'] if frame else None,
                                                 'after_ack':self.value['ack'],'wait_ms':LIVE_WAIT_MS,
                                                 'accept_repeat':True,'accept_delta':True})
                # The relay reports age at send time, after its frame wait.
                transit = max(0, time.monotonic()-started-value.get('waited_ms', 0)/1000)
                try:
                    self.publish(value,transit)
                    if value.get('frame'):
                        resync = False
                except DeltaError:
                    if resync:
                        raise
                    resync = True
                if 'waited_ms' not in value:
                    self.stop.wait(.06)
        except Exception as error:
            self.publish(dict(self.value,error=str(error)[:180]))
        finally:
            if not self.stop.is_set() and not self.client_stop.is_set() and not self.value['error']:
                self.publish(dict(self.value,error='连续画面已暂停，请重新连接'))
            self.stop.set()
            with self.changed:self.changed.notify_all()

    def close(self):
        self.stop.set()
        with self.changed:
            if self.stream is not None:
                self.stream.abort()
            self.changed.notify_all()
