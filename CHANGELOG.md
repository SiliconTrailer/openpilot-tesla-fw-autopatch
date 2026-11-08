# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.0] - 2026-11-08

### Added
- `--install`: copies the script to `/data/fw_autopatch.py` and adds the boot
  hook to `/data/continue.sh` (idempotent, backup, `bash -n` syntax check).
- `--uninstall`: removes the boot hook.
- `--status`: last fingerprint, hook state, approved versions and patch state
  of the live and staged copy, including whether the staged copy is a clean
  re-stage of the same commit or a real openpilot update.
- Clearer output: `OK` / `NEW` / `REVIEW` / `SKIP` per firmware string, and
  for new versions how they relate to the known ones
  (e.g. "newer firmware, latest known was E014.20.2").
- Boot hook log lines now name the copy and the re-added version.

### Changed
- Running without arguments on a recognized car shows the status overview
  instead of just "nothing to do".
- Only versions that are not part of the official release (checked against
  git `HEAD`) are approved for the boot hook.
- Backups are pruned to the oldest plus the last five per copy, since the boot
  hook may patch the staged copy on every start.

### Fixed
- `--revert` now also clears the approvals, so the boot hook doesn't re-add
  the reverted versions on the next start.

## [0.3.0] - 2026-11-07

### Added
- `--reapply` mode for a boot hook in `/data/continue.sh`. It re-adds only
  firmware versions you previously approved with `--apply` if an openpilot
  update removed them. It never adds new, unapproved versions.
- Approved versions are recorded in `/data/fw_autopatch_approved.txt`.
- README section on setting up the boot hook.

## [0.2.0] - 2026-11-07

### Fixed
- Patch was lost after a reboot when openpilot had already downloaded an
  update into `/data/safe_staging/finalized`. That staged copy replaces
  `/data/openpilot` on the next boot, even if the version number does not
  change. The script now patches both the live and the staged copy and tells
  you when a staged update is waiting.

### Changed
- Backups are now named per copy (`fingerprints.py.live.*`,
  `fingerprints.py.staged.*`), and `--revert` restores both.

## [0.1.0] - 2026-11-07

### Added
- Initial release: adds newly observed Tesla ECU firmware versions of known
  firmware families to the local `fingerprints.py` when openpilot falls back
  to `MOCK` / dashcam mode.
- Dry run by default, `--apply` with backup and verification, `--revert`.
- Tested on Tesla Model 3 (2021, HW3), comma four, openpilot 0.11.2.
