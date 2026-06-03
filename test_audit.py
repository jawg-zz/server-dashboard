#!/usr/bin/env python3
"""Audit all data-collection functions in server.py against live system tools."""

import sys
import os
import json
import subprocess
import importlib.util

# Load server.py as a module
spec = importlib.util.spec_from_file_location("server", "/opt/data/workspace/server-dashboard/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

results = {}

# ── 1. get_cpu_info ──────────────────────────────────────
def test_cpu_info():
    info = server.get_cpu_info()
    # Check /proc/loadavg directly
    with open("/proc/loadavg") as f:
        raw = f.read().strip().split()
    la1_sys, la5_sys, la15_sys = float(raw[0]), float(raw[1]), float(raw[2])
    
    issues = []
    if info["cores"] != os.cpu_count():
        issues.append(f"cores mismatch: {info['cores']} vs {os.cpu_count()}")
    if abs(info["load_1min"] - la1_sys) > 0.01:
        issues.append(f"load_1min mismatch: {info['load_1min']} vs {la1_sys}")
    if abs(info["load_5min"] - la5_sys) > 0.01:
        issues.append(f"load_5min mismatch: {info['load_5min']} vs {la5_sys}")
    if abs(info["load_15min"] - la15_sys) > 0.01:
        issues.append(f"load_15min mismatch: {info['load_15min']} vs {la15_sys}")
    return {
        "data": info,
        "match": len(issues) == 0,
        "issues": issues if issues else "OK",
    }

# ── 2. get_memory_info ──────────────────────────────────
def test_memory_info():
    info = server.get_memory_info()
    # Check with free -b
    r = subprocess.run(["free", "-b"], capture_output=True, text=True)
    lines = r.stdout.strip().split("\n")
    mem_line = lines[1].split()
    total_sys = int(mem_line[1])
    used_sys = int(mem_line[2])
    avail_sys = int(mem_line[6])  # available column
    swap_line = lines[2].split()
    swap_total_sys = int(swap_line[1])
    swap_used_sys = int(swap_line[2])
    
    issues = []
    # Allow small rounding differences (free -b and /proc/meminfo agree within kB)
    if abs(info["total_bytes"] - total_sys) > 2048:
        issues.append(f"total_bytes diff: {info['total_bytes']} vs {total_sys} (diff {info['total_bytes']-total_sys})")
    if abs(info["available_bytes"] - avail_sys) > 2048:
        issues.append(f"available_bytes diff: {info['available_bytes']} vs {avail_sys} (diff {info['available_bytes']-avail_sys})")
    # Used = total - available, which may differ from free's "used" (which includes buffers/cache)
    if abs(info["swap_total_bytes"] - swap_total_sys) > 2048:
        issues.append(f"swap_total_bytes diff: {info['swap_total_bytes']} vs {swap_total_sys} (diff {info['swap_total_bytes']-swap_total_sys})")
    if abs(info["swap_used_bytes"] - swap_used_sys) > 2048:
        issues.append(f"swap_used_bytes diff: {info['swap_used_bytes']} vs {swap_used_sys} (diff {info['swap_used_bytes']-swap_used_sys})")
    # Check formatting
    if "KB" not in info["total"] and info["total_bytes"] > 0:
        issues.append(f"total formatted string looks wrong: {info['total']}")
    return {
        "data": info,
        "match": len(issues) == 0,
        "issues": issues if issues else "OK",
    }

# ── 3. get_disk_info ─────────────────────────────────────
def test_disk_info():
    info = server.get_disk_info()
    r = subprocess.run(["df", "-B1", "/"], capture_output=True, text=True)
    lines = r.stdout.strip().split("\n")
    parts = lines[1].split()
    total_sys = int(parts[1])
    used_sys = int(parts[2])
    avail_sys = int(parts[3])
    pct_sys = float(parts[4].rstrip("%"))
    
    issues = []
    if abs(info["total_bytes"] - total_sys) > 4096:
        issues.append(f"total_bytes diff: {info['total_bytes']} vs {total_sys} (diff {info['total_bytes']-total_sys})")
    if abs(info["used_bytes"] - used_sys) > 4096:
        issues.append(f"used_bytes diff: {info['used_bytes']} vs {used_sys} (diff {info['used_bytes']-used_sys})")
    if abs(info["available_bytes"] - avail_sys) > 4096:
        issues.append(f"available_bytes diff: {info['available_bytes']} vs {avail_sys} (diff {info['available_bytes']-avail_sys})")
    if abs(info["usage_percent"] - pct_sys) > 0.5:
        issues.append(f"usage_percent diff: {info['usage_percent']} vs {pct_sys}")
    return {
        "data": info,
        "match": len(issues) == 0,
        "issues": issues if issues else "OK",
    }

# ── 4. get_process_count & get_top_processes ─────────────
def test_processes():
    count = server.get_process_count()
    top = server.get_top_processes()
    
    # Compare with ps count
    r = subprocess.run(["ps", "-e", "--no-headers"], capture_output=True, text=True)
    sys_count = len(r.stdout.strip().split("\n")) if r.stdout.strip() else 0
    
    issues = []
    if abs(count - sys_count) > 5:
        issues.append(f"process_count diff: {count} vs {sys_count}")
    if len(top) == 0:
        issues.append("get_top_processes returned empty list!")
    elif len(top) > 20:
        issues.append(f"get_top_processes returned {len(top)} items, expected max 20")
    # Check process names look real
    real_names = sum(1 for p in top if p["name"] != "?" and len(p["name"]) > 1)
    if real_names < len(top) * 0.5:
        issues.append(f"Too many processes with name '?' ({len(top)-real_names}/{len(top)})")
    return {
        "data": {"count": count, "top_count": len(top), "sample": top[:3]},
        "match": len(issues) == 0,
        "issues": issues if issues else "OK",
    }

# ── 5. get_network_interfaces ────────────────────────────
def test_network_interfaces():
    info = server.get_network_interfaces()
    # Check /proc/net/dev directly
    with open("/proc/net/dev") as f:
        raw_lines = f.readlines()[2:]
    
    issues = []
    if len(info) == 0:
        issues.append("No interfaces returned (might be OK if only lo)")
    else:
        for iface in info:
            if iface["rx_bytes"] < 0 or iface["tx_bytes"] < 0:
                issues.append(f"{iface['name']}: negative bytes")
            if iface["rx_speed_bps"] < 0:
                issues.append(f"{iface['name']}: negative rx_speed")
            if iface["tx_speed_bps"] < 0:
                issues.append(f"{iface['name']}: negative tx_speed")
    return {
        "data": info,
        "match": len(issues) == 0,
        "issues": issues if issues else "OK",
    }

# ── 6. get_disk_io ───────────────────────────────────────
def test_disk_io():
    info = server.get_disk_io()
    # Check /proc/diskstats
    with open("/proc/diskstats") as f:
        raw = f.read()
    
    issues = []
    if info["device"] == "unknown":
        issues.append("No disk device found (unknown)")
    elif not info["device"].startswith(("sd", "nvme", "vd", "xvd")):
        issues.append(f"Unexpected device name: {info['device']}")
    if info["reads_mb"] == 0 and info["writes_mb"] == 0:
        # Could be valid for a fresh system with no I/O, but unlikely
        issues.append("reads_mb and writes_mb both 0 (possible but suspicious)")
    return {
        "data": info,
        "match": len(issues) == 0,
        "issues": issues if issues else "OK",
    }

# ── 7. get_filesystems ───────────────────────────────────
def test_filesystems():
    mounts = server.get_filesystems()
    # Check no tmpfs mounts
    issues = []
    for m in mounts:
        if m["type"] in ("tmpfs", "devtmpfs", "overlay", "proc", "sysfs"):
            issues.append(f"{m['mount']} has type {m['type']} which should've been excluded")
        if "tmpfs" in m["mount"]:
            issues.append(f"Mount point {m['mount']} looks like tmpfs")
    # Compare total count with our own df
    r = subprocess.run(
        ["df", "-T", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs",
         "-x", "overlay", "-x", "proc", "-x", "sysfs", "-x", "cgroup",
         "-x", "cgroup2", "-x", "devpts", "-x", "mqueue"],
        capture_output=True, text=True, timeout=5,
    )
    sys_lines = r.stdout.strip().split("\n")[1:]
    sys_mounts = [l.split() for l in sys_lines if len(l.split()) >= 7]
    if abs(len(mounts) - len(sys_mounts)) > 2:
        issues.append(f"Filesystem count mismatch: {len(mounts)} vs {len(sys_mounts)} (df output)")
    if len(mounts) == 0:
        issues.append("get_filesystems returned empty list!")
    return {
        "data": mounts,
        "match": len(issues) == 0,
        "issues": issues if issues else "OK",
    }

# ── 8. get_listening_ports ───────────────────────────────
def test_listening_ports():
    ports = server.get_listening_ports()
    # Compare with ss
    r = subprocess.run(["ss", "-tlnp", "-4"], capture_output=True, text=True)
    sys_lines = r.stdout.strip().split("\n")[1:]
    sys_ports = set()
    for line in sys_lines:
        parts = line.split()
        if len(parts) >= 5:
            addr = parts[3]
            port_str = addr.rsplit(":", 1)[-1]
            try:
                sys_ports.add(int(port_str))
            except ValueError:
                pass
    
    port_set = set(p["port"] for p in ports)
    
    issues = []
    missing = sys_ports - port_set
    if missing:
        issues.append(f"Missing ports from ss output: {sorted(missing)}")
    extra = port_set - sys_ports
    if extra:
        issues.append(f"Extra ports not in ss output: {sorted(extra)}")
    
    return {
        "data": {"port_count": len(ports), "ports": sorted(port_set)},
        "match": len(issues) == 0,
        "issues": issues if issues else "OK",
    }

# ── 9. get_docker_stats ─────────────────────────────────
def test_docker_stats():
    info = server.get_docker_stats()
    issues = []
    # Should either gracefully report unavailable or return real data
    if not isinstance(info, dict):
        issues.append(f"Return type is not dict: {type(info)}")
    elif "available" not in info:
        issues.append("Missing 'available' key")
    elif info.get("available") and "containers" not in info:
        issues.append("Available but missing 'containers' key")
    # Either way, no exception should be raised
    return {
        "data": info,
        "match": len(issues) == 0,
        "issues": issues if issues else "OK",
    }

# ── Run all ──────────────────────────────────────────────
def main():
    tests = [
        ("get_cpu_info", test_cpu_info),
        ("get_memory_info", test_memory_info),
        ("get_disk_info", test_disk_info),
        ("get_processes", test_processes),
        ("get_network_interfaces", test_network_interfaces),
        ("get_disk_io", test_disk_io),
        ("get_filesystems", test_filesystems),
        ("get_listening_ports", test_listening_ports),
        ("get_docker_stats", test_docker_stats),
    ]
    
    all_pass = True
    for name, fn in tests:
        print(f"\n{'='*60}")
        print(f"  TEST: {name}")
        print(f"{'='*60}")
        try:
            result = fn()
            status = "✅ PASS" if result["match"] else "❌ FAIL"
            print(f"  Status: {status}")
            print(f"  Data: {json.dumps(result.get('data','?'), indent=2, default=str)}")
            if not result["match"]:
                print(f"  Issues: {result['issues']}")
                all_pass = False
            else:
                print(f"  Issues: {result['issues']}")
        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            all_pass = False
    
    print(f"\n{'='*60}")
    print(f"  OVERALL: {'✅ ALL PASSED' if all_pass else '❌ SOME FAILED'}")
    print(f"{'='*60}")
    return 0 if all_pass else 1

if __name__ == "__main__":
    sys.exit(main())
