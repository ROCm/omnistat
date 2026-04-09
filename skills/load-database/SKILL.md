---
name: load-database
description: Load and explore an Omnistat database using VictoriaMetrics. Use this when the user wants to view, query, or visualize collected Omnistat telemetry data from a user-mode job.
allowedPrompts:
  - tool: Bash
    prompt: check for running victoriametrics
  - tool: Bash
    prompt: run docker commands
  - tool: Bash
    prompt: start victoriametrics
  - tool: Bash
    prompt: stop victoriametrics
  - tool: Bash
    prompt: manage docker containers
  - tool: Bash
    prompt: check port availability
  - tool: Bash
    prompt: run victoriametrics binary
  - tool: Bash
    prompt: list directory contents
  - tool: Bash
    prompt: find database directories
  - tool: Bash
    prompt: check if path exists
  - tool: Bash
    prompt: explore filesystem for databases
  - tool: Bash
    prompt: verify victoriametrics is running
  - tool: Bash
    prompt: download files with curl or wget
---

# Load Omnistat Database

This skill provides instructions for loading an Omnistat database using VictoriaMetrics, which allows you to query and explore collected GPU telemetry data.

## Agentic Workflow

When helping a user load a database, follow these steps:

1. **Identify the database path**: Ask the user for the path, or help them find it by exploring the filesystem
2. **Validate the database structure**: Check that the path contains the expected subdirectories (`data/`, `cache/`, `indexdb/`, etc.)
3. **Check for running VictoriaMetrics instances**: Detect and stop any existing instances to avoid port conflicts
4. **Check port availability**: If port 8428 is in use by another process, use port 8429 (or another available port)
5. **Download VictoriaMetrics** (if not already available): Get the latest binary release
6. **Start VictoriaMetrics**: Launch with the correct parameters, especially `-retentionPeriod=100y`
7. **Verify the database loaded**: Confirm VictoriaMetrics is running and can return data
8. **Inform the user**: Let them know the database is ready for queries at the appropriate URL (e.g., `http://localhost:8428` or the alternative port)

## Database Structure

An Omnistat database collected in user-mode has the following structure:
```
database_dir/
├── cache/
├── data/
├── flock.lock
├── indexdb/
├── metadata/
├── snapshots/
└── tmp/
```

The `data/` subdirectory contains the VictoriaMetrics time-series data.

**Important:** When starting VictoriaMetrics, you should pass the **root database directory** (the directory containing `data/`, `cache/`, etc.) to the `-storageDataPath` parameter, **NOT** the `data/` subdirectory itself. VictoriaMetrics will automatically look for the data in the correct subdirectories.

## Checking for Existing VictoriaMetrics Instances

**IMPORTANT:** Before starting VictoriaMetrics, check if an instance is already running:

```bash
# Check for native VictoriaMetrics processes
ps aux | grep victoria-metrics-prod | grep -v grep

# Check for Docker containers
docker ps -a --filter "ancestor=victoriametrics/victoria-metrics" --format "table {{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Ports}}"

# Check if port 8428 is in use
lsof -i :8428
```

If an instance is running, stop it before proceeding:
- **Native binary**: `pkill -f victoria-metrics-prod` or `kill $(cat victoria.pid)`
- **Docker**: `docker stop victoria-metrics && docker rm victoria-metrics`

**Note:** Never use `sudo kill` on Docker containers - always use `docker stop`.

### Port Selection

If port 8428 is in use by another process (not VictoriaMetrics), use an alternative port like 8429:

```bash
# Check what's using port 8428
lsof -i :8428

# If it's not VictoriaMetrics and you can't stop it, use port 8429 instead
# (adjust the -httpListenAddr parameter when starting VictoriaMetrics)
```

## Option 1: Using VictoriaMetrics Binary (Recommended)

This is the recommended approach for most users - it provides direct control and better performance without Docker overhead.

### Download VictoriaMetrics

Download the latest VictoriaMetrics release automatically:

