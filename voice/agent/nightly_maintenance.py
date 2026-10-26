#!/usr/bin/env python3
"""Nightly 3:00 AM maintenance and Wake-on-LAN orchestrator for Spark and the TV."""
import argparse
import datetime
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

DEFAULT_BASE_DIR = Path.home() / ".local" / "share" / "r2d2-agent"
DEFAULT_CTL_SOCKET = DEFAULT_BASE_DIR / "pair" / "pair.sock"
DEFAULT_MAINT_DIR = DEFAULT_BASE_DIR / "maintenance"


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=5.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def parse_mac_bytes(value):
    if not isinstance(value, str):
        raise ValueError("INVALID_MAC")
    cleaned = re.sub(r"[:\-\s]", "", value.strip()).lower()
    if len(cleaned) != 12 or not re.fullmatch(r"[0-9a-f]{12}", cleaned):
        raise ValueError("INVALID_MAC")
    if cleaned in ("000000000000", "ffffffffffff"):
        raise ValueError("INVALID_MAC")
    return bytes.fromhex(cleaned)


def format_mac(mac_bytes):
    if not isinstance(mac_bytes, (bytes, bytearray)) or len(mac_bytes) != 6:
        raise ValueError("INVALID_MAC_BYTES")
    return ":".join(f"{b:02x}" for b in mac_bytes)


def build_wol_packet(mac_value):
    mac_bytes = parse_mac_bytes(mac_value) if isinstance(mac_value, str) else bytes(mac_value)
    if len(mac_bytes) != 6:
        raise ValueError("INVALID_MAC")
    return (b"\xff" * 6) + (mac_bytes * 16)


def load_known_tv_macs(maint_dir=DEFAULT_MAINT_DIR):
    macs = []
    seen = set()
    p = Path(maint_dir) / "tv_macs.json"
    try:
        if p.is_file():
            data = json.loads(p.read_text("utf-8"))
            items = data.get("macs", []) if isinstance(data, dict) else []
            for item in items:
                try:
                    raw = parse_mac_bytes(item)
                    norm = format_mac(raw)
                    if norm not in seen:
                        seen.add(norm)
                        macs.append(norm)
                except ValueError:
                    continue
    except Exception:
        pass
    return macs


def save_known_tv_macs(mac_list, maint_dir=DEFAULT_MAINT_DIR):
    valid = []
    seen = set()
    for item in mac_list or []:
        try:
            norm = format_mac(parse_mac_bytes(item))
            if norm not in seen:
                seen.add(norm)
                valid.append(norm)
        except ValueError:
            continue
    if not valid:
        return []
    existing = load_known_tv_macs(maint_dir)
    for norm in existing:
        if norm not in seen:
            seen.add(norm)
            valid.append(norm)
    d = Path(maint_dir)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    target = d / "tv_macs.json"
    tmp = target.with_suffix(".tmp")
    body = (json.dumps({"schema": 1, "macs": valid}, indent=2) + "\n").encode("utf-8")
    tmp.write_bytes(body)
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    return valid


def send_wake_on_lan(mac_list, broadcast_addrs=("255.255.255.255",), ports=(9, 7), send_fn=None):
    sent = 0
    packets = []
    for m in mac_list or []:
        try:
            packets.append(build_wol_packet(m))
        except ValueError:
            continue
    if not packets:
        return 0
    if send_fn is not None:
        for pkt in packets:
            for addr in broadcast_addrs:
                for port in ports:
                    send_fn(pkt, (addr, port))
                    sent += 1
        return sent
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for pkt in packets:
            for addr in broadcast_addrs:
                for port in ports:
                    try:
                        sock.sendto(pkt, (addr, port))
                        sent += 1
                    except OSError:
                        continue
    return sent


