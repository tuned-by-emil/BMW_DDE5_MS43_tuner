"""Build and verify the portable Windows release without implicit downloads."""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import subprocess
import sys
from datetime import datetime, timezone
import zipfile

ROOT = Path(__file__).resolve().parent
RELEASE_VERSION = "1.33.1"
RELEASE_AUTHOR = "Emil Larsson"
BUILD_NAME = "BMW_Bin_Editor_V1_33_1"


def pinned_packages() -> dict[str, str]:
    return dict(
        line.strip().split("==", 1)
        for line in (ROOT / "requirements-build.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def check_environment() -> dict[str, str]:
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Build the release using 64-bit Python on Windows x64.")
    if sys.version_info < (3, 11) or sys.maxsize <= 2**32:
        raise RuntimeError("The pinned release dependencies require Python 3.11+ x64.")
    installed = {}
    mismatches = []
    for name, expected in pinned_packages().items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = "not installed"
        if actual != expected:
            mismatches.append(f"  {name}: expected {expected}, found {actual}")
        installed[name] = actual
    if mismatches:
        raise RuntimeError(
            "Build dependencies differ from requirements-build.txt:\n"
            + "\n".join(mismatches)
            + "\nInstall the pinned dependencies into your build environment, or explicitly pass --install-deps."
        )
    app_metadata = runpy.run_path(str(ROOT / "bmw_bin_editor" / "__init__.py"))
    if app_metadata.get("__version__") != RELEASE_VERSION:
        raise RuntimeError(f"Application version does not match the {RELEASE_VERSION} release packaging.")
    if app_metadata.get("APP_AUTHOR") != RELEASE_AUTHOR:
        raise RuntimeError("Application author does not match the release metadata.")
    return installed


def command(args: list[str], *, env: dict[str, str] | None = None, timeout: int | None = None) -> None:
    print("Running:", subprocess.list2cmdline(args), flush=True)
    subprocess.run(args, cwd=ROOT, env=env, timeout=timeout, check=True)


def self_check(executable: Path, report: Path, *, source: bool = False) -> dict:
    report.unlink(missing_ok=True)
    check_env = os.environ.copy()
    check_env["QT_QPA_PLATFORM"] = "offscreen"
    args = [str(executable)]
    if source:
        args.append(str(ROOT / "run_editor.py"))
    command(args + ["--self-check-public", str(report)], env=check_env, timeout=120)
    if not report.is_file():
        raise RuntimeError("The startup self-check did not produce a report.")
    result = json.loads(report.read_text(encoding="utf-8"))
    if (result.get("ok") is not True or result.get("version") != RELEASE_VERSION
            or result.get("private_definitions_required") is not False
            or result.get("physical_port_opened") is not False
            or result.get("serial_process_check", {}).get("ok") is not True):
        raise RuntimeError(f"Public startup self-check failed: {result}")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_revision() -> str:
    if os.environ.get('GITHUB_SHA'):
        return os.environ['GITHUB_SHA']
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else 'unrecorded source archive'
    except OSError:
        return 'unrecorded source archive'


def native_ols_check(executable: Path, report: Path) -> dict | None:
    """Check the bundled parser against the local private pack without packaging it."""
    fixture = ROOT / "mappacks" / "BMW_30D_EDC16_218PS__E60.ols"
    if not fixture.is_file():
        return None
    if sha256(fixture) != "9470b1482cb6f4147ececa92511535909110a1a31c446f46b98543d85a4df743":
        return None
    report.unlink(missing_ok=True)
    command([str(executable), "--check-ols", str(fixture), str(report)], timeout=120)
    result = json.loads(report.read_text(encoding="utf-8"))
    if (result.get("ok") is not True or result.get("frozen") is not True
            or result.get("characteristics") != 9531 or result.get("converted") != 9414
            or result.get("skipped_count") != 117
            or result.get("library_proposals") != ["EDC16C31 361888"]
            or result.get("column_storage_display_evidence", {}).get("first_visible_row") != [0.8, 1.0, 1.06, 1.24]):
        raise RuntimeError("Portable native OLS recovery/conversion or library matching failed.")
    return {key: result[key] for key in ("source_sha256", "bin_sha256", "characteristics", "converted",
                                        "skipped_count", "library_proposals")}


def copy_third_party_notices(release: Path, packages: dict[str, str]) -> None:
    """Preserve supplier license files and distribution metadata in the package."""
    destination = release / "THIRD_PARTY_LICENSES"
    destination.mkdir()
    for name in packages:
        distribution = metadata.distribution(name)
        package_folder = destination / name
        package_folder.mkdir()
        text = distribution.read_text("METADATA")
        if text:
            (package_folder / "METADATA.txt").write_text(text, encoding="utf-8")
        for file in distribution.files or []:
            if not file.name.upper().startswith(("LICENSE", "COPYING")):
                continue
            source = Path(distribution.locate_file(file))
            if not source.is_file():
                continue
            relative = Path(*file.parts[1:]) if file.parts[0].endswith(".dist-info") else Path(file)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"Invalid supplier license path: {file}")
            target = package_folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        python_folder = destination / "Python"
        python_folder.mkdir()
        shutil.copy2(python_license, python_folder / "LICENSE.txt")
    shutil.copytree(ROOT / "third_party" / "ols_parser", destination / "ols_parser")
    (destination / "README.txt").write_text(
        "Supplier-provided license files and original distribution metadata.\n"
        "Includes application dependencies and build tooling; metadata records each supplier's license information.\n"
        "Qt for Python source and licensing information: https://doc.qt.io/qtforpython-6/\n",
        encoding="utf-8",
    )


def build(*, install_deps: bool = False) -> Path:
    # The default build reuses installed packages and never contacts a registry.
    if install_deps:
        command([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                 "-r", str(ROOT / "requirements-build.txt")])
    packages = check_environment()
    from finalize_windows_build import fix_icu

    dist = ROOT / "dist"
    release = dist / BUILD_NAME
    archive = dist / f"{BUILD_NAME}_Windows_x64_portable.zip"
    for output in (release, archive, archive.with_suffix(".zip.sha256")):
        if output.exists():
            raise RuntimeError(f"Output already exists: {output}\nMove the previous release aside before building again.")
    checks = ROOT / "build" / "release_checks"
    checks.mkdir(parents=True, exist_ok=True)
    self_check(Path(sys.executable), checks / "source-self-check.json", source=True)
    command([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
             "--distpath", str(dist), "--workpath", str(ROOT / "build"),
             str(ROOT / f"{BUILD_NAME}.spec")])
    fix_icu(release)
    executable = release / f"{BUILD_NAME}.exe"
    if not executable.is_file():
        raise RuntimeError(f"Build executable missing: {executable}")
    report = self_check(executable, checks / "frozen-self-check.json")
    ols_check = native_ols_check(executable, checks / "frozen-ols-check.json")
    for document in ("LICENSE.txt", "README.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / document, release / document)
    (release / "docs").mkdir()
    for document in ("LOCAL_RESOURCES.md", "MS43X_SUPPORT.txt", "CHATGPT_TUNING_TIPS.txt",
                     "DDE5_AUTHENTICATION.txt", "DDE5_CONFIG_CHECKSUMS.txt",
                     "DIRECT_SERIAL_LOGGING.txt", "EOI_ESTIMATION.md", "NATIVE_EDIABAS_LOGGING.txt"):
        shutil.copy2(ROOT / "docs" / document, release / "docs" / document)
    copy_third_party_notices(release, packages)
    build_info = {
        "product": "BMW Bin Editor",
        "version": RELEASE_VERSION,
        "author": RELEASE_AUTHOR,
        "built_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "architecture": "Windows x64",
        "packages": packages,
        "executable_sha256": sha256(executable),
        "source_revision": source_revision(),
        "startup_self_check": report,
        "native_ols_check": ols_check,
        "ols_parser": {"license": "0BSD", "supported_format": 113,
                       "helper_sha256": sha256(ROOT / "assets" / "ols" / "ols_dump.exe")},
    }
    (release / "build-info.json").write_text(json.dumps(build_info, indent=2) + "\n", encoding="utf-8")
    temporary_archive = archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary_archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for file in sorted(release.rglob("*")):
            if file.is_file():
                if not file.resolve().is_relative_to(release.resolve()):
                    raise RuntimeError(f"Release file points outside the build folder: {file}")
                package.write(file, file.relative_to(dist))
    with zipfile.ZipFile(temporary_archive) as package:
        bad_file = package.testzip()
        if bad_file:
            raise RuntimeError(f"ZIP integrity check failed: {bad_file}")
    temporary_archive.rename(archive)
    archive.with_suffix(".zip.sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-deps", action="store_true",
                        help="Explicitly allow pip to install pinned dependencies before building.")
    parser.add_argument("--check-only", action="store_true",
                        help="Check the environment and release metadata without building or downloading.")
    args = parser.parse_args()
    if args.check_only and args.install_deps:
        parser.error("--check-only cannot be combined with --install-deps")
    try:
        if args.check_only:
            check_environment()
            print("Windows release build environment and metadata are ready.")
        else:
            archive = build(install_deps=args.install_deps)
            print(f"\nVerified portable release: {archive}\nSHA-256: {archive.with_suffix('.zip.sha256')}")
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Release build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
