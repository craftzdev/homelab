# worker-1 disk investigation — 2026-09-13

Read-only measurements taken around 18:21–18:27 JST.

## Finding

The immediate load source is rebuilding the Prometheus Longhorn replica on
k8s-worker-1. It writes to VM 1101's 300 GiB Longhorn disk, which shares a single
SATA SSD and ZFS pool with the VM's 60 GiB OS disk. Long write latency and ZFS
queueing propagate to the OS and CI runner. Rebuild failures and retries have
prolonged the load. The observations identify the active load and bottleneck;
they do not establish an internal SSD hardware/firmware defect or prove why the
first replica failed.

Storage path:

- `k8s-worker-1` / `172.16.40.21` / VM `1101`
- `sv-proxmox-01` / `172.16.10.11`
- OS: `local-zfs/vm-1101-disk-0` -> `zd64`
- Longhorn: `local-zfs/vm-1101-disk-1` -> `zd96`
- Physical pool device: `/dev/sda1`, SUNEAST SE800 Lite SSD 1024GB, SATA 6 Gb/s
- The control-plane OS uses the separate Crucial NVMe; the worker does not.

## Evidence

- Prometheus 5-minute measurements: worker-1 CPU work ~10.8%, CPU iowait
  ~61.7%, available memory ~77.5%. Guest write latency ~1.72 s on OS `sda`,
  ~1.74 s on Longhorn `sdb`.
- ZFS 2-second samples: ~1.3–2.4 MiB/s physical writes, ~1 s device wait,
  total write wait up to 25 s and synchronous queue wait up to 51 s.
  Around 444–502 synchronous writes were pending in these samples.
- A later 10-second physical disk sample: sda write await 126.5 ms,
  4.85 MiB/s writes, 81.4% busy. The same host's NVMe write await was 0.7 ms.
  Separate comparison samples: node2 SATA 1.3 ms, node3 SATA 0.3 ms. Node2 also
  uses SUNEAST SE800 Lite; node3 reports P3-1TB. Workloads and time windows differ.
- ZFS transaction group sync times included ~53–145 seconds. Several ZFS zvol
  workers waited in `dmu_tx_wait`.
- cAdvisor 5-minute write rate: Longhorn instance-manager ~2.30 MB/s,
  Prometheus ~68.6 kB/s, moshitoku runner ~2.18 kB/s. These layers must not be
  added as independent physical writes.
- A separate 8-second `/proc/<pid>/io` sample inside Longhorn attributed about
  0.95 MB/s to the Prometheus volume's two processes; the only other observed
  writer was Grafana at ~0.5 kB/s.

## Rebuilding volume

- PVC: `monitoring/prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0`
- Volume: `pvc-364e7ae1-a18b-4dd1-9162-388d0c803454`
- Actual allocated size: 17,280,245,760 bytes; desired replica count: 3.
- State: attached, degraded. Engine reported one RW replica on worker-3 and
  one WO replica being rebuilt on worker-1.
- Latest rebuild started 18:04:08 JST; progress moved from 18% to 21% during
  investigation. Longhorn status reported no applied rebuild bandwidth limit
  (`appliedRebuildingMBps: 0`).
- Prior attempts at 17:44 and 17:59 failed while copying a ~17 GB snapshot,
  with RPC `Unavailable` / EOF and replica-removal deadline errors.
- The receiver log confirms direct-I/O snapshot transfer to worker-1. No guest
  OOM or block-I/O error appeared in the available kernel log filter.

## Exclusions and limits

- ZFS capacity: 7% used, ~880 GiB free; fragmentation 14%.
- Host available memory ~20.9 GiB; swap used 0.
- ZFS ONLINE, no read/write/checksum errors. Scrub completed at 04:30 with no
  repairs; no scrub was active in the measured windows.
- SMART overall status PASSED; reallocated/pending sectors and SATA CRC errors
  zero. Reported temperature 48 C. SMART does not prove latency health.
- SSD internal GC, cache exhaustion, wear-related slowdown or firmware faults
  were not independently tested. `autotrim` is off, but that alone does not
  establish the cause.
- Host IO PSI is also elevated on nodes 2/3 despite low disk latency; stale
  Ceph operations are present. Host PSI/load alone is not used to attribute the
  worker-1 SATA bottleneck.

No workload was paused, replica removed, disk migrated, TRIM started, or ZFS
sync semantics changed. Any remediation must preserve the remaining healthy
Prometheus replica. A durable approach should address the shared OS/Longhorn
storage and rebuild load rather than weakening synchronous-write guarantees.

---

## Correction (appended 2026-09-14)

The conclusion above — that the shared OS/Longhorn SATA pool is the durable
problem to address — was **not the root cause**. Follow-up work on the same
day identified and fixed the actual cause.

`local-zfs` had never been TRIMmed on any of the three hosts. Debian's
monthly `/etc/cron.d/zfsutils-linux` job calls `/usr/lib/zfs-linux/trim`,
which under its default `auto` setting only trims **NVMe-only** pools; a
single-SATA-device pool is skipped every time. The drives had ~3.4 years of
prior use (Ceph, per ADR-0009), so their FTLs still treated the whole device
as written despite the pool being 7% full and 7 days old.

After `zpool trim`, host 01's SATA write await went from 190–263 ms at 90%
busy to 1 ms at 1% busy, all 22 Longhorn volumes returned to healthy, and the
Proxmox ZFS replication job that had been failing with snapshot timeouts
completed in 2.9 s. The OS and Longhorn still share the same pool.

This document's measurements remain accurate; only the causal attribution
was wrong. Full record: `docs/storage-migration-2026-09-13.md`.
