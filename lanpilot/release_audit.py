"""Audit release archives without extracting them or reading a user profile.

Private names/IDs can be supplied in a local JSON list with --deny-file. That
file belongs outside the public source tree and is never included in reports.
Run with the build environment's Python and PyInstaller to inspect frozen code.
"""
import argparse
import io
import json
from pathlib import Path, PurePosixPath
import re
import types
import zipfile


PRIVATE_FILES = {'client.json', 'shell-session.json', 'pending.json', 'config.json',
                 'devices.json', 'peers.sqlite', 'peers.sqlite-wal', 'pair-key.dpapi',
                 'delivery.json', 'hub-outbox.json', 'startup.err'}
SECRET = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
                    rb'ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}')


class ReleaseAudit:
    def __init__(self, denied=()):
        self.needles = [(word.encode(encoding).lower()) for word in denied if word
                        for encoding in ('utf-8', 'utf-16le', 'utf-16be')]
        self.files = 0
        self.findings = []

    def inspect(self, name, data, depth=0):
        self.files += 1
        path = PurePosixPath(name.replace('\\', '/'))
        if path.name in PRIVATE_FILES or any(part in ('.git', 'captures', 'webview-profile') for part in path.parts):
            self.findings.append({'file': name, 'category': 'private-runtime-file'})
        if SECRET.search(data):
            self.findings.append({'file': name, 'category': 'private-key-or-token'})
        lowered = data.lower()
        if any(needle in lowered for needle in self.needles):
            self.findings.append({'file': name, 'category': 'private-name-id-or-path'})
        if path.name == 'peer-endpoint.json':
            try:
                if set(json.loads(data)) != {'url', 'pin'}:
                    raise ValueError()
            except (ValueError, TypeError):
                self.findings.append({'file': name, 'category': 'non-public-endpoint-fields'})
        if depth < 4 and zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for item in archive.infolist():
                    if not item.is_dir():
                        self.inspect(name + '!/' + item.filename, archive.read(item), depth + 1)

    def frozen(self, binary):
        from PyInstaller.archive.readers import CArchiveReader
        import marshal
        archive = CArchiveReader(str(binary))

        def walk(name, code):
            self.inspect(name + ':filename', code.co_filename.encode())
            for index, value in enumerate(code.co_consts):
                label = name + ':' + str(index)
                if isinstance(value, types.CodeType):
                    walk(label, value)
                elif isinstance(value, str):
                    self.inspect(label, value.encode('utf-8', errors='surrogatepass'))
                elif isinstance(value, bytes):
                    self.inspect(label, value)

        for name, entry in archive.toc.items():
            if entry[-1] == 's':
                walk(name, marshal.loads(archive.extract(name)))
            elif entry[-1] == 'z':
                embedded = archive.open_embedded_archive(name)
                for module in embedded.toc:
                    code = embedded.extract(module)
                    if isinstance(code, types.CodeType):
                        walk(module, code)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('artifacts', nargs='+', type=Path)
    parser.add_argument('--deny-file', type=Path)
    parser.add_argument('--frozen', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    audit = ReleaseAudit(json.loads(args.deny_file.read_text()) if args.deny_file else [])
    for artifact in args.artifacts:
        if artifact.is_dir():
            for file in artifact.rglob('*'):
                if file.is_file():
                    audit.inspect(file.relative_to(artifact).as_posix(), file.read_bytes())
        else:
            audit.inspect(artifact.name, artifact.read_bytes())
    if args.frozen:
        audit.frozen(args.frozen)
    report = {'ok': not audit.findings, 'files_checked': audit.files,
              'findings': audit.findings}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'ok': report['ok'], 'files_checked': audit.files,
                      'findings_count': len(audit.findings)}))
    raise SystemExit(0 if report['ok'] else 1)


if __name__ == '__main__':
    main()
