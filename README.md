| Supported Targets | ESP32 | ESP32-C2 | ESP32-C3 | ESP32-C5 | ESP32-C6 | ESP32-C61 | ESP32-H2 | ESP32-H21 | ESP32-H4 | ESP32-P4 | ESP32-S2 | ESP32-S3 | Linux |
| ----------------- | ----- | -------- | -------- | -------- | -------- | --------- | -------- | --------- | -------- | -------- | -------- | -------- | ----- |

# EcoWatt - Milestone 03: Local Buffering, Compression, and Upload Cycle

## Project Overview

### Background
EcoWatt is building a small embedded device called EcoWatt Device that pretends it is plugged into a real solar inverter. Because we don't have a physical inverter for the project, the device talks to an online Inverter SIM service that behaves like a real inverter: it answers register read requests and accepts configuration write commands.  

In this milestone, the device extends its functionality by buffering samples locally, compressing them with a lightweight codec, and periodically uploading compressed payloads to the EcoWatt Cloud.

### What the System Does
- **Read a lot, send a little**: The EcoWatt Device can read inverter data frequently but is restricted to uploading once every 15 minutes. It must buffer, compress, and package samples before transmission.  
- **Local buffering**: Samples are stored in a ring buffer until the next upload window.  
- **Compression**: Implements delta + run-length encoding (RLE v1) with CRC32 integrity checks.  
- **Periodic upload**: Data batches are compressed, encoded in Base64, and sent to the EcoWatt Cloud API endpoint.  
- **Benchmarking**: Compression efficiency is measured and reported (original vs compressed size, ratio, CPU overhead, lossless recovery).  

## Core Objective
Enhance the EcoWatt Device to:
- Buffer acquired samples for the entire upload interval without loss.  
- Apply a compression method (delta/RLE v1) and validate decompression integrity.  
- Build uplink payloads containing timestamps, codec metadata, and compressed data.  
- Upload payloads to EcoWatt Cloud at fixed intervals (15 minutes, simulated as 15 seconds for testing).  

## Actors & Channels
- **EcoWatt Device** (ESP32 MCU – the "slave")  
- **EcoWatt Cloud** (receives compressed payloads, ACKs, commands)  
- **Inverter SIM** (emulates inverter registers for acquisition)  

### Inverter SIM Deployment Options
- **Cloud API Mode** – EcoWatt Device communicates with Inverter SIM over HTTP REST.  
- **PC Modbus Mode** – (not used in this milestone, but supported).  

## Transport Constraints
- **EcoWatt Device → EcoWatt Cloud**: upload once every 15 minutes (simulated as 15 seconds).  
- **EcoWatt Device ↔ Inverter SIM**: high-frequency polling (e.g., 5 seconds).  

## EcoWatt Device Functional Blocks
- **Protocol Adapter Layer**: Formats Modbus requests, parses responses from Inverter SIM.  
- **Acquisition Scheduler**: Polls inverter registers periodically.  
- **Local Buffer**: Thread-safe ring buffer stores acquired records until next upload window.  
- **Compression Codec**: Delta + RLE v1 encoding with CRC-32 validation. Lossless decompression verified.  
- **Uplink Packetizer**: Builds JSON payloads with timestamps, field order, and Base64-encoded compressed block.  
- **Upload Cycle**: Posts payloads to EcoWatt Cloud endpoint; logs success/failure.  

## Key Workflows
- **Acquisition Loop**: Poll Inverter SIM → buffer sample → repeat.  
- **Upload Cycle**: Every 15 min/15 sec → snapshot buffer → compress → build payload → upload to cloud.  
- **Compression Benchmarking**: Measure payload size before/after compression, ratio, and CPU overhead.  

## Milestone Breakdown
### Milestone 1: System Modeling with Petri Nets and Scaffold Implementation (10%)  
### Milestone 2: Inverter SIM Integration and Basic Acquisition (20%)  
### Milestone 3: Local Buffering, Compression, and Upload Cycle (20%)  
- **Objective**: Implement local buffering, compression algorithm, and upload cycle.  
- **Scope**:  
  - Buffering: Ring buffer to store samples until upload window.  
  - Compression: Delta + RLE v1 codec with CRC. Benchmark compression ratio and verify lossless decompression.  
  - Uplink: Build JSON payloads and post to EcoWatt Cloud API.  

## Hardware Requirements
- ESP32 development board  
- Wi-Fi connectivity  
- USB cable for programming and debugging  

## Software Requirements
- ESP-IDF framework (latest stable version)  
- CMake build system  
- Python 3.7 or later (for testing/benchmarking scripts)  
- Git for version control  

## Build and Flash Instructions
```bash
# Setup ESP-IDF environment
idf.py set-target esp32
idf.py menuconfig   # Configure Wi-Fi, SIM endpoints, Cloud endpoints
idf.py build
idf.py -p <PORT> flash
idf.py -p <PORT> monitor
```
## Project Structure
```

├── CMakeLists.txt
├── sdkconfig
├── main/
│   ├── main.cpp             # Application entry point (tasks for acquisition + upload)
│   ├── buffer.cpp/.hpp      # Thread-safe ring buffer
│   ├── acquisition.cpp/.hpp # Data acquisition (Modbus over HTTP transport)
│   ├── modbus.cpp/.hpp      # Modbus request/response handling
│   ├── codec.cpp/.hpp       # Delta + RLE compression/CRC
│   ├── packetizer.cpp/.hpp  # Payload builder + uploader
│   ├── transport_idf.cpp    # HTTP client transport
│   └── Kconfig.projbuild
```

## Usage
1. Device connects to Wi-Fi and synchronizes time via NTP.  
2. Acquisition task polls Inverter SIM every 5 seconds.  
3. Records are pushed into the ring buffer.  
4. Every 15 seconds (demo) or 15 minutes (real), uplink task:  
   - Takes snapshot of buffer  
   - Compresses with codec  
   - Builds JSON payload with metadata + compressed block  
   - Uploads to EcoWatt Cloud endpoint  
5. Monitor serial logs for acquisition ticks, payload size, compression ratio, and upload status.  

## Configuration
- Wi-Fi SSID and password  
- Inverter SIM base URL and API key  
- Cloud server URL and API key  
- Sampling period (ms)  
- Upload interval (sec)  

## Troubleshooting
- **Buffer Overflow**: Increase ring buffer capacity if too many samples before upload.  
- **Upload Failures**: Check Wi-Fi, cloud API availability, and endpoint URL.  
- **Compression Errors**: Verify encoding/decoding with CRC.  
- **Build Issues**: Ensure ESP-IDF is installed and PATH configured.  

## Evaluation Criteria
- Correct buffering implementation and data integrity (20%)  
- Compression efficiency and benchmark quality (20%)  
- Robustness and correctness of packetizer + upload logic (20%)  
- Correct cloud API implementation (20%)  
- Clarity of demo video (10%)  
- Code readability and modularity (10%)  

## Technical Support
- ESP-IDF Docs: https://docs.espressif.com/projects/esp-idf/  
- ESP32 Forums: https://esp32.com/  
- Inverter SIM API docs: [Provided Link]  
- GitHub Issues: for reporting bugs and requests  
