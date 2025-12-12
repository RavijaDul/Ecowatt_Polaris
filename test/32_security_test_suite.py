#!/usr/bin/env python3
"""
Security Testing Suite for EcoWatt
Demonstrates HMAC authentication, nonce replay protection, and message tampering detection.
"""

import base64
import json
import hmac
import hashlib
import requests
import time
import sys
from typing import Dict, List, Tuple

# ==================== Configuration ====================
PSK = "ecowatt-demo-psk"  # Must match server
BASE_URL = "http://127.0.0.1:5000"
DEVICE_ID = "security-test-device"

class Colors:
    """ANSI color codes for terminal output"""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    GRAY = '\033[90m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def print_test(name: str):
    print(f"\n{Colors.BOLD}{Colors.BLUE}▶ {name}{Colors.RESET}")

def print_pass(msg: str):
    print(f"  {Colors.GREEN}✅ PASS{Colors.RESET} {msg}")

def print_fail(msg: str):
    print(f"  {Colors.RED}❌ FAIL{Colors.RESET} {msg}")

def print_info(msg: str):
    print(f"  {Colors.GRAY}ℹ {msg}{Colors.RESET}")

# ==================== Payload Creation ====================

def create_payload() -> dict:
    """Create a simple test payload"""
    return {
        "device_id": DEVICE_ID,
        "ts_start": 0,
        "ts_end": 0,
        "seq": 0,
        "codec": "none",
        "order": [],
        "block_b64": "",
        "ts_list": []
    }

def wrap_envelope(psk: str, payload: dict, nonce: int = None) -> dict:
    """Create a properly signed envelope"""
    if nonce is None:
        nonce = int(time.time() * 1000)
    
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.b64encode(payload_json.encode()).decode()
    mac = hmac.new(psk.encode(), f"{nonce}.{payload_b64}".encode(), hashlib.sha256).hexdigest()
    
    return {
        "nonce": nonce,
        "payload": payload_b64,
        "mac": mac
    }

# ==================== Test Cases ====================

def test_valid_message() -> Tuple[bool, str]:
    """✅ TEST 1: Valid message with correct HMAC and PSK"""
    print_test("Valid Message (Correct HMAC & PSK)")
    
    try:
        payload = create_payload()
        envelope = wrap_envelope(PSK, payload)
        
        print_info(f"Sending: nonce={envelope['nonce']}, mac={envelope['mac'][:16]}...")
        resp = requests.post(f"{BASE_URL}/api/device/upload", json=envelope, timeout=5)
        
        print_info(f"Response: {resp.status_code}")
        
        if resp.status_code == 200:
            print_pass("Message accepted by server")
            return True, f"Status {resp.status_code}"
        else:
            print_fail(f"Expected 200, got {resp.status_code}")
            return False, f"Status {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        print_fail(str(e))
        return False, str(e)

def test_bad_hmac() -> Tuple[bool, str]:
    """❌ TEST 2: Message with corrupted HMAC"""
    print_test("Corrupted HMAC (Invalid Signature)")
    
    try:
        payload = create_payload()
        payload_json = json.dumps(payload, separators=(",", ":"))
        payload_b64 = base64.b64encode(payload_json.encode()).decode()
        nonce = int(time.time() * 1000)
        
        # Intentionally wrong MAC
        bad_mac = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        envelope = {
            "nonce": nonce,
            "payload": payload_b64,
            "mac": bad_mac
        }
        
        print_info(f"Sending: nonce={nonce}, mac={bad_mac[:16]}... (invalid)")
        resp = requests.post(f"{BASE_URL}/api/device/upload", json=envelope, timeout=5)
        
        print_info(f"Response: {resp.status_code}")
        
        if resp.status_code != 200 or "bad-mac-or-nonce" in resp.text:
            print_pass("Server correctly rejected bad HMAC")
            return True, f"Rejected: {resp.text[:100]}"
        else:
            print_fail("Server should have rejected bad HMAC")
            return False, f"Status {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        print_fail(str(e))
        return False, str(e)

