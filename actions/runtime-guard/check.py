"""Check a repository against the org runtime standard (runtime-standards.json).

Two sources are checked:

* synthesized CDK templates (every ``*.template.json`` under each ``cdk.out``):
  Lambda functions and layers, EC2 instances, launch templates and AMI
  parameters, and Fargate task definitions;
* the toolchain files: ``.python-version``, ``.nvmrc``/``.node-version``,
  ``pyproject.toml`` (``requires-python``, ruff ``target-version``),
  ``package.json`` (``engines.node``), GitHub workflow ``python-version`` /
  ``node-version`` pins and Dockerfile ``FROM`` lines.

A version below ``minimum`` is an error, one below ``latest`` a warning. A
Python newer than ``latest`` is a warning too (a preview runtime, or CI running
a different Python than the Lambda). Anything on x86_64 is an error. Standard
library only, so it runs on any runner without an install step.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "cdk.out", ".venv", "venv", "dist", "build", ".nx", "vendor"}
GRAVITON = re.compile(r"^[a-z]+\d+[a-z]*g[a-z]*\.")  # t4g.nano, m7gd.large, c7gn.xlarge, g5g.2xlarge
NODE_ALIASES = {"node", "latest", "current", "stable"}


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning" | "exempt"
    where: str  # file path, or "template: LogicalId"
    message: str
    file: str = ""
    line: int = 0


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.match(r"^\s*v?(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def fmt(v: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in v)


class Checker:
    def __init__(self, standards: dict, exempt: list[str], root: Path):
        self.std = standards
        self.root = root
        ex = standards.get("exempt", {})
        self.exempt_ids = [re.compile(p) for p in ex.get("logical_id", [])]
        self.exempt_paths = [re.compile(p) for p in ex.get("path", [])]
        self.exempt_user = [re.compile(p) for p in exempt if p.strip()]
        self.findings: list[Finding] = []

    # -- helpers -----------------------------------------------------------------

    def add(self, level: str, where: str, message: str, file: str = "", line: int = 0) -> None:
        self.findings.append(Finding(level, where, message, file, line))

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(path)

    def judge(self, lang: str, version: tuple[int, ...], where: str, what: str,
              *, newer_ok: bool, file: str = "", line: int = 0) -> None:
        """Compare a version against the standard for ``lang`` ('python' or 'node')."""
        std = self.std[lang]
        width = 2 if lang == "python" else 1
        version = version[:width]
        minimum = parse_version(std["minimum"])[:width]
        latest = parse_version(std["latest"])[:width]
        if len(version) < width:
            return  # "3" alone says nothing about the minor version
        name = "Python" if lang == "python" else "Node"
        if version < minimum:
            self.add("error", where, f"{what} is {name} {fmt(version)}, below the org minimum "
                     f"{fmt(minimum)} (latest: {fmt(latest)})", file, line)
        elif version < latest:
            self.add("warning", where, f"{what} is {name} {fmt(version)}; the org standard is "
                     f"{fmt(latest)}", file, line)
        elif version > latest and not newer_ok:
            self.add("warning", where, f"{what} is {name} {fmt(version)}, newer than the org "
                     f"standard {fmt(latest)} (a preview runtime, or not what the Lambda runs)",
                     file, line)

    def runtime(self, runtime: object, where: str, what: str, file: str = "") -> None:
        if not isinstance(runtime, str):
            return
        if m := re.fullmatch(r"python(\d+\.\d+)", runtime):
            self.judge("python", parse_version(m[1]), where, what, newer_ok=False, file=file)
        elif m := re.fullmatch(r"nodejs(\d+)\.x", runtime):
            self.judge("node", parse_version(m[1]), where, what, newer_ok=False, file=file)

    def is_exempt(self, logical_id: str, path: str) -> bool:
        if any(p.search(logical_id) for p in self.exempt_ids):
            return True
        if any(p.search(path) for p in self.exempt_paths):
            return True
        return any(p.search(path) or p.search(logical_id) for p in self.exempt_user)

    # -- CDK templates -------------------------------------------------------------

    def check_cdk_out(self, cdk_out: Path) -> int:
        templates = sorted(cdk_out.rglob("*.template.json"))
        for t in templates:
            self.check_template(t)
        return len(templates)

    def check_template(self, path: Path) -> None:
        doc = json.loads(path.read_text())
        stack = path.name.removesuffix(".template.json")
        file = self.rel(path)

        for name, param in (doc.get("Parameters") or {}).items():
            default = param.get("Default")
            if "AWS::EC2::Image::Id" in str(param.get("Type", "")) and isinstance(default, str) \
                    and re.search(r"x86_64|amd64", default):
                self.add("error", f"{stack}: {name}", f"AMI parameter {default} is an x86_64 image; "
                         "use the arm64 one (and a Graviton instance type)", file)

        for lid, res in (doc.get("Resources") or {}).items():
            rtype = res.get("Type", "")
            props = res.get("Properties") or {}
            cdk_path = (res.get("Metadata") or {}).get("aws:cdk:path", "")
            where = f"{stack}: {cdk_path or lid}"

            if rtype in ("AWS::Lambda::Function", "AWS::Lambda::LayerVersion") \
                    and self.is_exempt(lid, cdk_path):
                self.add("exempt", where, "CDK-internal or exempted function, not checked", file)
                continue

            if rtype == "AWS::Lambda::Function":
                arch = props.get("Architectures") or ["x86_64"]
                if "arm64" not in arch:
                    self.add("error", where, "Lambda runs on x86_64; set "
                             "architecture=Architecture.ARM_64", file)
                self.runtime(props.get("Runtime"), where, "Lambda runtime", file)

            elif rtype == "AWS::Lambda::LayerVersion":
                archs = props.get("CompatibleArchitectures")
                if not archs:
                    self.add("warning", where, "layer declares no CompatibleArchitectures; set "
                             "[ARM_64] and build its packages for arm64", file)
                elif "arm64" not in archs:
                    self.add("error", where, "layer is built for x86_64 only", file)
                for rt in props.get("CompatibleRuntimes") or []:
                    self.runtime(rt, where, "layer runtime", file)

            elif rtype in ("AWS::EC2::Instance", "AWS::AutoScaling::LaunchConfiguration"):
                self.instance_type(props.get("InstanceType"), where, file)

            elif rtype == "AWS::EC2::LaunchTemplate":
                self.instance_type((props.get("LaunchTemplateData") or {}).get("InstanceType"),
                                   where, file)

            elif rtype == "AWS::ECS::TaskDefinition" and \
                    "FARGATE" in (props.get("RequiresCompatibilities") or []):
                cpu = (props.get("RuntimePlatform") or {}).get("CpuArchitecture", "X86_64")
                if cpu != "ARM64":
                    self.add("error", where, "Fargate task runs on X86_64; set runtimePlatform "
                             "cpuArchitecture ARM64", file)

    def instance_type(self, itype: object, where: str, file: str) -> None:
        if isinstance(itype, str) and not GRAVITON.match(itype):
            self.add("error", where, f"instance type {itype} is not Graviton; use the arm64 "
                     "family (t3 -> t4g, m7i -> m7g, ...)", file)

    # -- toolchain files -----------------------------------------------------------

    def walk(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                yield Path(dirpath) / f

    def check_toolchain(self) -> None:
        for path in self.walk():
            name = path.name
            try:
                if name == ".python-version":
                    self.toolchain_file(path, "python")
                elif name in (".nvmrc", ".node-version"):
                    self.toolchain_file(path, "node")
                elif name == "pyproject.toml":
                    self.pyproject(path)
                elif name == "package.json":
                    self.package_json(path)
                elif name.startswith("Dockerfile"):
                    self.dockerfile(path)
                elif path.suffix in (".yml", ".yaml") and \
                        self.rel(path).startswith(".github/workflows/"):
                    self.workflow(path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue

    def toolchain_file(self, path: Path, lang: str) -> None:
        text = path.read_text().strip()
        if lang == "node" and (text in NODE_ALIASES or text.startswith("lts/")):
            return  # an alias tracks the newest Node, which is fine for a toolchain
        v = parse_version(text)
        if v:
            self.judge(lang, v, self.rel(path), path.name, newer_ok=(lang == "node"),
                       file=self.rel(path), line=1)

    def pyproject(self, path: Path) -> None:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if m := re.match(r'\s*requires-python\s*=\s*["\']([^"\']*)', line):
                if lo := re.search(r">=\s*(\d+\.\d+)", m[1]):
                    self.judge("python", parse_version(lo[1]), self.rel(path),
                               "requires-python lower bound", newer_ok=False,
                               file=self.rel(path), line=i)
            elif m := re.match(r'\s*target-version\s*=\s*["\']py(\d)(\d+)["\']', line):
                self.judge("python", (int(m[1]), int(m[2])), self.rel(path),
                           "ruff/black target-version", newer_ok=False,
                           file=self.rel(path), line=i)

    def package_json(self, path: Path) -> None:
        node = (json.loads(path.read_text()).get("engines") or {}).get("node")
        if isinstance(node, str) and (lo := re.search(r"(?:>=|\^|~)?\s*(\d+)", node)):
            line = next((i for i, l in enumerate(path.read_text().splitlines(), 1)
                         if '"node"' in l), 0)
            self.judge("node", parse_version(lo[1]), self.rel(path), "engines.node",
                       newer_ok=True, file=self.rel(path), line=line)

    def workflow(self, path: Path) -> None:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            s = line.strip()
            if s.startswith("#") or "${{" in s:
                continue
            if m := re.match(r'python-version:\s*["\']?(\d+\.\d+)', s):
                self.judge("python", parse_version(m[1]), self.rel(path), "CI python-version",
                           newer_ok=False, file=self.rel(path), line=i)
            elif m := re.match(r'node-version:\s*["\']?(\d+)', s):
                self.judge("node", parse_version(m[1]), self.rel(path), "CI node-version",
                           newer_ok=True, file=self.rel(path), line=i)

    def dockerfile(self, path: Path) -> None:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            s = line.strip()
            if not s.upper().startswith("FROM "):
                continue
            if re.search(r"--platform=\S*(amd64|x86_64)", s):
                self.add("error", self.rel(path), "image is pinned to amd64; build for linux/arm64",
                         self.rel(path), i)
            m = re.search(r"(?:^|\s|/)(python|nodejs|node):(\d+(?:\.\d+)?)", s)
            if m:
                lang = "python" if m[1] == "python" else "node"
                self.judge(lang, parse_version(m[2]), self.rel(path), f"base image {m[1]}:{m[2]}",
                           newer_ok=(lang == "node"), file=self.rel(path), line=i)


# -- reporting ---------------------------------------------------------------------

def report(findings: list[Finding], templates: int, standards: dict) -> None:
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    shown = [f for f in findings if f.level != "exempt"]
    for f in shown:
        if gha:
            loc = f"file={f.file}" + (f",line={f.line}" if f.line else "") if f.file else ""
            msg = f"{f.where}: {f.message}".replace("%", "%25").replace("\n", "%0A")
            print(f"::{f.level} {loc}::{msg}")
        else:
            print(f"{f.level.upper():8} {f.where}: {f.message}")

    errors = sum(f.level == "error" for f in findings)
    warnings = sum(f.level == "warning" for f in findings)
    exempt = sum(f.level == "exempt" for f in findings)
    py, node = standards["python"], standards["node"]
    head = (f"Runtime guard: {errors} error(s), {warnings} warning(s); {templates} template(s) "
            f"checked, {exempt} CDK-internal resource(s) exempt. Standard: Python {py['latest']} "
            f"(min {py['minimum']}), Node {node['latest']} (min {node['minimum']}), "
            f"{standards['architecture']}.")
    print(head)

    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as out:
            out.write(f"### Runtime guard\n\n{head}\n\n")
            if shown:
                out.write("| Level | Where | Finding |\n|---|---|---|\n")
                for f in shown:
                    out.write(f"| {f.level} | `{f.where}` | {f.message} |\n")
            out.write("\nThe standard lives in [tivtav/.github/runtime-standards.json]"
                      "(https://github.com/tivtav/.github/blob/main/runtime-standards.json).\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--standards", type=Path, required=True)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--cdk-out", action="append", default=[],
                    help="cdk.out directory (repeatable); default: every cdk.out under --root")
    ap.add_argument("--exempt", action="append", default=[],
                    help="regex on a resource's logical id or aws:cdk:path to skip (repeatable)")
    ap.add_argument("--no-toolchain", action="store_true")
    ap.add_argument("--fail-on-warning", action="store_true")
    args = ap.parse_args(argv)

    standards = json.loads(args.standards.read_text())
    checker = Checker(standards, args.exempt, args.root)

    cdk_outs = [Path(p) for p in args.cdk_out if p.strip()]
    if not cdk_outs:
        cdk_outs = [p for p in args.root.rglob("cdk.out")
                    if p.is_dir() and "node_modules" not in p.parts]
    templates = 0
    for d in cdk_outs:
        if d.is_dir():
            templates += checker.check_cdk_out(d)
        else:
            checker.add("warning", str(d), "no cdk.out here; run cdk synth before this step")

    if not args.no_toolchain:
        checker.check_toolchain()

    report(checker.findings, templates, standards)
    levels = {f.level for f in checker.findings}
    return 1 if "error" in levels or (args.fail_on_warning and "warning" in levels) else 0


if __name__ == "__main__":
    sys.exit(main())
