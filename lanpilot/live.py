"""Bounded, ephemeral human desktop sessions, separate from guarded AI actions.

JPEG retains the latest frame and last viewer delivery. AVC retains a bounded
reference history. Inputs are
numbered, claimed once, and never retransmitted. A short worker lease releases
held keys on transport loss.
"""
import base64
from collections import deque
import re
import struct
import threading
import time
import uuid

from .live_delta import decode_jpeg

KEYS = {**{f'Key{c}': ord(c) for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'},
        **{f'Digit{n}': ord(n) for n in '0123456789'},
        **{f'F{n}': 111+n for n in range(1,13)},
        'Enter':13,'Tab':9,'Escape':27,'Backspace':8,'Delete':46,'Space':32,
        'ArrowLeft':37,'ArrowUp':38,'ArrowRight':39,'ArrowDown':40,
        'Home':36,'End':35,'PageUp':33,'PageDown':34,
        'ShiftLeft':160,'ShiftRight':161,'ControlLeft':162,'ControlRight':163,
        'AltLeft':164,'AltRight':165,'MetaLeft':91,'MetaRight':92,
        'Minus':189,'Equal':187,'BracketLeft':219,'BracketRight':221,
        'Backslash':220,'Semicolon':186,'Quote':222,'Comma':188,'Period':190,'Slash':191,'Backquote':192}

MEDIA_FAILURES = {
    'first_frame_timeout': '被控端未及时传来首帧，可以重连或使用逐步操作',
    'frame_gap': '连续画面传输超时，已释放键鼠，请重新连接',
}
LIVE_WAIT_MS = 250
LIVE_STREAM_SECONDS = 3
LIVE_STREAM_RECORD_MAX = 1_510_000
LIVE_STREAM_TYPE = 'application/x-ndjson'


AVC_HEADER = struct.Struct('<4sIQQHHIiiII')  # Windows LPA1 header
AVC_MAX_PAYLOAD = 2 * 1024 * 1024
AVC_MAX_ENCODED = (AVC_HEADER.size + AVC_MAX_PAYLOAD + 2) // 3 * 4
LIVE_AVC_STREAM_RECORD_MAX = AVC_MAX_ENCODED + 1024
AVC_MAX_HISTORY_BYTES = 4 * 1024 * 1024  # Includes base64 expansion
AVC_MAX_HISTORY_FRAMES = 64
AVC_MAX_CAPTURE_AGE_MS = 1500
AVC_FRAME_TTL = 2
AVC_START_CODE = re.compile(rb'\x00\x00\x01')


def accepts_avc(body):
    version = body.get('accept_avc')
    return type(version) is int and version == 1


def validate_events(events):
    if not isinstance(events,list) or not 1 <= len(events) <= 64:
        raise ValueError('无效键鼠事件')
    clean=[]
    for e in events:
        if not isinstance(e,dict): raise ValueError('无效键鼠事件')
        kind=e.get('type')
        if kind in ('move','down','up'):
            x,y=e.get('x'),e.get('y')
            if type(x) is not int or type(y) is not int or not 0 <= x <= 65535 or not 0 <= y <= 65535:
                raise ValueError('无效鼠标坐标')
            v={'type':kind,'x':x,'y':y}
            if kind != 'move':
                if e.get('button') not in (0,1,2): raise ValueError('无效鼠标按键')
                v['button']=e['button']
        elif kind in ('keydown','keyup'):
            if e.get('code') not in KEYS: raise ValueError('不支持的按键')
            v={'type':kind,'code':e['code'],'vk':KEYS[e['code']]}
        elif kind == 'wheel':
            if type(e.get('amount')) is not int or not -10 <= e['amount'] <= 10 or not e['amount']:
                raise ValueError('无效滚轮事件')
            v={'type':kind,'amount':e['amount']}
        elif kind == 'text':
            text=e.get('text')
            if not isinstance(text,str) or not 1 <= len(text) <= 500: raise ValueError('文字过长')
            v={'type':kind,'text':text}
        elif kind == 'release': v={'type':kind}
        else: raise ValueError('无效键鼠事件')
        clean.append(v)
    return clean


class LiveSessions:
    def __init__(self, hub):
        self.hub=hub
        self.sessions={}
        self.media_failures={}

    def allowed(self,s,now):
        target=self.hub._one('SELECT incoming FROM devices WHERE id=?',(s['target'],))
        return bool(target and target['incoming'] and self.hub._one(
            'SELECT 1 FROM grants WHERE caller=? AND target=? AND (expires IS NULL OR expires>?)',(s['caller'],s['target'],now)))

    def prune(self,now):
        for k,s in list(self.sessions.items()):
            if now-s['viewed']>12 or not self.allowed(s,now): self.remove(k)
        for k,s in list(self.media_failures.items()):
            if now>=s['expires'] or not self.allowed(s,now): self.media_failures.pop(k)

    def remove(self, identifier):
        session = self.sessions.pop(identifier)
        self.downgrade(session)
        session['changed'].notify_all()

    @staticmethod
    def downgrade(session):
        if session.get('avc') is not None:
            session['avc'].clear()
            session['avc'] = None
            session['changed'].notify_all()

    @staticmethod
    def media_options(session):
        if 'avc' not in session:
            return {}
        avc = session['avc']
        return {'live_avc': int(avc is not None),
                'need_idr': avc is not None and avc.need_idr}

    def wait_for_update(self, op, body, result, wait_ms):
        """Wait with no open SQLite transaction and release the hub lock."""
        identifier = body['id']
        session = self.sessions.get(identifier)
        if session is None:
            return None
        if op == 'live-view':
            if result['frame'] or result['error'] or result['ack'] != body.get('after_ack', 0):
                return None
        elif not result['active'] or result['inputs']:
            return None

        def ready():
            if identifier not in self.sessions or session['error']:
                return True
            if op == 'live-exchange':
                return bool(session['queue'])
            frame = session['frame']
            fresh_frame = frame and frame['seq'] != body.get('after') and self.hub.clock() - session['frame_at'] < 2
            return session['ack'] != result['ack'] or bool(fresh_frame)

        started = time.monotonic()
        session['changed'].wait_for(ready, timeout=wait_ms / 1000)
        return time.monotonic() - started

    def dispatch(self,op,me,body,now):
        self.prune(now)
        if op == 'live-open':
            target=body.get('peer')
            s={'caller':me,'target':target}
            if not self.allowed(s,now): raise PermissionError('尚未取得对方本次授权')
            cap=self.hub._one('SELECT live FROM capabilities WHERE id=?',(target,))
            if not cap or not cap['live']: raise ValueError('请先将对方邻控更新到支持连续桌面的版本')
            if any(x['target']==target for x in self.sessions.values()): raise ValueError('此电脑已有连续桌面会话')
            if self.hub.has_pending_command(target,now):
                raise ValueError('请先确认此电脑上一操作结果')
            if len(self.sessions)>=32: raise ValueError('连接服务繁忙，请稍候')
            identifier=uuid.uuid4().hex
            s.update(id=identifier,viewed=now,frame=None,frame_at=0,next=1,ack=0,queue=[],inflight=[],error='',agent_seen=now)
            s.update(view_sent=None, delta_pending=False)
            s['changed'] = threading.Condition(self.hub.lock)
            if (accepts_avc(body) and self.hub._one(
                    'SELECT 1 FROM live_avc_capabilities WHERE id=?', (target,))):
                s['avc'] = AvcHistory()
            self.sessions[identifier]=s
            return {'id':identifier, **self.media_options(s)}
        if op == 'live-agent-state':
            return {'sessions': [{'id': s['id'], **self.media_options(s)}
                                 for s in self.sessions.values() if s['target'] == me]}
        identifier=body.get('id')
        s=self.sessions.get(identifier)
        if not s and op == 'live-view':
            failure=self.media_failures.get(identifier)
            if failure and me == failure['caller']:
                return {'id':identifier,'frame':None,'ack':failure['ack'],'error':failure['error']}
        if not s or me not in (s['caller'],s['target']): raise PermissionError('连续桌面会话已结束')
        if op == 'live-close':
            reason=body.get('reason')
            if me == s['target'] and isinstance(reason,str) and reason in MEDIA_FAILURES:
                if len(self.media_failures)>=32:
                    self.media_failures.pop(next(iter(self.media_failures)))
                self.media_failures[identifier]={'caller':s['caller'],'target':s['target'],'ack':s['ack'],
                                                 'error':MEDIA_FAILURES[reason],'expires':now+12}
            self.remove(identifier)
            return {'closed':True}
        if op == 'live-view':
            if me != s['caller']: raise PermissionError('无权查看此桌面')
            self.downgrade(s)  # Polling viewers consume JPEG.
            s['viewed']=now
            confirmed = body.get('accept_delta') is not True or self.confirmed_view_base(s, body)
            return self.view(s, now, body.get('after') if confirmed else None, body.get('accept_repeat'))
        if op == 'live-input':
            if me != s['caller']: raise PermissionError('无权操作此桌面')
            if s['error']: raise ValueError(s['error'])
            seq=body.get('seq')
            if type(seq) is not int or seq != s['next']: raise ValueError('键鼠序号不连续，已停止输入')
            f, frame_at = s['frame'], s['frame_at']
            if s.get('avc') is not None:
                history = s['avc'].frames
                f, frame_at = history[-1] if history and now - s['viewed'] < 8 else (None, 0)
            if not f or now-frame_at>1.5 or body.get('geometry')!=f['geometry']:
                raise ValueError('画面已过期，请等待新画面')
            events=validate_events(body.get('events'))
            if len(s['queue'])+len(s['inflight'])>=8: raise ValueError('网络拥堵，请暂停操作')
            s['queue'].append({'seq':seq,'events':events,'geometry':f['geometry'],'expires':now+2})
            s['next']+=1
            s['changed'].notify_all()
            return {'accepted':seq}
        if op == 'live-frame':
            if me != s['target']: raise PermissionError('无权发送此桌面')
            frame = body.get('frame')
            active = not s['error'] and now - s['viewed'] < 8
            if isinstance(frame, dict) and 'avc_base64' in frame:
                if 'avc' not in s:
                    raise ValueError('未协商 AVC 连续画面')
                accepted = self.avc_frame(s, frame, now) if active and s['avc'] is not None else None
            else:
                accepted = (self.frame(s, frame, now, allow_repeat=True)
                            if active or s.get('avc') is None else None)
            return {'active': active, 'frame_seq': accepted, **self.media_options(s)}
        if op == 'live-exchange':
            if me != s['target']: raise PermissionError('无权发送此桌面')
            s['agent_seen']=now
            ack=body.get('ack',0)
            if type(ack) is not int or ack<s['ack'] or ack>=s['next']: raise ValueError('无效键鼠回执')
            if ack>s['ack']:
                claimed={q['seq'] for q in s['inflight']}
                if any(i not in claimed for i in range(s['ack']+1,ack+1)): raise ValueError('回执未对应已交付输入')
                s['ack']=ack;s['inflight']=[q for q in s['inflight'] if q['seq']>ack]
                s['changed'].notify_all()
            if body.get('error'):
                s['error']=str(body['error'])[:180]
                s['changed'].notify_all()
            f=body.get('frame')
            if f is not None and (s.get('avc') is None or (not s['error'] and now - s['viewed'] < 8)):
                self.frame(s, f, now)
            inputs=[]
            if not s['error']:
                for q in s['queue']:
                    if q['expires']<=now: s['error']='键鼠事件传输超时，已停止输入';break
                    item=dict(q);item['claimed']=now
                    s['inflight'].append(item)
                # Re-deliver only the original numbered batch. The agent's
                # durable acceptance marker and native claim prevent replay.
                for q in s['inflight']:
                    if q['expires']<=now: s['error']='键鼠回执未确认，已停止输入';break
                    inputs.append({**{k:v for k,v in q.items() if k not in ('expires','claimed')},
                                   'valid_for_ms':max(0,int((q['expires']-now)*1000))})
            s['queue']=[]
            if s['error']: inputs=[]
            remaining=max(0,int((s['viewed']+8-now)*1000))
            return {'inputs':inputs if remaining else [],'active':not bool(s['error']) and remaining>0,'lease_ms':min(2500,remaining), **self.media_options(s)}
        raise ValueError('无效连续桌面请求')

    def view(self, session, now, after, accept_repeat):
        inflight = session['inflight']
        if not session['error'] and inflight and now - inflight[0]['claimed'] > 3:
            session['error'] = '键鼠回执超时，已暂停输入，请结束连接后重连'
            session['changed'].notify_all()
        age = now - session['frame_at']
        frame = session['frame'] if age < 2 else None
        unchanged = frame is not None and frame['seq'] == after
        if (frame and not unchanged and accept_repeat is True and
                type(after) is int and session['same_since'] <= after < frame['seq']):
            frame = {'seq': frame['seq'], 'geometry': frame['geometry'], 'repeat_after': after}
        return {'id': session['id'], 'frame': None if unchanged else frame, 'unchanged': unchanged,
                'ack': session['ack'], 'error': session['error'], 'frame_age_ms': round(age * 1000)}

    def begin_stream(self, credential, identifier, accept_avc):
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('无效连续桌面请求')
        with self.hub.lock:
            device = self.hub._device(credential)
            caller = device['id']
            now = self.hub.clock()
            self.prune(now)
            session = self.sessions.get(identifier)
            if not session or session['caller'] != caller:
                failure = self.media_failures.get(identifier)
                raise PermissionError(failure['error'] if failure and failure['caller'] == caller
                                      else '连续桌面会话已结束')
            if session.get('streaming'):
                raise PermissionError('此会话已有连续画面连接')
            if now - device['seen'] >= 2:
                self.hub.db.execute('UPDATE devices SET seen=? WHERE id=?', (now, caller))
            # Only an authenticated renewal request touches the existing viewer lease.
            if not accept_avc:
                self.downgrade(session)
            session['streaming'] = True
            session['viewed'] = now
            return session

    def stream_update(self, session, after, ack, wait):
        with self.hub.lock:
            now = self.hub.clock()
            if self.sessions.get(session['id']) is not session or not self.allowed(session, now):
                failure = self.media_failures.get(session['id'])
                error = failure['error'] if failure and self.allowed(session, now) else '连续桌面会话已结束'
                return {'id': session['id'], 'frame': None, 'ack': session['ack'], 'error': error}
            using_avc = session.get('avc') is not None
            seq = after[1] if after is not None and after[0] == using_avc else None
            result = self.view(session, now, seq if not using_avc else None, True)
            if using_avc and not result['error']:
                avc = session['avc']
                frame, captured_at = avc.next_frame(seq, now)
                result.update(frame=frame, frame_age_ms=round((now - captured_at) * 1000) if frame else 0,
                              unchanged=frame is None and bool(avc.frames) and avc.frames[-1][0]['seq'] == seq)
            if result['frame'] or result['ack'] != ack or result['error']:
                return result
            session['changed'].wait(wait)
            return None

    def end_stream(self, session):
        with self.hub.lock:
            session['streaming'] = False

    def confirmed_view_base(self, session, body):
        sent = session['view_sent']
        after = body.get('after')
        return (sent is not None and type(after) is int and after == sent['seq'] and
                body.get('after_geometry') == sent['geometry'])

    def prepare_view(self, body, result):
        """Record a complete/repeated delivery or capture immutable compression inputs."""
        if result['frame'] is None:
            return None
        session = self.sessions[body['id']]
        current = session['frame']
        base = session['view_sent']
        if (body.get('accept_delta') is True and self.confirmed_view_base(session, body) and
                not session['delta_pending'] and 'repeat_after' not in result['frame'] and
                current['seq'] > base['seq'] and current['geometry'] == base['geometry']):
            session['delta_pending'] = True
            return session, base, current, session['frame_at']
        session['view_sent'] = current
        return None

    def finish_view_delta(self, result, work, delta):
        session, base, current, captured_at = work
        now = self.hub.clock()
        if self.sessions.get(session['id']) is not session or not self.allowed(session, now):
            raise PermissionError('连续桌面会话已结束')
        age = now - captured_at
        result['frame_age_ms'] = round(age * 1000)
        if age >= 2:
            result['frame'] = None
            return
        if delta is not None and session['view_sent'] is base:
            result['frame'] = {'seq': current['seq'], 'geometry': current['geometry'],
                               'delta_after': base['seq'], 'delta_base64': delta}
        session['view_sent'] = current

    def avc_frame(self, session, frame, now):
        accepted = session['avc'].add(frame, now)
        session['changed'].notify_all()
        return accepted

    def frame(self,s,f,now,*,allow_repeat=False):
        if not isinstance(f,dict) or not re.fullmatch(r'-?\d+,-?\d+,\d+,\d+',str(f.get('geometry',''))) or type(f.get('seq')) is not int:
            raise ValueError('无效连续画面')
        previous = s['frame']
        seq, geometry = f['seq'], f['geometry']
        if 'repeat_after' in f:
            base = f['repeat_after']
            if not allow_repeat or type(base) is not int or 'jpeg_base64' in f or seq <= base:
                raise ValueError('无效重复画面')
            if not previous or previous['seq'] != base or previous['geometry'] != geometry:
                return None
            encoded = previous['jpeg_base64']
        else:
            encoded = f.get('jpeg_base64', '')
            decode_jpeg(encoded)
            if previous and seq <= previous['seq']:
                # A transport retry can acknowledge a stored frame without
                # giving that capture a new timestamp.
                if seq == previous['seq'] and geometry == previous['geometry'] and encoded == previous['jpeg_base64']:
                    return seq
                return None

        if not previous or previous['geometry'] != geometry or previous['jpeg_base64'] != encoded:
            s['same_since'] = seq
        self.downgrade(s)
        s['frame'] = {'seq':seq, 'geometry':geometry, 'jpeg_base64':encoded}
        s['frame_at'] = now
        s['changed'].notify_all()
        return seq


def decode_avc_record(frame):
    encoded = frame.get('avc_base64')
    age = frame.get('capture_age_ms')
    if (set(frame) != {'avc_base64', 'capture_age_ms'} or
            not isinstance(encoded, str) or len(encoded) > AVC_MAX_ENCODED or
            type(age) is not int or age < 0):
        raise ValueError('无效 AVC 画面')
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) <= AVC_HEADER.size:
        raise ValueError('无效 AVC 画面')
    magic, length, seq, tick, width, height, flags, x, y, source_w, source_h = AVC_HEADER.unpack_from(raw)
    if (magic != b'LPA1' or length != len(raw) - AVC_HEADER.size or length > AVC_MAX_PAYLOAD or
            not seq or flags not in (0, 1) or width % 2 or height % 2 or
            not 2 <= width <= min(1920, source_w) or not 2 <= height <= min(1080, source_h) or
            source_w > 0x7fffffff or source_h > 0x7fffffff):
        raise ValueError('无效 AVC 帧头')
    picture = None
    sps = pps = False
    previous_header = None
    for index, match in enumerate(AVC_START_CODE.finditer(raw, AVC_HEADER.size)):
        header = match.end()
        if (index >= 256 or header >= len(raw) or raw[header] & 0x80 or
                (previous_header is None and any(raw[AVC_HEADER.size:match.start()])) or
                (previous_header is not None and match.start() <= previous_header + 1)):
            raise ValueError('无效 AVC NAL 单元')
        previous_header = header
        kind = raw[header] & 31
        if kind in (1, 5):
            if (picture is not None and picture != kind) or (kind == 5 and not (sps and pps)):
                raise ValueError('无效 AVC 参考帧')
            picture = kind
        elif kind in (7, 8) and picture is None:
            sps |= kind == 7
            pps |= kind == 8
    if previous_header is None or previous_header + 1 >= len(raw) or picture != (5 if flags else 1):
        raise ValueError('无效 AVC 画面格式')
    return {'seq': seq, 'geometry': f'{x},{y},{source_w},{source_h}',
            'avc_base64': encoded, 'idr': bool(flags)}, tick, (width, height), age


