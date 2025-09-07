| Supported Targets | ESP32 | ESP32-C2 | ESP32-C3 | ESP32-C5 | ESP32-C6 | ESP32-C61 | ESP32-H2 | ESP32-H21 | ESP32-H4 | ESP32-P4 | ESP32-S2 | ESP32-S3 | Linux |
| ----------------- | ----- | -------- | -------- | -------- | -------- | --------- | -------- | --------- | -------- | -------- | -------- | -------- | ----- |

# EcoWatt - Milestone 02: Inverter SIM Integration and Basic Acquisition

## Project Overview

### Background
EcoWatt is building a small embedded device called EcoWatt Device that pretends it is plugged into a real solar inverter. Because we don't have a physical inverter for the project, the device will talk to an online Inverter SIM service that behaves just like a real inverter would: it answers register read requests and accepts configuration write commands.

### What the System Does
- **Read a lot, send a little**: EcoWatt Device can read data (voltage, current, and later things like frequency) as often as we like, but it is only allowed to send data to the cloud once every 15 minutes. So it must store, shrink (compress), and package everything before the upload window.
- **Let EcoWatt Cloud stay in charge**: EcoWatt Cloud decides what to read, how often to read it, and can even push a new firmware image to the device. The device ("slave") simply follows those instructions safely.
- **Stay secure but lightweight**: We use small-footprint security (keys, message authentication, simple encryption) so the MCU can still run fast and save power.
- **Save power smartly**: The device should slow down its clock (DVFS) or sleep between jobs to cut its own energy use.
- **Be future-ready**: Later, we can swap the Inverter SIM for a real inverter with minimal code changes because we've cleanly separated the protocol layer.

## Core Objective
Build a microcontroller device called EcoWatt Device ("slave") that behaves like a field gateway. Instead of a physical inverter, it communicates with a Simulated Inverter called Inverter SIM. EcoWatt Device:
- Polls inverter registers (voltage, current, optional frequency, etc.) at a configurable rate.
- Buffers and compresses data locally.
- Uploads once every 15 minutes through a constrained link.
- Accepts remote configuration & firmware updates (FOTA) from the cloud "master."
- Implements lightweight security (auth, integrity, confidentiality) suitable for MCUs.
- Uses power saving / DVFS & sleep between tasks.

## Actors & Channels
Three main pieces interact:
- **EcoWatt Device** (ESP32 MCU – the "slave")
- **EcoWatt Cloud** (configuration, commands, FOTA, data ingestion – the "master")
- **Inverter SIM** (the inverter emulator EcoWatt Device talks to instead of real hardware) – provided to you.

### Inverter SIM Deployment Options
- **Cloud API Mode** – Inverter SIM runs as an HTTP/MQTT service online. EcoWatt Device reaches it over Wi-Fi just like it would reach EcoWatt Cloud. The inverter protocol is encapsulated inside these requests.
- **PC Modbus Mode** – Inverter SIM runs locally on a PC as a Modbus simulator. EcoWatt Device connects to the PC over USB (UART).

For this project, we will use the Cloud API Mode.

## Transport Constraints
- **EcoWatt Device → EcoWatt Cloud**: bulk payload upload strictly once every 15 minutes on a request-response setup.
- **EcoWatt Device ↔ Inverter SIM**: high-frequency polling allowed (configurable). Responses are stored locally and only forwarded (compressed) at the 15-minute.

## EcoWatt Device Functional Blocks
- **Protocol Adapter Layer**: Implements inverter serial semantics over network to Inverter SIM. Swappable for real RS-485 later.
- **Acquisition Scheduler**: Periodic reads (configurable from EcoWatt Cloud). Aperiodic writes/controls triggered by EcoWatt Cloud.
- **Local Buffer & Compression**: Local buffer for samples. Lightweight compression (delta/RLE or time-series scheme). Optional aggregation (min/avg/max).
- **Uplink Packetizer**: 15-minute tick: finalize compressed block → encrypt/MAC → transmit. Retry/chunk logic for unreliable links.
- **Remote Config & Command Handler**: Receives new sampling frequency, additional register list, compression level, etc. Applies changes without reflashing.
- **FOTA (Firmware Over-The-Air) Module**: Secure, chunked firmware download over multiple intervals. Integrity check (hash/signature), dual-bank/rollback.
- **Security Layer (Lightweight)**: Mutual auth (PSK + HMAC). AES-CCM / ChaCha20-Poly1305 for payloads. Anti-replay via nonces/sequence numbers.
- **Power Management / DVFS**: Clock scaling when idle; sleep between polls/uploads. Peripheral gating.
- **Diagnostics & Fault Handling**: Handle Inverter SIM timeouts, malformed frames, buffer overflow. Local event log.

