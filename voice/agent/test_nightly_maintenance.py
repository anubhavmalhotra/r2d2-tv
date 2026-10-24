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
            sub = Path(tmp) / "maint"
            m1 = self._sample_mac("11")
            m2 = self._sample_mac("22")
            saved = nm.save_known_tv_macs([m1, m2, m1], maint_dir=sub)
            self.assertEqual(saved, [m1, m2])
            loaded = nm.load_known_tv_macs(maint_dir=sub)
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
                            "resident": {
                                "tvPower": "active",
                                "residentPolls": 6,
                                "wakeOwned": True,
                                "wakeReason": "wol",
                                "userActive": False,
                                "foregroundAppId": "local.owner.applibrary",
                            },
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
            if obj["cmd"] == "maintenance-update-idle":
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
        self.assertIn("maintenance-update-idle", cmds)
        self.assertNotIn("maintenance-update", cmds)
        self.assertEqual(out["tvMaintenance"]["status"], "tv_off_deferred_to_idle_or_wake")
        self.assertEqual(out["tvMaintenance"]["mode"], "update-idle")
        self.assertTrue(out["tvMaintenance"]["requiresIdle"])
        self.assertEqual(out["tvMaintenance"]["reqId"], "req-deferred-1")

    def test_standby_heartbeat_with_incrementing_polls_not_accepted_as_wake(self):
        cmds = []
        clock = [0.0]
        polls = [5]

        def fake_monotonic():
            clock[0] += 5.0
            return clock[0]

        def fake_ctl(obj):
            cmds.append(obj["cmd"])
            if obj["cmd"] == "status":
                polls[0] += 1
                return {
                    "ok": True,
                    "result": {
                        "resident": {"tvPower": "standby", "residentPolls": polls[0]},
                    },
                }
            if obj["cmd"] == "maintenance-update-idle":
                return {"ok": True, "result": {"reqId": "req-standby-hb"}}
            raise AssertionError(f"Unexpected command {obj}")

        out = nm.orchestrate_tv_maintenance(
            mode="update",
            ctl_fn=fake_ctl,
            wol_fn=lambda macs: 2,
            known_macs=[self._sample_mac("55")],
            wake_wait_seconds=12.0,
            sleep_fn=lambda _: None,
            monotonic_fn=fake_monotonic,
        )
        self.assertIsNone(out["tvReport"])
        self.assertNotIn("maintenance-update-standby", cmds)
        self.assertNotIn("maintenance-update", cmds)
        self.assertIn("maintenance-update-idle", cmds)
        self.assertFalse(out["tvMaintenance"]["wokeFromStandby"])

    def test_wake_with_user_activity_runs_normal_update_without_standby_return(self):
        cmds = []
        status_count = [0]

        def fake_ctl(obj):
            cmds.append(obj["cmd"])
            if obj["cmd"] == "status":
                status_count[0] += 1
                if status_count[0] == 1:
                    return {
                        "ok": True,
                        "result": {
                            "resident": {"tvPower": "standby", "residentPolls": 10},
                        },
                    }
                if status_count[0] == 2:
                    return {
                        "ok": True,
                        "result": {
                            "resident": {
                                "tvPower": "active",
                                "residentPolls": 11,
                                "foregroundAppId": "netflix",
                            },
                        },
                    }
                return {
                    "ok": True,
                    "result": {
                        "resident": {
                            "tvPower": "active",
                            "residentPolls": 12,
                            "lastMaintenanceReport": {
                                "reqId": "req-user-wake",
                                "mode": "update",
                                "returnedToStandby": False,
                            },
                        },
                    },
                }
            if obj["cmd"] == "maintenance-update":
                return {"ok": True, "result": {"reqId": "req-user-wake"}}
            raise AssertionError(f"Unexpected command {obj}")

        out = nm.orchestrate_tv_maintenance(
            mode="update",
            ctl_fn=fake_ctl,
            wol_fn=lambda macs: 2,
            known_macs=[self._sample_mac("66")],
            sleep_fn=lambda _: None,
        )
        self.assertIn("maintenance-update", cmds)
        self.assertNotIn("maintenance-update-standby", cmds)
        self.assertEqual(out["tvReport"]["reqId"], "req-user-wake")

    def test_coincident_wake_without_positive_wake_ownership_does_not_use_standby_mode(self):
        cmds = []
        reads = [0]
        clock = [0.0]

        def mono():
            clock[0] += 1.0
            return clock[0]

        def fake_ctl(obj):
            cmds.append(obj["cmd"])
            if obj["cmd"] == "status":
                reads[0] += 1
                return {
                    "ok": True,
                    "result": {
                        "resident": {
                            "tvPower": "standby" if reads[0] == 1 else "active",
                            "residentPolls": reads[0],
                        },
                    },
                }
            return {"ok": True, "result": {"reqId": "coincident-user-wake"}}

        out = nm.orchestrate_tv_maintenance(
            ctl_fn=fake_ctl,
            wol_fn=lambda macs: 1,
            known_macs=[self._sample_mac("77")],
            sleep_fn=lambda _: None,
            monotonic_fn=mono,
            report_wait_seconds=2.0,
        )
        self.assertNotIn("maintenance-update-standby", cmds)
        self.assertIn("maintenance-update", cmds)
        self.assertFalse(out["tvMaintenance"]["wokeFromStandby"])

        # Explicit wakeOwned: False must NOT be overridden by stale wakeReason: 'wol'
        cmds2 = []
        reads2 = [0]

        def fake_ctl_unowned(obj):
            cmds2.append(obj["cmd"])
            if obj["cmd"] == "status":
                reads2[0] += 1
                if reads2[0] == 1:
                    return {"ok": True, "result": {"resident": {"tvPower": "standby", "residentPolls": 1}}}
                return {
                    "ok": True,
                    "result": {
                        "resident": {
                            "tvPower": "active",
                            "residentPolls": 2,
                            "wakeOwned": False,
                            "wakeReason": "wol",
                            "userActive": False,
                            "foregroundAppId": "local.owner.applibrary",
                        }
                    },
                }
            return {"ok": True, "result": {"reqId": "unowned-stale-reason"}}

        out2 = nm.orchestrate_tv_maintenance(
            ctl_fn=fake_ctl_unowned,
            wol_fn=lambda macs: 1,
            known_macs=[self._sample_mac("78")],
            sleep_fn=lambda _: None,
            monotonic_fn=mono,
            report_wait_seconds=2.0,
        )
        self.assertNotIn("maintenance-update-standby", cmds2)
        self.assertIn("maintenance-update", cmds2)
        self.assertFalse(out2["tvMaintenance"]["wokeFromStandby"])


if __name__ == "__main__":
    unittest.main()
