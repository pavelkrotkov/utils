# Nest Wifi monitor (macOS)

`nest_wifi_monitor.py` records this Mac's gateway, ping and DNS observations plus
Google/Nest Wifi cloud telemetry. It polls the Google Wifi endpoints for network
status, clients, traffic, saved speed-test results, mesh results and backhaul.
It never starts a speed test or changes router settings. The API and Android
authentication flow are unofficial and may stop working if Google changes them.

The script uses [uv](https://docs.astral.sh/uv/) and PEP 723 dependencies. No
manual virtual-environment setup is needed. Choose a **private data directory
outside the Git checkout** for logs and runtime files:

```bash
uv run ./nest_wifi_monitor.py --data-dir "$HOME/private/nest-wifi" auth
uv run ./nest_wifi_monitor.py --data-dir "$HOME/private/nest-wifi" snapshot
uv run ./nest_wifi_monitor.py --data-dir "$HOME/private/nest-wifi" status
uv run ./nest_wifi_monitor.py --data-dir "$HOME/private/nest-wifi" start
uv run ./nest_wifi_monitor.py --data-dir "$HOME/private/nest-wifi" stop
```

`auth` is interactive. Sign in at Google's EmbeddedSetup page, then copy the
`oauth_token` cookie **Value** from browser developer tools into the hidden
Terminal prompt. Do not paste it into a command, chat or file. The reusable
credential is stored in this tool's macOS Keychain item, not in the data
directory. The script requests Google's `accesspoints` scope. The credential
is more powerful than a read-only token; the program limits itself to GETs.

`snapshot` makes one observation. `start` launches a detached poll every five
minutes; `stop` terminates it. `--local-only` skips the Google API. The data
directory receives `logs/YYYY-MM-DD.jsonl` and `runtime/` lock/output files.
These observations can contain private device labels, MAC addresses, IPs and
home-network information; keep the directory private and out of Git. Returned
speed-test values carry their original timestamps and may be old.

Run checks with:

```bash
uv run pytest test_nest_wifi_monitor.py
uv run ruff check nest_wifi_*.py test_nest_wifi_monitor.py
uv run ruff format --check nest_wifi_*.py test_nest_wifi_monitor.py
```

The offline tests enforce cyclomatic complexity at most 8 and maintainability
index above 50 for each `nest_wifi_*.py` source file using Radon. They also
check redaction, endpoint selection and detached process wiring.
