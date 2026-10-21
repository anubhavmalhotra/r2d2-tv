"""Technical startup errors before audio input; never use for inference failures."""
from pathlib import Path
import re

def load_diagnostic(error):
    """Authorized public model construction only; never after ASR receives PCM."""
    name=type(error).__name__
    if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,79}',name):name='UnrecognizedExceptionClass'
    message=''.join(c for c in str(error)[:2048] if c in '\n\t' or 32<=ord(c)<127)
    message=re.sub(r'(?i)((?:password|authorization|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*)[^\s,;]+',r'\1[redacted]',message)
    frames=[];tb=error.__traceback__
    while tb is not None:
        code=tb.tb_frame.f_code
        module=Path(code.co_filename).name[:120];function=code.co_name[:120]
        if not re.fullmatch('[A-Za-z0-9_.<>-]{1,120}',module):module='other'
        if not re.fullmatch('[A-Za-z0-9_.<>-]{1,120}',function):function='other'
        frames.append({'module':module,'function':function,'line':min(1000000,max(1,tb.tb_lineno))})
        frames=frames[-8:];tb=tb.tb_next
    return {'exceptionClass':name,'message':message,'frames':frames,'localsOrEnvironmentRead':False,'audioProcessed':False}


def validate_diagnostic(value):
    if value is None:return None
    if(type(value)is not dict or set(value)!={'exceptionClass','message','frames','localsOrEnvironmentRead','audioProcessed'}
       or type(value['exceptionClass'])is not str or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,79}',value['exceptionClass'])
       or type(value['message'])is not str or len(value['message'])>2048
       or any(c not in '\n\t' and not 32<=ord(c)<127 for c in value['message'])
       or value['localsOrEnvironmentRead']is not False or value['audioProcessed']is not False
       or type(value['frames'])is not list or len(value['frames'])>8):raise ValueError('PUBLIC_LOAD_DIAGNOSTIC')
    for frame in value['frames']:
        if(type(frame)is not dict or set(frame)!={'module','function','line'}or type(frame['line'])is not int or not 1<=frame['line']<=1000000
           or any(type(frame[k])is not str or not re.fullmatch('[A-Za-z0-9_.<>-]{1,120}',frame[k])for k in ('module','function'))):raise ValueError('PUBLIC_LOAD_DIAGNOSTIC')
    return value

