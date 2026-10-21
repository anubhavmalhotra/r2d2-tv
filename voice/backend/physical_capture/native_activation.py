"""No native deployment/activation exists in this offline candidate."""
READY_RECEIPT_SHA=None
FIRST_PHYSICAL_RECEIPT_SHA=None
CURRENT_LIVE_HOST_SOURCE_SHA=None

def assemble(*_args,**_kwargs):
 # Keep refusal before imports, callback invocation, config/receipt reads or I/O.
 raise RuntimeError('PHYSICAL_CONSUMER_UNARMED')

if __name__=='__main__':
 print('{"schema":1,"armed":false,"nativeExecuted":false,"code":"PHYSICAL_CONSUMER_UNARMED"}')
