"""Bounded text comparison of two already-authorized immutable skill snapshots."""
import difflib
from gateway_mcp.services import skill_lifecycle as lifecycle

MAX_FILE_BYTES=16384
MAX_FILE_LINES=200
MAX_INPUT_LINES=1600
MAX_OUTPUT_LINES=600
MAX_OUTPUT_CHARS=60000


def compare(current,previous=None,*,basis=None):
    """No database, filesystem, network, code execution or permission changes."""
    lifecycle._verify(current)
    result={'current_revision':current['revision'],'current_version':current['version'],
        'current_fingerprint':current['fingerprint'],'previous_revision':None,'previous_version':None,'previous_fingerprint':None,
        'basis':None,'metadata':[],'files':[],'counts':{'added':0,'removed':0,'changed':0,'unchanged':0},
        'omitted_content':False,'input_lines':0,'output_lines':0,'output_chars':0,'read_only':True}
    if previous is None:return result
    lifecycle._verify(previous)
    if not current.get('skill_id') or current['skill_id']!=previous.get('skill_id'):
        raise ValueError('Можно сравнить только редакции одного навыка.')
    if type(previous['revision']) is not int or type(current['revision']) is not int or previous['revision']>=current['revision']:
        raise ValueError('Сравнение требует более раннюю сохранённую редакцию.')
    if basis not in {'published','previous_saved'}:raise ValueError('Неверное основание сравнения.')
    result.update(previous_revision=previous['revision'],previous_version=previous['version'],
                  previous_fingerprint=previous['fingerprint'],basis=basis)
    for field in ['title','description','version']:
        if previous[field]!=current[field]:result['metadata'].append({'field':field,'before':previous[field],'after':current[field]})
    for path in sorted(set(previous['files'])|set(current['files'])):
        before=previous['manifest'].get(path);after=current['manifest'].get(path)
        change='added' if before is None else 'removed' if after is None else 'unchanged' if before['sha256']==after['sha256'] else 'changed'
        result['counts'][change]+=1
        if change=='unchanged':continue
        entry={'path':path,'change':change,'before_sha256':before['sha256'] if before else None,
               'after_sha256':after['sha256'] if after else None,'diff':[],'diff_status':'complete'}
        result['files'].append(entry)
        if any(spec and spec['bytes']>MAX_FILE_BYTES for spec in [before,after]):
            entry['diff_status']='input_limit';result['omitted_content']=True;continue
        left=previous['files'].get(path,'').splitlines();right=current['files'].get(path,'').splitlines()
        if max(len(left),len(right))>MAX_FILE_LINES or result['input_lines']+len(left)+len(right)>MAX_INPUT_LINES:
            entry['diff_status']='input_limit';result['omitted_content']=True;continue
        result['input_lines']+=len(left)+len(right)
        if left==right:
            entry['diff_status']='line_endings_only';continue
        # Inputs and aggregate output are bounded before displaying any text.
        for index,line in enumerate(difflib.unified_diff(left,right,fromfile='previous',tofile='current',n=3,lineterm='')):
            if index<2:continue
            if result['output_lines']>=MAX_OUTPUT_LINES or result['output_chars']+len(line)>MAX_OUTPUT_CHARS:
                entry['diff_status']='output_limit';result['omitted_content']=True;break
            kind='added' if line.startswith('+') else 'removed' if line.startswith('-') else 'hunk' if line.startswith('@@') else 'context'
            entry['diff'].append({'kind':kind,'text':line})
            result['output_lines']+=1;result['output_chars']+=len(line)
    return result
