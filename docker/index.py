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
#
# omnistat-index -- Scan Omnistat database and serve list of jobs
#
# Scan an Omnistat database available at a given URL with bounded accuracy.
# Using a single PromQL query over very long time ranges (>=1m) requires a very
# coarse sampling granularity, leading to potentially inaccurate results in Grafana
# panels. This script generates many PromQL queries over short time ranges (~1d),
# allowing much more fine-grained sampling.
#
# This approach is helpful to guarantee highly accurate job information and
# timing in the local Grafana dashboards.
#
# After scanning the database, results will be served over HTTP in JSON format
# so they can be consumed by the Infinity data source in Grafana.

import argparse
import asyncio
import http.server
import json
import sys
import time

import aiohttp


class JobIndexHandler(http.server.BaseHTTPRequestHandler):
    """
    Handler to serve job index data in JSON format using Python's HTTP server

    Attributes:
        data (list): List of dictionaries with job index data. Each dictionary
        in the list includes jobid, start, stop, and duration.
    """

    def __init__(self, data):
        self.data = data

    def __call__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.data).encode("utf-8"))

    def log_request(self, code="-", size="-"):
        return


async def scan_query(session, address, start, end, step, timeout):
    """
    Asynchronous PromQL query to scan jobs in the given database.

    Args:
        session: aiohttp client session.
        address (str): Address of the Prometheus server.
        start (int): Start timestamp.
        end (int): Stop timestamp.
        step (int): Query resolution in seconds.
        timeout (float): Request timeout in seconds.

    Returns:
        list: list of tuples containing job ID, first timestamp, last
        timestamp, and number of nodes in the given range.
    """
    url = f"http://{address}/api/v1/query_range"
    params = {
        "query": "sum by (jobid) (rmsjob_info{})",
        "start": start,
        "end": end,
        "step": step,
    }

    result = []

    try:
        async with session.get(url, params=params, timeout=timeout) as response:
            response.raise_for_status()
            response = await response.json()
            for job in response["data"]["result"]:
                job_id = job["metric"]["jobid"]
                first = job["values"][0][0]
                last = job["values"][-1][0]
                num_samples = len(job["values"])
                num_nodes = job["values"][num_samples // 2][1]
                result.append((job_id, first, last, num_nodes))
            return result
    except Exception as e:
        print(f"Unexpected error: {e}")
        return None


async def scan_database(address, days, step, limit, timeout):
    """
    Generate many asynchronous queries to identify jobs and their start/end
    times in a database.

    Args:
        address (str): Address of the Prometheus server.
        days (int): Number of days to scan (range [now-days, now]).
        step (int): Query resolution in seconds.
        limit (int): Number of concurrent requests.
        timeout (float): Request timeout in seconds.

    Returns:
        list: list of scan results.
    """
    scan_start = time.time() - (days * 24 * 60 * 60)
    ranges = []

    for i in range(days):
        day_start = scan_start + i * 24 * 60 * 60
        day_end = day_start + 24 * 60 * 60
        ranges.append((int(day_start), int(day_end)))

    connector = aiohttp.TCPConnector(limit=limit)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for start, end in ranges:
            task = scan_query(session, address, start, end, step, timeout)
            tasks.append(task)
        results = await asyncio.gather(*tasks)
        return results


parser = argparse.ArgumentParser()
parser.add_argument("--address", type=str, default="localhost:9090")
parser.add_argument("--days", type=int, default=365)
parser.add_argument("--step", type=int, default=30)
parser.add_argument("--limit", type=int, default=16)
parser.add_argument("--timeout", type=float, default=5)
args = parser.parse_args()

start_time = time.time()
results = asyncio.run(scan_database(args.address, args.days, args.step, args.limit, args.timeout))
end_time = time.time()

num_queries = args.days
num_fail = results.count(None)
num_success = num_queries - num_fail

# Flatten results from different requests, which may contain information
# about multiple jobs.
results = [x for result in results if result != None for x in result]

# Index results by job ID, combining results from different requests.
job_index = {}
for job_id, first, last, num_nodes in results:
    if job_id in job_index:
        first = min(first, job_index[job_id][0])
        last = max(last, job_index[job_id][1])
        num_nodes = max(num_nodes, job_index[job_id][2])
    job_index[job_id] = (first, last, num_nodes)

# Tranform data to be used in Grafana; timestamps are expected to be
# milliseconds. Lowercase keys are hidden from the table and only used
# internally.
job_data = []
for job_id, (start, end, num_nodes) in job_index.items():
    job_data.append(
        {
            "Job ID": job_id,
            "Number of nodes": num_nodes,
            "Date": start * 1000,
            "Duration": end - start,
            "from": (start - args.step) * 1000,
            "to": (end + args.step) * 1000,
        }
    )

print(f"Scanned database in {end_time - start_time:.2f} seconds")
print(f".. Number of jobs in the last {args.days} days: {len(job_data)}")
if num_fail > 0:
    print(f".. Warning: {num_fail} out of {num_queries} queries failed")
sys.stdout.flush()

server_address = ("", 9091)
handler = JobIndexHandler(job_data)
httpd = http.server.HTTPServer(server_address, handler)
httpd.serve_forever()