def test_wrong_psk() -> Tuple[bool, str]:
    """❌ TEST 3: Message signed with wrong PSK"""
    print_test("Wrong Pre-Shared Key (PSK Mismatch)")
    
    try:
        wrong_psk = "wrong-psk-value-123"
        payload = create_payload()
        envelope = wrap_envelope(wrong_psk, payload)
        
        correct_mac = hmac.new(PSK.encode(), 
                              f"{envelope['nonce']}.{envelope['payload']}".encode(), 
                              hashlib.sha256).hexdigest()
        
        print_info(f"Signed with: '{wrong_psk}'")
        print_info(f"Server expects: '{PSK}'")
        print_info(f"Envelope MAC: {envelope['mac'][:16]}..., Correct MAC: {correct_mac[:16]}...")
        
        resp = requests.post(f"{BASE_URL}/api/device/upload", json=envelope, timeout=5)
        
        print_info(f"Response: {resp.status_code}")
        
        if resp.status_code != 200 or "bad-mac-or-nonce" in resp.text:
            print_pass("Server correctly rejected wrong PSK")
            return True, f"Rejected: {resp.text[:100]}"
        else:
            print_fail("Server should have rejected wrong PSK")
            return False, f"Status {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        print_fail(str(e))
        return False, str(e)

def test_replay_attack() -> Tuple[bool, str]:
    """❌ TEST 4: Replay Attack - Send same message twice"""
    print_test("Replay Attack Protection (Monotonic Nonce)")
    
    try:
        payload = create_payload()
        # Use timestamp-based nonce for more realistic scenario
        current_nonce = int(time.time() * 1000)
        envelope1 = wrap_envelope(PSK, payload, nonce=current_nonce)
        
        # First send
        print_info(f"Sending message #1 with nonce={current_nonce-1000}...")
        resp1 = requests.post(f"{BASE_URL}/api/device/upload", json=envelope1, timeout=5)
        print_info(f"Response #1: {resp1.status_code}")
        
        time.sleep(0.2)
        
        # Second send (REPLAY): same nonce as first
        print_info(f"Sending message #2 (REPLAY) with same nonce={current_nonce}...")
        envelope2 = wrap_envelope(PSK, payload, nonce=current_nonce)
        resp2 = requests.post(f"{BASE_URL}/api/device/upload", json=envelope2, timeout=5)
        print_info(f"Response #2: {resp2.status_code}")
        
        # Success: first accepted, second rejected (nonce <= last_seen)
        if (resp1.status_code == 200 and 
            (resp2.status_code != 200 or "bad-mac-or-nonce" in resp2.text)):
            print_pass(f"First accepted (nonce={current_nonce}), second rejected (anti-replay working)")
            return True, f"1st: {resp1.status_code}, 2nd: {resp2.status_code}"
        else:
            print_fail(f"Second message should have been rejected as replay (nonce not increasing)")
            return False, f"1st: {resp1.status_code}, 2nd: {resp2.status_code}"
    except Exception as e:
        print_fail(str(e))
        return False, str(e)

def test_tampered_payload() -> Tuple[bool, str]:
    """❌ TEST 5: Message Tampering - Modify payload after HMAC"""
    print_test("Payload Tampering Detection")
    
    try:
        payload = create_payload()
        envelope = wrap_envelope(PSK, payload)
        
        original_payload = envelope["payload"]
        # Tamper with base64 payload
        tampered_payload = "dGFtcGVyZWRkYXRh"  # Different base64 data
        
        print_info(f"Original payload: {original_payload[:20]}...")
        print_info(f"Tampered payload: {tampered_payload}")
        print_info(f"Original MAC: {envelope['mac'][:16]}... (computed for original)")
        
        envelope["payload"] = tampered_payload
        
        resp = requests.post(f"{BASE_URL}/api/device/upload", json=envelope, timeout=5)
        
        print_info(f"Response: {resp.status_code}")
        
        if resp.status_code != 200 or "bad-mac-or-nonce" in resp.text:
            print_pass("Server detected payload tampering")
            return True, f"Rejected: {resp.text[:100]}"
        else:
            print_fail("Server should have rejected tampered payload")
            return False, f"Status {resp.status_code}"
    except Exception as e:
        print_fail(str(e))
        return False, str(e)

def test_missing_mac() -> Tuple[bool, str]:
    """❌ TEST 6: Missing MAC field"""
    print_test("Missing MAC Field")
    
    try:
        payload = create_payload()
        payload_json = json.dumps(payload, separators=(",", ":"))
        payload_b64 = base64.b64encode(payload_json.encode()).decode()
        nonce = int(time.time() * 1000)
        
        # Omit MAC field
        envelope = {
            "nonce": nonce,
            "payload": payload_b64
            # "mac" field intentionally omitted
        }
        
        print_info(f"Envelope fields: {list(envelope.keys())}")
        
        resp = requests.post(f"{BASE_URL}/api/device/upload", json=envelope, timeout=5)
        
        print_info(f"Response: {resp.status_code}")
        
        if resp.status_code != 200:
            print_pass("Server rejected envelope without MAC")
            return True, f"Rejected: {resp.text[:100]}"
        else:
            print_fail("Server should have rejected missing MAC")
            return False, f"Status {resp.status_code}"
    except Exception as e:
        print_fail(str(e))
        return False, str(e)

