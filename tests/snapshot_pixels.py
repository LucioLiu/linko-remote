"""Run from repository root: PYTHONPATH=. uv run --with 'pillow>=10.1' tests/snapshot_pixels.py /path/to/chrome."""
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from PIL import Image, ImageDraw, ImageFont
from test_file_transfer import FileTransferTests


def table():
    image = Image.new('RGB', (1920, 720), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=14)
    for row in range(32):
        y = 8 + row * 22
        draw.line((0, y + 17, image.width, y + 17), fill='#adc5df')
        for column in range(10):
            draw.text((column * 192 + 8, y), f'Item {row:02d}-{column}: {row * 17 + column}.08',
                      font=font, fill='#133355')
    return image


def verify(chrome, fixture, original, encoding, folder):
    sender, receiver = fixture.a_client.agent_hub, fixture.b_client.agent_hub
    identifier = uuid.uuid4().hex
    sender.call('submit', {'peer': fixture.b_id, 'id': identifier,
                           'request': {'action': 'observe', 'image_format': 'png'}})
    command = receiver.call('poll')
    assert command['id'] == identifier and command['request']['image_format'] == 'png'
    stream = io.BytesIO()
    original.save(stream, format=encoding)
    data = base64.b64encode(stream.getvalue()).decode()
    field = encoding.lower() + '_base64'
    receiver.call('result', {'id': identifier, 'result': {'request_id': identifier, 'ok': True,
                  'input_attempted': False, 'result': {field: data, 'bounds': [0, 0, *original.size]}}})
    receipt = sender.call('recover', {'id': identifier})
    assert receipt['result']['result'][field] == data
    ui = Path(__file__).resolve().parents[1] / 'ui'
    html = (ui / 'index.html').read_text()
    for name in ['app.js', 'files.js', 'style.css']:
        (folder / name).write_bytes((ui / name).read_bytes())
        html = html.replace('"/' + name + '"', '"' + name + '"')
    html = html.replace('<script src="app.js">', '<script>history.replaceState=()=>{};'
                        'fetch=async()=>({json:async()=>({ok:false})})</script><script src="app.js">')
    original.save(folder / 'source.png')
    script = """
(async()=>{
 displayReceipt(RECEIPT); $('remote-inspect').click();
 const actual=$('snapshot-image'), source=new Image();source.src='source.png';
 await Promise.all([actual.decode(),source.decode()]);
 function pixels(image){
  const c=document.createElement('canvas');c.width=image.naturalWidth;c.height=image.naturalHeight;
  const ctx=c.getContext('2d');ctx.drawImage(image,0,0);return ctx.getImageData(0,0,c.width,c.height).data;
 }
 const a=pixels(actual),b=pixels(source);let mismatch=0;
 for(let i=0;i<a.length;i+=4)if(a[i]!==b[i]||a[i+1]!==b[i+1]||a[i+2]!==b[i+2])mismatch++;
 const box=actual.getBoundingClientRect();
 document.body.replaceChildren(JSON.stringify({mismatch,width:box.width,height:box.height,
   format:document.getElementById('snapshot-info').textContent}));
})()
"""
    (folder / 'index.html').write_text(html.replace('</body>', '<script>' +
        script.replace('RECEIPT', json.dumps(receipt)) + '</script></body>'))
    result = subprocess.run([chrome, '--headless', '--disable-extensions', '--disable-background-networking',
                             '--allow-file-access-from-files', '--user-data-dir=' + str(folder / 'profile'),
                             '--virtual-time-budget=5000', '--dump-dom', (folder / 'index.html').as_uri()],
                            check=True, capture_output=True, text=True, timeout=30)
    metrics = json.loads(result.stdout.split('<body>', 1)[1].split('</body>', 1)[0])
    assert (metrics['width'], metrics['height']) == original.size
    assert ('PNG 无损' if encoding == 'PNG' else 'JPEG 压缩') in metrics['format']
    assert (metrics['mismatch'] == 0) == (encoding == 'PNG')
    print(encoding, metrics)


if __name__ == '__main__':
    fixture = FileTransferTests()
    try:
        fixture.setUp()
        a, b = fixture.a_client.agent_hub, fixture.b_client.agent_hub
        b.call('availability', {'enabled': True})
        pending = a.call('connect', {'code': b.call('code')['code']})
        if pending.get('approval'):
            b.call('approve', {'approval': pending['approval'], 'allow': True})
        source = table()
        for encoding in ('PNG', 'JPEG'):
            with tempfile.TemporaryDirectory() as tmp:
                verify(sys.argv[1], fixture, source, encoding, Path(tmp))
    finally:
        fixture.doCleanups()
