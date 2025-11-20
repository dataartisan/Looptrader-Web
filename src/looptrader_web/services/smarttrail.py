"""SmartTrail service for tiered trailing stops based on distance to spot."""

import os
import re
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, List, Optional, Tuple, Dict
from dataclasses import dataclass

from models.database import SessionLocal, Bot, Position, Order, OrderLeg, Instrument
from models.database import upsert_trailing_stop, upsert_trailing_stops_batch


@dataclass
class PositionWithDistance:
    """Position with calculated distance to spot."""
    bot_id: int
    position_id: int
    order: Order
    ticker: str
    short_strike: float
    distance_to_spot: float
    spot_price: float


class SmartTrailService:
    """Service for applying tiered trailing stops based on distance to spot."""
    
    # Spot price cache: {ticker: (timestamp, price)}
    _spot_price_cache: Dict[str, Tuple[float, float]] = {}
    _cache_ttl_seconds = 8  # Cache spot prices for 8 seconds
    
    def __init__(self):
        """Initialize SmartTrailService."""
        self._schwab_client = None
    
    def _get_schwab_client(self):
        """Get or create Schwab client."""
        if self._schwab_client is None:
            import schwab
            token_path = '/app/token.json'
            if not os.path.exists(token_path):
                app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
                token_path = os.path.join(app_root, 'token.json')
            
            self._schwab_client = schwab.auth.client_from_token_file(
                token_path,
                api_key=os.environ.get('SCHWAB_API_KEY'),
                app_secret=os.environ.get('SCHWAB_APP_SECRET'),
                enforce_enums=False
            )
        return self._schwab_client
    
    def get_active_positions(
        self,
        bot_id: Optional[int] = None,
        selected_bot_ids: Optional[List[int]] = None,
        strategy_group: Optional[List[str]] = None
    ) -> List[Tuple[Bot, Position, Order]]:
        """
        Get active positions matching filters.
        
        Args:
            bot_id: Single bot ID filter (for backward compatibility)
            selected_bot_ids: List of bot IDs to filter
            strategy_group: Optional strategy group filter
            
        Returns:
            List of (Bot, Position, Order) tuples
        """
        positions = []
        
        with SessionLocal() as session:
            # Determine which bot IDs to filter
            bot_ids_to_filter = None
            if selected_bot_ids:
                bot_ids_to_filter = selected_bot_ids
            elif bot_id is not None:
                bot_ids_to_filter = [bot_id]
            
            # Get bots matching filters
            if bot_ids_to_filter:
                bots = session.query(Bot).filter(Bot.id.in_(bot_ids_to_filter)).all()
            else:
                bots = session.query(Bot).all()
            
            # Get active positions for each bot
            for bot in bots:
                if not bot:
                    continue
                
                # Get active position for this bot
                db_position = (
                    session.query(Position)
                    .filter(Position.bot_id == bot.id)
                    .filter(Position.active == True)
                    .first()
                )
                
                if db_position is None:
                    continue
                
                # Get opening order
                from sqlalchemy.orm import joinedload
                opening_order = (
                    session.query(Order)
                    .filter(Order.position_id == db_position.id)
                    .filter(Order.isOpenPosition == True)
                    .options(
                        joinedload(Order.orderLegCollection).joinedload(OrderLeg.instrument)
                    )
                    .first()
                )
                
                if opening_order is None or not opening_order.orderLegCollection:
                    continue
                
                # Filter by strategy group if specified
                # Note: Strategy group is not stored in Order, so we match by bot name pattern
                if strategy_group:
                    bot_name = bot.name or ""
                    # Check if bot name contains any strategy group (case-insensitive)
                    if not any(sg.lower() in bot_name.lower() for sg in strategy_group):
                        continue
                
                positions.append((bot, db_position, opening_order))
        
        return positions
    
    def extract_ticker_from_order(self, order: Order) -> Optional[str]:
        """
        Extract ticker symbol from order's instrument underlyingSymbol.
        
        Args:
            order: Order with orderLegCollection
            
        Returns:
            Ticker symbol (e.g., "SPX", "SPY") or None if not found
        """
        if not order.orderLegCollection:
            return None
        
        # Get ticker from first leg's instrument
        for leg in order.orderLegCollection:
            if leg.instrument and leg.instrument.underlyingSymbol:
                return leg.instrument.underlyingSymbol
        
        return None
    
    def extract_short_strike(self, order: Order) -> Optional[float]:
        """
        Extract the short leg strike from an order.
        
        For credit spreads (short positions), find the leg with SELL instruction
        and extract its strike from the symbol.
        
        Args:
            order: Order with orderLegCollection
            
        Returns:
            Strike price of short leg, or None if not found
        """
        if not order.orderLegCollection:
            return None
        
        # Find short leg (SELL instruction)
        for leg in order.orderLegCollection:
            instruction = str(leg.instruction).upper()
            if instruction.startswith("SELL"):
                # Extract strike from symbol
                symbol = leg.instrument.symbol
                strike = self._get_strike_from_symbol(symbol)
                if strike > 0:
                    return float(strike) / 1000.0  # Convert from symbol format to price
        
        # If no SELL leg found, return None
        return None
    
    def _get_strike_from_symbol(self, symbol: str) -> int:
        """Get the strike from an option symbol."""
        match = re.search(r"(\d+)$", symbol)
        return int(match.group(1)) if match else 0
    
    def _get_cached_spot_price(self, ticker: str) -> Optional[float]:
        """Get spot price from cache if available and not expired."""
        ticker_upper = ticker.upper()
        if ticker_upper in self._spot_price_cache:
            cached_time, cached_price = self._spot_price_cache[ticker_upper]
            if time.time() - cached_time < self._cache_ttl_seconds:
                print(f"Using cached spot price for {ticker}: {cached_price}")
                return cached_price
            else:
                # Remove expired entry
                del self._spot_price_cache[ticker_upper]
        return None
    
    def _cache_spot_price(self, ticker: str, price: float) -> None:
        """Cache spot price with current timestamp."""
        ticker_upper = ticker.upper()
        self._spot_price_cache[ticker_upper] = (time.time(), price)
        # Clean up old entries if cache gets too large (keep last 100)
        if len(self._spot_price_cache) > 100:
            # Remove oldest entries
            sorted_entries = sorted(self._spot_price_cache.items(), key=lambda x: x[1][0])
            for key, _ in sorted_entries[:-100]:
                del self._spot_price_cache[key]
    
    def _fetch_spot_price_from_broker(self, ticker: str, max_retries: int = 3, initial_delay: float = 1.0, max_delay: float = 5.0) -> Optional[float]:
        """
        Fetch spot price from broker API with retry logic.
        
        Args:
            ticker: Ticker symbol (e.g., "SPX", "SPY")
            max_retries: Maximum number of retry attempts
            initial_delay: Initial delay in seconds before first retry
            max_delay: Maximum delay in seconds between retries
            
        Returns:
            Spot price or None if unavailable
        """
        delay = initial_delay
        
        for attempt in range(max_retries):
            try:
                client = self._get_schwab_client()
                
                # Try different symbol formats
                symbols_to_try = [f'${ticker}.X', ticker, f'${ticker}']
                
                for symbol in symbols_to_try:
                    try:
                        # Try to get quote first
                        quote_response = client.get_quotes([symbol])
                        if quote_response.status_code == 200:
                            quote_data = quote_response.json()
                            if symbol in quote_data:
                                quote = quote_data[symbol]
                                if 'lastPrice' in quote:
                                    return float(quote['lastPrice'])
                                elif 'mark' in quote:
                                    return float(quote['mark'])
                        
                        # If quote doesn't work, try option chain for underlying price
                        from datetime import date
                        chain_response = client.get_option_chain(
                            symbol=symbol,
                            from_date=date.today(),
                            to_date=date.today()
                        )
                        if chain_response.status_code == 200:
                            chain_data = chain_response.json()
                            if 'underlyingPrice' in chain_data:
                                return float(chain_data['underlyingPrice'])
                    except Exception as e:
                        if attempt < max_retries - 1:
                            print(f"Attempt {attempt + 1} failed for {ticker} with symbol {symbol}: {e}")
                        continue
                
                # If we get here, all symbols failed for this attempt
                if attempt < max_retries - 1:
                    jitter = random.uniform(0, 0.1 * delay)  # Add jitter
                    print(f"All symbols failed for {ticker} on attempt {attempt + 1}/{max_retries}. Retrying in {delay + jitter:.2f} seconds...")
                    time.sleep(delay + jitter)
                    delay = min(delay * 2, max_delay)  # Exponential backoff
                
            except Exception as e:
                if attempt < max_retries - 1:
                    jitter = random.uniform(0, 0.1 * delay)
                    print(f"Error getting spot price for {ticker} on attempt {attempt + 1}/{max_retries}: {e}. Retrying in {delay + jitter:.2f} seconds...")
                    time.sleep(delay + jitter)
                    delay = min(delay * 2, max_delay)
                else:
                    print(f"Error getting spot price for {ticker} after {max_retries} attempts: {e}")
        
        return None
    
    def get_spot_price(self, ticker: str) -> Optional[float]:
        """
        Get current spot price for a ticker using Schwab API with caching and retry logic.
        
        Args:
            ticker: Ticker symbol (e.g., "SPX", "SPY")
            
        Returns:
            Spot price or None if unavailable
        """
        # Check cache first
        cached_price = self._get_cached_spot_price(ticker)
        if cached_price is not None:
            return cached_price
        
        # Fetch from broker with retry logic
        price = self._fetch_spot_price_from_broker(ticker)
        if price is not None:
            # Cache the result
            self._cache_spot_price(ticker, price)
        
        return price
    
    def calculate_distances(
        self,
        positions: List[Tuple[Bot, Position, Order]]
    ) -> List[PositionWithDistance]:
        """
        Calculate distance to spot for each position.
        
        Args:
            positions: List of (Bot, Position, Order) tuples
            
        Returns:
            List of PositionWithDistance sorted by distance (ascending)
        """
        positions_with_distance = []
        
        # Group positions by ticker to batch spot price requests
        ticker_groups: Dict[str, List[Tuple[Bot, Position, Order]]] = {}
        for bot, position, order in positions:
            ticker = self.extract_ticker_from_order(order)
            if not ticker:
                print(f"Could not extract ticker for bot {bot.id}, position {position.id}, skipping")
                continue
            if ticker not in ticker_groups:
                ticker_groups[ticker] = []
            ticker_groups[ticker].append((bot, position, order))
        
        # Get spot prices for all tickers concurrently
        spot_prices: Dict[str, float] = {}
        
        # Fetch all spot prices concurrently using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(ticker_groups), 10)) as executor:
            # Submit all spot price fetch tasks
            future_to_ticker = {
                executor.submit(self.get_spot_price, ticker): ticker
                for ticker in ticker_groups.keys()
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    spot_price = future.result()
                    if spot_price:
                        spot_prices[ticker] = spot_price
                    else:
                        print(f"Could not get spot price for {ticker}, skipping {len(ticker_groups[ticker])} positions")
                except Exception as e:
                    print(f"Error getting spot price for {ticker}: {e}")
        
        # Calculate distances for each position
        for bot, position, order in positions:
            ticker = self.extract_ticker_from_order(order)
            if not ticker or ticker not in spot_prices:
                continue
            
            spot_price = spot_prices[ticker]
            short_strike = self.extract_short_strike(order)
            
            if short_strike is None:
                print(f"Could not extract short strike for bot {bot.id}, position {position.id}")
                continue
            
            distance = abs(short_strike - spot_price)
            
            positions_with_distance.append(
                PositionWithDistance(
                    bot_id=bot.id,
                    position_id=position.id,
                    order=order,
                    ticker=ticker,
                    short_strike=short_strike,
                    distance_to_spot=distance,
                    spot_price=spot_price
                )
            )
        
        # Sort by distance (closest first)
        positions_with_distance.sort(key=lambda x: x.distance_to_spot)
        
        return positions_with_distance
    
    def tier_positions(
        self,
        positions_with_distance: List[PositionWithDistance],
        tier_activation_thresholds: List[float]
    ) -> List[Tuple[PositionWithDistance, float]]:
        """
        Divide positions into tiers based on activation thresholds.
        
        Args:
            positions_with_distance: List of positions sorted by distance
            tier_activation_thresholds: List of activation thresholds for each tier
            
        Returns:
            List of (PositionWithDistance, activation_threshold) tuples
        """
        if not positions_with_distance:
            return []
        
        num_tiers = len(tier_activation_thresholds)
        num_positions = len(positions_with_distance)
        
        tiered_positions = []
        
        # Calculate positions per tier
        positions_per_tier = num_positions // num_tiers
        remainder = num_positions % num_tiers
        
        start_idx = 0
        for tier_idx, activation_threshold in enumerate(tier_activation_thresholds):
            # Last tier gets remainder
            if tier_idx == num_tiers - 1:
                end_idx = num_positions
            else:
                end_idx = start_idx + positions_per_tier
            
            # Assign positions to this tier
            for pos in positions_with_distance[start_idx:end_idx]:
                tiered_positions.append((pos, activation_threshold))
            
            start_idx = end_idx
        
        return tiered_positions
    
    def apply_tiered_trails(
        self,
        tier_activation_thresholds: List[float],
        trailing_percentage: float,
        bot_id: Optional[int] = None,
        selected_bot_ids: Optional[List[int]] = None,
        strategy_group: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Apply tiered trailing stops to positions.
        
        Args:
            tier_activation_thresholds: List of activation thresholds for each tier
            trailing_percentage: Trailing percentage (fixed for all tiers)
            bot_id: Optional single bot ID filter
            selected_bot_ids: Optional list of bot IDs to filter
            strategy_group: Optional strategy group filter
            
        Returns:
            Dictionary with summary information
        """
        # Get active positions
        positions = self.get_active_positions(
            bot_id=bot_id,
            selected_bot_ids=selected_bot_ids,
            strategy_group=strategy_group
        )
        
        print(f"Found {len(positions)} active positions matching filters")
        
        if not positions:
            return {
                "success": False,
                "message": "No active positions found",
                "positions_processed": 0
            }
        
        # Calculate distances
        positions_with_distance = self.calculate_distances(positions)
        
        print(f"Successfully calculated distances for {len(positions_with_distance)} positions out of {len(positions)}")
        
        if not positions_with_distance:
            return {
                "success": False,
                "message": f"Could not calculate distances for any positions. Found {len(positions)} positions but none had valid ticker/strike data.",
                "positions_processed": 0
            }
        
        # Tier positions
        tiered_positions = self.tier_positions(
            positions_with_distance,
            tier_activation_thresholds
        )
        
        # Apply trailing stops atomically
        tier_summary = {}
        
        # Collect all trailing stop configurations
        trailing_stop_configs = []
        for pos_with_dist, activation_threshold in tiered_positions:
            trailing_stop_configs.append({
                "bot_id": pos_with_dist.bot_id,
                "activation_threshold": activation_threshold,
                "trailing_percentage": trailing_percentage,
                "trailing_mode": "percentage"
            })
            
            # Track tier summary (for reporting)
            tier_key = f"{activation_threshold}%"
            if tier_key not in tier_summary:
                tier_summary[tier_key] = 0
            tier_summary[tier_key] += 1
        
        print(f"Applying trailing stops to {len(tiered_positions)} positions across {len(tier_activation_thresholds)} tiers in atomic transaction")
        
        # Apply all updates in a single atomic transaction
        success, applied_count, error_list = upsert_trailing_stops_batch(trailing_stop_configs)
        
        # Convert error list to string format for backward compatibility
        errors = []
        if error_list:
            for error in error_list:
                bot_id = error.get("bot_id", "Unknown")
                error_msg = error.get("error", "Unknown error")
                errors.append(f"Bot {bot_id}: {error_msg}")
                # Adjust tier_summary to reflect actual successes
                for pos_with_dist, activation_threshold in tiered_positions:
                    if pos_with_dist.bot_id == bot_id:
                        tier_key = f"{activation_threshold}%"
                        if tier_key in tier_summary and tier_summary[tier_key] > 0:
                            tier_summary[tier_key] -= 1
                        break
        
        result = {
            "success": success and applied_count > 0,
            "message": f"Applied tiered trailing stops to {applied_count} positions",
            "positions_processed": applied_count,
            "tier_summary": tier_summary,
            "total_positions": len(positions_with_distance)
        }
        
        if errors:
            result["errors"] = errors
        
        return result

