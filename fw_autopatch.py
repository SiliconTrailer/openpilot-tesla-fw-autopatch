#!/usr/bin/env python3
"""
fw_autopatch.py - add newly observed Tesla ECU firmware versions to the local
opendbc fingerprints when openpilot falls back to MOCK / dashcam mode after a
Tesla software update.

Run on the comma device (no extra dependencies, plain Python 3.9+):

  python3 /data/fw_autopatch.py            # dry run: show what would be added
  python3 /data/fw_autopatch.py --apply    # patch fingerprints.py, then: sudo reboot
  python3 /data/fw_autopatch.py --revert   # restore the original file

Safety gates:
  * only acts if the last fingerprint was MOCK (override with --force)
  * a new version is only accepted if it belongs to an already known firmware
    family for that car + ECU (same base string and same version family,
    e.g. "TeM3_E014p10_0.0.0" + "E014"); unknown families are only reported
  * a family must map to exactly one car + ECU, otherwise it is skipped
  * dry run by default; --apply keeps a backup and verifies the result
  * the next openpilot update overwrites the patch, which is intended
"""
import argparse
import ast
import datetime
import glob
import os
import re
import shutil
import subprocess
import sys

PARAMS_DIR = "/data/params/d"
PARAM_FILES = ["CarParams", "CarParamsPersistent", "CarParamsPrevRoute"]
BACKUP_DIR = "/data/fw_autopatch_backups"
FINGERPRINT_GLOBS = [
  "/data/openpilot/opendbc_repo/opendbc/car/tesla/fingerprints.py",
  "/data/openpilot/opendbc/car/tesla/fingerprints.py",
  "/data/openpilot/**/car/tesla/fingerprints.py",
]
VERSION_RE = re.compile(
  r"^(?P<base>.+) \((?P<build>\d+)\),(?P<family>[A-Za-z]+\d+)\.(?P<rest>[0-9][0-9.]*)$"
)
MARKER = "fw_autopatch"


def find_fingerprints() -> str:
  for pattern in FINGERPRINT_GLOBS:
    hits = glob.glob(pattern, recursive=True)
    if hits:
      return hits[0]
  sys.exit("fingerprints.py not found under /data/openpilot")


def read_param_blob():
  for name in PARAM_FILES:
    path = os.path.join(PARAMS_DIR, name)
    if os.path.exists(path):
      with open(path, "rb") as f:
        return name, f.read()
  sys.exit("No CarParams* in /data/params/d - start the car once with the comma attached.")


def find_fw_dict(tree: ast.Module) -> ast.Dict:
  for node in tree.body:
    if isinstance(node, ast.Assign):
      names = [t.id for t in node.targets if isinstance(t, ast.Name)]
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
      names = [node.target.id]
    else:
      continue
    if "FW_VERSIONS" in names and isinstance(node.value, ast.Dict):
      return node.value
  sys.exit("FW_VERSIONS dict not found in fingerprints.py")


def parse_entries(src: str):
  """Return a list of {car, ecu, node, versions} for every firmware list."""
  fw = find_fw_dict(ast.parse(src))
  entries = []
  for car_key, ecus in zip(fw.keys, fw.values):
    if not isinstance(ecus, ast.Dict):
      continue
    car = ast.unparse(car_key)
    for ecu_key, lst in zip(ecus.keys, ecus.values):
      if not isinstance(lst, ast.List) or not lst.elts:
        continue
      versions = [e.value for e in lst.elts
                  if isinstance(e, ast.Constant) and isinstance(e.value, bytes)]
      ecu = ast.get_source_segment(src, ecu_key) or ast.unparse(ecu_key)
      entries.append({"car": car, "ecu": ecu, "node": lst, "versions": versions})
  return entries


def family_of(version: bytes):
  m = VERSION_RE.match(version.decode("latin-1"))
  return (m["base"], m["family"]) if m else None


def build_family_map(entries):
  """(base, family) -> list of entry indices that contain that family."""
  fam_map = {}
  for idx, entry in enumerate(entries):
    for v in entry["versions"]:
      fam = family_of(v)
      if fam:
        fam_map.setdefault(fam, set()).add(idx)
  return fam_map


