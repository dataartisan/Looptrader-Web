"""Database models for LoopTrader Web Interface with AdminLTE styling"""

import os
from datetime import datetime
from typing import List, Optional
from sqlalchemy import create_engine, Integer, String, Boolean, Float, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, Mapped, mapped_column, joinedload
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database configuration
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    # Fallback to constructing from individual environment variables
    db_user = os.getenv('DB_USER', 'admin')
    db_password = os.getenv('DB_PASSWORD', '')  # No default password
    db_host = os.getenv('DB_HOST', 'localhost')
    db_port = os.getenv('DB_PORT', '5432')
    db_name = os.getenv('DB_NAME', 'looptrader')
    DATABASE_URL = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

# Create engine and session
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)

# Base class for all models
Base = declarative_base()

def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        return db
    finally:
        pass  # Don't close here, let the caller handle it

class BrokerageAccount(Base):
    """Account model matching LoopTrader Pro"""
    __tablename__ = "Account"
    
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    name = mapped_column(String, nullable=False)
    account_id = mapped_column(Integer, unique=True, nullable=False)
    
    # Relationships
    positions = relationship("Position", foreign_keys="Position.account_id", primaryjoin="BrokerageAccount.account_id == Position.account_id")
    
    def __repr__(self):
        return f"<Account {self.name} ({self.account_id})>"
    
    @property
    def total_positions(self):
        # Always get fresh data from the relationship 
        return len(self.positions) if self.positions else 0
    
    @property
    def active_positions(self):
        # Always get fresh data from the relationship
        return len([p for p in self.positions if p.active]) if self.positions else 0
    
    @property
    def bots_count(self):
        # Get unique bots from positions
        bot_ids = set()
        if self.positions:
            for position in self.positions:
                if position.bot_id:
                    bot_ids.add(position.bot_id)
        return len(bot_ids)
    
    @property
    def recent_activity(self):
        if not self.positions:
            return None
        # Return most recent position activity
        return max(self.positions, key=lambda p: p.opened_datetime) if self.positions else None

class Bot(Base):
    """Bot model matching LoopTrader Pro"""
    __tablename__ = "Bot"
    
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    name = mapped_column(String, nullable=False)
    state = mapped_column(String, nullable=False)
    enabled = mapped_column(Boolean, nullable=False)
    paused = mapped_column(Boolean, default=False)
    
    # Relationships
    trailing_stop_state = relationship("TrailingStopState", back_populates="bot", uselist=False)
    positions = relationship("Position", back_populates="bot")
    
    def __repr__(self):
        return f"<Bot {self.name} ({self.state})>"
    
    @property
    def state_badge_class(self):
        state_upper = self.state.upper()
        if 'RUNNING' in state_upper or 'ACTIVE' in state_upper:
            return "success"
        elif 'WAITING' in state_upper or 'SCANNING' in state_upper:
            return "info"
        elif 'STOPPED' in state_upper or 'ERROR' in state_upper:
            return "danger"
        else:
            return "secondary"
    
    @property
    def status_badge_class(self):
        if self.paused:
            return "warning"
        elif self.enabled:
            return "success"
        else:
            return "danger"
    
    @property
    def status_text(self):
        if self.paused:
            return "Paused"
        elif self.enabled:
            return "Enabled"
        else:
            return "Disabled"
    
    @property
    def has_trailing_stop(self):
        return self.trailing_stop_state is not None
    
    @property
    def active_positions_count(self):
        # Always get fresh data from the relationship
        return len([p for p in self.positions if p.active])

    @property
    def total_positions(self):
        # Always get fresh data from the relationship 
        return len(self.positions)
    
    @property
    def account_name(self):
        """Return the account name for the most recent position."""
        if not self.positions:
            return "No Account"
        
        # Get the most recent position
        recent_position = max(self.positions, key=lambda p: p.opened_datetime)
        
        # Get account name with fresh session
        if recent_position.account_id:
            db = SessionLocal()
            try:
                account = db.query(BrokerageAccount).filter_by(account_id=recent_position.account_id).first()
                return account.name if account else "Unknown"
            finally:
                db.close()
        return "Unknown"

    @property
    def remaining_position_slots(self):
        """Calculate remaining position slots: max_positions - active_positions_count."""
        active_count = self.active_positions_count  # This now always gets fresh data
        # Default max_positions to 1 since column doesn't exist in DB yet
        max_pos = 1  
        return max(0, max_pos - active_count)

