---
name: laptop-ec-fan-probe
description: "Read-only Linux investigation of laptop fan control (EC registers, fan levels/thresholds, control-interface checklist). Use when asked how to control, trigger, or verify a laptop fan."
---

# Laptop EC fan investigation (read-only)

Goal: answer "how do I make the fan spin / can I control it" WITHOUT writing to the EC.

## 1. Identify hardware and surfaces
- `cat /sys/class/dmi/id/{sys_vendor,product_name,bios_version}`
- hwmon: `ls /sys/class/hwmon/*/name`; look for `pwm*`/`fan*` files (usually absent on Acer).
- ACPI fan: `/proc/acpi/fan`, `ls /sys/bus/acpi/devices | grep PNP0C0B`, `cat /sys/class/thermal/cooling_device*/type`.
- Vendor bits: `/sys/module/acer_wmi/parameters/`, `modinfo acerhdf` (DMI alias whitelist), `nbfc config -l`, `/sys/firmware/acpi/platform_profile`.

## 2. Read the EC (root required)
- Port space: custom acpi_ec module exposes /dev/ec; read via pread(fd,1,addr) for 0x00-0xFF. Alt: `modprobe ec_sys` + `/sys/kernel/debug/ec/ec0/io`.
- Acer EC-RAM windows via /dev/mem read-only mmap: ERAM 0xFF70D500, EROM 0xFF70D900, EMBX 0xFF70DA00 (0x100 each).
- Broadwell-era Aspire mapping (R7-371T verified): ERAM+0x55 = fan level (0x02 idle / 0x03 low / 0x04 medium / 0x05 high observed; 0-8 scale per nbfc Acer E5-471G config); +0x56/+0x58 = EC temps (~coretemp ±1); +0x5C/+0x5D = speed pair (0x5D high byte tracks level, 0x5C low byte is a counter); port 0x00 = CTMP (≈+0x56).
- pkexec prompt appears on user's screen; for long runs: `pkexec sh -c 'nohup python3 /tmp/fanprobe.py 1800 2 /tmp/mon.csv &'`; chmod 0644 the CSV afterwards (root-owned).

## 3. Find thresholds
- Sample 2s: EC port dump + ERAM window + thermal zones + battery; 30 min catches all levels.
- Detect state-byte transitions, correlate with EC temp: they come in hysteresis pairs (3->4 ~68 rising / 4->3 ~61 falling; 4->5 ~73 / 5->4 ~67 on R7-371T).
- Fan spins by die temperature only; CPU vs GPU load is irrelevant (same die). To trigger without CPU load: iGPU load (VAAPI/GL).

## 4. Control-interface checklist (mostly dead ends on old Acer)
- No hwmon pwm -> no fancontrol/pwmconfig. No ACPI fan -> no /proc/acpi/fan.
- acer-wmi fan WMI methods exist only with Nitro/Predator quirks (cpu_fans/gpu_fans); check source for the model.
- acerhdf: Atom-era whitelist; force_product exists but writes model-specific registers -> risky.
- nbfc: sibling configs use port-space RegisterWrite (e.g. 0x55) - verify the register actually moves on the target machine first; on R7-371T port 0x55 reads 0x00, so they don't apply.
- Raw EC writes (/dev/ec, /dev/mem, debugfs io): unsupported/untested; only with explicit user request + understanding of risk. NEVER write during an investigation.

## 5. Reporting
- State plainly: temperature-driven with hysteresis; give thresholds; list dead-end control paths with evidence; keep artifacts (CSV, dumps, script paths).
