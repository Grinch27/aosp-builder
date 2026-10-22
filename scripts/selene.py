#!/usr/bin/env python3
# Requirements: reproduce one locked LOS20 graph; private manifest stays off public output.
# Ambiguity: resource policy is conservative, not a measured LOS20 minimum or boot proof.
# Research: exact TKUCNXM firmware, geometry, AVB trust and FBE still need device evidence.
# Optimization: fetch one archive file; stream encryption without duplicating large ZIPs.
# Risks: unavailable SHAs, legacy tools, inherited source flags, resource exhaustion.
# Validation: manifest identity, every checkout/link, real exit, boot/OTA/target-files, CMS.
"""Restricted native selene build orchestration; Python standard library only."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-f]{40}\Z")
SAFE_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TZ", "TERM"}
STAGES = {"repo_tool", "manifest_seed", "repo_init", "repo_parser", "source_sync",
          "checkout", "compile", "products", "avb", "seal", "complete"}


class BuildError(Exception):
    """A fixed, safe public error message and a real failure status."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def relative(value):
    """Reject traversal, normalization and special Git/Repo administrative paths."""
    if (not isinstance(value, str) or not value or value.startswith("-")
            or "\\" in value or any(ord(c) < 32 for c in value)):
        raise BuildError("Invalid relative input path.")
    parts = value.split("/")
    if any(p in {"", ".", "..", ".git", ".repo"} for p in parts):
        raise BuildError("Invalid relative input path.")
    return PurePosixPath(value)


def load_lock(path=ROOT / "selene.lock.json"):
    lock = json.loads(Path(path).read_text())
    source, repo, build = lock["source"], lock["repo_tool"], lock["build"]
    if (lock["schema"] != 1 or source["repository"] != "Grinch27/lineageos_selene"
            or source["path"] != "default.xml"
            or not all(SHA.fullmatch(x) for x in (source["commit"], source["blob"], repo["commit"]))
            or not re.fullmatch(r"[0-9a-f]{64}", repo["launcher_sha256"])
            or not re.fullmatch(r"v[0-9]+\.[0-9]+", repo["version"])
            or source["projects"] != 1257 or source["directives"] != 44
            or not 1 <= source["size"] <= 1024 * 1024
            or build["device"] != "selene" or build["lunch"] != "lineage_selene-userdebug"
            or build["targets"] != ["bootimage", "bacon", "target-files-package"]):
        raise BuildError("Invalid selene lock contract.")
    for key, low, high in [("sync_jobs", 1, 8), ("build_jobs", 1, 8),
                           ("sync_seconds", 60, 9000), ("compile_seconds", 60, 10200),
                           ("minimum_free_gib", 400, 4096), ("minimum_memory_gib", 64, 4096)]:
        if type(build[key]) is not int or not low <= build[key] <= high:
            raise BuildError("Invalid build resource or timeout policy.")
    return lock


def check_blob(data, entry):
    oid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    if len(data) != entry["size"] or oid != entry["blob"]:
        raise BuildError("Archive input identity mismatch.")


