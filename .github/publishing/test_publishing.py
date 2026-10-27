"""Finite mocked protocol tests. No registry/GitHub HTTP, token or child execution.

These tests are not an OIDC or actual registry acceptance receipt. Run them in
either the canonical source directory or the selected standalone SDK export.
"""
import copy
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_prebuilt as publisher
import provider_helpers as helpers


class FakeGitHub:
    def __init__(self, state):
        self.state, self.files, self.writes = state, {}, []
        self.lose_marker_ack = False
        self.tag = state['manifest']['source_commit']

    def main(self):
        return 'a' * 40

    def file(self, path, ref):
        return copy.deepcopy(self.state['approval']), 'b' * 40

    def tag_commit(self, tag):
        return self.tag

    def marker(self, path):
        return copy.deepcopy(self.files.get(path))

    def put_marker(self, path, record, existing_sha=None, *, approved_head=None):
        if existing_sha is None:
            helpers.require(approved_head == self.main(), 'approval_head_changed')
        old = self.files.get(path)
        helpers.require(old is None if existing_sha is None else old is not None and old[1] == existing_sha, 'marker_conflict')
        sha = format(len(self.writes) + 1, '040x')
        self.files[path] = copy.deepcopy(record), sha
        self.writes.append(copy.deepcopy(record))
        if self.lose_marker_ack:
            self.lose_marker_ack = False
            raise helpers.Stop('request_unknown')
        return sha