def snapd_get(path, timeout=15.0):
    conn = UnixHTTPConnection("/run/snapd.socket", timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": "localhost"})
        res = conn.getresponse()
        raw = res.read(262144)
        return json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def check_spark_chromium():
    uid = os.getuid()
    browser_running = False
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            p = Path("/proc") / entry
            try:
                if p.stat().st_uid != uid:
                    continue
                cmd = (p / "cmdline").read_bytes().decode("utf-8", "ignore")
                if "/snap/chromium/" in cmd or "chromium" in cmd:
                    browser_running = True
                    break
            except OSError:
                continue
    except OSError:
        pass

    out = {
        "browserIdle": not browser_running,
        "installedVersion": None,
        "installedRevision": None,
        "trackingChannel": None,
        "storeVersion": None,
        "storeRevision": None,
        "upToDate": None,
        "error": None,
    }
    try:
        local_info = snapd_get("/v2/snaps/chromium")
        res = local_info.get("result") or {}
        out["installedVersion"] = res.get("version")
        out["installedRevision"] = str(res.get("revision")) if res.get("revision") is not None else None
        out["trackingChannel"] = res.get("tracking-channel") or res.get("channel")
        store_info = snapd_get("/v2/find?name=chromium")
        sres = store_info.get("result") or []
        if sres and isinstance(sres[0], dict):
            channels = sres[0].get("channels") or {}
            ch = channels.get("latest/stable") or channels.get(out["trackingChannel"] or "latest/stable") or {}
            out["storeVersion"] = ch.get("version")
            out["storeRevision"] = str(ch.get("revision")) if ch.get("revision") is not None else None
        if out["installedRevision"] and out["storeRevision"]:
            out["upToDate"] = out["installedRevision"] == out["storeRevision"]
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out


def ctl_call(cmd_obj, socket_path=DEFAULT_CTL_SOCKET, timeout=5.0):
    payload = (json.dumps(cmd_obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(socket_path))
        s.sendall(payload)
        chunks = []
        while True:
            c = s.recv(4096)
            if not c:
                break
            chunks.append(c)
            if b"\n" in c:
                break
        return json.loads(b"".join(chunks).decode("utf-8"))


def check_systemd_units():
    units = {}
    for u in ("r2d2-agent.service", "baaz-airplay.service", "r2d2-nightly-maintenance.timer"):
        try:
            r = subprocess.run(
                ["/usr/bin/systemctl", "--user", "is-active", u],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            units[u] = r.stdout.strip() or "unknown"
        except Exception:
            units[u] = "error"
    return units


def orchestrate_tv_maintenance(
    *,
    mode="update",
    ctl_fn=ctl_call,
    wol_fn=send_wake_on_lan,
    known_macs=(),
    wake_wait_seconds=45.0,
    report_wait_seconds=90.0,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
):
    st0 = ctl_call({"cmd": "status"}) if ctl_fn is ctl_call else ctl_fn({"cmd": "status"})
    res0 = ((st0.get("result") or {}).get("resident") or {}) if isinstance(st0, dict) and st0.get("ok") else {}
    initial_power = res0.get("tvPower") or ((st0.get("result") or {}).get("warm") or {}).get("warm", {}).get("power")
    initial_polls = res0.get("residentPolls", 0) if isinstance(res0.get("residentPolls"), int) else 0
    discovered_macs = list(res0.get("tvMacs") or [])
    all_macs = list(dict.fromkeys([*(known_macs or []), *discovered_macs]))

    woke_from_standby = False
    wol_packets_sent = 0
    idle_foreground_apps = {"", "local.owner.applibrary", "com.webos.app.screensaver", "com.webos.app.home"}

    if mode == "update" and initial_power != "active":
        if all_macs:
            wol_packets_sent = wol_fn(all_macs)
        if wol_packets_sent > 0:
            wake_deadline = monotonic_fn() + wake_wait_seconds
            while monotonic_fn() < wake_deadline:
                sleep_fn(2.0)
                st_wake = ctl_fn({"cmd": "status"})
                if isinstance(st_wake, dict) and st_wake.get("ok"):
                    r_wake = (st_wake.get("result") or {}).get("resident") or {}
                    p_wake = r_wake.get("tvPower")
                    polls_wake = r_wake.get("residentPolls", 0) if isinstance(r_wake.get("residentPolls"), int) else 0
                    fg_raw = r_wake.get("foregroundAppId")
                    has_idle_fg = (
                        isinstance(fg_raw, str)
                        and bool(fg_raw.strip())
                        and fg_raw.strip() in idle_foreground_apps
                    )
                    explicit_user_inactive = r_wake.get("userActive") is False
                    positive_wake_owned = (
                        r_wake.get("wakeOwned") is True
                        or (
                            "wakeOwned" not in r_wake
                            and r_wake.get("wakeReason") in ("wol", "network", "magicPacket")
                        )
                    )
                    if p_wake == "active" and polls_wake > initial_polls:
                        initial_power = "active"
                        woke_from_standby = bool(
                            positive_wake_owned and explicit_user_inactive and has_idle_fg
                        )
                        break

        if initial_power != "active":
            # Queue an idle-gated deferred update so the TV only runs it after 30 minutes of verified idle
            trig_def = ctl_fn({"cmd": "maintenance-update-idle"})
            req_id_def = (
                trig_def.get("result", {}).get("reqId")
                if isinstance(trig_def, dict) and trig_def.get("ok") and isinstance(trig_def.get("result"), dict)
                else None
            )
            return {
                "tvReport": None,
                "residentSummary": {
                    "tvPower": initial_power or "off",
                    "hidraw": res0.get("hidraw"),
                    "residentPolls": initial_polls,
                },
                "tvMaintenance": {
                    "status": "tv_off_deferred_to_idle_or_wake",
                    "mode": "update-idle",
                    "requiresIdle": True,
                    "reqId": req_id_def,
                    "wolPacketsSent": wol_packets_sent,
                    "wokeFromStandby": False,
                },
            }

    if mode == "check":
        cmd_name = "maintenance-check"
    elif mode == "update-idle":
        cmd_name = "maintenance-update-idle"
    elif woke_from_standby or mode == "update-standby":
        cmd_name = "maintenance-update-standby"
    else:
        cmd_name = "maintenance-update"

    trig = ctl_fn({"cmd": cmd_name})

    req_id = None
    if isinstance(trig, dict) and trig.get("ok") and isinstance(trig.get("result"), dict):
        req_id = trig["result"].get("reqId")

    tv_report = None
    resident_summary = {
        "tvPower": initial_power,
        "hidraw": res0.get("hidraw"),
        "residentPolls": initial_polls,
    }
    if req_id:
        deadline = monotonic_fn() + report_wait_seconds
        while monotonic_fn() < deadline:
            sleep_fn(2.0)
            st = ctl_fn({"cmd": "status"})
            if isinstance(st, dict) and st.get("ok") and isinstance(st.get("result"), dict):
                res = st["result"].get("resident") or {}
                resident_summary = {
                    "tvPower": res.get("tvPower"),
                    "hidraw": res.get("hidraw"),
                    "residentPolls": res.get("residentPolls"),
                }
                rep = res.get("lastMaintenanceReport")
                if isinstance(rep, dict) and rep.get("reqId") == req_id:
                    tv_report = rep
                    break

    return {
        "tvReport": tv_report,
        "residentSummary": resident_summary,
        "tvMaintenance": tv_report
        if tv_report is not None
        else {
            "status": "tv_standby_or_unreachable",
            "reqId": req_id,
            "wolPacketsSent": wol_packets_sent,
            "wokeFromStandby": woke_from_standby,
        },
    }


def run_maintenance(
    mode="update",
    wait_seconds=90.0,
    wake_wait_seconds=45.0,
    socket_path=DEFAULT_CTL_SOCKET,
    maint_dir=DEFAULT_MAINT_DIR,
):
    started_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    spark_browser = check_spark_chromium()
    units = check_systemd_units()
    known_macs = load_known_tv_macs(maint_dir)

    outcome = orchestrate_tv_maintenance(
        mode=mode,
        ctl_fn=lambda obj: ctl_call(obj, socket_path=socket_path),
        known_macs=known_macs,
        wake_wait_seconds=wake_wait_seconds,
        report_wait_seconds=wait_seconds,
    )
    tv_report = outcome["tvReport"]
    if isinstance(tv_report, dict) and isinstance(tv_report.get("tvMacs"), list):
        save_known_tv_macs(tv_report["tvMacs"], maint_dir=maint_dir)

    receipt = {
        "schema": 1,
        "timestampUtc": started_utc,
        "mode": mode,
        "sparkUnits": units,
        "sparkBrowser": spark_browser,
        "tvResident": outcome["residentSummary"],
        "tvMaintenance": outcome["tvMaintenance"],
    }

    maint_path = Path(maint_dir)
    maint_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(maint_path, 0o700)
    runs_dir = maint_path / "runs"
    runs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runs_dir, 0o700)

    body = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    for target in (maint_path / "latest.json", runs_dir / f"{stamp}.json"):
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(body)
        os.chmod(tmp, 0o600)
        tmp.replace(target)

    print(
        json.dumps(
            {
                "ok": True,
                "timestampUtc": started_utc,
                "mode": mode,
                "sparkChromiumUpToDate": spark_browser.get("upToDate"),
                "sparkChromiumVersion": spark_browser.get("installedVersion"),
                "tvReportReceived": tv_report is not None,
                "tvFences": tv_report.get("fences") if tv_report else None,
                "tvLgServerBlocked": tv_report.get("lgServerBlocked") if tv_report else None,
                "tvVoiceLeveling": (
                    (tv_report.get("voiceLeveling") or {}).get("status")
                    if isinstance(tv_report, dict) and isinstance(tv_report.get("voiceLeveling"), dict)
                    else None
                ),
                "tvInstalledCount": tv_report.get("installedCount") if tv_report else None,
                "tvUpdatesAttempted": len(tv_report.get("updatesAttempted") or []) if tv_report else 0,
                "tvUpdatesCompleted": len(tv_report.get("updatesCompleted") or []) if tv_report else 0,
            }
        )
    )
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check-only", action="store_true", help="Only inspect status without installing TV app updates")
    ap.add_argument("--wait-seconds", type=float, default=90.0)
    ap.add_argument("--wake-wait-seconds", type=float, default=45.0)
    args = ap.parse_args(argv)
    return run_maintenance(
        mode="check" if args.check_only else "update",
        wait_seconds=args.wait_seconds,
        wake_wait_seconds=args.wake_wait_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