def test_invalid_base64() -> Tuple[bool, str]:
    """❌ TEST 7: Invalid Base64 encoding"""
    print_test("Invalid Base64 Encoding")
    
    try:
        nonce = int(time.time() * 1000)
        invalid_b64 = "!!!not@valid#base64$$$"
        
        # Compute HMAC even though payload is invalid
        mac = hmac.new(PSK.encode(), f"{nonce}.{invalid_b64}".encode(), hashlib.sha256).hexdigest()
        
        envelope = {
            "nonce": nonce,
            "payload": invalid_b64,
            "mac": mac
        }
        
        print_info(f"Payload: '{invalid_b64}' (invalid base64)")
        print_info(f"MAC: {mac[:16]}... (computed for invalid payload)")
        
        resp = requests.post(f"{BASE_URL}/api/device/upload", json=envelope, timeout=5)
        
        print_info(f"Response: {resp.status_code}")
        
        if resp.status_code != 200:
            print_pass("Server rejected invalid base64")
            return True, f"Rejected: {resp.text[:100]}"
        else:
            print_fail("Server should have rejected invalid base64")
            return False, f"Status {resp.status_code}"
    except Exception as e:
        print_fail(str(e))
        return False, str(e)

def test_missing_nonce() -> Tuple[bool, str]:
    """❌ TEST 8: Missing nonce field"""
    print_test("Missing Nonce Field")
    
    try:
        payload = create_payload()
        payload_json = json.dumps(payload, separators=(",", ":"))
        payload_b64 = base64.b64encode(payload_json.encode()).decode()
        
        mac = hmac.new(PSK.encode(), f"0.{payload_b64}".encode(), hashlib.sha256).hexdigest()
        
        # Omit nonce field
        envelope = {
            "payload": payload_b64,
            "mac": mac
            # "nonce" field intentionally omitted
        }
        
        print_info(f"Envelope fields: {list(envelope.keys())}")
        
        resp = requests.post(f"{BASE_URL}/api/device/upload", json=envelope, timeout=5)
        
        print_info(f"Response: {resp.status_code}")
        
        if resp.status_code != 200:
            print_pass("Server rejected envelope without nonce")
            return True, f"Rejected: {resp.text[:100]}"
        else:
            print_fail("Server should have rejected missing nonce")
            return False, f"Status {resp.status_code}"
    except Exception as e:
        print_fail(str(e))
        return False, str(e)

# ==================== Test Runner ====================

def main():
    print(f"{Colors.BOLD}{Colors.BLUE}{'='*60}")
    print(f"    EcoWatt Security Testing Suite")
    print(f"{'='*60}{Colors.RESET}")
    print(f"\nTarget: {BASE_URL}")
    print(f"PSK: {PSK}")
    print(f"Device: {DEVICE_ID}")
    
    tests: List[Tuple[str, callable]] = [
        ("Valid Message", test_valid_message),
        ("Bad HMAC", test_bad_hmac),
        ("Wrong PSK", test_wrong_psk),
        ("Replay Attack", test_replay_attack),
        ("Tampered Payload", test_tampered_payload),
        ("Missing MAC", test_missing_mac),
        ("Invalid Base64", test_invalid_base64),
        ("Missing Nonce", test_missing_nonce),
    ]
    
    results: List[Tuple[str, bool, str]] = []
    
    for name, test_func in tests:
        try:
            success, detail = test_func()
            results.append((name, success, detail))
        except Exception as e:
            print(f"  {Colors.RED}❌ EXCEPTION: {e}{Colors.RESET}")
            results.append((name, False, str(e)))
        
        time.sleep(0.2)
    
    # ==================== Summary ====================
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*60}")
    print(f"    Test Summary")
    print(f"{'='*60}{Colors.RESET}")
    
    passed = sum(1 for _, success, _ in results if success)
    failed = len(results) - passed
    
    for name, success, detail in results:
        status = f"{Colors.GREEN}✅ PASS{Colors.RESET}" if success else f"{Colors.RED}❌ FAIL{Colors.RESET}"
        print(f"{status:20s} {name:25s} {detail[:40]}")
    
    print(f"\n{Colors.BOLD}Results: {Colors.GREEN}{passed} passed{Colors.RESET}, {Colors.RED}{failed} failed{Colors.RESET}, {len(results)} total{Colors.RESET}")
    
    if failed == 0:
        print(f"{Colors.GREEN}✅ All security tests passed!{Colors.RESET}")
        return 0
    else:
        print(f"{Colors.RED}❌ Some tests failed{Colors.RESET}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
