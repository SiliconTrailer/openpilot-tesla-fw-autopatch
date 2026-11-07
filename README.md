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

## Usage

Copy the script to `/data` (this partition survives openpilot and AGNOS updates,
but not a full reflash or reset):

```bash
scp fw_autopatch.py comma@<device-ip>:/data/
ssh comma@<device-ip>
```

Then:

```bash
python3 /data/fw_autopatch.py            # dry run: show what would be added
python3 /data/fw_autopatch.py --apply    # patch fingerprints.py
sudo reboot                              # reboot with the car awake
python3 /data/fw_autopatch.py --revert   # restore the original file
```

Example dry run:

```
fingerprints.py: /data/openpilot/opendbc_repo/opendbc/car/tesla/fingerprints.py
Using CarParamsPersistent: last fingerprint MOCK (unrecognized)
ADD    TeM3_E014p10_0.0.0 (35),E014.21.2  ->  CAR.TESLA_MODEL_3 / (Ecu.eps, 0x730, None)

Dry run. Run again with --apply to write, then: sudo reboot
```

Added lines are marked with a `# fw_autopatch <date>` comment. Backups are kept
in `/data/fw_autopatch_backups/`.

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
