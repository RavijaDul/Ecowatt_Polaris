#!/usr/bin/env python3
"""
Device Security Testing via Real Server
========================================

Uses the actual app.py server (running on 192.168.8.195:5000) to send 
intentionally malformed config_update messages to test device security.

Usage:
    python test/34_test_security_via_real_server.py <scenario> [--sampling N] [--registers REG1,REG2,...]

Scenarios:
    valid       - Send valid config_update (should be accepted)
    bad_hmac    - Send with corrupted MAC (should be rejected)
    wrong_psk   - Sign with different PSK (should be rejected)
    replay      - Send with old nonce (should be rejected)
    invalid_b64 - Invalid base64 encoding (should be rejected)
    missing_mac - Omit MAC field (should be rejected)

Example:
    python test/34_test_security_via_real_server.py valid --sampling 10 --registers VAC1,IAC1
    python test/34_test_security_via_real_server.py bad_hmac
    python test/34_test_security_via_real_server.py replay
"""

import json
import os
import sys
import argparse
import requests
from pathlib import Path

# Configuration
SERVER_URL = "http://192.168.8.195:5000"
LOG_DIR = "server/logs"

# ANSI colors
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

def print_title(msg):
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}{msg}{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")

def print_success(msg):
    print(f"{GREEN}✅ {msg}{RESET}")

def print_info(msg):
    print(f"{YELLOW}ℹ️  {msg}{RESET}")

def print_error(msg):
    print(f"{RED}❌ {msg}{RESET}")

def main():
    parser = argparse.ArgumentParser(
        description="Test device security using real server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument('scenario', 
                       choices=['valid', 'bad_hmac', 'wrong_psk', 'replay', 'invalid_b64', 'missing_mac'],
                       help='Security test scenario')
    parser.add_argument('--sampling', type=int, default=10,
                       help='Sampling interval in seconds (default: 10)')
    parser.add_argument('--registers', default='VAC1,IAC1,FAC1',
                       help='Comma-separated register list (default: VAC1,IAC1,FAC1)')
    parser.add_argument('--server', default=SERVER_URL,
                       help=f'Server URL (default: {SERVER_URL})')
    
    args = parser.parse_args()
    
    print_title(f"Security Test: {args.scenario.upper()}")
    
    # Ensure logs directory exists
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    
    # Build config_update payload
    registers = [r.strip() for r in args.registers.split(',')]
    config_update = {
        "config_update": {
            "sampling_interval": args.sampling,
            "registers": registers
        }
    }
    
    # Write config_update.json
    cfg_file = os.path.join(LOG_DIR, "config_update.json")
    with open(cfg_file, 'w') as f:
        json.dump(config_update, f, indent=2)
    print_success(f"Created {cfg_file}")
    print_info(f"Config: sampling={args.sampling}s, registers={registers}")
    
    # Write test_security_mode.txt to trigger test mode
    test_file = os.path.join(LOG_DIR, "test_security_mode.txt")
    with open(test_file, 'w') as f:
        f.write(args.scenario)
    print_success(f"Created {test_file} with scenario: {args.scenario}")
    
    # Instructions
    print(f"\n{BOLD}Next Steps:{RESET}")
    print(f"1. Device will poll server at: {args.server}/api/device/upload")
    print(f"2. Server will respond with config_update ({args.scenario})")
    print(f"3. Check device logs in ESP-IDF Monitor:")
    
    if args.scenario == 'valid':
        print(f"   {GREEN}✅ Expected: 'queued config: sampling=...'${RESET}")
    else:
        print(f"   {RED}❌ Expected: 'bad HMAC or replay in cloud reply — ignored'${RESET}")
    
    print(f"\n{YELLOW}Waiting for device to poll (typically ~15 seconds)...${RESET}")
    print(f"{YELLOW}Watch ESP-IDF Monitor for device logs.${RESET}\n")

if __name__ == "__main__":
    main()
