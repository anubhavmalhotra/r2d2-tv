"""Opt-in TV handoff through the existing fixed TinyBrowser launch contract.

The caller supplies a reviewed, bounded local TV launch primitive. This module
creates no network client, obtains no grant and logs no payload. Launch acceptance
is not evidence of a displayed frame. Session fragments are cleared by /open.
"""
import ipaddress
import math
import re
import threading
import time


class TVViewerHandoff:
    def __init__(self, spark_ip, manager_key, launch, *, qualified=False):
        try:
            address = ipaddress.IPv4Address(spark_ip)
            valid_ip = (str(address) == spark_ip and any(address in ipaddress.IPv4Network(network)
                        for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '192.0.2.0/24')))
        except (ValueError, TypeError):
            valid_ip = False
        if not valid_ip or type(manager_key) is not str or not re.fullmatch('[a-f0-9]{64}', manager_key):
            raise ValueError('VIEWER_CONFIG')
        self.origin = 'http://' + spark_ip
        self.open_url = self.origin + ':4181/' + manager_key + '/open'
        self.viewer_pattern = re.compile(re.escape(self.origin + ':4177/') + '[a-f0-9]{48}/viewer')
        self.launch = launch
        self.enabled = qualified is True and callable(launch)
        self.operations = set()
        self.lock = threading.Lock()

    def open(self, session, operation, *, viewer_url=None, timeout_s):
        if (not self.enabled or type(session) is not str or not re.fullmatch('[a-f0-9]{32}', session) or
                type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:start', operation) or
                type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 5 or
                (viewer_url is not None and (type(viewer_url) is not str or not self.viewer_pattern.fullmatch(viewer_url)))):
            return 'rejected'
        end = time.monotonic() + timeout_s
        if not self.lock.acquire(timeout=max(0, end-time.monotonic())):
            return 'rejected'
        try:
            if operation in self.operations or len(self.operations) >= 256 or time.monotonic() >= end:
                return 'rejected'
            self.operations.add(operation)
            # Fragment is not an HTTP request parameter. /open clears it before
            # its first fetch and may claim only this exact already-created id.
            target = viewer_url or self.open_url + '#voice-session=' + session
            try:
                result = self.launch({'id': 'com.webos.app.tinybrowser',
                                      'params': {'contentTarget': target}},
                                     timeout_s=end-time.monotonic())
            except Exception:
                return 'unknown'
            finally:
                target = None
            if time.monotonic() >= end:
                return 'unknown'
            if type(result) is dict and result.get('returnValue') is True:
                return 'accepted'
            if type(result) is dict and result.get('returnValue') is False:
                return 'rejected'
            return 'unknown'
        finally:
            self.lock.release()