def parse_manifest(data, source):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise BuildError("Manifest entity declarations are not supported.")
    root = ET.fromstring(data)
    if root.tag != "manifest" or any(e.tag not in {"remote", "default", "project"} for e in root):
        raise BuildError("Unreviewed manifest structure.")
    remotes = {r.get("name"): r.get("fetch") for r in root.findall("remote")}
    if (len(remotes) != len(root.findall("remote")) or not remotes
            or any(url not in {"https://github.com", "https://android.googlesource.com"}
                   for url in remotes.values())):
        raise BuildError("Unreviewed manifest remote.")
    defaults = root.findall("default")
    if len(defaults) != 1 or defaults[0].get("remote") not in remotes:
        raise BuildError("Invalid manifest default.")
    projects = root.findall("project")
    paths, destinations, directives = set(), set(), 0
    for p in projects:
        relative(p.get("name", ""))
        path = str(relative(p.get("path", p.get("name", ""))))
        if (path in paths or not SHA.fullmatch(p.get("revision", ""))
                or p.get("remote", defaults[0].get("remote")) not in remotes):
            raise BuildError("Duplicate project, unlocked revision or unknown remote.")
        paths.add(path)
        for child in p:
            if child.tag not in {"copyfile", "linkfile"}:
                raise BuildError("Unreviewed project directive.")
            relative(child.get("src", ""))
            dest = str(relative(child.get("dest", "")))
            if dest in destinations:
                raise BuildError("Duplicate copy/link destination.")
            destinations.add(dest)
            directives += 1
    if len(projects) != source["projects"] or directives != source["directives"]:
        raise BuildError("Manifest project/directive count mismatch.")
    # A link may sit alongside projects but must not replace a project directory.
    if any(p == d or p.startswith(d + "/") for p in paths for d in destinations):
        raise BuildError("A copy/link destination replaces a project directory.")
    return projects


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BuildError("Authenticated source redirects are rejected.")


def fetch_manifest(lock, token):
    source = lock["source"]
    url = ("https://api.github.com/repos/" + source["repository"] + "/contents/default.xml?ref="
           + source["commit"])
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "selene-lineage-builder"})
    opener = urllib.request.build_opener(NoRedirect)
    for attempt in range(3):
        try:
            with opener.open(request, timeout=60) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise BuildError("Archive API response exceeds limit.")
            record = json.loads(raw)
            if (record.get("type") != "file" or record.get("encoding") != "base64"
                    or record.get("sha") != source["blob"] or record.get("size") != source["size"]):
                raise BuildError("Archive API file metadata mismatch.")
            data = base64.b64decode("".join(record["content"].split()), validate=True)
            check_blob(data, source)
            parse_manifest(data, source)
            return data
        except urllib.error.HTTPError as error:
            if attempt == 2 or error.code not in {429, 500, 502, 503, 504}:
                raise BuildError("Private archive request failed.") from None
            time.sleep(2 ** attempt)
        except urllib.error.URLError:
            raise BuildError("Private archive connection failed.") from None
    raise BuildError("Private archive retry limit reached.")


def new_file(path, data):
    path = Path(path)
    with path.open("xb") as handle:
        handle.write(data)
    path.chmod(0o600)


def clean_environment(parent):
    env = {k: v for k, v in parent.items() if k in SAFE_ENV}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="user.name", GIT_CONFIG_VALUE_0="selene-builder",
               GIT_CONFIG_KEY_1="user.email", GIT_CONFIG_VALUE_1="selene-builder@local.invalid",
               GIT_AUTHOR_NAME="selene-builder", GIT_COMMITTER_NAME="selene-builder",
               GIT_AUTHOR_EMAIL="selene-builder@local.invalid",
               GIT_COMMITTER_EMAIL="selene-builder@local.invalid")
    return env


def stop_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
        if sig == signal.SIGKILL:
            break
    process.wait(timeout=5)


def command(argv, cwd, env, log, timeout):
    """Keep raw output private; terminate the entire process group on interruption."""
    with Path(log).open("xb") as output:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            result = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BuildError("Build phase timed out.", 124) from None
        finally:
            stop_group(process)
    if result:
        raise BuildError("Build phase failed; raw output remains private.", 128 - result if result < 0 else result)


def git_output(project, *args):
    return subprocess.check_output(["git", "-C", str(project), *args], stderr=subprocess.DEVNULL,
                                   env=clean_environment(os.environ), timeout=60).strip()


