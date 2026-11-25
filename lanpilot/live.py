"""Bounded, ephemeral human desktop sessions, separate from guarded AI actions.

Only the most recent JPEG is retained. Inputs are numbered, claimed once, and
never retransmitted. A short worker lease releases held keys on transport loss.
"""
import base64
import re
import threading
import time
import uuid

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
        self.sessions.pop(identifier)['changed'].notify_all()

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
            s['changed'] = threading.Condition(self.hub.lock)
            self.sessions[identifier]=s
            return {'id':identifier}
        if op == 'live-agent-state':
            return {'sessions':[{'id':s['id']} for s in self.sessions.values() if s['target']==me]}
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
            s['viewed']=now
            if not s['error'] and s['inflight'] and now-s['inflight'][0]['claimed']>3:
                s['error']='键鼠回执超时，已暂停输入，请结束连接后重连'
                s['changed'].notify_all()
            age=now-s['frame_at']
            frame=s['frame'] if age<2 else None
            unchanged=frame is not None and frame['seq']==body.get('after')
            after = body.get('after')
            if (frame and not unchanged and body.get('accept_repeat') is True and
                    type(after) is int and s['same_since'] <= after < frame['seq']):
                frame = {'seq': frame['seq'], 'geometry': frame['geometry'], 'repeat_after': after}
            return {'id':s['id'],'frame':None if unchanged else frame,'unchanged':unchanged,
                    'ack':s['ack'],'error':s['error'],'frame_age_ms':round(age*1000)}
        if op == 'live-input':
            if me != s['caller']: raise PermissionError('无权操作此桌面')
            if s['error']: raise ValueError(s['error'])
            seq=body.get('seq');f=s['frame']
            if type(seq) is not int or seq != s['next']: raise ValueError('键鼠序号不连续，已停止输入')
            if not f or now-s['frame_at']>1.5 or body.get('geometry')!=f['geometry']:
                raise ValueError('画面已过期，请等待新画面')
            events=validate_events(body.get('events'))
            if len(s['queue'])+len(s['inflight'])>=8: raise ValueError('网络拥堵，请暂停操作')
            s['queue'].append({'seq':seq,'events':events,'geometry':f['geometry'],'expires':now+2})
            s['next']+=1
            s['changed'].notify_all()
            return {'accepted':seq}
        if op == 'live-frame':
            if me != s['target']: raise PermissionError('无权发送此桌面')
            accepted = self.frame(s, body.get('frame'), now, allow_repeat=True)
            return {'active':not bool(s['error']) and now-s['viewed']<8, 'frame_seq':accepted}
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
            if f is not None:
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
            return {'inputs':inputs if remaining else [],'active':not bool(s['error']) and remaining>0,'lease_ms':min(2500,remaining)}
        raise ValueError('无效连续桌面请求')

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
            if not isinstance(encoded,str) or len(encoded)>1_400_000: raise ValueError('画面过大')
            raw = base64.b64decode(encoded, validate=True)
            if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'): raise ValueError('无效画面格式')
            if previous and seq <= previous['seq']:
                # A transport retry can acknowledge a stored frame without
                # giving that capture a new timestamp.
                if seq == previous['seq'] and geometry == previous['geometry'] and encoded == previous['jpeg_base64']:
                    return seq
                return None

        if not previous or previous['geometry'] != geometry or previous['jpeg_base64'] != encoded:
            s['same_since'] = seq
        s['frame'] = {'seq':seq, 'geometry':geometry, 'jpeg_base64':encoded}
        s['frame_at'] = now
        s['changed'].notify_all()
        return seq