class AvcHistory:
    def __init__(self):
        self.frames = deque()
        self.size = 0
        self.last_seq = 0
        self.last_tick = None
        self.last_format = None
        self.need_idr = True

    def discard_first(self):
        frame, _ = self.frames.popleft()
        self.size -= len(frame['avc_base64'])

    def clear(self):
        self.frames.clear()
        self.size = 0
        self.need_idr = True

    def expire(self, now):
        # Capture-age estimates can cross when upload delays vary. Discard
        # every preceding frame as well whenever an interior frame expires.
        expired = max((index for index, (_, captured_at) in enumerate(self.frames)
                       if now - captured_at >= AVC_FRAME_TTL), default=-1)
        for _ in range(expired + 1):
            self.discard_first()

    def add(self, record, now):
        frame, tick, dimensions, age = decode_avc_record(record)
        self.expire(now)
        seq = frame['seq']
        if seq <= self.last_seq:
            if self.frames and seq == self.last_seq and self.frames[-1][0] == frame:
                return seq
            return None

        video_format = (frame['geometry'], dimensions)
        continuous = (self.frames and seq == self.last_seq + 1 and
                      video_format == self.last_format and tick >= self.last_tick)
        self.last_seq = seq
        if age >= AVC_MAX_CAPTURE_AGE_MS or (self.last_tick is not None and tick < self.last_tick):
            self.clear()
            return None
        if not continuous:
            self.clear()
            if not frame['idr']:
                return None
        if frame['idr']:
            self.need_idr = False
        captured_at = now - age / 1000
        self.frames.append((frame, captured_at))
        self.last_tick = tick
        self.last_format = video_format
        self.size += len(frame['avc_base64'])
        while self.size > AVC_MAX_HISTORY_BYTES or len(self.frames) > AVC_MAX_HISTORY_FRAMES:
            self.discard_first()
        return seq

    def next_frame(self, after, now):
        self.expire(now)
        for frame, captured_at in self.frames:
            seq = frame['seq']
            if after is None or seq > after:
                if frame['idr'] or (after is not None and seq == after + 1):
                    return frame, captured_at
        if not self.frames or self.frames[-1][0]['seq'] != after:
            self.need_idr = True
        return None, 0
