"""omnistat-inspect: Agentic-first CLI tool for HPC job analysis.

Provides structured JSON output for hypothesis-driven GPU telemetry analysis.
Designed for use with the analyze-job SKILL in agentic workflows.
"""

from omnistat.inspect.inspector import JobInspector, QueryLedger
from omnistat.inspect.cli import build_parser, main
