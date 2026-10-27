"""Prebuilt SDK publisher. Provider activation and live acceptance are separate.

No builds, package hooks, token CLI arguments, arbitrary manifest URLs, upload
retry, tag changes, marker deletion, or private application repository access.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import time
from urllib.parse import quote, urlencode, urljoin, urlsplit

from provider_helpers import (Stop, require, sha, closed, private_file, verify_payload,
                              multipart, python_upload, cargo_upload, bounded_child,
                              nuget_signed_match, MAX_ARTIFACT)

POLICY = {
    'typescript': ('availkit/sdk-typescript', '@availkit/booking'),
    'python': ('availkit/sdk-python', 'availkit'),
    'dotnet': ('availkit/sdk-dotnet', 'AvailKit'),
    'ruby': ('availkit/sdk-ruby', 'availkit'),
    'rust': ('availkit/sdk-rust', 'availkit-sdk'),
}
GITHUB = 'https://api.github.com'
READ_HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
              'registry.npmjs.org', 'pypi.org', 'upload.pypi.org', 'files.pythonhosted.org', 'api.nuget.org', 'www.nuget.org',
              'rubygems.org', 'index.crates.io', 'crates.io', 'static.crates.io'}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def strict_json(data):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate_json_field')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=object_pairs)
    except Stop:
        raise
    except Exception:
        raise Stop('invalid_json') from None


class Wire:
    """Bounded stdlib HTTP, one request per call; explicit read redirects only."""
    def request(self, method, url, headers=None, body=None, maximum=2 * 1024 * 1024):
        parsed = urlsplit(url)
        require(parsed.scheme == 'https' and not parsed.username and not parsed.password and not parsed.fragment and parsed.port in (None, 443), 'endpoint')
        require(parsed.hostname in READ_HOSTS or parsed.hostname.endswith('.actions.githubusercontent.com'), 'endpoint')
        connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=20, context=ssl.create_default_context())
        try:
            end = time.monotonic() + 30
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            connection.request(method, target, body=body, headers={'User-Agent': 'AvailKit-prebuilt-publisher/1', **(headers or {})})
            response = connection.getresponse()
            chunks, length = [], 0
            while True:
                require(time.monotonic() < end, 'request_unknown')
                chunk = response.read(min(65536, maximum + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk); length += len(chunk)
                require(length <= maximum, 'response_bound')
            return response.status, dict((key.lower(), value) for key, value in response.getheaders()), b''.join(chunks)
        except Stop:
            raise
        except Exception:
            raise Stop('request_unknown') from None
        finally:
            connection.close()

    def json(self, method, url, value=None, headers=None, missing=False):
        data = None if value is None else json.dumps(value, separators=(',', ':')).encode()
        status, _, body = self.request(method, url, {'Accept': 'application/json', **({'Content-Type': 'application/json'} if data is not None else {}), **(headers or {})}, data)
        if status == 404 and missing:
            return None
        require(200 <= status < 300, 'http_status')
        return strict_json(body)

    def download(self, url, headers=None, maximum=MAX_ARTIFACT):
        # Authentication is never forwarded across a download redirect.
        for _ in range(4):
            status, response_headers, body = self.request('GET', url, headers, maximum=maximum)
            if status in (301, 302, 303, 307, 308):
                url = urljoin(url, response_headers.get('location', ''))
                parsed = urlsplit(url)
                require(parsed.scheme == 'https' and parsed.hostname in READ_HOSTS, 'download_redirect')
                headers = {'Accept': 'application/octet-stream'}
                continue
            require(status == 200, 'download_status')
            return body
        raise Stop('download_redirect_bound')


class GitHub:
    def __init__(self, wire, repository, token):
        require(repository in {item[0] for item in POLICY.values()}, 'repository')
        require(isinstance(token, str) and 0 < len(token) < 16384 and '\n' not in token and '\r' not in token, 'github_auth_missing')
        self.wire, self.repository = wire, repository
        self.root = GITHUB + '/repos/' + repository
        self.headers = {'Authorization': 'Bearer ' + token, 'X-GitHub-Api-Version': '2022-11-28'}

    def json(self, method, path, value=None, missing=False):
        require((path == '' or path.startswith('/')) and '://' not in path, 'github_path')
        return self.wire.json(method, self.root + path, value, self.headers, missing)

    def main(self):
        value = self.json('GET', '/git/ref/heads/main')
        require(value['object']['type'] == 'commit' and re.fullmatch('[0-9a-f]{40}', value['object']['sha']), 'main_ref')
        return value['object']['sha']

    def file(self, path, ref):
        value = self.json('GET', '/contents/' + path + '?ref=' + quote(ref, safe=''), missing=True)
        if value is None:
            return None
        require(value.get('type') == 'file' and value.get('encoding') == 'base64' and value.get('size', 65537) <= 65536, 'github_file')
        return strict_json(base64.b64decode(value['content'])), value['sha']

    def tag_commit(self, tag):
        value = self.json('GET', '/git/ref/tags/' + quote(tag, safe=''))['object']
        for _ in range(4):
            require(re.fullmatch('[0-9a-f]{40}', value.get('sha', '')), 'tag_ref')
            if value.get('type') == 'commit':
                return value['sha']
            require(value.get('type') == 'tag', 'tag_ref')
            value = self.json('GET', '/git/tags/' + value['sha'])['object']
        raise Stop('tag_depth')

    def marker(self, path):
        return self.file(path, self.main())

    def put_marker(self, path, record, existing_sha=None, *, approved_head=None):
        match = re.fullmatch(r'\.github/publishing/attempts/(typescript|python|dotnet|ruby|rust)/([0-9]+\.[0-9]+\.[0-9]+)/([A-Za-z0-9_.-]+)\.json', path)
        require(match is not None and POLICY[match[1]][0] == self.repository, 'marker_path')
        allowed = expected_files(match[1], match[2])
        allowed.pop('cargo-metadata', None)
        require(match[3] in allowed.values() and record.get('filename') == match[3], 'marker_path')
        before = self.main()
        if existing_sha is None:
            # Carry the exact commit at which admission read the approval. A
            # new read here alone would authorize an intervening changed/deleted
            # approval without checking it. Updates only settle existing intent.
            require(isinstance(approved_head, str) and re.fullmatch('[0-9a-f]{40}', approved_head), 'approval_head_required')
            require(before == approved_head, 'approval_head_changed')
            before = approved_head
        payload = {'branch': 'main', 'message': 'Record registry publication ' + record['filename'],
                   'content': base64.b64encode((json.dumps(record, sort_keys=True, indent=2) + '\n').encode()).decode()}
        if existing_sha is not None:
            payload['sha'] = existing_sha
        result = self.json('PUT', '/contents/' + path, payload)
        parents = result.get('commit', {}).get('parents', [])
        # Contents API atomically enforces create-only/file-SHA CAS. It has no
        # branch-head request precondition: a change since the approval read,
        # including after the check above, retains the marker and stops BEFORE
        # any registry upload. Never substitute a fresher unchecked head.
        require(len(parents) == 1 and parents[0].get('sha') == before, 'marker_head_changed')
        require(re.fullmatch('[0-9a-f]{40}', result.get('content', {}).get('sha', '')), 'marker_ack_unknown')
        return result['content']['sha']

    def asset(self, release, filename, expected_digest=None):
        assets = [item for item in release.get('assets', []) if item.get('name') == filename]
        require(len(assets) == 1, 'release_asset')
        asset = assets[0]
        require(isinstance(asset.get('id'), int) and asset['id'] > 0 and asset.get('size', MAX_ARTIFACT + 1) <= MAX_ARTIFACT, 'release_asset')
        data = self.wire.download(self.root + '/releases/assets/' + str(asset['id']), {**self.headers, 'Accept': 'application/octet-stream'})
        require(len(data) == asset['size'], 'release_asset_size')
        if expected_digest:
            require(sha(data) == expected_digest, 'release_asset_digest')
            require(asset.get('digest') in (None, 'sha256:' + expected_digest), 'release_asset_digest')
        return data


def expected_files(ecosystem, version):
    return {
        'typescript': {'package': 'availkit-booking-' + version + '.tgz'},
        'python': {'wheel': 'availkit-' + version + '-py3-none-any.whl', 'sdist': 'availkit-' + version + '.tar.gz'},
        'dotnet': {'package': 'AvailKit.' + version + '.nupkg'},
        'ruby': {'package': 'availkit-' + version + '.gem'},
        'rust': {'package': 'availkit-sdk-' + version + '.crate', 'cargo-metadata': 'availkit-sdk-' + version + '.publish.json'},
    }[ecosystem]


def load_release(github, ecosystem, tag, directory):
    repository, package = POLICY[ecosystem]
    require(re.fullmatch(r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', tag) is not None and len(tag) < 48, 'release_tag')
    version = tag[1:]
    metadata = github.json('GET', '')
    require(metadata.get('default_branch') == 'main' and metadata.get('private') is False and metadata.get('fork') is False, 'repository_policy')
    main = github.main()
    approval = github.file('.github/publishing/approved/' + version + '.json', main)
    require(approval is not None, 'release_not_approved')
    approval = approval[0]
    require(closed(approval, {'schema', 'ecosystem', 'package', 'version', 'tag', 'source_commit', 'manifest_sha256'}), 'approval_schema')
    require(approval['schema'] == 1 and approval['ecosystem'] == ecosystem and approval['package'] == package and approval['version'] == version and approval['tag'] == tag, 'approval_identity')
    require(re.fullmatch('[0-9a-f]{40}', approval['source_commit']) and re.fullmatch('[0-9a-f]{64}', approval['manifest_sha256']), 'approval_digest')
    require(github.tag_commit(tag) == approval['source_commit'], 'source_tag_mismatch')
    release = github.json('GET', '/releases/tags/' + tag)
    require(release.get('tag_name') == tag and not release.get('draft') and not release.get('prerelease') and release.get('published_at') and release.get('immutable') is True, 'immutable_published_release_required')
    require(len(release.get('assets', [])) <= 32, 'release_asset_bound')
    manifest_data = github.asset(release, 'release-manifest.json', approval['manifest_sha256'])
    require(len(manifest_data) < 65536, 'manifest_bound')
    manifest = strict_json(manifest_data)
    require(closed(manifest, {'schema', 'ecosystem', 'repository', 'package', 'version', 'tag', 'source_commit', 'artifacts'}), 'manifest_schema')
    require({key: manifest[key] for key in ['schema', 'ecosystem', 'package', 'version', 'tag', 'source_commit']} == {key: approval[key] for key in ['schema', 'ecosystem', 'package', 'version', 'tag', 'source_commit']} and manifest['repository'] == repository, 'manifest_identity')
    require(isinstance(manifest['artifacts'], list) and len(manifest['artifacts']) == len(expected_files(ecosystem, version)), 'artifact_allowlist')
    expected = expected_files(ecosystem, version)
    seen = set()
    for artifact in manifest['artifacts']:
        require(closed(artifact, {'role', 'filename', 'sha256', 'size'}) and artifact['role'] in expected and artifact['role'] not in seen, 'artifact_schema')
        seen.add(artifact['role'])
        require(artifact['filename'] == expected[artifact['role']] and re.fullmatch('[0-9a-f]{64}', artifact['sha256']) and type(artifact['size']) is int and 0 < artifact['size'] <= MAX_ARTIFACT, 'artifact_allowlist')
        data = github.asset(release, artifact['filename'], artifact['sha256'])
        require(len(data) == artifact['size'], 'artifact_size')
        if artifact['role'] == 'cargo-metadata':
            require(len(data) <= 131072, 'cargo_metadata_bound')
        else:
            verify_payload(ecosystem, package, version, artifact['filename'], data, repository)
        private_file(directory / artifact['filename'], data)
    if ecosystem == 'rust':
        files = {item['role']: directory / item['filename'] for item in manifest['artifacts']}
        metadata_bytes, crate_bytes = files['cargo-metadata'].read_bytes(), files['package'].read_bytes()
        strict_json(metadata_bytes)  # Reject duplicate keys before protocol framing.
        # Exercise the SAME complete framing/identity validator before prepare
        # succeeds or any later create-only attempt can be admitted. Both files
        # were size/hash checked; crate identity/source was independently read.
        cargo_upload(metadata_bytes, crate_bytes, package, version, 'https://github.com/' + repository)
    # Explicit fixed order for the finite Python two-file partial release.
    manifest['artifacts'].sort(key=lambda item: {'wheel': 0, 'package': 0, 'sdist': 1, 'cargo-metadata': 2}[item['role']])
    return {'schema': 1, 'approval': approval, 'manifest': manifest, 'release_id': release['id'],
            'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
            'directory': str(directory)}


class Registry:
    def __init__(self, wire, state):
        self.wire, self.state = wire, state
        self.manifest = state['manifest']; self.ecosystem = self.manifest['ecosystem']
        self.directory = Path(state['directory'])

    def existing(self, artifact):
        e, version = self.ecosystem, self.manifest['version']
        expected = (self.directory / artifact['filename']).read_bytes()
        require(sha(expected) == artifact['sha256'] and len(expected) == artifact['size'], 'local_artifact_changed')
        if e == 'typescript':
            metadata = self.wire.json('GET', 'https://registry.npmjs.org/' + quote('@availkit/booking', safe='') + '/' + version, missing=True)
            if metadata is None:
                return None
            require(metadata.get('name') == '@availkit/booking' and metadata.get('version') == version, 'registry_identity')
            url = 'https://registry.npmjs.org/@availkit/booking/-/booking-' + version + '.tgz'
            require(metadata.get('dist', {}).get('tarball') == url, 'registry_download')
        elif e == 'python':
            metadata = self.wire.json('GET', 'https://pypi.org/pypi/availkit/' + version + '/json', missing=True)
            if metadata is None:
                return None
            urls = metadata.get('urls', [])
            allowed = set(expected_files(e, version).values())
            require(len(urls) <= 2 and all(item.get('filename') in allowed for item in urls), 'registry_artifact_allowlist')
            rows = [item for item in urls if item['filename'] == artifact['filename']]
            require(len(rows) <= 1, 'registry_duplicate')
            if not rows:
                return None
            require(rows[0].get('digests', {}).get('sha256') == artifact['sha256'], 'registry_digest_mismatch')
            url = rows[0]['url']; parsed = urlsplit(url)
            require(parsed.scheme == 'https' and parsed.hostname == 'files.pythonhosted.org' and parsed.path.startswith('/packages/'), 'registry_download')
        elif e == 'dotnet':
            url = 'https://api.nuget.org/v3-flatcontainer/availkit/' + version + '/availkit.' + version + '.nupkg'
            status, _, data = self.wire.request('GET', url, maximum=MAX_ARTIFACT)
            if status == 404:
                return None
            require(status == 200, 'registry_read_unknown')
            # Repository signing is the sole permitted provider transformation.
            return nuget_signed_match(self.wire, expected, data, self.directory)
        elif e == 'ruby':
            metadata = self.wire.json('GET', 'https://rubygems.org/api/v2/rubygems/availkit/versions/' + version + '.json', missing=True)
            if metadata is None:
                return None
            require(metadata.get('name') == 'availkit' and metadata.get('version') == version and metadata.get('sha') == artifact['sha256'], 'registry_digest_mismatch')
            url = 'https://rubygems.org/downloads/availkit-' + version + '.gem'
        else:
            metadata = self.wire.json('GET', 'https://crates.io/api/v1/crates/availkit-sdk/' + version, missing=True)
            if metadata is None:
                return None
            require(metadata.get('version', {}).get('num') == version and metadata.get('version', {}).get('checksum') == artifact['sha256'], 'registry_digest_mismatch')
            url = 'https://static.crates.io/crates/availkit-sdk/availkit-sdk-' + version + '.crate'
        data = self.wire.download(url)
        require(sha(data) == artifact['sha256'] and len(data) == artifact['size'], 'registry_digest_mismatch')
        return {'method': 'exact_archive_bytes', 'upload_sha256': artifact['sha256'], 'download_sha256': sha(data)}

    def token(self):
        if self.ecosystem == 'python':
            request_url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL', '')
            parsed = urlsplit(request_url)
            require(parsed.scheme == 'https' and parsed.hostname and parsed.hostname.endswith('.actions.githubusercontent.com'), 'oidc_endpoint')
            oidc = self.wire.json('GET', request_url + ('&' if parsed.query else '?') + urlencode({'audience': 'pypi'}), headers={'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
            response = self.wire.json('POST', 'https://pypi.org/_/oidc/mint-token', {'token': oidc['value']})
            token = response.get('token')
        elif self.ecosystem == 'ruby':
            token = os.environ.get('GEM_HOST_API_KEY')
        else:
            token = os.environ.get('REGISTRY_TOKEN')
        require(isinstance(token, str) and 0 < len(token) <= 16384 and '\n' not in token and '\r' not in token, 'registry_auth_missing')
        return token

    def upload(self, artifact, token):
        e = self.ecosystem
        data = (self.directory / artifact['filename']).read_bytes()
        require(sha(data) == artifact['sha256'] and len(data) == artifact['size'], 'local_artifact_changed')
        if e == 'typescript':
            configuration = self.directory / 'npmrc'
            private_file(configuration, b'registry=https://registry.npmjs.org/\nignore-scripts=true\nfetch-retries=0\nprovenance=false\n')
            empty_global = self.directory / 'npm-globalrc'; private_file(empty_global, b'')
            env = {key: value for key, value in os.environ.items() if not key.lower().startswith('npm_config_') and key not in {'NODE_AUTH_TOKEN', 'NPM_TOKEN', 'NODE_OPTIONS', 'NODE_PATH', 'REGISTRY_TOKEN', 'GITHUB_TOKEN', 'GEM_HOST_API_KEY'}}
            env.update({'NPM_CONFIG_USERCONFIG': str(configuration), 'NPM_CONFIG_GLOBALCONFIG': str(empty_global), 'NPM_CONFIG_FETCH_RETRIES': '0', 'NPM_CONFIG_IGNORE_SCRIPTS': 'true', 'NPM_CONFIG_PROVENANCE': 'false', 'NPM_CONFIG_CACHE': str(self.directory / 'npm-cache')})
            result, _, _ = bounded_child(['npm', 'publish', str(self.directory / artifact['filename']), '--ignore-scripts', '--provenance=false', '--access=public', '--registry=https://registry.npmjs.org/'], self.directory, env, 'npm-publish')
            require(result == 0, 'upload_unknown')
            return
        if e == 'python':
            body, kind = python_upload(data, artifact['filename'], self.manifest['version'])
            url, method = 'https://upload.pypi.org/legacy/', 'POST'
            auth = {'Authorization': 'Basic ' + base64.b64encode(('__token__:' + token).encode()).decode()}
        elif e == 'dotnet':
            body, kind = multipart([], artifact['filename'], data)
            url, method, auth = 'https://www.nuget.org/api/v2/package', 'PUT', {'X-NuGet-ApiKey': token}
        elif e == 'ruby':
            body, kind = data, 'application/octet-stream'
            url, method, auth = 'https://rubygems.org/api/v1/gems', 'POST', {'Authorization': token}
        else:
            metadata = next(item for item in self.manifest['artifacts'] if item['role'] == 'cargo-metadata')
            metadata_bytes = (self.directory / metadata['filename']).read_bytes()
            require(sha(metadata_bytes) == metadata['sha256'], 'local_artifact_changed')
            body = cargo_upload(metadata_bytes, data, 'availkit-sdk', self.manifest['version'], 'https://github.com/availkit/sdk-rust')
            kind, url, method, auth = 'application/octet-stream', 'https://crates.io/api/v1/crates/new', 'PUT', {'Authorization': token}
        status, _, reply = self.wire.request(method, url, {'Content-Type': kind, 'Accept': 'application/json', **auth}, body)
        require(200 <= status < 300, 'upload_unknown')
        if e == 'rust':
            require('errors' not in strict_json(reply), 'upload_unknown')


def marker_path(state, artifact):
    manifest = state['manifest']
    return '.github/publishing/attempts/' + manifest['ecosystem'] + '/' + manifest['version'] + '/' + artifact['filename'] + '.json'


def binding(state, artifact):
    manifest = state['manifest']
    return {'schema': 1, 'repository': manifest['repository'], 'ecosystem': manifest['ecosystem'], 'package': manifest['package'],
            'version': manifest['version'], 'tag': manifest['tag'], 'source_commit': manifest['source_commit'],
            'manifest_sha256': state['approval']['manifest_sha256'], 'release_id': state['release_id'],
            'filename': artifact['filename'], 'artifact_sha256': artifact['sha256']}


def reconcile(github, registry, state, artifact):
    path = marker_path(state, artifact)
    existing = github.marker(path)
    proof = registry.existing(artifact)
    if existing is None:
        return proof is not None
    record, current_sha = existing
    fields = set(binding(state, artifact)) | {'run_id', 'run_attempt', 'created_at', 'state', 'verification'}
    require(closed(record, fields) and all(record[key] == value for key, value in binding(state, artifact).items()), 'marker_mismatch')
    require(record['state'] in ('attempted', 'confirmed'), 'marker_state')
    require(proof is not None, 'attempt_unknown_stop_no_upload')
    if record['state'] != 'confirmed' or record['verification'] != proof:
        record.update(state='confirmed', verification=proof)
        github.put_marker(path, record, current_sha)
    return True


def publish(github, registry, state):
    token = None
    auth_ready = False
    packages = [item for item in state['manifest']['artifacts'] if item['role'] != 'cargo-metadata']
    for artifact in packages:
        if reconcile(github, registry, state, artifact):
            continue
        if not auth_ready:
            if registry.ecosystem == 'typescript':
                result, stdout, _ = bounded_child(['npm', '--version'], Path(state['directory']), dict(os.environ), 'npm-version', seconds=15)
                match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)\s*', stdout.read_text())
                require(result == 0 and match and tuple(map(int, match.groups())) >= (11, 5, 1), 'npm_oidc_version')
            else:
                token = registry.token()  # Auth only; no package attempt yet.
            auth_ready = True
        # Recheck current approval and source immediately before durable admission.
        approved_head = github.main()
        approval = github.file('.github/publishing/approved/' + state['manifest']['version'] + '.json', approved_head)
        require(approval is not None and approval[0] == state['approval'], 'approval_changed')
        require(github.tag_commit(state['manifest']['tag']) == state['manifest']['source_commit'], 'source_tag_mismatch')
        require(github.marker(marker_path(state, artifact)) is None, 'attempt_exists_stop')
        record = {**binding(state, artifact), 'run_id': state['run_id'], 'run_attempt': state['run_attempt'],
                  'created_at': timestamp(), 'state': 'attempted', 'verification': None}
        current_sha = github.put_marker(marker_path(state, artifact), record, approved_head=approved_head)  # Create-only admission at the checked approval head.
        registry.upload(artifact, token)  # One explicit upload. Any failure stops the second Python file too.
        proof = registry.existing(artifact)
        require(proof is not None, 'upload_unknown_stop_reconcile_later')
        record.update(state='confirmed', verification=proof)
        github.put_marker(marker_path(state, artifact), record, current_sha)
    return len(packages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'publish'])
    parser.add_argument('--ecosystem', choices=POLICY, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'hosted_oidc_workflow_required')
        require(os.environ.get('GITHUB_REPOSITORY') == POLICY[args.ecosystem][0], 'repository')
        require(os.environ.get('GITHUB_EVENT_NAME') in ('release', 'workflow_dispatch'), 'event')
        require(os.environ.get('GITHUB_WORKFLOW_REF', '').startswith(POLICY[args.ecosystem][0] + '/.github/workflows/publish.yml@'), 'workflow_identity')
        if os.environ['GITHUB_EVENT_NAME'] == 'release':
            require(os.environ.get('GITHUB_REF') == 'refs/tags/' + os.environ.get('PUBLISH_TAG', ''), 'event_tag')
        if os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch':
            require(os.environ.get('GITHUB_REF') == 'refs/heads/main', 'dispatch_main_only')
        require(re.fullmatch('[0-9]+', os.environ.get('GITHUB_RUN_ID', '')) and re.fullmatch('[0-9]+', os.environ.get('GITHUB_RUN_ATTEMPT', '')), 'run_identity')
        github = GitHub(Wire(), POLICY[args.ecosystem][0], os.environ.get('GITHUB_TOKEN'))
        directory = args.directory.absolute()
        temporary = Path(os.environ['RUNNER_TEMP']).resolve()
        require(directory.is_relative_to(temporary) and directory != temporary and directory.resolve() == directory, 'private_directory')
        if args.action == 'prepare':
            directory.mkdir(mode=0o700, exist_ok=False)
            state = load_release(github, args.ecosystem, os.environ.get('PUBLISH_TAG', ''), directory)
            registry = Registry(github.wire, state)
            needed = []
            for artifact in state['manifest']['artifacts']:
                if artifact['role'] != 'cargo-metadata':
                    needed.append(not reconcile(github, registry, state, artifact))
            private_file(directory / 'state.json', (json.dumps(state) + '\n').encode())
            require(os.environ.get('GITHUB_OUTPUT'), 'workflow_output')
            with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
                stream.write('upload_required=' + str(any(needed)).lower() + '\n')
            print(json.dumps({'state': 'verified_prebuilt_inputs', 'upload_required': any(needed), 'files': len(needed)}))
        else:
            info = directory.stat()
            require(stat.S_ISDIR(info.st_mode) and info.st_mode & 0o077 == 0 and info.st_uid == os.getuid(), 'private_directory')
            state = strict_json((directory / 'state.json').read_bytes())
            require(state['run_id'] == os.environ['GITHUB_RUN_ID'] and state['run_attempt'] == os.environ['GITHUB_RUN_ATTEMPT'] and state['manifest']['ecosystem'] == args.ecosystem, 'run_identity')
            count = publish(github, Registry(github.wire, state), state)
            print(json.dumps({'state': 'registry_verified', 'files': count}))
        return 0
    except Stop as error:
        print(json.dumps({'state': 'stopped', 'code': error.code}))
        return 1
    except BaseException:
        print(json.dumps({'state': 'stopped', 'code': 'unexpected_stop_preserve_marker'}))
        return 1


if __name__ == '__main__':
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Stop('process_deadline')))
    signal.alarm(180)
    raise SystemExit(main())
