"""Strict synthetic-only scalar projection. No I/O and no private-input permission."""
import re

MAX_CHILD_SECONDS = 35
MAX_RESPONSE_BYTES = 16384
MAX_PAYLOAD_BYTES = 131072
MAX_TRANSPORT_SECONDS = 60
MAX_CLEANUP_SECONDS = 10
FAMILIES = (1, 2, 10, 16)
FALSE = ('privateInputAllowed', 'ASRQualified', 'QwenQualified', 'microphoneQualified',
         'LGBackendBlockedProved', 'GPUOrBrokerPrivacyQualified',
         'filesystemPrivacyQualified', 'forensicRetentionQualified')
BOOLS = ('complete', 'torchVersionMatches', 'cudaVersionMatches',
         'cudaInitSucceeded', 'bootstrapTensorMatched', 'TSYNCZero',
         'policyHashMatches', 'inventoryBeforeVerified', 'inventoryAfterVerified',
         'inventoryFinalVerified', 'postTensorMatched', 'networkDenialsVerified')
NUMBERS = ('driverInitCode', 'deviceCountCode', 'deviceCount', 'sampleSwapBytes',
           'sampleLockedBytes', 'maximumLiveTensorElements')
ERRORS = {'PROBE_UNCONFIRMED', 'MEMORY_UNQUALIFIED', 'GPU_UNQUALIFIED',
          'INVENTORY_UNQUALIFIED', 'TSYNC_UNQUALIFIED', 'POSTSEAL_UNQUALIFIED',
          'NETWORK_UNQUALIFIED', 'DEADLINE_OR_OWNER_DRIFT', 'CHILD_AUTHORITY_MISSING'}
PHASES = {'new', 'memory', 'bootstrap', 'inventory-before', 'tsync',
          'inventory-after', 'tensor-after', 'network', 'inventory-final', 'complete'}


def new(attempt):
    if type(attempt) is not str or re.fullmatch('[a-f0-9]{32}', attempt) is None:
        raise ValueError('ATTEMPT_SCOPE')
    row = {name: False for name in (*FALSE, *BOOLS)}
    row.update({name: None for name in NUMBERS})
    row.update(schema=1, attempt=attempt, phase='new', error=None,
               networkDenials=[], wallMs=0)
    return row


def valid(row):
    if type(row) is not dict or set(row) != set((*FALSE, *BOOLS, *NUMBERS,
                                                'schema', 'attempt', 'phase',
                                                'error', 'networkDenials', 'wallMs')):
        return False
    if type(row['schema']) is not int or row['schema'] != 1:
        return False
    if type(row['attempt']) is not str or re.fullmatch('[a-f0-9]{32}', row['attempt']) is None:
        return False
    if any(row[name] is not False for name in FALSE) or any(type(row[name]) is not bool for name in BOOLS):
        return False
    if (type(row['phase']) is not str or row['phase'] not in PHASES
            or row['error'] is not None and
            (type(row['error']) is not str or row['error'] not in ERRORS)):
        return False
    if type(row['wallMs']) is not int or not 0 <= row['wallMs'] <= 35000:
        return False
    for name in NUMBERS:
        if row[name] is not None and (type(row[name]) is not int or not 0 <= row[name] <= 2**63-1):
            return False
    denials = row['networkDenials']
    if type(denials) is not list or len(denials) > 4:
        return False
    for i, item in enumerate(denials):
        if (type(item) is not dict or set(item) != {'family', 'return', 'errno'}
                or type(item['family']) is not int or item['family'] != FAMILIES[i]
                or type(item['return']) is not int or item['return'] != -1
                or type(item['errno']) is not int or item['errno'] != 1):
            return False
    if row['networkDenialsVerified'] and len(denials) != 4:
        return False
    if row['complete']:
        if (row['phase'] != 'complete' or row['error'] is not None
                or not all(row[name] is True for name in BOOLS)
                or row['driverInitCode'] != 0 or row['deviceCountCode'] != 0
                or type(row['deviceCount']) is not int or row['deviceCount'] < 1
                or row['sampleSwapBytes'] != 0
                or type(row['sampleLockedBytes']) is not int or row['sampleLockedBytes'] <= 0
                or row['maximumLiveTensorElements'] != 48 or len(denials) != 4):
            return False
    return True
