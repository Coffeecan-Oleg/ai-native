"""Bounded synthetic source-copy smoke load. Not a capacity benchmark."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib,io,json,os,sys,time,zipfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'onboarding_tests')]
from test_skill_lifecycle import LifecycleFixture
from test_skill_library import CONTENT
from gateway_mcp.services import corporate_skills,skill_activity,skill_library,skill_capabilities
from gateway_mcp.services.storage_core import _connect

def main():
    assert os.getenv('GATEWAY_TEST_ISOLATED')=='true'
    fixture=LifecycleFixture('runTest');started=time.monotonic();report={'synthetic_only':True,'production_load':False,
        'budget':{'workers':4,'catalogue_skills':32,'read_requests':24,'preflight_requests':8,'wall_seconds':90,'external_calls':0}}
    try:
        fixture.setUp();owner=fixture.owner;subjects=[];ids=[]
        for index in range(4):
            who=replace(fixture.reader,subject='yandex:'+str(9100+index),yandex_id=str(9100+index),email='load'+str(index)+'@example.invalid')
            fixture.approve(who);subjects.append(who)
            for j in range(8):
                row=fixture.draft(who,title='Synthetic load '+str(index)+' '+str(j));ids.append(row['name'])
                fixture.action(row['name'],'submit',who,revision=fixture.row(row['name'],who)['revision'])
                fixture.action(row['name'],'publish',owner,revision=fixture.row(row['name'],owner)['revision'])
        actor=replace(fixture.fresh(subjects[0]),scopes=('skills:read','telemetry:read'));sid=ids[0]
        def read(_):
            stamp=time.monotonic();data=corporate_skills.read_skill(sid,actor=actor)
            receipt=skill_activity.record_read(actor,data)
            assert receipt['recorded'] and data['sha256']==hashlib.sha256(CONTENT).hexdigest()
            return time.monotonic()-stamp
        with patch.object(skill_activity,'enabled',return_value=True),ThreadPoolExecutor(max_workers=4) as pool:timings=list(pool.map(read,range(24)))
        with ThreadPoolExecutor(max_workers=4) as pool:checks=list(pool.map(lambda _:skill_capabilities.check(actor,sid),range(8)))
        assert all(c['requirements_source']=='requirements_missing' for c in checks)
        with patch.object(skill_activity,'enabled',return_value=True):timeline=skill_activity.own_activity(actor)
        assert len(timeline['items'])==24
        body=b'x'*skill_library.MAX_FILE
        skill_library.parse_upload('SKILL.md',body)
        try:skill_library.parse_upload('SKILL.md',body+b'x');raise AssertionError('Oversize accepted')
        except ValueError:pass
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('SKILL.md',CONTENT)
            for i in range(31):z.writestr('refs/'+str(i)+'.md',body)
        try:skill_library.parse_upload('too-expanded.zip',stream.getvalue());raise AssertionError('Expanded size accepted')
        except ValueError:pass
        elapsed=time.monotonic()-started;assert elapsed<90
        report.update(successful=True,catalogue_added=len(ids),reads=len(timings),preflights=len(checks),
            read_p95_ms=round(sorted(timings)[int(len(timings)*.95)-1]*1000,2),read_max_ms=round(max(timings)*1000,2),
            wall_seconds=round(elapsed,2),upload_boundary_bytes=skill_library.MAX_FILE,oversize_rejected=True,
            expanded_archive_rejected=True,own_timeline_reads=len(timeline['items']))
    finally:fixture.doCleanups()
    print(json.dumps(report));return 0

if __name__=='__main__':raise SystemExit(main())
