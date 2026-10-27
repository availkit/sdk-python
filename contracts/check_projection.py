#!/usr/bin/env python3
"""Check this SDK's supported contract projection; no network or API calls."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def encoded(v):return (json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n").encode()
def expand(v,doc,trail=()):
 if isinstance(v,dict):
  if '$ref'in v:
   ref=v['$ref'];assert ref.startswith('#/') and ref not in trail and len(trail)<64
   target=doc
   for part in ref[2:].split('/'):target=target[part.replace('~1','/').replace('~0','~')]
   return expand(target,doc,(*trail,ref))|{k:expand(c,doc,trail)for k,c in v.items()if k!='$ref'}
  return {k:expand(c,doc,trail)for k,c in v.items()}
 if isinstance(v,list):return [expand(c,doc,trail)for c in v]
 return v
doc=json.loads((ROOT/'contracts/api.openapi.json').read_text());meta=json.loads((ROOT/'contract-projection.json').read_text());ops={op['operationId']:(path,method.upper(),op)for path,ms in doc['paths'].items()for method,op in ms.items()if isinstance(op,dict)and 'operationId'in op}
projected={}
for name in meta['operationIds']:
 path,method,op=ops[name];projected[name]={'method':method,'path':path,'security':op.get('security',doc.get('security',[])),**{k:expand(op[k],doc)for k in ('parameters','requestBody','responses','deprecated')if k in op}}
used={name for route in projected.values()for requirement in route['security']for name in requirement}
schemes=doc.get('components',{}).get('securitySchemes',{})
value={'operations':projected,'securitySchemes':{name:schemes[name]for name in sorted(used)}}
assert hashlib.sha256(encoded(value)).hexdigest()==meta['supportedProjectionSha256'],'supported contract drift'
print(json.dumps({'status':'pass','supportedOperations':len(projected),'sdkVersion':meta['sdkVersion']}))
