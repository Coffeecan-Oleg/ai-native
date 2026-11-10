#!/usr/bin/python3
"""One-time DEV identity setup. Secrets are entered only in the server TTY."""
import getpass,json,re,sys,time,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import development as dev
from dev_main import engine
from release_control.core import Refused,atomic,encoded,read,sha
from release_control.linux import file_sha,write_bytes
BASE=Path('/opt/platforma-dev')
KEYS=['GATEWAY_BITRIX_CLIENT_ID','GATEWAY_BITRIX_CLIENT_SECRET','GATEWAY_BITRIX_MEMBER_ID','GATEWAY_BITRIX_EMBED_ENABLED']

def validate_fields(fields):
    if set(fields)!={'client_id','client_secret','member_id'}:raise Refused('development_credentials_fields')
    if not re.fullmatch(r'local\.[a-zA-Z0-9.]{8,120}',fields['client_id']):raise Refused('development_client_id_format')
    secret=fields['client_secret']
    if not 16<=len(secret)<=512 or any(c.isspace() for c in secret):raise Refused('development_secret_format')
    if fields['member_id'] and not re.fullmatch('[a-f0-9]{32}',fields['member_id']):raise Refused('development_member_format')

def render_env(current,fields):
    validate_fields(fields)
    lines=[line for line in current.splitlines() if line.split('=',1)[0] not in KEYS]
    values=[fields['client_id'],fields['client_secret'],fields['member_id'],'true']
    return '\n'.join(lines+[k+'='+v for k,v in zip(KEYS,values)])+'\n'

def public_ready(ctl):
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        values=[]
        for path in ['/healthz','/.well-known/oauth-authorization-server']:
            with opener.open(dev.PUBLIC_URL+path,timeout=8) as response:
                if response.status!=200:raise Refused('development_public_http_unavailable')
                raw=response.read(65537)
                if len(raw)>65536:raise Refused('development_public_response_limit')
                values.append(json.loads(raw))
        local=ctl.b.health()
        if not local['ok'] or values[0].get('ok') is not True or values[0].get('auth_enabled') is not True or values[1].get('issuer')!=dev.PUBLIC_URL or values[0].get('release_sha')!=local['release_sha']:
            raise Refused('development_public_route_mismatch')
    except Refused:raise
    except Exception:raise Refused('development_public_https_not_ready') from None

def fresh(base,ctl):
    if ctl.s.active():raise Refused('active_release')
    receipt=read(base/'bootstrap.json')
    if receipt.get('public_auth')!='pending':raise Refused('first_time_development_setup_only')
    for line in (base/'app/.env').read_text().splitlines():
        if line.startswith('GATEWAY_BITRIX_CLIENT_ID=') and line.split('=',1)[1]:raise Refused('development_already_configured')

def stage(base,fields):
    validate_fields(fields);ctl=engine(base)
    with ctl.s.lock():
        fresh(base,ctl);atomic(base/'config/bitrix.pending.json',fields)
    return {'saved':True,'activated':False,'secret_values_returned':False}

def activation_plan(base):
    ctl=engine(base);fresh(base,ctl)
    fields=read(base/'config/bitrix.pending.json');validate_fields(fields);public_ready(ctl)
    payload={'pending_sha256':file_sha(base/'config/bitrix.pending.json'),'env_sha256':file_sha(base/'app/.env'),
      'caddy_sha256':file_sha(base/'edge/platforma.caddy'),'public_url':dev.PUBLIC_URL,
      'changed_keys':KEYS,'expected_scope':['user_brief'],'database_restore':False,
      'created':time.time(),'expires':time.time()+1800}
    return {'payload':payload,'sha256':sha(encoded(payload))}

def activate(base,entry,digest):
    ctl=engine(base)
    with ctl.s.lock():
        p=entry['payload']
        if digest!=entry['sha256'] or digest!=sha(encoded(p)) or time.time()>p['expires']:raise Refused('development_auth_plan_invalid')
        actual=activation_plan(base)['payload']
        if {k:v for k,v in actual.items() if k not in ['created','expires']}!={k:v for k,v in p.items() if k not in ['created','expires']}:
            raise Refused('development_auth_baseline_changed')
        fields=read(base/'config/bitrix.pending.json')
        old_env=(base/'app/.env').read_bytes();old_caddy=(base/'edge/platforma.caddy').read_bytes()
        backup=base/'config/auth-before';backup.mkdir(mode=0o700)
        write_bytes(backup/'env',old_env);write_bytes(backup/'platforma.caddy',old_caddy)
        ctl.b.compose_run('stop','gateway',timeout=60)
        write_bytes(base/'app/.env',render_env(old_env.decode(),fields).encode())
        ctl.b.compose_run('up','-d','--no-deps','gateway',timeout=90)
        end=time.monotonic()+90
        while time.monotonic()<end:
            if ctl.b.health()['ok']:break
            time.sleep(1)
        else:raise Refused('development_auth_health_failed_gate_pending')
        receipt=read(base/'bootstrap.json');receipt['public_auth']='opening';atomic(base/'bootstrap.json',receipt)
        try:
            write_bytes(base/'edge/platforma.caddy',b':8080 {\n reverse_proxy gateway:8000\n}\n')
            ctl.b.caddy_command('validate');ctl.b.caddy_command('reload');public_ready(ctl)
        except Exception:
            write_bytes(base/'edge/platforma.caddy',old_caddy)
            try:ctl.b.caddy_command('validate');ctl.b.caddy_command('reload')
            except Exception:pass
            receipt['public_auth']='attention_required';atomic(base/'bootstrap.json',receipt)
            raise Refused('development_auth_opening_requires_review') from None
        receipt['public_auth']='configured_awaiting_owner_login';atomic(base/'bootstrap.json',receipt)
        return {'configured':True,'owner_login_verified':False,'database_restored':False}

def main():
    args=sys.argv[1:]
    if args==['stage']:
        dev.require_host()
        if not sys.stdin.isatty():raise Refused('development_credentials_require_ssh_tty')
        fields={'client_id':getpass.getpass('DEV Bitrix Client ID (hidden): ').strip(),
          'client_secret':getpass.getpass('DEV Bitrix Client Secret (hidden): ').strip(),
          'member_id':getpass.getpass('DEV member_id, optional (hidden): ').strip()}
        if fields['client_secret']!=getpass.getpass('Repeat DEV Client Secret (hidden): ').strip():raise Refused('development_secret_mismatch')
        value=stage(BASE,fields)
    elif args==['plan']:value=activation_plan(BASE)
    elif len(args)==3 and args[0]=='apply':value=activate(BASE,read(args[1]),args[2])
    else:raise Refused('development_credentials_command_not_allowed')
    print(json.dumps(value))

if __name__=='__main__':
    try:main()
    except Exception as ex:
        print(json.dumps({'ok':False,'reason':str(ex) if isinstance(ex,Refused) else 'development_credentials_internal_error'}));raise SystemExit(1)
