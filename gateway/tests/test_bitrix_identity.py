"""Synthetic identity and authorization boundary tests; never contact a portal."""
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from gateway_mcp.services import bitrix_identity as identity, corporate_access as access, policy, storage
RAW_REQUEST=identity._request

MEMBER='a'*32
SUBJECT='bitrix24:'+MEMBER+':42'
TOKENS={'access_token':'synthetic-access-token','refresh_token':'synthetic-refresh-token',
        'member_id':MEMBER,'expires_at':time.time()+3600}
RESPONSE={'access_token':TOKENS['access_token'],'refresh_token':TOKENS['refresh_token'],
          'member_id':MEMBER,'expires_in':3600,'client_endpoint':'https://akmetron.bitrix24.ru/rest/','scope':'user_brief'}
ENV={'GATEWAY_LOGIN_PROVIDER':'bitrix24','GATEWAY_BITRIX_PORTAL':'akmetron.bitrix24.ru',
     'GATEWAY_BITRIX_CLIENT_ID':'local.synthetic.12345678','GATEWAY_BITRIX_CLIENT_SECRET':'synthetic-application-secret',
     'GATEWAY_BITRIX_MEMBER_ID':MEMBER,'GATEWAY_BITRIX_OWNER_USER_ID':'3318',
     'GATEWAY_BITRIX_UNISENDER_USER_IDS':'','GATEWAY_BITRIX_DENIED_USER_IDS':''}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,ENV))
        identity._CACHE.clear()
        self.row={'ID':'42','ACTIVE':True,'USER_TYPE':'employee','UF_DEPARTMENT':[1],
                  'NAME':'Synthetic','LAST_NAME':'Employee','EMAIL':'not-retained@example.invalid','PERSONAL_BIRTHDAY':'not-retained'}
        self.requests=[]
        def request(url,data):
            self.requests.append((url,copy.deepcopy(data)))
            if url.endswith('/scope.json'): return {'result':['user_brief']}
            if 'user.current' in url: return {'result':{'ID':'42','ACTIVE':True}}
            if 'user.get' in url: return {'result':[self.row]}
            return RESPONSE
        self.request=self.enterContext(patch.object(identity,'_request',side_effect=request))

    def test_only_own_portal_and_server_member_are_accepted(self):
        self.assertEqual(identity._validate_tokens(RESPONSE)['member_id'],MEMBER)
        for change in ({'member_id':'b'*32},{'client_endpoint':'https://foreign.bitrix24.ru/rest/'},
                       {'client_endpoint':'https://akmetron.bitrix24.ru.attacker.invalid/rest/'},
                       {'member_id':''}):
            with self.subTest(change=change),self.assertRaises(PermissionError):identity._validate_tokens(RESPONSE|change)

    def test_user_scope_is_readonly_minimum_and_not_crm(self):
        for scope in ['user','user_basic','user_brief,crm','','user_brief,user']:
            with self.subTest(scope=scope),self.assertRaises(PermissionError):identity._validate_tokens(RESPONSE|{'scope':scope})

    def test_minimum_scope_normalizes_only_its_representation(self):
        for scope in [' user_brief ', 'user_brief,', ['user_brief']]:
            with self.subTest(scope=scope):self.assertEqual(identity._validate_tokens(RESPONSE|{'scope':scope})['member_id'],MEMBER)
        for scope in [['user_brief','crm'], ['user_brief',1], {'user_brief':True}, None]:
            with self.subTest(scope=scope),self.assertRaises(PermissionError):identity._validate_tokens(RESPONSE|{'scope':scope})

    def test_authorization_base_scope_requires_verified_portal_permissions(self):
        self.assertEqual(identity._validate_tokens(RESPONSE|{'scope':'app'})['member_id'],MEMBER)
        scope_call=[data for url,data in self.requests if url.endswith('/scope.json')][-1]
        self.assertNotIn('full',scope_call)
        for claimed in ['app','user_brief']:
            for granted in [[], ['app'], ['user'], ['user_brief','crm'], ['user_brief',1], {'user_brief':True}, None]:
                with self.subTest(claimed=claimed,granted=granted),patch.object(identity,'_request',return_value={'result':granted}),self.assertRaises(PermissionError):
                    identity._validate_tokens(RESPONSE|{'scope':claimed})

    def test_active_employee_is_resolved_by_own_token(self):
        employee=identity.read_employee(TOKENS)
        self.assertEqual(employee['subject'],SUBJECT)
        self.assertEqual(self.requests[-1][1]['FILTER[ID]'],'42')
        self.assertNotIn('EMAIL',employee);self.assertNotIn('PERSONAL_BIRTHDAY',employee)
        self.assertEqual({url.split('/rest/')[-1] for url,_ in self.requests},{'user.current.json','user.get.json'})

    def test_all_external_bot_and_unknown_types_fail_closed(self):
        for kind in ['extranet','email','bot','collaber','unknown',None]:
            self.row['USER_TYPE']=kind
            with self.subTest(kind=kind),self.assertRaises(PermissionError):identity.read_employee(TOKENS)

    def test_dismissed_and_missing_status_are_rejected(self):
        for active in [False,'N','true',1,None]:
            self.row['ACTIVE']=active
            with self.subTest(active=active),self.assertRaises(PermissionError):identity.read_employee(TOKENS)

    def test_ignored_filters_cannot_admit_different_user(self):
        self.row['ID']='43'
        with self.assertRaises(PermissionError):identity.read_employee(TOKENS)

    def test_missing_zero_or_invalid_department_is_rejected(self):
        for department in [None,[],[0],['0'],[False],['not-an-id']]:
            self.row['UF_DEPARTMENT']=department
            with self.subTest(department=department),self.assertRaises(PermissionError):identity.read_employee(TOKENS)

    def test_callback_member_mismatch_does_not_persist_credentials(self):
        with patch.object(identity,'_save') as save:
            with self.assertRaises(PermissionError):identity.complete_login('fixture-code','b'*32)
            save.assert_not_called()

    def test_credentials_are_passed_only_to_encrypted_storage(self):
        with patch.object(storage,'save_user_oauth_token',return_value=True) as save:
            employee=identity.complete_login('fixture-code',MEMBER)
            args=save.call_args.kwargs
            self.assertEqual(args['provider'],'bitrix24-login')
            self.assertEqual(args['actor_subject'],SUBJECT)
            self.assertEqual(json.loads(args['access_token'])['refresh_token'],TOKENS['refresh_token'])
            self.assertNotIn(TOKENS['refresh_token'],json.dumps(args['metadata']))
            self.assertEqual(args['email'],'')

    def test_revoked_credential_denies_even_with_fresh_cache(self):
        identity._CACHE[SUBJECT]={'subject':SUBJECT,'verified_at':time.time()}
        with patch.object(storage,'get_user_oauth_token',return_value=None):
            with self.assertRaises(PermissionError):identity.current_employee(SUBJECT)

    def test_stale_membership_cannot_survive_api_failure(self):
        identity._CACHE[SUBJECT]={'subject':SUBJECT,'verified_at':time.time()-61}
        record={'access_token':json.dumps(TOKENS),'scopes':['user_brief']}
        with patch.object(storage,'get_user_oauth_token',return_value=record),patch.object(identity,'read_employee',side_effect=PermissionError('unavailable')):
            with self.assertRaises(PermissionError):identity.current_employee(SUBJECT)
        self.assertNotIn(SUBJECT,identity._CACHE)

    def test_browser_state_is_bound_and_redirects_are_local(self):
        self.assertTrue(identity.browser_state_matches('x'*32,'x'*32))
        for state,cookie in [('x'*32,''),('x'*32,'y'*32),('short','short')]:
            self.assertFalse(identity.browser_state_matches(state,cookie))
        for target in ['https://attacker.invalid','//attacker.invalid','/\\attacker','bad','/\r\nbad']:
            self.assertEqual(identity.safe_next(target),'/admin')

    def test_upstream_error_never_returns_raw_tokens(self):
        raw_client=httpx.Client
        def factory(**kwargs):
            self.assertFalse(kwargs['follow_redirects']);self.assertFalse(kwargs['trust_env'])
            return raw_client(transport=httpx.MockTransport(lambda r:httpx.Response(400,json={'error':TOKENS['access_token']})),**kwargs)
        with patch.object(httpx,'Client',side_effect=factory):
            with self.assertRaisesRegex(PermissionError,'^bitrix_upstream_rejected$'):RAW_REQUEST(identity.TOKEN_URL,{'secret':'synthetic-secret'})

    def test_access_log_strips_callback_code_and_state(self):
        record=logging.LogRecord('uvicorn.access',20,'',0,'%s %s %s %s %s',('client','GET','/auth/bitrix24/callback?code=private&state=private','1.1',303),None)
        identity.SafeAccessLog().filter(record)
        self.assertNotIn('private',record.getMessage())


class CorporateAccessTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,ENV))
        self.employee={'subject':SUBJECT,'user_id':'42','member_id':MEMBER,'name':'Synthetic Employee','verified_at':time.time()}
        self.current=self.enterContext(patch.object(identity,'current_employee',return_value=self.employee))
        self.grants=self.enterContext(patch.object(storage,'list_active_scope_grants',return_value=[]))
        self.resources=self.enterContext(patch.object(storage,'list_active_resource_grants',return_value=[]))

    def test_employee_automatically_gets_only_baseline(self):
        actor=access.fresh_actor(SUBJECT)
        self.assertEqual(set(actor.scopes),access.BASE_SCOPES)
        for scope in ['*','access:admin','memory:read','memory:write','unisender:read','crm:write']:
            self.assertNotIn(scope,actor.scopes)

    def test_forged_or_old_admin_claims_and_email_aliases_do_not_expand(self):
        actor=policy.actor_from_claims({'sub':SUBJECT,'groups':['admins'],'scope':'* access:admin memory:admin',
                                      'email':'owner@example.invalid','yandex_id':'old-owner'})
        self.assertEqual(set(actor.scopes),access.BASE_SCOPES)
        self.grants.assert_called_with([('user',SUBJECT)])
        self.assertEqual(actor.email,'');self.assertEqual(actor.yandex_id,'');self.assertEqual(actor.groups,())

    def test_old_yandex_tokens_are_rejected_in_corporate_mode(self):
        with self.assertRaisesRegex(PermissionError,'corporate_bitrix_login_required'):
            policy.actor_from_claims({'sub':'yandex:fixture','scope':'*'})

    def test_exact_owner_browser_role_does_not_enter_mcp(self):
        owner='bitrix24:'+MEMBER+':3318'
        actor=access.fresh_actor(owner)
        self.assertIn('access:admin',actor.scopes)
        delegated=access.delegated(actor)
        self.assertNotIn('access:admin',delegated.scopes)
        self.assertNotIn('*',delegated.scopes)
        self.assertIsNotNone(delegated.tool_allowlist)

    def test_real_unisender_requires_explicit_user_list(self):
        self.assertNotIn('unisender:read',access.fresh_actor(SUBJECT).scopes)
        with patch.dict(os.environ,{'GATEWAY_BITRIX_UNISENDER_USER_IDS':'42'}):
            self.assertIn('unisender:read',access.fresh_actor(SUBJECT).scopes)

    def test_gateway_ban_and_stale_employee_fail_closed(self):
        with patch.dict(os.environ,{'GATEWAY_BITRIX_DENIED_USER_IDS':'42'}),self.assertRaises(PermissionError):access.fresh_actor(SUBJECT)
        self.current.side_effect=PermissionError('dismissed')
        with self.assertRaises(PermissionError):access.fresh_actor(SUBJECT)

    def test_database_deny_is_kept_but_allow_cannot_grant_admin(self):
        self.grants.return_value=[{'effect':'allow','scope':'*'},{'effect':'deny','scope':'platforma:write'}]
        actor=access.fresh_actor(SUBJECT)
        self.assertNotIn('*',actor.scopes);self.assertNotIn('platforma:write',actor.scopes)
        self.grants.return_value=[{'effect':'deny','scope':'*'}]
        with self.assertRaises(PermissionError):access.fresh_actor(SUBJECT)

    def test_old_scope_ceiling_and_tool_allowlist_cannot_widen_on_refresh(self):
        actor=access.fresh_actor(SUBJECT,ceiling=('skills:read',),tools=('gateway_corporate_skill_list',))
        delegated=access.delegated(actor)
        self.assertEqual(delegated.scopes,('skills:read',))
        self.assertEqual(delegated.tool_allowlist,('gateway_corporate_skill_list',))

    def test_shared_profile_excludes_private_owner_documents_and_memory(self):
        from gateway_mcp.services.platforma_onboarding import DOCUMENTS
        template={'documents':{access.SPACE:{p:{} for p in DOCUMENTS}|{'owner/private.md':{}}},'skill_telemetry':{}}
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'profiles.json';f.write_text(json.dumps({'profiles':{'legacy':template}}))
            with patch.dict(os.environ,{'GATEWAY_ADDON_PROFILES_FILE':str(f),'GATEWAY_PLATFORMA_OWNER_SUBJECT':'legacy'}):
                profile=access.profile(access.fresh_actor(SUBJECT))
        self.assertEqual(set(profile['documents'][access.SPACE]),set(DOCUMENTS))
        self.assertNotIn('mcp:gateway_memory_search',{r['id'] for r in profile['resources']})
        from gateway_mcp.services import addon_guard
        # Exercise the actual dispatch boundary, not only profile field contents.
        addon_guard._check(profile, access.fresh_actor(SUBJECT),
            'mcp:gateway_access_profile', 'tool', 'read')

    def test_only_own_pages_are_available_to_employee(self):
        actor=access.fresh_actor(SUBJECT)
        for path in ['/admin','/platforma/skills','/admin/audit']:
            self.assertTrue(access.web_path_allowed(actor,path))
        for path in ['/platforma/requests','/admin/access','/credentials','/admin/users','/admin/platforma-readers']:
            self.assertFalse(access.web_path_allowed(actor,path))

    def test_routes_are_finite_and_resource_denies_win(self):
        with patch.dict(os.environ,{'GATEWAY_BITRIX_UNISENDER_USER_IDS':'42'}):
            actor=access.fresh_actor(SUBJECT)
        route={'name':'unisender.getLists','backend':'unisender','transport':'unisender-readonly','scope':'unisender:read','http_method':'GET'}
        # Fresh policy is checked again for each call, not cached grants.
        self.enterContext(patch.dict(os.environ,{'GATEWAY_BITRIX_UNISENDER_USER_IDS':'42'}))
        self.assertEqual(access.require_route_access(actor,route,{})['decision'],'allow')
        for change in [{'name':'bitrix24.user.get'},{'name':'unisender.sendEmail'},{'transport':'infra'},{'http_method':'DELETE'}]:
            with self.subTest(change=change),self.assertRaises(PermissionError):access.require_route_access(actor,route|change,{})
        self.resources.return_value=[{'effect':'deny','actions':['read'],'resource_pattern':'*'}]
        with self.assertRaises(PermissionError):access.require_route_access(actor,route,{})

    def test_corporate_help_describes_bitrix_and_separate_real_service_access(self):
        from gateway_mcp.routes.platforma_guide import content
        docs=content()['documents']
        self.assertNotIn('Яндекс',docs['memo']+docs['message'])
        self.assertIn('Базовый доступ получает',docs['memo'])
        self.assertIn('Отправка не подключена',docs['message'])


if __name__=='__main__':unittest.main()
