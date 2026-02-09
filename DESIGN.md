# Initial Design Notes

> This document captures the original design thinking and motivation for the beholder project.
> For usage instructions, see [README.md](README.md).

# datalab-plugin-beholder

Filesystem watcher daemon for datalab instances.

## Overview

A lightweight daemon for monitoring laboratory instrument file systems and synchronizing metadata/files to datalab instances. This enables near-real-time data capture from instruments without exposing them to the internet or requiring complex network configurations.

## Use Cases

### Primary Use Case: Instrument PC File Watching

**Scenario**: A research group has laboratory instruments (XRD, NMR, electrochemistry stations, etc.) that write data files to local storage. Researchers need timely access to this data in datalab for analysis and sample tracking.

**Challenges**:
- Instrument PCs should not be directly exposed to the internet for security reasons
- Manual file transfer via USB drives or network shares is error-prone and time-consuming
- Researchers need files available in datalab within minutes of acquisition for ongoing experiments
- Different instruments may be on separate networks or behind institutional firewalls

**Solution**: Install a lightweight daemon on the instrument PC (or an adjacent system with file system access) that:
1. Monitors configured directories for new/modified files
2. Periodically syncs file metadata (paths, sizes, timestamps) to datalab
3. Waits for user requests from the datalab UI to transfer specific files
4. Pushes requested files to datalab on-demand

**Benefits**:
- One-way outbound connections only (no inbound firewall rules needed)
- Users see available files in near-real-time without full transfer
- Selective file transfer reduces bandwidth usage
- Instrument PCs remain isolated from general network access

### Secondary Use Case: SSH-Based Remote File Systems

**Scenario**: Multiple research groups collaborate on a shared datalab instance, but maintain separate file storage systems (NAS, departmental servers, HPC clusters). Each group wants their files visible in the shared datalab without exposing their entire file system to other groups.

**Challenges**:
- Groups don't want to expose their file systems to everyone using the datalab
- Current approach using SSH + `tree` command is slow for large directories (10,000+ files)
- No ability to install software on remote systems (limited to SSH access only)
- Need efficient way to browse and selectively attach files to samples

**Solution**: Develop a high-performance file system scanning library that:
1. Can be invoked remotely via SSH without installation (single binary transfer)
2. Generates structured metadata about file systems much faster than `tree`
3. Supports incremental scanning (diffs since last scan)
4. Provides JSON output for integration with datalab API

**Key Difference from Primary Use Case**:
- No persistent daemon running on remote system
- Triggered on-demand by datalab server via SSH
- Read-only access - no file transfer capability
- Used for browsing and metadata only; actual file access still via SSH/SCP

**Benefits**:
- 10-100x faster than current `tree`-based approach
- Structured output eliminates text parsing
- Can show last-modified times and file sizes for better filtering
- No installation burden on remote systems
- Maintains existing security model (SSH-based access)

### Use Case Comparison

| Aspect | Instrument PC Daemon | SSH Remote Scanning |
|--------|---------------------|---------------------|
| **Installation** | Persistent daemon on local system | No installation; binary transferred per-use |
| **Network Access** | Outbound HTTPS only | Inbound SSH from datalab server |
| **File Transfer** | Push to datalab on request | Pull via SCP/SFTP (existing mechanism) |
| **Update Frequency** | Real-time (20 min metadata, 1 min requests) | On-demand when user browses |
| **Primary Goal** | Near-real-time instrument data access | Efficient browsing of shared storage |
| **Security Model** | Instrument isolation | Per-group SSH credentials |

## Motivation

### Current Challenges

**Instrument Data Capture**:
- Laboratory instruments often cannot or should not be directly exposed to the internet
- Manual file transfer from instruments is error-prone and delays data availability
- Researchers need timely access to instrument data for ongoing experiments
- Different instruments may have different network configurations

**Collaborative Remote Storage**:
- Current remote file system access via SSH + `tree` is slow and inefficient
- Parsing text-based `tree` output is fragile
- No way to incrementally update large directory listings
- Groups sharing a datalab want selective file system exposure

### Benefits of Proposed Solutions

**Daemon Approach (Instruments)**:
- **Security**: Instruments remain behind firewalls; daemon initiates outbound connections only
- **Real-time workflows**: Files become available in datalab minutes after acquisition
- **Reduced friction**: No manual copying or USB drives needed
- **Scalability**: Metadata syncing on schedule with on-demand file transfer

**Scanning Library (SSH Remotes)**:
- **Performance**: 10-100x faster than `tree` for large directories
- **Structure**: Clean JSON output instead of text parsing
- **Incremental updates**: Diffs reduce scanning overhead
- **Zero installation**: Single binary transferred and executed via SSH

