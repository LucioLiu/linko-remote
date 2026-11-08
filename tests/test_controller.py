import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('desktop',Path(__file__).parents[1]/'desktop.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)

class ControllerTests(unittest.TestCase):
 def test_requires_frame_before_input(self):
  for action in ('click','doubleclick','text','key','scroll'):
   with self.assertRaises(ValueError):d.make_request(action)
 def test_invalid_host_cannot_inject_options_or_commands(self):
  for host in ('-oProxyCommand=bad','user;bad','user$(bad)','user`bad`'):
   with self.assertRaises(ValueError):d.call(host,'x')
 def test_payload_roundtrip_no_shell_interpolation(self):
  text='中文\n$(`danger`)\'"; exit; #'
  req=d.make_request('text',frame_id='a'*32,text=text)
  script=d.remote_script('b'*32,req)
  self.assertNotIn(text,script)
  b64=script.split("FromBase64String('")[1].split("')")[0]
  self.assertEqual(json.loads(base64.b64decode(b64))['text'],text)
 def test_recover_never_resubmits(self):
  script=d.remote_script('a'*32)
  self.assertNotIn('WriteAllBytes',script)
  self.assertNotIn('Move-Item',script)
 def test_ssh_is_strict_and_failure_not_retried(self):
  calls=[]
  def runner(argv,**kwargs):
   calls.append(argv);return subprocess.CompletedProcess(argv,1,b'',b'secret payload')
  with self.assertRaises(RuntimeError) as ctx:d.call('win-test','x',runner)
  self.assertEqual(len(calls),1)
  self.assertIn('StrictHostKeyChecking=yes',calls[0])
  self.assertNotIn('secret',str(ctx.exception))
 def test_large_unicode_payload_uses_stdin_not_commandline(self):
  req=d.make_request('text',frame_id='a'*32,text='中'*2000)
  script=d.remote_script('b'*32,req)
  def runner(argv,**kwargs):
   self.assertLess(len(argv[-1]),1000)
   self.assertEqual(kwargs['input'].decode(),script)
   return subprocess.CompletedProcess(argv,0,b'{"ok":true}',b'')
  self.assertTrue(d.call('win-test',script,runner)['ok'])
 def test_result_saved_private_and_base64_not_printed(self):
  with tempfile.TemporaryDirectory() as tmp:
   response={'request_id':'a'*32,'ok':True,'result':{'png_base64':base64.b64encode(b'\x89PNG\r\n\x1a\nmock').decode()}}
   saved=d.save_result(response,Path(tmp),'a'*32,'test')
   self.assertNotIn('png_base64',saved['result'])
   p=Path(saved['result']['screenshot'])
   self.assertEqual(p.stat().st_mode & 0o777,0o600)
 def test_wrong_response_id_rejected(self):
  with self.assertRaises(ValueError):d.save_result({'request_id':'wrong'},Path('/unused'),'a'*32,'test')
 def test_input_limits(self):
  for amount in (0,11,-11):
   with self.assertRaises(ValueError):d.make_request('scroll',frame_id='a'*32,amount=amount)
  with self.assertRaises(ValueError):d.make_request('text',frame_id='a'*32,text='x'*2001)

 def test_compact_screenshot_declares_format_dimensions_and_stays_private(self):
  from unittest.mock import Mock
  from lanpilot.mcp import invoke
  payload='/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAIBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAABP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AO//Z'
  rid='a'*32
  def response(**fields):return {'ok':True,'request_id':rid,'result':fields}
  with tempfile.TemporaryDirectory() as tmp:
   saved=d.save_result(response(jpeg_base64=payload,bounds=[0,0,2,1]),Path(tmp),rid,'test')
   p=Path(saved['result']['screenshot'])
   self.assertEqual(p.suffix,'.jpg');self.assertEqual(p.stat().st_mode&0o777,0o600)
   bridge=Mock();bridge.request.return_value=saved
   result=invoke(bridge,'observe',{'device':'sample','image_format':'compact'})
   bridge.request.assert_called_with('sample',{'action':'observe','image_format':'compact'})
   self.assertEqual(result['content'][1],{'type':'image','mimeType':'image/jpeg','data':payload})
   with self.assertRaisesRegex(ValueError,'dimensions'):
    d.save_result(response(jpeg_base64=payload,bounds=[0,0,1,2]),Path(tmp),rid,'test')
   with self.assertRaisesRegex(ValueError,'Ambiguous'):
    d.save_result(response(jpeg_base64=payload,png_base64=payload),Path(tmp),rid,'test')
   with self.assertRaisesRegex(ValueError,'Invalid JPEG'):
    d.save_result(response(jpeg_base64=base64.b64encode(b'\xff\xd8\xffbogus\xff\xd9').decode()),Path(tmp),rid,'test')
   saved=d.save_result(response(screenshot='/injected/local/file'),Path(tmp),rid,'test')
   self.assertNotIn('screenshot',saved['result'])
 def test_screenshot_negotiation_is_optional(self):
  self.assertNotIn('image_format',d.make_request('observe'))
  self.assertEqual(d.make_request('observe',image_format='compact')['image_format'],'compact')
  with self.assertRaisesRegex(ValueError,'format'):d.make_request('observe',image_format='unknown')

if __name__=='__main__':unittest.main()
