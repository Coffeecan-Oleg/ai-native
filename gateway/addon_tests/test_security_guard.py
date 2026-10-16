import unittest
from copy import deepcopy
from gateway_mcp.services.security_guard import authorize, fingerprint

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.p={'status':'active','subject':'employee','project':'project-a','expires_at':None,
                'approver_subject':'owner','sending_mailbox':'employee@example.invalid',
                'protect_existing_skills':True,'resources':[
                {'id':'doc-a','kind':'document','operations':['read','create','update']},
                {'id':'skill-a','kind':'skill','operations':['read','update']},
                {'id':'crm-a','kind':'crm','operations':['create','update']},
                {'id':'employee@example.invalid','kind':'mail','operations':['send']}]}
    def check(self, **kw):
        args=dict(subject='employee',project='project-a',resource='doc-a',operation='read',resource_kind='document')
        args.update(kw);return authorize(self.p,**args)
    def test_allowed(self): self.assertTrue(self.check().allowed)
    def test_foreign_project(self): self.assertFalse(self.check(project='project-b').allowed)
    def test_foreign_subject(self): self.assertFalse(self.check(subject='owner').allowed)
    def test_unknown_resource(self): self.assertFalse(self.check(resource='doc-b').allowed)
    def test_no_delete(self): self.assertFalse(self.check(operation='delete').allowed)
    def test_kind_spoof(self): self.assertFalse(self.check(resource_kind='skill').allowed)
    def test_existing_skill(self): self.assertFalse(self.check(resource='skill-a',resource_kind='skill',operation='update').allowed)
    def test_crm_unknown_impact(self): self.assertFalse(self.check(resource='crm-a',resource_kind='crm',operation='create').allowed)
    def test_crm_safe(self): self.assertTrue(self.check(resource='crm-a',resource_kind='crm',operation='create',skill_impact='none').allowed)
    def test_crm_requires_approval(self): self.assertFalse(self.check(resource='crm-a',resource_kind='crm',operation='create',skill_impact='affects_skills').allowed)
    def test_bound_approval(self):
        payload={'new_field':'area'}
        digest=fingerprint('employee','project-a','crm-a','create',payload)
        verify=lambda d,a:d==digest and a=='owner'
        args=dict(resource='crm-a',resource_kind='crm',operation='create',skill_impact='affects_skills',approval_check=verify)
        self.assertTrue(self.check(payload=payload,**args).allowed)
        self.assertFalse(self.check(payload={'new_field':'different'},**args).allowed)
    def test_revoke(self):
        self.p['status']='revoked';self.assertFalse(self.check().allowed)
    def test_expired(self):
        self.p['expires_at']='2000-01-01T00:00:00Z';self.assertFalse(self.check().allowed)
    def test_invalid_date(self):
        self.p['expires_at']='tomorrow';self.assertFalse(self.check().allowed)
    def test_owner_mail(self): self.assertFalse(self.check(resource='owner@example.invalid',resource_kind='mail',operation='send').allowed)
    def test_employee_mail(self): self.assertTrue(self.check(resource='employee@example.invalid',resource_kind='mail',operation='send').allowed)

if __name__=='__main__': unittest.main()
