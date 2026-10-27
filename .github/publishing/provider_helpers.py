"""Registry protocols for already reviewed bytes. No build/repack/install hooks.

All network calls are supplied by the finite publisher transport. Registry token
values stay in headers or memory, never command arguments, state or diagnostics.
"""
import base64
from email.parser import Parser
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import signal
import struct
import subprocess
import tarfile
import time
import zipfile
import xml.etree.ElementTree as ET

MAX_ARTIFACT = 16 * 1024 * 1024
MAX_UNPACKED = 64 * 1024 * 1024


class Stop(Exception):
    """Fixed public-safe codes only; do not preserve raw transport/CLI errors."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition, code):
    if not condition:
        raise Stop(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def closed(value, fields):
    return isinstance(value, dict) and set(value) == set(fields)


def private_file(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def zip_entries(data):
    require(len(data) <= MAX_ARTIFACT, 'archive_bound')
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        require(len(entries) <= 512, 'archive_bound')
        require(sum(item.file_size for item in entries) <= MAX_UNPACKED, 'archive_bound')
        for item in entries:
            require(not item.filename.startswith('/') and '..' not in item.filename.split('/') and '\\' not in item.filename and item.filename not in result, 'archive_path')
            require((item.external_attr >> 16) & 0o170000 != 0o120000, 'archive_symlink')
            result[item.filename] = archive.read(item)
    return result


def tar_entries(data):
    require(len(data) <= MAX_ARTIFACT, 'archive_bound')
    result = {}
    total = 0
    if data.startswith(b'\x1f\x8b'):
        import gzip
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            data = compressed.read(MAX_UNPACKED + 1)
        require(len(data) <= MAX_UNPACKED, 'archive_bound')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for index, item in enumerate(archive):
            require(index < 512 and not item.name.startswith('/') and '..' not in item.name.split('/') and '\\' not in item.name, 'archive_path')
            if item.isdir():
                continue
            require(item.isfile() and item.name not in result, 'archive_entry')
            total += item.size
            require(total <= MAX_UNPACKED, 'archive_bound')
            result[item.name] = archive.extractfile(item).read()
    return result


def python_metadata(data, filename, version):
    if filename.endswith('.whl'):
        entries = zip_entries(data)
        names = [key for key in entries if key.endswith('.dist-info/METADATA')]
    else:
        entries = tar_entries(data)
        names = [key for key in entries if key == 'availkit-' + version + '/PKG-INFO']
    require(len(names) == 1, 'python_metadata')
    metadata = Parser().parsestr(entries[names[0]].decode('utf-8'))
    require(metadata['Name'] == 'availkit' and metadata['Version'] == version and metadata['Metadata-Version'] in ('2.4', '2.5', '2.6'), 'package_identity')
    return metadata


def verify_payload(ecosystem, package, version, filename, data, repo):
    require(0 < len(data) <= MAX_ARTIFACT, 'artifact_bound')
    source_url = 'https://github.com/' + repo
    if ecosystem == 'typescript':
        entries = tar_entries(data)
        require('package/package.json' in entries, 'package_identity')
        metadata = json.loads(entries['package/package.json'])
        require(metadata.get('name') == package and metadata.get('version') == version, 'package_identity')
        repository = metadata.get('repository', {})
        require(isinstance(repository, dict) and repository.get('url') == source_url + '.git', 'package_source')
    elif ecosystem == 'python':
        metadata = python_metadata(data, filename, version)
        require('Repository, ' + source_url in metadata.get_all('Project-URL', []), 'package_source')
        python_upload(data, filename, version)  # Validate complete upload fields before any attempt marker.
    elif ecosystem == 'dotnet':
        entries = zip_entries(data)
        require('.signature.p7s' not in entries, 'unsigned_nuget_input_required')
        names = [key for key in entries if key.endswith('.nuspec')]
        require(len(names) == 1 and len(entries[names[0]]) < 131072, 'package_identity')
        document = ET.fromstring(entries[names[0]])
        values = {item.tag.rsplit('}', 1)[-1]: item.text for item in document.iter()}
        repositories = [item for item in document.iter() if item.tag.rsplit('}', 1)[-1] == 'repository']
        require(values.get('id') == package and values.get('version') == version, 'package_identity')
        require(len(repositories) == 1 and repositories[0].get('url') == source_url, 'package_source')
    elif ecosystem == 'ruby':
        import gzip
        entries = tar_entries(data)
        require('metadata.gz' in entries and len(entries['metadata.gz']) <= 131072, 'package_identity')
        # Parse only these finite scalar lines; never load Ruby/YAML objects.
        with gzip.GzipFile(fileobj=io.BytesIO(entries['metadata.gz'])) as compressed:
            metadata = compressed.read(131073)
        require(len(metadata) <= 131072, 'archive_bound')
        text = metadata.decode()
        require(re.search(r'^name: ' + re.escape(package) + r'\s*$', text, re.M) is not None, 'package_identity')
        require(re.search(r'^  version: [\'"]?' + re.escape(version) + r'[\'"]?\s*$', text, re.M) is not None, 'package_identity')
        require(source_url in text, 'package_source')
    elif ecosystem == 'rust':
        import tomllib
        entries = tar_entries(data)
        prefix = package + '-' + version + '/'
        require(prefix + 'Cargo.toml' in entries, 'package_identity')
        metadata = tomllib.loads(entries[prefix + 'Cargo.toml'].decode())['package']
        require(metadata.get('name') == package and metadata.get('version') == version and metadata.get('repository') == source_url, 'package_identity')
    else:
        raise Stop('ecosystem')


def multipart(fields, filename, data):
    boundary = 'availkit-' + secrets.token_hex(24)
    parts = []
    for name, value in fields:
        require(re.fullmatch('[a-z0-9_:]+', name) is not None, 'multipart_field')
        parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"\r\n\r\n' + value + '\r\n').encode())
    require(re.fullmatch('[A-Za-z0-9_.-]+', filename) is not None, 'artifact_name')
    parts.extend([('--' + boundary + '\r\nContent-Disposition: form-data; name="content"; filename="' + filename + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode(), data, ('\r\n--' + boundary + '--\r\n').encode()])
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


def python_upload(data, filename, version):
    metadata = python_metadata(data, filename, version)
    fields = [(':action', 'file_upload'), ('protocol_version', '1'), ('filetype', 'bdist_wheel' if filename.endswith('.whl') else 'sdist'), ('pyversion', 'py3' if filename.endswith('.whl') else 'source'), ('sha256_digest', sha(data))]
    mapping = {'Metadata-Version': 'metadata_version', 'Name': 'name', 'Version': 'version', 'Summary': 'summary', 'Home-page': 'home_page', 'Author': 'author', 'Author-email': 'author_email', 'License': 'license', 'License-Expression': 'license_expression', 'License-File': 'license_file', 'Requires-Python': 'requires_python', 'Description-Content-Type': 'description_content_type', 'Classifier': 'classifiers', 'Project-URL': 'project_urls', 'Requires-Dist': 'requires_dist', 'Provides-Extra': 'provides_extra', 'Platform': 'platform', 'Keywords': 'keywords', 'Dynamic': 'dynamic', 'Maintainer': 'maintainer', 'Maintainer-email': 'maintainer_email', 'Import-Name': 'import_names', 'Import-Namespace': 'import_namespaces'}
    for header in metadata.keys():
        require(header in mapping, 'python_metadata_field')
    for header, field in mapping.items():
        fields.extend((field, value) for value in metadata.get_all(header, []))
    description = metadata.get_payload()
    require(isinstance(description, str), 'python_metadata')
    fields.append(('description', description))
    return multipart(fields, filename, data)


def cargo_upload(metadata_bytes, crate, name, version, source_url):
    metadata = json.loads(metadata_bytes)
    allowed = {'name', 'vers', 'deps', 'features', 'authors', 'description', 'documentation', 'homepage', 'readme', 'readme_file', 'keywords', 'categories', 'license', 'license_file', 'repository', 'badges', 'links', 'rust_version'}
    require(isinstance(metadata, dict) and not set(metadata) - allowed and {'name', 'vers', 'deps', 'features'}.issubset(metadata), 'cargo_metadata')
    require(metadata['name'] == name and metadata['vers'] == version and metadata.get('repository') == source_url, 'package_identity')
    require(len(metadata_bytes) <= 131072 and len(crate) <= MAX_ARTIFACT, 'artifact_bound')
    # Documented Cargo registry framing; the .crate bytes are never repackaged.
    return struct.pack('<I', len(metadata_bytes)) + metadata_bytes + struct.pack('<I', len(crate)) + crate


def bounded_child(command, directory, environment, name, seconds=90):
    """Only npm or read-only dotnet verification, no shell, scripts or builds."""
    stdout = directory / (name + '-stdout-private.log')
    stderr = directory / (name + '-stderr-private.log')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(stdout, flags, 0o600), 'wb') as out, os.fdopen(os.open(stderr, flags, 0o600), 'wb') as err:
        child = subprocess.Popen(command, cwd=directory, env=environment, stdout=out, stderr=err, start_new_session=True)
        try:
            result = child.wait(timeout=seconds)
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            raise Stop('child_unknown') from None
    require(stdout.stat().st_size < 2 * 1024 * 1024 and stderr.stat().st_size < 2 * 1024 * 1024, 'child_output_bound')
    return result, stdout, stderr


def nuget_signed_match(wire, approved, downloaded, directory):
    before, after = zip_entries(approved), zip_entries(downloaded)
    require('.signature.p7s' not in before and set(after) == set(before) | {'.signature.p7s'}, 'nuget_content_mismatch')
    require(all(after[name] == data for name, data in before.items()), 'nuget_content_mismatch')
    signatures = wire.json('GET', 'https://api.nuget.org/v3-index/repository-signatures/5.0.0/index.json')
    require(signatures.get('allRepositorySigned') is True, 'nuget_signer_policy')
    fingerprints = [item.get('fingerprints', {}).get('2.16.840.1.101.3.4.2.1') for item in signatures.get('signingCertificates', [])]
    require(0 < len(fingerprints) <= 16 and all(isinstance(fp, str) and re.fullmatch('[0-9a-f]{64}', fp) for fp in fingerprints), 'nuget_signer_policy')
    target = directory / ('nuget-registry-' + sha(downloaded) + '.nupkg')
    if target.exists():
        require(target.is_file() and not target.is_symlink() and target.stat().st_mode & 0o077 == 0 and target.read_bytes() == downloaded, 'private_registry_evidence')
    else:
        private_file(target, downloaded)
    command = ['dotnet', 'nuget', 'verify', '--all', str(target)]
    for fingerprint in fingerprints:
        command.extend(['--certificate-fingerprint', fingerprint])
    environment = {key: value for key, value in os.environ.items() if key not in {'REGISTRY_TOKEN', 'GITHUB_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN', 'GEM_HOST_API_KEY'}}
    environment['DOTNET_CLI_UI_LANGUAGE'] = 'en-US'
    result, out, err = bounded_child(command, directory, environment, 'nuget-verify-' + sha(downloaded)[:12] + '-' + secrets.token_hex(4))
    require(result == 0, 'nuget_signature_failed')
    output = out.read_text() + err.read_text()
    require('Signature type: Repository' in output, 'nuget_repository_signature_required')
    require('nuget-v3-service-index-url: https://api.nuget.org/v3/index.json' in output, 'nuget_repository_signature_required')
    repository = output.split('Signature type: Repository', 1)[1]
    require(any(fp.upper() in repository.upper() for fp in fingerprints), 'nuget_unannounced_signer')
    return {'method': 'nuget_repository_signature_and_exact_non_signature_entries', 'upload_sha256': sha(approved), 'download_sha256': sha(downloaded)}
