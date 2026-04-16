"""Output formatting helpers for omnistat-inspect."""

import json
import os
from datetime import datetime


def _output_json(data):
    """Format output as JSON."""
    print(json.dumps(data, indent=2, default=str))


def _write_scratch(scratch_dir, filename, data):
    """Write data to a scratch directory file and return the path."""
    os.makedirs(scratch_dir, exist_ok=True)
    filepath = os.path.join(scratch_dir, filename)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return filepath


def _append_query_log(scratch_dir, query_stats):
    """Append query stats to cumulative query log in scratch dir."""
    if not scratch_dir:
        return
    os.makedirs(scratch_dir, exist_ok=True)
    log_path = os.path.join(scratch_dir, "query_log.json")
    existing = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = []
    existing.append(
        {
            "timestamp": datetime.now().isoformat(),
            "stats": query_stats,
        }
    )
    with open(log_path, "w") as f:
        json.dump(existing, f, indent=2, default=str)
