"""Narrow IPv4/IPv6 TCP/8000 ingress guard; never flush any shared chain."""
import argparse,json,os,subprocess

CHAIN='AKME-GW-8000'
COMMENT='akmetron-gateway-8000-v1'
INTERFACE='eth0'

def run(args,check=True):
    if args[0] in {'iptables','ip6tables'}:args=[args[0],'-t','raw',*args[1:]]
    p=subprocess.run(args,capture_output=True,text=True,timeout=10)
    if check and p.returncode:raise RuntimeError('firewall_command_failed:'+args[0])
    return p

def exists(binary,args):
    return run([binary,'-w','5',*args],check=False).returncode==0

def ensure(binary,args):
    check=['-C',*args[1:]] if args[0]=='-A' else None
    if check and exists(binary,check):return
    run([binary,'-w','5',*args])

def hook(binary,chain):
    rule=['-i',INTERFACE,'-p','tcp','--dport','8000','-m','comment','--comment',COMMENT,'-j',CHAIN]
    if not exists(binary,['-C',chain,*rule]):
        run([binary,'-w','5','-I',chain,'1',*rule])
    return rule

def apply(binary):
    if not exists(binary,['-S',CHAIN]):run([binary,'-w','5','-N',CHAIN])
    known=run([binary,'-S',CHAIN]).stdout.strip().splitlines()
    if any(line not in {'-N '+CHAIN,'-A '+CHAIN+' -j DROP'} for line in known):
        raise RuntimeError('private_firewall_chain_has_unexpected_rules')
    # DROP is intentional: no unconditional ESTABLISHED bypass before this guard.
    # Only ingress TCP/8000 from eth0 reaches this private chain; lo is untouched.
    ensure(binary,['-A',CHAIN,'-j','DROP'])
    # Before routing, DNAT and Docker's accept rules. Covers both INPUT and
    # FORWARD, including the Docker<28 loopback-published-port L2 bypass.
    # Docker does not need to be running, so the unit can precede Docker at boot.
    hook(binary,'PREROUTING')

def remove(binary):
    rule=['-i',INTERFACE,'-p','tcp','--dport','8000','-m','comment','--comment',COMMENT,'-j',CHAIN]
    for parent in ['PREROUTING']:
        if exists(binary,['-C',parent,*rule]):run([binary,'-w','5','-D',parent,*rule])
    if exists(binary,['-S',CHAIN]):
        # Only our one exact rule is removed; unexpected rules leave chain intact.
        if exists(binary,['-C',CHAIN,'-j','DROP']):run([binary,'-w','5','-D',CHAIN,'-j','DROP'])
        run([binary,'-w','5','-X',CHAIN],check=False)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['apply','remove','status'])
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('root_required_for_network_guard')
    if not os.path.exists('/sys/class/net/'+INTERFACE):raise RuntimeError('expected_interface_missing')
    for binary in ['iptables','ip6tables']:
        if args.action=='apply':apply(binary)
        elif args.action=='remove':remove(binary)
    rule=['-i',INTERFACE,'-p','tcp','--dport','8000','-m','comment','--comment',COMMENT,'-j',CHAIN]
    active={binary:exists(binary,['-C','PREROUTING',*rule]) and exists(binary,['-C',CHAIN,'-j','DROP']) for binary in ['iptables','ip6tables']}
    if args.action=='apply' and not all(active.values()):raise RuntimeError('firewall_guard_not_active')
    report={'action':args.action,'interface':INTERFACE,'port':8000,'family':'IPv4+IPv6','only_private_chain_modified':True,'active':active}
    print(json.dumps(report))

if __name__=='__main__':main()
