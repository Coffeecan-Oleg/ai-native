"""Generate, but never activate, a native user scheduler configuration."""
import argparse,json,plistlib,shlex,subprocess,sys
from pathlib import Path
from xml.etree import ElementTree as ET
def prepare(os_name,python,script,config,dest):
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    args=[python,script,'--config',config]
    if os_name=='macos':
        p=dest/'org.gateway.usage-export.plist'
        p.write_bytes(plistlib.dumps({'Label':'org.gateway.usage-export','ProgramArguments':args,'StartInterval':300,'RunAtLoad':True,'WorkingDirectory':str(Path(script).parent),'StandardOutPath':str(dest/'usage-export.log'),'StandardErrorPath':str(dest/'usage-export-error.log')}))
    elif os_name=='linux':
        def quote(s):return '"'+s.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
        p=dest/'gateway-usage-export.service';p.write_text('[Unit]\nDescription=Gateway numeric usage export\n[Service]\nType=oneshot\nExecStart='+ ' '.join(quote(x) for x in args)+'\n',encoding='utf-8')
        (dest/'gateway-usage-export.timer').write_text('[Unit]\nDescription=Gateway usage export every five minutes\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec=5min\nPersistent=true\n[Install]\nWantedBy=timers.target\n',encoding='utf-8')
    elif os_name=='windows':
        ns='http://schemas.microsoft.com/windows/2004/02/mit/task';ET.register_namespace('',ns)
        task=ET.Element('{'+ns+'}Task',version='1.2')
        def child(parent,name,text=None,**attrs):
            e=ET.SubElement(parent,'{'+ns+'}'+name,attrs);e.text=text;return e
        trigger=child(child(task,'Triggers'),'TimeTrigger');child(trigger,'StartBoundary','2020-01-01T00:00:00');rep=child(trigger,'Repetition');child(rep,'Interval','PT5M');child(rep,'StopAtDurationEnd','false');child(trigger,'Enabled','true')
        principal=child(child(task,'Principals'),'Principal',id='Author');child(principal,'LogonType','InteractiveToken');child(principal,'RunLevel','LeastPrivilege')
        settings=child(task,'Settings');child(settings,'MultipleInstancesPolicy','IgnoreNew');child(settings,'StartWhenAvailable','true');child(settings,'ExecutionTimeLimit','PT3M')
        action=child(child(task,'Actions',Context='Author'),'Exec');child(action,'Command',python);child(action,'Arguments',subprocess.list2cmdline(args[1:]));child(action,'WorkingDirectory',str(Path(script).parent))
        p=dest/'gateway-usage-export.xml';ET.ElementTree(task).write(p,encoding='utf-16',xml_declaration=True)
    else:raise ValueError('Unknown OS')
    return str(p)
if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--os',choices=['macos','linux','windows'],required=True);a.add_argument('--python',default=sys.executable);a.add_argument('--script',required=True);a.add_argument('--config',required=True);a.add_argument('--output',required=True);v=a.parse_args();print(prepare(v.os,v.python,v.script,v.config,v.output))
