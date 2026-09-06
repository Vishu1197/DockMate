#!/usr/bin/env python3
"""
DockMate - batch AutoDock Vina docking driven through the UCSF Chimera command line.

One receptor, every ligand in a folder, one ranked table out.

    python dockmate.py --check-box    inspect the search box against the receptor
    python dockmate.py --dry-run      list what would run, execute nothing
    python dockmate.py                run the screen
    python dockmate.py --only NAME    run one ligand (substring match)
    python dockmate.py --force        re-dock ligands that already have results

Everything you need to change lives in the CONFIG block below.

Project: https://github.com/Vishu1197/DockMate
License: MIT
"""

from pathlib import Path
import csv
import math
import re
import shutil
import subprocess
import sys
import time

# =============================================================================
#                                   CONFIG
#           This is the only part of the file you need to edit.
# =============================================================================

# --- 1. WHERE YOUR FILES ARE -------------------------------------------------
# Folder holding the prepared receptor. Use a raw string (r"...") on Windows.
#   Windows : Path(r"C:\path\to\project\MYTARGET")
#   Linux   : Path("/home/user/project/MYTARGET")
#   macOS   : Path("/Users/user/project/MYTARGET")
BASE_DIR = Path(r"C:\path\to\your\project\MYTARGET")

# Prepared receptor filename inside BASE_DIR (mol2 or pdb).
RECEPTOR = "MYTARGET_prepared.mol2"

# Where the ligands live. None means "same folder as BASE_DIR".
LIGAND_DIR = None

# Which files count as ligands. Narrow this if the folder holds other things.
LIGAND_GLOB = "*.mol2"

# Extra filenames to ignore (the receptor is excluded automatically).
EXCLUDE = []


# --- 2. THE SEARCH BOX -------------------------------------------------------
# REQUIRED. Centre of the docking box, in the receptor's own coordinate frame.
# There is no sensible default: this is different for every structure, and a
# centre copied from another target will silently dock into empty solvent.
# See "Finding your box centre" in the README, then run --check-box.
CENTER = None                      # e.g. (19.7122, -12.5993, 1.5019)

# Box dimensions in Angstroms. 20-30 A suits most small-molecule pockets.
# A ligand needs room to rotate: keep the box comfortably larger than the
# ligand's longest dimension. DockMate warns when it is not.
SIZE = (25.0, 25.0, 25.0)


# --- 3. VINA SAMPLING --------------------------------------------------------
EXHAUSTIVENESS = 8       # Vina default. Use 16-32 for work you intend to publish.
NUM_MODES = 9            # binding modes to report per ligand
ENERGY_RANGE = 3.0       # kcal/mol; discard modes worse than this vs the best


# --- 4. RECEPTOR STATE -------------------------------------------------------
# True  -> receptor already has explicit hydrogens (e.g. from Chimera Dock Prep)
#          DockMate then sends "r_addh false" and skips re-protonation.
# False -> let Chimera add hydrogens before docking.
RECEPTOR_HAS_HYDROGENS = True


# --- 5. EXECUTABLES ----------------------------------------------------------
# Chimera:
#   Windows : Path(r"C:\Program Files\Chimera 1.19\bin\chimera.exe")
#   Linux   : Path("/usr/local/chimera/bin/chimera")
#   macOS   : Path("/Applications/Chimera.app/Contents/MacOS/chimera")
CHIMERA_EXE = Path(r"C:\Program Files\Chimera 1.19\bin\chimera.exe")

# AutoDock Vina executable.
# IMPORTANT: this path must contain NO SPACES. Chimera's command parser splits
# arguments on whitespace and will mangle it. On Windows the default install
# lands in "C:\Program Files (x86)\The Scripps Research Institute\Vina\" -
# copy vina.exe somewhere clean such as C:\vina\ and point here.
VINA_EXE = Path(r"C:\vina\vina.exe")


# --- 6. RUN CONTROL ----------------------------------------------------------
OUT_DIR = None           # None -> BASE_DIR / "vina_out"
TIMEOUT_MIN = 60         # per-ligand wall-clock limit
SKIP_EXISTING = True     # skip ligands that already have results (resumable)

# =============================================================================
#                                END OF CONFIG
# =============================================================================


# ------------------------------------------------------------------ helpers --

def cpath(p: Path) -> str:
    """Chimera wants forward slashes in paths, even on Windows."""
    return str(p).replace("\\", "/")


