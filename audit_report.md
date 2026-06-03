# server.py Data-Collection Function Audit

**Generated:** Live test against actual system  
**System:** Container with 4 cores, 23.4 GB RAM, 192.7 GB ext4 root  
**Python:** 3.13  

---

## ✅ get_cpu_info — PASS

- **cores** = 4 (matches `os.cpu_count()`)
- **load_1min/5min/15min** match `/proc/loadavg` directly
- Sensible, no issues

---

## ✅ get_memory_info — PASS (fix verified)

- Parses `/proc/meminfo` correctly — colon stripping (`rstrip(":")`) works as expected
- `total_bytes`, `available_bytes` match `free -b` within timing noise (< 1 MB)
- `swap_total_bytes` correctly reports 0 (no swap)
- Formatting: "23.4 GB" for 25 GB RAM is correct
- **Note:** `used` is computed as `total - available` (by design), so `used + available == total` always. This differs from `free`'s "used" column (which excludes buffers/cache), but for a dashboard using `MemAvailable` this is the recommended approach.

---

## ⚠️ get_disk_info — MINOR DISCREPANCY

- Uses `os.statvfs("/")` with `f_bfree` (total free blocks **including** root-reserved 5%)
- `df -B1` uses `f_bavail` (blocks available to non-root)
- Result: `available_bytes` is ~16 MB higher than `df` reports (reserved block pool)
- `usage_percent` = 29.2% (calculated from f_bfree) vs `get_filesystems` which reads df's rounded 30%
- **Impact:** The main stats card (using `get_disk_info`) and the filesystems tab (using `get_filesystems`) show slightly different values for the same root filesystem. Minor cosmetic issue.

---

## ✅ get_processes — PASS

- `get_process_count()`: Returns 8, matches `ps -e --no-headers | wc -l` exactly
- `get_top_processes()`: Returns all 8 processes sorted by CPU, all with valid PIDs, names, CPU%, MEM%, RSS
- RSS values match `ps aux` output (checked: 17884 KB → 17.5 MB, correct)
- No processes with name "?" — all commands parsed correctly

---

## ✅ get_network_interfaces — PASS

- Two interfaces found: `eth0` and `eth1` (loopback `lo` correctly excluded)
- RX/TX bytes, packets match `/proc/net/dev` directly
- Column indexing verified: `parts[9]` = tx_bytes, `parts[10]` = tx_packets — **correct**
- Speed calculation: first call returns 0 (no baseline), second call returns real speeds (e.g., 4.3 KB/s)
- Formatted strings look correct ("40.3 MB", "53.2 MB")

---

## 🐛 get_disk_io — BUGS FOUND

### Bug 1: `write_speed_mbps` missing
The function computes `read_speed` but **never computes or returns** `write_speed_mbps`. The variable `write_speed` is never calculated, and the return dict only has `read_speed_mbps`. The `prev_write` value is saved but never consumed.

**Affected code** (lines 231-245):
```python
read_speed = 0
if dt > 0 and DISK_STATS_CACHE["prev_time"] > 0:
    dr = (read_sectors - DISK_STATS_CACHE["prev_read"]) * 512 / 1024 / 1024
    read_speed = round(dr / dt, 2)
DISK_STATS_CACHE["prev_read"] = read_sectors
DISK_STATS_CACHE["prev_write"] = write_sectors   # saved but never used
DISK_STATS_CACHE["prev_time"] = now

return {
    "reads_mb": reads_mb, "writes_mb": writes_mb,
    "read_speed_mbps": read_speed,               # no write_speed_mbps
    "device": name,
}
```

### Bug 2: NVMe disks silently skipped
The filtering logic at lines 221-225 skips any block device whose last character is a digit:
```python
if name.startswith("sd") or name.startswith("nvme") or ...:
    if name[-1].isdigit():
        continue   # skips partitions — but also skips nvme0n1!
    if name.startswith("nvme") and not name[-1].isdigit():
        pass       # this is a no-op, does nothing
```

NVMe base devices like `nvme0n1` end with a digit (`1`), so they are **incorrectly skipped**. Only SD/VD/XVD disks (which end with a letter) are captured. The `pass` on line 225 appears to be an abandoned attempt to fix this.

**On this system:** Only `sda` exists, so the bug doesn't manifest. But on any system with NVMe drives, disk I/O would report "unknown" device and 0 values.

### Bug 3: Returns only first matching disk
The function `return`s inside the loop, so only the first matching disk (by /proc/diskstats order) is ever returned. Multi-disk systems miss all other devices.

---

## ✅ get_filesystems — PASS

- Properly excludes tmpfs, devtmpfs, overlay, proc, sysfs, cgroup, etc.
- On this system: correctly returns only `/opt/data` (ext4)
- Total/used/avail values match `df -T` output exactly
- Usage percent reads df's rounded integer (30%) — differs from main card's 29.2% (see get_disk_info note)

---

## ✅ get_listening_ports — PASS

- Found all 4 listening TCP ports: 8765, 8787, 9119, 34789
- Matches `ss -tlnp -4` output exactly
- Address extraction (`.rsplit(":", 1)[0]`) correct — "0.0.0.0" and "127.0.0.11"
- PID/process extraction via regex works for named processes, returns empty for kernel/internal listeners

---

## ✅ get_docker_stats — PASS (graceful failure)

- Docker daemon not running on this system
- Returns `{"available": false, "error": "Docker not available"}`
- No exception raised
- Catches `FileNotFoundError` and `TimeoutExpired` explicitly

---

## Summary

| Function | Status | Issues |
|---|---|---|
| `get_cpu_info` | ✅ PASS | None |
| `get_memory_info` | ✅ PASS | Colon fix verified; swap_percent safe with 0-division guard |
| `get_disk_info` | ⚠️ NOTE | Uses `f_bfree` vs df's `f_bavail` → 16 MB / 0.8% discrepancy with `get_filesystems` |
| `get_processes` | ✅ PASS | Count matches, all processes have valid names |
| `get_network_interfaces` | ✅ PASS | Correct column indexing, loopback excluded |
| **`get_disk_io`** | 🐛 **BUGS** | **Missing `write_speed_mbps`**, **NVMe disks skipped**, **single-disk-only** |
| `get_filesystems` | ✅ PASS | tmpfs/virtual FS properly excluded |
| `get_listening_ports` | ✅ PASS | All TCP ports found, matches `ss` exactly |
| `get_docker_stats` | ✅ PASS | Graceful failure when Docker unavailable |

### Real bugs to fix:
1. **`get_disk_io`:** Add `write_speed_mbps` calculation and return it
2. **`get_disk_io`:** Fix NVMe filter to use `p\d+` suffix detection instead of `last-char-is-digit`
3. **`get_disk_io` (optional):** Consider aggregating or returning all disks instead of just the first

### Cosmetic issue:
- `get_disk_info` shows 29.2% usage for root, `get_filesystems` shows 30.0% — rounding difference from different calculation methods
