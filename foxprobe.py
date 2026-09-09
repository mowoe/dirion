"""Diagnose 'Could not initialize bootsequence' from foxflasher.py.

Usage:
    python foxprobe.py --list           # show all serial ports + VID/PID
    python foxprobe.py -p COM5          # probe the STM32 bootloader
    python foxprobe.py -p COM5 --listen # just listen, no boot/reset (is the MCU alive?)
"""
import argparse
import sys
import time

import serial
import serial.tools.list_ports

ACK, NACK, INIT = 0x79, 0x1F, 0x7F


def list_ports():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found at all.")
        return
    for p in ports:
        vid = "%04X" % p.vid if p.vid is not None else "----"
        pid = "%04X" % p.pid if p.pid is not None else "----"
        tag = "  <-- foxBMS FTDI" if (vid, pid) == ("0403", "6015") else ""
        print("{:<10} VID:PID={}:{}  SER={}  {}{}".format(
            p.device, vid, pid, p.serial_number, p.description, tag))


def open_port(port, parity=serial.PARITY_EVEN):
    """Bootloader link is 8E1 (AN3155); the application UART is 8N1."""
    return serial.Serial(port=port, baudrate=115200, bytesize=8,
                         parity=parity, stopbits=1,
                         xonxoff=0, rtscts=0, dsrdtr=0, timeout=1)


def listen(port, seconds=15):
    """DTR low = normal boot, so the application runs. With
    BUILD_MODULE_ENABLE_COM=1 the firmware printfs a startup banner at 115200 8N1."""
    with open_port(port, serial.PARITY_NONE) as s:
        s.dtr = False
        s.rts = True; time.sleep(0.5); s.rts = False
        print("Reset into APPLICATION mode, listening %ds..." % seconds)
        end = time.time() + seconds
        got = b""
        while time.time() < end:
            chunk = s.read(256)
            if chunk:
                sys.stdout.write(chunk.decode("ascii", "replace"))
                sys.stdout.flush()
                got += chunk
        print("\n--- received %d bytes ---" % len(got))
        if not got:
            print("-> silent: dies before BOOT_Init/COM_StartupInfo, or this UART")
            print("   is not routed to the FTDI. Check the LEDs too.")
        elif got.count(b"System starting") > 1:
            print("-> banner repeated: reset loop (watchdog?). Read the RCC CSR value.")
        else:
            print("-> reached COM_StartupInfo; it dies later than early init.")


def probe(port, tries):
    with open_port(port) as s:
        print("pyserial %s, opened %s @115200 8E1" % (serial.__version__, port))
        for i in range(1, tries + 1):
            s.dtr = True                     # BOOT pin high
            s.rts = True; time.sleep(0.5)    # assert reset
            s.rts = False; time.sleep(0.5)   # release -> enters bootloader
            s.reset_input_buffer()
            s.write(bytes([INIT]))
            s.flush()
            r = s.read(1)
            if not r:
                print("try %d: no answer (timeout)" % i)
                continue
            b = r[0]
            extra = s.read(16)
            if b == ACK:
                print("try %d: ACK 0x79  -> bootloader is alive, foxflasher should work" % i)
                s.dtr = False; s.rts = True; time.sleep(0.3); s.rts = False
                return 0
            if b == NACK:
                print("try %d: NACK 0x1F -> bootloader answered but refused init" % i)
                return 0
            print("try %d: got 0x%02X extra=%s -> unexpected byte on the line" % (i, b, extra.hex()))
        print("\nNo ACK after %d tries." % tries)
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port")
    ap.add_argument("-n", "--tries", type=int, default=5)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--listen", action="store_true")
    a = ap.parse_args()
    if a.list:
        list_ports(); return 0
    if not a.port:
        ap.error("give -p COMx (or --list)")
    if a.listen:
        listen(a.port); return 0
    return probe(a.port, a.tries)


if __name__ == "__main__":
    sys.exit(main())
