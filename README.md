# tor-circuit-collector

`tor-circuit-collector` is a Python terminal application that maintains a small pool of Tor circuits, keeps idle circuits warm, and lets you choose a numbered circuit for an HTTP(S) download such as a PDF or EPUB.

The interface uses an in-place `curses` display, so status refreshes do not scroll the terminal. By default, it maintains **5 circuits** and refreshes the CLI every **5 seconds**.

## Files

```text
tor-circuit-collector.py      Main Python CLI application
tor-circuit-collector.sh      Bash runner that works from any current directory
tor-circuit-collector.json    JSON configuration file
README.md                     This file
```

## What it does

- Connects to Tor through the Tor ControlPort.
- Builds controller-owned Tor circuits.
- Keeps the configured number of circuits available.
- Rebuilds a circuit if Tor reports it as closed or failed.
- Runs periodic keep-alive requests on idle circuits.
- Lets the user press a circuit number and enter a download URL.
- Streams downloads through the selected circuit using Tor SOCKS credentials and stream attachment.
- Shows download progress in place without extra scrolling lines.
- Saves completed downloads to the configured output directory.

## Requirements

- Python 3.8 or newer is recommended.
- Tor must be installed and running locally or on a reachable host.
- Python packages:

```bash
python3 -m pip install stem "requests[socks]"
```

On Debian or Ubuntu systems, Tor can usually be installed with:

```bash
sudo apt update
sudo apt install tor
```

## Tor configuration

The script needs both a SOCKS port and a ControlPort.

A minimal `torrc` example:

```torrc
SocksPort 9050 IsolateSOCKSAuth
ControlPort 9051
CookieAuthentication 1
```

With this setup, the script can usually authenticate through Tor cookie authentication without a control password.

If you use password authentication instead, configure Tor accordingly and set `control_password` in `tor-circuit-collector.json`, or pass `--control-password` on the command line.

## Quick start

Put these files in the same directory:

```text
tor-circuit-collector.py
tor-circuit-collector.sh
tor-circuit-collector.json
```

Make the runner executable if needed:

```bash
chmod +x tor-circuit-collector.sh
```

Run it:

```bash
./tor-circuit-collector.sh
```

The runner resolves the Python script relative to its own path, so it can be invoked from any current working directory:

```bash
/path/to/tor-circuit-collector.sh
```

You can also place a symlink to `tor-circuit-collector.sh` somewhere on your `PATH`, as long as the real script target remains next to `tor-circuit-collector.py`.

## Using the terminal UI

Once running:

- Press `1` through `5` to select a circuit.
- Enter an absolute `http://` or `https://` URL.
- Press `Enter` to start the download.
- Press `Esc` while entering a URL to cancel the prompt.
- Press `q` to quit.

The UI shows each circuit's current Tor circuit ID, exit relay, age, keep-alive status, failure count, and download progress.

## Configuration

By default, the Python script loads:

```text
tor-circuit-collector.json
```

from the same directory as `tor-circuit-collector.py`.

You can use another config file with:

```bash
./tor-circuit-collector.sh --config /path/to/tor-circuit-collector.json
```

You can also set:

```bash
export TOR_CIRCUIT_COLLECTOR_CONFIG=/path/to/tor-circuit-collector.json
./tor-circuit-collector.sh
```

Command-line flags override JSON values.

### Suggested config

```json
{
  "tor": {
    "socks_host": "127.0.0.1",
    "socks_port": 9050,
    "control_host": "127.0.0.1",
    "control_port": 9051,
    "control_password": null
  },
  "collector": {
    "circuits": 5,
    "keepalive_url": "https://check.torproject.org/api/ip",
    "keepalive_interval": 60,
    "max_keepalive_failures": 2,
    "circuit_build_timeout": 90,
    "leave_circuits": false,
    "show_paths": false
  },
  "downloads": {
    "output_dir": "downloads",
    "keep_partials": false
  },
  "ui": {
    "status_interval": 5
  },
  "timeouts": {
    "connect_timeout": 20,
    "read_timeout": 30,
    "download_read_timeout": 120
  }
}
```

### Relative paths

If `downloads.output_dir` is relative, it is resolved relative to the config file location unless `--output-dir` is passed explicitly.

For example, this config entry:

```json
{
  "downloads": {
    "output_dir": "downloads"
  }
}
```

creates or uses a `downloads` directory beside the JSON config file.

## Command-line options

Run:

```bash
./tor-circuit-collector.sh --help
```

Common options:

```bash
./tor-circuit-collector.sh --config /path/to/config.json
./tor-circuit-collector.sh --output-dir ~/Downloads/tor
./tor-circuit-collector.sh --circuits 5
./tor-circuit-collector.sh --show-paths
./tor-circuit-collector.sh --keep-partials
```

Boolean options support both positive and negative forms:

```bash
./tor-circuit-collector.sh --show-paths
./tor-circuit-collector.sh --no-show-paths
./tor-circuit-collector.sh --leave-circuits
./tor-circuit-collector.sh --no-leave-circuits
```

## How circuit selection works

The script uses Tor's control interface to build circuits and temporarily sets:

```text
__LeaveStreamsUnattached=1
```

This lets the controller attach new SOCKS streams to the selected circuit. Each circuit slot uses distinct SOCKS credentials, and stream events are mapped back to the corresponding numbered slot.

On shutdown, the script attempts to restore the previous `__LeaveStreamsUnattached` value and close controller-built circuits unless `leave_circuits` is enabled.

## Downloads

Downloads are streamed in chunks and written first to a `.part` file. When a download completes, the `.part` file is atomically moved to the final filename.

If the server provides a `Content-Disposition` filename, that name is used. Otherwise, the filename is inferred from the URL path. Existing filenames are not overwritten; the script appends `-1`, `-2`, and so on.

By default, failed partial downloads are removed. To keep them:

```bash
./tor-circuit-collector.sh --keep-partials
```

or set:

```json
{
  "downloads": {
    "keep_partials": true
  }
}
```

## Troubleshooting

### `Could not connect/authenticate to Tor ControlPort`

Check that Tor is running and that `ControlPort` is enabled.

Example checks:

```bash
systemctl status tor
ss -ltnp | grep -E '9050|9051'
```

Also confirm that your `tor-circuit-collector.json` uses the same host and port values as Tor.

### Missing Python dependencies

Install the required packages:

```bash
python3 -m pip install stem "requests[socks]"
```

### Terminal too small

Use a terminal window at least 80 columns wide and 12 rows high.

### Downloads fail but circuits stay alive

Some servers block Tor exits, require JavaScript, require cookies, or reject automated clients. Try a different URL or a different document source.

### Circuits build slowly

Tor circuit construction can take time. Increase the build timeout if needed:

```json
{
  "collector": {
    "circuit_build_timeout": 120
  }
}
```

## Notes and limitations

- Only absolute `http://` and `https://` URLs are supported.
- The UI supports single-digit circuit selection, so `circuits` must be between 1 and 9.
- This tool does not make downloads anonymous by itself; your anonymity depends on Tor, your system configuration, destination behavior, document contents, and operational choices.
- Opening downloaded PDFs or EPUBs in external applications may leak information outside Tor if those applications fetch remote resources.
- Use this only for lawful downloads and with respect for the destination site's terms and rate limits.

## Example layout

```text
~/tools/tor-circuit-collector/
├── tor-circuit-collector.py
├── tor-circuit-collector.sh
├── tor-circuit-collector.json
├── README.md
└── downloads/
```

Run from anywhere:

```bash
~/tools/tor-circuit-collector/tor-circuit-collector.sh
```
