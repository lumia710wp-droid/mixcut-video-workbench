import importlib.util
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'scripts/check_facts.py'
s=importlib.util.spec_from_file_location('facts',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def sample():
 r={'schema_version':'material_facts_v1','production_approved':False,'source':{'source_id':'x','duration_sec':3},'phases':[{'start_sec':0,'end_sec':1,'observation':'取料','quality':'遮挡'},{'start_sec':1,'end_sec':3,'observation':'整形','quality':'清楚'}],'events':[{'time_sec':1,'text':'整形'}],'coverage':{'actually_viewed_sheet_ids':['s']},'evidence':{'sheets':[{'id':'s'}],'frames':[{'time_sec':0},{'time_sec':2.5}]}}
 return r
def validate(r):r['content_sha256']=m.digest(r);return m.validate(r,False)
def test_full_facts():assert validate(sample())==[]
def test_gap():
 r=sample();r['phases'][1]['start_sec']=1.2;assert any('gap' in e for e in validate(r))
def test_editorial_field():
 r=sample();r['phases'][0]['speed']=1.1;assert any('editorial' in e for e in validate(r))
def test_unviewed():
 r=sample();r['coverage']['actually_viewed_sheet_ids']=[];assert any('unviewed' in e for e in validate(r))
def test_approval_not_inferred():
 r=sample();r['production_approved']=True;assert any('approval' in e for e in validate(r))