def safe_stem(stem: str) -> str:
    """
    Reduce a ligand filename to a token the Chimera command line can swallow.

    Natural-product and database ligand names are full of commas, brackets,
    parentheses and spaces. Spaces break Chimera's argument splitting outright;
    the rest just make output filenames painful to work with.
    """
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    s = re.sub(r"_+", "_", s)
    if not s:
        s = "ligand"
    if s[0].isdigit():          # never let a name look like a model-number list
        s = "L_" + s
    return s


def mol2_heavy_atoms(path: Path):
    """[(x, y, z, resname)] for non-hydrogen atoms of a mol2 file."""
    out, inside = [], False
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith("@<TRIPOS>ATOM"):
                inside = True
                continue
            if inside and line.startswith("@<TRIPOS>"):
                break
            if inside:
                f = line.split()
                if len(f) >= 6 and not f[5].upper().startswith("H"):
                    try:
                        res = f[7] if len(f) >= 8 else "?"
                        out.append((float(f[2]), float(f[3]), float(f[4]), res))
                    except ValueError:
                        pass
    return out


def pdb_heavy_atoms(path: Path):
    """[(x, y, z, resname)] for non-hydrogen ATOM/HETATM records of a pdb file."""
    out = []
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith(("ATOM", "HETATM")):
                elem = line[76:78].strip() or line[12:16].strip()[:1]
                if elem.upper().startswith("H"):
                    continue
                try:
                    out.append((float(line[30:38]), float(line[38:46]),
                                float(line[46:54]), line[17:20].strip()))
                except ValueError:
                    pass
    return out


def heavy_atoms(path: Path):
    if path.suffix.lower() in (".pdb", ".ent"):
        return pdb_heavy_atoms(path)
    return mol2_heavy_atoms(path)


def ligand_max_dim(path: Path) -> float:
    """Longest heavy-atom extent, to sanity-check a ligand against the box."""
    c = heavy_atoms(path)
    if not c:
        return 0.0
    xs = [a[0] for a in c]
    ys = [a[1] for a in c]
    zs = [a[2] for a in c]
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


# ---------------------------------------------------------------- box check --

def check_box(receptor: Path, verbose: bool = True) -> int:
    """
    Report how the search box sits against the receptor.

    Returns the number of receptor heavy atoms enclosed. The useful signals:

      * enclosed atoms ~ 0        -> the box is not on the protein at all
      * nearest atom < ~2 A       -> the centre is buried inside an atom
      * no atoms within 3 A       -> the centre is in open space, i.e. a cavity
    """
    heavy = heavy_atoms(receptor)
    if not heavy:
        print("  !  could not read any heavy atoms from the receptor")
        return 0

    xs = [a[0] for a in heavy]
    ys = [a[1] for a in heavy]
    zs = [a[2] for a in heavy]
    cx, cy, cz = CENTER
    hx, hy, hz = (s / 2.0 for s in SIZE)

    enclosed = [a for a in heavy
                if abs(a[0] - cx) <= hx and abs(a[1] - cy) <= hy
                and abs(a[2] - cz) <= hz]

    if verbose:
        print(f"  receptor heavy atoms      : {len(heavy)}")
        print(f"  receptor bbox X           : {min(xs):9.2f} .. {max(xs):9.2f}")
        print(f"  receptor bbox Y           : {min(ys):9.2f} .. {max(ys):9.2f}")
        print(f"  receptor bbox Z           : {min(zs):9.2f} .. {max(zs):9.2f}")
        print(f"  box spans   X             : {cx - hx:9.2f} .. {cx + hx:9.2f}")
        print(f"  box spans   Y             : {cy - hy:9.2f} .. {cy + hy:9.2f}")
        print(f"  box spans   Z             : {cz - hz:9.2f} .. {cz + hz:9.2f}")
        inside_bbox = (min(xs) <= cx - hx and cx + hx <= max(xs)
                       and min(ys) <= cy - hy and cy + hy <= max(ys)
                       and min(zs) <= cz - hz and cz + hz <= max(zs))
        print(f"  box inside receptor bbox  : {inside_bbox}")
        print(f"  heavy atoms inside box    : {len(enclosed)}")

        d = sorted(math.dist((a[0], a[1], a[2]), (cx, cy, cz)) for a in heavy)
        print(f"  nearest heavy atom        : {d[0]:.2f} A")
        for r in (3, 5, 8):
            print(f"  heavy atoms within {r:2d} A    : {sum(1 for v in d if v <= r)}")

        lining = sorted({a[3] for a in heavy
                         if math.dist((a[0], a[1], a[2]), (cx, cy, cz)) <= 8})
        if lining:
            print(f"  residues lining site <=8A : {' '.join(lining)}")

        print()
        if not enclosed:
            print("  !! The box encloses NO receptor atoms. Wrong centre. !!")
        elif len(enclosed) < 50:
            print("  !  Very few receptor atoms in the box - check the centre.")
        elif d[0] < 2.0:
            print("  !  Centre sits inside an atom - likely buried, not a cavity.")
        else:
            print("  Box looks reasonable: on the protein, centre in open space.")

    return len(enclosed)


