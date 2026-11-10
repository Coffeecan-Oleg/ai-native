"""25 -> 26 -> 27 -> 28 -> 29 on an explicitly isolated, synthetic test DB."""
import hashlib,json,os,shutil,tempfile
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from psycopg import sql
from gateway_mcp.services import migrations


def main():
    if os.getenv('GATEWAY_TEST_ISOLATED')!='true' or urlsplit(migrations.database_url()).path!='/platforma_upgrade_test':
        raise RuntimeError('Refuse a non-isolated upgrade database')
    files=migrations.migration_files()
    if len(files)!=29 or files[-1].stem!='0029_activity_notifications':
        raise RuntimeError('Unexpected schema boundary')
    with tempfile.TemporaryDirectory() as temp:
        for path in files[:25]:shutil.copyfile(path,Path(temp)/path.name)
        with patch.dict(os.environ,{'GATEWAY_MIGRATIONS_DIR':temp}):
            initial=migrations.migrate_up()
            assert len(initial['status']['applied'])==25 and not initial['status']['pending']
    with migrations.connect() as conn:
        tables=[r['tablename'] for r in conn.execute("select tablename from pg_tables where schemaname='public' and tablename<>'schema_migrations' order by tablename")]
        for number in range(3):
            sid='shared-'+format(number+1,'032x');content=('Synthetic retained instruction '+str(number)).encode()
            digest=hashlib.sha256(content).hexdigest()
            conn.execute("""insert into platforma_shared_skills
                (skill_id,origin,title,title_key,description,version,author_subject,author_label,manifest)
                values (%s,'uploaded',%s,%s,'Synthetic only','1.0.0','copy-author','Copy author',%s::jsonb)""",
                (sid,'Copy skill '+str(number),'copy skill '+str(number),json.dumps({'SKILL.md':{'sha256':digest,'bytes':len(content)}})))
            conn.execute('insert into platforma_shared_skill_files values (%s,%s,%s,%s)',(sid,'SKILL.md',content,digest))
            conn.execute("insert into platforma_shared_skill_events(skill_id,revision,actor_subject,action) values (%s,1,'copy-author','uploaded')",(sid,))
    def snapshot():
        result={}
        with migrations.connect() as conn:
            for table in tables:
                rows=conn.execute(sql.SQL('select row_to_json(t)::text as data from {} t').format(sql.Identifier(table))).fetchall()
                data='\n'.join(sorted(r['data'] for r in rows)).encode()
                result[table]={'count':len(rows),'sha256':hashlib.sha256(data).hexdigest()}
        return result
    before=snapshot()
    with tempfile.TemporaryDirectory() as temp:
        for path in files[:26]:shutil.copyfile(path,Path(temp)/path.name)
        with patch.dict(os.environ,{'GATEWAY_MIGRATIONS_DIR':temp}):upgraded=migrations.migrate_up()
    after=snapshot()
    assert upgraded['applied']==['0026_skill_lifecycle'] and before==after
    with migrations.connect() as conn:
        assert conn.execute('select count(*) n from platforma_skill_workflows').fetchone()['n']==0
        # A legacy reader still sees exactly the old three publications.
        assert conn.execute("select count(*) n from platforma_shared_skills where origin='uploaded'").fetchone()['n']==3
        sid='shared-'+format(4,'032x');content='Synthetic private draft';raw=content.encode()
        manifest={'SKILL.md':{'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}}
        conn.execute("""insert into platforma_skill_workflows(skill_id,author_subject,author_label,stage,candidate_revision)
            values (%s,'copy-author','Copy author','draft',1)""",(sid,))
        conn.execute("""insert into platforma_skill_versions
            (skill_id,revision,title,title_key,description,version,manifest,files,fingerprint,created_by)
            values (%s,1,'Copy draft','copy draft','Synthetic only','1.0.0',%s::jsonb,%s::jsonb,%s,'copy-author')""",
            (sid,json.dumps(manifest),json.dumps({'SKILL.md':content}),hashlib.sha256(raw).hexdigest()))
        conn.execute("""insert into platforma_skill_workflow_events(skill_id,revision,actor_subject,action)
            values (%s,1,'copy-author','draft_created')""",(sid,))
        tables=[r['tablename'] for r in conn.execute("select tablename from pg_tables where schemaname='public' and tablename<>'schema_migrations' order by tablename")]
    before27=snapshot()
    with tempfile.TemporaryDirectory() as temp:
        for path in files[:27]:shutil.copyfile(path,Path(temp)/path.name)
        with patch.dict(os.environ,{'GATEWAY_MIGRATIONS_DIR':temp}):last=migrations.migrate_up()
    after27=snapshot()
    assert last['applied']==['0027_external_limits'] and before27==after27
    with migrations.connect() as conn:assert conn.execute('select count(*) n from gateway_external_limits').fetchone()['n']==0
    with migrations.connect() as conn:
        conn.execute("""insert into gateway_external_limits(integration,bucket,tat,blocked_until,updated_at)
            values ('unisender-readonly','global',clock_timestamp()+interval '1 minute',clock_timestamp()+interval '2 minutes',clock_timestamp())""")
        tables=[r['tablename'] for r in conn.execute("select tablename from pg_tables where schemaname='public' and tablename<>'schema_migrations' order by tablename")]
    before28=snapshot()
    with tempfile.TemporaryDirectory() as temp:
        for path in files[:28]:shutil.copyfile(path,Path(temp)/path.name)
        with patch.dict(os.environ,{'GATEWAY_MIGRATIONS_DIR':temp}):last=migrations.migrate_up()
    after28=snapshot()
    assert last['applied']==['0028_external_safety'] and before28==after28
    with migrations.connect() as conn:
        assert conn.execute('select count(*) n from gateway_external_calls').fetchone()['n']==0
        assert conn.execute('select count(*) n from gateway_external_circuits').fetchone()['n']==0
        schema27_tables=len(tables)
        tables=[r['tablename'] for r in conn.execute("select tablename from pg_tables where schemaname='public' and tablename<>'schema_migrations' order by tablename")]
    before29=snapshot();last=migrations.migrate_up();after29=snapshot()
    assert last['applied']==['0029_activity_notifications'] and before29==after29
    assert len(last['status']['applied'])==29 and not last['status']['pending']
    assert migrations.migrate_up()['applied']==[]
    with migrations.connect() as conn:
        settings=conn.execute('select enabled,categories,revision,interval_seconds from platforma_activity_settings where id=1').fetchone()
        assert settings=={'enabled':False,'categories':[],'revision':1,'interval_seconds':60}
        assert conn.execute('select count(*) n from platforma_activity_outbox').fetchone()['n']==0
        final_tables={r['tablename'] for r in conn.execute("select tablename from pg_tables where schemaname='public' and tablename<>'schema_migrations'")}
        assert final_tables-set(tables)=={'platforma_activity_settings','platforma_activity_outbox'}
    print(json.dumps({'successful':True,'from_migrations':25,'to_migrations':29,'dev_upgrade_from':28,
        'dev_upgrade_to':29,'schema27_tables_unchanged':schema27_tables,'schema28_tables_unchanged':len(tables),
        'activity_notifications_disabled':True,'activity_outbox_empty':True,'new_tables':2,
        'rate_state_retained':True,'private_draft_retained':True,
        'old_tables_unchanged':len(tables),'synthetic_publications_retained':3,
        'legacy_workflows_created':0,'idempotent':True,'production_data':False}))


if __name__=='__main__':main()
