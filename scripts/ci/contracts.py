"""Engineering checks shared by local development and GitHub Actions."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def facts():
    required = ['AGENTS.md', 'README.md', 'CHANGELOG.md', 'Magpie-主线一实施与验收记录.md',
                'Magpie-主线二实施与验收记录.md', 'docs/research-write-safety-plan.md',
                'docs/api-research-filters-plan.md', 'docs/ci-delivery-plan.md',
                'docs/ci-delivery.md', 'docs/ci-delivery-verification.md',
                'packaging/magpie.spec', 'packaging/smoke.sh', 'frontend/package-lock.json']
    for path in required:
        if not (ROOT / path).is_file() or not (ROOT / path).stat().st_size:
            raise ValueError(f'Missing project fact: {path}')
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    version = project['version']
    module = re.search(r'^__version__ = "([^"]+)"', (ROOT / 'magpie/__init__.py').read_text(encoding='utf-8'), re.M)
    snapshot = json.loads((ROOT / 'magpie/api/tests/openapi_snapshot.json').read_text(encoding='utf-8'))
    if not module or module[1] != version or snapshot['info']['version'] != version:
        raise ValueError('Application, package and OpenAPI versions disagree')
    if not snapshot.get('paths') or not snapshot['openapi'].startswith('3.'):
        raise ValueError('OpenAPI snapshot has no usable contract')
    migrations = sorted(int(p.name.split('_', 1)[0]) for p in (ROOT / 'schema').glob('[0-9][0-9][0-9]_*.sql'))
    if migrations != list(range(1, len(migrations) + 1)):
        raise ValueError('Schema migration numbers are not unique and contiguous')
    print(f'Project facts verified: {version}; {len(migrations)} migrations')


def workflows():
    import yaml
    for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
        # BaseLoader keeps the Actions key "on" instead of YAML 1.1's boolean.
        doc = yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        if not isinstance(doc, dict) or not doc.get('on') or not doc.get('jobs'):
            raise ValueError(f'Incomplete workflow: {path.name}')
        if doc.get('permissions', {}).get('contents') != 'read':
            raise ValueError(f'Workflow must default to contents: read: {path.name}')
        for job in doc['jobs'].values():
            for step in job.get('steps', []):
                action = step.get('uses', '')
                if action and not action.startswith('./') and not re.fullmatch(r'[^@]+@[0-9a-f]{40}', action):
                    raise ValueError(f'Action must use an immutable commit: {action}')
        print(f'Workflow structure verified: {path.name}')


def changes(base):
    if base and not re.fullmatch('[0-9a-f]{40}', base):
        raise ValueError('Comparison base must be a complete commit SHA')
    if not base or base == '0' * 40:
        try:
            base = git('rev-parse', 'HEAD^')
        except subprocess.CalledProcessError:
            base = git('hash-object', '-t', 'tree', '--stdin')
    subprocess.run(['git', 'diff', '--check', base, 'HEAD', '--', '.', ':(exclude)magpie/api/web'],
                   cwd=ROOT, check=True)
    for line in git('diff', '--name-status', '--no-renames', base, 'HEAD', '--', 'schema').splitlines():
        if line and line.split('\t')[0] != 'A':
            raise ValueError(f'Existing schema migrations must remain immutable: {line}')
    print('Changed source whitespace and existing schema migrations verified')


def web():
    status = git('status', '--porcelain', '--untracked-files=all', '--', 'magpie/api/web')
    if status:
        raise ValueError('Frontend build differs from committed files, or contains untracked artifacts:\n' + status)
    print('Frontend build exactly matches committed artifacts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='')
    parser.add_argument('--web-only', action='store_true')
    args = parser.parse_args()
    if args.web_only:
        web()
    else:
        facts(); workflows(); changes(args.base)


if __name__ == '__main__':
    main()
