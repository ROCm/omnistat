#!/usr/bin/env python3
# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -------------------------------------------------------------------------------

"""annotate.py

Standalone utility for creating user annotation labels in json format. Intended
for use in conjunction with companion Slurm data collector that looks for files of the
following form:

/tmp/omnistat_${USER}_annotate.json

File can also be imported for direct Python usage.
"""

import argparse
import json
import os
import time


class omnistat_annotate:
    def __init__(self):
        self.filename = "/tmp/omnistat_" + os.environ.get("USER") + "_annotate.json"

    def start(self, label):
        data = {}
        data["annotation"] = label
        data["timestamp_secs"] = int(time.time())

        with open(self.filename, "w") as outfile:
            outfile.write(json.dumps(data, indent=4))
            outfile.write("\n")
        return

    def stop(self):
        if os.path.exists(self.filename):
            os.remove(self.filename)
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["start", "stop"], help="annotation mode", required=True)
    parser.add_argument("--text", help="desired annotation", required=False)
    args = parser.parse_args()

    if args.mode == "start" and args.text is None:
        parser.error('The --text option is required for "start" mode.')

    annotate = omnistat_annotate()

    if args.mode == "start":
        annotate.start(args.text)
    else:
        annotate.stop()


if __name__ == "__main__":
    main()
