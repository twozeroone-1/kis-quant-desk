# Project Memory

## KIS Strategy Builder Runtime Split

- `http://ww.tailea9a3f.ts.net:8081` is the Strategy Builder vps/mock-investing endpoint.
- `http://ww.tailea9a3f.ts.net:8083` is the Strategy Builder prod/live-investing endpoint.
- `http://ww.tailea9a3f.ts.net:8082` remains the backtester endpoint.
- The vps and prod builder backends run as separate Docker services, with separate token, mode, and runtime directories.
- `KIS_LOCK_MODE` is enabled so `8081` rejects prod login and `8083` rejects vps login.
- `open-trading-agent.service` is explicitly pinned to `8081` for vps risk monitoring.
- Live/prod orders must use `8083` and still require explicit user confirmation before submission.
