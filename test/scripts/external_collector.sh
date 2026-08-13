#!/usr/bin/env bash
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
#
# Stateful metric generator for external collector tests.
#
# Usage:
#   external_collector.sh -init <statefile>    # reset state (counter=0)
#   external_collector.sh <statefile>          # emit metrics for current run, then increment
#   external_collector.sh                      # emit default metric (no state tracking)

STATEFILE="$1"

# Handle -help: print usage
if [ "$1" = "-h" ] || [ "$1" = "-help" ] || [ "$1" = "--help" ]; then
    echo "Usage:"
    echo "  $(basename "$0") -init <statefile>   Reset state (counter=0)"
    echo "  $(basename "$0") <statefile>          Emit metrics for current run, then increment"
    echo "  $(basename "$0")                      Emit default metric (no state tracking)"
    exit 0
fi

# Handle -init: reset counter to 0
if [ "$1" = "-init" ]; then
    echo 0 > "$2"
    exit 0
fi

# No argument: emit a static default metric
if [ -z "$STATEFILE" ]; then
    echo 'my_snazzy_metric{my_snazzy_label="omnistat_for_the_win"} 42'
    exit 0
fi

# Read current run counter (default to 0 if missing)
RUN=0
if [ -f "$STATEFILE" ]; then
    RUN=$(cat "$STATEFILE")
fi

# Emit metrics based on run number
# Every run includes comment lines, a blank line, a garbage line, and a label-less
# metric to exercise parser edge cases in collector_external.py.
case $RUN in
    0)
        echo '# this is a comment'
        echo ''
        echo 'this is garbage and should be skipped'
        echo 'my_snazzy_metric{my_snazzy_label="omnistat_for_the_win"} 42'
        echo 'my_nolabel_metric 99'
        ;;
    1)
        echo '# another comment'
        echo 'my_snazzy_metric2{my_snazzy_label2="rocks"} 43'
        echo 'my_nolabel_metric 100'
        ;;
    2)
        echo 'my_snazzy_metric3{my_snazzy_label3="ftw"} 44'
        echo 'my_snazzy_metric3b{my_snazzy_label3b="bonus"} 45'
        echo 'my_nolabel_metric 101'
        ;;
    3)
        # Non-zero exit with valid output — collector should still record metrics
        echo 'my_snazzy_metric4{my_snazzy_label4="nonzero"} 46' >&1
        echo 'script had a partial failure' >&2
        echo $((RUN + 1)) > "$STATEFILE"
        exit 1
        ;;
    *)
        echo 'my_snazzy_metric{my_snazzy_label="omnistat_for_the_win"} 42'
        echo 'my_nolabel_metric 99'
        ;;
esac

# Increment counter
echo $((RUN + 1)) > "$STATEFILE"