## Architecture

### Components

#### 1. File System Scanning Library (new, shared component)
- **Language**: Go (see rationale below)
- **Purpose**: Efficient file system traversal and metadata extraction
- **Modes**: 
 - Standalone CLI for SSH use case
 - Library for daemon integration
- **Output**: Structured JSON with file metadata and optional diffs

#### 2. File System Daemon (new, for instruments)
- Runs on instrument PC or adjacent system with file system access
- Uses scanning library for metadata collection
- Authenticates to datalab instance via API key
- Pushes metadata updates on schedule (every 20 minutes)
- Polls for file requests and pushes requested files (every 1 minute)

#### 3. datalab API Extensions (new endpoints)
- Accept metadata updates from daemons
- Queue file requests from UI
- Serve file request lists to daemons
- Receive file uploads from daemons
- Support SSH-based remote scanning (invoke library via SSH)

#### 4. datalab UI Extensions (File Manager enhancements)
- Display available remote files from both daemons and SSH remotes
- Allow users to request specific files for attachment to samples
- Show sync status and file availability
- Separate views for daemon-connected vs SSH-accessed file systems

### Communication Flow

#### Instrument Daemon Flow
```
[Instrument PC] 
                 (every 20 min)
                     
                 [datalab Server]
                      Request file attachment
                     
                 Queue in MongoDB
                     
[Daemon] 
                 [datalab Server]
                      POST /api/remote-files/upload
                     
                 File storage + Sample attachment
```

#### SSH Remote Scanning Flow
```
[User in UI] 
                 [datalab Server]
                     
                 Transfer scanning library binary (if needed)
                     
                 Parse JSON output
                     
                 Display in UI

[User in UI] 
                 [datalab Server]
                     
                 Store and attach to sample
```

## Technical Specifications

### File System Metadata Library

A new lightweight library for efficient file system scanning and change detection.

#### Requirements
- Cross-platform (Windows, Linux, macOS)
- Minimal memory footprint (instruments may have limited resources)
- Efficient diff generation (send only changes since last sync)
- Handle large directory trees (10,000+ files)
- **No installation required** - single standalone binary for SSH use case
- Usable both as CLI tool and as a library

Example output:

```json
{
 "scanner_version": "1.0.0",
 "root_path": "/path/to/watched/folder",
 "timestamp": "2025-01-29T10:30:00Z",
 "snapshot_type": "full|diff",
 "entries": [
   {
     "path": "relative/path/to/file.dat",
     "size": 1024000,
     "modified": "2025-01-29T10:15:00Z",
     "is_directory": false,
     "checksum": "sha256:abc123...",  // optional, computed on demand
     "status": "new|modified|deleted"   // for diffs only
   }
 ],
 "statistics": {
   "total_files": 1523,
   "total_directories": 145,
   "total_size": 15234000000,
   "scan_duration_ms": 1234
 }
}
```

#### Change Detection Strategy

**For Daemon Use (Persistent)**:
1. **Initial scan**: Full directory traversal, store metadata in local SQLite database
2. **File system watching**: Use OS-native APIs (inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows)
3. **Periodic diff generation**: Compare current state to last synced state
4. **Optimization**: Only compute checksums for files explicitly requested

**For SSH Use (Stateless)**:
1. **On-demand scan**: Full directory traversal on each invocation
2. **Optional diff mode**: Accept previous scan output as input, compute diff
3. **Performance**: Optimized traversal, minimal allocations
4. **Output**: JSON to stdout for easy parsing

#### CLI Interface

```bash
# Full scan
datalab-fs-scan /path/to/scan --format json

# Scan with filters
datalab-fs-scan /path/to/scan \
 --include "*.raw,*.csv,*.txt" \
 --exclude "**/temp/**,**/.tmp" \
 --format json

# Diff mode (for incremental updates)
datalab-fs-scan /path/to/scan \
 --format json \
 --diff \
 --previous-scan previous-scan.json

# Include checksums (slower)
datalab-fs-scan /path/to/scan \
 --format json \
 --checksums sha256

# Max depth
datalab-fs-scan /path/to/scan \
 --format json \
 --max-depth 5
```

### Daemon Implementation

#### Configuration

```yaml
# daemon-config.yaml
datalab:
 url: "https://datalab.example.org"
 api_key: "daemon-specific-api-key"
 
watched_paths:
 - path: "/mnt/instrument/data"
   name: "XRD-Room-A"
   include_patterns: ["*.raw", "*.csv", "*.txt"]
   exclude_patterns: ["**/temp/**", "**/.tmp"]
   
sync:
 metadata_interval: 1200  # seconds (20 minutes)
 file_request_poll: 60    # seconds (1 minute)
 
daemon:
 log_level: "info"
 cache_db: "/var/lib/datalab-daemon/cache.db"
```

