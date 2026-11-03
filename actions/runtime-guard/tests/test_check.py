import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import check  # noqa: E402

STANDARDS = json.loads((HERE.parents[2] / "runtime-standards.json").read_text())
STANDARDS = {**STANDARDS, "python": {"latest": "3.14", "minimum": "3.13"},
             "node": {"latest": "24", "minimum": "22"}}


def fn(runtime="python3.14", arch=("arm64",), path="Stack/Fn/Resource", **extra):
    props = {"Runtime": runtime, **extra}
    if arch is not None:
        props["Architectures"] = list(arch)
    return {"Type": "AWS::Lambda::Function", "Properties": props,
            "Metadata": {"aws:cdk:path": path}}


class Repo:
    """A temporary repository with a cdk.out and arbitrary files."""

    def __init__(self, resources=None, parameters=None, files=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        out = self.root / "cdk.out"
        out.mkdir()
        doc = {"Resources": resources or {}}
        if parameters:
            doc["Parameters"] = parameters
        (out / "Stack.template.json").write_text(json.dumps(doc))
        for name, text in (files or {}).items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def run(self, *exempt):
        c = check.Checker(STANDARDS, list(exempt), self.root)
        c.check_cdk_out(self.root / "cdk.out")
        c.check_toolchain()
        self.tmp.cleanup()
        return [(f.level, f.message) for f in c.findings]


def levels(findings):
    return sorted(level for level, _ in findings)


class LambdaTests(unittest.TestCase):
    def test_compliant_function_is_clean(self):
        self.assertEqual(Repo({"Fn": fn()}).run(), [])

    def test_missing_architecture_means_x86(self):
        out = Repo({"Fn": fn(arch=None)}).run()
        self.assertEqual(levels(out), ["error"])
        self.assertIn("x86_64", out[0][1])

    def test_runtime_below_minimum_is_error_below_latest_is_warning(self):
        self.assertEqual(levels(Repo({"Fn": fn("python3.12")}).run()), ["error"])
        self.assertEqual(levels(Repo({"Fn": fn("python3.13")}).run()), ["warning"])
        self.assertEqual(levels(Repo({"Fn": fn("nodejs20.x")}).run()), ["error"])
        self.assertEqual(levels(Repo({"Fn": fn("nodejs22.x")}).run()), ["warning"])
        self.assertEqual(Repo({"Fn": fn("nodejs24.x")}).run(), [])

    def test_preview_runtime_newer_than_latest_warns(self):
        out = Repo({"Fn": fn("python3.15")}).run()
        self.assertEqual(levels(out), ["warning"])
        self.assertIn("preview", out[0][1])

    def test_other_runtimes_only_get_the_architecture_check(self):
        self.assertEqual(Repo({"Fn": fn("provided.al2023")}).run(), [])

    def test_cdk_internal_functions_are_exempt(self):
        res = {
            "LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8aFD4BFC8A": fn("nodejs22.x", None),
            "MigrationsProviderframeworkonEvent33437111":
                fn("nodejs22.x", None, path="Stack/MigrationsProvider/framework-onEvent/Resource"),
            "CustomS3AutoDeleteObjectsCustomResourceProviderHandler9D90184F": fn("nodejs22.x", None),
            "AWS679f53fac002430cb0da5b7982bd2287A1F4F3C9": fn("nodejs22.x", None),
        }
        self.assertEqual(levels(Repo(res).run()), ["exempt"] * 4)

    def test_user_exemption_matches_the_cdk_path(self):
        res = {"Ns": fn("nodejs22.x", None, path="Stack/Dsf/RedshiftNamespace/Handler")}
        self.assertEqual(levels(Repo(res).run("RedshiftNamespace")), ["exempt"])


class LayerTests(unittest.TestCase):
    def layer(self, **props):
        return {"L": {"Type": "AWS::Lambda::LayerVersion", "Properties": props}}

    def test_layer_checks(self):
        ok = self.layer(CompatibleRuntimes=["python3.14"], CompatibleArchitectures=["arm64"])
        self.assertEqual(Repo(ok).run(), [])
        self.assertEqual(levels(Repo(self.layer(CompatibleRuntimes=["python3.14"])).run()),
                         ["warning"])
        self.assertEqual(levels(Repo(self.layer(CompatibleArchitectures=["x86_64"])).run()),
                         ["error"])


class ComputeTests(unittest.TestCase):
    def test_instance_types(self):
        for itype, expected in [("t4g.nano", []), ("m7gd.large", []), ("c7gn.xlarge", []),
                                ("g5g.2xlarge", []), ("t3.nano", ["error"]),
                                ("m7i-flex.large", ["error"]), ("g5.xlarge", ["error"])]:
            res = {"I": {"Type": "AWS::EC2::Instance", "Properties": {"InstanceType": itype}}}
            self.assertEqual(levels(Repo(res).run()), expected, itype)

    def test_launch_template_instance_type(self):
        res = {"LT": {"Type": "AWS::EC2::LaunchTemplate",
                      "Properties": {"LaunchTemplateData": {"InstanceType": "t3.micro"}}}}
        self.assertEqual(levels(Repo(res).run()), ["error"])

    def test_x86_ami_parameter(self):
        params = {"Ami": {"Type": "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
                          "Default": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"}}
        self.assertEqual(levels(Repo(parameters=params).run()), ["error"])
        params["Ami"]["Default"] = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-arm64"
        self.assertEqual(Repo(parameters=params).run(), [])

    def test_fargate_needs_arm64(self):
        td = {"Type": "AWS::ECS::TaskDefinition", "Properties": {"RequiresCompatibilities": ["FARGATE"]}}
        self.assertEqual(levels(Repo({"T": td}).run()), ["error"])
        td["Properties"]["RuntimePlatform"] = {"CpuArchitecture": "ARM64"}
        self.assertEqual(Repo({"T": td}).run(), [])


class ToolchainTests(unittest.TestCase):
    def test_version_files(self):
        self.assertEqual(levels(Repo(files={".python-version": "3.12\n"}).run()), ["error"])
        self.assertEqual(levels(Repo(files={".python-version": "3.13.0\n"}).run()), ["warning"])
        self.assertEqual(Repo(files={".python-version": "3.14\n"}).run(), [])
        self.assertEqual(Repo(files={".nvmrc": "lts/*\n"}).run(), [])
        self.assertEqual(Repo(files={".nvmrc": "26\n"}).run(), [])  # newer Node in CI is fine
        self.assertEqual(levels(Repo(files={".nvmrc": "v20.11.0\n"}).run()), ["error"])

    def test_pyproject(self):
        text = 'requires-python = ">=3.13"\n[tool.ruff]\ntarget-version = "py312"\n'
        self.assertEqual(levels(Repo(files={"pyproject.toml": text}).run()), ["error", "warning"])

    def test_package_json_engines(self):
        self.assertEqual(levels(Repo(files={"package.json": '{"engines": {"node": ">=20"}}'}).run()),
                         ["error"])
        self.assertEqual(Repo(files={"package.json": '{"engines": {"node": ">=24"}}'}).run(), [])

    def test_workflows(self):
        wf = ("jobs:\n  t:\n    steps:\n      - uses: actions/setup-python@v5\n"
              "        with:\n          python-version: \"3.12\"\n"
              "      - uses: actions/setup-node@v4\n        with:\n          node-version: '20'\n"
              "      # node-version: '18'\n"
              "      - with:\n          node-version: ${{ env.NODE_VERSION }}\n")
        out = Repo(files={".github/workflows/ci.yml": wf}).run()
        self.assertEqual(levels(out), ["error", "error"])

    def test_dockerfiles(self):
        files = {"Dockerfile": "FROM public.ecr.aws/lambda/python:3.14\n",
                 "svc/Dockerfile.dev": "FROM --platform=linux/amd64 node:20-slim\n"}
        self.assertEqual(levels(Repo(files=files).run()), ["error", "error"])

    def test_node_modules_is_skipped(self):
        files = {"node_modules/x/package.json": '{"engines": {"node": ">=12"}}'}
        self.assertEqual(Repo(files=files).run(), [])


class MainTests(unittest.TestCase):
    def test_exit_code_and_missing_cdk_out(self):
        with tempfile.TemporaryDirectory() as d:
            std = Path(d) / "s.json"
            std.write_text(json.dumps(STANDARDS))
            (Path(d) / ".python-version").write_text("3.13")
            self.assertEqual(check.main(["--standards", str(std), "--root", d]), 0)
            self.assertEqual(check.main(["--standards", str(std), "--root", d,
                                         "--fail-on-warning"]), 1)
            (Path(d) / ".python-version").write_text("3.11")
            self.assertEqual(check.main(["--standards", str(std), "--root", d]), 1)


if __name__ == "__main__":
    unittest.main()
