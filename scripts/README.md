# Threshold Monitor Script

## Overview

The `threshold_monitor.py` script monitors SPX price against configurable put/call thresholds and automatically unpauses bots via webhook when thresholds are crossed.

## Features

- **JSON Configuration**: Easy-to-edit JSON config file
- **State Persistence**: Tracks triggered bots to prevent duplicate calls
- **Direction-Aware**: Only triggers on correct crossing direction (puts below, calls above)
- **0 DTE Pricing**: Uses 0 days-to-expiration option chain for accurate SPX pricing
- **Error Handling**: Robust error handling with retries and logging

## Configuration

### 1. Create Config File

Copy the example config and customize:

```bash
cp config/threshold_config.json.example config/threshold_config.json
```

Edit `config/threshold_config.json`:

```json
{
  "webhook_url": "http://localhost:5000/api/webhook/unpause-bot",
  "check_interval_seconds": 5,
  "token_path": "/app/token.json",
  "state_file": "/app/data/threshold_state.json",
  "symbol": "SPX",
  "thresholds": {
    "puts": [
      {"level": 6694, "bot_name": "putbot1"},
      {"level": 6663, "bot_name": "putbot2"}
    ],
    "calls": [
      {"level": 6820, "bot_name": "callbot1"},
      {"level": 6850, "bot_name": "callbot2"}
    ]
  }
}
```

### 2. Configure Docker Volume Mounts

Update `docker-compose.yml` to mount the config directory:

```yaml
volumes:
  - ./token.json:/app/token.json
  - ./src:/app/src
  - ./config:/app/config  # Add this line
  - ./data:/app/data       # Add this line for state persistence
```

## Running the Script

### Inside Docker Container

```bash
# Enter the container
docker exec -it looptrader-web bash

# Run the script
python scripts/threshold_monitor.py --config /app/config/threshold_config.json
```

### As a Background Service

Add to docker-compose.yml as a separate service:

```yaml
services:
  threshold-monitor:
    build: .
    volumes:
      - ./token.json:/app/token.json
      - ./config:/app/config
      - ./data:/app/data
    env_file:
      - .env
    environment:
      - SCHWAB_API_KEY=${SCHWAB_API_KEY}
      - SCHWAB_APP_SECRET=${SCHWAB_APP_SECRET}
    command: python scripts/threshold_monitor.py --config /app/config/threshold_config.json
    restart: unless-stopped
    networks:
      - looptrader-pro_default
```

## How It Works

1. **Price Monitoring**: Fetches SPX price from 0 DTE option chain every N seconds (configurable)
2. **Threshold Detection**: 
   - **Puts**: Triggers when price crosses BELOW threshold (e.g., price drops from 6700 to 6680, triggers putbot1 at 6694)
   - **Calls**: Triggers when price crosses ABOVE threshold (e.g., price rises from 6800 to 6830, triggers callbot1 at 6820)
3. **State Tracking**: Maintains list of triggered bots in `threshold_state.json`
4. **Webhook Calls**: Calls webhook endpoint to unpause bot when threshold is crossed
5. **Duplicate Prevention**: Skips bots that have already been triggered

## State File

The state file (`threshold_state.json`) is automatically created and persists:

```json
{
  "triggered_bots": ["putbot1"],
  "last_price": 6700.0,
  "last_check": "2024-01-01T12:00:00Z"
}
```

To reset and allow bots to be triggered again, delete or edit the state file:

```bash
# Reset all triggers
rm data/threshold_state.json

# Or manually edit to remove specific bots
```

## Logging

The script logs to stdout with timestamps:
- INFO: Normal operations, threshold crossings, webhook calls
- WARNING: API failures, missing data
- ERROR: Critical errors, webhook failures
- DEBUG: Detailed price checks (enable with logging level)

## Troubleshooting

### Script can't find config file
- Ensure config directory is mounted in docker-compose.yml
- Check file path matches `--config` argument

### Can't connect to Schwab API
- Verify `token.json` exists and is valid
- Check `SCHWAB_API_KEY` and `SCHWAB_APP_SECRET` environment variables
- Ensure token hasn't expired

### Webhook calls failing
- Verify webhook URL is correct (should be accessible from container)
- Check that looptrader-web service is running
- Verify bot names match exactly (case-insensitive)

### State file not persisting
- Ensure data directory is mounted as a volume
- Check file permissions in container

## Example Usage

1. **Initial Setup**:
   ```bash
   # Create config from example
   cp config/threshold_config.json.example config/threshold_config.json
   
   # Edit config with your thresholds and bot names
   nano config/threshold_config.json
   ```

2. **Start Monitoring**:
   ```bash
   docker exec -d looptrader-web python scripts/threshold_monitor.py --config /app/config/threshold_config.json
   ```

3. **Check Logs**:
   ```bash
   docker logs -f looptrader-web
   ```

4. **View State**:
   ```bash
   cat data/threshold_state.json
   ```

