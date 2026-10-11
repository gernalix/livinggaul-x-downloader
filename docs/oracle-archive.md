# Oracle catalog and Seagate archive

Oracle is the sole writer after cutover. `downloads.status=downloaded` remains
compatible with existing queries; `storage_state=staged|archived|failed` tracks
placement separately. The `video_catalog` adds archive location and ACK time.
An archived entry remains downloaded without a VM file. IDs, snapshots, static
counters, fingerprints, duplicate links and source checks survive migration.

Runtime: `/srv/livinggaul/repo`, root-owned, group-readable only by `livinggaul`;
CPython 3.12.15 under `/srv/livinggaul/runtime/python`, isolated environment
`/srv/livinggaul/venv`, pinned dependencies in `requirements-oracle.txt`.
System Python stays unchanged. Python installation uses Astral uv and its
python-build-standalone distribution, not an Ubuntu Python replacement.

State: `/home/livinggaul/.local/state/livinggaul-x-downloader/`, staging
`/home/livinggaul/staging/`, both owner-only. Root-only existing outbound
credentials under `/etc/livinggaul/` enter services through `LoadCredential`.
No cookie transfer, bot creation, incoming Telegram consumer or Pn-bot change.

All Oracle units and SSH queue/ACK/snapshot commands serialize on
`/home/livinggaul/runtime.lock`. Oneshots use bounded restarts and timeouts,
CPU 35%, memory high 160 MiB/max 220 MiB, swap 64 MiB, low I/O priority.
Downloads cap individual files at 512 MiB; guard checks available RAM >=100 MiB,
disk free >=4 GiB and staging <=6 GiB before subprocesses and every 5 seconds
while running them. Guard termination kills the whole subprocess group.
Outbox timer emits persistent resource/service-failure alerts.
Logrotate retains seven daily logs with a 2 MiB rotation threshold.

Fedora uses existing `oracle-vm` SSH transport. The nonprivileged Oracle owner
serves rsync through the existing ubuntu sudo boundary. Pull runs at boot,
every five minutes, and on mount-directory change. It refuses an absent Seagate
mount and preserves conflicting destination files. Partial transfer resumes;
SHA-256 and size must match before fsync/atomic rename and ACK. Oracle commits
ACK before deleting; restart finishes leftover cleanup. ACK includes row ID,
size, hash and canonical archive path, and cannot change an existing location.

Archive files use `<id>-<sha256>.mp4` under
`/mnt/Seagate6TB/LivingGaul_X/videos/`. Original descriptive filenames remain
in the catalog. The recovery directory receives an API-consistent SQLite copy,
a manifest whose archived files are verified locally, and yt-dlp archive.
`PRAGMA integrity_check` and `foreign_key_check` validate recovery catalogs.
Oracle's notification cycles also export consistent catalog snapshots. State and
staging reside under /home/livinggaul so the existing restic /home backup includes
them without changing its scope or service. Restore the API-consistent export,
not a potentially live catalog copy.

Each successful new video and its Telegram event commit together. Historical
imports/dry-run/duplicates never emit success events. Messages contain the
LivingGaul post, quoted source when exposed by metadata, and CDN MP4 when
available (URL may expire). Each message has one video, below 4000 UTF-16 units,
with HTML escaping and validated HTTPS link hosts. `message_id` is persisted.
Certain Telegram rejections retry at most eight times with exponential backoff
and `retry_after`. Network/malformed responses and restart during send become
`ambiguous` and never blindly resend. An operator can reconcile the observed
message ID using `livinggaul_pipeline.py reconcile --event ID --message-id ID`
in the credential-loaded service context; editing that same message confirms
receipt without creating a duplicate or consuming incoming bot updates.

Before enabling timers, verify real [TEST] receipt, transfer/ACK/hash/cleanup,
public coverage, 480p download, dedup after cleanup, source check, fault handling
and restored catalog. Keep Fedora availability operational until all gates pass.
For rollback stop new Oracle timers/downloader first, preserve current DB/outbox,
copy a fresh snapshot, then resume Fedora writing only with current state.
Never restore old backups over committed notifications or archive ACKs.
Seagate ext4 is unencrypted: anyone with disk access can read videos/catalog.
