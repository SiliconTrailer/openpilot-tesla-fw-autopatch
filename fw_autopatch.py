#!/usr/bin/env python3
"""
fw_autopatch.py - keep a Tesla on openpilot recognized after a Tesla firmware
update, without waiting for the next openpilot release.

When a Tesla software update changes an ECU firmware string that the installed
openpilot release doesn't know yet, openpilot falls back to MOCK
("Car Unrecognized: Dashcam Mode"). This script adds the new version to the
local opendbc fingerprints, but only if it belongs to a firmware family that is
already listed for exactly one car + ECU.

Run on the comma device (no extra dependencies, plain Python 3.9+):

  python3 fw_autopatch.py              # dry run, or status if the car is recognized
  python3 fw_autopatch.py --apply      # patch fingerprints.py and approve the versions
  python3 fw_autopatch.py --install    # copy to /data and add the boot hook
  python3 fw_autopatch.py --status     # hook, approved versions, patch state per copy
  python3 fw_autopatch.py --uninstall  # remove the boot hook
  python3 fw_autopatch.py --revert     # restore fingerprints.py, forget approvals

Two copies of openpilot are handled:
  live    /data/openpilot                (running now)
  staged  /data/safe_staging/finalized   (prepared by the updater, swapped in on
                                          the next start)

The updater re-stages a clean copy whenever the live copy differs from git, so
the staged copy loses the patch again and again. The boot hook (--reapply,
installed with --install) re-adds the approved versions before openpilot starts
and before the staged copy is swapped in. It never adds unapproved versions.
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

DATA = os.environ.get("FW_AUTOPATCH_DATA", "/data")  # override only for testing
PARAMS_DIR = os.path.join(DATA, "params/d")
PARAM_FILES = ["CarParams", "CarParamsPersistent", "CarParamsPrevRoute"]
BACKUP_DIR = os.path.join(DATA, "fw_autopatch_backups")
BACKUPS_KEPT = 5  # per copy, plus the oldest one
APPROVED_FILE = os.path.join(DATA, "fw_autopatch_approved.txt")
INSTALL_PATH = os.path.join(DATA, "fw_autopatch.py")
LOG_FILE = os.path.join(DATA, "fw_autopatch.log")
CONTINUE_SH = os.path.join(DATA, "continue.sh")
CONTINUE_BAK = CONTINUE_SH + ".fw_autopatch.bak"
HOOK_TAG = "fw_autopatch.py --reapply"
HOOK_LINE = (f"timeout 20 python3 {INSTALL_PATH} --reapply >> {LOG_FILE} 2>&1 || true"
             "  # fw_autopatch boot hook")
INSTALL_ROOTS = {
  "live": os.path.join(DATA, "openpilot"),
  "staged": os.path.join(DATA, "safe_staging/finalized"),
}
FINGERPRINT_SUBPATHS = [
  "opendbc_repo/opendbc/car/tesla/fingerprints.py",
  "opendbc/car/tesla/fingerprints.py",
  "**/car/tesla/fingerprints.py",
]
VERSION_RE = re.compile(
  r"^(?P<base>.+) \((?P<build>\d+)\),(?P<family>[A-Za-z]+\d+)\.(?P<rest>[0-9][0-9.]*)$"
)
MARKER = "fw_autopatch"


# ---------------------------------------------------------------- helpers

def now():
  return datetime.datetime.now().isoformat(timespec="seconds")


def fw_short(version: bytes) -> str:
  """b'TeM3_E014p10_0.0.0 (35),E014.21.2' -> 'E014.21.2'"""
  return version.decode("latin-1").split(",", 1)[-1]


def family_of(version: bytes):
  m = VERSION_RE.match(version.decode("latin-1"))
  return (m["base"], m["family"]) if m else None


def version_key(version: bytes):
  m = VERSION_RE.match(version.decode("latin-1"))
  return tuple(int(x) for x in m["rest"].split(".") if x), int(m["build"])


def write_atomic(path, text, mode_from=None):
  tmp = path + ".tmp"
  with open(tmp, "w") as f:
    f.write(text)
  if mode_from and os.path.exists(mode_from):
    shutil.copymode(mode_from, tmp)
  os.replace(tmp, path)


def find_fingerprints():
  """Return {label: path} for every openpilot copy that has a Tesla fingerprints.py."""
  found = {}
  for label, root in INSTALL_ROOTS.items():
    if not os.path.isdir(root):
      continue
    for sub in FINGERPRINT_SUBPATHS:
      hits = glob.glob(os.path.join(root, sub), recursive=True)
      if hits:
        found[label] = hits[0]
        break
  if "live" not in found:
    sys.exit(f"fingerprints.py not found under {INSTALL_ROOTS['live']}")
  return found


def read_param_blob(required=True):
  for name in PARAM_FILES:
    path = os.path.join(PARAMS_DIR, name)
    if os.path.exists(path):
      with open(path, "rb") as f:
        return name, f.read()
  if required:
    sys.exit(f"No CarParams* in {PARAMS_DIR} - start the car once with the comma attached.")
  return None, None


def fingerprint_name(blob):
  if blob is None:
    return None
  if b"MOCK" in blob:
    return "MOCK"
  m = re.search(rb"TESLA_[A-Z0-9_]+", blob)
  return m.group(0).decode() if m else "unknown"


def load_approved():
  if not os.path.exists(APPROVED_FILE):
    return set()
  with open(APPROVED_FILE, "rb") as f:
    return {line.strip() for line in f if line.strip()}


def save_approved(versions):
  approved = load_approved() | set(versions)
  write_atomic(APPROVED_FILE, "".join(v.decode("latin-1") + "\n" for v in sorted(approved)))


def hook_installed():
  if not os.path.exists(CONTINUE_SH):
    return False
  with open(CONTINUE_SH) as f:
    return HOOK_TAG in f.read()


def official_versions(label, path):
  """Versions in the committed (unpatched) fingerprints.py, or None if git can't tell."""
  root = INSTALL_ROOTS[label]
  rel = os.path.relpath(path, root)
  result = subprocess.run(["git", "-C", root, "show", f"HEAD:{rel}"], capture_output=True, text=True)
  if result.returncode != 0:
    return None
  try:
    return {v for e in parse_entries(result.stdout) for v in e["versions"]}
  except (SyntaxError, SystemExit):
    return None


