"""Synthetic regression suite against the installed image, not checkout imports."""
import io,json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
import gateway_mcp

source=Path(__file__).resolve().parents[1]
names=['test_frame_sdk_delivery','test_corporate_stability','test_bitrix_embed','test_bitrix_identity','test_bitrix_auth_flow','test_knowledge_result_contract','test_corporate_core','test_platforma_clients','test_skill_library_validation','test_unisender_readonly','test_admin_role_authority','test_platforma_sandbox_telemetry','test_connection_diagnostics','test_evening_release','test_skill_capabilities']
with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    shutil.copytree(source/'tests',root/'tests',ignore=shutil.ignore_patterns('__pycache__'))
    for name in ['gateway-platforma-tools.json','addons/platforma-training/example-addon-profile.json']:
        target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/name,target)
    (root/'gateway_mcp').symlink_to(Path(gateway_mcp.__file__).parent,target_is_directory=True)
    env=dict(os.environ)
    for key in ['GATEWAY_DATABASE_URL','GATEWAY_LOGIN_PROVIDER']:env.pop(key,None)
    env['GATEWAY_AUTH_ENABLED']='false' # Test fixtures patch their own auth; never a deployment setting.
    env['GATEWAY_AUDIT_LOG']=str(root/'audit.jsonl')
    report=[]
    # Legacy fixtures replace imports globally. Keep the real diagnostic tests
    # independent, using the same installed package in each fresh process.
    for cohort in [[name] for name in names+['test_metrics_access']]:
        program="""import io,json,logging,sys,unittest
logging.disable(logging.CRITICAL)
sys.path.insert(0,'tests')
suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in NAMES)
stream=io.StringIO();result=unittest.TextTestRunner(stream=stream).run(suite)
if not result.wasSuccessful():print(stream.getvalue());raise SystemExit(1)
print(json.dumps({'successful':True,'tests':result.testsRun,'skipped':len(result.skipped)}))
""".replace('NAMES',repr(cohort))
        p=subprocess.run([sys.executable,'-c',program],cwd=root,env=env,capture_output=True,text=True,timeout=120)
        if p.returncode:print(p.stdout[-5000:]);print(p.stderr[-1500:]);raise SystemExit(p.returncode)
        item=json.loads(p.stdout.strip().splitlines()[-1]);assert item['skipped']==0
        report.append(item)
    print(json.dumps({'successful':True,'cohorts':report,'tests':sum(r['tests'] for r in report),'uid':os.getuid(),'network_required':False,'real_api_calls':False,'runtime_source':str(gateway_mcp.__file__)}))
