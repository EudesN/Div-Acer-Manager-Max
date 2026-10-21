#!/usr/bin/env python3
"""
KeyboardMonitor - Simple keyboard monitor that launches GUI as regular user
"""

import logging
import os
import pwd
import re
import select
import struct
import subprocess
import threading
import time
from pathlib import Path

# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
# using the native C layout (24 bytes on 64-bit, 16 bytes on 32-bit).
EVENT_FORMAT = 'llHHi'
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EVENTS_PER_READ = 64

# Event types and codes
EV_KEY = 1
KEY_PRESS = 1
TARGET_KEYCODE = 425

class KeyboardMonitor:
    def __init__(self, target_keycode=TARGET_KEYCODE, command_to_run="/opt/damx/gui/DivAcerManagerMax", logger=None):
        self.target_keycode = target_keycode
        self.command_to_run = command_to_run
        self.running = False
        self.device_path = None
        self.monitor_thread = None
        self.log = logger or logging.getLogger("KeyboardMonitor")
        
    def find_keyboard_device(self):
        """Find the keyboard input device"""
        try:
            devices_path = Path("/proc/bus/input/devices")
            if not devices_path.exists():
                self.log.error("Cannot access /proc/bus/input/devices")
                return None
                
            with open(devices_path, 'r') as f:
                content = f.read()
                
            devices = content.split('\n\n')
            
            for device in devices:
                lines = device.strip().split('\n')
                if not lines:
                    continue
                    
                is_keyboard = False
                event_num = None
                
                for line in lines:
                    line = line.strip()
                    if 'keyboard' in line.lower():
                        is_keyboard = True
                    elif line.startswith('H:') and 'event' in line:
                        match = re.search(r'event(\d+)', line)
                        if match:
                            event_num = match.group(1)
                
                if is_keyboard and event_num:
                    device_path = f"/dev/input/event{event_num}"
                    if os.path.exists(device_path):
                        self.log.info(f"Found keyboard device: {device_path}")
                        return device_path
                        
        except Exception as e:
            self.log.error(f"Error finding keyboard device: {e}")
            
        return None
    
    def execute_command(self):
        """Execute the GUI command using systemd-run for proper user context"""
        try:
            # Get the current desktop user (most reliable method)
            user = os.environ.get('SUDO_USER') or self.get_console_user()
            if not user:
                self.log.error("Could not determine user to run command")
                return False

            # The session bus belongs to the desktop user, not to root.
            try:
                pw = pwd.getpwnam(user)
            except KeyError:
                self.log.error(f"Unknown user {user!r}; cannot launch GUI")
                return False

            env = {
                'DISPLAY': ':0',
                'XAUTHORITY': os.path.join(pw.pw_dir, '.Xauthority'),
                'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{pw.pw_uid}/bus'
            }

            # Try running as the user with proper environment
            cmd = [
                'sudo', '-u', user,
                'env',
                f'DISPLAY={env["DISPLAY"]}',
                f'XAUTHORITY={env["XAUTHORITY"]}',
                f'DBUS_SESSION_BUS_ADDRESS={env["DBUS_SESSION_BUS_ADDRESS"]}',
                self.command_to_run
            ]
            
            subprocess.Popen(cmd, 
                            stdout=subprocess.DEVNULL, 
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
            self.log.info(f"Executed as user {user}: {self.command_to_run}")
            return True
            
        except Exception as e:
            self.log.error(f"Failed to execute command: {e}")
            return False
    
    def get_console_user(self):
        """Get the user currently logged into the console"""
        try:
            # Check who is on the console
            result = subprocess.run(['who'], capture_output=True, text=True)
            for line in result.stdout.splitlines():
                if 'console' in line or ':0' in line:
                    return line.split()[0]
            
            # Fallback to first user in who output
            if result.stdout.strip():
                return result.stdout.splitlines()[0].split()[0]

        except Exception:
            pass
        return None
    
    def _process_event(self, data):
        _, _, event_type, code, value = struct.unpack(EVENT_FORMAT, data)
        if event_type == EV_KEY and code == self.target_keycode and value == KEY_PRESS:
            self.log.info(f"Target keycode {self.target_keycode} pressed!")
            self.execute_command()

    def monitor_events(self):
        """Monitor keyboard events.

        The device is read unbuffered (os.read on the raw fd, in multiples of
        the input_event size) so select() always reflects what is unread;
        a buffered file object would swallow queued events into its buffer.
        """
        if not self.device_path:
            self.log.error("No device path set")
            return

        fd = None
        try:
            fd = os.open(self.device_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            self.log.info(f"Monitoring {self.device_path} for keycode {self.target_keycode}")
            pending = b""

            while self.running:
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue
                try:
                    data = os.read(fd, EVENT_SIZE * EVENTS_PER_READ)
                except BlockingIOError:
                    continue
                if not data:
                    self.log.warning(f"{self.device_path} closed")
                    break

                pending += data
                usable = len(pending) - (len(pending) % EVENT_SIZE)
                for off in range(0, usable, EVENT_SIZE):
                    self._process_event(pending[off:off + EVENT_SIZE])
                pending = pending[usable:]

        except PermissionError:
            self.log.error(f"Permission denied accessing {self.device_path}. Run as root.")
        except Exception as e:
            self.log.error(f"Error monitoring events: {e}")
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def start_monitoring(self):
        """Start monitoring"""
        if self.running:
            return False
            
        self.device_path = self.find_keyboard_device()
        if not self.device_path:
            return False
            
        self.running = True
        self.monitor_thread = threading.Thread(target=self.monitor_events, daemon=True)
        self.monitor_thread.start()
        
        self.log.info("Keyboard monitoring started")
        return True
    
    def stop_monitoring(self):
        """Stop monitoring"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=2.0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, 
                       format='%(asctime)s - %(levelname)s - %(message)s')
    
    monitor = KeyboardMonitor()
    
    if monitor.start_monitoring():
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            monitor.stop_monitoring()
    else:
        print("Failed to start monitoring")