def git_commit(label):
  path = os.path.join(INSTALL_ROOTS[label], "git_src_commit")
  if os.path.exists(path):
    with open(path) as f:
      return f.read().strip()
  return None


# ------------------------------------------------------ fingerprints.py

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


def load_fingerprints(path):
  with open(path) as f:
    src = f.read()
  entries = parse_entries(src)
  fam_map = {}
  for idx, entry in enumerate(entries):
    for v in entry["versions"]:
      fam = family_of(v)
      if fam:
        fam_map.setdefault(fam, set()).add(idx)
  return src, entries, fam_map


def observed_versions(blob: bytes, fam_map):
  """Firmware strings in the CarParams blob that belong to a known family."""
  runs = re.findall(rb"[\x20-\x7e]{8,}", blob)
  found = {}
  for (base, family) in fam_map:
    pattern = re.compile(
      re.escape(base.encode()) + rb" \(\d+\)," + re.escape(family.encode()) + rb"\.[0-9][0-9.]*"
    )
    for run in runs:
      for m in pattern.finditer(run):
        found[m.group(0).rstrip(b".")] = (base, family)
  unknown = [run for run in runs
             if run.startswith(b"Te") and b"," in run and not any(v in run for v in found)]
  return found, unknown


def relation(entry, version):
  """Describe a new version relative to the known versions of its family."""
  fam = family_of(version)
  key = version_key(version)
  same = [v for v in entry["versions"] if family_of(v) == fam]
  lower = [v for v in same if version_key(v) < key]
  higher = [v for v in same if version_key(v) > key]
  if lower and not higher:
    return f"newer firmware, latest known was {fw_short(max(lower, key=version_key))}"
  if lower and higher:
    return (f"between known {fw_short(max(lower, key=version_key))} "
            f"and {fw_short(min(higher, key=version_key))}")
  return f"older than all known, oldest was {fw_short(min(higher, key=version_key))}"


