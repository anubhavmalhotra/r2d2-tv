from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nightly_maintenance as nm


class NightlyMaintenanceTests(unittest.TestCase):
    def _sample_mac(self, last="01"):
        return ":".join(["02", "00", "00", "00", "00", last])

    def test_wol_magic_packet_format_and_validation(self):
        mac = self._sample_mac("ab")
        pkt = nm.build_wol_packet(mac)
        self.assertEqual(len(pkt), 102)
        self.assertEqual(pkt[:6], b"\xff" * 6)
        self.assertEqual(pkt[6:], bytes.fromhex("0200000000ab") * 16)
        for invalid in ("", ":".join(["00"] * 6), ":".join(["ff"] * 6), "not-a-mac"):
            with self.assertRaises(ValueError):
                nm.build_wol_packet(invalid)

    def test_known_mac_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            m1 = self._sample_mac("11")
            m2 = self._sample_mac("22")
            saved = nm.save_known_tv_macs([m1, m2, m1], maint_dir=tmp)
            self.assertEqual(saved, [m1, m2])
            loaded = nm.load_known_tv_macs(maint_dir=tmp)
            self.assertEqual(loaded, [m1, m2])

    def test_active_tv_at_3am_runs_update_and_stays_on(self):
        cmds = []

        def fake_ctl(obj):
            cmds.append(obj["cmd"])
            if obj["cmd"] == "status":
                if "maintenance-update" not in cmds:
                    return {
                        "ok": True,
                        "result": {
                            "resident": {"tvPower": "active", "residentPolls": 10},
                        },
                    }
                return {
                    "ok": True,
                    "result": {
                        "resident": {
                            "tvPower": "active",
                            "residentPolls": 12,
                            "lastMaintenanceReport": {
                                "reqId": "req-active-1",
                                "mode": "update",
                                "returnedToStandby": False,
                                "updatesCompleted": ["netflix"],
                            },
                        },
                    },
                }
            if obj["cmd"] == "maintenance-update":
                return {"ok": True, "result": {"reqId": "req-active-1"}}
            raise AssertionError(f"Unexpected command {obj}")

        out = nm.orchestrate_tv_maintenance(
            mode="update",
            ctl_fn=fake_ctl,
            wol_fn=lambda macs: 0,
            sleep_fn=lambda _: None,
        )
        self.assertIn("maintenance-update", cmds)
        self.assertNotIn("maintenance-update-standby", cmds)
        self.assertEqual(out["tvReport"]["reqId"], "req-active-1")
        self.assertFalse(out["tvReport"]["returnedToStandby"])

    def test_standby_tv_woken_at_3am_runs_update_standby(self):
        cmds = []
        wol_calls = []
        status_count = 0

        def fake_ctl(obj):
            nonlocal status_count
            cmds.append(obj["cmd"])
            if obj["cmd"] == "status":
                status_count += 1
                if status_count == 1:
                    return {
                        "ok": True,
                        "result": {
                            "resident": {"tvPower": "standby", "residentPolls": 5},
                        },
                    }
                if status_count == 2:
                    return {
                        "ok": True,
                        "result": {
                            "resident": {"tvPower": "active", "residentPolls": 6},
                        },
                    }
                return {
                    "ok": True,
                    "result": {
                        "resident": {
                            "tvPower": "standby",
                            "residentPolls": 7,
                            "lastMaintenanceReport": {
                                "reqId": "req-standby-1",
                                "mode": "update-standby",
                                "returnedToStandby": True,
                                "updatesCompleted": ["youtube.leanback.v4"],
                            },
                        },
                    },
                }
            if obj["cmd"] == "maintenance-update-standby":
                return {"ok": True, "result": {"reqId": "req-standby-1"}}
            raise AssertionError(f"Unexpected command {obj}")

        out = nm.orchestrate_tv_maintenance(
            mode="update",
            ctl_fn=fake_ctl,
            wol_fn=lambda macs: (wol_calls.extend(macs) or 4),
            known_macs=[self._sample_mac("33")],
            sleep_fn=lambda _: None,
        )
        self.assertEqual(len(wol_calls), 1)
        self.assertIn("maintenance-update-standby", cmds)
        self.assertEqual(out["tvReport"]["reqId"], "req-standby-1")
        self.assertTrue(out["tvReport"]["returnedToStandby"])

    def test_tv_remaining_off_at_3am_defers_to_idle_or_wake(self):
        cmds = []
        clock = [0.0]

        def fake_monotonic():
            clock[0] += 10.0
            return clock[0]

        def fake_ctl(obj):
            cmds.append(obj["cmd"])
            if obj["cmd"] == "status":
                return {
                    "ok": True,
                    "result": {
                        "resident": {"tvPower": "off", "residentPolls": 5},
                    },
                }
            if obj["cmd"] == "maintenance-update":
                return {"ok": True, "result": {"reqId": "req-deferred-1"}}
            raise AssertionError(f"Unexpected command {obj}")

        out = nm.orchestrate_tv_maintenance(
            mode="update",
            ctl_fn=fake_ctl,
            wol_fn=lambda macs: 2,
            known_macs=[self._sample_mac("44")],
            wake_wait_seconds=15.0,
            sleep_fn=lambda _: None,
            monotonic_fn=fake_monotonic,
        )
        self.assertIsNone(out["tvReport"])
        self.assertEqual(out["tvMaintenance"]["status"], "tv_off_deferred_to_idle_or_wake")
        self.assertEqual(out["tvMaintenance"]["reqId"], "req-deferred-1")


if __name__ == "__main__":
    unittest.main()
