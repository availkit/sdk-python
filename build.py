#!/usr/bin/env python3
"""Check exact SDK source inputs before conventional wheel/sdist build."""
import argparse,copy,gzip,io,os,shutil,stat,subprocess,sys,tarfile,tempfile,zipfile
from pathlib import PurePosixPath
from pathlib import Path
PACKAGE_FILES = ('CHANGELOG.md', 'DEVELOPMENT.md', 'LICENSE', 'MANIFEST.in', 'README.md', 'SECURITY.md', 'SUPPORT.md', 'VERSION', 'build.py', 'contract-projection.json', 'contracts/api.openapi.json', 'contracts/check_projection.py', 'coverage.json', 'examples/README.md', 'examples/durable_booking.py', 'examples/first_booking.py', 'examples/public_booking.py', 'examples/webhook_receiver.py', 'pyproject.toml', 'runtime-policy.json', 'src/availkit/__init__.py', 'src/availkit/_transport.py', 'src/availkit/_validation.py', 'src/availkit/client.py', 'src/availkit/coverage.json', 'src/availkit/durable.py', 'src/availkit/errors.py', 'src/availkit/examples/README.md', 'src/availkit/examples/__init__.py', 'src/availkit/examples/durable_booking.py', 'src/availkit/examples/first_booking.py', 'src/availkit/examples/public_booking.py', 'src/availkit/examples/webhook_receiver.py', 'src/availkit/intents.py', 'src/availkit/models.py', 'src/availkit/operations.json', 'src/availkit/py.typed', 'src/availkit/schemas.json', 'src/availkit/webhooks.py', 'tests/test_durable.py', 'tests/test_examples.py', 'tests/test_sdk.py', 'tools/generate_contract.py')

def check(root):
 for name in PACKAGE_FILES:
  current=root
  for part in Path(name).parts:
   current/=part;info=current.lstat()
   if stat.S_ISLNK(info.st_mode):raise ValueError('symlink_package_input')
  if not stat.S_ISREG(info.st_mode):raise ValueError('nonregular_package_input')
 for current in (root/'src').rglob('*'):
  relative=current.relative_to(root)
  if '__pycache__' in relative.parts:continue
  if current.is_symlink():raise ValueError('symlink_package_input')
  if current.is_file() and relative.as_posix() not in PACKAGE_FILES:raise ValueError('unexpected_python_package_input')

def normalize(archive):
 # Normalize transport metadata only; package file payloads remain unchanged.
 if archive.suffix=='.whl':
  with zipfile.ZipFile(archive) as source:
   entries=source.infolist()
   if len({entry.filename for entry in entries})!=len(entries):raise ValueError('duplicate_wheel_member')
   output=io.BytesIO()
   with zipfile.ZipFile(output,'w') as target:
    target.comment=source.comment
    for entry in entries:
     name=PurePosixPath(entry.filename)
     if entry.is_dir() or name.is_absolute() or '..' in name.parts or stat.S_ISLNK(entry.external_attr>>16):raise ValueError('invalid_wheel_member')
     info=copy.copy(entry);info.external_attr=(0o100664 if info.filename.endswith('.dist-info/RECORD') else 0o100644)<<16
     target.writestr(info,source.read(entry.filename))
  archive.write_bytes(output.getvalue())
 elif archive.name.endswith('.tar.gz'):
  with tarfile.open(archive,'r:gz') as source:
   members=source.getmembers();names=set()
   if len(members)>1024:raise ValueError('too_many_sdist_members')
   output=io.BytesIO()
   with gzip.GzipFile(fileobj=output,mode='wb',filename='',mtime=0) as compressed:
    with tarfile.open(fileobj=compressed,mode='w',format=tarfile.PAX_FORMAT) as target:
     for member in sorted(members,key=lambda value:value.name):
      name=PurePosixPath(member.name)
      if name.is_absolute() or '..' in name.parts or member.name in names or member.issym() or member.islnk() or not(member.isfile()or member.isdir()) or member.size>4*1024*1024:raise ValueError('invalid_sdist_member')
      names.add(member.name);info=copy.copy(member)
      info.uid=info.gid=0;info.uname=info.gname='';info.mtime=1793059200;info.mode=0o755 if info.isdir() else 0o644;info.pax_headers={}
      target.addfile(info,source.extractfile(member) if member.isfile() else None)
  archive.write_bytes(output.getvalue())

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');parser.add_argument('--output',type=Path);args=parser.parse_args()
 root=Path(__file__).resolve().parent;check(root)
 if args.output is not None:
  args.output.mkdir(parents=True,exist_ok=True)
  # Build only the checked literal inputs in disposable staging: setuptools may
  # create egg-info/build products, which must never become future source inputs.
  with tempfile.TemporaryDirectory(prefix='availkit-python-build-') as directory:
   source=Path(directory)/'source';source.mkdir()
   for name in PACKAGE_FILES:
    target=source/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/name,target)
   subprocess.run([sys.executable,'-m','build','--no-isolation','--outdir',str(args.output.resolve()),str(source)],check=True,cwd=args.output.resolve(),env=dict(os.environ,SOURCE_DATE_EPOCH='1793059200'))
  for archive in args.output.iterdir():
   if archive.name.startswith('availkit-0.2.0') and (archive.suffix=='.whl' or archive.name.endswith('.tar.gz')):normalize(archive)
 print('Checked exact Python SDK source inputs')