## Key Workflows
- **Acquisition Loop**: EcoWatt Device polls Inverter SIM at configured rate → buffers & compresses → sleeps/DVFS between polls.
- **15-Minute Upload Cycle**: Wake → finalize block → encrypt/HMAC → upload to EcoWatt Cloud → receive ACK + pending configs/commands.
- **Remote Configuration Update**: EcoWatt Cloud sends new config → EcoWatt Device validates & updates acquisition tables instantly.
- **Firmware Update (FOTA)**: EcoWatt Cloud flags update → EcoWatt Device downloads chunks → verifies → swaps image → reports result.
- **Command Execution (Writes)**: EcoWatt Cloud queues write → delivered at next slot → EcoWatt Device forwards to Inverter SIM → returns status next slot.
- **Fault & Recovery**: Network/API errors → backoff & retry. Integrity failures → discard and alert.

## Milestone Breakdown
### Milestone 1: System Modeling with Petri Nets and Scaffold Implementation (10%)
- **Objective**: Use Petri Net to model periodic polling, buffering, and uploading. Build scaffold prototype.
- **Scope**: Poll Inverter SIM (simulate), buffer data, upload every 15 seconds (simulated). Scaffold in Python, modular structure.

### Milestone 2: Inverter SIM Integration and Basic Acquisition (20%)
- **Objective**: Integrate EcoWatt Device with Inverter SIM, implement protocol adapter, basic acquisition scheduler.
- **Scope**: Protocol adapter for request/response, acquisition scheduler polling voltage/current every 5 seconds, store in memory, perform write operation.

## Hardware Requirements
- ESP32 development board
- Wi-Fi connectivity
- USB cable for programming and debugging

## Software Requirements
- ESP-IDF framework (latest stable version)
- CMake build system
- Python 3.7 or later
- Git for version control

## Build and Flash Instructions
1. **Setup ESP-IDF Environment**:
   ```bash
   # Follow ESP-IDF getting started guide for your platform
   # https://docs.espressif.com/projects/esp-idf/en/stable/get-started/index.html
   ```

2. **Clone and Build**:
   ```bash
   git clone https://github.com/RavijaDul/Ecowatt_Polaris.git
   cd Ecowatt_Polaris
   idf.py set-target esp32
   idf.py menuconfig  # Configure project settings
   idf.py build
   ```

3. **Flash to Device**:
   ```bash
   idf.py -p <PORT> flash
   idf.py -p <PORT> monitor
   ```

## Project Structure
```
├── CMakeLists.txt              # Main project build configuration
├── sdkconfig                   # Project configuration
├── main/
│   ├── CMakeLists.txt          # Main component build config
│   ├── main.cpp                # Application entry point
│   ├── acquisition.cpp/.hpp    # Data acquisition module
│   ├── modbus.cpp/.hpp         # Modbus communication module
│   ├── transport_idf.cpp/.hpp  # Transport layer implementation
│   └── Kconfig.projbuild       # Component configuration
├── .clangd                     # Clang language server config
├── .gitignore                  # Git ignore rules
└── README.md                   # This file
```

## Usage
After flashing the firmware:
1. The device will initialize and connect to Wi-Fi
2. Start polling Inverter SIM for voltage and current data
3. Buffer data locally
4. Upload compressed data every 15 minutes to EcoWatt Cloud
5. Monitor serial output for status and debug information

## Configuration
Use `idf.py menuconfig` to configure:
- Wi-Fi settings
- Inverter SIM endpoint
- Polling intervals
- Security parameters

## Troubleshooting
* **Build Issues**: Ensure ESP-IDF is properly installed and configured
* **Flash Failure**: Check USB connection and correct port selection
* **Communication Errors**: Verify Wi-Fi connection and Inverter SIM endpoint
* **API Errors**: Check Inverter SIM service availability

## Evaluation Criteria
- **Success Criteria**: Compression/buffering fits 15-min payload, config flexibility, FOTA reliability, security, power saving, protocol abstraction.
- **Rubric for Milestone 2**: Correct read/write operations (30%), protocol adapter clarity (25%), code modularity (20%), video demo (15%), documentation (10%).

## Technical Support
For technical queries and support:
- ESP-IDF Documentation: https://docs.espressif.com/projects/esp-idf/
- ESP32 Forums: https://esp32.com/
- Project Issues: Create GitHub issues for bug reports and feature requests
- Inverter SIM Documentation: [Provided Link]
