"""omnistat-inspect: Agentic-first CLI tool for HPC job analysis.

Provides structured JSON output for hypothesis-driven GPU telemetry analysis.
Designed for use with the analyze-job SKILL in agentic workflows.
"""

from omnistat.inspect._analyzer import AnalyzeJob, QueryLedger
from omnistat.inspect._cli import build_parser, main
