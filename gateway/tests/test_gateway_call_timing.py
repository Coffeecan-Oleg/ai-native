"""Server-origin timing and truthful, separated outcome rendering."""
import unittest
from unittest.mock import patch
from gateway_mcp import audit
from gateway_mcp.routes import admin_integration_status as ui
from gateway_mcp.services import external_status as status
from gateway_mcp.services.policy import GatewayActor


class ServerTimingTests(unittest.TestCase):
    def test_monotonic_duration_overwrites_claimed_value_without_mutating_input(self):
        original={'tool_name':'unisender.getLists','gateway_duration_ms':1,
            'gateway_timing_kind':'client','arguments':{'gateway_duration_ms':999999}}
        with patch.object(audit.time,'perf_counter',return_value=12.345):
            value=audit._timed_arguments(original,10)
        self.assertEqual(value['gateway_duration_ms'],2345)
        self.assertEqual(value['gateway_timing_kind'],'tool_run_v1')
        self.assertEqual(original['gateway_duration_ms'],1)
        self.assertEqual(value['arguments']['gateway_duration_ms'],999999)

    def test_zero_is_a_measurement_not_missing_data(self):
        with patch.object(audit.time,'perf_counter',return_value=10):
            self.assertEqual(audit._timed_arguments({},10)['gateway_duration_ms'],0)

    def test_invalid_or_out_of_range_clock_never_invents_zero_or_keeps_client_value(self):
        for started,finished in [(11,10),(0,float('inf')),(float('nan'),10),('invalid',10),(0,86401)]:
            with self.subTest(started=started,finished=finished),patch.object(audit.time,'perf_counter',return_value=finished):
                row=audit._timed_arguments({'gateway_duration_ms':1,'gateway_timing_kind':'tool_run_v1'},started)
                self.assertNotIn('gateway_duration_ms',row);self.assertNotIn('gateway_timing_kind',row)

    def test_day_boundary_is_valid_and_integer(self):
        with patch.object(audit.time,'perf_counter',return_value=86400):
            value=audit._timed_arguments({},0)['gateway_duration_ms']
        self.assertEqual(value,86400000);self.assertIs(type(value),int)

    def test_success_error_limit_and_denial_share_server_boundary(self):
        actor=GatewayActor(subject='synthetic:owner')
        for outcome,decision in [('ok','allow'),('error','allow'),('rate_limited','allow'),('denied','deny')]:
            with self.subTest(outcome=outcome),patch.object(audit.time,'perf_counter',return_value=10.125), \
                 patch.object(audit,'observe_tool_call') as metric,patch.object(audit,'audit_event') as event, \
                 patch.object(audit,'current_actor',return_value=actor):
                audit.finish_tool(tool='gateway_call_tool',system='unisender',started_at=10,status=outcome,
                    decision=decision,scope='unisender:read',arguments={'tool_name':'unisender.getLists'})
                self.assertEqual(event.call_args.kwargs['arguments']['gateway_duration_ms'],125)
                self.assertEqual(event.call_args.kwargs['status'],outcome)
                self.assertEqual(event.call_args.kwargs['decision'],decision)
                metric.assert_called_once()

    def test_other_tools_keep_their_audit_contract(self):
        args={'value':'synthetic'}
        with patch.object(audit,'observe_tool_call'),patch.object(audit,'audit_event') as event, \
             patch.object(audit,'current_actor',return_value=GatewayActor(subject='synthetic:owner')), \
             patch.object(audit,'_timed_arguments') as timing:
            audit.finish_tool(tool='gateway_access_profile',system='access',started_at=10,status='ok',
                decision='allow',scope='skills:read',arguments=args)
        timing.assert_not_called();self.assertEqual(event.call_args.kwargs['arguments'],args)


class TimingViewTests(unittest.TestCase):
    def test_no_measurements_is_explicitly_unknown_not_zero_milliseconds(self):
        model=status._timing_projection([{'outcome':'success','observed':7,'timed':0}])
        rendered=ui._timing(model)
        self.assertIn('Замеров нет',rendered);self.assertIn('событий: 7',rendered)
        self.assertNotIn('0 мс',rendered);self.assertNotIn('<table',rendered)

    def test_successes_and_fast_denials_render_as_separate_groups(self):
        model=status._timing_projection([
            {'outcome':'success','observed':4,'timed':3,'p50_ms':2000,'p95_ms':4000,'max_ms':4000},
            {'outcome':'limited','observed':16,'timed':16,'p50_ms':10,'p95_ms':20,'max_ms':25}])
        rendered=ui._timing(model)
        self.assertIn('Успешные',rendered);self.assertIn('Ограничения',rendered)
        self.assertIn('3 / 4',rendered);self.assertIn('16 / 16',rendered)
        self.assertIn('4000 мс',rendered);self.assertIn('20 мс',rendered)
        self.assertIn('последние до 1000',rendered)
        self.assertIn('без последующей записи аудита',rendered)

    def test_zero_duration_remains_visible_and_untimed_error_is_not_zero(self):
        model=status._timing_projection([
            {'outcome':'success','observed':1,'timed':1,'p50_ms':0,'p95_ms':0,'max_ms':0},
            {'outcome':'errors','observed':1,'timed':0}])
        rendered=ui._timing(model)
        self.assertIn('0 мс',rendered);self.assertIn('Нет замеров',rendered)
        self.assertIn('0 / 1',rendered)

    def test_empty_projection_has_no_fake_percentiles(self):
        value=status._timing_projection([])
        self.assertEqual(value['events_considered'],0)
        self.assertEqual(value['timed_events'],0)
        self.assertTrue(all(g['p95_ms'] is None for g in value['groups']))


if __name__=='__main__':unittest.main()