def observed_versions(blob: bytes, fam_map):
  """Find firmware strings in the CarParams blob that belong to a known family."""
  runs = re.findall(rb"[\x20-\x7e]{8,}", blob)
  found = {}  # version bytes -> (base, family)
  for (base, family) in fam_map:
    pattern = re.compile(
      re.escape(base.encode()) + rb" \(\d+\)," + re.escape(family.encode()) + rb"\.[0-9][0-9.]*"
    )
    for run in runs:
      for m in pattern.finditer(run):
        found[m.group(0).rstrip(b".")] = (base, family)
  # Tesla-looking strings that match no known family -> report only
  unknown = []
  for run in runs:
    if run.startswith(b"Te") and b"," in run and not any(v in run for v in found):
      unknown.append(run)
  return found, unknown


def plan(entries, fam_map, found):
  all_known = {v for e in entries for v in e["versions"]}
  additions, skipped = [], []
  for version, fam in sorted(found.items()):
    if version in all_known:
      continue
    targets = fam_map.get(fam, set())
    if len(targets) != 1:
      skipped.append((version, f"family {fam} maps to {len(targets)} car/ECU entries"))
      continue
    additions.append((next(iter(targets)), version))
  return additions, skipped


def apply_patch(path, src, entries, additions):
  os.makedirs(BACKUP_DIR, exist_ok=True)
  stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
  backup = os.path.join(BACKUP_DIR, f"fingerprints.py.{stamp}")
  shutil.copy2(path, backup)

  lines = src.splitlines(keepends=True)
  today = datetime.date.today().isoformat()
  inserts = []
  for idx, version in additions:
    last = entries[idx]["node"].elts[-1]
    indent = " " * last.col_offset
    inserts.append((last.end_lineno, f"{indent}{version!r},  # {MARKER} {today}\n"))
  # insert bottom-up so earlier line numbers stay valid
  for line_no, text in sorted(inserts, key=lambda x: x[0], reverse=True):
    lines.insert(line_no, text)
  new_src = "".join(lines)

  # verify: valid Python and every new version is now in the right list
  compile(new_src, path, "exec")
  new_entries = parse_entries(new_src)
  for idx, version in additions:
    car, ecu = entries[idx]["car"], entries[idx]["ecu"]
    if not any(e["car"] == car and e["ecu"] == ecu and version in e["versions"] for e in new_entries):
      sys.exit(f"Verification failed for {version!r}, nothing written. Backup: {backup}")

  tmp = path + ".tmp"
  with open(tmp, "w") as f:
    f.write(new_src)
  os.replace(tmp, path)
  print(f"Backup: {backup}")


def revert(path):
  directory, name = os.path.split(path)
  result = subprocess.run(["git", "-C", directory, "checkout", "--", name],
                          capture_output=True, text=True)
  if result.returncode == 0:
    print("Restored fingerprints.py from git.")
    return
  backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "fingerprints.py.*")))
  if not backups:
    sys.exit(f"git checkout failed ({result.stderr.strip()}) and no backup found.")
  shutil.copy2(backups[0], path)  # oldest backup = state before the first patch
  print(f"git checkout failed, restored oldest backup {backups[0]}")


def main():
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--apply", action="store_true", help="write the changes")
  ap.add_argument("--force", action="store_true", help="run even if the car was recognized")
  ap.add_argument("--revert", action="store_true", help="restore the original fingerprints.py")
  args = ap.parse_args()

  path = find_fingerprints()
  print(f"fingerprints.py: {path}")

  if args.revert:
    revert(path)
    print("Done. Reboot the comma: sudo reboot")
    return

  param_name, blob = read_param_blob()
  is_mock = b"MOCK" in blob
  print(f"Using {param_name}: last fingerprint {'MOCK (unrecognized)' if is_mock else 'recognized'}")
  if not is_mock and not args.force:
    print("Car was recognized, nothing to do. Use --force to check anyway.")
    return

  with open(path) as f:
    src = f.read()
  entries = parse_entries(src)
  fam_map = build_family_map(entries)
  found, unknown = observed_versions(blob, fam_map)
  additions, skipped = plan(entries, fam_map, found)

  for version, reason in skipped:
    print(f"SKIP   {version.decode()}  ({reason})")
  for run in unknown:
    print(f"REVIEW {run.decode(errors='replace')}  (no known family - check manually)")
  if not additions:
    print("No new versions of known families found.")
    return

  for idx, version in additions:
    e = entries[idx]
    print(f"ADD    {version.decode()}  ->  {e['car']} / {e['ecu']}")

  if not args.apply:
    print("\nDry run. Run again with --apply to write, then: sudo reboot")
    return

  apply_patch(path, src, entries, additions)
  print("Patched. Reboot the comma with the car awake: sudo reboot")


if __name__ == "__main__":
  main()
