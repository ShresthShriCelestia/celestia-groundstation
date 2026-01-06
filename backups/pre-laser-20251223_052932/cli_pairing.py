#!/usr/bin/env python3
"""
CLI Pairing Command for Terra Station
Allows local operators or SSH users to generate pairing codes for remote access.
"""
import argparse
import sys
import time
from pathlib import Path

# Add backend to path so we can import modules
sys.path.insert(0, str(Path(__file__).parent))

from pairing import pairing_manager


def main():
    parser = argparse.ArgumentParser(
        description="Terra Station Pairing Management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cli_pairing.py start                    # Start pairing mode (5 min timeout)
  python cli_pairing.py start --timeout 600      # Start with 10 min timeout
  python cli_pairing.py status                   # Check pairing status
  python cli_pairing.py stop                     # Stop pairing mode
  python cli_pairing.py list                     # List paired devices
  python cli_pairing.py unpair --all             # Unpair all devices
  python cli_pairing.py unpair --token TOKEN     # Unpair specific device

Security Notice:
  Only run this command on the Terra Station itself or via SSH.
  Pairing codes grant access to the control system.
        """)
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Start pairing command
    start_parser = subparsers.add_parser('start', help='Start pairing mode')
    start_parser.add_argument('--timeout', type=int, default=300,
                             help='Timeout in seconds (default: 300 = 5 minutes)')
    
    # Status command  
    status_parser = subparsers.add_parser('status', help='Check pairing system status')
    
    # Stop command
    stop_parser = subparsers.add_parser('stop', help='Stop pairing mode')
    
    # List devices command
    list_parser = subparsers.add_parser('list', help='List all paired devices')
    
    # Unpair command
    unpair_parser = subparsers.add_parser('unpair', help='Unpair devices')
    unpair_group = unpair_parser.add_mutually_exclusive_group(required=True)
    unpair_group.add_argument('--all', action='store_true', help='Unpair all devices')
    unpair_group.add_argument('--token', help='Unpair device with specific token')
    
    args = parser.parse_args()
    
    if args.command == 'start':
        start_pairing(args.timeout)
    elif args.command == 'status':
        show_status()
    elif args.command == 'stop':
        stop_pairing()
    elif args.command == 'list':
        list_devices()
    elif args.command == 'unpair':
        if args.all:
            unpair_all()
        else:
            unpair_device(args.token)
    else:
        parser.print_help()


def start_pairing(timeout_seconds: int):
    """Start pairing mode and display the pairing code"""
    if pairing_manager.is_pairing_active():
        print("⚠️  Pairing mode is already active!")
        show_status()
        return
    
    print(f"🔗 Starting pairing mode (timeout: {timeout_seconds//60} minutes)...")
    code = pairing_manager.start_pairing_mode(timeout_seconds)
    
    print()
    print("╔" + "═" * 48 + "╗")
    print("║" + " " * 16 + "PAIRING CODE" + " " * 19 + "║")
    print("║" + " " * 48 + "║")
    print(f"║{' ' * 21}{code:06d}{' ' * 21}║")
    print("║" + " " * 48 + "║")
    print(f"║  Valid for: {timeout_seconds//60} minutes{' ' * (27 - len(str(timeout_seconds//60)))}║")
    print("╚" + "═" * 48 + "╝")
    print()
    
    print("📋 Instructions for remote operator:")
    print(f"   1. Go to your Terra Station URL/admin")  
    print(f"   2. Enter pairing code: {code:06d}")
    print(f"   3. Complete device pairing")
    print()
    print("🔒 Security: This code grants access to Terra Station controls.")
    print("   Only share with authorized operators.")
    print()
    
    # Monitor pairing until timeout or success
    print("⏳ Waiting for device to pair... (Press Ctrl+C to cancel)")
    try:
        start_time = time.time()
        while pairing_manager.is_pairing_active():
            time.sleep(2)
            elapsed = time.time() - start_time
            remaining = max(0, timeout_seconds - elapsed)
            print(f"\r   Time remaining: {remaining//60:02.0f}:{remaining%60:02.0f} ", end="", flush=True)
        
        print(f"\n✅ Device paired successfully!")
        
    except KeyboardInterrupt:
        print(f"\n🛑 Pairing cancelled by user")
        pairing_manager.cancel_pairing_mode()


def show_status():
    """Show current pairing system status"""
    status = pairing_manager.get_status()
    
    print("📊 Terra Station Pairing Status")
    print("=" * 40)
    
    if status["pairing_active"]:
        expires_at = status["pairing_expires_at"]
        print(f"🟢 Pairing Mode: ACTIVE")
        print(f"   Expires: {expires_at}")
        print(f"   Code: {pairing_manager.pairing_code}")
    else:
        print(f"🔴 Pairing Mode: INACTIVE")
    
    print(f"📱 Paired Devices: {status['paired_device_count']}")
    
    if status["paired_devices"]:
        print("\nDevice List:")
        for i, device in enumerate(status["paired_devices"], 1):
            print(f"  {i}. {device['name']} ({device['type']})")
            print(f"     Access: {device['access_level']}")
            print(f"     Paired: {device['paired_at']}")
            print(f"     Last Seen: {device['last_seen']}")
    
    print()


def stop_pairing():
    """Stop pairing mode"""
    if not pairing_manager.is_pairing_active():
        print("ℹ️  Pairing mode is not active")
        return
    
    pairing_manager.cancel_pairing_mode()
    print("🛑 Pairing mode stopped")


def list_devices():
    """List all paired devices"""
    devices = pairing_manager.get_paired_devices()
    
    if not devices:
        print("📱 No devices currently paired")
        return
    
    print(f"📱 Paired Devices ({len(devices)})")
    print("=" * 50)
    
    for i, device in enumerate(devices, 1):
        print(f"{i}. {device.device_name}")
        print(f"   Type: {device.device_type}")
        print(f"   Access Level: {device.access_level}")
        print(f"   Paired: {device.paired_at}")
        print(f"   Last Seen: {device.last_seen}")
        print(f"   Token: {device.token[:16]}...")
        print()


def unpair_device(token: str):
    """Unpair a specific device"""
    if pairing_manager.unpair_device(token):
        print(f"✅ Device unpaired successfully")
    else:
        print(f"❌ Device not found or already unpaired")


def unpair_all():
    """Unpair all devices"""
    devices = pairing_manager.get_paired_devices()
    count = len(devices)
    
    if count == 0:
        print("ℹ️  No devices to unpair")
        return
    
    confirm = input(f"⚠️  This will unpair {count} device(s). Continue? (y/N): ")
    if confirm.lower() != 'y':
        print("❌ Operation cancelled")
        return
    
    pairing_manager.unpair_all()
    print(f"✅ All {count} devices unpaired")


if __name__ == "__main__":
    main()