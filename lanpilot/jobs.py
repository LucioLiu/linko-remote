"""Bounded job manifests for work inside the authenticated Windows user's job area."""
import base64
import hashlib
from pathlib import Path
import re

FILE_LIMIT = 524288
TOTAL_LIMIT = 2097152
DIGEST = re.compile(r'[a-f0-9]{64}')
SCOPE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}')
RESERVED = re.compile(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', re.I)


def fields(obj, required, optional=()):
    if not isinstance(obj, dict) or not set(required) <= obj.keys() <= set(required) | set(optional):
        raise ValueError('Unexpected job manifest fields')


def safe_name(value, csv=False):
    if not isinstance(value, str) or (csv and not value.endswith('.csv')):
        raise ValueError('Invalid job name')
    stem = value[:-4] if csv else value
    if not SCOPE.fullmatch(stem) or RESERVED.fullmatch(stem):
        raise ValueError('Invalid job name')


def digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError('Invalid job SHA-256')


def validate_job(manifest):
    if not isinstance(manifest, dict) or type(manifest.get('version')) is not int or manifest['version'] != 1:
        raise ValueError('Unsupported job version')
    base = ('version', 'operation', 'input_scope')
    op = manifest.get('operation')
    if op in ('job-status', 'job-result'):
        fields(manifest, ('version','operation','job_id'))
        if not isinstance(manifest['job_id'],str) or not re.fullmatch('[a-f0-9]{32}',manifest['job_id']):
            raise ValueError('Invalid original job ID')
        return
    safe_name(manifest.get('input_scope'))
    if op == 'stage-inputs':
        fields(manifest, (*base, 'files'))
        files = manifest['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 48:
            raise ValueError('Stage between 1 and 48 CSV files')
        seen, total = set(), 0
        for item in files:
            fields(item, ('name', 'sha256', 'content_base64'))
            safe_name(item['name'], csv=True)
            digest(item['sha256'])
            key, encoded = item['name'].lower(), item['content_base64']
            if key in seen or not isinstance(encoded, str) or len(encoded) > 4 * ((FILE_LIMIT + 2) // 3):
                raise ValueError('Duplicate or oversized stage input')
            seen.add(key)
            data = base64.b64decode(encoded, validate=True)
            total += len(data)
            if len(data)>FILE_LIMIT or total>TOTAL_LIMIT or hashlib.sha256(data).hexdigest()!=item['sha256']:
                raise ValueError('Stage input exceeds budget or differs from digest')
            data.decode('utf-8')
    elif op == 'reconcile-ledgers':
        fields(manifest, (*base, 'batches'), ('receipt_scope','execution'))
        if manifest.get('execution','foreground') not in ('foreground','background'):
            raise ValueError('Invalid job execution mode')
        if 'receipt_scope' in manifest:
            safe_name(manifest['receipt_scope'])
            if manifest['receipt_scope'].lower() == manifest['input_scope'].lower():
                raise ValueError('Input and receipt scopes must differ')
        batches = manifest['batches']
        if not isinstance(batches, list) or not 1 <= len(batches) <= 24:
            raise ValueError('Reconcile between 1 and 24 batches')
        seen = set()
        for pair in batches:
            fields(pair, ('batch','expected_sha256','actual_sha256'))
            batch = pair['batch']
            if not isinstance(batch,str) or not re.fullmatch('[0-9]{2}',batch) or batch in seen:
                raise ValueError('Invalid or duplicate batch')
            seen.add(batch)
            digest(pair['expected_sha256'])
            digest(pair['actual_sha256'])
    elif op == 'read-artifact':
        fields(manifest, (*base, 'file','sha256'))
        if manifest['file'] != 'result.csv':
            raise ValueError('Only result.csv may be read')
        digest(manifest['sha256'])
    else:
        raise ValueError('Unsupported local job operation')


def stage_manifest(scope, paths):
    safe_name(scope)
    if not isinstance(paths, (tuple,list)) or not 1 <= len(paths) <= 48:
        raise ValueError('Stage between 1 and 48 CSV files')
    files, total = [], 0
    for item in paths:
        if isinstance(item, dict):
            fields(item, ('path', 'name'))
            path, name = Path(item['path']), item['name']
        else:
            path = Path(item)
            name = path.name
        safe_name(name, csv=True)
        with path.open('rb') as stream:
            data = stream.read(FILE_LIMIT + 1)
        total += len(data)
        if len(data)>FILE_LIMIT or total>TOTAL_LIMIT:
            raise ValueError('Staged files exceed size budget')
        files.append(dict(name=name, sha256=hashlib.sha256(data).hexdigest(),
                          content_base64=base64.b64encode(data).decode('ascii')))
    manifest=dict(version=1,operation='stage-inputs',input_scope=scope,files=files)
    validate_job(manifest)
    return manifest
