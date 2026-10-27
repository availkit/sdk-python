# Build and test the SDK source

Run from this flat SDK repository root. No private application checkout, deployment
configuration, production database or credentials are needed for these synthetic
checks. Use the supported runtime in `runtime-policy.json` with current security
updates. Run resource-intensive checks serially; do not start live tutorials as
part of tests. Linux file-helper tests require their documented OS facilities.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'build==1.3.0' 'setuptools==82.0.1' 'packaging==26.3' 'pyproject_hooks==1.3.3'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python build.py --output artifacts
```

After building, install the produced archive/package in a new consumer outside
this tree. Follow the README's explicit configuration and lifecycle commands with
owned test fixtures only. Public repo/registry activation and production acceptance
are separate gates; source copying or native compilation is not that evidence.
Do not run examples with production credentials during a source/build check.

`contracts/check_projection.py` checks only this SDK's declared wire surface
against the included public contract fixture. It fails on supported route/schema
changes, not unrelated new optional operations. Regeneration is a reviewed source
change; never use it to infer authority, retries or unimplemented SDK methods.

Package once from the reviewed version, compare file manifests/checksums, then use
those exact files for hosting and registry upload. Never rebuild in an uploader or
replace an already published version. Registry secrets are absent from builds.

The build helper normalizes wheel permissions and sdist tar/gzip timestamps,
owner/group and member ordering without changing payload bytes. The same exact
normalized files are used for hosted and registry distribution; test each in a
fresh consumer before publication. Old versioned files are never overwritten.
