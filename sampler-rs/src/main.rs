use std::fmt::Write as FmtWrite;
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write as IoWrite};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const ERAM: u64 = 0xFF70_D500;
const ERAM_LEN: usize = 0x100;
const INTERVAL: Duration = Duration::from_secs(1);

struct Sensor {
    name: String,
    temp: f64,
}

fn read_eram() -> io::Result<[u8; ERAM_LEN]> {
    let mut device = File::open("/dev/mem")?;
    device.seek(SeekFrom::Start(ERAM))?;
    let mut bytes = [0; ERAM_LEN];
    device.read_exact(&mut bytes)?;
    Ok(bytes)
}

fn read_text(path: &Path) -> Option<String> {
    fs::read_to_string(path)
        .ok()
        .map(|value| value.trim().to_owned())
}

fn read_milli_celsius(path: &Path) -> Option<i64> {
    read_text(path)?.parse().ok()
}

fn sorted_paths(path: &Path) -> Vec<PathBuf> {
    let mut paths = match fs::read_dir(path) {
        Ok(entries) => entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .collect(),
        Err(_) => Vec::new(),
    };
    paths.sort();
    paths
}

fn coretemp_sensors() -> Vec<Sensor> {
    let mut sensors = Vec::new();
    for hwmon in sorted_paths(Path::new("/sys/class/hwmon")) {
        if read_text(&hwmon.join("name")).as_deref() != Some("coretemp") {
            continue;
        }

        for input in sorted_paths(&hwmon) {
            let Some(file_name) = input.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            let Some(label_base) = file_name.strip_suffix("_input") else {
                continue;
            };
            if !label_base.starts_with("temp") {
                continue;
            }
            let Some(milli) = read_milli_celsius(&input) else {
                continue;
            };
            let fallback = file_name.to_owned();
            let label = read_text(&hwmon.join(format!("{label_base}_label")))
                .filter(|value| !value.is_empty())
                .unwrap_or(fallback);
            sensors.push(Sensor {
                name: label,
                temp: milli as f64 / 1000.0,
            });
        }
    }
    sensors
}

fn thermal_zone_sensors() -> Vec<Sensor> {
    let mut sensors = Vec::new();
    for zone in sorted_paths(Path::new("/sys/class/thermal")) {
        let Some(basename) = zone.file_name().and_then(|name| name.to_str()) else {
            continue;
        };
        if !basename.starts_with("thermal_zone") {
            continue;
        }
        let Some(milli) = read_milli_celsius(&zone.join("temp")) else {
            continue;
        };
        let name = read_text(&zone.join("type"))
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| basename.to_owned());
        sensors.push(Sensor {
            name,
            temp: milli as f64 / 1000.0,
        });
    }
    sensors
}

fn push_json_string(json: &mut String, value: &str) {
    json.push('"');
    for character in value.chars() {
        match character {
            '"' => json.push_str("\\\""),
            '\\' => json.push_str("\\\\"),
            '\u{0008}' => json.push_str("\\b"),
            '\u{000C}' => json.push_str("\\f"),
            '\n' => json.push_str("\\n"),
            '\r' => json.push_str("\\r"),
            '\t' => json.push_str("\\t"),
            c if c <= '\u{001F}' => {
                write!(json, "\\u{:04x}", u32::from(c)).expect("writing to String cannot fail");
            }
            c => json.push(c),
        }
    }
    json.push('"');
}

fn push_sensors(json: &mut String, sensors: &[Sensor]) {
    json.push('[');
    for (index, sensor) in sensors.iter().enumerate() {
        if index != 0 {
            json.push(',');
        }
        json.push_str("{\"name\":");
        push_json_string(json, &sensor.name);
        write!(&mut *json, ",\"temp\":{}}}", sensor.temp).expect("writing to String cannot fail");
    }
    json.push(']');
}

fn snapshot(eram: &[u8; ERAM_LEN]) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64());
    let mut json = String::with_capacity(512);
    write!(
        &mut json,
        "{{\"ts\":{now},\"fan\":{{\"state\":{},\"temp\":{},\"speed_lo\":{},\"speed_hi\":{}}},\"cpu\":",
        eram[0x55], eram[0x56], eram[0x5C], eram[0x5D]
    )
    .expect("writing to String cannot fail");
    push_sensors(&mut json, &coretemp_sensors());
    json.push_str(",\"zones\":");
    push_sensors(&mut json, &thermal_zone_sensors());
    json.push('}');
    json
}

fn error_snapshot(error: &io::Error) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64());
    let mut json = String::with_capacity(128);
    write!(&mut json, "{{\"ts\":{now},\"error\":").expect("writing to String cannot fail");
    push_json_string(&mut json, &error.to_string());
    json.push('}');
    json
}

fn write_line(output: &mut impl IoWrite, line: &str) -> io::Result<()> {
    output.write_all(line.as_bytes())?;
    output.write_all(b"\n")?;
    output.flush()
}
fn is_client_disconnect(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::BrokenPipe
            | io::ErrorKind::ConnectionReset
            | io::ErrorKind::ConnectionAborted
            | io::ErrorKind::NotConnected
    )
}

fn run() -> io::Result<()> {
    let stdin = io::stdin();
    let mut input = BufReader::new(stdin.lock());
    let mut command = String::new();
    if input.read_line(&mut command)? == 0
        || command.trim_end_matches(|character| character == '\r' || character == '\n') != "START"
    {
        return Ok(());
    }
    drop(input);

    let stdout = io::stdout();
    let mut output = stdout.lock();
    loop {
        let started = Instant::now();
        let line = match read_eram() {
            Ok(eram) => snapshot(&eram),
            Err(error) => {
                let line = error_snapshot(&error);
                match write_line(&mut output, &line) {
                    Ok(()) => return Ok(()),
                    Err(write_error) if is_client_disconnect(&write_error) => return Ok(()),
                    Err(write_error) => return Err(write_error),
                }
            }
        };
        match write_line(&mut output, &line) {
            Ok(()) => thread::sleep(INTERVAL.saturating_sub(started.elapsed())),
            Err(error) if is_client_disconnect(&error) => return Ok(()),
            Err(error) => return Err(error),
        }
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("fanmon-sampler: {error}");
        std::process::exit(1);
    }
}
