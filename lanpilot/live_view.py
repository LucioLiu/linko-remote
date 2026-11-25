"""Latest-frame cache independent of browser background timer throttling."""
import threading
import time

from .live import LIVE_WAIT_MS


class LiveView:
    def __init__(self, hub, identifier, stop_event):
        self.hub=hub;self.id=identifier;self.client_stop=stop_event
        self.stop=threading.Event();self.changed=threading.Condition()
        self.value={'id':identifier,'frame':None,'ack':0,'error':''}
        self.arrivals=[];self.updated=0;self.ui_seen=time.monotonic()
        self.thread=None

    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def publish(self, value, roundtrip=0):
        now=time.monotonic()
        with self.changed:
            previous=self.value.get('frame');frame=value.get('frame')
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

    def run(self):
        try:
            while not self.stop.is_set() and not self.client_stop.is_set() and time.monotonic()-self.ui_seen<12:
                frame=self.value.get('frame')
                started=time.monotonic()
                value=self.hub.call('live-view',{'id':self.id,'after':frame['seq'] if frame else None,
                                                 'after_ack':self.value['ack'],'wait_ms':LIVE_WAIT_MS})
                # The relay reports age at send time, after its frame wait.
                transit = max(0, time.monotonic()-started-value.get('waited_ms', 0)/1000)
                self.publish(value,transit)
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
        with self.changed:self.changed.notify_all()