def prune_backups(label):
  backups = sorted(glob.glob(os.path.join(BACKUP_DIR, f"fingerprints.py.{label}.*")))
  for old in backups[1:-BACKUPS_KEPT]:
    os.remove(old)


def apply_patch(label, path, src, entries, additions):
  os.makedirs(BACKUP_DIR, exist_ok=True)
  stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
  backup = os.path.join(BACKUP_DIR, f"fingerprints.py.{label}.{stamp}")
  shutil.copy2(path, backup)

  lines = src.splitlines(keepends=True)
  today = datetime.date.today().isoformat()
  inserts = []
  for idx, version in additions:
    last = entries[idx]["node"].elts[-1]
    indent = " " * last.col_offset
    inserts.append((last.end_lineno, f"{indent}{version!r},  # {MARKER} {today}\n"))
  for line_no, text in sorted(inserts, key=lambda x: x[0], reverse=True):
    lines.insert(line_no, text)
  new_src = "".join(lines)

  compile(new_src, path, "exec")
  new_entries = parse_entries(new_src)
  for idx, version in additions:
    car, ecu = entries[idx]["car"], entries[idx]["ecu"]
    if not any(e["car"] == car and e["ecu"] == ecu and version in e["versions"] for e in new_entries):
      sys.exit(f"[{label}] verification failed for {version!r}, nothing written. Backup: {backup}")

  write_atomic(path, new_src, mode_from=path)
  prune_backups(label)
  return backup


def process(label, path, apply, blob=None, approved=None):
  src, entries, fam_map = load_fingerprints(path)
  if blob is not None:
    found, unknown = observed_versions(blob, fam_map)
  else:
    found = {v: family_of(v) for v in approved if family_of(v) in fam_map}
    unknown = []
  known = {v for e in entries for v in e["versions"]}
  additions, skipped = [], []
  for version, fam in sorted(found.items()):
    if version in known:
      continue
    targets = fam_map.get(fam, set())
    if len(targets) != 1:
      skipped.append((version, f"family {fam[1]} is listed for {len(targets)} car/ECU entries"))
      continue
    additions.append((next(iter(targets)), version))

  res = {"label": label, "path": path, "entries": entries, "additions": additions,
         "present": sorted(v for v in found if v in known), "skipped": skipped,
         "unknown": unknown, "found": set(found), "backup": None}
  if apply and additions:
    res["backup"] = apply_patch(label, path, src, entries, additions)
  return res


def print_result(res):
  print(f"\n[{res['label']}] {res['path']}")
  for v in res["present"]:
    print(f"  OK      {fw_short(v):<14} already listed")
  for v, reason in res["skipped"]:
    print(f"  SKIP    {v.decode()}  ({reason})")
  for run in res["unknown"]:
    print(f"  REVIEW  {run.decode(errors='replace')}  (unknown family, not added - check manually)")
  for idx, v in res["additions"]:
    e = res["entries"][idx]
    print(f"  NEW     {v.decode()}")
    print(f"          {relation(e, v)}")
    print(f"          -> {e['car']} / {e['ecu']}")
  if res["backup"]:
    print(f"  WROTE   {len(res['additions'])} line(s), backup: {res['backup']}")
  if not (res["present"] or res["skipped"] or res["unknown"] or res["additions"]):
    print("  no firmware of a known family found in CarParams")


# ---------------------------------------------------------------- commands

