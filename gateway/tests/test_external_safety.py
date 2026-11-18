"""Synthetic circuit wrapper: cancellation, classification and no automatic retry."""
import asyncio,json,os,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import httpx
from gateway_mcp.services import external_safety as safety,external_limits as limits
from gateway_mcp.backends import unisender_readonly as backend,router as dispatch
from gateway_mcp.backends.common import BackendRouteError

ROUTE={'name':'unisender.getLists','transport':'unisender-readonly','backend':'unisender','scope':'unisender:read','http_method':'GET'}

class SafetyPolicyTests(unittest.TestCase):
    def test_defaults_and_only_classified_transport(self):
        self.assertEqual(safety.policy(),{'USER_CONCURRENCY':2,'SHARED_CONCURRENCY':4,'FAILURE_THRESHOLD':3,'OPEN_SECONDS':30,'TIMEOUT_SECONDS':30})
        with patch('gateway_mcp.services.corporate_access.enabled',return_value=True):
            self.assertTrue(safety.enabled(ROUTE));self.assertFalse(safety.enabled({'transport':'gitlab-rest'}))
            self.assertFalse(safety.enabled({'transport':'platforma-demo'}))
        with patch('gateway_mcp.services.corporate_access.enabled',return_value=False):self.assertFalse(safety.enabled(ROUTE))
    def test_invalid_configuration_cannot_turn_safety_off(self):
        for key,value in [('USER_CONCURRENCY','0'),('SHARED_CONCURRENCY','33'),('TIMEOUT_SECONDS','61'),
                          ('FAILURE_THRESHOLD','x'),('OPEN_SECONDS','-1'),('OPEN_SECONDS','')]:
            with patch.dict(os.environ,{safety.PREFIX+key:value}),self.assertRaises(ValueError):safety.policy()
        with patch.dict(os.environ,{safety.PREFIX+'USER_CONCURRENCY':'5',safety.PREFIX+'SHARED_CONCURRENCY':'4'}),self.assertRaises(ValueError):safety.policy()
    def test_safe_distinct_errors_have_no_raw_details(self):
        for code,status in [('EXTERNAL_CONCURRENCY_LIMIT',429),('EXTERNAL_CIRCUIT_OPEN',503),
                            ('EXTERNAL_CALL_TIMEOUT',504),('EXTERNAL_SERVICE_UNAVAILABLE',502),('EXTERNAL_SAFETY_UNAVAILABLE',503)]:
            r=safety.SafetyError(code,9999,level='integration').response()
            self.assertEqual(r['status'],status);self.assertFalse(r['upstream_contacted']);self.assertEqual(r['retry_after_seconds'],3600)
    def test_unused_permit_release_is_once_and_consumed_permit_is_preserved(self):
        lease=safety.Lease('synthetic','unisender-readonly');p=limits._Permit(ROUTE,'employee',lease=lease)
        with patch.object(safety,'finish') as finish:
            limits.release_unused(p);limits.release_unused(p);finish.assert_called_once_with(lease,'neutral')
        p=limits._Permit(ROUTE,'employee',lease=lease);limits.consume(p,ROUTE,SimpleNamespace(subject='employee'))
        with patch.object(safety,'finish') as finish:limits.release_unused(p);finish.assert_not_called()
    def test_storage_failure_is_closed_and_sanitized(self):
        with patch.object(limits,'_connect',side_effect=RuntimeError('SECRET')):
            for fn,args in [(safety.start,(safety.Lease('a','unisender-readonly'),)),(safety.finish,(safety.Lease('a','unisender-readonly'),'success'))]:
                with self.assertRaises(safety.SafetyError) as caught:fn(*args)
                self.assertNotIn('SECRET',json.dumps(caught.exception.response()));self.assertEqual(caught.exception.response()['status'],503)
    def test_rejected_approval_releases_reserved_slot_without_backend(self):
        from contextlib import ExitStack
        from test_external_limits import FakeMcp
        from gateway_mcp.tools import router
        route=ROUTE|{'requires_approval_ref':True};actor=SimpleNamespace(subject='employee');permit=limits._Permit(route,actor.subject)
        with ExitStack() as stack:
            for name,value in [('read_json',lambda *a,**k:{'tools':[route]}),('current_actor',lambda:actor),
                               ('require_resource_access',lambda **kw:{'decision':'allow'}),('_audit_resource_access',lambda **kw:None)]:
                stack.enter_context(patch.object(router,name,side_effect=value))
            stack.enter_context(patch.object(router.ToolRun,'start'))
            stack.enter_context(patch.object(limits,'enabled',return_value=True))
            stack.enter_context(patch.object(limits,'require',return_value=permit))
            release=stack.enter_context(patch.object(limits,'release_unused'))
            backend=stack.enter_context(patch.object(router,'call_backend',AsyncMock()))
            stack.enter_context(patch.object(router,'validate_and_consume_approval',side_effect=PermissionError('denied')))
            fake=FakeMcp();router.register_router_tools(fake)
            with self.assertRaises(PermissionError):asyncio.run(fake.tools['gateway_call_tool']('unisender.getLists',arguments_json='{"approval_ref":"synthetic"}'))
            release.assert_called_once_with(permit);backend.assert_not_awaited()

class SafetyExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.permit=limits._Permit(ROUTE,'employee',lease=safety.Lease('synthetic','unisender-readonly'))
        self.enterContext(patch.object(safety,'start',return_value=1))
        self.finish=self.enterContext(patch.object(safety,'finish'))
    async def test_success_is_once_and_releases_slot(self):
        call=AsyncMock(return_value={'ok':True,'data':{}})
        self.assertTrue((await safety.invoke(self.permit,call))['ok']);call.assert_awaited_once()
        self.finish.assert_called_once_with(self.permit.lease,'success')
    async def test_classified_failure_counts_without_automatic_retry(self):
        call=AsyncMock(side_effect=safety.TransientFailure('sanitized'))
        with self.assertRaises(safety.SafetyError) as caught:await safety.invoke(self.permit,call)
        self.assertEqual(caught.exception.code,'EXTERNAL_SERVICE_UNAVAILABLE');self.assertTrue(caught.exception.contacted)
        call.assert_awaited_once();self.finish.assert_called_once_with(self.permit.lease,'failure')
    async def test_auth_bad_input_and_429_do_not_count_as_service_failure(self):
        for error in [PermissionError('denied'),BackendRouteError('bad arguments'),limits.LimitError('EXTERNAL_UPSTREAM_RATE_LIMITED')]:
            self.finish.reset_mock()
            with self.assertRaises(type(error)):await safety.invoke(self.permit,AsyncMock(side_effect=error))
            self.finish.assert_called_once_with(self.permit.lease,'neutral')
    async def test_absolute_timeout_cancels_backend_and_releases_slot(self):
        closed=asyncio.Event()
        async def hanging():
            try:await asyncio.sleep(60)
            finally:closed.set()
        with patch.object(safety,'start',return_value=.02):
            with self.assertRaises(safety.SafetyError) as caught:await safety.invoke(self.permit,hanging)
        self.assertEqual(caught.exception.code,'EXTERNAL_CALL_TIMEOUT');self.assertTrue(closed.is_set())
        self.finish.assert_called_once_with(self.permit.lease,'failure')
    async def test_client_cancellation_is_not_a_provider_failure(self):
        entered=asyncio.Event()
        async def waiting():entered.set();await asyncio.sleep(60)
        task=asyncio.create_task(safety.invoke(self.permit,waiting));await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.finish.assert_called_once_with(self.permit.lease,'neutral')
    async def test_missing_or_stale_permit_never_calls_provider(self):
        call=AsyncMock()
        with self.assertRaises(safety.SafetyError):await safety.invoke(SimpleNamespace(lease=None),call)
        with patch.object(safety,'start',side_effect=safety.SafetyError('EXTERNAL_SAFETY_UNAVAILABLE')):
            with self.assertRaises(safety.SafetyError):await safety.invoke(self.permit,call)
        call.assert_not_awaited()
    async def test_failed_completion_is_sanitized_and_does_not_repeat_provider(self):
        call=AsyncMock(return_value={'ok':True})
        self.finish.side_effect=safety.SafetyError('EXTERNAL_SAFETY_UNAVAILABLE')
        with self.assertRaises(safety.SafetyError) as caught:await safety.invoke(self.permit,call)
        self.assertTrue(caught.exception.contacted);self.assertEqual(caught.exception.response()['status'],503);call.assert_awaited_once()
    async def test_common_dispatch_uses_guard_and_keeps_provider_429_cooldown(self):
        actor=SimpleNamespace(subject='employee');self.permit.route=ROUTE
        with (patch.object(limits,'enabled',return_value=True),patch.object(safety,'enabled',return_value=True),
              patch('gateway_mcp.services.auth.current_actor',return_value=actor),
              patch.object(limits,'require',return_value=self.permit),
              patch.object(dispatch,'_backend_callable',return_value=AsyncMock(return_value={'ok':True}))):
            self.assertTrue((await dispatch.call_backend(ROUTE,{}))['ok'])
        self.permit.used=False;self.finish.reset_mock()
        call=AsyncMock(side_effect=limits.LimitError('EXTERNAL_UPSTREAM_RATE_LIMITED',17,contacted=True))
        with (patch.object(limits,'enabled',return_value=True),patch.object(safety,'enabled',return_value=True),
              patch('gateway_mcp.services.auth.current_actor',return_value=actor),
              patch.object(limits,'require',return_value=self.permit),patch.object(limits,'cooldown') as cool,
              patch.object(dispatch,'_backend_callable',return_value=call)):
            with self.assertRaises(limits.LimitError):await dispatch.call_backend(ROUTE,{})
            cool.assert_called_once_with(ROUTE,17);call.assert_awaited_once()
    async def test_circuit_failure_does_not_turn_into_rate_limit_cooldown(self):
        actor=SimpleNamespace(subject='employee')
        with (patch.object(limits,'enabled',return_value=True),patch.object(safety,'enabled',return_value=True),
              patch('gateway_mcp.services.auth.current_actor',return_value=actor),
              patch.object(limits,'require',return_value=self.permit),patch.object(limits,'cooldown') as cool,
              patch.object(dispatch,'_backend_callable',return_value=AsyncMock(side_effect=safety.TransientFailure('sanitized')))):
            with self.assertRaises(safety.SafetyError) as caught:await dispatch.call_backend(ROUTE,{})
            self.assertEqual(caught.exception.code,'EXTERNAL_SERVICE_UNAVAILABLE');cool.assert_not_called()

class UniSenderFailureClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def request(self,status=None,error=None):
        original=httpx.AsyncClient
        def reply(req):
            if error:raise error
            return httpx.Response(status,text='SECRET_PROVIDER_BODY')
        with (patch.object(backend,'_credential',return_value='synthetic-key'),
              patch.object(backend.httpx,'AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(reply),**kw))):
            return await backend._post_api('getLists',{})
    async def test_http_5xx_and_network_timeout_are_transient(self):
        for status in [500,502,503,504]:
            with self.assertRaises(safety.TransientFailure) as caught:await self.request(status)
            self.assertNotIn('SECRET',str(caught.exception))
        with self.assertRaises(safety.TransientFailure):await self.request(error=httpx.ReadTimeout('SECRET_TIMEOUT'))
    async def test_http_auth_input_and_bad_payload_are_not_transient(self):
        for status in [400,401,403,404,200]:
            with self.assertRaises(BackendRouteError) as caught:await self.request(status)
            self.assertNotIsInstance(caught.exception,safety.TransientFailure);self.assertNotIn('SECRET',str(caught.exception))
