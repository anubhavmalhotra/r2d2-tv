import threading
import time
import unittest
from types import SimpleNamespace
from channel_adapter import ChannelBrowserAdapter
from browser_spawn import ControlledSpawn


class Manager:
    def __init__(self):
        self.lock=threading.RLock();self.state='idle';self.session=None;self.starts=0
    def status(self):
        with self.lock:return {'state':self.state}
    def start(self):
        with self.lock:self.starts+=1;self.state='starting';return 202,{}


class AdapterDeadlineTests(unittest.TestCase):
    def test_blocked_manager_deadline_never_late_start_or_navigation(self):
        manager=Manager();spawn=ControlledSpawn('/unused/runtime.py');spawn.bind_manager(manager)
        adapter=ChannelBrowserAdapter(manager,spawn,None,viewer=SimpleNamespace(enabled=True),qualified=True)
        adapter.identity='a'*32
        entered=threading.Event();release=threading.Event()
        def hold():
            with manager.lock:entered.set();release.wait(2)
        other=threading.Thread(target=hold);other.start();entered.wait(1)
        try:
            for call in (lambda:adapter.launch_browser('a'*32+':start',timeout_s=.06),
                         lambda:adapter.browser_observation(timeout_s=.06),
                         lambda:adapter.navigate('b'*32,'https://www.youtube.com','c'*32+':navigate',timeout_s=.06)):
                before=time.monotonic();call();self.assertLess(time.monotonic()-before,.25)
        finally:release.set();other.join(1)
        time.sleep(.02)
        self.assertEqual(manager.starts,0);self.assertEqual(manager.state,'idle');self.assertEqual(spawn.clients,{})

    def test_default_disabled_does_not_wait_on_manager(self):
        manager=Manager();spawn=ControlledSpawn('/unused/runtime.py');spawn.bind_manager(manager)
        adapter=ChannelBrowserAdapter(manager,spawn,None)
        self.assertEqual(adapter.launch_browser('a'*32+':start',timeout_s=1),'rejected')
        self.assertIsNone(adapter.browser_observation(timeout_s=1))
        self.assertEqual(manager.starts,0)


if __name__=='__main__':unittest.main()
