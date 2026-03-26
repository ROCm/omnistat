# ---------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2026 Advanced Micro Devices, Inc. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.
# ---------------------------------------------------------------------------
"""Run HIP test workloads from the test/workloads/ directory."""

import os
import subprocess

WORKLOADS_DIR = os.path.dirname(os.path.abspath(__file__))


def run(name, args=None, env=None):
    """Run a previously built HIP test workload.

    Args:
        name: Workload name (target name, without extension).
        args: List of command-line arguments to pass to the sample.
        env: Optional environment dict, merged on top of the current
             environment.

    Returns:
        subprocess.CompletedProcess

    Raises:
        FileNotFoundError: If the binary does not exist (run build() first).
    """
    binary = os.path.join(WORKLOADS_DIR, name)
    if not os.path.exists(binary):
        raise FileNotFoundError(f"Binary not found: {binary} (run build() first)")

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    cmd = [binary] + [str(a) for a in (args or [])]
    return subprocess.run(cmd, capture_output=True, text=True, env=run_env)