# ---------------------------------------------------------------- preflight --

def preflight(receptor: Path, ligands, out_dir: Path):
    problems = []

    if CENTER is None:
        problems.append(
            "CENTER is not set. Open dockmate.py and set CENTER = (x, y, z)\n"
            "     for this receptor, then verify with:  python dockmate.py --check-box"
        )
    for label, exe in (("Chimera", CHIMERA_EXE), ("Vina", VINA_EXE)):
        if not exe.exists():
            problems.append(f"{label} executable not found: {exe}")
    if " " in str(VINA_EXE):
        problems.append(
            f"VINA_EXE contains a space: {VINA_EXE}\n"
            "     Chimera's parser will mangle it. Copy vina to a path without spaces."
        )
    if not receptor.exists():
        problems.append(f"Receptor not found: {receptor}")
    if not ligands:
        problems.append(f"No ligand files matched {LIGAND_GLOB!r}.")

    if problems:
        print("\nPre-flight failed:\n")
        for p in problems:
            print("  x  " + p)
        print()
        sys.exit(1)

    check_box(receptor)
    out_dir.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------- chimera invocation --

def write_com(com_path: Path, receptor: Path, ligand: Path, out_prefix: Path):
    """Build the Chimera command file for one docking run."""
    r_addh = "false" if RECEPTOR_HAS_HYDROGENS else "true"
    cx, cy, cz = CENTER
    sx, sy, sz = SIZE

    # Two things matter here and both bite silently:
    #
    #  1. The vina command must be ONE physical line. Chimera has no
    #     line-continuation syntax.
    #  2. search_center and search_size are all-or-nothing. Supply only one
    #     and BOTH are ignored, which quietly expands the box to enclose the
    #     entire receptor - slow, and docking into the wrong place.
    vina_line = (
        "vina docking receptor #0 ligand #1"
        f" output {cpath(out_prefix)}"
        f" search_center {cx},{cy},{cz}"
        f" search_size {sx},{sy},{sz}"
        f" exhaustiveness {EXHAUSTIVENESS}"
        f" num_modes {NUM_MODES}"
        f" energy_range {ENERGY_RANGE}"
        f" r_addh {r_addh}"
        " backend local"
        f" location {cpath(VINA_EXE)}"
        " wait true"
    )

    com_path.write_text(
        "# auto-generated by DockMate - do not hand-edit\n"
        "close session\n"
        f"open 0 {cpath(receptor)}\n"
        f"open 1 {cpath(ligand)}\n"
        f"{vina_line}\n"
        "stop confirmed\n",
        encoding="utf-8",
    )


def parse_poses(result_file: Path):
    """Extract 'REMARK VINA RESULT: affinity rmsd_lb rmsd_ub' from the PDBQT."""
    poses = []
    if not result_file.exists():
        return poses
    with open(result_file, errors="replace") as fh:
        for line in fh:
            if "VINA RESULT" in line:
                f = line.split()
                try:
                    poses.append((float(f[-3]), float(f[-2]), float(f[-1])))
                except (ValueError, IndexError):
                    pass
    return poses


# ------------------------------------------------------------------- driver --

