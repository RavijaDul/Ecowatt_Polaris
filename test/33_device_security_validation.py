#!/usr/bin/env python3
"""
Device Security Validation Test Suite
======================================

Tests whether the EcoWatt DEVICE properly validates:
- HMAC signatures on incoming config_update/command messages
- Nonce replay protection
- Tampered payloads
- Wrong PSK signatures
- Missing/malformed envelope fields

The test acts as a MOCK SERVER that sends intentionally malformed/secure messages
and monitors whether the device properly rejects them.

Usage:
    python test/33_device_security_validation.py
    
Requires:
    - Device must be running and polling the test server
    - Device must have CONFIG_ECOWATT_CLOUD_BASE_URL pointing to localhost:8888
    - ESP-IDF Monitor running to capture device logs
"""

import json
import base64
import hmac
import hashlib
import time
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Event
import sys

# ============================================================================
# CONFIGURATION
# ============================================================================

PSK = "ecowatt-demo-psk"  # Must match device's CONFIG_ECOWATT_PSK
TEST_PORT = 5000
DEVICE_ID = "EcoWatt-Dev-01"

# ANSI colors
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def print_test(name):
    print(f"\n{BLUE}{'='*70}{RESET}")
    print(f"{BLUE}🧪 TEST: {name}{RESET}")
    print(f"{BLUE}{'='*70}{RESET}")

def print_info(msg):
    print(f"{YELLOW}ℹ️  {msg}{RESET}")

def print_pass(msg):
    print(f"{GREEN}✅ PASS: {msg}{RESET}")

def print_fail(msg):
    print(f"{RED}❌ FAIL: {msg}{RESET}")

def wrap_envelope(psk, payload_dict, nonce=None, tamper_mac=False, wrong_psk=False):
    """
    Wrap payload in HMAC-signed envelope (server→device format)
    
    Args:
        psk: Pre-shared key (or wrong key if testing)
        payload_dict: Python dict to be signed
        nonce: Optional nonce (if None, uses timestamp)
        tamper_mac: If True, return envelope with corrupted MAC
        wrong_psk: If True, use different PSK for HMAC
    
    Returns:
        dict: {"nonce": N, "payload": "base64(...)", "mac": "hexdigest(...)"}
    """
    if nonce is None:
        nonce = int(time.time() * 1000)
    
    payload_json = json.dumps(payload_dict, separators=(",", ":"))
    payload_b64 = base64.b64encode(payload_json.encode()).decode()
    
    # Sign with PSK (or wrong_psk if testing)
    key_for_mac = "wrong-psk-key" if wrong_psk else psk
    mac = hmac.new(key_for_mac.encode(), f"{nonce}.{payload_b64}".encode(), hashlib.sha256).hexdigest()
    
    # Optionally tamper with MAC
    if tamper_mac:
        mac = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    
    return {"nonce": nonce, "payload": payload_b64, "mac": mac}

# ============================================================================
# TEST SERVER (Mock)
# ============================================================================

