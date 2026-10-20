"""Source provenance and corruption tests, without publishing or external services."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / 'delivery.py'
spec = importlib.util.spec_from_file_location('magpie_delivery', MODULE)
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)
SHA = 'a' * 40
REPO = 'owner/magpie'


class SourceGateTests(unittest.TestCase):
    def setUp(self):
        self.run = {'head_sha':SHA,'head_branch':'main','status':'completed','conclusion':'success',
                    'path':'.github/workflows/ci.yml','name':'Magpie CI',
                    'head_repository':{'full_name':REPO},'event':'push'}
        self.args = dict(expected_sha=SHA,repository=REPO,verified=True,
                         evidence='docs/browser-review.md',dispatch_ref='refs/heads/main')

    def test_successful_same_source(self):
        self.assertEqual(delivery.validate_run(self.run, **self.args), SHA)

    def test_failed_skipped_fork_pr_old_or_wrong_branch_never_authorize_delivery(self):
        for update in ({'conclusion':'failure'}, {'conclusion':'skipped'}, {'status':'in_progress'},
                       {'head_sha':'b'*40}, {'head_branch':'feature'}, {'path':'.github/workflows/other.yml'},
                       {'event':'pull_request'}, {'head_repository':{'full_name':'attacker/fork'}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                delivery.validate_run({**self.run, **update}, **self.args)

    def test_human_declaration_and_dispatch_ref_cannot_be_omitted(self):
        for update in ({'verified':False}, {'verified':'true'}, {'dispatch_ref':'refs/heads/feature'},
                       {'expected_sha':'HEAD'}, {'expected_sha':'a'*7}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                delivery.validate_run(self.run, **{**self.args, **update})

    def test_evidence_paths_cannot_escape_the_repository(self):
        for path in ('', '../private.md', 'docs/../../private.md', '/docs/review.md',
                     'C:\\private.md', 'docs/review.md:payload', '.github/workflows/ci.yml'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                delivery.evidence_path(path)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='magpie-artifact-test-')
        self.root = Path(self.tmp.name)
        self.output = self.root / 'deliverables'
        self.records = []
        for platform, entry in [('windows-x86_64','magpie.exe'),('linux-x86_64','magpie')]:
            folder = self.root / platform / 'magpie'
            (folder / '_internal').mkdir(parents=True)
            (folder / entry).write_text('synthetic unit-test payload, not an executable', encoding='utf-8')
            self.records.append(delivery.package(folder, self.output, platform, SHA))

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_zip_and_tar_archives_have_matching_manifests(self):
        version = delivery.verify(self.output, SHA)
        self.assertEqual(version, self.records[0]['version'])
        self.assertEqual(len((self.output / 'SHA256SUMS').read_text().splitlines()), 2)
        delivery.verify(self.output, SHA)  # repeat verification is valid

    def test_corruption_and_wrong_source_are_rejected(self):
        with self.assertRaises(ValueError):
            delivery.verify(self.output, 'b'*40)
        archive = self.output / self.records[0]['archive']
        archive.write_bytes(archive.read_bytes() + b'tampered')
        with self.assertRaises(ValueError):
            delivery.verify(self.output, SHA)

    def test_missing_platform_is_rejected(self):
        next(self.output.glob('magpie-*.json')).unlink()
        with self.assertRaises(ValueError):
            delivery.verify(self.output, SHA)

    def test_unexpected_payload_and_manifest_path_injection_are_rejected(self):
        extra = self.output / 'unexpected.txt'
        extra.write_text('unexpected')
        with self.assertRaises(ValueError):
            delivery.verify(self.output, SHA)
        extra.unlink()
        manifest = next(self.output.glob('magpie-*.json'))
        record = json.loads(manifest.read_text())
        record['archive'] = '../outside.zip'
        manifest.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            delivery.verify(self.output, SHA)

    def test_existing_package_is_never_overwritten(self):
        directory = self.root / 'windows-x86_64' / 'magpie'
        before = (self.output / self.records[0]['archive']).read_bytes()
        with self.assertRaises(ValueError):
            delivery.package(directory, self.output, 'windows-x86_64', SHA)
        self.assertEqual((self.output / self.records[0]['archive']).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
