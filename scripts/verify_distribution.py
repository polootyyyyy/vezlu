"""Build a wheel and check its installed interfaces outside the checkout."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    with tempfile.TemporaryDirectory(prefix="vezlu-check-") as temporary:
        work = Path(temporary).resolve()
        if work.is_relative_to(repository):
            raise RuntimeError("Choose a temporary directory outside the checkout")

        def run(*command, capture_output=False):
            print("+", *map(str, command), flush=True)
            return subprocess.run(list(map(str, command)), cwd=work, env=environment,
                                  check=True, text=True, capture_output=capture_output)

        # Build from a temporary source copy so build artifacts are cleaned up too.
        source = work / "source"
        source.mkdir()
        for name in ("pyproject.toml", "README.md", "LICENSE", "vezlu.py"):
            shutil.copy2(repository / name, source / name)

        build_environment = work / "build-env"
        install_environment = work / "install-env"
        for target in (build_environment, install_environment):
            run(sys.executable, "-I", "-m", "venv", target)
        build_python = build_environment / "bin" / "python"
        installed_python = install_environment / "bin" / "python"
        wheels = work / "wheels"
        run(build_python, "-I", "-m", "pip", "wheel", "--no-deps",
            "--wheel-dir", wheels, source)
        built_wheels = list(wheels.glob("*.whl"))
        if len(built_wheels) != 1:
            raise RuntimeError(f"Expected exactly one wheel, found {len(built_wheels)}")
        run(installed_python, "-I", "-m", "pip", "install", built_wheels[0])

        suite = work / "tests"
        suite.mkdir()
        copied_test = suite / "test_vezlu.py"
        shutil.copy2(repository / "test_vezlu.py", copied_test)
        provenance = run(installed_python, "-I", "-c",
            "from pathlib import Path; import sys, vezlu; "
            "module = Path(vezlu.__file__).resolve(); "
            "expected = Path(sys.argv[1]).resolve(); "
            "assert Path(sys.prefix).resolve() == expected, sys.prefix; "
            "assert module.is_relative_to(expected), module; print(module)",
            install_environment, capture_output=True)
        print("Installed module:", provenance.stdout.strip(), flush=True)
        run(installed_python, "-I", copied_test, "-v")

        console = install_environment / "bin" / "vezlu"
        help_result = run(console, "--help", capture_output=True)
        if "usage:" not in help_result.stdout:
            raise RuntimeError("Installed console help did not show usage")
        demo_result = run(console, "--store", work / "demo-store", "demo",
                          capture_output=True)
        if json.loads(demo_result.stdout).get("text_source") != "fixtures":
            raise RuntimeError("Installed console demo did not identify fixture text")
        print("Installed console help and fixture demo passed.", flush=True)


if __name__ == "__main__":
    main()