class TestRequestHandler(BaseHTTPRequestHandler):
    """Mock server that responds to device polling with test payloads"""
    
    # Class variables to control what response to send
    test_scenario = "none"
    device_last_nonce = 0
    test_passed = False
    
    def do_POST(self):
        """Handle device POST to /api/device/upload"""
        if self.path != "/api/device/upload":
            self.send_error(404)
            return
        
        # Read device's upload
        content_length = int(self.headers.get('Content-Length', 0))
        device_upload = self.rfile.read(content_length)
        
        try:
            device_msg = json.loads(device_upload)
            device_nonce = device_msg.get('nonce', 0)
            TestRequestHandler.device_last_nonce = device_nonce
            
            print_info(f"Device uploaded with nonce={device_nonce}")
            
        except Exception as e:
            print_fail(f"Failed to parse device upload: {e}")
        
        # Send response based on current test scenario
        response = self._get_test_response()
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())
    
    def _get_test_response(self):
        """Generate response envelope based on current test"""
        scenario = TestRequestHandler.test_scenario
        
        if scenario == "none":
            return {}
        
        # ---- Scenario 1: Valid Config Update ----
        if scenario == "valid_config":
            payload = {
                "config_update": {
                    "sampling_interval": 10,
                    "registers": ["VAC1", "IAC1", "FAC1"]
                }
            }
            return wrap_envelope(PSK, payload)
        
        # ---- Scenario 2: Config with Bad HMAC ----
        if scenario == "config_bad_hmac":
            payload = {
                "config_update": {
                    "sampling_interval": 10,
                    "registers": ["VAC1", "IAC1"]
                }
            }
            return wrap_envelope(PSK, payload, tamper_mac=True)
        
        # ---- Scenario 3: Config Signed with Wrong PSK ----
        if scenario == "config_wrong_psk":
            payload = {
                "config_update": {
                    "sampling_interval": 20,
                    "registers": ["VAC1"]
                }
            }
            return wrap_envelope(PSK, payload, wrong_psk=True)
        
        # ---- Scenario 4: Config Replay (same nonce) ----
        if scenario == "config_replay":
            payload = {
                "config_update": {
                    "sampling_interval": 5
                }
            }
            # Use old nonce (device will reject as "too old")
            return wrap_envelope(PSK, payload, nonce=12345)
        
        # ---- Scenario 5: Command with Bad HMAC ----
        if scenario == "command_bad_hmac":
            payload = {
                "command": {
                    "value": 99  # 99% export
                }
            }
            return wrap_envelope(PSK, payload, tamper_mac=True)
        
        # ---- Scenario 6: Command with Wrong PSK ----
        if scenario == "command_wrong_psk":
            payload = {
                "command": {
                    "value": 50
                }
            }
            return wrap_envelope(PSK, payload, wrong_psk=True)
        
        # ---- Scenario 7: Command Replay ----
        if scenario == "command_replay":
            payload = {
                "command": {
                    "value": 75
                }
            }
            # Use fixed old nonce
            return wrap_envelope(PSK, payload, nonce=11111)
        
        # ---- Scenario 8: Tampered Payload (but valid HMAC) ----
        if scenario == "config_tampered_payload":
            # Create valid envelope for malicious config
            payload = {
                "config_update": {
                    "sampling_interval": 1,  # Extreme value
                    "registers": ["ALL_REGISTERS"]  # Invalid register list
                }
            }
            return wrap_envelope(PSK, payload)
        
        # ---- Scenario 9: Missing MAC field ----
        if scenario == "config_missing_mac":
            payload = {
                "config_update": {
                    "sampling_interval": 10
                }
            }
            nonce = int(time.time() * 1000)
            payload_json = json.dumps(payload, separators=(",", ":"))
            payload_b64 = base64.b64encode(payload_json.encode()).decode()
            # Intentionally omit 'mac' field
            return {"nonce": nonce, "payload": payload_b64}
        
        # ---- Scenario 10: Invalid Base64 Payload ----
        if scenario == "config_invalid_b64":
            nonce = int(time.time() * 1000)
            invalid_b64 = "not!!!valid!!!base64!!!"
            mac = hmac.new(PSK.encode(), f"{nonce}.{invalid_b64}".encode(), hashlib.sha256).hexdigest()
            return {"nonce": nonce, "payload": invalid_b64, "mac": mac}
        
        return {}
    
    def log_message(self, format, *args):
        """Suppress default HTTP logging"""
        pass

# ============================================================================
# TEST SCENARIOS
# ============================================================================

def run_test_scenario(scenario_name, expected_rejection=False):
    """
    Run a single test scenario
    
    Args:
        scenario_name: Name of scenario to test
        expected_rejection: Whether device should reject (True) or accept (False)
    """
    print_test(scenario_name)
    
    TestRequestHandler.test_scenario = scenario_name
    
    print_info("Waiting for device to poll server...")
    print_info("(Device must be configured to poll http://localhost:5000/api/device/upload)")
    print_info("Looking at device logs for: 'bad HMAC or replay' (rejection) or 'queued config'/'execution' (acceptance)")
    
    # Give device time to poll (typically every 15 seconds)
    for i in range(30, 0, -1):
        sys.stdout.write(f"\r⏳ Waiting {i}s for device to poll... ")
        sys.stdout.flush()
        time.sleep(1)
    print()
    
    if expected_rejection:
        print_info("Check device logs for: '⚠️ bad HMAC or replay in cloud reply — ignored'")
        user_input = input(f"{YELLOW}Did device reject the message? (y/n): {RESET}")
        if user_input.lower() == 'y':
            print_pass(f"Device correctly rejected {scenario_name}")
            return True
        else:
            print_fail(f"Device should have rejected {scenario_name} but didn't")
            return False
    else:
        print_info("Check device logs for: 'queued config' or 'command executed'")
        user_input = input(f"{YELLOW}Did device accept and process the message? (y/n): {RESET}")
        if user_input.lower() == 'y':
            print_pass(f"Device correctly accepted {scenario_name}")
            return True
        else:
            print_fail(f"Device should have accepted {scenario_name} but didn't")
            return False

