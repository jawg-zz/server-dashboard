#!/usr/bin/env python3
import http.server
import json
import logging
import os
import platform
import socket
import socketserver
import subprocess
import time
from collections import deque
from datetime import datetime

LOG = logging.getLogger(__name__)
START_TIME = time.time()
UPTIME_HISTORY = deque(maxlen=60)
DISK_STATS_CACHE = {"prev_read": 0, "prev_write": 0, "prev_time": 0}
CPU_CORE_PREV = {}
NET_PREV = {}

# DRY: single byte-formatting function used by all collectors
def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def _fmt_bps(n):
    if n < 1024:
        return f"{n:.0f} B/s"
    elif n < 1024 * 1024:
        return f"{n/1024:.1f} KB/s"
    else:
        return f"{n/1024/1024:.1f} MB/s"


def get_cpu_info():
    cores = os.cpu_count() or 0
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().strip().split()
            la1, la5, la15 = float(parts[0]), float(parts[1]), float(parts[2])
    except Exception as e:
        LOG.exception("get_cpu_info failed")
        la1 = la5 = la15 = 0.0
    return {"cores": cores, "load_1min": la1, "load_5min": la5, "load_15min": la15}


def get_memory_info():
    data = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].strip().rstrip(":")
                    val_str = parts[1].strip().split()[0]
                    data[key] = int(val_str) * 1024
        total = data.get("MemTotal", 0)
        available = data.get("MemAvailable", data.get("MemFree", 0))
        used = total - available
        swap_total = data.get("SwapTotal", 0)
        swap_free = data.get("SwapFree", 0)
        swap_used = swap_total - swap_free
    except Exception as e:
        LOG.exception("get_memory_info failed")
        total = used = available = swap_total = swap_used = 0

    return {
        "total": _fmt_bytes(total), "used": _fmt_bytes(used), "available": _fmt_bytes(available),
        "total_bytes": total, "used_bytes": used, "available_bytes": available,
        "swap_total": _fmt_bytes(swap_total), "swap_used": _fmt_bytes(swap_used),
        "swap_total_bytes": swap_total, "swap_used_bytes": swap_used,
        "swap_percent": round(swap_used / swap_total * 100, 1) if swap_total > 0 else 0,
    }


def get_disk_info():
    try:
        s = os.statvfs("/")
        total = s.f_frsize * s.f_blocks
        free = s.f_frsize * s.f_bfree
        used = total - free
        pct = (used / total * 100) if total else 0
    except Exception as e:
        LOG.exception("get_disk_info failed")
        total = used = 0
        pct = 0

    return {
        "total": _fmt_bytes(total), "used": _fmt_bytes(used), "available": _fmt_bytes(total - used),
        "usage_percent": round(pct, 1),
        "total_bytes": total, "used_bytes": used, "available_bytes": total - used,
    }