def main():
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return

    dry_run = "--dry-run" in argv
    box_only = "--check-box" in argv
    force = "--force" in argv
    only = None
    if "--only" in argv:
        i = argv.index("--only")
        if i + 1 < len(argv):
            only = argv[i + 1].lower()

    receptor = BASE_DIR / RECEPTOR
    lig_dir = Path(LIGAND_DIR) if LIGAND_DIR else BASE_DIR
    out_dir = Path(OUT_DIR) if OUT_DIR else BASE_DIR / "vina_out"

    # --check-box needs only the receptor and CENTER
    if box_only:
        if CENTER is None:
            print("CENTER is not set in the CONFIG block.")
            sys.exit(1)
        if not receptor.exists():
            print(f"Receptor not found: {receptor}")
            sys.exit(1)
        print("=" * 72)
        print(f"  receptor : {receptor.name}")
        print(f"  box      : centre {CENTER}  size {SIZE}")
        print("=" * 72)
        check_box(receptor)
        return

    skip = {receptor.name, *EXCLUDE}
    ligands = sorted(p for p in lig_dir.glob(LIGAND_GLOB)
                     if p.name not in skip and p.is_file())
    if only:
        ligands = [p for p in ligands if only in p.name.lower()]

    print("=" * 72)
    print(f"  receptor : {receptor.name}")
    print(f"  ligands  : {len(ligands)} from {lig_dir}")
    print(f"  box      : centre {CENTER}  size {SIZE}")
    print(f"  sampling : exhaustiveness {EXHAUSTIVENESS}, {NUM_MODES} modes")
    print(f"  output   : {out_dir}")
    print("=" * 72)

    preflight(receptor, ligands, out_dir)

    com_dir = out_dir / "_com"
    log_dir = out_dir / "_logs"
    clean_dir = out_dir / "_ligands_clean"
    for d in (com_dir, log_dir, clean_dir):
        d.mkdir(parents=True, exist_ok=True)

    if dry_run:
        print("\n-- dry run, nothing will be executed --\n")
        for p in ligands:
            print(f"  {p.name}  ->  {safe_stem(p.stem)}")
        return

    rows, pose_rows = [], []
    t_all = time.time()

    for i, lig in enumerate(ligands, 1):
        stem = safe_stem(lig.stem)
        prefix = out_dir / stem
        print(f"\n[{i}/{len(ligands)}] {lig.name}")

        if SKIP_EXISTING and not force and prefix.exists():
            poses = parse_poses(prefix)
            best = poses[0][0] if poses else None
            print(f"    already done (best {best}) - skipping")
            rows.append([lig.name, stem, "skipped", best, len(poses), 0.0])
            for n, (a, lb, ub) in enumerate(poses, 1):
                pose_rows.append([lig.name, n, a, lb, ub])
            continue

        md = ligand_max_dim(lig)
        if md > 0.8 * min(SIZE):
            print(f"    !  ligand spans {md:.1f} A in a {min(SIZE):.0f} A box - "
                  "little room to sample; consider a larger box")

        # Stage under a parser-safe name; originals are never modified.
        clean = clean_dir / (stem + lig.suffix)
        shutil.copy2(lig, clean)

        com = com_dir / (stem + ".com")
        write_com(com, receptor, clean, prefix)

        t0 = time.time()
        try:
            proc = subprocess.run(
                [str(CHIMERA_EXE), "--nogui", str(com)],
                capture_output=True, text=True, timeout=TIMEOUT_MIN * 60,
            )
            output = (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or "")
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            output, rc = f"TIMEOUT after {TIMEOUT_MIN} min", -1
        dt = time.time() - t0

        (log_dir / (stem + ".log")).write_text(output, encoding="utf-8")

        # Chimera can exit 0 having produced nothing, so trust the output file.
        poses = parse_poses(prefix)
        if poses:
            status, best = "ok", poses[0][0]
            print(f"    best {best:8.2f} kcal/mol   {len(poses)} modes   {dt:.0f}s")
        else:
            status, best = ("timeout" if rc == -1 else "FAILED"), None
            print(f"    {status}  ({dt:.0f}s)  see {log_dir / (stem + '.log')}")

        rows.append([lig.name, stem, status, best, len(poses), round(dt, 1)])
        for n, (a, lb, ub) in enumerate(poses, 1):
            pose_rows.append([lig.name, n, a, lb, ub])

    # ----------------------------------------------------------- reports ----
    summary = out_dir / "summary.csv"
    with open(summary, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ligand", "safe_name", "status",
                    "best_affinity_kcal_mol", "n_modes", "seconds"])
        w.writerows(sorted(rows, key=lambda r: (r[3] is None,
                                                r[3] if r[3] is not None else 0)))

    allp = out_dir / "all_poses.csv"
    with open(allp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ligand", "mode", "affinity_kcal_mol", "rmsd_lb", "rmsd_ub"])
        w.writerows(pose_rows)

    ok = sum(1 for r in rows if r[2] in ("ok", "skipped"))
    print("\n" + "=" * 72)
    print(f"  {ok}/{len(rows)} ligands with results   "
          f"({(time.time() - t_all) / 60:.1f} min total)")
    print(f"  ranked summary : {summary}")
    print(f"  every pose     : {allp}")
    print("=" * 72)

    best = [r for r in rows if r[3] is not None]
    if best:
        print("\n  Top hits:")
        for r in sorted(best, key=lambda r: r[3])[:10]:
            print(f"    {r[3]:8.2f}   {r[0]}")


if __name__ == "__main__":
    main()
