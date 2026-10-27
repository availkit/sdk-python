"""Refresh supported schema/types/operation coverage from the repository OpenAPI."""
from pathlib import Path
import json,re,hashlib
root=Path(__file__).resolve().parents[3]
package=Path(__file__).resolve().parents[1]
contract=package/'contracts/api.openapi.json'
if not contract.is_file():contract=root/'docs/contracts/api.openapi.json'
p=json.loads(contract.read_text())
supported=set(json.loads((package/'src/availkit/operations.json').read_text()))
schemas={}
operations={}
coverage={}
method_names={'setHours':'replace_hours','updateResource':'replace_resource','updateOffering':'replace_offering','book':'book','cancel':'cancel','rescheduleBooking':'reschedule_booking','bookPublic':'book','getPublicPage':'get_page','listPublicOfferings':'list_offerings','publicAvailability':'availability','getPublicBooking':'get_booking','cancelPublicBooking':'cancel','reschedulePublicBooking':'reschedule'}
def snake(s):return re.sub(r'(?<!^)(?=[A-Z])','_',s).lower()
def add_schema(schema):
 if isinstance(schema,dict):
  if '$ref' in schema:
   n=schema['$ref'].rsplit('/',1)[-1]
   if n not in schemas:
    schemas[n]=p['components']['schemas'][n]
    add_schema(schemas[n])
  for v in schema.values():add_schema(v)
 elif isinstance(schema,list):
  for v in schema:add_schema(v)
for path, methods in p['paths'].items():
 for method, op in methods.items():
  if method not in ('get','post','put'):continue
  name=op['operationId']
  if name not in supported:
   coverage[name]={'status':'browser-account-session-only' if path.startswith('/v1/human') else 'operator-http','method':method.upper(),'path':path}
   if path.startswith('/v2/human/recovery/'):coverage[name]['status']='unsupported-browser-recovery-v2'
   elif name=='getBusinessExportPage':coverage[name]['status']='unsupported-optional-business-export'
   elif path.startswith('/v1/human/recovery/'):coverage[name]['status']='disabled-legacy-v1'
   continue
  req=op.get('requestBody',{}).get('content',{}).get('application/json',{}).get('schema')
  status,res=next((int(code),v) for code,v in op['responses'].items() if code.startswith('2'))
  out=res['content']['application/json']['schema']
  operations[name]={'method':method.upper(),'path':path,'input':req,'status':status,'output':out}
  add_schema(req);add_schema(out)
  coverage[name]={'status':'supported','client':'PublicBookingClient' if path.startswith('/v1/public') else 'AvailKitClient','method':method.upper(),'path':path,'sdk':method_names.get(name,snake(name)),'replay':'explicit-original-intent-only' if name in ['book','cancel','rescheduleBooking','bookPublic','cancelPublicBooking','reschedulePublicBooking'] else 'none'}
(package/'src/availkit/schemas.json').write_text(json.dumps(schemas,indent=2)+'\n')
(package/'src/availkit/operations.json').write_text(json.dumps(operations,indent=2)+'\n')
(package/'coverage.json').write_text(json.dumps({'version':'0.2.0','openapi_sha256':hashlib.sha256(contract.read_bytes()).hexdigest(),'workspace_operations':34,'public_operations':7,'operations':coverage},indent=2)+'\n')
(package/'src/availkit/coverage.json').write_bytes((package/'coverage.json').read_bytes())
# Generate static TypedDict definitions, including named nested objects.
classes={}
def type_of(s,context):
 if '$ref' in s:return s['$ref'].rsplit('/',1)[-1]
 if 'const' in s:return 'Literal['+repr(s['const'])+']'
 if 'enum' in s:return 'Literal['+', '.join(repr(x) for x in s['enum'] if x is not None)+']'+(' | None' if None in s['enum'] else '')
 if 'oneOf' in s or 'anyOf' in s:return ' | '.join(type_of(x,context+str(i)) for i,x in enumerate(s.get('oneOf',s.get('anyOf'))))
 t=s.get('type')
 if isinstance(t,list):return ' | '.join(type_of(dict(s,type=x),context) for x in t)
 if t=='object':
  classes[context]=s
  return context
 if t=='array':return 'list['+type_of(s['items'],context+'Item')+']'
 return {'string':'str','integer':'int','number':'float','boolean':'bool','null':'None'}.get(t,'Any')
for n,s in schemas.items():
 if s.get('type')=='object':classes[n]=s
output=['"""Typed API shapes generated from the supported OpenAPI schemas.','Wire field names intentionally retain their canonical camelCase spelling.','Regenerate with tools/generate_contract.py from a repository checkout.','"""','from __future__ import annotations','','from typing import Any, Literal, NotRequired, TypedDict','']
processed=set()
while len(processed)<len(classes):
 for n,s in list(classes.items()):
  if n in processed:continue
  processed.add(n)
  output.append('class '+n+'(TypedDict):')
  props=s.get('properties',{})
  if not props:output.append('    pass')
  for k,v in props.items():
   annotation=type_of(v,n+k[:1].upper()+k[1:])
   if k not in s.get('required',[]):annotation='NotRequired['+annotation+']'
   output.append('    '+k+': '+annotation)
  output.extend(['',''])
(package/'src/availkit/models.py').write_text('\n'.join(output).rstrip()+'\n')