def get_filesystems():
    """Return all real filesystem mount points with usage."""
    try:
        result = subprocess.run(
            ["df", "-T", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs",
             "-x", "overlay", "-x", "proc", "-x", "sysfs", "-x", "cgroup",
             "-x", "cgroup2", "-x", "devpts", "-x", "mqueue", "-x", "pstore",
             "-x", "securityfs", "-x", "hugetlbfs", "-x", "autofs",
             "-x", "debugfs", "-x", "tracefs", "-x", "configfs", "-x", "efivarfs",
             "-x", "fusectl", "-x", "bpf", "-x", "none"],
            capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n")[1:]  # skip header
        mounts = []
        for line in lines:
            parts = line.split()
            if len(parts) < 7:
                continue
            fstype = parts[1]
            total = int(parts[2]) * 1024
            used = int(parts[3]) * 1024
            avail = int(parts[4]) * 1024
            pct = parts[5].rstrip("%")
            mount = parts[6]
            try:
                pct_f = float(pct)
            except ValueError:
                pct_f = 0.0

            mounts.append({
                "mount": mount,
                "type": fstype,
                "total": _fmt_bytes(total),
                "used": _fmt_bytes(used),
                "avail": _fmt_bytes(avail),
                "usage_percent": pct_f,
                "total_bytes": total,
                "used_bytes": used,
            })
        return mounts
    except Exception as e:
        LOG.exception("get_filesystems failed")
        return []


def get_uptime():
    try:
        with open("/proc/uptime") as f:
            uptime_secs = float(f.read().strip().split()[0])
    except Exception as e:
        LOG.exception("get_uptime failed")
        return "Unknown", 0
    days = int(uptime_secs // 86400)
    hours = int((uptime_secs % 86400) // 3600)
    minutes = int((uptime_secs % 3600) // 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts), uptime_secs


def get_network_info():
    hostname = platform.node()
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception as e:
        LOG.exception("get_network_info failed")
        pass
    return {"hostname": hostname, "ip": ip}


def get_process_count():
    try:
        result = subprocess.run(
            ["ps", "-e", "--no-headers"], capture_output=True, text=True,
        )
        return len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
    except Exception as e:
        LOG.exception("get_process_count failed")
        return 0


def get_top_processes():
    try:
        result = subprocess.run(
            ["ps", "aux", "--sort=-%cpu", "--no-headers"],
            capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n")[:20]
        processes = []
        for line in lines:
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            try:
                pid = int(parts[1])
                cpu = float(parts[2])
                mem = float(parts[3])
                rss_kb = int(parts[5])
                mem_mb = round(rss_kb / 1024, 1)
                status = parts[7]
                user = parts[0]
                name = parts[10][:60] if len(parts) > 10 else "?"
                processes.append({
                    "pid": pid, "name": name, "cpu_percent": cpu,
                    "mem_percent": mem, "mem_mb": mem_mb,
                    "status": status, "user": user,
                })
            except (ValueError, IndexError):
                continue
        return processes
    except Exception as e:
        LOG.exception("get_top_processes failed")
        return []


def get_disk_io():
    """Return disk I/O for the busiest disk device, with speed deltas."""
    try:
        with open("/proc/diskstats") as f:
            devices = []
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                name = parts[2]
                # Accept SD/VD/XVD base devices (sda, vda, xvda — but not sda1)
                is_sd_base = name.startswith("sd") and len(name) == 3
                is_virt_base = (name.startswith("vd") or name.startswith("xvd")) and len(name) == 3
                is_nvme_base = name.startswith("nvme") and "p" not in name
                if not (is_sd_base or is_virt_base or is_nvme_base):
                    continue

                read_sectors = int(parts[5])
                write_sectors = int(parts[9])
                reads_mb = round(read_sectors * 512 / 1024 / 1024, 1)
                writes_mb = round(write_sectors * 512 / 1024 / 1024, 1)

                now = time.time()
                dt = now - DISK_STATS_CACHE["prev_time"]
                read_speed = 0
                write_speed = 0
                if dt > 0 and DISK_STATS_CACHE["prev_time"] > 0:
                    dr = (read_sectors - DISK_STATS_CACHE["prev_read"]) * 512 / 1024 / 1024
                    dw = (write_sectors - DISK_STATS_CACHE["prev_write"]) * 512 / 1024 / 1024
                    read_speed = round(dr / dt, 2)
                    write_speed = round(dw / dt, 2)

                devices.append({
                    "reads_mb": reads_mb, "writes_mb": writes_mb,
                    "read_speed_mbps": read_speed, "write_speed_mbps": write_speed,
                    "device": name,
                    "total_io_mb": reads_mb + writes_mb,
                })

            if not devices:
                return {"reads_mb": 0, "writes_mb": 0, "read_speed_mbps": 0, "write_speed_mbps": 0, "device": "none"}

            # Pick the device with the most I/O and update cache from it
            best = max(devices, key=lambda d: d["total_io_mb"])
            # Re-read the best device's sectors for accurate delta next time
            for line in open("/proc/diskstats"):
                spl = line.split()
                if len(spl) >= 14 and spl[2] == best["device"]:
                    DISK_STATS_CACHE["prev_read"] = int(spl[5])
                    DISK_STATS_CACHE["prev_write"] = int(spl[9])
                    break
            DISK_STATS_CACHE["prev_time"] = time.time()

            return {
                "reads_mb": best["reads_mb"], "writes_mb": best["writes_mb"],
                "read_speed_mbps": best["read_speed_mbps"],
                "write_speed_mbps": best["write_speed_mbps"],
                "device": best["device"],
            }
    except Exception as e:
        LOG.exception("get_disk_io failed")
        pass
    return {"reads_mb": 0, "writes_mb": 0, "read_speed_mbps": 0, "write_speed_mbps": 0, "device": "error"}


# ===== Per-Core CPU =====
def get_cpu_per_core():
    """Return per-core CPU usage percentages using delta from /proc/stat."""
    try:
        with open("/proc/stat") as f:
            lines = f.readlines()
        cores = []
        now = time.time()
        for line in lines:
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            label = parts[0]
            if label == "cpu":
                continue
            nums = [int(v) for v in parts[1:]]
            total = sum(nums)
            idle = nums[3] + nums[4]  # idle + iowait

            prev = CPU_CORE_PREV.get(label, {"total": total, "idle": idle, "time": now})
            dt = now - prev["time"]
            if dt > 0 and prev["total"] > 0:
                dtotal = total - prev["total"]
                didle = idle - prev["idle"]
                pct = round((dtotal - didle) / dtotal * 100, 1) if dtotal > 0 else 0.0
            else:
                pct = 0.0

            CPU_CORE_PREV[label] = {"total": total, "idle": idle, "time": now}
            cores.append({"core": label, "usage_percent": min(pct, 100.0)})
        return cores
    except Exception as e:
        LOG.exception("get_cpu_per_core failed")
        return [{"core": f"cpu{i}", "usage_percent": 0.0} for i in range(os.cpu_count() or 1)]


# ===== Network Interfaces =====

def get_network_interfaces():
    """Return per-interface RX/TX stats with bandwidth from /proc/net/dev."""
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]  # skip headers
    except Exception as e:
        LOG.exception("get_network_interfaces failed")
        return []
    now = time.time()
    interfaces = []
    for line in lines:
        parts = line.strip().split()
        if len(parts) < 10:
            continue
        name = parts[0].rstrip(":")
        rx_bytes = int(parts[1])
        rx_packets = int(parts[2])
        tx_bytes = int(parts[9])
        tx_packets = int(parts[10])
        if name == "lo":
            continue  # skip loopback

        prev = NET_PREV.get(name, {"rx": rx_bytes, "tx": tx_bytes, "time": now})
        dt = now - prev["time"]
        rx_speed = tx_speed = 0.0
        if dt > 0 and prev["time"] > 0:
            rx_speed = round((rx_bytes - prev["rx"]) / dt, 0)
            tx_speed = round((tx_bytes - prev["tx"]) / dt, 0)

        NET_PREV[name] = {"rx": rx_bytes, "tx": tx_bytes, "time": now}

        interfaces.append({
            "name": name,
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "rx_packets": rx_packets,
            "tx_packets": tx_packets,
            "rx_speed_bps": rx_speed,
            "tx_speed_bps": tx_speed,
            "rx_speed": _fmt_bps(rx_speed),
            "tx_speed": _fmt_bps(tx_speed),
            "rx_total": _fmt_bytes(rx_bytes),
            "tx_total": _fmt_bytes(tx_bytes),
        })
    return interfaces


# ===== Listening Ports =====
def get_listening_ports():
    """Return listening TCP ports with process info via ss."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp", "-4"],
            capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n")[1:]
        ports = []
        for line in lines:
            parts = line.split()
            if len(parts) < 5:
                continue
            addr = parts[3]
            port_str = addr.rsplit(":", 1)[-1]
            try:
                port = int(port_str)
            except ValueError:
                continue
            proc_info = parts[-1] if len(parts) > 5 else ""

            import re
            pid = ""
            name = ""
            m = re.search(r'pid=(\d+)', proc_info)
            if m:
                pid = m.group(1)
                m2 = re.search(r'users:\(\("([^"]+)"', proc_info)
                if m2:
                    name = m2.group(1)

            ports.append({
                "port": port,
                "address": addr.rsplit(":", 1)[0],
                "pid": pid,
                "process": name,
            })
        return ports
    except Exception as e:
        LOG.exception("get_listening_ports failed")
        return []


# ===== Docker =====
def get_docker_stats():
    """Return container stats via docker CLI, or indicate unavailability."""
    try:
        # Single call to list containers
        ps_result = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        if ps_result.returncode != 0:
            return {"available": False, "error": "Docker not available"}

        lines = ps_result.stdout.strip().split("\n")
        containers_raw = {}
        cid_order = []
        for line in lines:
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                cid = parts[0]
                cid_order.append(cid)
                containers_raw[cid] = {
                    "id": cid[:12],
                    "image": parts[1],
                    "status": parts[2],
                    "name": parts[3],
                }

        if not cid_order:
            return {"available": True, "containers": []}

        # Batch: single docker stats call for ALL containers
        stats_result = subprocess.run(
            ["docker", "stats", "--no-stream", "--no-trunc", "--format",
             "{{.ID}}\t{{.CPUPerc}}\t{{.MemPerc}}\t{{.MemUsage}}\t{{.NetIO}}"] + cid_order,
            capture_output=True, text=True, timeout=10,
        )

        stats_map = {}
        if stats_result.returncode == 0 and stats_result.stdout.strip():
            for sline in stats_result.stdout.strip().split("\n"):
                sparts = sline.split("\t")
                if len(sparts) >= 5:
                    stats_map[sparts[0]] = {
                        "cpu_percent": sparts[1],
                        "mem_percent": sparts[2],
                        "mem_usage": sparts[3],
                        "net_io": sparts[4],
                    }

        containers = []
        for cid in cid_order:
            info = containers_raw[cid]
            stats = stats_map.get(cid, {})
            containers.append({
                "id": info["id"],
                "image": info["image"],
                "name": info["name"],
                "status": info["status"],
                "cpu_percent": stats.get("cpu_percent"),
                "mem_percent": stats.get("mem_percent"),
                "mem_usage": stats.get("mem_usage"),
                "net_io": stats.get("net_io"),
            })

        return {"available": True, "containers": containers}
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        LOG.warning("get_docker_stats failed: %s", e)
        return {"available": False, "error": "Docker daemon not running"}
    except Exception as e:
        LOG.exception("get_docker_stats failed")
        return {"available": False, "error": str(e)}


def collect_stats():
    cpu = get_cpu_info()
    mem = get_memory_info()
    uptime_str, uptime_secs = get_uptime()
    mem_used_pct = round(mem["used_bytes"] / mem["total_bytes"] * 100, 1) if mem["total_bytes"] else 0

    entry = {
        "time": datetime.now().strftime("%H:%M"),
        "load_1m": cpu["load_1min"],
        "mem_used_pct": mem_used_pct,
    }
    UPTIME_HISTORY.append(entry)

    return {
        "cpu": cpu,
        "memory": mem,
        "disk": get_disk_info(),
        "uptime": uptime_str,
        "uptime_seconds": uptime_secs,
        "os": {"name": platform.system(), "version": platform.release()},
        "network": get_network_info(),
        "process_count": get_process_count(),
        "python_version": platform.python_version(),
        "timestamp": datetime.now().isoformat(),
    }


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Server Dashboard</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap">
<style>
/* ===== Design System ===== */
:root {
  --bg-deep: #07070e;
  --bg-surface: #0d0d1a;
  --bg-card: rgba(255,255,255,0.025);
  --bg-card-hover: rgba(255,255,255,0.05);
  --bg-elevated: rgba(255,255,255,0.04);
  --border: rgba(255,255,255,0.06);
  --border-hover: rgba(255,255,255,0.14);
  --border-active: rgba(255,255,255,0.2);
  --text-primary: #f0f0f4;
  --text-secondary: rgba(255,255,255,0.5);
  --text-muted: rgba(255,255,255,0.22);
  --blue: #3b82f6;
  --blue-bg: rgba(59,130,246,0.12);
  --blue-glow: rgba(59,130,246,0.25);
  --emerald: #10b981;
  --emerald-bg: rgba(16,185,129,0.12);
  --emerald-glow: rgba(16,185,129,0.25);
  --purple: #8b5cf6;
  --purple-bg: rgba(139,92,246,0.12);
  --purple-glow: rgba(139,92,246,0.25);
  --amber: #f59e0b;
  --amber-bg: rgba(245,158,11,0.12);
  --amber-glow: rgba(245,158,11,0.25);
  --rose: #f43f5e;
  --rose-bg: rgba(244,63,94,0.12);
  --rose-glow: rgba(244,63,94,0.25);
  --cyan: #06b6d4;
  --radius: 16px;
  --radius-sm: 10px;
  --radius-xs: 6px;
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.3);
  --shadow: 0 8px 32px rgba(0,0,0,0.35);
  --shadow-lg: 0 16px 48px rgba(0,0,0,0.5);
  --font: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  --mono: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: var(--font);
  background: var(--bg-deep);
  color: var(--text-primary);
  min-height: 100vh;
  overflow-x: hidden;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
/* Ambient glow */
body::before {
  content: '';
  position: fixed;
  top: -50%; left: -50%;
  width: 200%; height: 200%;
  background:
    radial-gradient(ellipse at 20% 40%, rgba(59,130,246,0.04) 0%, transparent 60%),
    radial-gradient(ellipse at 80% 20%, rgba(139,92,246,0.03) 0%, transparent 60%),
    radial-gradient(ellipse at 50% 90%, rgba(16,185,129,0.02) 0%, transparent 60%);
  pointer-events: none;
  z-index: 0;
}
.container {
  max-width: 1280px;
  margin: 0 auto;
  padding: 24px;
  position: relative;
  z-index: 1;
}

/* ===== Sticky Header ===== */
header {
  position: sticky;
  top: 16px;
  z-index: 100;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  padding: 14px 22px;
  background: rgba(7,7,14,0.75);
  backdrop-filter: blur(24px) saturate(1.4);
  -webkit-backdrop-filter: blur(24px) saturate(1.4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.04);
  margin-bottom: 20px;
  transition: box-shadow 0.3s;
}
header:hover { box-shadow: 0 8px 40px rgba(0,0,0,0.5); }
.h-left { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.h-left h1 {
  font-size: 1.2rem;
  font-weight: 700;
  background: linear-gradient(135deg, #e8e8ff, #8a8ac8);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  letter-spacing: -0.02em;
}
.h-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.h-badge {
  font-size: 0.7rem;
  font-weight: 500;
  padding: 4px 12px;
  border-radius: 20px;
  border: 1px solid var(--border);
  background: var(--bg-card);
  color: var(--text-secondary);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.h-badge.host { background: var(--blue-bg); color: var(--blue); border-color: rgba(59,130,246,0.2); }
.h-badge.uptime { color: var(--cyan); }
.h-badge.os { color: var(--text-secondary); }
.h-badge.py { color: var(--amber); }
.live-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--emerald);
  box-shadow: 0 0 8px var(--emerald-glow);
  animation: pulse-dot 2s ease-in-out infinite;
  display: inline-block;
}
@keyframes pulse-dot { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.4;transform:scale(0.85)} }
.header-controls { display: flex; align-items: center; gap: 6px; }
.last-updated {
  font-size: 0.68rem;
  color: var(--text-muted);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.btn-icon {
  width: 32px; height: 32px; border-radius: var(--radius-xs);
  background: var(--bg-card); border: 1px solid var(--border);
  color: var(--text-secondary); font-size: 1.05rem; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: all 0.2s;
}
.btn-icon:hover { background: var(--bg-elevated); color: var(--text-primary); border-color: var(--border-hover); transform: translateY(-1px); }
.btn-icon:active { transform: scale(0.9); }
.btn-icon.spinning { animation: spin 0.6s linear; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
.rate-select {
  background: var(--bg-card); border: 1px solid var(--border);
  color: var(--text-secondary); font-size: 0.7rem;
  padding: 5px 8px; border-radius: var(--radius-xs); cursor: pointer;
  transition: border-color 0.2s;
  font-family: var(--font);
}
.rate-select:focus { outline: none; border-color: var(--blue); }

/* ===== Tab Navigation ===== */
.tabs-wrap {
  display: flex; gap: 2px; margin-bottom: 20px;
  background: var(--bg-surface);
  border-radius: var(--radius-sm);
  padding: 4px;
  border: 1px solid var(--border);
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
}
.tabs-wrap::-webkit-scrollbar { display: none; }
.tab-btn {
  padding: 10px 20px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 0.8rem;
  border-radius: 8px;
  transition: all 0.25s cubic-bezier(0.4,0,0.2,1);
  font-weight: 500;
  white-space: nowrap;
  font-family: var(--font);
  position: relative;
}
.tab-btn:hover { color: var(--text-primary); background: var(--bg-elevated); }
.tab-btn.active {
  background: var(--bg-elevated);
  color: var(--text-primary);
  box-shadow: 0 1px 8px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.06);
}
.tab-btn.active::after {
  content: '';
  position: absolute;
  bottom: 2px;
  left: 50%;
  transform: translateX(-50%);
  width: 20px;
  height: 2px;
  background: var(--blue);
  border-radius: 1px;
}
.tab-select { display: none; width: 100%; margin-bottom: 16px; }
.tab-content { display: none; }
.tab-content.active { display: block; animation: tabIn 0.3s cubic-bezier(0.4,0,0.2,1); }
@keyframes tabIn {
  from { opacity: 0; transform: translateY(8px) scale(0.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

/* ===== Gauges (Overview) ===== */
.gauge-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}
.gauge-card {
  background: var(--bg-card);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 22px 18px 18px;
  text-align: center;
  box-shadow: var(--shadow-sm);
  transition: all 0.3s cubic-bezier(0.4,0,0.2,1);
  position: relative;
  overflow: hidden;
}
.gauge-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent, var(--blue)), transparent);
  opacity: 0.5;
}
.gauge-card:hover {
  transform: translateY(-4px);
  border-color: var(--border-hover);
  background: var(--bg-card-hover);
  box-shadow: var(--shadow);
}
.gauge-card.cpu { --accent: var(--blue); }
.gauge-card.mem { --accent: var(--emerald); }
.gauge-card.disk { --accent: var(--purple); }
.gauge-card.net { --accent: var(--amber); }
.gauge-title {
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.gauge-svg { width: 110px; height: 110px; display: block; margin: 6px auto 4px; }
.gauge-track { fill: none; stroke: rgba(255,255,255,0.04); stroke-width: 7; }
.gauge-arc {
  fill: none; stroke-width: 7; stroke-linecap: round;
  transition: stroke-dashoffset 1.2s cubic-bezier(0.4, 0, 0.2, 1);
  filter: drop-shadow(0 0 4px var(--accent-glow, var(--blue-glow)));
}
.gauge-value { font-size: 20px; font-weight: 800; fill: var(--text-primary); font-family: var(--font); }
.gauge-label { font-size: 8px; fill: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.6px; }
.gauge-sub {
  font-size: 0.65rem;
  color: var(--text-secondary);
  margin-top: 6px;
  font-variant-numeric: tabular-nums;
}
.gauge-bar {
  height: 4px;
  background: rgba(255,255,255,0.05);
  border-radius: 2px;
  margin: 10px 8px 0;
  overflow: hidden;
}
.gauge-bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 1.2s cubic-bezier(0.4, 0, 0.2, 1);
}

/* ===== Info Row ===== */
.info-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
  margin-bottom: 20px;
}
.info-card {
  background: var(--bg-card);
  backdrop-filter: blur(12px);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  display: flex;
  align-items: center;
  gap: 10px;
  transition: all 0.2s;
}
.info-card:hover { border-color: var(--border-hover); background: var(--bg-card-hover); }
.info-icon { font-size: 1rem; width: 24px; text-align: center; opacity: 0.7; }
.info-label { font-size: 0.65rem; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.4px; }
.info-value { font-size: 0.82rem; font-weight: 600; margin-left: auto; white-space: nowrap; font-variant-numeric: tabular-nums; }

/* ===== Charts ===== */
.chart-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}
.chart-card {
  background: var(--bg-card);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px;
  box-shadow: var(--shadow-sm);
  transition: all 0.25s cubic-bezier(0.4,0,0.2,1);
}
.chart-card:hover { border-color: var(--border-hover); transform: translateY(-2px); box-shadow: var(--shadow); }
.chart-card.half { flex: 1; min-width: 220px; }
.chart-title {
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  color: var(--text-secondary);
  margin-bottom: 10px;
}
.chart-svg { width: 100%; height: 80px; display: block; }
.chart-svg-big { width: 100%; height: 140px; display: block; }

/* ===== Detail Tab Grid ===== */
.tab-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 16px;
}
.card-full {
  background: var(--bg-card);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow-sm);
  transition: all 0.25s cubic-bezier(0.4,0,0.2,1);
}
.card-full:hover { border-color: var(--border-hover); box-shadow: var(--shadow); }
.card-full h3 {
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 0.7px;
  text-transform: uppercase;
  color: var(--text-secondary);
  margin-bottom: 14px;
}

/* ===== Load display ===== */
.load-row { display: flex; gap: 24px; justify-content: center; }
.load-item { text-align: center; }
.load-val {
  font-size: 1.6rem;
  font-weight: 700;
  display: block;
  color: var(--blue);
  font-variant-numeric: tabular-nums;
}
.load-lbl {
  font-size: 0.65rem;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.6px;
}

/* ===== Process Table ===== */
.table-scroll { overflow-x: auto; }
.proc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.76rem;
}
.proc-table th {
  text-align: left;
  padding: 9px 10px;
  color: var(--text-secondary);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
  position: sticky;
  top: 0;
  background: rgba(7,7,14,0.95);
  backdrop-filter: blur(8px);
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.proc-table th:hover { color: var(--text-primary); }
.proc-table td {
  padding: 7px 10px;
  border-bottom: 1px solid rgba(255,255,255,0.03);
  vertical-align: middle;
}
.proc-table tr { transition: background 0.15s; }
.proc-table tr:nth-child(even) td { background: rgba(255,255,255,0.012); }
.proc-table tr:hover td { background: rgba(255,255,255,0.04); }
.sort-arrow { font-size: 0.6rem; margin-left: 2px; opacity: 0.5; }
th.sorted .sort-arrow { opacity: 1; color: var(--blue); }

.cpu-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.72rem;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.cpu-badge.high { background: var(--rose-bg); color: var(--rose); }
.cpu-badge.med { background: var(--amber-bg); color: var(--amber); }
.cpu-badge.low { background: var(--emerald-bg); color: var(--emerald); }

.progress-inline {
  display: inline-block;
  width: 56px;
  height: 4px;
  background: rgba(255,255,255,0.06);
  border-radius: 2px;
  vertical-align: middle;
  margin-right: 6px;
  overflow: hidden;
}
.progress-inline-fill {
  height: 100%;
  border-radius: 2px;
  background: var(--blue);
  transition: width 0.6s cubic-bezier(0.4,0,0.2,1);
}
.pid-cell { color: var(--text-secondary); font-family: var(--mono); font-size: 0.7rem; }

/* ===== Memory Detail ===== */
.mem-detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.mem-item {
  display: flex;
  justify-content: space-between;
  padding: 6px 0;
  font-size: 0.8rem;
  border-bottom: 1px solid rgba(255,255,255,0.03);
}
.mem-item .lbl { color: var(--text-secondary); }
.mem-item .val { font-weight: 600; font-variant-numeric: tabular-nums; }

/* ===== Per-Core CPU ===== */
.core-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 10px;
}
.core-card {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 14px;
  text-align: center;
  transition: all 0.2s;
}
.core-card:hover { border-color: var(--border-hover); background: var(--bg-card-hover); transform: translateY(-2px); }
.core-label {
  font-size: 0.65rem;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin-bottom: 6px;
}
.core-pct {
  font-size: 1.3rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.core-pct.blue { color: var(--blue); }
.core-pct.emerald { color: var(--emerald); }
.core-pct.amber { color: var(--amber); }
.core-pct.rose { color: var(--rose); }
.core-bar {
  height: 4px;
  background: rgba(255,255,255,0.06);
  border-radius: 2px;
  margin-top: 8px;
  overflow: hidden;
}
.core-bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 1.0s cubic-bezier(0.4,0,0.2,1);
}

/* ===== Network Interfaces ===== */
.iface-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 10px;
}
.iface-card {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 14px;
  transition: all 0.2s;
}
.iface-card:hover { border-color: var(--border-hover); background: var(--bg-card-hover); }
.iface-name {
  font-size: 0.82rem;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--amber);
}
.iface-stat {
  display: flex;
  justify-content: space-between;
  padding: 3px 0;
  font-size: 0.73rem;
}
.iface-stat .lbl { color: var(--text-secondary); }
.iface-stat .val { font-weight: 500; font-variant-numeric: tabular-nums; }
.iface-speed {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--emerald);
  display: block;
  padding: 4px 0;
  font-variant-numeric: tabular-nums;
}
.iface-speed.tx { color: var(--blue); }

/* ===== Docker ===== */
.docker-unavail { text-align: center; padding: 30px; color: var(--text-secondary); }
.docker-unavail .icon { font-size: 2rem; margin-bottom: 10px; display: block; opacity: 0.5; }
.docker-unavail .msg { font-size: 0.9rem; }
.docker-unavail .sub { font-size: 0.75rem; margin-top: 4px; color: var(--text-muted); }

/* ===== Filesystems ===== */
.fs-grid { display: grid; grid-template-columns: 1fr; gap: 8px; }
.fs-card {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 14px;
  display: flex;
  align-items: center;
  gap: 14px;
  transition: all 0.2s;
}
.fs-card:hover { border-color: var(--border-hover); background: var(--bg-card-hover); }
.fs-info { min-width: 140px; }
.fs-mount { font-size: 0.82rem; font-weight: 600; }
.fs-type {
  font-size: 0.62rem;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.fs-bar-wrap { flex: 1; }
.fs-bar { height: 5px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; }
.fs-bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
.fs-stats {
  text-align: right;
  min-width: 110px;
  font-size: 0.7rem;
  color: var(--text-secondary);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

/* ===== Overview Process Tables ===== */
.chart-card.half .proc-table.mini th { font-size: 0.63rem; padding: 5px 6px; }
.chart-card.half .proc-table.mini td { font-size: 0.68rem; padding: 4px 6px; }

/* ===== Search Input ===== */
.proc-search {
  width: 100%;
  padding: 9px 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  color: var(--text-primary);
  font-size: 0.8rem;
  outline: none;
  box-sizing: border-box;
  font-family: var(--font);
  transition: border-color 0.2s;
}
.proc-search:focus { border-color: var(--blue); }
.proc-search::placeholder { color: var(--text-muted); }

/* ===== Loading ===== */
.loading-overlay { display: none; align-items: center; justify-content: center; min-height: 200px; }
.spinner { width: 28px; height: 28px; border: 3px solid rgba(255,255,255,0.08); border-top-color: var(--emerald); border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
#main-content.loading .tab-content { display: none; }
#main-content.loading .loading-overlay { display: flex; }

/* ===== Scrollbar ===== */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.06); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.12); }

/* ===== Responsive ===== */
@media (max-width: 768px) {
  .container { padding: 12px; }
  header { flex-direction: column; align-items: stretch; top: 8px; padding: 12px 16px; }
  .h-left { justify-content: space-between; }
  .h-right { justify-content: space-between; flex-wrap: wrap; }
  .tabs-wrap { display: none; }
  .tab-select { display: block; }
  .gauge-row { grid-template-columns: 1fr; }
  .gauge-svg { width: 96px; height: 96px; }
  .chart-row { grid-template-columns: 1fr; }
  .info-row { grid-template-columns: repeat(2, 1fr); }
  .tab-grid { grid-template-columns: 1fr; }
  .load-val { font-size: 1.3rem; }
  .mem-detail-grid { grid-template-columns: 1fr; }
  .core-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
</head>
<body>
<div class="container">
  <!-- Header -->
  <header>
    <div class="h-left">
      <h1>📊 Server Dashboard</h1>
      <span class="h-badge host" id="hostname-el">--</span>
      <span class="h-badge uptime"><span class="live-dot"></span> <span id="uptime-val">--</span></span>
    </div>
    <div class="h-right">
      <span class="h-badge os" id="os-val">--</span>
      <span class="h-badge py" id="py-ver">--</span>
      <span class="last-updated" id="last-updated">--</span>
      <div class="header-controls">
        <button class="btn-icon" id="export-btn" title="Export JSON">&#x21E9;</button>
        <button class="btn-icon" id="refresh-btn" title="Refresh now">&#x21bb;</button>
        <select class="rate-select" id="rate-select">
          <option value="2000">2s</option>
          <option value="5000" selected>5s</option>
          <option value="10000">10s</option>
          <option value="30000">30s</option>
          <option value="0">Pause</option>
        </select>
      </div>
    </div>
  </header>

  <!-- Tab nav -->
  <select class="tab-select" id="tab-select">
    <option value="overview">Overview</option>
    <option value="cpu">CPU</option>
    <option value="memory">Memory</option>
    <option value="disk">Disk</option>
    <option value="network">Network</option>
    <option value="docker">Docker</option>
    <option value="processes">Processes</option>
  </select>
  <nav class="tabs-wrap" id="tabs">
    <button class="tab-btn active" data-tab="overview">Overview</button>
    <button class="tab-btn" data-tab="cpu">CPU</button>
    <button class="tab-btn" data-tab="memory">Memory</button>
    <button class="tab-btn" data-tab="disk">Disk</button>
    <button class="tab-btn" data-tab="network">Network</button>
    <button class="tab-btn" data-tab="docker">Docker</button>
    <button class="tab-btn" data-tab="processes">Processes</button>
  </nav>

  <!-- ============ OVERVIEW ============ -->
  <div class="tab-content active" id="tab-overview">
    <div class="loading-placeholder" style="display:none;text-align:center;padding:40px;color:var(--text-muted);"><div class="spinner"></div></div>
    <div class="gauge-row">
      <!-- CPU Gauge -->
      <div class="gauge-card cpu">
        <div class="gauge-title">CPU Load</div>
        <svg class="gauge-svg" viewBox="0 0 140 140">
          <defs><linearGradient id="g-cpu" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" stop-color="#3b82f6"/><stop offset="100%" stop-color="#6366f1"/></linearGradient></defs>
          <circle class="gauge-track" cx="70" cy="70" r="54"/>
          <circle class="gauge-arc" id="cpu-arc" cx="70" cy="70" r="54" stroke="url(#g-cpu)"/>
          <text class="gauge-value" id="cpu-gauge-val" x="70" y="62" text-anchor="middle">0%</text>
          <text class="gauge-label" x="70" y="80" text-anchor="middle" id="cpu-gauge-sub">idle</text>
        </svg>
        <div class="gauge-sub" id="cpu-detail">Load: 0.00 / 0.00 / 0.00</div>
        <div class="gauge-bar"><div class="gauge-bar-fill" id="cpu-bar" style="width:0%;background:var(--blue);"></div></div>
      </div>
      <!-- Memory Gauge -->
      <div class="gauge-card mem">
        <div class="gauge-title">Memory</div>
        <svg class="gauge-svg" viewBox="0 0 140 140">
          <defs><linearGradient id="g-mem" x1="0%" y1="0%" x2="0%" y2="100%"><stop offset="0%" stop-color="#10b981"/><stop offset="100%" stop-color="#34d399"/></linearGradient></defs>
          <circle class="gauge-track" cx="70" cy="70" r="54"/>
          <circle class="gauge-arc" id="mem-arc" cx="70" cy="70" r="54" stroke="url(#g-mem)"/>
          <text class="gauge-value" id="mem-gauge-val" x="70" y="62" text-anchor="middle">0%</text>
          <text class="gauge-label" x="70" y="80" text-anchor="middle" id="mem-gauge-sub">used</text>
        </svg>
        <div class="gauge-sub" id="mem-detail">-- / --</div>
        <div class="gauge-bar"><div class="gauge-bar-fill" id="mem-bar" style="width:0%;background:var(--emerald);"></div></div>
      </div>
      <!-- Disk Gauge -->
      <div class="gauge-card disk">
        <div class="gauge-title">Disk</div>
        <svg class="gauge-svg" viewBox="0 0 140 140">
          <defs><linearGradient id="g-disk" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#8b5cf6"/><stop offset="100%" stop-color="#a78bfa"/></linearGradient></defs>
          <circle class="gauge-track" cx="70" cy="70" r="54"/>
          <circle class="gauge-arc" id="disk-arc" cx="70" cy="70" r="54" stroke="url(#g-disk)"/>
          <text class="gauge-value" id="disk-gauge-val" x="70" y="62" text-anchor="middle">0%</text>
          <text class="gauge-label" x="70" y="80" text-anchor="middle" id="disk-gauge-sub">used</text>
        </svg>
        <div class="gauge-sub" id="disk-detail">-- / --</div>
        <div class="gauge-bar"><div class="gauge-bar-fill" id="disk-bar" style="width:0%;background:var(--purple);"></div></div>
      </div>
      <!-- Network Gauge -->
      <div class="gauge-card net">
        <div class="gauge-title">Network</div>
        <svg class="gauge-svg" viewBox="0 0 140 140">
          <defs><linearGradient id="g-net" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" stop-color="#f59e0b"/><stop offset="100%" stop-color="#f97316"/></linearGradient></defs>
          <circle class="gauge-track" cx="70" cy="70" r="54"/>
          <circle class="gauge-arc" id="net-arc" cx="70" cy="70" r="54" stroke="url(#g-net)" stroke-dasharray="339.292" stroke-dashoffset="339.292"/>
          <text class="gauge-value" id="net-gauge-val" x="70" y="62" text-anchor="middle">--</text>
          <text class="gauge-label" x="70" y="80" text-anchor="middle" id="net-gauge-sub">interfaces</text>
        </svg>
        <div class="gauge-sub" id="net-detail">RX: -- / TX: --</div>
        <div class="gauge-bar"><div class="gauge-bar-fill" id="net-bar" style="width:0%;background:var(--amber);"></div></div>
      </div>
    </div>

    <div class="chart-row">
      <div class="chart-card">
        <div class="chart-title">CPU Load History</div>
        <svg class="chart-svg" id="chart-cpu" viewBox="0 0 400 80" preserveAspectRatio="none"></svg>
      </div>
      <div class="chart-card">
        <div class="chart-title">Memory Usage History</div>
        <svg class="chart-svg" id="chart-mem" viewBox="0 0 400 80" preserveAspectRatio="none"></svg>
      </div>
    </div>

    <div class="info-row">
      <div class="info-card"><span class="info-icon">&#x2699;</span><span class="info-label">Processes</span><span class="info-value" id="proc-count">--</span></div>
      <div class="info-card"><span class="info-icon">&#x1F310;</span><span class="info-label">IP Address</span><span class="info-value" id="ip-val">--</span></div>
      <div class="info-card"><span class="info-icon">&#x1F504;</span><span class="info-label">Refresh Rate</span><span class="info-value" id="rate-val">5s</span></div>
      <div class="info-card"><span class="info-icon">&#x1F4CB;</span><span class="info-label">Cores</span><span class="info-value" id="cores-val">--</span></div>
    </div>

    <div class="chart-row">
      <div class="chart-card half">
        <div class="chart-title">Top CPU Processes</div>
        <div class="table-scroll" style="max-height:180px;">
          <table class="proc-table mini" id="ov-cpu-proc-table">
            <thead><tr><th>PID</th><th>Name</th><th>CPU%</th></tr></thead>
            <tbody id="ov-cpu-proc-body"></tbody>
          </table>
        </div>
      </div>
      <div class="chart-card half">
        <div class="chart-title">Top Memory Processes</div>
        <div class="table-scroll" style="max-height:180px;">
          <table class="proc-table mini" id="ov-mem-proc-table">
            <thead><tr><th>PID</th><th>Name</th><th>MEM%</th></tr></thead>
            <tbody id="ov-mem-proc-body"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ CPU ============ -->
  <div class="tab-content" id="tab-cpu">
    <div class="tab-grid">
      <div class="card-full">
        <h3>Load Average</h3>
        <div class="load-row">
          <div class="load-item"><span class="load-val" id="cpu-la1">0.00</span><span class="load-lbl">1 min</span></div>
          <div class="load-item"><span class="load-val" id="cpu-la5">0.00</span><span class="load-lbl">5 min</span></div>
          <div class="load-item"><span class="load-val" id="cpu-la15">0.00</span><span class="load-lbl">15 min</span></div>
        </div>
      </div>
      <div class="card-full">
        <h3>Per-Core Usage</h3>
        <div class="core-grid" id="core-grid"></div>
      </div>
      <div class="card-full">
        <h3>CPU Load History</h3>
        <svg class="chart-svg-big" id="chart-cpu-full" viewBox="0 0 400 140" preserveAspectRatio="none"></svg>
      </div>
      <div class="card-full">
        <h3>Top CPU Processes</h3>
        <div class="table-scroll">
          <table class="proc-table" id="cpu-proc-table">
            <thead><tr><th>PID</th><th>Name</th><th data-sort="cpu_pct" class="sorted">CPU% <span class="sort-arrow">&#x25B4;</span></th><th>MEM%</th></tr></thead>
            <tbody id="cpu-proc-body"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ MEMORY ============ -->
  <div class="tab-content" id="tab-memory">
    <div class="tab-grid">
      <div class="card-full">
        <h3>Memory Usage</h3>
        <svg class="chart-svg-big" id="chart-mem-full" viewBox="0 0 400 140" preserveAspectRatio="none"></svg>
      </div>
      <div class="card-full">
        <h3>Memory Details</h3>
        <div class="mem-detail-grid">
          <div class="mem-item"><span class="lbl">Total</span><span class="val" id="mem-total">--</span></div>
          <div class="mem-item"><span class="lbl">Used</span><span class="val" id="mem-used">--</span></div>
          <div class="mem-item"><span class="lbl">Available</span><span class="val" id="mem-avail">--</span></div>
          <div class="mem-item"><span class="lbl">Free</span><span class="val" id="mem-free">--</span></div>
          <div class="mem-item" style="border-top:1px solid rgba(255,255,255,0.06);padding-top:8px;margin-top:4px;"><span class="lbl">Swap Total</span><span class="val" id="swap-total">--</span></div>
          <div class="mem-item"><span class="lbl">Swap Used</span><span class="val" id="swap-used">--</span></div>
          <div class="mem-item full-row"><span class="lbl">Swap Usage</span><span class="val"><div class="progress-inline" style="width:120px;display:inline-block;vertical-align:middle;margin-right:6px;"><div class="progress-inline-fill" id="swap-bar" style="width:0%;background:var(--rose);"></div></div><span id="swap-pct">0%</span></span></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ DISK ============ -->
  <div class="tab-content" id="tab-disk">
    <div class="tab-grid">
      <div class="card-full">
        <h3>Disk Usage</h3>
        <svg class="chart-svg-big" id="chart-disk-full" viewBox="0 0 400 140" preserveAspectRatio="none"></svg>
      </div>
      <div class="card-full">
        <h3>Disk I/O</h3>
        <div class="mem-detail-grid">
          <div class="mem-item"><span class="lbl">Device</span><span class="val" id="disk-device">--</span></div>
          <div class="mem-item"><span class="lbl">Total Reads</span><span class="val" id="disk-reads">--</span></div>
          <div class="mem-item"><span class="lbl">Total Writes</span><span class="val" id="disk-writes">--</span></div>
          <div class="mem-item"><span class="lbl">Read Speed</span><span class="val" id="disk-read-speed">--</span></div>
          <div class="mem-item"><span class="lbl">Write Speed</span><span class="val" id="disk-write-speed">--</span></div>
        </div>
      </div>
      <div class="card-full">
        <h3>Filesystems</h3>
        <div class="fs-grid" id="fs-grid"></div>
      </div>
    </div>
  </div>

  <!-- ============ NETWORK ============ -->
  <div class="tab-content" id="tab-network">
    <div class="tab-grid">
      <div class="card-full">
        <h3>Network Info</h3>
        <div class="mem-detail-grid">
          <div class="mem-item"><span class="lbl">Hostname</span><span class="val" id="net-host">--</span></div>
          <div class="mem-item"><span class="lbl">IP Address</span><span class="val" id="net-ip">--</span></div>
        </div>
      </div>
      <div class="card-full">
        <h3>Network Interfaces</h3>
        <div class="iface-grid" id="iface-grid"></div>
      </div>
      <div class="card-full">
        <h3>Network Activity</h3>
        <svg class="chart-svg-big" id="chart-net-full" viewBox="0 0 400 140" preserveAspectRatio="none"></svg>
      </div>
      <div class="card-full">
        <h3>Listening Ports</h3>
        <div class="table-scroll" style="max-height:200px;">
          <table class="proc-table" id="ports-table">
            <thead><tr><th>Port</th><th>Address</th><th>Process</th><th>PID</th></tr></thead>
            <tbody id="ports-body"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ DOCKER ============ -->
  <div class="tab-content" id="tab-docker">
    <div class="tab-grid">
      <div class="card-full">
        <h3>Container Status</h3>
        <div id="docker-status"></div>
      </div>
      <div class="card-full" id="docker-containers-card" style="display:none;">
        <h3>Running Containers</h3>
        <div class="table-scroll">
          <table class="proc-table" id="docker-table">
            <thead><tr><th>ID</th><th>Name</th><th>Image</th><th>Status</th><th>CPU%</th><th>MEM%</th><th>Memory</th><th>Net I/O</th></tr></thead>
            <tbody id="docker-body"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ PROCESSES ============ -->
  <div class="tab-content" id="tab-processes">
    <div class="card-full">
      <h3>Running Processes</h3>
      <div style="margin-bottom:10px;">
        <input type="text" id="proc-search" class="proc-search" placeholder="&#x1F50D; Search by name, PID, user...">
      </div>
      <div class="table-scroll">
        <table class="proc-table" id="proc-table-full">
          <thead>
            <tr>
              <th data-col="pid">PID</th>
              <th data-col="name">Name</th>
              <th data-col="cpu_percent" class="sorted">CPU% <span class="sort-arrow">&#x25B4;</span></th>
              <th data-col="mem_percent">MEM%</th>
              <th data-col="mem_mb">MEM (MB)</th>
              <th data-col="user">User</th>
              <th data-col="status">Status</th>
            </tr>
          </thead>
          <tbody id="proc-body-full"></tbody>
        </table>
      </div>
    </div>
  </div>

  <div style="text-align:center;font-size:0.7rem;color:var(--text-muted);margin-top:16px;padding:10px;">Server Dashboard &mdash; Live monitoring</div>
</div>

<script>
// ===== RING BUFFER (O(1) amortized) =====
class RingBuffer {
  constructor(maxLen) {
    this.buf = new Array(maxLen);
    this.maxLen = maxLen;
    this.head = 0;
    this._len = 0;
  }
  push(item) {
    this.buf[this.head] = item;
    this.head = (this.head + 1) % this.maxLen;
    if (this._len < this.maxLen) this._len++;
  }
  toArray() {
    if (this._len === 0) return [];
    const res = new Array(this._len);
    const start = this._len < this.maxLen ? 0 : this.head;
    for (let i = 0; i < this._len; i++) res[i] = this.buf[(start + i) % this.maxLen];
    return res;
  }
  get length() { return this._len; }
}

// ===== STATE =====
let statsData = null;
let processData = [];
let diskIOData = null;
let currentTab = 'overview';
let sortCol = 'cpu_percent', sortDir = -1;
let refreshInterval = null;
let baseUptimeSeconds = 0;
let procSearchValue = '';
let lastCpuCoresData = [];
let lastFilesystems = [];
const MAX_HIST = 60;
const cpuHistory = new RingBuffer(MAX_HIST);  // {time, val}
const memHistory = new RingBuffer(MAX_HIST);  // {time, val}
const diskHistory = new RingBuffer(MAX_HIST); // {time, speed}
const netHistory = new RingBuffer(MAX_HIST);  // {time, rx, tx}

// ===== VISIBILITY API =====
let pageVisible = true;
document.addEventListener('visibilitychange', () => { pageVisible = !document.hidden; });

// ===== DEBOUNCE & VISIBILITY HELPERS =====
function debounce(fn, ms) {
  let timer;
  return function(...args) { clearTimeout(timer); timer = setTimeout(() => fn.apply(this, args), ms); };
}
function visibleOnly(fn) {
  return () => { if (pageVisible) fn(); };
}

// ===== GAUGE CONST =====
const GAUGE_CIRCUM = 2 * Math.PI * 78;
// ===== TABS =====
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  const tc = document.getElementById('tab-' + tab);
  if (tc) tc.classList.add('active');
  const btn = document.querySelector('.tab-btn[data-tab="' + tab + '"]');
  if (btn) btn.classList.add('active');
  document.getElementById('tab-select').value = tab;
  if (tab === 'processes') fetchProcesses();
  if (tab === 'disk') { fetchDiskIO(); fetchFilesystems(); }
  if (tab === 'cpu') fetchCPUCores();
  if (tab === 'network') { fetchNetworkInterfaces(); fetchPorts(); }
  if (tab === 'docker') fetchDocker();
}
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});
document.getElementById('tab-select').addEventListener('change', e => switchTab(e.target.value));

// ===== REFRESH =====
let rateMs = 5000;
function setRefreshRate(ms) {
  rateMs = ms;
  if (refreshInterval) { clearInterval(refreshInterval); refreshInterval = null; }
  if (ms > 0) refreshInterval = setInterval(() => { if (pageVisible) fetchStats(); }, ms);
}
document.getElementById('rate-select').addEventListener('change', e => {
  setRefreshRate(parseInt(e.target.value));
});
document.getElementById('refresh-btn').addEventListener('click', () => {
  const btn = document.getElementById('refresh-btn');
  btn.classList.remove('spinning'); void btn.offsetWidth;
  btn.classList.add('spinning');
  fetchStats();
  if (currentTab === 'processes') fetchProcesses();
  if (currentTab === 'disk') fetchDiskIO();
});

// ===== HELPERS =====
function fmtBytes(n) {
  for (const u of ['B','KB','MB','GB','TB']) { if (n < 1024) return n.toFixed(1) + ' ' + u; n /= 1024; }
  return n.toFixed(1) + ' PB';
}

function gaugeColor(pct) {
  if (pct > 85) return '#f43f5e';
  if (pct > 60) return '#f59e0b';
  return '#22c55e';
}

function setGauge(arcId, pct) {
  const arc = document.getElementById(arcId);
  if (!arc) return;
  const offset = GAUGE_CIRCUM * (1 - Math.min(Math.max(pct, 0), 100) / 100);
  arc.setAttribute('stroke-dasharray', GAUGE_CIRCUM);
  arc.setAttribute('stroke-dashoffset', offset);
}

function fmtDuration(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  let r = '';
  if (d) r += d + 'd ';
  if (h || d) r += h + 'h ';
  r += m + 'm';
  return r.trim();
}

// ===== SVG SMOOTH PATH =====
function smoothPathPoints(points, w, h, key, minVal, maxVal) {
  if (!points || points.length < 2) return [];
  const rng = maxVal - minVal || 1;
  return points.map((d, i) => ({
    x: (i / (points.length - 1)) * w,
    y: h - ((d[key] - minVal) / rng) * (h - 8) - 4
  }));
}

function buildSmoothPath(pts) {
  if (!pts || pts.length < 2) return '';
  let d = 'M' + pts[0].x.toFixed(1) + ',' + pts[0].y.toFixed(1);
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[Math.min(pts.length - 1, i + 2)];
    const cp1x = p1.x + (p2.x - p0.x) / 6;
    const cp1y = p1.y + (p2.y - p0.y) / 6;
    const cp2x = p2.x - (p3.x - p1.x) / 6;
    const cp2y = p2.y - (p3.y - p1.y) / 6;
    d += ' C' + cp1x.toFixed(1) + ',' + cp1y.toFixed(1) + ' ' + cp2x.toFixed(1) + ',' + cp2y.toFixed(1) + ' ' + p2.x.toFixed(1) + ',' + p2.y.toFixed(1);
  }
  return d;
}

function renderAreaChart(svgId, data, key, color, minVal, maxVal) {
  const svg = document.getElementById(svgId);
  if (!svg || !data || data.length < 2) { if (svg) svg.innerHTML = ''; return; }
  const vb = svg.getAttribute('viewBox') || '0 0 400 80';
  const w = parseInt(vb.split(/\s+/)[2]) || 400;
  const h = parseInt(vb.split(/\s+/)[3]) || 80;
  const pts = smoothPathPoints(data, w, h, key, minVal, maxVal);
  if (pts.length < 2) return;
  const linePath = buildSmoothPath(pts);
  const areaPath = linePath + ' L' + pts[pts.length-1].x.toFixed(1) + ',' + h + ' L' + pts[0].x.toFixed(1) + ',' + h + ' Z';
  const id = 'g-' + svgId.replace(/[^a-z0-9]/g,'');
  // Time labels (4 evenly spaced)
  let labels = '';
  const labelCount = Math.min(data.length, 4);
  const step = Math.max(1, Math.floor((data.length - 1) / (labelCount - 1)));
  for (let i = 0; i < data.length; i += step) {
    if (data[i] && data[i].time) {
      const d = new Date(data[i].time);
      const label = String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
      const x = (i / (data.length - 1)) * w;
      labels += '<text x="' + x.toFixed(1) + '" y="' + (h - 2) + '" fill="rgba(255,255,255,0.15)" font-size="6" text-anchor="middle">' + label + '</text>';
    }
  }
  svg.innerHTML = '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="' + color + '" stop-opacity="0.35"/><stop offset="100%" stop-color="' + color + '" stop-opacity="0.01"/></linearGradient></defs>'
    + '<path d="' + areaPath + '" fill="url(#' + id + ')" />'
    + '<path d="' + linePath + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />'
    + labels;
}

// ===== ANIMATE VALUE =====
function animateValue(el, start, end, duration, suffix) {
  if (!el) return;
  const startTime = performance.now();
  const isNum = typeof end === 'number';
  function tick(now) {
    const t = Math.min((now - startTime) / duration, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    const val = start + (end - start) * eased;
    el.textContent = (isNum ? val.toFixed(1) : Math.round(val)) + (suffix || '');
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ===== FETCH =====
async function fetchStats() {
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();
    statsData = data;
    renderHeader(data);
    renderOverview(data);
    if (currentTab === 'cpu') renderCPUTab(data);
    if (currentTab === 'memory') renderMemoryTab(data);
    if (currentTab === 'disk') renderDiskTab(data);
    if (currentTab === 'network') renderNetworkTab(data);
    markInitialized();
  } catch(e) { markInitialized(); /* ignore */ }
}

async function fetchProcesses() {
  try {
    const res = await fetch('/api/processes');
    const data = await res.json();
    processData = data;
    renderProcessTables(data);
  } catch(e) { markInitialized(); /* ignore */ }
}

async function fetchDiskIO() {
  try {
    const res = await fetch('/api/disk-io');
    const data = await res.json();
    diskIOData = data;
    const speed = data.read_speed_mbps || 0;
    diskHistory.push({time: Date.now(), speed: speed});
    if (currentTab === 'disk') renderDiskTab(null, data);
  } catch(e) { markInitialized(); /* ignore */ }
}

// ===== RENDER HEADER =====
function renderHeader(data) {
  document.getElementById('hostname-el').textContent = data.network.hostname;
  document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
  document.getElementById('uptime-val').textContent = data.uptime;
  document.getElementById('py-ver').textContent = 'v' + data.python_version;
  document.getElementById('os-val').textContent = data.os.name + ' ' + data.os.version;
  document.getElementById('ip-val').textContent = data.network.ip;
  document.getElementById('cores-val').textContent = data.cpu.cores;
  const ms = parseInt(document.getElementById('rate-select').value) || 0;
  document.getElementById('rate-val').textContent = ms > 0 ? (ms / 1000) + 's' : 'Paused';
  // Store for live uptime counter
  window._uptimeStart = Date.now();
  if (data.uptime_seconds) {
    baseUptimeSeconds = data.uptime_seconds;
    updateUptime();
  }
}

// ===== RENDER OVERVIEW =====
function renderOverview(data) {
  const cpuPct = Math.min(100, (data.cpu.load_1min / data.cpu.cores) * 100);
  const memPct = data.memory.total_bytes > 0 ? (data.memory.used_bytes / data.memory.total_bytes * 100) : 0;
  const diskPct = data.disk.usage_percent;

  // Gauges
  setGauge('cpu-arc', cpuPct);
  document.getElementById('cpu-gauge-val').textContent = cpuPct.toFixed(0) + '%';
  document.getElementById('cpu-gauge-sub').textContent = data.cpu.cores + ' cores';
  document.getElementById('cpu-detail').textContent = 'Load: ' + data.cpu.load_1min.toFixed(2) + ' / ' + data.cpu.load_5min.toFixed(2) + ' / ' + data.cpu.load_15min.toFixed(2);
  document.getElementById('cpu-bar').style.width = cpuPct + '%';
  document.getElementById('cpu-bar').style.background = gaugeColor(cpuPct);

  setGauge('mem-arc', memPct);
  document.getElementById('mem-gauge-val').textContent = memPct.toFixed(0) + '%';
  document.getElementById('mem-gauge-sub').textContent = 'used';
  document.getElementById('mem-detail').textContent = data.memory.used + ' / ' + data.memory.total;
  document.getElementById('mem-bar').style.width = memPct + '%';
  document.getElementById('mem-bar').style.background = gaugeColor(memPct);

  setGauge('disk-arc', diskPct);
  document.getElementById('disk-gauge-val').textContent = diskPct.toFixed(0) + '%';
  document.getElementById('disk-gauge-sub').textContent = 'used';
  document.getElementById('disk-detail').textContent = data.disk.used + ' / ' + data.disk.total;
  document.getElementById('disk-bar').style.width = diskPct + '%';
  document.getElementById('disk-bar').style.background = gaugeColor(diskPct);

  document.getElementById('proc-count').textContent = data.process_count;

  // Network gauge - show hostname and IP
  document.getElementById('net-detail').textContent = data.network.ip || '--';
  document.getElementById('net-gauge-val').textContent = data.network.hostname || '--';
  document.getElementById('net-gauge-sub').textContent = 'hostname';

  // History update
  const t = Date.now();
  cpuHistory.push({time: t, val: cpuPct});
  memHistory.push({time: t, val: memPct});

  renderAreaChart('chart-cpu', cpuHistory.toArray(), 'val', '#3b82f6', 0, 100);
  renderAreaChart('chart-mem', memHistory.toArray(), 'val', '#10b981', 0, 100);
}

// ===== RENDER CPU TAB =====
function renderCPUTab(data) {
  document.getElementById('cpu-la1').textContent = data.cpu.load_1min.toFixed(2);
  document.getElementById('cpu-la5').textContent = data.cpu.load_5min.toFixed(2);
  document.getElementById('cpu-la15').textContent = data.cpu.load_15min.toFixed(2);
  renderAreaChart('chart-cpu-full', cpuHistory.toArray(), 'val', '#3b82f6', 0, 100);

  // Per-core CPU
  fetchCPUCores();

  // Top processes by CPU
  fetchProcesses();
}

// ===== RENDER MEMORY TAB =====
function renderMemoryTab(data) {
  renderAreaChart('chart-mem-full', memHistory.toArray(), 'val', '#10b981', 0, 100);
  document.getElementById('mem-total').textContent = data.memory.total;
  document.getElementById('mem-used').textContent = data.memory.used;
  document.getElementById('mem-avail').textContent = data.memory.available;
  // We don't have MemFree directly but we can estimate
  const freeBytes = data.memory.total_bytes - data.memory.used_bytes;
  document.getElementById('mem-free').textContent = fmtBytes(freeBytes);
  // Swap
  document.getElementById('swap-total').textContent = data.memory.swap_total || '--';
  document.getElementById('swap-used').textContent = data.memory.swap_used || '--';
  const sp = data.memory.swap_percent || 0;
  document.getElementById('swap-pct').textContent = sp + '%';
  document.getElementById('swap-bar').style.width = Math.min(sp, 100) + '%';
}

// ===== RENDER DISK TAB =====
function renderDiskTab(data, io) {
  if (data) {
    // Render chart from disk IO history (populated by fetchDiskIO every 4s)
    if (diskHistory.length >= 2) {
      renderAreaChart('chart-disk-full', diskHistory.toArray(), 'speed', '#8b5cf6', 0, Math.max(...diskHistory.toArray().map(d => d.speed), 1) * 1.2);
    }
    document.getElementById('disk-device').textContent = diskIOData ? diskIOData.device : '--';
    document.getElementById('disk-reads').textContent = diskIOData ? diskIOData.reads_mb.toFixed(1) + ' MB' : '--';
    document.getElementById('disk-writes').textContent = diskIOData ? diskIOData.writes_mb.toFixed(1) + ' MB' : '--';
    document.getElementById('disk-read-speed').textContent = (diskIOData && diskIOData.read_speed_mbps !== undefined) ? diskIOData.read_speed_mbps.toFixed(2) + ' MB/s' : '--';
    document.getElementById('disk-write-speed').textContent = (diskIOData && diskIOData.write_speed_mbps !== undefined) ? diskIOData.write_speed_mbps.toFixed(2) + ' MB/s' : '--';
  }
  if (io) {
    document.getElementById('disk-device').textContent = io.device || '--';
    document.getElementById('disk-reads').textContent = io.reads_mb ? io.reads_mb.toFixed(1) + ' MB' : '--';
    document.getElementById('disk-writes').textContent = io.writes_mb ? io.writes_mb.toFixed(1) + ' MB' : '--';
    document.getElementById('disk-read-speed').textContent = (io.read_speed_mbps !== undefined) ? io.read_speed_mbps.toFixed(2) + ' MB/s' : '--';
    document.getElementById('disk-write-speed').textContent = (io.write_speed_mbps !== undefined) ? io.write_speed_mbps.toFixed(2) + ' MB/s' : '--';
  }
  fetchFilesystems();
}

// ===== RENDER NETWORK TAB =====
function renderNetworkTab(data) {
  document.getElementById('net-host').textContent = data.network.hostname;
  document.getElementById('net-ip').textContent = data.network.ip;
  renderNetChart();
  fetchNetworkInterfaces();
}

// ===== REAL NETWORK HISTORY CHART =====
function renderNetChart() {
  const svg = document.getElementById('chart-net-full');
  const arr = netHistory.toArray();
  if (!svg || arr.length < 2) return;
  const w = 400, h = 140;
  let maxV = 1;
  for (const p of arr) {
    if (p.rx > maxV) maxV = p.rx;
    if (p.tx > maxV) maxV = p.tx;
  }
  maxV = Math.max(maxV, 1);
  const pad = 2;
  const step = (w - pad * 2) / Math.max(arr.length - 1, 1);

  let rxPoints = '', txPoints = '';
  for (let i = 0; i < arr.length; i++) {
    const x = pad + i * step;
    const rxY = h - pad - ((arr[i].rx / maxV) * (h - pad * 2));
    const txY = h - pad - ((arr[i].tx / maxV) * (h - pad * 2));
    rxPoints += (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + rxY.toFixed(1);
    txPoints += (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + txY.toFixed(1);
  }

  svg.innerHTML = ''
    + '<defs>'
    + '<linearGradient id="netRxGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#10b981" stop-opacity="0.25"/><stop offset="100%" stop-color="#10b981" stop-opacity="0"/></linearGradient>'
    + '<linearGradient id="netTxGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#3b82f6" stop-opacity="0.25"/><stop offset="100%" stop-color="#3b82f6" stop-opacity="0"/></linearGradient>'
    + '</defs>'
    // Grid lines
    + '<line x1="' + pad + '" y1="' + pad + '" x2="' + pad + '" y2="' + (h - pad) + '" stroke="rgba(255,255,255,0.06)" stroke-width="1"/>'
    + '<line x1="' + pad + '" y1="' + (h - pad) + '" x2="' + (w - pad) + '" y2="' + (h - pad) + '" stroke="rgba(255,255,255,0.06)" stroke-width="1"/>'
    // RX area
    + '<path d="' + rxPoints + 'L' + (pad + (arr.length - 1) * step).toFixed(1) + ',' + (h - pad) + 'L' + pad + ',' + (h - pad) + 'Z" fill="url(#netRxGrad)" opacity="0.5"/>'
    // RX line
    + '<path d="' + rxPoints + '" fill="none" stroke="#10b981" stroke-width="1.5" stroke-linejoin="round"/>'
    // TX area
    + '<path d="' + txPoints + 'L' + (pad + (arr.length - 1) * step).toFixed(1) + ',' + (h - pad) + 'L' + pad + ',' + (h - pad) + 'Z" fill="url(#netTxGrad)" opacity="0.5"/>'
    // TX line
    + '<path d="' + txPoints + '" fill="none" stroke="#3b82f6" stroke-width="1.5" stroke-linejoin="round"/>'
    // Legend
    + '<rect x="' + (w - 85) + '" y="4" width="80" height="28" rx="4" fill="rgba(0,0,0,0.4)"/>'
    + '<circle cx="' + (w - 78) + '" cy="12" r="3" fill="#10b981"/>'
    + '<text x="' + (w - 71) + '" y="14" fill="#10b981" font-size="7">RX</text>'
    + '<circle cx="' + (w - 78) + '" cy="23" r="3" fill="#3b82f6"/>'
    + '<text x="' + (w - 71) + '" y="25" fill="#3b82f6" font-size="7">TX</text>'
    // y-axis label
    + '<text x="' + (w - 4) + '" y="' + (h - 4) + '" fill="rgba(255,255,255,0.2)" font-size="6" text-anchor="end">' + fmtNet(maxV) + '</text>'
    + '<text x="' + (w - 4) + '" y="' + (pad + 8) + '" fill="rgba(255,255,255,0.15)" font-size="6" text-anchor="end">0</text>'
    ;
}

function fmtNet(bps) {
  if (bps < 1000) return Math.round(bps) + ' B/s';
  if (bps < 1000000) return (bps / 1000).toFixed(0) + ' KB/s';
  return (bps / 1000000).toFixed(1) + ' MB/s';
}

// ===== RENDER PER-CORE CPU =====
lastCpuCoresData = [];
function fetchCPUCores() {
  fetch('/api/cpu-per-core').then(r=>r.json()).then(data => {
    lastCpuCoresData = data;
    const grid = document.getElementById('core-grid');
    if (!grid) return;
    grid.innerHTML = data.map(c => {
      const pct = c.usage_percent;
      const cls = pct > 80 ? 'rose' : pct > 50 ? 'amber' : pct > 20 ? 'blue' : 'emerald';
      const barCls = pct > 80 ? '#f43f5e' : pct > 50 ? '#f59e0b' : pct > 20 ? '#3b82f6' : '#22c55e';
      const lbl = c.core.replace('cpu', 'CPU ');
      return '<div class="core-card">'
        + '<div class="core-label">' + lbl + '</div>'
        + '<div class="core-pct ' + cls + '">' + pct.toFixed(1) + '%</div>'
        + '<div class="core-bar"><div class="core-bar-fill" style="width:' + pct.toFixed(0) + '%;background:' + barCls + ';"></div></div>'
        + '</div>';
    }).join('');
  }).catch(() => console.warn('fetchCPUCores failed'));
}

// ===== RENDER NETWORK INTERFACES =====
function fetchNetworkInterfaces() {
  fetch('/api/network-interfaces').then(r=>r.json()).then(data => {
    const grid = document.getElementById('iface-grid');
    if (!grid) return;

    // Track total RX/TX speed for history chart
    let totalRx = 0, totalTx = 0;
    if (data && data.length > 0) {
      for (const iface of data) {
        totalRx += iface.rx_speed_bps || 0;
        totalTx += iface.tx_speed_bps || 0;
      }
    }
    netHistory.push({time: Date.now(), rx: totalRx, tx: totalTx});
    // Re-render chart if on network tab
    if (currentTab === 'network') renderNetChart();

    if (!data || data.length === 0) {
      grid.innerHTML = '<div style="color:var(--text-secondary);text-align:center;padding:20px;grid-column:1/-1;">No network interfaces found</div>';
      return;
    }
    grid.innerHTML = data.map(iface => {
      const rxMbps = (iface.rx_speed_bps / 1024 / 1024).toFixed(2);
      const txMbps = (iface.tx_speed_bps / 1024 / 1024).toFixed(2);
      return '<div class="iface-card">'
        + '<div class="iface-name">' + iface.name + '</div>'
        + '<div class="iface-stat"><span class="lbl">&#x2B07; RX</span><span class="val">' + iface.rx_total + '</span></div>'
        + '<div class="iface-stat"><span class="lbl">&#x2B06; TX</span><span class="val">' + iface.tx_total + '</span></div>'
        + '<div class="iface-stat"><span class="lbl">Packets</span><span class="val">' + iface.rx_packets + ' / ' + iface.tx_packets + '</span></div>'
        + '<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.04);">'
        + '<span class="iface-speed">&#x2B07; ' + iface.rx_speed + ' (RX)</span>'
        + '<span class="iface-speed tx">&#x2B06; ' + iface.tx_speed + ' (TX)</span>'
        + '</div>'
        + '</div>';
    }).join('');
    }).catch(() => console.warn('fetchNetworkInterfaces failed'));
}

// ===== LISTENING PORTS =====
function fetchPorts() {
  fetch('/api/ports').then(r=>r.json()).then(data => {
    const tbody = document.getElementById('ports-body');
    if (!tbody) return;
    if (!data || data.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-secondary);padding:16px;">No listening ports</td></tr>';
      return;
    }
    tbody.innerHTML = data.map(p => {
      const portColor = p.port < 1024 ? 'var(--amber)' : p.port < 10000 ? 'var(--emerald)' : 'var(--text-secondary)';
      return '<tr>'
        + '<td style="color:' + portColor + ';font-weight:600;">' + p.port + '</td>'
        + '<td>' + escHtml(p.address) + '</td>'
        + '<td>' + escHtml(p.process || '--') + '</td>'
        + '<td class="pid-cell">' + (p.pid || '--') + '</td>'
        + '</tr>';
    }).join('');
  }).catch(() => console.warn('fetchPorts failed'));
}

// ===== EXPORT JSON =====
function exportJSON() {
  const payload = {
    timestamp: new Date().toISOString(),
    stats: statsData,
    processes: processData,
    diskIO: diskIOData,
    docker: null,
  };
  fetch('/api/docker').then(r=>r.json()).then(d => {
    payload.docker = d;
  }).catch(() => console.warn('exportJSON failed')).finally(() => {
    const blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'dashboard-export-' + new Date().toISOString().slice(0, 19) + '.json';
    a.click();
    URL.revokeObjectURL(url);
  });
}
document.getElementById('export-btn')?.addEventListener('click', exportJSON);

// ===== PROCESS SEARCH =====
const debouncedSearch = debounce(function() {
  procSearchValue = this.value.toLowerCase();
  filterAndRenderProcesses();
}, 150);
document.getElementById('proc-search')?.addEventListener('input', debouncedSearch);
function filterAndRenderProcesses() {
  if (!procSearchValue) {
    renderProcessTables(processData);
    return;
  }
  const filtered = processData.filter(p =>
    String(p.pid).includes(procSearchValue) ||
    (p.name && p.name.toLowerCase().includes(procSearchValue)) ||
    (p.user && p.user.toLowerCase().includes(procSearchValue))
  );
  renderProcessTables(filtered);
}
lastFilesystems = [];
function fetchFilesystems() {
  fetch('/api/filesystems').then(r=>r.json()).then(data => {
    lastFilesystems = data;
    const grid = document.getElementById('fs-grid');
    if (!grid) return;
    if (!data || data.length === 0) {
      grid.innerHTML = '<div style="color:var(--text-secondary);text-align:center;padding:16px;grid-column:1/-1;">No filesystem data</div>';
      return;
    }
    grid.innerHTML = data.map(fs => {
      const pct = fs.usage_percent;
      const color = pct > 85 ? '#f43f5e' : pct > 60 ? '#f59e0b' : '#3b82f6';
      return '<div class="fs-card">'
        + '<div class="fs-info">'
        + '<div class="fs-mount">' + escHtml(fs.mount) + '</div>'
        + '<div class="fs-type">' + escHtml(fs.type) + '</div>'
        + '</div>'
        + '<div class="fs-bar-wrap">'
        + '<div class="fs-bar"><div class="fs-bar-fill" style="width:' + pct + '%;background:' + color + ';"></div></div>'
        + '</div>'
        + '<div class="fs-stats">' + fs.used + ' / ' + fs.total + ' <span style="font-weight:600;">' + pct + '%</span></div>'
        + '</div>';
    }).join('');
  }).catch(() => console.warn('fetchFilesystems failed'));
}

// ===== RENDER DOCKER =====
function fetchDocker() {
  fetch('/api/docker').then(r=>r.json()).then(data => {
    const statusEl = document.getElementById('docker-status');
    const cardEl = document.getElementById('docker-containers-card');
    if (!data.available) {
      statusEl.innerHTML = '<div class="docker-unavail">'
        + '<span class="icon">&#x1F433;</span>'
        + '<div class="msg">Docker is not available</div>'
        + '<div class="sub">' + (data.error || 'Docker daemon is not running on this host') + '</div>'
        + '</div>';
      if (cardEl) cardEl.style.display = 'none';
      return;
    }
    if (data.containers && data.containers.length > 0) {
      statusEl.innerHTML = '<div style="padding:10px 0;"><span style="color:var(--emerald);font-weight:600;">&#x25CF;</span> ' + data.containers.length + ' container(s) running</div>';
      if (cardEl) cardEl.style.display = 'block';
    } else {
      statusEl.innerHTML = '<div style="padding:10px 0;color:var(--text-secondary);">No running containers</div>';
      if (cardEl) cardEl.style.display = 'none';
      return;
    }
    const tbody = document.getElementById('docker-body');
    if (!tbody) return;
    tbody.innerHTML = data.containers.map(c => {
      const statusGood = c.status && c.status.toLowerCase().includes('up');
      return '<tr>'
        + '<td class="pid-cell">' + escHtml(c.id) + '</td>'
        + '<td>' + escHtml(c.name) + '</td>'
        + '<td style="max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + escHtml(c.image) + '">' + escHtml(c.image.split('/').pop()) + '</td>'
        + '<td><span style="color:' + (statusGood ? 'var(--emerald)' : 'var(--rose)') + ';">' + escHtml(c.status) + '</span></td>'
        + '<td>' + (c.cpu_percent || '--') + '</td>'
        + '<td>' + (c.mem_percent || '--') + '</td>'
        + '<td>' + (c.mem_usage || '--') + '</td>'
        + '<td>' + (c.net_io || '--') + '</td>'
        + '</tr>';
    }).join('');
  }).catch(() => console.warn('fetchDocker failed'));
}

// ===== RENDER PROCESS TABLES =====
baseUptimeSeconds = 0;
function renderProcessTables(procs) {
  renderProcTable('cpu-proc-body', procs.slice(0, 10), false);
  renderProcTable('proc-body-full', procs, true);
  // Overview mini tables — always from original processData (pre-sorted CPU desc by API)
  renderProcTable('ov-cpu-proc-body', processData.slice(0, 5), false);
  const byMem = [...processData].sort((a, b) => b.mem_percent - a.mem_percent);
  renderMiniMemTable('ov-mem-proc-body', byMem.slice(0, 5));
}

function renderMiniMemTable(tbodyId, procs) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  if (!procs || procs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3" style="text-align:center;color:var(--text-secondary);padding:16px;">No data</td></tr>';
    return;
  }
  tbody.innerHTML = procs.map(p => {
    const cls = p.mem_percent > 20 ? 'high' : p.mem_percent > 8 ? 'med' : 'low';
    return '<tr>'
      + '<td class="pid-cell">' + p.pid + '</td>'
      + '<td style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + p.name.replace(/"/g,'&quot;') + '">' + escHtml(p.name) + '</td>'
      + '<td><span class="cpu-badge ' + cls + '">' + p.mem_percent.toFixed(1) + '%</span></td>'
      + '</tr>';
  }).join('');
}

// ===== LIVE UPTIME COUNTER =====
function updateUptime() {
  if (!baseUptimeSeconds) return;
  const total = baseUptimeSeconds + (Date.now() / 1000 - (window._uptimeStart || Date.now() / 1000));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = Math.floor(total % 60);
  const el = document.getElementById('uptime-val');
  if (el) el.textContent = d + 'd ' + h + 'h ' + m + 'm ' + s + 's';
}

function renderProcTable(tbodyId, procs, sortable) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  if (!procs || procs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-secondary);padding:20px;">No data</td></tr>';
    return;
  }
  tbody.innerHTML = procs.map(p => {
    const cpuCls = p.cpu_percent > 30 ? 'high' : p.cpu_percent > 10 ? 'med' : 'low';
    const cpuBar = Math.min(p.cpu_percent, 100);
    return '<tr>'
      + '<td class="pid-cell">' + p.pid + '</td>'
      + '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + p.name.replace(/"/g,'&quot;') + '">' + escHtml(p.name) + '</td>'
      + '<td><span class="cpu-badge ' + cpuCls + '">' + p.cpu_percent.toFixed(1) + '%</span></td>'
      + '<td><div class="progress-inline"><div class="progress-inline-fill" style="width:' + Math.min(p.mem_percent, 100) + '%;background:' + gaugeColor(p.mem_percent) + ';"></div></div>' + p.mem_percent.toFixed(1) + '%</td>'
      + (sortable ? '<td>' + p.mem_mb.toFixed(1) + '</td>' : '')
      + (sortable ? '<td>' + escHtml(p.user) + '</td>' : '')
      + (sortable ? '<td>' + p.status + '</td>' : '')
      + '</tr>';
  }).join('');
}

function escHtml(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ===== SORT PROCESS TABLE =====
document.addEventListener('click', function(e) {
  const th = e.target.closest('.proc-table th[data-col]');
  if (!th) return;
  const col = th.dataset.col;
  if (sortCol === col) sortDir *= -1;
  else { sortCol = col; sortDir = -1; }
  document.querySelectorAll('.proc-table th.sorted').forEach(el => el.classList.remove('sorted'));
  th.classList.add('sorted');
  th.querySelector('.sort-arrow').textContent = sortDir === -1 ? '\u25B4' : '\u25BE';
  const sorted = [...processData].sort((a, b) => {
    const va = a[col], vb = b[col];
    if (typeof va === 'string') return sortDir * va.localeCompare(vb);
    return sortDir * (va - vb);
  });
  renderProcessTables(sorted);
});

// ===== LOADING STATE =====
let dataInitialized = false;
function markInitialized() { if (!dataInitialized) { dataInitialized = true; document.querySelectorAll('.loading-placeholder').forEach(el => el.remove()); } }

// ===== INIT =====
fetchStats();
fetchDiskIO();
fetchCPUCores();
fetchNetworkInterfaces();
fetchDocker();
fetchFilesystems();
fetchPorts();
setRefreshRate(5000);

// Periodically fetch processes (tab-aware + visibility)
setInterval(() => {
  if ((currentTab === 'processes' || currentTab === 'cpu') && pageVisible) fetchProcesses();
}, 6000);
setInterval(visibleOnly(fetchDiskIO), 4000);
setInterval(visibleOnly(fetchCPUCores), 3000);
setInterval(visibleOnly(fetchNetworkInterfaces), 4000);
setInterval(visibleOnly(fetchDocker), 10000);
setInterval(visibleOnly(fetchFilesystems), 8000);
setInterval(updateUptime, 1000);
setInterval(visibleOnly(fetchPorts), 30000);
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/stats":
            data = collect_stats()
            self._json(data)

        elif path == "/api/processes":
            self._json(get_top_processes())

        elif path == "/api/disk-io":
            self._json(get_disk_io())

        elif path == "/api/cpu-per-core":
            self._json(get_cpu_per_core())

        elif path == "/api/network-interfaces":
            self._json(get_network_interfaces())

        elif path == "/api/docker":
            self._json(get_docker_stats())

        elif path == "/api/filesystems":
            self._json(get_filesystems())

        elif path == "/api/ports":
            self._json(get_listening_ports())

        elif path == "/api/uptime-history":
            self._json(list(UPTIME_HISTORY))

        elif path == "/health":
            _, secs = get_uptime()
            self._json({
                "status": "ok",
                "uptime": get_uptime()[0],
                "uptime_seconds": int(secs),
                "timestamp": datetime.now().isoformat(),
            })

        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]} {args[2]}")


if __name__ == "__main__":
    port = 8765
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"Server Dashboard running at http://0.0.0.0:{port}")
    print(f"  GET /api/stats         - System stats")
    print(f"  GET /api/processes     - Top 20 processes")
    print(f"  GET /api/disk-io       - Disk I/O stats")
    print(f"  GET /api/cpu-per-core  - Per-core CPU usage")
    print(f"  GET /api/network-intf  - Per-interface network stats")
    print(f"  GET /api/docker        - Docker container stats")
    print(f"  GET /api/filesystems   - All mount points")
    print(f"  GET /api/ports         - Listening ports")
    print(f"  GET /api/uptime-history - Uptime history (60 entries)")
    print(f"  GET /health            - Health check")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
