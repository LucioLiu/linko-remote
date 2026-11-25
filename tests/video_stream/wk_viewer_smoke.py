"""Run on macOS: python3 tests/video_stream/wk_viewer_smoke.py."""
import base64
import http.server
from pathlib import Path
import struct
import subprocess
import threading
import sys
import tempfile
import time
import zlib
project = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(project), str(project / 'tests')]
from test_live_stream import LiveStreamTLSTests, avc_upload
from lanpilot.peer_client import LocalHandler

workspace = tempfile.TemporaryDirectory(prefix='lanpilot-wk-')
root = Path(workspace.name)
subprocess.run(['swiftc', str(Path(__file__).with_name('WKViewerSmoke.swift')),
                '-o', str(root / 'wk')], check=True)
def chunk(name, data):
    return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data))
(root/'red.png').write_bytes(b'\x89PNG\r\n\x1a\n' +
    chunk(b'IHDR', struct.pack('>IIBBBBB', 64,64,8,2,0,0,0)) +
    chunk(b'IDAT', zlib.compress((b'\0'+bytes((220,32,22))*64)*64)) + chunk(b'IEND', b''))
subprocess.run(['sips','-s','format','jpeg',root/'red.png','--out',root/'red.jpg'],check=True,capture_output=True)
jpeg = base64.b64encode((root/'red.jpg').read_bytes()).decode()
test = LiveStreamTLSTests()
test.setUp()
test.sender.call('register', {'name':'Fixture', 'platform':'windows', 'live_version':1, 'live_avc':1})
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler)
server.client = test.client
server.authority = f'127.0.0.1:{server.server_port}'
server.nonce = 'local-fixture'
threading.Thread(target=server.serve_forever, daemon=True).start()
process = subprocess.Popen([root/'wk','http://'+server.authority+'/#'+server.nonce,test.target])
try:
    test.wait_for(lambda: test.client.live_view is not None, timeout=6)
    view = test.client.live_view
    assert view.media_feed, 'WKWebView did not advertise H.264'
    ident = view.id
    test.wait_for(lambda: '/v2/live-stream' in test.relay.paths)
    for seq, unit in enumerate((0, 1, 2, 3), 1):
        print('UPLOAD AVC', test.upload_avc(ident, avc_upload(seq,unit)), flush=True)
        time.sleep(.15)
    for seq,unit in enumerate((0,1,2,3),5):
        time.sleep(.62)
        print('UPLOAD AVC', test.upload_avc(ident, avc_upload(seq,unit)), flush=True)
    ack = 0
    for seq in range(1,130):
        if process.poll() is not None:
            break
        value = test.sender.call('live-exchange', {'id':ident,'ack':ack})
        if value['live_avc'] == 0:
            if seq == 1 or not value['inputs']:
                test.sender.call('live-frame', {'id':ident,'frame':{
                    'seq':seq,'geometry':'0,0,64,64','jpeg_base64':jpeg}})
            for item in value['inputs']:
                print('FRESH INPUT', item, flush=True)
                ack = item['seq']
        time.sleep(.1)
    assert process.wait(timeout=3) == 0
    assert ack == 1
    assert test.relay.paths.count('/v2/live-stream') >= 2
    assert view.jpeg_requested.is_set()
finally:
    if process.poll() is None:
        process.terminate(); process.wait(timeout=3)
    server.shutdown()
    server.server_close()
    test.doCleanups()