class FakeRegistry:
    def __init__(self, state):
        self.state, self.ecosystem = state, 'python'
        self.saved, self.uploads = {}, []
        self.lose_upload_ack = None
        self.mismatch = False

    def token(self):
        return 'short-mock-token-never-logged'

    def existing(self, artifact):
        if self.mismatch:
            raise helpers.Stop('registry_digest_mismatch')
        data = self.saved.get(artifact['filename'])
        if data is None:
            return None
        helpers.require(helpers.sha(data) == artifact['sha256'], 'registry_digest_mismatch')
        return {'method': 'exact_archive_bytes', 'upload_sha256': artifact['sha256'], 'download_sha256': artifact['sha256']}

    def upload(self, artifact, token):
        data = (Path(self.state['directory']) / artifact['filename']).read_bytes()
        self.uploads.append((artifact['filename'], data))
        self.saved[artifact['filename']] = data
        if self.lose_upload_ack == artifact['filename']:
            self.lose_upload_ack = None
            raise helpers.Stop('request_unknown')


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.state = {'schema': 1, 'approval': {'schema': 1, 'ecosystem': 'python', 'package': 'availkit', 'version': '0.2.0', 'tag': 'v0.2.0', 'source_commit': 'c' * 40, 'manifest_sha256': 'd' * 64},
                      'manifest': {'schema': 1, 'ecosystem': 'python', 'repository': 'availkit/sdk-python', 'package': 'availkit', 'version': '0.2.0', 'tag': 'v0.2.0', 'source_commit': 'c' * 40, 'artifacts': []},
                      'release_id': 123, 'run_id': '456', 'run_attempt': '1', 'directory': str(self.directory)}
        for role, filename in publisher.expected_files('python', '0.2.0').items():
            data = ('mock-reviewed-' + role).encode()
            (self.directory / filename).write_bytes(data)
            self.state['manifest']['artifacts'].append({'role': role, 'filename': filename, 'size': len(data), 'sha256': helpers.sha(data)})
        self.github, self.registry = FakeGitHub(self.state), FakeRegistry(self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_stop(self, code, action):
        with self.assertRaises(helpers.Stop) as result:
            action()
        self.assertEqual(result.exception.code, code)

    def test_two_files_create_markers_before_upload_and_rerun_never_uploads_again(self):
        original_upload = self.registry.upload
        def checked_upload(artifact, token):
            marker = self.github.marker(publisher.marker_path(self.state, artifact))
            self.assertIsNotNone(marker)
            self.assertEqual(marker[0]['state'], 'attempted')
            self.assertEqual(marker[0]['artifact_sha256'], helpers.sha((self.directory / artifact['filename']).read_bytes()))
            original_upload(artifact, token)
        self.registry.upload = checked_upload
        self.assertEqual(publisher.publish(self.github, self.registry, self.state), 2)
        self.assertEqual(len(self.registry.uploads), 2)
        self.assertEqual(publisher.publish(self.github, self.registry, self.state), 2)
        self.assertEqual(len(self.registry.uploads), 2)
        self.assertTrue(all(row[0]['state'] == 'confirmed' for row in self.github.files.values()))

    def test_lost_first_upload_stops_second_then_reconciles_exact_first_and_only_sends_unattempted_second(self):
        first = self.state['manifest']['artifacts'][0]
        self.registry.lose_upload_ack = first['filename']
        self.assert_stop('request_unknown', lambda: publisher.publish(self.github, self.registry, self.state))
        self.assertEqual([row[0] for row in self.registry.uploads], [first['filename']])
        self.assertEqual(len(self.github.files), 1)
        self.assertEqual(publisher.publish(self.github, self.registry, self.state), 2)
        self.assertEqual(len(self.registry.uploads), 2)
        self.assertEqual(len({row[0] for row in self.registry.uploads}), 2)

    def test_lost_marker_ack_makes_zero_uploads_and_existing_unresolved_marker_blocks_rerun(self):
        self.github.lose_marker_ack = True
        self.assert_stop('request_unknown', lambda: publisher.publish(self.github, self.registry, self.state))
        self.assertEqual(self.registry.uploads, [])
        self.assertEqual(len(self.github.files), 1)
        self.assert_stop('attempt_unknown_stop_no_upload', lambda: publisher.publish(self.github, self.registry, self.state))
        self.assertEqual(self.registry.uploads, [])
        self.assertEqual(len(self.github.writes), 1)

    def test_changed_tag_and_mismatched_registry_bytes_stop_before_attempt(self):
        self.github.tag = 'e' * 40
        self.assert_stop('source_tag_mismatch', lambda: publisher.publish(self.github, self.registry, self.state))
        self.assertEqual(self.github.writes, [])
        self.github.tag = self.state['manifest']['source_commit']
        self.registry.mismatch = True
        self.assert_stop('registry_digest_mismatch', lambda: publisher.publish(self.github, self.registry, self.state))
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.registry.uploads, [])

    def test_contents_create_only_and_branch_parent_race_fail_before_registry_stage(self):
        calls = []
        class Wire:
            def json(self, method, url, value=None, headers=None, missing=False):
                calls.append((method, url, value))
                if method == 'GET':
                    return {'object': {'type': 'commit', 'sha': 'a' * 40}}
                return {'commit': {'parents': [{'sha': 'b' * 40}]}, 'content': {'sha': 'c' * 40}}
        github = publisher.GitHub(Wire(), 'availkit/sdk-python', 'mock-github-token')
        self.assert_stop('marker_head_changed', lambda: github.put_marker('.github/publishing/attempts/python/0.2.0/availkit-0.2.0-py3-none-any.whl.json', {'filename': 'availkit-0.2.0-py3-none-any.whl'}, approved_head='a' * 40))
        self.assertEqual(len(calls), 2)
        self.assertNotIn('sha', calls[1][2])
        self.assertEqual(calls[1][2]['branch'], 'main')

    def test_changed_or_deleted_approval_between_read_and_marker_never_uploads(self):
        # Use the actual GitHub admission helper. Advancing main either before
        # its precheck or during the PUT must never authorize the older approval.
        import base64
        from urllib.parse import urlsplit, parse_qs
        state = self.state
        for mutation in ('change', 'delete'):
            for timing in ('before_precheck', 'during_put'):
                with self.subTest(mutation=mutation, timing=timing):
                    class Wire:
                        def __init__(self):
                            self.head = 'a' * 40
                            self.marker_record = None
                            self.puts = 0
                            self.approval_read = False
                            self.current_approval = copy.deepcopy(state['approval'])

                        def mutate(self):
                            self.head = 'e' * 40
                            self.current_approval = None if mutation == 'delete' else {**state['approval'], 'manifest_sha256': 'f' * 64}

                        def json(self, method, url, value=None, headers=None, missing=False):
                            parsed = urlsplit(url)
                            if parsed.path.endswith('/git/ref/heads/main'):
                                return {'object': {'type': 'commit', 'sha': self.head}}
                            if '/git/ref/tags/' in parsed.path:
                                return {'object': {'type': 'commit', 'sha': state['manifest']['source_commit']}}
                            if '/contents/.github/publishing/approved/' in parsed.path:
                                # Immutable A is the approval actually read;
                                # a simultaneous edit/delete moves main to E.
                                self.asserted_ref = parse_qs(parsed.query)['ref'][0]
                                self.approval_read = True
                                result = {'type': 'file', 'encoding': 'base64', 'size': 1000, 'sha': 'b' * 40,
                                          'content': base64.b64encode(json.dumps(state['approval']).encode()).decode()}
                                if timing == 'before_precheck':
                                    self.mutate()
                                return result
                            if '/contents/.github/publishing/attempts/' in parsed.path:
                                if method == 'GET':
                                    return None
                                self.puts += 1
                                if timing == 'during_put':
                                    self.mutate()
                                self.marker_record = json.loads(base64.b64decode(value['content']))
                                return {'commit': {'parents': [{'sha': self.head}]}, 'content': {'sha': 'd' * 40}}
                            raise AssertionError('unexpected mocked request')

                    wire = Wire()
                    github = publisher.GitHub(wire, 'availkit/sdk-python', 'mock-github-token')
                    registry = FakeRegistry(state)
                    code = 'approval_head_changed' if timing == 'before_precheck' else 'marker_head_changed'
                    self.assert_stop(code, lambda: publisher.publish(github, registry, state))
                    self.assertTrue(wire.approval_read)
                    self.assertEqual(wire.asserted_ref, 'a' * 40)
                    self.assertEqual(registry.uploads, [])
                    self.assertNotEqual(wire.current_approval, state['approval'])
                    if timing == 'during_put':
                        self.assertEqual(wire.puts, 1)
                        self.assertEqual(wire.marker_record['state'], 'attempted')
                        self.assertEqual(wire.marker_record['manifest_sha256'], state['approval']['manifest_sha256'])
                    else:
                        self.assertEqual(wire.puts, 0)
                        self.assertIsNone(wire.marker_record)

    def test_unknown_manifest_fields_or_filename_stop_before_any_package_download(self):
        state = self.state
        class ReleaseGitHub:
            def __init__(self, manifest):
                self.manifest = json.dumps(manifest).encode(); self.downloads = []
                self.approval = copy.deepcopy(state['approval']); self.approval['manifest_sha256'] = helpers.sha(self.manifest)
            def json(self, method, path):
                if path == '': return {'default_branch': 'main', 'private': False, 'fork': False}
                return {'id': 123, 'tag_name': 'v0.2.0', 'draft': False, 'prerelease': False, 'published_at': '2026-10-27T00:00:00Z', 'immutable': True, 'assets': []}
            def main(self): return 'a' * 40
            def file(self, path, ref): return self.approval, 'b' * 40
            def tag_commit(self, tag): return 'c' * 40
            def asset(self, release, filename, expected_digest=None):
                self.downloads.append(filename)
                if filename != 'release-manifest.json': raise AssertionError('unexpected artifact download')
                return self.manifest
        for variant, code in [('unknown_root', 'manifest_schema'), ('arbitrary_url', 'artifact_schema'), ('path_escape', 'artifact_allowlist')]:
            with self.subTest(variant=variant):
                manifest = copy.deepcopy(state['manifest'])
                if variant == 'unknown_root': manifest['unapproved_url'] = 'https://example.invalid'
                elif variant == 'arbitrary_url': manifest['artifacts'][0]['url'] = 'https://example.invalid'
                else: manifest['artifacts'][0]['filename'] = '../private.key'
                github = ReleaseGitHub(manifest)
                with patch.dict(os.environ, {'GITHUB_RUN_ID': '456', 'GITHUB_RUN_ATTEMPT': '1'}):
                    self.assert_stop(code, lambda: publisher.load_release(github, 'python', 'v0.2.0', self.directory))
                self.assertEqual(github.downloads, ['release-manifest.json'])

    def test_cargo_raw_framing_retains_exact_prebuilt_crate(self):
        metadata = json.dumps({'name': 'availkit-sdk', 'vers': '0.1.1', 'deps': [], 'features': {}, 'repository': 'https://github.com/availkit/sdk-rust'}).encode()
        crate = b'exact-reviewed-prebuilt-crate-raw-bytes'
        body = helpers.cargo_upload(metadata, crate, 'availkit-sdk', '0.1.1', 'https://github.com/availkit/sdk-rust')
        length = struct.unpack('<I', body[:4])[0]
        self.assertEqual(body[4:4 + length], metadata)
        self.assertEqual(struct.unpack('<I', body[4 + length:8 + length])[0], len(crate))
        self.assertEqual(body[8 + length:], crate)

    def test_cargo_preflight_validates_approved_metadata_and_crate_before_any_attempt(self):
        import tarfile
        version = '0.1.1'
        stream = io.BytesIO()
        cargo_toml = b'[package]\nname = "availkit-sdk"\nversion = "0.1.1"\nrepository = "https://github.com/availkit/sdk-rust"\n'
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            entry = tarfile.TarInfo('availkit-sdk-0.1.1/Cargo.toml')
            entry.size = len(cargo_toml)
            archive.addfile(entry, io.BytesIO(cargo_toml))
        crate = stream.getvalue()
        valid = {'name': 'availkit-sdk', 'vers': version, 'deps': [], 'features': {}, 'repository': 'https://github.com/availkit/sdk-rust'}
        variants = [('valid', json.dumps(valid).encode(), None),
                    ('wrong_identity', json.dumps({**valid, 'vers': '0.1.2'}).encode(), 'package_identity'),
                    ('bad_json', b'{', 'invalid_json'),
                    ('duplicate_identity', json.dumps(valid).encode()[:-1] + b',"vers":"0.1.2"}', 'duplicate_json_field')]
        for label, metadata, code in variants:
            with self.subTest(variant=label):
                files = publisher.expected_files('rust', version)
                payloads = {files['package']: crate, files['cargo-metadata']: metadata}
                manifest = {'schema': 1, 'ecosystem': 'rust', 'repository': 'availkit/sdk-rust', 'package': 'availkit-sdk',
                            'version': version, 'tag': 'v' + version, 'source_commit': 'c' * 40,
                            'artifacts': [{'role': role, 'filename': name, 'size': len(payloads[name]), 'sha256': helpers.sha(payloads[name])} for role, name in files.items()]}
                manifest_bytes = json.dumps(manifest).encode()
                class ReleaseGitHub:
                    attempts = 0
                    def main(self): return 'a' * 40
                    def tag_commit(self, tag): return 'c' * 40
                    def file(self, path, ref):
                        return {key: manifest[key] for key in ['schema', 'ecosystem', 'package', 'version', 'tag', 'source_commit']} | {'manifest_sha256': helpers.sha(manifest_bytes)}, 'b' * 40
                    def json(self, method, path):
                        if path == '': return {'default_branch': 'main', 'private': False, 'fork': False}
                        return {'id': 123, 'tag_name': 'v' + version, 'draft': False, 'prerelease': False, 'published_at': '2026-10-27T00:00:00Z', 'immutable': True, 'assets': []}
                    def asset(self, release, filename, expected_digest=None):
                        return manifest_bytes if filename == 'release-manifest.json' else payloads[filename]
                    def put_marker(self, *args, **kwargs):
                        self.attempts += 1
                        raise AssertionError('preflight cannot record an attempt')
                github = ReleaseGitHub()
                directory = self.directory / label
                directory.mkdir()
                with patch.dict(os.environ, {'GITHUB_RUN_ID': '456', 'GITHUB_RUN_ATTEMPT': '1'}), patch.object(publisher, 'cargo_upload', wraps=helpers.cargo_upload) as framing:
                    action = lambda: publisher.load_release(github, 'rust', 'v' + version, directory)
                    if code:
                        self.assert_stop(code, action)
                    else:
                        result = action()
                        framing.assert_called_once_with(metadata, crate, 'availkit-sdk', version, 'https://github.com/availkit/sdk-rust')
                        self.assertEqual(result['manifest'], manifest)
                self.assertEqual(github.attempts, 0)

    def test_python_multipart_has_unmodified_wheel_bytes_and_metadata(self):
        metadata = b'Metadata-Version: 2.4\nName: availkit\nVersion: 0.2.0\nProject-URL: Repository, https://github.com/availkit/sdk-python\n\nSDK description.\n'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('availkit-0.2.0.dist-info/METADATA', metadata)
        wheel = stream.getvalue()
        body, content_type = helpers.python_upload(wheel, 'availkit-0.2.0-py3-none-any.whl', '0.2.0')
        self.assertTrue(content_type.startswith('multipart/form-data; boundary='))
        self.assertEqual(body.count(wheel), 1)
        self.assertIn(helpers.sha(wheel).encode(), body)

    def test_npm_upload_disables_hooks_retries_provenance_and_has_no_token_arguments(self):
        data = b'mock-reviewed-npm-tarball'
        filename = 'availkit-booking-0.4.0.tgz'
        (self.directory / filename).write_bytes(data)
        state = copy.deepcopy(self.state)
        state['manifest']['ecosystem'] = 'typescript'
        artifact = {'filename': filename, 'sha256': helpers.sha(data), 'size': len(data)}
        calls = []
        def child(command, directory, environment, name, seconds=90):
            calls.append((command, environment))
            return 0, None, None
        with patch.dict(os.environ, {'NODE_AUTH_TOKEN': 'mock-secret', 'NPM_TOKEN': 'mock-secret', 'NPM_CONFIG_PROVENANCE_FILE': '/unapproved', 'NODE_OPTIONS': '--unapproved'}), patch.object(publisher, 'bounded_child', child):
            publisher.Registry(None, state).upload(artifact, None)
        command, environment = calls[0]
        self.assertIn('--ignore-scripts', command)
        self.assertIn('--provenance=false', command)
        self.assertNotIn('mock-secret', ' '.join(command))
        self.assertEqual(environment['NPM_CONFIG_FETCH_RETRIES'], '0')
        self.assertNotIn('NPM_CONFIG_PROVENANCE_FILE', environment)
        self.assertNotIn('NODE_AUTH_TOKEN', environment)
        self.assertNotIn('NODE_OPTIONS', environment)

    def test_nuget_only_announced_repository_signer_and_unchanged_payload_are_accepted(self):
        def archive(entries):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as target:
                for name, data in entries.items(): target.writestr(name, data)
            return stream.getvalue()
        original = archive({'lib/net10.0/AvailKit.dll': b'reviewed-dll'})
        signed = archive({'lib/net10.0/AvailKit.dll': b'reviewed-dll', '.signature.p7s': b'mock-signature'})
        fingerprint = 'a' * 64
        class Wire:
            def json(self, method, url):
                self.url = url
                return {'allRepositorySigned': True, 'signingCertificates': [{'fingerprints': {'2.16.840.1.101.3.4.2.1': fingerprint}}]}
        wire = Wire()
        def child(command, directory, environment, name, seconds=90):
            self.assertNotIn('REGISTRY_TOKEN', environment)
            self.assertEqual(command[:4], ['dotnet', 'nuget', 'verify', '--all'])
            out, err = directory / (name + '.out'), directory / (name + '.err')
            out.write_text('Signature type: Repository\nnuget-v3-service-index-url: https://api.nuget.org/v3/index.json\nSHA256 hash: ' + fingerprint)
            err.write_text('')
            return 0, out, err
        with patch.object(helpers, 'bounded_child', child):
            proof = helpers.nuget_signed_match(wire, original, signed, self.directory)
        self.assertEqual(proof['upload_sha256'], helpers.sha(original))
        self.assertEqual(proof['download_sha256'], helpers.sha(signed))
        self.assert_stop('nuget_content_mismatch', lambda: helpers.nuget_signed_match(wire, original, archive({'lib/net10.0/AvailKit.dll': b'changed', '.signature.p7s': b'mock'}), self.directory))

    def test_nuget_valid_other_author_signature_is_not_nuget_repository_proof(self):
        def archive(entries):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as target:
                for name, data in entries.items(): target.writestr(name, data)
            return stream.getvalue()
        original = archive({'fixture': b'expected'})
        signed = archive({'fixture': b'expected', '.signature.p7s': b'mock-signature'})
        class Wire:
            def json(self, *args): return {'allRepositorySigned': True, 'signingCertificates': [{'fingerprints': {'2.16.840.1.101.3.4.2.1': 'a' * 64}}]}
        def child(command, directory, environment, name, seconds=90):
            out, err = directory / (name + '.out'), directory / (name + '.err')
            out.write_text('Signature type: Author\nSHA256 hash: ' + 'a' * 64); err.write_text('')
            return 0, out, err
        with patch.object(helpers, 'bounded_child', child):
            self.assert_stop('nuget_repository_signature_required', lambda: helpers.nuget_signed_match(Wire(), original, signed, self.directory))

    def test_child_timeout_kills_only_its_owned_group_and_joins_before_return(self):
        import subprocess
        class Child:
            pid = 123456
            calls = 0
            def wait(self, timeout=None):
                self.calls += 1
                if timeout is not None: raise subprocess.TimeoutExpired(['mock-npm'], timeout)
                return -9
        child = Child(); signalled = []
        with patch.object(helpers.subprocess, 'Popen', return_value=child), patch.object(helpers.os, 'killpg', side_effect=lambda pid, signal: signalled.append(pid)):
            self.assert_stop('child_unknown', lambda: helpers.bounded_child(['npm', 'publish', 'owned.tgz'], self.directory, {}, 'owned-timeout', seconds=1))
        self.assertEqual(signalled, [child.pid])
        self.assertEqual(child.calls, 2)

    def test_future_workflows_are_repository_scoped_pinned_upload_only(self):
        root = Path(__file__).resolve().parent
        import re
        if (root / 'workflows').is_dir():
            workflows = [(ecosystem, root / 'workflows' / ecosystem / 'publish.yml') for ecosystem in publisher.POLICY]
        else:
            # A public SDK export contains this suite beside its helpers and
            # exactly the selected .github/workflows/publish.yml, not templates
            # or private application source from the other SDK repositories.
            self.assertEqual(root.name, 'publishing')
            self.assertEqual(root.parent.name, '.github')
            workflow = root.parent / 'workflows' / 'publish.yml'
            repositories = re.findall(r"github\.repository == '(availkit/sdk-[a-z]+)'", workflow.read_text())
            self.assertEqual(len(repositories), 1)
            ecosystems = [name for name, policy in publisher.POLICY.items() if policy[0] == repositories[0]]
            self.assertEqual(len(ecosystems), 1)
            workflows = [(ecosystems[0], workflow)]
        for ecosystem, workflow in workflows:
            source = workflow.read_text()
            self.assertIn("github.repository == 'availkit/sdk-" + ecosystem + "'", source)
            self.assertIn('environment: registry-release', source)
            self.assertIn('types: [published]', source)
            self.assertIn('workflow_dispatch:', source)
            actions = re.findall(r'uses: ([^\n]+)', source)
            self.assertTrue(actions)
            self.assertTrue(all(re.search(r'@[0-9a-f]{40}(?:\s|$)', action) for action in actions))
            self.assertNotIn('release-gem', source)
            for command in ['cargo publish', 'cargo package', 'dotnet pack', 'npm install', 'npm ci', 'pip install', 'gem build']:
                self.assertNotIn(command, source)


if __name__ == '__main__':
    unittest.main()