```bash
# Get the latest version dynamically
LATEST_VERSION=$(curl -s https://api.github.com/repos/VictoriaMetrics/VictoriaMetrics/releases/latest | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')

# Download for Linux AMD64
wget https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/${LATEST_VERSION}/victoria-metrics-linux-amd64-${LATEST_VERSION}.tar.gz

# Extract
tar xzf victoria-metrics-linux-amd64-${LATEST_VERSION}.tar.gz

# Verify the binary exists
if [ ! -f victoria-metrics-prod ]; then
    echo "ERROR: Download or extraction failed. Check your internet connection."
    exit 1
fi

# Make executable
chmod +x victoria-metrics-prod
```

Alternatively, check the [VictoriaMetrics releases page](https://github.com/VictoriaMetrics/VictoriaMetrics/releases) to download a specific version manually.

### Start VictoriaMetrics

```bash
./victoria-metrics-prod \
  -storageDataPath=/path/to/omnistat/database \
  -httpListenAddr=0.0.0.0:8428 \
  -retentionPeriod=100y \
  -search.disableCache \
  -search.latencyOffset=0 \
  -search.maxPointsPerTimeseries=90000
```

**Important:** Replace `/path/to/omnistat/database` with the actual path to your Omnistat database's **root directory** (the directory containing `data/`, `cache/`, etc.), **NOT** the `data/` subdirectory itself.

The binary will run in the foreground. To run in the background:

```bash
nohup ./victoria-metrics-prod \
  -storageDataPath=/path/to/omnistat/database \
  -httpListenAddr=0.0.0.0:8428 \
  -retentionPeriod=100y \
  -search.disableCache \
  -search.latencyOffset=0 \
  -search.maxPointsPerTimeseries=90000 \
  > victoria.log 2>&1 &

# Save the process ID
echo $! > victoria.pid
```

### Key Parameters Explained

- `-storageDataPath=/path/to/database`: Points to the database **root directory** - VictoriaMetrics will automatically find the data in subdirectories
- `-httpListenAddr=0.0.0.0:8428`: Exposes the query API on port 8428
- `-retentionPeriod=100y`: **CRITICAL** - Keeps data for 100 years to prevent automatic deletion of historical data (default is only 1 month)
- `-search.disableCache`: Ensures fresh data reads (useful for exploring)
- `-search.latencyOffset=0`: No artificial latency for queries
- `-search.maxPointsPerTimeseries=90000`: Maximum data points per time series

### Access the Database

Once running, you can:
1. Query VictoriaMetrics directly via HTTP API at `http://localhost:8428`
2. Use Omnistat's query tools pointing to `http://localhost:8428`
3. Connect Grafana to VictoriaMetrics for visualization

### Verify the Database Loaded

After starting VictoriaMetrics, verify it's running and has data. **IMPORTANT:** Omnistat databases contain historical data, so you must use time-range queries spanning a wide period (e.g., 2020-2030) rather than instant queries which only check "now".

Use **only curl** to verify data is present:

```bash
# 1. Check if VictoriaMetrics is responding
curl -s "http://localhost:8428/api/v1/status/tsdb" | head -c 200

# 2. List available metric names with a wide historical time range
# Use Unix timestamps: start=1577836800 (2020-01-01) to end=1893456000 (2030-01-01)
curl -s 'http://localhost:8428/api/v1/label/__name__/values?start=1577836800&end=1893456000'

# 3. List available job IDs (confirms job data is present)
curl -s 'http://localhost:8428/api/v1/label/jobid/values?start=1577836800&end=1893456000'
```

**Expected results:**
- The first command should return JSON with TSDB status
- The second command should return a list of metric names (e.g., `omnistat_info`, `rocm_utilization_percentage`, etc.)
- The third command should return a list of job IDs

If the metric names and job IDs are returned, the database is loaded and ready for queries. If you get empty results `{"data":[]}`, the time range may not cover the data collection period, or there may be an issue with the database.

### Stop VictoriaMetrics

If running in foreground, press `Ctrl+C`.

If running in background:
```bash
# Using the saved PID
kill $(cat victoria.pid)

# Or find and kill the process
pkill -f victoria-metrics-prod
```

## Option 2: Using VictoriaMetrics Docker Image

If you prefer to use Docker:

### Check Before Starting

**ALWAYS** check for existing Docker containers first:
```bash
docker ps -a --filter "ancestor=victoriametrics/victoria-metrics"
```

If a container exists, stop and remove it:
```bash
docker stop victoria-metrics
docker rm victoria-metrics
```

### Start VictoriaMetrics

```bash
docker run -d \
  --name victoria-metrics \
  -p 8428:8428 \
  -v /path/to/omnistat/database:/data:rw \
  victoriametrics/victoria-metrics \
  -storageDataPath=/data \
  -httpListenAddr=0.0.0.0:8428 \
  -retentionPeriod=100y \
  -search.disableCache \
  -search.latencyOffset=0 \
  -search.maxPointsPerTimeseries=90000
```

**Important:**
- Replace `/path/to/omnistat/database` with the actual path to your Omnistat database's **root directory** (the directory containing `data/`, `cache/`, etc.), **NOT** the `data/` subdirectory itself.
- The volume mount uses `:rw` (read-write) to ensure VictoriaMetrics can read and write to the database directory. This is required for VictoriaMetrics to function properly.

### Stop VictoriaMetrics Docker Container

**IMPORTANT:** For Docker-based VictoriaMetrics, use Docker commands (no sudo required):

```bash
# Stop the container gracefully
docker stop victoria-metrics

# Remove the container
docker rm victoria-metrics
```

**DO NOT** use `sudo kill` or `pkill` commands on Docker containers - these are incorrect and will not work properly.

## Using Omnistat Query Tools

Once VictoriaMetrics is running with your database loaded, you can use Omnistat's standalone query utilities:

```bash
# Generate a report card for a specific job
omnistat-query --prometheus-url http://localhost:8428 --job <job_id>
```

Refer to the Omnistat query documentation for more advanced usage.

## Important Notes

### Retention Period

**Always set `-retentionPeriod` to a very long duration** (e.g., `100y` for 100 years) when loading databases for analysis. VictoriaMetrics defaults to a 1-month retention period, which means it will automatically delete data older than 1 month. Since Omnistat databases often contain historical telemetry data you want to preserve for analysis, failing to set this flag could result in permanent data loss.

## Troubleshooting

### VictoriaMetrics won't start
- Verify the database path points to the **root database directory** (containing `data/`, `cache/`, etc.), **not** the `data/` subdirectory
- Check for existing instances (see "Checking for Existing VictoriaMetrics Instances" above)
- Ensure you have read/write permissions on the database directory

### Port 8428 is already in use

If port 8428 is occupied by another process (not VictoriaMetrics), use an alternative port:

```bash
# Binary: use a different port
./victoria-metrics-prod \
  -storageDataPath=/path/to/database \
  -httpListenAddr=0.0.0.0:8429 \
  -retentionPeriod=100y \
  ...

# Docker: map to a different host port
docker run -d \
  --name victoria-metrics \
  -p 8429:8428 \
  ...
```

Then use `http://localhost:8429` instead of `http://localhost:8428` for queries.

### No data returned from queries
- Confirm VictoriaMetrics is pointing to the correct **root database directory** (not the `data/` subdirectory)
- Check that the database actually contains data (should have files in the `data/` subdirectory)
- Verify the time range of your queries matches the collection period
- **Check if data was deleted due to retention period** - if you didn't set `-retentionPeriod=100y`, old data may have been automatically removed

### Data disappeared after loading
- This usually means the retention period was not set properly
- VictoriaMetrics deleted data older than the default 1-month retention
- Always use `-retentionPeriod=100y` to prevent automatic deletion

### Permission errors
- Ensure the database directory has appropriate read/write permissions
- For Docker, the volume mount must be `:rw` (read-write)
- Check that the database directory is readable by your user
