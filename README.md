# fw_autopatch

A small helper for **Tesla owners running openpilot** whose car suddenly shows
**"Car Unrecognized: Dashcam Mode"** after a Tesla software update.

Tesla fingerprinting in openpilot is an exact match on the firmware strings the
car reports (e.g. the EPS at `0x730`). A Tesla update can bump such a string
before the new version lands in an openpilot release, so the car falls back to
`MOCK` and openpilot won't engage. The fix upstream is a one-line addition to
`opendbc/car/tesla/fingerprints.py`, but until it ships in a release you are
stuck with a dashcam.

`fw_autopatch.py` reads the firmware versions your car reported, finds the ones
that are a new version of an **already known firmware family**, and adds them to
your local `fingerprints.py`. Nothing else in openpilot is changed.

> **Tested only on:** Tesla Model 3 (2021, HW3), comma four, openpilot 0.11.2
> (release). It should work for other Tesla Model 3/Y variants listed in
> `opendbc/car/tesla/fingerprints.py`, but that is untested. It does **not**
> support other brands.

## Disclaimer

This is not an official comma.ai tool and is not affiliated with comma.ai or
Tesla. The fingerprint check exists so that a new firmware is reviewed before
openpilot controls the car. This script relaxes that check for version bumps
within a known family. Use it at your own risk, stay attentive, and keep your
hands ready to take over, as always with openpilot.

## Safety gates

- Only acts if the last fingerprint was `MOCK` (unrecognized). Override with `--force`.
- A new version is only accepted if it matches a known family for exactly one
  car + ECU: same base string and same version stem, e.g.
  `TeM3_E014p10_0.0.0 (35),E014.21.2` is accepted because
  `TeM3_E014p10_0.0.0 (24),E014.20.2` is already listed for `TESLA_MODEL_3`.
- Firmware strings from unknown families are **only reported** (`REVIEW`), never added.
- Dry run by default. `--apply` makes a backup, checks the Python syntax and
  verifies each new line landed in the right list before replacing the file.
- The patch is local only. The next openpilot update overwrites it, which is
  intended: by then the version should be in the official release.

## Requirements

- comma 3X or comma four running openpilot on a supported Tesla Model 3/Y
- SSH access to the device (Settings → Developer → Enable SSH, add your GitHub username)
- The car must have been started once with the comma attached, so that
  `CarParamsPersistent` contains the reported firmware versions

No extra Python packages are needed.

## Quick start

Copy the script to the device and install it:

```bash
scp fw_autopatch.py comma@<device-ip>:/tmp/
ssh comma@<device-ip>
python3 /tmp/fw_autopatch.py --install   # copies itself to /data, adds the boot hook
```

When the car shows **Car Unrecognized** after a Tesla update:

```bash
python3 /data/fw_autopatch.py            # dry run: shows what it would add
python3 /data/fw_autopatch.py --apply    # patch and approve the new version
sudo reboot                              # with the car awake
```

That's it. The boot hook keeps the approved versions in place across openpilot
updates until an official release contains them.

## Commands

| Command | What it does |
|---|---|
| *(none)* | Dry run if the car is unrecognized, status overview if it is recognized |
| `--apply` | Patch `fingerprints.py` (live and staged copy) and approve the versions |
| `--force` | With `--apply` or the dry run: check even if the car is recognized |
| `--status` | Last fingerprint, boot hook, approved versions, patch state per copy |
| `--install` | Copy the script to `/data/fw_autopatch.py` and add the boot hook to `/data/continue.sh` |
| `--uninstall` | Remove the boot hook |
| `--revert` | Restore `fingerprints.py` from git and forget all approvals |
| `--reapply` | Used by the boot hook: re-add approved versions only |

Already patched by hand? Run `--apply --force` once so the version gets
approved for the boot hook.

## Example output

Dry run after a Tesla update:

```
Last fingerprint: MOCK  (CarParamsPersistent)

[live] /data/openpilot/opendbc_repo/opendbc/car/tesla/fingerprints.py
  OK      EL014.20.2     already listed
  NEW     TeM3_E014p10_0.0.0 (35),E014.21.2
          newer firmware, latest known was E014.20.2
          -> CAR.TESLA_MODEL_3 / (Ecu.eps, 0x730, None)

Dry run, nothing written. Run with --apply, then: sudo reboot
```

Status when everything is fine:

```
Last fingerprint: TESLA_MODEL_3  (CarParamsPersistent)
Boot hook:        installed in /data/continue.sh
Approved:         E014.21.2
live copy:        patched
staged copy:      missing E014.21.2 -> boot hook re-adds it on next start
                  staged = same commit as live: the updater re-stages a clean copy
                  while git sees the live copy as modified (expected, harmless)
Last hook action: 2026-11-08T08:12:03 [staged] re-added TeM3_E014p10_0.0.0 (35),E014.21.2 -> ...
```

Firmware strings of an unknown family show up as `REVIEW` and are never added.
Added lines are marked with a `# fw_autopatch <date>` comment. Backups are kept
in `/data/fw_autopatch_backups/` (the oldest plus the last five per copy).

## Why a boot hook is needed

openpilot keeps a second copy in `/data/safe_staging/finalized`, prepared by its
updater, and swaps it in for `/data/openpilot` on the next start. As soon as the
live `fingerprints.py` is patched, git sees it as modified and the updater
re-stages a **clean copy of the same commit**, without the patch. Patching only
the live copy is therefore lost on the next reboot.

The boot hook runs `--reapply` from `/data/continue.sh`, right before
`launch_openpilot.sh`, which is where the staged copy gets swapped in. So both
copies are patched before openpilot starts, on every boot. It only re-adds
versions you approved with `--apply`; a new Tesla firmware always needs your
`--apply`. It runs for a fraction of a second, no daemon. `timeout` and
`|| true` make sure openpilot always starts, even if the script fails.

`/data` survives openpilot and AGNOS updates, but a full reflash or reset wipes
it and recreates `continue.sh`. Run `--install` again afterwards.

## Please report your firmware upstream

This script is a stopgap. If it found a new version for your car, check whether
it is already in
[`commaai/opendbc` master](https://github.com/commaai/opendbc/blob/master/opendbc/car/tesla/fingerprints.py).
If not, open a PR or post it in the `#tesla` channel on the
[comma Discord](https://discord.comma.ai) with the firmware string, your Tesla
software version and your hardware (HW3/HW4), so everyone gets it in the next
release. Only share your dongle ID or a route if you are comfortable with it;
routes contain GPS data.

## How it works

1. Reads `/data/params/d/CarParams` (or `CarParamsPersistent` / `CarParamsPrevRoute`)
   and extracts the printable firmware strings from the binary blob.
2. Parses `fingerprints.py` with Python's `ast` module (no openpilot imports)
   to get every car/ECU firmware list and its line numbers.
3. Groups known versions into families (`<base> (<build>),<family>.<version>`)
   and matches the observed strings against them.
4. Inserts new versions at the end of the matching list.

## License

MIT
