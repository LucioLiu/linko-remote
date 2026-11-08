import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from desktop import make_request, save_result
from lanpilot.jobs import stage_manifest, validate_job


class JobTests(unittest.TestCase):
    def test_exact_named_file_roundtrip_and_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'expected-01.csv'
            raw = 'id,amount\r\n中文,1.20\r\n'.encode()
            path.write_bytes(raw)
            manifest = stage_manifest('november', [path])
            self.assertEqual(base64.b64decode(manifest['files'][0]['content_base64']), raw)
            self.assertEqual(make_request('job',manifest=manifest)['manifest'],manifest)
            manifest['files'][0]['sha256'] = '0'*64
            with self.assertRaises(ValueError): validate_job(manifest)
            with self.assertRaises(ValueError): stage_manifest('november',[path,path])
            for scope in ['../root','NUL','c:/tmp','a.b','']:
                with self.assertRaises(ValueError): stage_manifest(scope,[path])

    def test_unscoped_or_duplicate_jobs_rejected(self):
        base={'version':1,'operation':'reconcile-ledgers','input_scope':'november'}
        pair={'batch':'01','expected_sha256':'a'*64,'actual_sha256':'b'*64}
        validate_job(dict(base,batches=[pair],execution='background'))
        for changes in ({'batches':[pair,pair]},{'batches':[pair],'command':'whoami'},
                        {'batches':[pair],'receipt_scope':'NOVEMBER'},{'batches':[]}):
            with self.assertRaises(ValueError): validate_job(dict(base,**changes))
        with self.assertRaises(ValueError): make_request('job',manifest=base,command='hostname')
        validate_job({'version':1,'operation':'job-result','job_id':'1'*32})

    def test_job_artifact_is_saved_only_after_full_digest_matches(self):
        raw=b'batch,id,expected_cents,actual_cents,status\r\n'
        rid='a'*32
        artifact={'name':'result.csv','content_base64':base64.b64encode(raw).decode(),
                  'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        def response(a):
            return {'request_id':rid,'ok':True,'result':{'artifact':dict(a),'report':
                    {'csv_sha256':artifact['sha256'],'csv_bytes':len(raw)}}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError): save_result(response(dict(artifact,sha256='0'*64)),Path(tmp),rid,'test')
            self.assertFalse(list(Path(tmp).iterdir()))
            saved=save_result(response(artifact),Path(tmp),rid,'test')['result']['artifact']
            file=Path(saved['local_path'])
            self.assertEqual(file.read_bytes(),raw)
            self.assertEqual(saved['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertNotIn('content_base64',saved)
            self.assertEqual(file.stat().st_mode&0o777,0o600)