def cmd_status(paths, param_name=None, blob=None):
  if param_name is None:
    param_name, blob = read_param_blob(required=False)
  approved = sorted(load_approved())
  installed = hook_installed()

  fp = fingerprint_name(blob)
  print(f"Last fingerprint: {fp + f'  ({param_name})' if fp else 'unknown (car not started yet)'}")
  print(f"Boot hook:        {'installed in ' + CONTINUE_SH if installed else 'NOT installed (run --install)'}")
  print(f"Approved:         {fw_short(approved[0]) if approved else 'none (run --apply)'}"
        + "".join(f"\n                  {fw_short(v)}" for v in approved[1:]))

  for label, path in paths.items():
    _, entries, _ = load_fingerprints(path)
    known = {v for e in entries for v in e["versions"]}
    missing = [v for v in approved if v not in known]
    if not approved:
      state = "-"
    elif not missing:
      state = "patched"
    else:
      todo = ("boot hook re-adds it on next start" if installed
              else "run --apply before the next start, or --install")
      state = f"missing {', '.join(fw_short(v) for v in missing)} -> {todo}"
    print(f"{label + ' copy:':<18}{state}")

  if "staged" in paths:
    live_c, staged_c = git_commit("live"), git_commit("staged")
    if live_c and live_c == staged_c:
      print("                  staged = same commit as live: the updater re-stages a clean copy")
      print("                  while git sees the live copy as modified (expected, harmless)")
    elif live_c and staged_c:
      print(f"                  staged = different commit ({staged_c[:8]}): a real openpilot update")

  if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE):
    with open(LOG_FILE) as f:
      last = f.read().strip().splitlines()[-1]
    print(f"Last hook action: {last}")


def cmd_apply(paths, args):
  param_name, blob = read_param_blob()
  fp = fingerprint_name(blob)
  if fp != "MOCK" and not args.force:
    cmd_status(paths, param_name, blob)
    print("\nCar is recognized, nothing to patch. Use --force to check anyway.")
    return

  print(f"Last fingerprint: {fp}  ({param_name})")
  results = [process(label, path, args.apply, blob=blob) for label, path in paths.items()]
  for res in results:
    print_result(res)

  # approve only versions that are not part of the official release
  matched = set().union(*(r["found"] for r in results))
  official = official_versions("live", paths["live"])
  to_approve = {v for v in matched if official is None or v not in official}
  added = {r["label"]: len(r["additions"]) for r in results if r["additions"]}
  print()
  if args.apply and to_approve:
    save_approved(to_approve)
  if not added:
    print("Nothing to add.")
  elif not args.apply:
    print("Dry run, nothing written. Run with --apply, then: sudo reboot")
  else:
    print("Summary: " + ", ".join(f"{k} +{v}" for k, v in added.items()) + " line(s) added")
  if args.apply and to_approve:
    print(f"Approved for the boot hook: {', '.join(fw_short(v) for v in sorted(to_approve))}")
  if args.apply and added:
    print("Next: sudo reboot with the car awake")
    if not hook_installed():
      print("Tip: run --install so an openpilot update can't drop the patch again")


def cmd_reapply(paths):
  approved = load_approved()
  if not approved:
    return
  for label, path in paths.items():
    res = process(label, path, apply=True, approved=approved)
    for idx, v in res["additions"]:
      e = res["entries"][idx]
      print(f"{now()} [{label}] re-added {v.decode()} -> {e['car']} / {e['ecu']}")


