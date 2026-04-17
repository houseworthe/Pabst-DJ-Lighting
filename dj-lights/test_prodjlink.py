#!/usr/bin/env python3
"""
Test script to simulate Pro DJ Link events and verify lightd integration.

Sends test events to lightd socket to verify the protocol works.
"""

import json
import socket
import time

SOCKET_PATH = '/tmp/lightd.sock'

def send_event(event):
    """Send a Pro DJ Link event to lightd."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(SOCKET_PATH)
        
        msg = json.dumps(event) + '\n'
        sock.sendall(msg.encode())
        
        response = sock.recv(4096).decode().strip()
        print(f"✅ Sent: {event['type']}")
        print(f"   Response: {response}")
        
        sock.close()
        return True
    except Exception as e:
        print(f"❌ Failed to send event: {e}")
        return False


def main():
    print("=" * 60)
    print("🧪 Pro DJ Link Protocol Test")
    print("=" * 60)
    print(f"Target: {SOCKET_PATH}\n")
    
    # Test 1: Connection event
    print("1. Testing connection event...")
    send_event({
        'source': 'prodjlink',
        'type': 'connection',
        'timestamp': time.time(),
        'status': 'connected',
        'vcdj_number': 5,
    })
    time.sleep(0.5)
    
    # Test 2: Track load event
    print("\n2. Testing track load event...")
    send_event({
        'source': 'prodjlink',
        'type': 'track_load',
        'timestamp': time.time(),
        'deck': 1,
        'title': 'Losing It',
        'artist': 'Fisher',
        'album': '',
        'bpm': 128.0,
        'key': 'Am',
        'duration': 210,
    })
    time.sleep(0.5)
    
    # Test 3: Master deck change
    print("\n3. Testing master deck change...")
    send_event({
        'source': 'prodjlink',
        'type': 'master_change',
        'timestamp': time.time(),
        'master_deck': 1,
        'bpm': 128.0,
    })
    time.sleep(0.5)
    
    # Test 4: Deck update (playing)
    print("\n4. Testing deck update (playing)...")
    send_event({
        'source': 'prodjlink',
        'type': 'deck_update',
        'timestamp': time.time(),
        'deck': 1,
        'changes': {
            'play_state': 'playing',
            'beat': 1,
            'beat_count': 64,
        },
        'state': {
            'bpm': 128.0,
            'beat': 1,
            'beat_count': 64,
            'play_state': 'playing',
            'pitch': 1.0,
            'actual_pitch': 1.0,
            'key': 'Am',
            'loop_active': False,
            'on_air': True,
            'is_master': True,
            'title': 'Losing It',
            'artist': 'Fisher',
        }
    })
    time.sleep(0.5)
    
    # Test 5: Loop engaged
    print("\n5. Testing loop engaged...")
    send_event({
        'source': 'prodjlink',
        'type': 'deck_update',
        'timestamp': time.time(),
        'deck': 1,
        'changes': {
            'loop_active': True,
            'loop_start': 32.5,
            'loop_end': 40.5,
        },
        'state': {
            'bpm': 128.0,
            'beat': 2,
            'beat_count': 128,
            'play_state': 'playing',
            'pitch': 1.0,
            'actual_pitch': 1.0,
            'key': 'Am',
            'loop_active': True,
            'on_air': True,
            'is_master': True,
            'title': 'Losing It',
            'artist': 'Fisher',
        }
    })
    time.sleep(0.5)
    
    # Test 6: Disconnection
    print("\n6. Testing disconnection...")
    send_event({
        'source': 'prodjlink',
        'type': 'connection',
        'timestamp': time.time(),
        'status': 'disconnected',
    })
    
    print("\n" + "=" * 60)
    print("✅ Test complete!")
    print("Check lightd logs for event handling.")
    print("=" * 60)


if __name__ == '__main__':
    main()