#### Core Operations

1. **Metadata Sync Loop**
  ```
  Every 20 minutes:
    - Scan watched paths for changes using library
    - Generate diff from last sync
    - POST to /api/remote-files/metadata
    - Update local cache on success
  ```

2. **File Request Poll Loop**
  ```
  Every 1 minute:
    - GET /api/remote-files/pending?daemon_id={id}
    - For each pending file:
      - Read file from disk
      - Compute checksum if requested
      - POST to /api/remote-files/upload
      - Mark as completed
  ```

3. **Error Handling**
  - Exponential backoff on network failures
  - Local queue for metadata updates during outages
  - File locks to prevent reading files currently being written
  - Logging for debugging and monitoring

### API Endpoints

#### POST /api/remote-files/metadata
```python
# Request
{
 "source_type": "daemon|ssh",  # distinguish between sources
 "daemon_id": "xrd-room-a",    # for daemon sources
 "remote_id": "group-nas-01",  # for SSH sources
 "root_path": "/mnt/instrument/data",
 "timestamp": "2025-01-29T10:30:00Z",
 "snapshot_type": "full|diff",
 "entries": [...]
}

# Response
{
 "status": "success",
 "received": 150,
 "processed": 150
}
```

#### GET /api/remote-files/pending
```python
# Request
GET /api/remote-files/pending?daemon_id=xrd-room-a

# Response
{
 "requests": [
   {
     "request_id": "req-abc123",
     "path": "2025-01/sample-042/diffraction.raw",
     "priority": "high",  # user-requested vs background sync
     "requested_at": "2025-01-29T10:35:00Z"
   }
 ]
}
```

#### POST /api/remote-files/upload
```python
# Request (multipart/form-data)
{
 "request_id": "req-abc123",
 "file": <binary data>,
 "checksum": "sha256:def456...",
 "metadata": {
   "size": 1024000,
   "modified": "2025-01-29T10:15:00Z"
 }
}

# Response
{
 "status": "success",
 "file_id": "file-xyz789",
 "attached_to": ["sample-042"]
}
```

#### POST /api/remote-files/scan-ssh (internal)
```python
# Triggered when user browses SSH remote
# Server-side function that:
# 1. SSHs to remote system
# 2. Transfers scanning binary if needed
# 3. Executes scan
# 4. Caches results
# 5. Returns to UI

# Not directly exposed to external callers
```

### UI Integration

#### File Manager Enhancements

1. **Remote File Browser Tab**
  - Tree view of available files from connected sources
  - **Separate sections for Daemon sources vs SSH sources**
  - Real-time status indicators (synced 5 minutes ago, etc.)
  - Filter by source, date range, file type
  - Search across metadata
  - **Access control**: Users only see sources they have permission for

2. **File Request Workflow (Daemon Sources)**
  ```
  User clicks "Attach to Sample" 
  Files added to request queue 
  Updates to "Available" when uploaded 
  Server triggers SSH scan (if cache stale) 
  User clicks "Attach to Sample" 
  File stored and attached to sample
  ```

4. **Source Management (Admin)**
  - List of connected daemons with status
  - List of configured SSH remotes
  - Last sync/scan time, files available
  - API key generation/rotation for daemons
  - SSH credential management for remotes
  - Access control configuration per source

## Deployment and Installation

### Scanning Library Distribution

**Standalone Binary**:
```bash
# Download for platform
wget https://releases.datalab-org.io/fs-scan/v1.0.0/datalab-fs-scan-linux-amd64

# Make executable
chmod +x datalab-fs-scan-linux-amd64

# Run
./datalab-fs-scan-linux-amd64 /path/to/scan --format json
```

**For SSH Use**:
- Binary automatically transferred by datalab server on first use
- Cached in user's home directory on remote system
- Updated automatically when new version available

### Daemon Installation

**Option 1: Python Package (pip install)**
```bash
pip install datalab-daemon
datalab-daemon init  # creates config template
datalab-daemon start --config daemon-config.yaml
```

**Option 2: Standalone Binary**
```bash
# Download for platform
wget https://releases.datalab-org.io/daemon/v1.0.0/datalab-daemon-windows-amd64.exe

# Run with config
datalab-daemon.exe --config daemon-config.yaml
```

**Option 3: Docker Container**
```bash
docker run -d \
 -v /mnt/instrument:/data:ro \
 -v ./daemon-config.yaml:/config.yaml \
 datalab/daemon:latest
```
