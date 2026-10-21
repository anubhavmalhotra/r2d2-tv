"""No target listener, hardware reader, provider or model is installed here."""
READY_RECEIPT_SHA = None
FIRST_PHYSICAL_RECEIPT_SHA = None
GLOBAL_ROUTE_QUALIFIED = False
CONTROLLED_CAPTURE_QUALIFIED = False

def assemble(*args, **kwargs):
    raise RuntimeError('REMOTE_LIFECYCLE_NATIVE_UNARMED')

if __name__ == '__main__':
    print('REMOTE_LIFECYCLE_NATIVE_UNARMED')