def cmd_install():
  me = os.path.abspath(__file__)
  if me != os.path.abspath(INSTALL_PATH):
    existed = os.path.exists(INSTALL_PATH)
    shutil.copy2(me, INSTALL_PATH)
    os.chmod(INSTALL_PATH, 0o755)
    print(f"{'Updated' if existed else 'Copied'} script: {INSTALL_PATH}")

  if not os.path.exists(CONTINUE_SH):
    sys.exit(f"{CONTINUE_SH} not found - nothing changed.")
  with open(CONTINUE_SH) as f:
    lines = f.read().splitlines(keepends=True)

  if any(HOOK_TAG in line for line in lines):
    print(f"Boot hook already installed in {CONTINUE_SH}")
  else:
    exec_idx = next((i for i, line in enumerate(lines) if line.lstrip().startswith("exec ")), None)
    if exec_idx is None:
      sys.exit(f"No 'exec' line in {CONTINUE_SH} - not touching it.")
    new_src = "".join(lines[:exec_idx] + [HOOK_LINE + "\n"] + lines[exec_idx:])
    check = subprocess.run(["bash", "-n"], input=new_src, capture_output=True, text=True)
    if check.returncode != 0:
      sys.exit(f"Syntax check failed, nothing changed: {check.stderr.strip()}")
    if not os.path.exists(CONTINUE_BAK):
      shutil.copy2(CONTINUE_SH, CONTINUE_BAK)
      print(f"Backup:  {CONTINUE_BAK}")
    write_atomic(CONTINUE_SH, new_src, mode_from=CONTINUE_SH)
    print(f"Installed boot hook in {CONTINUE_SH}, before line {exec_idx + 1}:")
    print(f"  + {HOOK_LINE}")
    print(f"    {lines[exec_idx].rstrip()}")

  if not load_approved():
    print("\nNo approved versions yet. The hook only re-adds versions you approved:")
    print(f"  python3 {INSTALL_PATH} --apply           # if the car shows Car Unrecognized")
    print(f"  python3 {INSTALL_PATH} --apply --force   # if you already patched it by hand")


def cmd_uninstall():
  if not hook_installed():
    print("Boot hook is not installed.")
    return
  with open(CONTINUE_SH) as f:
    lines = f.read().splitlines(keepends=True)
  removed = [line for line in lines if HOOK_TAG in line]
  new_src = "".join(line for line in lines if HOOK_TAG not in line)
  check = subprocess.run(["bash", "-n"], input=new_src, capture_output=True, text=True)
  if check.returncode != 0:
    sys.exit(f"Syntax check failed, nothing changed: {check.stderr.strip()}")
  write_atomic(CONTINUE_SH, new_src, mode_from=CONTINUE_SH)
  for line in removed:
    print(f"Removed from {CONTINUE_SH}:\n  - {line.rstrip()}")


def cmd_revert(paths):
  for label, path in paths.items():
    directory, name = os.path.split(path)
    result = subprocess.run(["git", "-C", directory, "checkout", "--", name],
                            capture_output=True, text=True)
    if result.returncode == 0:
      print(f"[{label}] restored from git")
      continue
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, f"fingerprints.py.{label}.*")))
    if backups:
      shutil.copy2(backups[0], path)
      print(f"[{label}] git checkout failed, restored oldest backup {backups[0]}")
    else:
      print(f"[{label}] git checkout failed ({result.stderr.strip()}) and no backup found")
  if os.path.exists(APPROVED_FILE):
    os.replace(APPROVED_FILE, APPROVED_FILE + ".bak")
    print(f"Approvals cleared (saved as {APPROVED_FILE}.bak), so the boot hook won't re-add them")
  print("Done. Reboot the comma: sudo reboot")


def main():
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  mode = ap.add_mutually_exclusive_group()
  mode.add_argument("--apply", action="store_true", help="write the changes and approve the versions")
  mode.add_argument("--status", action="store_true", help="show hook, approvals and patch state")
  mode.add_argument("--install", action="store_true", help="copy to /data and add the boot hook")
  mode.add_argument("--uninstall", action="store_true", help="remove the boot hook")
  mode.add_argument("--revert", action="store_true", help="restore fingerprints.py, forget approvals")
  mode.add_argument("--reapply", action="store_true", help="boot hook: re-add approved versions only")
  ap.add_argument("--force", action="store_true", help="check even if the car was recognized")
  args = ap.parse_args()

  if args.install:
    return cmd_install()
  if args.uninstall:
    return cmd_uninstall()

  paths = find_fingerprints()
  if args.reapply:
    return cmd_reapply(paths)
  if args.status:
    return cmd_status(paths)
  if args.revert:
    return cmd_revert(paths)
  return cmd_apply(paths, args)


if __name__ == "__main__":
  main()
