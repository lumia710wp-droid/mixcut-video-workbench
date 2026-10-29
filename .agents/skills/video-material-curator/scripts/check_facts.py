#!/usr/bin/env python3
"""Validate factual annotations; does not perform visual or human approval."""
import argparse,hashlib,json,math
from pathlib import Path
FORBIDDEN={'recommendation','usage','priority','role','speed','speed_range','transition','crop','source_audio_gain','recommended_in','recommended_out','editing_guidance'}
def digest(x):return hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def file_hash(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def validate(row,verify_files=True):
 errors=[]
 def check_tree(x,path=''):
  if isinstance(x,dict):
   for k,v in x.items():
    if k in FORBIDDEN:errors.append(f'editorial field {path}/{k}')
    check_tree(v,path+'/'+k)
  elif isinstance(x,list):
   for i,v in enumerate(x):check_tree(v,path+'/'+str(i))
 check_tree(row)
 if row.get('schema_version')!='material_facts_v1':errors.append('schema_version')
 if row.get('production_approved') is not False:errors.append('new facts must not imply production approval')
 source=row['source'];duration=source['duration_sec'];phases=row.get('phases',[])
 if not isinstance(duration,(float,int)) or not math.isfinite(duration) or duration<=0:errors.append('duration')
 end=0
 for i,p in enumerate(phases):
  a,b=p['start_sec'],p['end_sec']
  if not all(isinstance(n,(int,float)) and math.isfinite(n) for n in [a,b]) or not 0<=a<b<=duration+1e-6:errors.append(f'phase {i} range')
  if abs(a-end)>1e-6:errors.append(f'phase {i} gap/overlap')
  if not p.get('observation') or not p.get('quality'):errors.append(f'phase {i} missing observations')
  end=b
 if not phases or abs(end-duration)>1e-6:errors.append('incomplete full duration')
 for e in row.get('events',[]):
  if not 0<=e['time_sec']<duration:errors.append('event outside source')
 viewed=set(row.get('coverage',{}).get('actually_viewed_sheet_ids',[]))
 sheets=row.get('evidence',{}).get('sheets',[])
 if viewed!={s['id'] for s in sheets} or not viewed:errors.append('unviewed evidence sheets')
 frames=row.get('evidence',{}).get('frames',[])
 if not frames or frames[0]['time_sec']!=0 or frames[-1]['time_sec']<duration-1.1:errors.append('sampling does not reach both ends')
 if row.get('content_sha256')!=digest({k:v for k,v in row.items() if k!='content_sha256'}):errors.append('annotation digest mismatch')
 if verify_files:
  if file_hash(source['local_path'])!=source['file_sha256']:errors.append('source digest mismatch')
  for f in frames+sheets+[row['evidence']['review']]:
   if file_hash(f['path'])!=f['sha256']:errors.append('evidence digest mismatch '+f['path'])
 return errors
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--report',required=True);ap.add_argument('--skip-source-files',action='store_true');args=ap.parse_args()
 obj=json.loads(Path(args.input).read_text());rows=obj.get('materials',[obj]);results=[]
 for row in rows:
  try:errors=validate(row,not args.skip_source_files)
  except (KeyError,ValueError,OSError,TypeError) as e:errors=[str(e)]
  results.append({'source_id':row.get('source',{}).get('source_id'),'errors':errors})
 report={'status':'pass' if all(not x['errors'] for x in results) else 'fail','checked':len(rows),'file_hashes_verified':not args.skip_source_files,'results':results}
 target=Path(args.report)
 if target.exists():raise SystemExit('Report already exists; choose a new path')
 target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:report[k] for k in ['status','checked','file_hashes_verified']}));raise SystemExit(0 if report['status']=='pass' else 1)