class Position(Base):
    """Position model matching LoopTrader Pro"""
    __tablename__ = "Position"
    
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    active = mapped_column(Boolean, nullable=False)
    opened_datetime = mapped_column(DateTime, nullable=False)
    closed_datetime = mapped_column(DateTime, nullable=True)
    account_id = mapped_column(Integer, ForeignKey("Account.account_id"))
    bot_id = mapped_column(Integer, ForeignKey("Bot.id"))
    
    # Relationships
    bot = relationship("Bot", back_populates="positions")
    orders = relationship("Order", back_populates="position")
    
    def __repr__(self):
        status = "Active" if self.active else "Closed"
        return f"<Position {self.id} ({status})>"
    
    @property
    def status_badge_class(self):
        return "success" if self.active else "secondary"
    
    @property
    def status_text(self):
        return "Active" if self.active else "Closed"
    
    @property
    def duration_text(self):
        if self.active:
            delta = datetime.utcnow() - self.opened_datetime
            total_minutes = int(delta.total_seconds() // 60)
            return f"{total_minutes} min"
        else:
            # Only calculate if closed_datetime is not None
            if self.closed_datetime:
                delta = self.closed_datetime - self.opened_datetime
                total_minutes = int(delta.total_seconds() // 60)
                return f"{total_minutes} min"
            else:
                return "Unknown"
    
    @property
    def account_name(self):
        """Get account name for this position"""
        db = SessionLocal()
        try:
            account = db.query(BrokerageAccount).filter(BrokerageAccount.account_id == self.account_id).first()
            return account.name if account else "Unknown"
        finally:
            db.close()
    
    @property
    def initial_premium_sold(self):
        """Calculate the net initial premium from the opening order.
        
        In LoopTrader Pro, order.price is the NET price of the entire multi-leg order:
        - For credit spreads (NET_CREDIT): price is positive (we receive credit)
        - For debit spreads (NET_DEBIT): price is negative (we pay debit)
        - Formula: premium = price * filledQuantity * 100
        """
        try:
            # Find the opening order (marked with isOpenPosition=True)
            opening_order = None
            for order in self.orders:
                if hasattr(order, 'isOpenPosition') and order.isOpenPosition:
                    opening_order = order
                    break
            
            if not opening_order:
                # Fallback: use first FILLED order if no opening order marked
                for order in self.orders:
                    if order.status and 'FILLED' in order.status.upper():
                        opening_order = order
                        break
            
            if not opening_order:
                print(f"Position {self.id}: No opening order found")
                return 0.0
            
            # Check if order is filled with valid price and quantity
            if not (opening_order.status and 'FILLED' in opening_order.status.upper()):
                print(f"Position {self.id}: Opening order not filled (status: {opening_order.status})")
                return 0.0
            
            if opening_order.price is None:
                print(f"Position {self.id}: Opening order has no price")
                return 0.0
            
            # Use filledQuantity if available, otherwise fall back to quantity
            quantity = opening_order.filledQuantity if opening_order.filledQuantity else opening_order.quantity
            if not quantity:
                print(f"Position {self.id}: Opening order has no quantity")
                return 0.0
            
            # Calculate total premium: price is already the net price per contract
            # Multiply by quantity and 100 (option multiplier)
            total_premium = float(opening_order.price) * float(quantity) * 100
            
            print(f"Position {self.id}: Opening order price=${opening_order.price:.2f}, qty={quantity}, premium=${total_premium:.2f}")
            return total_premium
            
        except Exception as e:
            print(f"Error calculating initial premium for position {self.id}: {e}")
            import traceback
            traceback.print_exc()
            return 0.0
    
    def get_net_position_details(self):
        """Get detailed net position information"""
        try:
            net_contracts = 0.0
            total_cost_basis = 0.0
            orders_summary = []
            
            for order in self.orders:
                if (order.status and 'FILLED' in order.status.upper() and 
                    order.filledQuantity is not None and order.price is not None):
                    
                    filled_qty = float(order.filledQuantity)
                    order_price = float(order.price)
                    
                    if order.orderType and 'SELL' in order.orderType.upper():
                        net_contracts += filled_qty
                        total_cost_basis += filled_qty * order_price * 100
                        orders_summary.append({
                            'type': 'SELL',
                            'quantity': filled_qty,
                            'price': order_price,
                            'premium': filled_qty * order_price * 100
                        })
                    elif order.orderType and 'BUY' in order.orderType.upper():
                        net_contracts -= filled_qty
                        total_cost_basis -= filled_qty * order_price * 100
                        orders_summary.append({
                            'type': 'BUY',
                            'quantity': filled_qty,
                            'price': order_price,
                            'premium': filled_qty * order_price * 100
                        })
            
            return {
                'net_contracts': net_contracts,
                'total_cost_basis': total_cost_basis,
                'orders': orders_summary,
                'is_short': net_contracts > 0,
                'is_long': net_contracts < 0,
                'is_closed': abs(net_contracts) < 0.01
            }
        except Exception as e:
            print(f"Error calculating net position for position {self.id}: {e}")
            return {
                'net_contracts': 0,
                'total_cost_basis': 0,
                'orders': [],
                'is_short': False,
                'is_long': False,
                'is_closed': True
            }
    
    def get_current_market_value(self, schwab_cache=None):
        """Get the current market value from Schwab account data
        
        Returns the cost to close this position based on real-time market data matched
        by bot name and underlying symbol.
        
        Args:
            schwab_cache: Optional dictionary with position-specific market values
                         Format: {position_id: market_value}
        """
        try:
            # Import here to avoid circular imports
            import os
            import schwab
            import json
            
            print(f"Position {self.id}: Starting get_current_market_value()")
            
            # If cache is provided and has data for this specific position, use it
            if schwab_cache and self.id in schwab_cache:
                market_value = schwab_cache[self.id]
                print(f"Position {self.id}: Using cached market value ${market_value:,.2f}")
                return market_value if market_value > 0 else None
            
            # No cache provided, fetch data directly (slower path)
            print(f"Position {self.id}: No cache provided, fetching from Schwab API")
            
            # Check if Schwab token is available using the same logic as app.py
            token_path = os.path.join('/app', 'token.json')
            if not os.path.exists(token_path):
                app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                token_path = os.path.join(app_root, 'token.json')
            
            if not os.path.exists(token_path):
                print(f"Position {self.id}: Token file not found at {token_path}")
                return None
            
            # Try to load and validate token
            try:
                with open(token_path, 'r') as f:
                    token_data = json.load(f)
                    
                # Handle nested token structure
                token_info = token_data.get('token', token_data)
                if not all(key in token_info for key in ['access_token', 'refresh_token']):
                    print(f"Position {self.id}: Token file missing required fields")
                    return None
            except Exception as e:
                print(f"Position {self.id}: Error loading token: {e}")
                return None
            
            # Create Schwab client
            print(f"Position {self.id}: Creating Schwab client")
            client = schwab.auth.client_from_token_file(
                token_path,
                api_key=os.environ.get('SCHWAB_API_KEY'),
                app_secret=os.environ.get('SCHWAB_APP_SECRET'),
                enforce_enums=False
            )
            
            # Get account numbers to find the hash for this account_id
            print(f"Position {self.id}: Getting account numbers from Schwab")
            accounts_response = client.get_account_numbers()
            if accounts_response.status_code != 200:
                print(f"Position {self.id}: Failed to get account numbers, status code: {accounts_response.status_code}")
                return None
            
            accounts_data = accounts_response.json()
            print(f"Position {self.id}: Found {len(accounts_data)} accounts from Schwab")
            
            # Find the matching account hash
            # We need to match by account number stored in our database
            db = SessionLocal()
            try:
                brokerage_account = db.query(BrokerageAccount).filter(
                    BrokerageAccount.account_id == self.account_id
                ).first()
                
                if not brokerage_account:
                    print(f"Position {self.id}: No brokerage account found for account_id {self.account_id}")
                    return None
                
                print(f"Position {self.id}: Found brokerage account {brokerage_account.name} with account_id {brokerage_account.account_id}")
                
                # Match account by number (account_id might be the last 4 digits or full number)
                account_hash = None
                for account in accounts_data:
                    account_number = account.get('accountNumber', '')
                    # Check if the account number matches (exact or ends with our account_id)
                    if (str(brokerage_account.account_id) in str(account_number) or 
                        str(account_number).endswith(str(brokerage_account.account_id))):
                        account_hash = account.get('hashValue')
                        print(f"Position {self.id}: Matched account {account_number} with hash {account_hash}")
                        break
                
                if not account_hash:
                    print(f"Position {self.id}: No matching account hash found for account_id {brokerage_account.account_id}")
                    return None
                
                # Get count of active positions in this account
                active_positions_count = db.query(Position).filter(
                    Position.account_id == self.account_id,
                    Position.active == True
                ).count()
                
                print(f"Position {self.id}: Found {active_positions_count} active positions in this account")
                
                # Get total initial premium for all active positions in this account
                active_positions = db.query(Position).filter(
                    Position.account_id == self.account_id,
                    Position.active == True
                ).all()
                
                total_initial_premium = sum(pos.initial_premium_sold for pos in active_positions)
                print(f"Position {self.id}: Total initial premium across all active positions: ${total_initial_premium:,.2f}")
                
            finally:
                db.close()
            
            # Get account with positions
            print(f"Position {self.id}: Getting account positions from Schwab")
            account_response = client.get_account(account_hash, fields=['positions'])
            
            if account_response.status_code != 200:
                print(f"Position {self.id}: Failed to get account positions, status code: {account_response.status_code}")
                return None
            
            account_data = account_response.json()
            securities_account = account_data.get('securitiesAccount', {})
            positions = securities_account.get('positions', [])
            
            print(f"Position {self.id}: Found {len(positions)} total positions in Schwab account")
            
            # Sum up the absolute market value of all option positions
            # This represents the cost to close all option positions in the account
            total_option_market_value = 0.0
            option_count = 0
            for position in positions:
                instrument = position.get('instrument', {})
                
                # Check if this is an option position
                if instrument.get('assetType') == 'OPTION':
                    market_value = position.get('marketValue', 0)
                    option_count += 1
                    print(f"Position {self.id}: Option {option_count} - Symbol: {instrument.get('symbol', 'N/A')}, Market Value: ${market_value:,.2f}")
                    # Use absolute value because we want the cost to close
                    # Negative market value means we owe money to close (short positions)
                    total_option_market_value += abs(float(market_value))
            
            print(f"Position {self.id}: Total option market value: ${total_option_market_value:,.2f} from {option_count} options")
            
            if total_option_market_value == 0:
                print(f"Position {self.id}: No option positions found, returning None")
                return None
            
            # If there's only one active position, all option market value belongs to it
            if active_positions_count == 1:
                print(f"Position {self.id}: Only 1 active position, using full market value ${total_option_market_value:,.2f}")
                return total_option_market_value
            
            # If there are multiple positions, allocate proportionally based on initial premium
            if total_initial_premium > 0:
                proportion = self.initial_premium_sold / total_initial_premium
                allocated_value = total_option_market_value * proportion
                print(f"Position {self.id}: Multiple positions, allocating {proportion*100:.1f}% = ${allocated_value:,.2f}")
                return allocated_value
            
            print(f"Position {self.id}: Could not allocate, returning None")
            return None
            
        except Exception as e:
            print(f"Error getting current market value for position {self.id}: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    @property 
    def current_open_premium(self):
        """Calculate the current open premium (cost to close position)"""
        try:
            if not self.active:
                return 0.0
            
            position_details = self.get_net_position_details()
            
            # If position is effectively closed, return 0
            if position_details['is_closed']:
                return 0.0
            
            # Try to get real market value first
            # Check if a Schwab cache was injected into this object
            schwab_cache = getattr(self, '_schwab_cache', None)
            market_value = self.get_current_market_value(schwab_cache=schwab_cache)
            if market_value is not None:
                print(f"Position {self.id}: Using real market value ${market_value:,.2f} for current open premium")
                return market_value
            else:
                print(f"Position {self.id}: Real market value not available, using fallback calculation")
            
            # Enhanced fallback calculation
            net_contracts = position_details['net_contracts']
            total_cost_basis = position_details['total_cost_basis']
            
            if abs(net_contracts) < 0.01:
                return 0.0
            
            # For more accurate estimation, let's analyze the order pattern
            # to determine if this is likely a credit spread, debit spread, or naked position
            orders = position_details['orders']
            
            # If we have both buys and sells, this might be a spread
            has_buys = any(order['type'] == 'BUY' for order in orders)
            has_sells = any(order['type'] == 'SELL' for order in orders)
            
            if has_buys and has_sells:
                # This is likely a spread - use a more conservative approach
                # The current cost should be closer to the net premium paid/received
                avg_net_premium_per_contract = total_cost_basis / (abs(net_contracts) * 100) if net_contracts != 0 else 0
                
                # For spreads, use 50% of the net premium as a reasonable estimate
                # This accounts for partial moves in underlying price
                estimated_close_cost = abs(avg_net_premium_per_contract * 0.5)
                return abs(net_contracts) * estimated_close_cost * 100
                
            elif position_details['is_short']:  # Pure short position
                # For naked short positions, use a more conservative time decay
                avg_sell_price = total_cost_basis / (abs(net_contracts) * 100) if net_contracts != 0 else 0
                
                # Calculate position age in hours for more granular decay
                from datetime import datetime, timezone
                position_age_hours = (datetime.now(timezone.utc) - self.opened_datetime).total_seconds() / 3600
                
                # More realistic decay model for short options
                if position_age_hours <= 6:  # First 6 hours
                    decay_factor = 0.95  # 5% decay
                elif position_age_hours <= 24:  # First day
                    decay_factor = 0.85  # 15% decay
                elif position_age_hours <= 168:  # First week
                    decay_factor = 0.60  # 40% decay
                elif position_age_hours <= 720:  # First month
                    decay_factor = 0.30  # 70% decay
                else:
                    decay_factor = 0.10  # 90% decay for very old positions
                
                estimated_current_price = max(avg_sell_price * decay_factor, avg_sell_price * 0.02)
                return abs(net_contracts) * estimated_current_price * 100
                
            elif position_details['is_long']:  # Pure long position
                # For long positions, more aggressive decay since options lose value over time
                avg_buy_price = abs(total_cost_basis) / (abs(net_contracts) * 100) if net_contracts != 0 else 0
                
                from datetime import datetime, timezone
                position_age_hours = (datetime.now(timezone.utc) - self.opened_datetime).total_seconds() / 3600
                
                # Aggressive decay for long options
                if position_age_hours <= 6:  # First 6 hours
                    decay_factor = 0.90  # 10% decay
                elif position_age_hours <= 24:  # First day
                    decay_factor = 0.70  # 30% decay
                elif position_age_hours <= 168:  # First week
                    decay_factor = 0.40  # 60% decay
                elif position_age_hours <= 720:  # First month
                    decay_factor = 0.15  # 85% decay
                else:
                    decay_factor = 0.05  # 95% decay for very old positions
                
                estimated_current_price = max(avg_buy_price * decay_factor, avg_buy_price * 0.01)
                return abs(net_contracts) * estimated_current_price * 100
            
            return 0.0
                
        except Exception as e:
            print(f"Error calculating current open premium for position {self.id}: {e}")
            return 0.0
    
    @property
    def current_pnl(self):
        """Calculate current profit/loss
        
        For credit spreads (most common for PCS):
        - initial_premium_sold = credit received (e.g., $285 for $2.85 credit)
        - current_open_premium = cost to buy back and close (e.g., $250 for $2.50)
        - P&L = credit received - cost to close = $285 - $250 = $35 profit
        
        Example:
        - Sold PCS for $2.85 credit: initial_premium_sold = $285
        - Current market to close is $2.50: current_open_premium = $250
        - P&L = $285 - $250 = $35 profit
        """
        initial = self.initial_premium_sold
        current = self.current_open_premium
        
        # For credit positions (sold for credit), P&L = credit - cost_to_close
        # For debit positions (paid debit), P&L = current_value - debit_paid
        # Since initial_premium_sold is positive for credits and negative for debits,
        # this formula works for both:
        pnl = initial - current
        
        print(f"Position {self.id}: P&L = ${initial:.2f} (initial) - ${current:.2f} (current) = ${pnl:.2f}")
        return pnl
    
    @property
    def current_pnl_percent(self):
        """Calculate current P&L percentage"""
        if abs(self.initial_premium_sold) > 0.01:  # Avoid division by zero
            return (self.current_pnl / abs(self.initial_premium_sold)) * 100
        return 0.0
    
    @property
    def formatted_current_pnl(self):
        """Get formatted current P&L"""
        return f"${self.current_pnl:,.2f}"
    
    @property
    def formatted_current_pnl_percent(self):
        """Get formatted current P&L percentage"""
        return f"{self.current_pnl_percent:+.2f}%"
    
    @property
    def formatted_initial_premium_sold(self):
        """Get formatted initial premium sold"""
        return f"${self.initial_premium_sold:,.2f}"
    
    @property
    def formatted_current_open_premium(self):
        """Get formatted current open premium"""
        return f"${self.current_open_premium:,.2f}"
    
    def get_greeks_from_broker(self, schwab_client=None):
        """Calculate position Greeks by fetching live quotes from Schwab broker API.
        
        This matches LoopTrader Pro's approach in /risk and /positions commands:
        1. Extract option symbols from orderLegCollection
        2. Fetch live quotes with Greeks from broker
        3. Sum Greeks across legs with proper sign (SELL = negative, BUY = positive)
        
        Returns dict with delta, gamma, theta, vega (0.0 if broker unavailable).
        """
        greeks = {
            'delta': 0.0,
            'gamma': 0.0,
            'theta': 0.0,
            'vega': 0.0
        }
        
        try:
            # Find the opening order
            opening_order = None
            for order in self.orders:
                if hasattr(order, 'isOpenPosition') and order.isOpenPosition:
                    opening_order = order
                    break
            
            if not opening_order:
                return greeks
            
            # Check if orderLegCollection exists
            if not hasattr(opening_order, 'orderLegCollection') or not opening_order.orderLegCollection:
                return greeks
            
            # Extract symbols for quotes
            symbols = [leg.instrument.symbol for leg in opening_order.orderLegCollection if leg.instrument]
            if not symbols:
                return greeks
            
            # Get Schwab client if not provided
            if schwab_client is None:
                import os
                import schwab
                from schwab.auth import client_from_token_file
                
                token_path = os.path.join('/app', 'token.json')
                if not os.path.exists(token_path):
                    app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                    token_path = os.path.join(app_root, 'token.json')
                
                if not os.path.exists(token_path):
                    print(f"Position {self.id}: No token.json found, cannot get Greeks from broker")
                    return greeks
                
                api_key = os.getenv('SCHWAB_API_KEY')
                app_secret = os.getenv('SCHWAB_APP_SECRET')
                
                if not api_key or not app_secret:
                    print(f"Position {self.id}: Missing SCHWAB credentials")
                    return greeks
                
                schwab_client = client_from_token_file(token_path, api_key, app_secret)
            
            # Fetch quotes from broker
            import asyncio
            if asyncio.iscoroutinefunction(schwab_client.get_quotes):
                # Async client
                quotes_resp = asyncio.run(schwab_client.get_quotes(symbols))
            else:
                # Sync client
                quotes_resp = schwab_client.get_quotes(symbols)
            
            if not quotes_resp or quotes_resp.status_code != 200:
                print(f"Position {self.id}: Failed to get quotes from broker")
                return greeks
            
            quotes_data = quotes_resp.json()
            
            # Sum Greeks across legs (matching LoopTrader Pro's logic)
            for leg in opening_order.orderLegCollection:
                if not leg.instrument:
                    continue
                
                symbol = leg.instrument.symbol
                quote_info = quotes_data.get(symbol, {}).get('quote', {})
                
                if not quote_info:
                    continue
                
                quantity = leg.quantity if leg.quantity else 0
                
                # Determine sign based on instruction (SELL = negative, BUY = positive)
                # This matches LoopTrader Pro: multiplier = -quantity for SELL, +quantity for BUY
                multiplier = -quantity if leg.instruction and 'SELL' in leg.instruction.upper() else quantity
                
                # Extract Greeks from quote and multiply by 100 (per-contract multiplier)
                delta = quote_info.get('delta', 0.0)
                gamma = quote_info.get('gamma', 0.0)
                theta = quote_info.get('theta', 0.0)
                vega = quote_info.get('vega', 0.0)
                
                greeks['delta'] += multiplier * delta * 100
                greeks['gamma'] += multiplier * gamma * 100
                greeks['theta'] += multiplier * theta * 100
                greeks['vega'] += multiplier * vega * 100
            
            print(f"Position {self.id}: Greeks from broker - Δ{greeks['delta']:.2f}, Γ{greeks['gamma']:.3f}, Θ{greeks['theta']:.2f}, V{greeks['vega']:.2f}")
            return greeks
            
        except Exception as e:
            print(f"Error getting Greeks from broker for position {self.id}: {e}")
            import traceback
            traceback.print_exc()
            return greeks

class TrailingStopState(Base):
    """Trailing stop state model"""
    __tablename__ = "TrailingStopState"
    
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id = mapped_column(Integer, ForeignKey("Bot.id"), nullable=False)
    activation_threshold = mapped_column(Float, nullable=False)
    trailing_percentage = mapped_column(Float, nullable=True)  # Nullable when using dollar mode
    is_active = mapped_column(Boolean, default=False)
    high_water_mark = mapped_column(Float, nullable=True)
    created_at = mapped_column(DateTime, default=datetime.utcnow)
    updated_at = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # New columns for different trailing stop modes
    trailing_mode = mapped_column(String(20), nullable=False, default='percentage')
    trailing_dollar_amount = mapped_column(Float, nullable=True)
    
    # Relationship
    bot = relationship("Bot", back_populates="trailing_stop_state")
    
    def __repr__(self):
        status = "Active" if self.is_active else "Inactive"
        mode = f"{self.trailing_mode.upper()}" if self.trailing_mode else "PERCENTAGE"
        return f"<TrailingStop Bot:{self.bot_id} ({status}, {mode})>"
    
    def validate(self):
        """Validate that the appropriate trailing value is set based on mode"""
        if self.trailing_mode == 'dollar':
            if self.trailing_dollar_amount is None or self.trailing_dollar_amount <= 0:
                raise ValueError("trailing_dollar_amount must be set and positive when trailing_mode is 'dollar'")
        elif self.trailing_mode == 'percentage':
            if self.trailing_percentage is None or self.trailing_percentage <= 0:
                raise ValueError("trailing_percentage must be set and positive when trailing_mode is 'percentage'")
        else:
            raise ValueError(f"Invalid trailing_mode: {self.trailing_mode}. Must be 'percentage' or 'dollar'")
    
    @property
    def status_badge_class(self):
        return "success" if self.is_active else "secondary"
    
    @property
    def status_text(self):
        return "Active" if self.is_active else "Inactive"
    
    @property
    def trailing_display(self):
        """Display trailing amount based on mode"""
        if self.trailing_mode == 'dollar':
            return f"${self.trailing_dollar_amount:.2f}" if self.trailing_dollar_amount else "N/A"
        else:
            return f"{self.trailing_percentage:.1f}%" if self.trailing_percentage else "N/A"

class Order(Base):
    """Order model matching LoopTrader Pro"""
    __tablename__ = "Order"
    
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    orderId = mapped_column(String, nullable=True)
    orderType = mapped_column(String, nullable=True)
    status = mapped_column(String, nullable=True)
    quantity = mapped_column(Float, nullable=True)
    filledQuantity = mapped_column(Float, nullable=True)
    price = mapped_column(Float, nullable=True)
    enteredTime = mapped_column(DateTime, nullable=True)
    position_id = mapped_column(Integer, ForeignKey("Position.id"))
    accountId = mapped_column(Integer, nullable=True)
    isOpenPosition = mapped_column(Boolean, nullable=True, default=False)
    
    # Relationships
    position = relationship("Position", back_populates="orders")
    orderLegCollection = relationship("OrderLeg", back_populates="order", lazy="joined")
    
    def __repr__(self):
        return f"<Order {self.orderId} ({self.status})>"
    
    @property
    def status_badge_class(self):
        if self.status:
            status_upper = self.status.upper()
            if 'FILLED' in status_upper:
                return "success"
            elif 'CANCELLED' in status_upper:
                return "danger"
            elif 'PENDING' in status_upper:
                return "warning"
            else:
                return "info"
        return "secondary"

class OrderLeg(Base):
    """Order leg model matching LoopTrader Pro"""
    __tablename__ = "OrderLeg"
    
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    legId = mapped_column(Integer, nullable=True)
    instruction = mapped_column(String, nullable=True)  # SELL_TO_OPEN, BUY_TO_CLOSE, etc.
    quantity = mapped_column(Integer, nullable=True)
    positionEffect = mapped_column(String, nullable=True)  # OPENING, CLOSING
    order_id = mapped_column(Integer, ForeignKey("Order.id"))
    
    # Relationships
    order = relationship("Order", back_populates="orderLegCollection")
    instrument = relationship("Instrument", back_populates="leg", uselist=False, lazy="joined")
    
    def __repr__(self):
        return f"<OrderLeg {self.instruction} {self.quantity}x>"

class Instrument(Base):
    """Instrument model matching LoopTrader Pro - stores option details
    
    Note: Greeks are stored in the Instrument table in LoopTrader Pro, but only delta
    is present in the current schema. Gamma, theta, vega, rho may be added in future.
    """
    __tablename__ = "Instrument"
    
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    assetType = mapped_column(String, nullable=True)
    cusip = mapped_column(String, nullable=True)
    symbol = mapped_column(String, nullable=True)
    description = mapped_column(String, nullable=True)
    type = mapped_column(String, nullable=True)
    underlyingSymbol = mapped_column(String, nullable=True)
    putCall = mapped_column(String, nullable=True)
    instrumentId = mapped_column(Integer, nullable=True)
    orderLegType = mapped_column(String, nullable=True)
    optionMultiplier = mapped_column(Float, nullable=True)
    
    # Greeks - only delta exists in current schema
    delta = mapped_column(Float, nullable=True)
    # Note: gamma, theta, vega, rho columns don't exist in database yet
    # These will be None when accessed
    
    legId = mapped_column(Integer, ForeignKey("OrderLeg.id"))
    
    # Relationships
    leg = relationship("OrderLeg", back_populates="instrument")
    
    def __repr__(self):
        return f"<Instrument {self.symbol}>"
    
    # Property accessors for missing Greek columns (return None)
    @property
    def gamma(self):
        return None
    
    @property
    def theta(self):
        return None
    
    @property
    def vega(self):
        return None
    
    @property
    def rho(self):
        return None

# Analytics helper functions
def build_schwab_cache_for_positions(positions):
    """Build a cache of Schwab account data with position-level matching
    
    Args:
        positions: List of Position objects
        
    Returns:
        Dictionary mapping position.id to its current market value:
        {position_id: market_value}
    """
    import os
    import schwab
    import json
    
    cache = {}
    
    try:
        # Check if Schwab token is available
        token_path = os.path.join('/app', 'token.json')
        if not os.path.exists(token_path):
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            token_path = os.path.join(app_root, 'token.json')
        
        if not os.path.exists(token_path):
            print("build_schwab_cache: Token file not found")
            return cache
        
        # Load and validate token
        with open(token_path, 'r') as f:
            token_data = json.load(f)
        
        token_info = token_data.get('token', token_data)
        if not all(key in token_info for key in ['access_token', 'refresh_token']):
            print("build_schwab_cache: Token file missing required fields")
            return cache
        
        # Create Schwab client
        client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Get all unique account_ids from positions
        account_ids = set(pos.account_id for pos in positions if pos.account_id and pos.active)
        
        print(f"build_schwab_cache: Processing {len(account_ids)} unique accounts for {len(positions)} positions")
        
        # Get account numbers from Schwab
        accounts_response = client.get_account_numbers()
        if accounts_response.status_code != 200:
            print(f"build_schwab_cache: Failed to get account numbers")
            return cache
        
        accounts_data = accounts_response.json()
        
        db = SessionLocal()
        try:
            # Build account hash mapping
            account_hash_map = {}
            for account_id in account_ids:
                brokerage_account = db.query(BrokerageAccount).filter(
                    BrokerageAccount.account_id == account_id
                ).first()
                
                if not brokerage_account:
                    continue
                
                # Match account by number
                for account in accounts_data:
                    account_number = account.get('accountNumber', '')
                    if (str(brokerage_account.account_id) in str(account_number) or 
                        str(account_number).endswith(str(brokerage_account.account_id))):
                        account_hash_map[account_id] = account.get('hashValue')
                        break
            
            # For each account, get Schwab positions and match to our positions
            for account_id, account_hash in account_hash_map.items():
                # Get account positions from Schwab
                account_response = client.get_account(account_hash, fields=['positions'])
                if account_response.status_code != 200:
                    print(f"build_schwab_cache: Failed to get positions for account {account_id}")
                    continue
                
                account_data_resp = account_response.json()
                securities_account = account_data_resp.get('securitiesAccount', {})
                schwab_positions = securities_account.get('positions', [])
                
                # Build a list of option positions with details
                # CRITICAL: Keep market_value signed (negative for shorts, positive for longs)
                # This allows proper spread calculation where net = short + long
                option_positions = []
                for schwab_pos in schwab_positions:
                    instrument = schwab_pos.get('instrument', {})
                    if instrument.get('assetType') == 'OPTION':
                        market_value = float(schwab_pos.get('marketValue', 0))
                        option_positions.append({
                            'symbol': instrument.get('symbol', ''),
                            'underlying': instrument.get('underlyingSymbol', ''),
                            'description': instrument.get('description', ''),
                            'market_value': market_value,  # Keep sign: negative = short, positive = long
                            'quantity': schwab_pos.get('longQuantity', 0) - schwab_pos.get('shortQuantity', 0),
                            'short_quantity': schwab_pos.get('shortQuantity', 0),
                            'long_quantity': schwab_pos.get('longQuantity', 0)
                        })
                
                print(f"build_schwab_cache: Account {account_id} has {len(option_positions)} option positions")
                
                # Match our positions to Schwab option positions
                db_positions = [p for p in positions if p.account_id == account_id and p.active]
                
                for db_pos in db_positions:
                    # Get bot name to extract underlying symbol
                    bot_name = db_pos.bot.name if db_pos.bot else ""
                    print(f"build_schwab_cache: Matching position {db_pos.id} with bot '{bot_name}'")
                    
                    # Extract underlying symbol from bot name (e.g., "short SPX Put" -> "SPX")
                    underlying_symbol = None
                    for word in bot_name.upper().split():
                        if word in ['SPX', 'SPY', 'QQQ', 'IWM', 'DIA']:  # Common underlyings
                            underlying_symbol = word
                            break
                    
                    if not underlying_symbol:
                        # Try to extract from description or default to SPX
                        underlying_symbol = 'SPX'
                        print(f"build_schwab_cache: Could not extract underlying from '{bot_name}', defaulting to SPX")
                    
                    # Get net position details to understand if we're long or short
                    position_details = db_pos.get_net_position_details()
                    net_contracts = position_details.get('net_contracts', 0)
                    
                    # Match Schwab positions by underlying symbol
                    matched_value = 0.0
                    for opt_pos in option_positions:
                        if opt_pos['underlying'] == underlying_symbol:
                            matched_value += opt_pos['market_value']
                            print(f"build_schwab_cache: Matched {opt_pos['symbol']} - ${opt_pos['market_value']:,.2f}")
                    
                    # If only one DB position for this underlying, assign all matched value to it
                    # Otherwise, split proportionally
                    same_underlying_positions = [p for p in db_positions 
                                                 if p.bot and underlying_symbol in p.bot.name.upper()]
                    
                    if len(same_underlying_positions) == 1:
                        cache[db_pos.id] = matched_value
                        print(f"build_schwab_cache: Position {db_pos.id} gets full value ${matched_value:,.2f}")
                    else:
                        # Split proportionally based on initial premium
                        total_premium = sum(p.initial_premium_sold for p in same_underlying_positions if p.initial_premium_sold > 0)
                        if total_premium > 0 and db_pos.initial_premium_sold > 0:
                            proportion = db_pos.initial_premium_sold / total_premium
                            allocated_value = matched_value * proportion
                            cache[db_pos.id] = allocated_value
                            print(f"build_schwab_cache: Position {db_pos.id} gets {proportion*100:.1f}% = ${allocated_value:,.2f}")
                        else:
                            cache[db_pos.id] = 0.0
        
        finally:
            db.close()
    
    except Exception as e:
        print(f"build_schwab_cache: Error building cache: {e}")
        import traceback
        traceback.print_exc()
    
    return cache

def get_dashboard_stats():
    """Get dashboard statistics with P&L calculation matching looptrader-pro logic"""
    db = SessionLocal()
    try:
        # Get all active positions with schwab cache for P&L calculation
        active_positions = db.query(Position).options(
            joinedload(Position.orders).joinedload(Order.orderLegCollection).joinedload(OrderLeg.instrument)
        ).filter(Position.active == True).all()
        
        # Build schwab cache for all active positions
        schwab_cache = build_schwab_cache_for_positions(active_positions)
        
        # Calculate total P&L
        total_pnl = 0.0
        total_cost_basis = 0.0
        
        for position in active_positions:
            # Inject cache into position
            position._schwab_cache = schwab_cache
            
            # Get opening order
            opening_order = None
            for order in position.orders:
                if order.isOpenPosition:
                    opening_order = order
                    break
            
            if not opening_order or not opening_order.price or not opening_order.quantity:
                continue
            
            # Calculate current market value using the cached value
            market_value = schwab_cache.get(position.id, 0)
            
            # Calculate position P&L based on order type (matches looptrader-pro /risk command)
            entry_price = opening_order.price
            quantity = opening_order.quantity
            
            # Position cost basis
            position_cost = abs(entry_price) * quantity * 100
            
            # Calculate P&L
            if entry_price > 0:  # Credit spread (we received credit)
                # P&L = credit received - current cost to close
                position_pnl = entry_price * quantity * 100 - abs(market_value)
            else:  # Debit spread (we paid debit)
                # P&L = current value - debit paid
                position_pnl = market_value - abs(entry_price) * quantity * 100
            
            total_pnl += position_pnl
            total_cost_basis += position_cost
        
        # Calculate P&L percentage
        total_pnl_pct = (total_pnl / total_cost_basis * 100) if total_cost_basis > 0 else 0
        
        stats = {
            'total_bots': db.query(Bot).count(),
            'active_bots': db.query(Bot).filter(Bot.enabled == True, Bot.paused == False).count(),
            'paused_bots': db.query(Bot).filter(Bot.paused == True).count(),
            'active_positions': db.query(Position).filter(Position.active == True).count(),
            'total_accounts': db.query(BrokerageAccount).count(),
            'trailing_stops': db.query(TrailingStopState).count(),
            'active_trailing_stops': db.query(TrailingStopState).filter(TrailingStopState.is_active == True).count(),
            'total_pnl': total_pnl,
            'total_pnl_pct': total_pnl_pct,
            'total_cost_basis': total_cost_basis,
        }
        return stats
    finally:
        db.close()

def get_recent_positions(limit=10):
    """Get recent positions for dashboard"""
    db = SessionLocal()
    try:
        positions = db.query(Position).options(joinedload(Position.bot)).order_by(Position.opened_datetime.desc()).limit(limit).all()
        return positions
    finally:
        db.close()

def get_bots_by_account():
    """Group bots under every account they've ever had a position with.

    Requirements:
    - A bot must appear under an account even if it currently has no OPEN positions.
    - If a bot has positions across multiple accounts historically, list it under each.
    - Bots with zero positions appear under a synthetic "No Account" group.
    - Bots within each account sorted by bot.id ascending.
    - Provide total historical position count (Bot.total_positions property already added).
    """
    db = SessionLocal()
    try:
        # Use eager loading to load all relationships immediately
        bots = (db.query(Bot)
                  .options(
                      joinedload(Bot.positions),
                      joinedload(Bot.trailing_stop_state)
                  )
                  .all())
        
        # Also eager load accounts with their positions
        accounts = (db.query(BrokerageAccount)
                   .options(joinedload(BrokerageAccount.positions))
                   .all())
        accounts_index = {a.account_id: a for a in accounts}

        class NoAccount:
            def __init__(self):
                self.id = -1
                self.name = "No Account"
                self.account_id = -1
            def __repr__(self):
                return "<NoAccount>"
            @property
            def active_positions(self):
                return 0

        no_account_placeholder = NoAccount()
        bots_by_account: dict = {}

        for bot in bots:
            if bot.positions:
                # Collect distinct account_ids from ALL positions
                account_ids = {p.account_id for p in bot.positions if p.account_id is not None}
                if not account_ids:
                    bots_by_account.setdefault(no_account_placeholder, []).append(bot)
                else:
                    for aid in account_ids:
                        account = accounts_index.get(aid)
                        if account is None:
                            # Fallback to No Account if account vanished
                            bots_by_account.setdefault(no_account_placeholder, []).append(bot)
                        else:
                            bots_by_account.setdefault(account, []).append(bot)
            else:
                bots_by_account.setdefault(no_account_placeholder, []).append(bot)

        # Deduplicate bots per account (if any anomaly added twice) then sort by id
        for acct, bot_list in list(bots_by_account.items()):
            unique = {b.id: b for b in bot_list}
            sorted_bots = sorted(unique.values(), key=lambda b: b.id)
            bots_by_account[acct] = sorted_bots

        return bots_by_account
    finally:
        db.close()


def pause_all_bots():
    """Pause all enabled bots"""
    db = SessionLocal()
    try:
        bots = db.query(Bot).filter(Bot.enabled == True, Bot.paused == False).all()
        count = 0
        for bot in bots:
            bot.paused = True
            count += 1
        db.commit()
        return count
    finally:
        db.close()

def resume_all_bots():
    """Resume all paused bots"""
    db = SessionLocal()
    try:
        bots = db.query(Bot).filter(Bot.paused == True).all()
        count = 0
        for bot in bots:
            bot.paused = False
            # Set state to SLEEPING when resuming from pause
            if bot.state.upper() == 'INITIALIZING':
                bot.state = 'SLEEPING'
            count += 1
        db.commit()
        return count
    finally:
        db.close()

def close_all_positions():
    """Close all active positions"""
    db = SessionLocal()
    try:
        positions = db.query(Position).filter(Position.active == True).all()
        count = 0
        for position in positions:
            position.active = False
            position.closed_datetime = datetime.utcnow()
            count += 1
        db.commit()
        return count
    finally:
        db.close()

def close_position_by_bot(bot_id):
    """Close active position for a specific bot"""
    db = SessionLocal()
    try:
        position = db.query(Position).filter(Position.bot_id == bot_id, Position.active == True).first()
        if position:
            position.active = False
            position.closed_datetime = datetime.utcnow()
            db.commit()
            return True
        return False
    finally:
        db.close()

def update_bot(bot_id: int, name: Optional[str] = None, enabled: Optional[bool] = None, paused: Optional[bool] = None):
    """Update basic bot fields."""
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.id == bot_id).first()
        if not bot:
            return False, "Bot not found"
        changed = False
        if name is not None and name.strip() and bot.name != name.strip():
            bot.name = name.strip()
            changed = True
        if enabled is not None:
            bot.enabled = enabled
            changed = True
            if enabled and paused is None:
                # if enabling, default to unpaused unless explicitly set
                bot.paused = False
        if paused is not None:
            bot.paused = paused
            changed = True
        if changed:
            db.commit()
        return True, "Updated" if changed else "No changes"
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def upsert_trailing_stop(bot_id: int, activation_threshold: float, trailing_percentage: Optional[float] = None, 
                         trailing_dollar_amount: Optional[float] = None, trailing_mode: str = 'percentage',
                         is_active: Optional[bool] = None):
    """Create or update a trailing stop configuration for a bot.
    
    Args:
        bot_id: The bot ID
        activation_threshold: Profit threshold to activate trailing stop
        trailing_percentage: Percentage to trail (for percentage mode)
        trailing_dollar_amount: Dollar amount to trail (for dollar mode)
        trailing_mode: 'percentage' or 'dollar'
        is_active: Whether the trailing stop is active
    """
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.id == bot_id).first()
        if not bot:
            return False, "Bot not found"
        
        # Validate mode and corresponding value
        if trailing_mode == 'percentage' and trailing_percentage is None:
            return False, "trailing_percentage required for percentage mode"
        if trailing_mode == 'dollar' and trailing_dollar_amount is None:
            return False, "trailing_dollar_amount required for dollar mode"
            
        ts = bot.trailing_stop_state
        if ts is None:
            ts = TrailingStopState(
                bot_id=bot.id,
                activation_threshold=activation_threshold,
                trailing_percentage=trailing_percentage,
                trailing_dollar_amount=trailing_dollar_amount,
                trailing_mode=trailing_mode,
                is_active=is_active if is_active is not None else False
            )
            # Validate before adding
            try:
                ts.validate()
            except ValueError as e:
                return False, str(e)
            db.add(ts)
        else:
            ts.activation_threshold = activation_threshold
            ts.trailing_percentage = trailing_percentage
            ts.trailing_dollar_amount = trailing_dollar_amount
            ts.trailing_mode = trailing_mode
            # Only set is_active if explicitly provided (preserve existing state when just updating config)
            if is_active is not None:
                ts.is_active = bool(is_active)
            # Validate before committing
            try:
                ts.validate()
            except ValueError as e:
                return False, str(e)
        db.commit()
        return True, "Trailing stop saved"
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def delete_trailing_stop(bot_id: int):
    """Delete a trailing stop configuration for a bot if it exists."""
    db = SessionLocal()
    try:
        ts = db.query(TrailingStopState).filter(TrailingStopState.bot_id == bot_id).first()
        if not ts:
            return False, "No trailing stop to delete"
        db.delete(ts)
        db.commit()
        return True, "Trailing stop removed"
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

# Initialize database (only create tables if they don't exist)
def init_db():
    """Initialize database tables"""
    # Don't create tables since they should already exist in LoopTrader Pro
    pass

from sqlalchemy import text

def test_connection():
    """Test database connection"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "Database connection successful"
    except Exception as e:
        return False, f"Database connection failed: {e}"

if __name__ == "__main__":
    # Test database connection
    success, message = test_connection()
    if success:
        print(f"✅ {message}")
        stats = get_dashboard_stats()
        print(f"📊 Dashboard Stats: {stats}")
    else:
        print(f"❌ {message}")