def verify_checkout(projects, source):
    source = Path(source).resolve(strict=True)
    for p in projects:
        project = source / p.get("path", p.get("name"))
        if project.resolve(strict=True) != project.absolute():
            raise BuildError("Project path traverses a symlink.")
        if git_output(project, "rev-parse", "--show-toplevel").decode() != str(project):
            raise BuildError("Project Git root mismatch.")
        if git_output(project, "rev-parse", "HEAD").decode() != p.get("revision"):
            raise BuildError("Checkout revision mismatch.")
        if git_output(project, "status", "--porcelain", "--untracked-files=all"):
            raise BuildError("Unexpected checkout changes.")
        if any(line.startswith(b"160000 ") for line in git_output(project, "ls-files", "--stage").splitlines()):
            raise BuildError("Unreviewed nested Git dependency.")
        for child in p:
            expected = project / child.get("src")
            actual = source / child.get("dest")
            if not expected.resolve(strict=True).is_relative_to(project):
                raise BuildError("Copy/link source escapes its project.")
            if child.tag == "linkfile":
                if not actual.is_symlink() or actual.resolve(strict=True) != expected.resolve(strict=True):
                    raise BuildError("Checkout linkfile mismatch.")
            elif (actual.is_symlink() or not actual.is_file() or not expected.is_file()
                  or digest(actual) != digest(expected)):
                raise BuildError("Checkout copyfile mismatch.")


def verify_parser(original, parsed):
    def index(projects):
        return {p.get("path", p.get("name")): (p.get("name"), p.get("revision"),
                sorted((c.tag, c.get("src"), c.get("dest")) for c in p)) for p in projects}
    if index(original) != index(ET.parse(parsed).getroot().findall("project")):
        raise BuildError("Repo parser changed project identities or directives.")


