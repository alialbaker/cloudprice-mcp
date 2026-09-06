"""Build the deployment zip for the AgentCore Gateway Lambda.

Run:  python gateway_lambda/build.py

Produces gateway_lambda/dist/cloudprice-gateway.zip containing:

  handler.py        the Gateway entry point
  cloudprice_mcp/   the engine, including ~1.8 MB of bundled price JSON
  yaml/             PyYAML, the one third-party import the engine has

Deliberately NOT included: the MCP SDK and the starlette/uvicorn stack under
it. Gateway speaks MCP to clients; this function only ever answers Gateway,
so it imports `cloudprice_mcp.dispatch`, which has no MCP dependency.

PyYAML ships compiled wheels, so it is fetched for Lambda's platform rather
than this machine's — building on Windows would otherwise put a win_amd64
wheel in a Linux runtime.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BUILD = HERE / "build"
DIST = HERE / "dist"
ZIP_PATH = DIST / "cloudprice-gateway.zip"

# Match the Lambda runtime we deploy to, not the interpreter running this script.
LAMBDA_PYTHON = "3.12"
LAMBDA_PLATFORM = "manylinux2014_x86_64"


def python_with_pip() -> str:
    """Find an interpreter that actually has pip.

    `uv venv` does not install pip, so the project venv usually cannot run
    `-m pip`. Any interpreter will do here: the target directory and the
    explicit --platform/--python-version flags decide what gets installed,
    not the interpreter running the install.
    """
    candidates = [sys.executable, shutil.which("python"), shutil.which("python3")]
    for exe in candidates:
        if not exe:
            continue
        probe = subprocess.run([exe, "-m", "pip", "--version"],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            return exe
    raise SystemExit(
        "No interpreter with pip found. Install pip, or run this script with "
        "a system Python rather than a uv-created venv."
    )


def run(*args: str) -> None:
    print(f"  $ {' '.join(args[:6])}{' ...' if len(args) > 6 else ''}")
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        # Surface what pip actually said. Swallowing this turns a one-line
        # dependency error into a traceback that explains nothing.
        print(result.stdout.strip())
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"Command failed with exit {result.returncode}")


def main() -> int:
    print("1. Cleaning previous build")
    shutil.rmtree(BUILD, ignore_errors=True)
    DIST.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True)

    pip_python = python_with_pip()
    print(f"   using pip from: {pip_python}")

    print("2. Installing cloudprice_mcp (no dependencies)")
    # --no-deps keeps mcp out: it is declared in pyproject for the stdio
    # server, and pulling it here would drag in the whole web stack.
    run(pip_python, "-m", "pip", "install", "--quiet",
        "--target", str(BUILD), "--no-deps", str(REPO))

    print(f"3. Installing PyYAML for {LAMBDA_PLATFORM} / py{LAMBDA_PYTHON}")
    run(pip_python, "-m", "pip", "install", "--quiet",
        "--target", str(BUILD), "--no-deps",
        "--platform", LAMBDA_PLATFORM,
        "--python-version", LAMBDA_PYTHON,
        "--only-binary=:all:", "PyYAML")

    print("4. Adding handler.py at the zip root")
    # Lambda's handler is addressed as "handler.lambda_handler", so the module
    # must sit at the top level of the archive, not inside a package.
    shutil.copy2(HERE / "handler.py", BUILD / "handler.py")

    print("5. Removing build metadata")
    for pattern in ("*.dist-info", "*.egg-info", "__pycache__"):
        for path in BUILD.rglob(pattern):
            shutil.rmtree(path, ignore_errors=True)

    print("6. Writing the zip")
    ZIP_PATH.unlink(missing_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(BUILD.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(BUILD))

    size_mb = ZIP_PATH.stat().st_size / 1_048_576
    with zipfile.ZipFile(ZIP_PATH) as zf:
        names = zf.namelist()

    print()
    print(f"Built {ZIP_PATH}")
    print(f"  {size_mb:.2f} MB zipped, {len(names)} files")
    print(f"  handler.py at root: {'handler.py' in names}")
    print(f"  cloudprice_mcp:     {any(n.startswith('cloudprice_mcp/') for n in names)}")
    print(f"  price data files:   {sum('cloudprice_mcp/data/' in n for n in names)}")
    print(f"  yaml:               {any(n.startswith('yaml/') for n in names)}")
    print(f"  mcp NOT bundled:    {not any(n.startswith('mcp/') for n in names)}")
    print(f"  Lambda zip limit:   50 MB direct upload — {'OK' if size_mb < 50 else 'TOO BIG'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
