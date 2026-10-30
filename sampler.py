#!/usr/bin/env python3
"""fanmon sampler: read-only EC fan and temperature sampler.

Reads the EC RAM window at 0xFF70D500 (Acer Aspire R7-371T layout) plus the
sysfs thermal sensors and writes a JSON snapshot to /run/fanmon/status.json.
Needs root (/dev/mem); started by fanmon.service.
"""
import glob
import json
import mmap
import os
import time

ERAM = 0xFF70D500
PAGE = 0x1000
OUTDIR = '/run/fanmon'
OUT = os.path.join(OUTDIR, 'status.json')
INTERVAL = 1.0


def read_mem(base, length):
    off = base & ~(PAGE - 1)
    delta = base - off
    fd = os.open('/dev/mem', os.O_RDONLY)
    try:
        m = mmap.mmap(fd, delta + length, offset=off, access=mmap.ACCESS_READ)
        try:
            return bytes(m[delta:delta + length])
        finally:
            m.close()
    finally:
        os.close(fd)


def read_int(path):
    try:
        with open(path) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def read_str(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def hwmon_temps(name):
    """Temperature sensors of the hwmon device whose name matches."""
    out = []
    for hw in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
        if read_str(os.path.join(hw, 'name')) != name:
            continue
        for inp in sorted(glob.glob(os.path.join(hw, 'temp*_input'))):
            milli = read_int(inp)
            if milli is None:
                continue
            label = read_str(inp[: -len('_input')] + '_label') or os.path.basename(inp)
            out.append({'name': label, 'temp': milli / 1000.0})
    return out


def thermal_zones():
    out = []
    for zone in sorted(glob.glob('/sys/class/thermal/thermal_zone*')):
        milli = read_int(os.path.join(zone, 'temp'))
        if milli is None:
            continue
        name = read_str(os.path.join(zone, 'type')) or os.path.basename(zone)
        out.append({'name': name, 'temp': milli / 1000.0})
    return out


def snapshot():
    eram = read_mem(ERAM, 0x100)
    state = eram[0x55]
    return {
        'ts': time.time(),
        'fan': {
            'state': state,
            'temp': eram[0x56],
            'speed_lo': eram[0x5C],
            'speed_hi': eram[0x5D],
        },
        'cpu': hwmon_temps('coretemp'),
        'zones': thermal_zones(),
    }


def write_status(data):
    os.makedirs(OUTDIR, exist_ok=True)
    tmp = OUT + '.tmp'
    with open(tmp, 'w') as handle:
        json.dump(data, handle)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)


def main():
    print('fanmon sampler: writing {} every {:.0f}s'.format(OUT, INTERVAL), flush=True)
    while True:
        started = time.monotonic()
        try:
            data = snapshot()
        except Exception as exc:  # keep the error visible to the GUI
            data = {'ts': time.time(),
                    'error': '{}: {}'.format(type(exc).__name__, exc)}
        write_status(data)
        time.sleep(max(0.0, INTERVAL - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