def main():
    parser = argparse.ArgumentParser(description="Device Security Validation Test Suite")
    parser.add_argument('--port', type=int, default=TEST_PORT, help=f'Server port (default: {TEST_PORT})')
    parser.add_argument('--psk', default=PSK, help=f'Pre-shared key (default: {PSK})')
    args = parser.parse_args()
    
    PSK_OVERRIDE = args.psk
    
    print(f"\n{BOLD}{BLUE}")
    print("╔════════════════════════════════════════════════════════════════════╗")
    print("║           Device Security Validation Test Suite                   ║")
    print("║                                                                    ║")
    print("║  Tests whether device properly validates incoming messages with:  ║")
    print("║  - HMAC signatures                                               ║")
    print("║  - Nonce replay protection                                       ║")
    print("║  - Tampered payloads                                             ║")
    print("║  - Wrong PSK                                                     ║")
    print("║  - Missing/malformed fields                                      ║")
    print("╚════════════════════════════════════════════════════════════════════╝")
    print(f"{RESET}\n")
    
    print(f"{YELLOW}Configuration:{RESET}")
    print(f"  Port: {args.port}")
    print(f"  PSK: {args.psk}")
    print(f"  Device ID: {DEVICE_ID}")
    
    print(f"\n{YELLOW}SETUP INSTRUCTIONS:{RESET}")
    print(f"1. Configure device firmware to poll: http://localhost:{args.port}/api/device/upload")
    print(f"2. Flash device: idf.py build flash monitor")
    print(f"3. Start this script (it will host a mock server)")
    print(f"4. Watch device logs in ESP-IDF Monitor for acceptance/rejection")
    print(f"5. Answer prompts about what device did\n")
    
    # Start mock server
    server = HTTPServer(('localhost', args.port), TestRequestHandler)
    print(f"{GREEN}✓ Mock server started on localhost:{args.port}{RESET}\n")
    
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    
    try:
        results = {}
        
        # Test scenarios: (name, should_be_rejected)
        scenarios = [
            ("valid_config", False),                    # ✓ Should accept
            ("config_bad_hmac", True),                  # ✗ Should reject
            ("config_wrong_psk", True),                 # ✗ Should reject
            ("config_replay", True),                    # ✗ Should reject
            ("config_invalid_b64", True),               # ✗ Should reject
            ("config_missing_mac", True),               # ✗ Should reject
            ("command_bad_hmac", True),                 # ✗ Should reject
            ("command_wrong_psk", True),                # ✗ Should reject
            ("command_replay", True),                   # ✗ Should reject
        ]
        
        for scenario_name, should_reject in scenarios:
            result = run_test_scenario(scenario_name, expected_rejection=should_reject)
            results[scenario_name] = result
            time.sleep(2)  # Pause between tests
        
        # Summary
        print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
        print(f"{BOLD}{BLUE}TEST SUMMARY{RESET}{BLUE}{RESET}")
        print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")
        
        passed = sum(1 for v in results.values() if v)
        total = len(results)
        
        for scenario, result in results.items():
            symbol = "✅" if result else "❌"
            print(f"  {symbol} {scenario}")
        
        print(f"\n{BOLD}Result: {passed}/{total} tests passed{RESET}")
        
        if passed == total:
            print(f"{GREEN}🎉 All security validation tests passed!{RESET}")
        else:
            print(f"{RED}⚠️  Some tests failed. Review device logs.{RESET}")
        
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Test interrupted by user{RESET}")
    finally:
        server.shutdown()

if __name__ == "__main__":
    main()
