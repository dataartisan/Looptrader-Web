#!/usr/bin/env python3
"""
SPX Threshold Monitor Script

Monitors SPX price against configurable put/call thresholds and automatically
unpauses bots via webhook when thresholds are crossed.

Features:
- JSON-based configuration
- State persistence to prevent duplicate triggers
- Direction-aware threshold detection
- 0 DTE option chain for accurate SPX pricing
"""

import os
import sys
import json
import time
import logging
import requests
import signal
import atexit
from datetime import date, datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path

# Import timezone support
try:
    from zoneinfo import ZoneInfo
    NYSE_TZ = ZoneInfo("America/New_York")
except ImportError:
    # Fallback for Python < 3.9
    try:
        import pytz
        NYSE_TZ = pytz.timezone("America/New_York")
    except ImportError:
        raise ImportError("Either zoneinfo (Python 3.9+) or pytz is required")

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

try:
    import schwab
    from schwab.auth import client_from_token_file
except ImportError:
    print("Error: schwab-py library not found. Install with: pip install schwab-py")
    sys.exit(1)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('threshold_monitor')


class ThresholdMonitor:
    """Monitors SPX price and triggers bot unpause via webhook when thresholds are crossed."""
    
    def __init__(self, config_path: str):
        """Initialize monitor with configuration file."""
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self.state_file = self.config['state_file']
        self.pid_file = self.config.get('pid_file', '/app/data/threshold_monitor.pid')
        self.state = self._load_state()
        self.schwab_client = None
        self._init_schwab_client()
        self._running = True
        self._last_trigger = None
        # Track if this is the first check after startup (to trigger on existing conditions)
        self._first_check = True
        
        # Check if today is a trading day (using EST/ET timezone)
        today = self._get_today_est()
        self._is_trading_day_flag = not self._is_market_holiday(today)
        self._next_trading_day = self._get_next_trading_day(today) if not self._is_trading_day_flag else today
        
        # Reset triggered bots if it's a new trading day
        self._reset_triggered_bots_if_new_day(today)
        
        if not self._is_trading_day_flag:
            logger.warning(f"⚠️  Started on non-trading day ({today}). Monitor will skip all checks until next trading day ({self._next_trading_day})")
        else:
            logger.info(f"✓ Started on trading day ({today})")
        
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        atexit.register(self._cleanup_pid_file)
        
        # Sort thresholds for efficient checking
        self.put_thresholds = sorted(
            self.config['thresholds']['puts'],
            key=lambda x: x['level'],
            reverse=True  # Highest first
        )
        self.call_thresholds = sorted(
            self.config['thresholds']['calls'],
            key=lambda x: x['level']
        )  # Lowest first
        
        logger.info(f"Initialized with {len(self.put_thresholds)} put thresholds and {len(self.call_thresholds)} call thresholds")
        triggered_bots = self.state.get('triggered_bots', [])
        triggered_date = self.state.get('triggered_bots_date')
        if triggered_bots and triggered_date:
            logger.info(f"Already triggered bots today ({triggered_date}): {triggered_bots}")
        else:
            logger.info("No bots triggered today")
    
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from JSON file."""
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            
            # Validate required fields
            required_fields = ['webhook_url', 'thresholds', 'state_file', 'token_path']
            for field in required_fields:
                if field not in config:
                    raise ValueError(f"Missing required config field: {field}")
            
            # Set defaults
            config.setdefault('check_interval_seconds', 300)  # Default: 5 minutes (300 seconds)
            config.setdefault('symbol', 'SPX')
            config.setdefault('pid_file', '/app/data/threshold_monitor.pid')
            
            logger.info(f"Loaded configuration from {config_path}")
            return config
        except FileNotFoundError:
            logger.error(f"Config file not found: {config_path}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            raise
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            raise
    
    def _load_state(self) -> Dict:
        """Load state from file, creating default if it doesn't exist."""
        state_file = Path(self.config['state_file'])
        
        # Create directory if it doesn't exist
        state_file.parent.mkdir(parents=True, exist_ok=True)
        
        if state_file.exists():
            try:
                with open(state_file, 'r') as f:
                    state = json.load(f)
                logger.info(f"Loaded state from {state_file}")
                return state
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Error loading state file, creating new one: {e}")
        
        # Create default state
        default_state = {
            'triggered_bots': [],
            'triggered_bots_date': None,  # Date (YYYY-MM-DD) when bots were triggered
            'last_price': None,
            'last_check': None
        }
        self._save_state(default_state)
        return default_state
    
    def _save_state(self, state: Optional[Dict] = None) -> None:
        """Save state to file."""
        if state is None:
            state = self.state
        
        state_file = Path(self.config['state_file'])
        state_file.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2)
            logger.debug(f"Saved state to {state_file}")
        except IOError as e:
            logger.error(f"Error saving state: {e}")
    
    def _remove_threshold(self, level: float, threshold_type: str) -> None:
        """
        Remove a threshold level from the configuration after it triggers.
        
        Args:
            level: The threshold level to remove
            threshold_type: 'put' or 'call'
        """
        try:
            threshold_list = self.config['thresholds'][f'{threshold_type}s']
            
            # Find and remove the threshold with matching level
            original_count = len(threshold_list)
            self.config['thresholds'][f'{threshold_type}s'] = [
                t for t in threshold_list if t['level'] != level
            ]
            removed_count = original_count - len(self.config['thresholds'][f'{threshold_type}s'])
            
            if removed_count > 0:
                # Save updated config to file
                config_file = Path(self.config_path)
                config_file.parent.mkdir(parents=True, exist_ok=True)
                with open(config_file, 'w') as f:
                    json.dump(self.config, f, indent=2)
                
                # Reload thresholds lists
                self.put_thresholds = sorted(
                    self.config['thresholds']['puts'],
                    key=lambda x: x['level'],
                    reverse=True  # Highest first
                )
                self.call_thresholds = sorted(
                    self.config['thresholds']['calls'],
                    key=lambda x: x['level']
                )  # Lowest first
                
                logger.info(f"✅ Removed {threshold_type.upper()} threshold level ${level:.2f} from configuration. "
                          f"Remaining {threshold_type}s: {len(self.config['thresholds'][f'{threshold_type}s'])}")
            else:
                logger.warning(f"⚠️  Threshold level ${level:.2f} not found in {threshold_type}s list to remove")
        except Exception as e:
            logger.error(f"❌ Error removing threshold level ${level:.2f} from config: {e}", exc_info=True)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, shutting down...")
        self._running = False
    
    def _cleanup_pid_file(self):
        """Remove PID file on exit."""
        pid_file = Path(self.pid_file)
        if pid_file.exists():
            try:
                pid_file.unlink()
                logger.info("Removed PID file")
            except Exception as e:
                logger.warning(f"Could not remove PID file: {e}")
    
    def _write_pid_file(self):
        """Write PID file."""
        pid_file = Path(self.pid_file)
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(pid_file, 'w') as f:
                f.write(str(os.getpid()))
            logger.info(f"Wrote PID file: {pid_file}")
        except Exception as e:
            logger.error(f"Could not write PID file: {e}")
    
    def _is_market_holiday(self, check_date: date) -> bool:
        """Check if date is a market holiday (weekend or US stock market holiday)."""
        # Check weekend
        weekday = check_date.weekday()
        if weekday >= 5:  # Saturday = 5, Sunday = 6
            return True
        
        # US Stock Market Holidays for 2025
        holidays_2025 = [
            date(2025, 1, 1),   # New Year's Day
            date(2025, 1, 20),  # MLK Day
            date(2025, 2, 17),  # Presidents' Day
            date(2025, 4, 18),  # Good Friday
            date(2025, 5, 26),  # Memorial Day
            date(2025, 6, 19),  # Juneteenth
            date(2025, 7, 4),   # Independence Day
            date(2025, 9, 1),   # Labor Day
            date(2025, 11, 27), # Thanksgiving
            date(2025, 12, 25), # Christmas
        ]
        
        return check_date in holidays_2025
    
    def _get_next_trading_day(self, start_date: date) -> date:
        """Get the next trading day after start_date."""
        next_day = start_date + timedelta(days=1)
        while self._is_market_holiday(next_day):
            next_day += timedelta(days=1)
        return next_day
    
    def _get_today_est(self) -> date:
        """Get today's date in EST/ET timezone (for market operations)."""
        now_est = datetime.now(NYSE_TZ)
        return now_est.date()
    
    def _reset_triggered_bots_if_new_day(self, today: date) -> None:
        """
        Reset triggered bots list if it's a new trading day.
        This allows bots to be triggered once per day.
        """
        triggered_date_str = self.state.get('triggered_bots_date')
        today_str = today.isoformat()
        
        if triggered_date_str != today_str:
            if triggered_date_str:
                logger.info(f"New trading day detected ({today_str}). Resetting triggered bots from previous day ({triggered_date_str})")
            self.state['triggered_bots'] = []
            self.state['triggered_bots_date'] = today_str
            self._save_state()
    
    def _is_trading_day(self, check_date: date) -> bool:
        """Check if date is a trading day (not weekend or holiday)."""
        return not self._is_market_holiday(check_date)
    
    def _check_and_wait_for_trading_day(self) -> bool:
        """
        Check if today is a trading day. If not, wait until next trading day.
        
        Returns:
            True if today is a trading day, False if waiting for next trading day
        """
        today = self._get_today_est()
        is_trading = self._is_trading_day(today)
        
        if not is_trading:
            next_trading = self._get_next_trading_day(today)
            logger.info(f"⏸️  Skipping checks - today ({today}) is not a trading day. Next trading day: {next_trading}")
            self._is_trading_day_flag = False
            self._next_trading_day = next_trading
            return False
        
        # If we were waiting and now it's a trading day, update flags and reset triggered bots
        was_waiting = not self._is_trading_day_flag
        self._is_trading_day_flag = True
        self._next_trading_day = today
        
        if was_waiting:
            logger.info(f"✓ Trading day resumed: {today}")
            # Reset triggered bots for the new trading day
            self._reset_triggered_bots_if_new_day(today)
        
        # Always check if it's a new day (in case we've been running across midnight)
        self._reset_triggered_bots_if_new_day(today)
        
        return True
    
    def _init_schwab_client(self) -> None:
        """Initialize Schwab API client from token file."""
        token_path = self.config['token_path']
        
        if not os.path.exists(token_path):
            logger.error(f"Token file not found: {token_path}")
            raise FileNotFoundError(f"Token file not found: {token_path}")
        
        try:
            self.schwab_client = client_from_token_file(
                token_path,
                api_key=os.environ.get('SCHWAB_API_KEY'),
                app_secret=os.environ.get('SCHWAB_APP_SECRET'),
                enforce_enums=False
            )
            logger.info("Schwab client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Schwab client: {e}")
            raise
    
    def get_spx_price(self) -> Optional[float]:
        """
        Get current SPX spot price from option chain.
        Uses 0 DTE if today is a trading day, otherwise uses next trading day.
        
        Returns:
            Spot price or None if unavailable
        """
        if not self.schwab_client:
            logger.error("Schwab client not initialized")
            return None
        
        try:
            today = self._get_today_est()
            
            # Check if today is a market holiday
            if self._is_market_holiday(today):
                expiration_date = self._get_next_trading_day(today)
                logger.info(f"Today ({today}) is a market holiday, using next trading day ({expiration_date}) for SPX price")
            else:
                expiration_date = today
                logger.debug(f"Using today's ({today}) option chain for SPX price")
            
            # Try different symbol formats for SPX
            symbols_to_try = ["$SPX.X", "$SPX", "SPX"]
            
            for symbol in symbols_to_try:
                try:
                    # Get option chain for the appropriate expiration date
                    chain_response = self.schwab_client.get_option_chain(
                        symbol=symbol,
                        from_date=expiration_date,
                        to_date=expiration_date
                    )
                    
                    if chain_response.status_code == 200:
                        chain_data = chain_response.json()
                        
                        # Try to get underlying price from chain
                        if 'underlyingPrice' in chain_data:
                            price = float(chain_data['underlyingPrice'])
                            logger.info(f"📊 SPX spot price: ${price:.2f} (from {symbol}, expiration: {expiration_date})")
                            return price
                        elif 'underlying' in chain_data and 'last' in chain_data['underlying']:
                            price = float(chain_data['underlying']['last'])
                            logger.info(f"📊 SPX spot price: ${price:.2f} (from {symbol} underlying.last, expiration: {expiration_date})")
                            return price
                    
                except Exception as e:
                    logger.debug(f"Failed to get option chain with symbol {symbol}: {e}")
                    continue
            
            logger.warning("Could not get SPX price from any symbol format")
            return None
            
        except Exception as e:
            logger.error(f"Error getting SPX price: {e}")
            return None
    
    def check_thresholds(self, current_price: float) -> List[Tuple[str, float]]:
        """
        Check if any thresholds have been crossed.
        
        For PUT thresholds: Triggers when current_price < threshold (price is below threshold)
        For CALL thresholds: Triggers when current_price > threshold (price is above threshold)
        
        Args:
            current_price: Current SPX price
            
        Returns:
            List of (bot_name, threshold_level) tuples for newly triggered bots
        """
        # Note: Trading day check is already done in run() loop before calling this method
        triggered = []
        last_price = self.state.get('last_price')
        
        # Check put thresholds (trigger when price is BELOW threshold)
        for threshold in self.put_thresholds:
            level = threshold['level']
            bot_name = threshold['bot_name']
            
            # Skip if already triggered
            if bot_name in self.state.get('triggered_bots', []):
                continue
            
            # Trigger if current price is below threshold
            # PUT bots trigger when price is BELOW the threshold
            if current_price < level:
                # Trigger if:
                # 1. This is the first check after startup (to catch existing conditions), OR
                # 2. Previous price was at or above threshold (crossed down from above), OR
                # 3. No previous price recorded (first time checking)
                should_trigger = False
                if self._first_check:
                    should_trigger = True
                    logger.info(f"PUT threshold check (first check): {bot_name} at level ${level}, current price: ${current_price:.2f}")
                elif last_price is None:
                    should_trigger = True
                    logger.info(f"PUT threshold check (no previous price): {bot_name} at level ${level}, current price: ${current_price:.2f}")
                elif last_price >= level:
                    should_trigger = True
                    logger.info(f"PUT threshold crossed down: {bot_name} at level ${level}, price dropped from ${last_price:.2f} to ${current_price:.2f}")
                
                if should_trigger:
                    triggered.append((bot_name, level))
                    last_price_str = f"${last_price:.2f}" if last_price is not None else "N/A"
                    logger.info(f"✅ PUT threshold triggered: {bot_name} at level ${level} (current price: ${current_price:.2f}, was: {last_price_str})")
                else:
                    last_price_str = f"${last_price:.2f}" if last_price is not None else "N/A"
                    logger.debug(f"PUT threshold not triggered: {bot_name} at level ${level}, current: ${current_price:.2f}, last: {last_price_str} (already below)")
        
        # Check call thresholds (trigger when price is ABOVE threshold)
        for threshold in self.call_thresholds:
            level = threshold['level']
            bot_name = threshold['bot_name']
            
            # Skip if already triggered
            if bot_name in self.state.get('triggered_bots', []):
                continue
            
            # Trigger if current price is above threshold
            # CALL bots trigger when price is ABOVE the threshold
            if current_price > level:
                # Trigger if:
                # 1. This is the first check after startup (to catch existing conditions), OR
                # 2. Previous price was at or below threshold (crossed up from below), OR
                # 3. No previous price recorded (first time checking)
                should_trigger = False
                if self._first_check:
                    should_trigger = True
                    logger.info(f"CALL threshold check (first check): {bot_name} at level ${level}, current price: ${current_price:.2f}")
                elif last_price is None:
                    should_trigger = True
                    logger.info(f"CALL threshold check (no previous price): {bot_name} at level ${level}, current price: ${current_price:.2f}")
                elif last_price <= level:
                    should_trigger = True
                    logger.info(f"CALL threshold crossed up: {bot_name} at level ${level}, price rose from ${last_price:.2f} to ${current_price:.2f}")
                
                if should_trigger:
                    triggered.append((bot_name, level))
                    last_price_str = f"${last_price:.2f}" if last_price is not None else "N/A"
                    logger.info(f"✅ CALL threshold triggered: {bot_name} at level ${level} (current price: ${current_price:.2f}, was: {last_price_str})")
                else:
                    last_price_str = f"${last_price:.2f}" if last_price is not None else "N/A"
                    logger.debug(f"CALL threshold not triggered: {bot_name} at level ${level}, current: ${current_price:.2f}, last: {last_price_str} (already above)")
        
        return triggered
    
    def call_webhook(self, bot_name: str) -> bool:
        """
        Call webhook to unpause bot.
        
        Args:
            bot_name: Name of bot to unpause
            
        Returns:
            True if successful, False otherwise
        """
        webhook_url = self.config['webhook_url']
        
        try:
            response = requests.post(
                webhook_url,
                json={'bot_name': bot_name},
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    logger.info(f"Successfully unpaused bot '{bot_name}' via webhook")
                    return True
                else:
                    logger.warning(f"Webhook returned success=False for '{bot_name}': {result.get('message')}")
                    return False
            else:
                logger.error(f"Webhook returned status {response.status_code} for '{bot_name}': {response.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Error calling webhook for '{bot_name}': {e}")
            return False
    
    def run(self) -> None:
        """Main monitoring loop."""
        check_interval = self.config['check_interval_seconds']
        check_interval_minutes = check_interval / 60.0
        logger.info("Starting threshold monitor...")
        logger.info(f"Monitoring {self.config['symbol']} price")
        logger.info(f"Check interval: {check_interval} seconds ({check_interval_minutes:.1f} minutes)")
        logger.info(f"Webhook URL: {self.config['webhook_url']}")
        
        # Write PID file
        self._write_pid_file()
        
        try:
            while self._running:
                try:
                    # Check if today is a trading day - skip all checks if not
                    if not self._check_and_wait_for_trading_day():
                        # Not a trading day - wait until next trading day
                        # Calculate seconds until next trading day (check every hour)
                        today = self._get_today_est()
                        next_trading = self._get_next_trading_day(today)
                        days_until_trading = (next_trading - today).days
                        
                        # Wait in smaller increments (1 hour) so we can check if it becomes a trading day
                        wait_seconds = min(3600, self.config['check_interval_seconds'])  # Check every hour or check_interval, whichever is smaller
                        logger.info(f"Waiting {wait_seconds} seconds before checking trading day status again...")
                        
                        for _ in range(wait_seconds):
                            if not self._running:
                                break
                            time.sleep(1)
                        continue
                    
                    # Get current price
                    current_price = self.get_spx_price()
                    
                    if current_price is None:
                        logger.warning("Could not get current price, retrying in next cycle")
                        time.sleep(self.config['check_interval_seconds'])
                        continue
                    
                    # Log current SPX spot price
                    logger.info(f"📊 Current SPX spot price: ${current_price:.2f}")
                    
                    # Check thresholds BEFORE updating last_price (so we can detect crossings)
                    triggered = self.check_thresholds(current_price)
                    
                    # Update last price in state after checking thresholds
                    self.state['last_price'] = current_price
                    self.state['last_check'] = datetime.now(timezone.utc).isoformat()
                    
                    # Save state immediately after updating price (so UI can display it)
                    self._save_state()
                    
                    # Mark that first check is complete (only on trading days)
                    if self._first_check:
                        self._first_check = False
                    
                    # Trigger webhooks for newly crossed thresholds
                    for bot_name, level in triggered:
                        logger.info(f"🎯 THRESHOLD TRIGGERED: Bot '{bot_name}' at level {level} (current price: {current_price})")
                        
                        # Determine threshold type (put or call) before removing
                        threshold_type = 'put' if level in [t['level'] for t in self.put_thresholds] else 'call'
                        
                        success = self.call_webhook(bot_name)
                        
                        if success:
                            # Ensure triggered_bots list exists and reset if new day
                            today = self._get_today_est()
                            self._reset_triggered_bots_if_new_day(today)
                            
                            # Add to triggered bots list
                            if 'triggered_bots' not in self.state:
                                self.state['triggered_bots'] = []
                            if bot_name not in self.state['triggered_bots']:
                                self.state['triggered_bots'].append(bot_name)
                                self.state['triggered_bots_date'] = today.isoformat()
                            
                            # Store last trigger info for notifications
                            self._last_trigger = {
                                'bot_name': bot_name,
                                'threshold': level,
                                'price': current_price,
                                'timestamp': datetime.now(timezone.utc).isoformat(),
                                'type': threshold_type
                            }
                            
                            # Also save last_trigger to state file for persistence
                            self.state['last_trigger'] = self._last_trigger
                            
                            # Remove the threshold level from configuration
                            self._remove_threshold(level, threshold_type)
                            
                            # Save state immediately
                            self._save_state()
                            logger.info(f"✅ Bot '{bot_name}' successfully triggered and added to triggered list for {today.isoformat()}")
                        else:
                            logger.error(f"❌ Failed to trigger webhook for '{bot_name}', will retry on next check")
                    
                    # Log current status
                    logger.debug(f"Current price: {current_price}, Triggered bots: {len(self.state.get('triggered_bots', []))}")
                    
                    # Wait before next check (check _running flag periodically)
                    for _ in range(self.config['check_interval_seconds']):
                        if not self._running:
                            break
                        time.sleep(1)
                
                except KeyboardInterrupt:
                    logger.info("Received interrupt signal, shutting down...")
                    self._running = False
                    break
                except Exception as e:
                    logger.error(f"Unexpected error in monitoring loop: {e}", exc_info=True)
                    # Continue running instead of stopping - wait before retrying
                    logger.info("Waiting before retrying after error...")
                    # Save state before continuing
                    self._save_state()
                    # Wait before retrying
                    for _ in range(self.config['check_interval_seconds']):
                        if not self._running:
                            break
                        time.sleep(1)
                    # Continue the loop (this continue is now inside the while loop)
                    continue
                
        finally:
            # Only execute cleanup when actually stopping
            # Save final state
            self._save_state()
            self._cleanup_pid_file()
            logger.info("Monitor stopped")
    
    def get_status(self) -> Dict:
        """Get current monitor status."""
        pid_file = Path(self.pid_file)
        running = False
        pid = None
        started_at = None
        
        if pid_file.exists():
            try:
                with open(pid_file, 'r') as f:
                    pid = int(f.read().strip())
                
                # Check if process is actually running
                try:
                    os.kill(pid, 0)  # Signal 0 doesn't kill, just checks if process exists
                    running = True
                except (OSError, ProcessLookupError):
                    # Process doesn't exist, remove stale PID file
                    pid_file.unlink()
                    pid = None
            except (ValueError, IOError):
                pass
        
        # Try to get started_at from state file
        if running and self.state.get('last_check'):
            started_at = self.state.get('last_check')
        
        # Check current trading day status (using EST/ET timezone)
        today = self._get_today_est()
        is_trading_day = self._is_trading_day(today)
        next_trading_day = self._get_next_trading_day(today) if not is_trading_day else today
        
        return {
            'running': running,
            'pid': pid,
            'started_at': started_at,
            'last_trigger': self._last_trigger,
            'last_price': self.state.get('last_price'),
            'triggered_bots': self.state.get('triggered_bots', []),
            'is_trading_day': is_trading_day,
            'next_trading_day': next_trading_day.isoformat() if not is_trading_day else None
        }


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Monitor SPX price thresholds and trigger bot unpause')
    parser.add_argument(
        '--config',
        type=str,
        default='/app/config/threshold_config.json',
        help='Path to configuration file (default: /app/config/threshold_config.json)'
    )
    
    args = parser.parse_args()
    
    try:
        monitor = ThresholdMonitor(args.config)
        monitor.run()
    except Exception as e:
        logger.error(f"Failed to start monitor: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