def preflight(lock, work):
    work = Path(work).absolute()
    if (not work.parent.is_dir() or work.parent.resolve() != work.parent
            or work.exists() or work.is_symlink() or work == ROOT):
        raise BuildError("Use a new work directory under an existing real directory.")
    if (sys.platform != "linux" or os.uname().machine != "x86_64"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise BuildError("Full builds require an isolated x86_64 GitHub-hosted Linux runner.")
    memory = int(re.search(r"^MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text(), re.M).group(1)) * 1024
    free = shutil.disk_usage(work.parent).free
    policy = lock["build"]
    if free < policy["minimum_free_gib"] * 1024**3 or memory < policy["minimum_memory_gib"] * 1024**3:
        raise BuildError("Resource preflight failed: require 400 GiB free disk and 64 GiB RAM; no cleanup is attempted.")
    work.mkdir(mode=0o700)
    (work / "logs").mkdir(mode=0o700)


def fetch_inputs(lock, work):
    work = Path(work)
    if not (work / "logs").is_dir() or work.resolve() != work.absolute():
        raise BuildError("Preflight work directory is unavailable.")
    token = os.environ.get("LINEAGE_SOURCE_TOKEN", "")
    certificate = os.environ.get("SELENE_DELIVERY_CERT", "")
    if not token or not certificate:
        raise BuildError("Configure LINEAGE_SOURCE_TOKEN and SELENE_DELIVERY_CERT before building.")
    inputs = work / "inputs"
    inputs.mkdir(mode=0o700)
    new_file(inputs / "recipient.pem", certificate.encode())
    command(["openssl", "x509", "-in", str(inputs / "recipient.pem"), "-noout", "-checkend", "21600"],
            work, clean_environment(os.environ), work / "logs/certificate.log", 30)
    # Test CMS recipient compatibility before source download or compilation.
    new_file(inputs / "certificate-test.txt", b"selene recipient compatibility check\n")
    command(["openssl", "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER",
             "-in", str(inputs / "certificate-test.txt"), "-out", str(inputs / "certificate-test.cms"),
             str(inputs / "recipient.pem")], work, clean_environment(os.environ), work / "logs/cms-test.log", 30)
    new_file(inputs / "default.xml", fetch_manifest(lock, token))
    print("Private manifest identity and structure: PASS.", flush=True)


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def boot_header(image):
    with Path(image).open("rb") as handle:
        header = handle.read(1660)
    if len(header) != 1660 or header[:8] != b"ANDROID!":
        raise BuildError("Boot image magic or header missing.")
    kernel, ramdisk, second, page, version = (
        struct.unpack_from("<I", header, offset)[0] for offset in (8, 16, 24, 36, 40))
    size = Path(image).stat().st_size
    if (version != 2 or page != 2048 or not kernel or not ramdisk or size > 64 * 1024**2
            or struct.unpack_from("<I", header, 1644)[0] != 1660):
        raise BuildError("Boot image differs from the recorded selene header/partition contract.")
    minimum = page + sum((n + page - 1) // page * page for n in (kernel, ramdisk, second))
    dtbo = struct.unpack_from("<I", header, 1632)[0]
    dtbo_offset = struct.unpack_from("<Q", header, 1636)[0]
    dtb = struct.unpack_from("<I", header, 1648)[0]
    if minimum + dtbo + dtb > size or (dtbo and dtbo_offset + dtbo > size):
        raise BuildError("Truncated boot payload.")
    return {"header_version": version, "page_size": page, "bytes": size,
            "kernel_bytes": kernel, "ramdisk_bytes": ramdisk}


def zip_check(archive):
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise BuildError("Duplicate archive entries.")
    for name in names:
        relative(name.rstrip("/"))
    if archive.testzip() is not None:
        raise BuildError("Archive CRC verification failed.")


def small_entry(archive, name):
    if archive.getinfo(name).file_size > 1024**2:
        raise BuildError("Unexpected metadata entry size.")
    return archive.read(name)


def products(out):
    product = Path(out) / "target/product/selene"
    boot = product / "boot.img"
    header = boot_header(boot)
    roms = list(product.glob("lineage-*.zip"))
    targets = list((product / "obj/PACKAGING/target_files_intermediates").glob("*.zip"))
    if len(roms) != 1 or len(targets) != 1:
        raise BuildError("Exactly one fresh OTA and one target-files ZIP are required.")
    if any(p.is_symlink() or not p.is_file() for p in (boot, roms[0], targets[0])):
        raise BuildError("Build outputs must be regular files.")
    with zipfile.ZipFile(roms[0]) as ota:
        zip_check(ota)
        metadata = dict(line.split("=", 1) for line in
                        small_entry(ota, "META-INF/com/android/metadata").decode().splitlines() if "=" in line)
        if metadata.get("ota-type") != "AB" or "selene" not in metadata.get("pre-device", "").split("|"):
            raise BuildError("OTA device/type mismatch.")
        with ota.open("payload.bin") as payload:
            payload_header = payload.read(24)
        if (len(payload_header) != 24 or payload_header[:4] != b"CrAU"
                or struct.unpack_from(">Q", payload_header, 4)[0] != 2):
            raise BuildError("Missing Android AB payload v2.")
        manifest_size, signature_size = struct.unpack_from(">QI", payload_header, 12)
        if not manifest_size or 24 + manifest_size + signature_size > ota.getinfo("payload.bin").file_size:
            raise BuildError("Truncated Android AB payload.")
        properties = dict(line.split("=", 1) for line in
                          small_entry(ota, "payload_properties.txt").decode().splitlines() if "=" in line)
        metadata_bytes = 24 + manifest_size + signature_size
        payload_digest, metadata_digest, offset = hashlib.sha256(), hashlib.sha256(), 0
        with ota.open("payload.bin") as payload:
            while block := payload.read(1024**2):
                payload_digest.update(block)
                if offset < metadata_bytes:
                    metadata_digest.update(block[:metadata_bytes - offset])
                offset += len(block)
        expected = {"FILE_SIZE": str(offset), "METADATA_SIZE": str(metadata_bytes),
                    "FILE_HASH": base64.b64encode(payload_digest.digest()).decode(),
                    "METADATA_HASH": base64.b64encode(metadata_digest.digest()).decode()}
        if any(properties.get(key) != value for key, value in expected.items()):
            raise BuildError("OTA payload properties do not match the actual bytes.")
    with zipfile.ZipFile(targets[0]) as target:
        zip_check(target)
        misc = dict(line.split("=", 1) for line in small_entry(target, "META/misc_info.txt").decode().splitlines()
                    if "=" in line and not line.startswith("#"))
        if misc.get("ab_update") != "true" or misc.get("use_dynamic_partitions") != "true":
            raise BuildError("Target-files AB/dynamic partition contract mismatch.")
        partitions = small_entry(target, "META/ab_partitions.txt").decode().split()
        if not {"system", "vendor"}.issubset(partitions):
            raise BuildError("Target-files AB partition list is incomplete.")
        small_entry(target, "META/dynamic_partitions_info.txt")
        with target.open("IMAGES/boot.img") as image:
            if hashlib.file_digest(image, "sha256").hexdigest() != digest(boot):
                raise BuildError("Standalone and target-files boot images differ.")
    files = {"boot.img": boot, "rom.zip": roms[0], "target-files.zip": targets[0]}
    report = {"device": "selene", "build": "PASS", "boot": header, "ota_type": "AB",
              "virtual_ab": misc.get("virtual_ab", "UNCONFIRMED"), "ab_partitions": partitions,
              "stock_compatibility": "NOT_TESTED", "device_boot": "NOT_TESTED",
              "flash_readiness": "NOT_ACHIEVED", "files": {
                  name: {"bytes": path.stat().st_size, "sha256": digest(path)} for name, path in files.items()}}
    return files, report


def seal(files, report, work):
    """Only a successful, validated payload is exported; no source or logs."""
    delivery = work / "delivery"
    delivery.mkdir(mode=0o700)
    report_path, sums_path = work / "static-validation.json", work / "SHA256SUMS"
    new_file(report_path, (json.dumps(report, indent=2) + "\n").encode())
    files = {**files, "static-validation.json": report_path}
    new_file(sums_path, "".join(digest(p) + "  " + n + "\n" for n, p in files.items()).encode())
    files["SHA256SUMS"] = sums_path
    with (delivery / "selene-output.cms").open("xb") as output, (work / "logs/seal.log").open("xb") as log:
        process = subprocess.Popen(["openssl", "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER",
                                    str(work / "inputs/recipient.pem")], env=clean_environment(os.environ),
                                   stdin=subprocess.PIPE, stdout=output, stderr=log, start_new_session=True)
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                for name, path in files.items():
                    archive.add(path, arcname=name, recursive=False)
            process.stdin.close()
            if process.wait(timeout=300):
                raise BuildError("CMS delivery encryption failed.")
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            stop_group(process)


def run(lock, work):
    work = Path(work).resolve(strict=True)
    env = clean_environment(os.environ)
    data = (work / "inputs/default.xml").read_bytes()
    check_blob(data, lock["source"])
    graph = parse_manifest(data, lock["source"])
    stage = "repo_tool"

    def phase(name):
        nonlocal stage
        if name not in STAGES:
            raise BuildError("Invalid progress stage.")
        stage = name
        status = {"stage": name}
        if name == "complete":
            status["exit_code"] = 0
        (work / "status.json").write_text(json.dumps(status) + "\n")
        print("Build stage: " + name, flush=True)

    try:
        for name in ("bin", "manifest", "android", "tmp"):
            (work / name).mkdir(mode=0o700)
        phase("repo_tool")
        tool = lock["repo_tool"]
        command(["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "2",
                 "--max-time", "120", "https://raw.githubusercontent.com/GerritCodeReview/git-repo/"
                 + tool["commit"] + "/repo", "--output", str(work / "bin/repo")], work, env,
                work / "logs/repo-tool.log", 400)
        if digest(work / "bin/repo") != tool["launcher_sha256"]:
            raise BuildError("Repo launcher digest mismatch.")
        (work / "bin/repo").chmod(0o700)
        env["PATH"] = str(work / "bin") + os.pathsep + env["PATH"]
        phase("manifest_seed")
        new_file(work / "manifest/default.xml", data)
        for index, argv in enumerate((["git", "init", "--initial-branch=main", "--quiet"],
                                       ["git", "add", "default.xml"],
                                       ["git", "commit", "--quiet", "-m", "Locked manifest seed"])):
            command(argv, work / "manifest", env, work / ("logs/seed-" + str(index) + ".log"), 30)
        source = work / "android"
        phase("repo_init")
        command(["repo", "init", "-u", "file://" + str(work / "manifest"), "-b", "main", "-m", "default.xml",
                 "--groups=all", "--depth=1", "--no-clone-bundle", "--no-use-superproject",
                 "--repo-rev=" + tool["version"]], source, env, work / "logs/repo-init.log", 300)
        if git_output(source / ".repo/repo", "rev-parse", "HEAD").decode() != tool["commit"]:
            raise BuildError("Repo implementation revision mismatch.")
        phase("repo_parser")
        parsed = work / "parser-manifest.xml"
        command(["repo", "manifest", "-o", str(parsed)], source, env, work / "logs/parser.log", 60)
        verify_parser(graph, parsed)
        phase("source_sync")
        command(["repo", "sync", "-c", "-j" + str(lock["build"]["sync_jobs"]), "--no-clone-bundle",
                 "--no-tags", "--fail-fast", "--retry-fetches=2"], source, env,
                work / "logs/repo-sync.log", lock["build"]["sync_seconds"])
        phase("checkout")
        verify_checkout(graph, source)
        command(["repo", "manifest", "-r", "-o", str(work / "checked-manifest.xml")], source, env,
                work / "logs/manifest-checked.log", 60)
        verify_parser(graph, work / "checked-manifest.xml")
        phase("compile")
        env.update(OUT_DIR=str(work / "out"), TMPDIR=str(work / "tmp"), BUILD_ROOT=str(work),
                   SELENE_BUILD_JOBS=str(lock["build"]["build_jobs"]), GOMAXPROCS="4")
        command(["bash", str(ROOT / "scripts/selene-build.sh")], source, env,
                work / "logs/compile.log", lock["build"]["compile_seconds"])
        phase("products")
        files, report = products(work / "out")
        report["source_commit"] = lock["source"]["commit"]
        report["manifest_blob"] = lock["source"]["blob"]
        report["verified_projects"] = len(graph)
        phase("avb")
        for operation in ("info_image", "verify_image"):
            command([sys.executable, str(source / "external/avb/avbtool.py"), operation,
                     "--image", str(files["boot.img"])], source, env, work / ("logs/avb-" + operation + ".log"), 120)
        report["avb_self_consistency"] = "PASS"
        report["avb_stock_key_trust"] = "NOT_TESTED"
        phase("seal")
        seal(files, report, work)
        phase("complete")
    except BaseException as error:
        (work / "status.json").write_text(json.dumps({"stage": stage, "exit_code": getattr(error, "code", 1)}) + "\n")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["validate-lock", "check-manifest", "preflight", "fetch", "run"])
    parser.add_argument("--work", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    lock = load_lock()
    if args.operation == "validate-lock":
        print("Selene lock contract: PASS.")
        return
    if args.operation == "check-manifest":
        if args.manifest is None:
            parser.error("--manifest is required")
        data = args.manifest.read_bytes()
        check_blob(data, lock["source"])
        graph = parse_manifest(data, lock["source"])
        print("Manifest identity: PASS; projects=" + str(len(graph)) + "; directives=44.")
        return
    if args.work is None:
        parser.error("--work is required")
    {"preflight": preflight, "fetch": fetch_inputs, "run": run}[args.operation](lock, args.work)


if __name__ == "__main__":
    os.umask(0o077)

    def interrupted(signum, frame):
        raise BuildError("Build interrupted.", 128 + signum)

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except BuildError as error:
        print(str(error), file=sys.stderr)
        sys.exit(error.code)
    except Exception:
        print("Restricted build operation failed; raw details remain private.", file=sys.stderr)
        sys.exit(1)
