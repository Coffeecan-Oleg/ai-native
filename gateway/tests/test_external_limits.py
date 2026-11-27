"""Synthetic pacing mathematics and the authorization/admission boundary."""
import asyncio,json,os,unittest
from contextlib import ExitStack
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from gateway_mcp.services import external_limits as limits
from gateway_mcp.backends import router as dispatch
from gateway_mcp.tools import router

ROUTE={'name':'unisender.getLists','transport':'unisender-readonly','backend':'unisender','scope':'unisender:read','http_method':'GET'}
ACTOR=SimpleNamespace(subject='synthetic:employee')
NOW=datetime(2026,1,1,tzinfo=timezone.utc)

class FakeMcp:
    def __init__(self):self.tools={}
    def tool(self,**kwargs):
        def decorate(fn):self.tools[fn.__name__]=fn;return fn
        return decorate

class LimitMathTests(unittest.TestCase):
    def test_default_policy(self):
        with patch.dict(os.environ,{limits.PREFIX+k:str(v) for k,v in [('USER_PER_MINUTE',30),('USER_BURST',5),('SHARED_PER_MINUTE',120),('SHARED_BURST',10)]}):
            self.assertEqual(limits.policy(),{'USER_PER_MINUTE':30,'USER_BURST':5,'SHARED_PER_MINUTE':120,'SHARED_BURST':10})
    def test_invalid_configuration_fails_instead_of_disabling(self):
        for value in ['0','-1','1.5','6001','infinite','']:
            with patch.dict(os.environ,{limits.PREFIX+'USER_PER_MINUTE':value}),self.assertRaises(ValueError):limits.policy()
        with patch.dict(os.environ,{limits.PREFIX+'USER_PER_MINUTE':'1',limits.PREFIX+'USER_BURST':'2'}),self.assertRaises(ValueError):limits.policy()
    def test_burst_and_refill_are_paced_without_sleep(self):
        tat=NOW
        for _ in range(5):wait,tat=limits.advance(NOW,tat,30,5);self.assertEqual(wait,0)
        self.assertEqual(limits.advance(NOW,tat,30,5)[0],2)
        self.assertEqual(limits.advance(NOW+timedelta(seconds=1),tat,30,5)[0],1)
        self.assertEqual(limits.advance(NOW+timedelta(seconds=2),tat,30,5)[0],0)
    def test_expired_bucket_refills_without_old_debt(self):
        wait,tat=limits.advance(NOW,NOW-timedelta(days=2),30,5)
        self.assertEqual(wait,0);self.assertEqual(tat,NOW+timedelta(seconds=2))
    def test_fractional_interval_is_rounded_conservatively(self):
        wait,tat=limits.advance(NOW,NOW,7,1)
        self.assertGreaterEqual((tat-NOW).total_seconds(),60/7);self.assertEqual(wait,0)
    def test_pseudonymous_key_and_bounded_identity(self):
        key=limits.bucket(ACTOR.subject);self.assertRegex(key,r'^user:[a-f0-9]{64}$');self.assertNotIn(ACTOR.subject,key)
        self.assertEqual(key,limits.bucket(ACTOR.subject))
        for subject in ['',None,'x'*513]:
            with self.assertRaises(ValueError):limits.bucket(subject)
    def test_exact_dispatch_coverage_and_internal_exclusions(self):
        self.assertEqual(limits.TRANSPORTS,set(dispatch.BACKEND_DISPATCH)-{'platforma-demo','notification'})
        with patch('gateway_mcp.services.corporate_access.enabled',return_value=True):
            self.assertTrue(limits.enabled(ROUTE));self.assertFalse(limits.enabled({'transport':'platforma-demo'}))
        with patch('gateway_mcp.services.corporate_access.enabled',return_value=False):self.assertFalse(limits.enabled(ROUTE))
    def test_retry_header_is_bounded_and_never_echoed(self):
        for raw,expected in [('2',2),('0',1),('999999',3600),('SECRET_HEADER',60),(None,60),('2026-11-07',60)]:
            self.assertEqual(limits.retry_after(raw),expected)
        e=limits.LimitError(retry=999999).response();self.assertEqual(e['status'],429);self.assertEqual(e['retry_after_seconds'],3600)
        self.assertFalse(e['upstream_contacted'])
    def test_permit_is_single_use_and_bound_to_identity_and_route_object(self):
        p=limits._Permit(ROUTE,ACTOR.subject);limits.consume(p,ROUTE,ACTOR)
        with self.assertRaises(limits.LimitError):limits.consume(p,ROUTE,ACTOR)
        for permit,route,actor in [('fake',ROUTE,ACTOR),(limits._Permit(ROUTE,ACTOR.subject),dict(ROUTE),ACTOR),
            (limits._Permit(ROUTE,ACTOR.subject),ROUTE,SimpleNamespace(subject='other'))]:
            with self.assertRaises(limits.LimitError):limits.consume(permit,route,actor)
    def test_database_failure_is_sanitized_and_fails_closed(self):
        with patch.object(limits,'enabled',return_value=True),patch.object(limits,'_connect',side_effect=RuntimeError('SECRET_DATABASE_VALUE')):
            with self.assertRaises(limits.LimitError) as e:limits.require(ROUTE,ACTOR)
        self.assertEqual(e.exception.response()['status'],503);self.assertNotIn('SECRET_DATABASE_VALUE',json.dumps(e.exception.response()))

class RouterAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.stack=self.enterContext(ExitStack());self.route=dict(ROUTE);self.run=Mock()
        self.stack.enter_context(patch.object(router.ToolRun,'start',return_value=self.run))
        self.stack.enter_context(patch.object(router,'read_json',side_effect=lambda *a,**k:{'tools':[self.route]}))
        self.stack.enter_context(patch.object(router,'current_actor',return_value=ACTOR))
        self.access=self.stack.enter_context(patch.object(router,'require_resource_access',return_value={'decision':'allow','system':'unisender'}))
        self.stack.enter_context(patch.object(router,'_audit_resource_access'))
        self.on=self.stack.enter_context(patch.object(limits,'enabled',return_value=True))
        self.reserve=self.stack.enter_context(patch.object(limits,'require',return_value=limits._Permit(self.route,ACTOR.subject)))
        self.backend=self.stack.enter_context(patch.object(router,'call_backend',AsyncMock(return_value={'ok':True,'status':200,'data':{}})))
        self.approval=self.stack.enter_context(patch.object(router,'validate_and_consume_approval'))
        self.claim=self.stack.enter_context(patch.object(router,'claim_idempotency',return_value={'claimed':True}))
        self.fail=self.stack.enter_context(patch.object(router,'fail_idempotency'))
        self.stack.enter_context(patch.object(router,'complete_idempotency'))
        self.stack.enter_context(patch.object(router,'record_upstream_error'))
        fake=FakeMcp();router.register_router_tools(fake);self.call=fake.tools['gateway_call_tool']
    def result(self,**kwargs):return json.loads(asyncio.run(self.call(self.route['name'],**kwargs)))
    def test_auth_failure_precedes_quota_and_backend(self):
        self.access.side_effect=PermissionError('denied')
        with self.assertRaises(PermissionError):self.result()
        self.reserve.assert_not_called();self.backend.assert_not_awaited()
    def test_local_limit_returns_retry_and_request_id_without_outbound(self):
        self.reserve.side_effect=limits.LimitError(retry=2)
        r=self.result();self.assertEqual(r['status'],429);self.assertEqual(r['retry_after_seconds'],2)
        self.assertRegex(r['gateway_request_id'],r'^[a-f0-9]{16}$');self.backend.assert_not_awaited()
        self.run.finish.assert_called_once_with(status='rate_limited',scope='unisender:read',error='EXTERNAL_RATE_LIMITED')
    def test_fail_closed_admission_does_not_call_backend(self):
        self.reserve.side_effect=limits.LimitError('EXTERNAL_LIMITER_UNAVAILABLE',5,level='service')
        self.assertEqual(self.result()['status'],503);self.backend.assert_not_awaited()
    def test_write_denial_releases_claim_and_does_not_consume_approval(self):
        self.route.update(http_method='POST',requires_approval_ref=True)
        self.reserve.side_effect=limits.LimitError(retry=2)
        r=self.result(arguments_json='{"approval_ref":"synthetic"}',idempotency_key='synthetic-key')
        self.assertEqual(r['status'],429);self.fail.assert_called_once();self.approval.assert_not_called();self.backend.assert_not_awaited()
    def test_cleanup_failure_blocks_automatic_write_retry(self):
        self.route['http_method']='POST';self.reserve.side_effect=limits.LimitError();self.fail.side_effect=RuntimeError('SECRET')
        r=self.result(idempotency_key='synthetic-key');self.assertEqual(r['status'],503);self.assertFalse(r['retryable'])
        self.assertEqual(r['idempotency_state'],'pending');self.assertNotIn('SECRET',json.dumps(r));self.backend.assert_not_awaited()
    def test_saved_write_replay_uses_no_admission(self):
        self.route['http_method']='POST';self.claim.return_value={'replayed':True,'response':{'ok':True,'data':{'saved':True}}}
        self.assertTrue(self.result(idempotency_key='synthetic-key')['idempotency']['replayed'])
        self.reserve.assert_not_called();self.backend.assert_not_awaited()
    def test_validated_admission_is_passed_to_the_common_dispatcher(self):
        self.result();self.backend.assert_awaited_once_with(self.route,{},admission=self.reserve.return_value)

class CommonDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_helpers_also_reserve_once(self):
        route=dict(ROUTE);permit=limits._Permit(route,ACTOR.subject);backend=AsyncMock(return_value={'ok':True})
        with (patch.object(limits,'enabled',return_value=True),patch.object(limits,'require',return_value=permit) as reserve,
             patch('gateway_mcp.services.auth.current_actor',return_value=ACTOR),patch.object(dispatch,'_backend_callable',return_value=backend)):
            self.assertTrue((await dispatch.call_backend(route,{}))['ok'])
            reserve.assert_called_once();self.assertTrue(permit.used)
    async def test_pre_reserved_permit_is_not_charged_twice(self):
        route=dict(ROUTE);permit=limits._Permit(route,ACTOR.subject)
        with (patch.object(limits,'enabled',return_value=True),patch.object(limits,'require',side_effect=AssertionError('double charge')),
             patch('gateway_mcp.services.auth.current_actor',return_value=ACTOR),patch.object(dispatch,'_backend_callable',return_value=AsyncMock(return_value={'ok':True}))):
            await dispatch.call_backend(route,{},admission=permit)
            with self.assertRaises(limits.LimitError):await dispatch.call_backend(route,{},admission=permit)
    async def test_provider_cooldown_is_recorded_once_without_retry(self):
        route=dict(ROUTE);backend=AsyncMock(side_effect=limits.LimitError('EXTERNAL_UPSTREAM_RATE_LIMITED',17,level='integration',contacted=True))
        with (patch.object(limits,'enabled',return_value=False),patch.object(limits,'cooldown') as cool,
             patch.object(dispatch,'_backend_callable',return_value=backend)):
            with self.assertRaises(limits.LimitError):await dispatch.call_backend(route,{})
            backend.assert_awaited_once();cool.assert_called_once_with(route,17)
