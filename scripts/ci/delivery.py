"""Fail-closed release provenance and archive integrity checks; no network calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = {'windows-x86_64': 'zip', 'linux-x86_64': 'tar.gz'}


def sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value):
        raise ValueError('A complete lowercase source commit SHA is required')
    return value


def evidence_path(value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or '..' in path.parts or '\\' in value
            or ':' in value or path.suffix != '.md'
            or not (value.startswith('docs/') or value == 'Magpie-主线二实施与验收记录.md')):
        raise ValueError('Evidence must be a repository Markdown record in docs/ or the mainline-two record')
    return path.as_posix()


def validate_run(run, *, expected_sha, repository, verified, evidence, dispatch_ref):
    sha(expected_sha); evidence_path(evidence)
    if dispatch_ref != 'refs/heads/main':
        raise ValueError('Delivery must be dispatched from main')
    if verified is not True:
        raise ValueError('Explicit local browser verification is required before packaging')
    expected = {'head_sha': expected_sha, 'head_branch': 'main', 'status': 'completed',
                'conclusion': 'success', 'path': '.github/workflows/ci.yml', 'name': 'Magpie CI'}
    if any(run.get(key) != value for key, value in expected.items()):
        raise ValueError('CI run does not match the successful main-branch workflow and source SHA')
    if (run.get('head_repository', {}).get('full_name') != repository
            or run.get('event') not in ('push', 'workflow_dispatch')):
        raise ValueError('CI run is from another repository, fork or PR event')
    return expected_sha


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def package(directory, output, platform, source_sha):
    sha(source_sha)
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Delivery currently requires a three-part application version')
    if platform not in PLATFORMS or directory.name != 'magpie':
        raise ValueError('Expected a magpie onedir folder and a supported platform')
    entry = 'magpie.exe' if platform.startswith('windows') else 'magpie'
    if not (directory / entry).is_file() or not (directory / '_internal').is_dir():
        raise ValueError('Incomplete PyInstaller onedir bundle')
    output.mkdir(parents=True, exist_ok=True)
    name = f'magpie-{version}-{platform}'
    archive = output / (name + '.' + PLATFORMS[platform])
    manifest = output / (name + '.json')
    if archive.exists() or manifest.exists():
        raise ValueError('Refusing to overwrite an existing archive or manifest')
    (directory / 'README.md').write_text((ROOT / 'packaging/README-pack.md').read_text(
        encoding='utf-8').replace('@VERSION@', version), encoding='utf-8')
    shutil.make_archive(str(output / name), 'zip' if platform.startswith('windows') else 'gztar',
                        root_dir=directory.parent, base_dir='magpie')
    record = {'version': version, 'source_sha': source_sha, 'platform': platform,
              'archive': archive.name, 'size': archive.stat().st_size, 'sha256': digest(archive)}
    manifest.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record


def verify(directory, source_sha):
    sha(source_sha)
    manifests = list(directory.glob('magpie-*.json'))
    if any(not p.is_file() or p.is_symlink() for p in manifests):
        raise ValueError('Manifests must be regular files')
    records = [json.loads(p.read_text(encoding='utf-8')) for p in manifests]
    if len(records) != 2 or {r.get('platform') for r in records} != set(PLATFORMS):
        raise ValueError('Exactly one Windows and one Linux manifest is required')
    versions = {r.get('version') for r in records}
    if len(versions) != 1:
        raise ValueError('Platform versions disagree')
    version = next(iter(versions))
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid release version')
    expected_files = set()
    sums = []
    for record in records:
        name = f"magpie-{version}-{record['platform']}.{PLATFORMS[record['platform']]}"
        path = directory / name
        if (record.get('source_sha') != source_sha or record.get('archive') != name
                or not path.is_file() or path.is_symlink()
                or record.get('size') != path.stat().st_size or record.get('sha256') != digest(path)):
            raise ValueError('Archive provenance, size or SHA-256 mismatch')
        expected_files.add(name)
        expected_files.add(f"magpie-{version}-{record['platform']}.json")
        sums.append(f"{record['sha256']}  {name}")
    if (directory / 'SHA256SUMS').exists():
        expected_files.add('SHA256SUMS')
    if any(not p.is_file() or p.is_symlink() for p in directory.iterdir()):
        raise ValueError('Deliverables must be regular files, without subdirectories or symlinks')
    actual_files = {p.name for p in directory.iterdir()}
    if actual_files != expected_files:
        raise ValueError('Unexpected or missing release archives')
    (directory / 'SHA256SUMS').write_text('\n'.join(sorted(sums)) + '\n', encoding='utf-8')
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    gate = sub.add_parser('gate'); gate.add_argument('run', type=Path)
    pack = sub.add_parser('package'); pack.add_argument('directory', type=Path)
    pack.add_argument('output', type=Path); pack.add_argument('platform', choices=PLATFORMS)
    check = sub.add_parser('verify'); check.add_argument('directory', type=Path)
    for child in (gate, pack, check):
        child.add_argument('--sha', required=True)
    gate.add_argument('--repository', required=True); gate.add_argument('--evidence', required=True)
    gate.add_argument('--verified', choices=['true', 'false'], required=True)
    gate.add_argument('--dispatch-ref', required=True)
    args = parser.parse_args()
    if args.command == 'gate':
        print(validate_run(json.loads(args.run.read_text(encoding='utf-8')), expected_sha=args.sha,
                           repository=args.repository, evidence=args.evidence,
                           verified=args.verified == 'true', dispatch_ref=args.dispatch_ref))
        subprocess.run(['git', 'merge-base', '--is-ancestor', args.sha, 'origin/main'], cwd=ROOT, check=True)
        record = subprocess.check_output(['git', 'show', f'{args.sha}:{evidence_path(args.evidence)}'], cwd=ROOT)
        if not record.strip():
            raise ValueError('The verified source commit contains no evidence record')
    elif args.command == 'package':
        print(json.dumps(package(args.directory, args.output, args.platform, args.sha)))
    else:
        print(verify(args.directory, args.sha))


if __name__ == '__main__':
    main()
