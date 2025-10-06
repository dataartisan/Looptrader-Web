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
        """Calculate the net initial premium (total credit received - total debit paid)"""
        try:
            total_premium = 0.0
            order_count = 0
            
            print(f"Position {self.id}: Calculating initial_premium_sold, found {len(self.orders)} orders")
            
            for order in self.orders:
                print(f"Position {self.id}: Order {order.orderId} - Status: {order.status}, Type: {order.orderType}, Price: {order.price}, Qty: {order.filledQuantity}")
                
                if (order.status and 'FILLED' in order.status.upper() and 
                    order.price is not None and order.filledQuantity is not None):
                    
                    order_premium = float(order.price) * float(order.filledQuantity) * 100
                    order_count += 1
                    
                    # Check order type to determine if this is a credit (SELL) or debit (BUY)
                    if order.orderType and 'SELL' in order.orderType.upper():
                        # Selling options = receiving premium (positive)
                        total_premium += order_premium
                        print(f"Position {self.id}: Order #{order_count} SELL - adding ${order_premium:,.2f}")
                    elif order.orderType and 'BUY' in order.orderType.upper():
                        # Buying options = paying premium (negative)
                        total_premium -= order_premium
                        print(f"Position {self.id}: Order #{order_count} BUY - subtracting ${order_premium:,.2f}")
                    else:
                        # If order type is unknown, assume it's a SELL (credit) for backward compatibility
                        # This maintains the original behavior where all premiums were added
                        total_premium += order_premium
                        print(f"Position {self.id}: Order #{order_count} UNKNOWN type '{order.orderType}' - assuming SELL, adding ${order_premium:,.2f}")
            
            print(f"Position {self.id}: Total initial premium (net) from {order_count} filled orders: ${total_premium:,.2f}")
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
    
    def get_current_market_value(self):
        """Get the current market value from Schwab account data
        
        Returns the cost to close this position based on real-time market data.
        If there's only one active position in the account, returns the total option market value.
        If there are multiple positions, returns a proportional amount based on this position's
        initial premium relative to all active positions in the account.
        """
        try:
            # Import here to avoid circular imports
            import os
            import schwab
            import json
            
            print(f"Position {self.id}: Starting get_current_market_value()")
            
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
            market_value = self.get_current_market_value()
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
        
        Formula: Initial Premium (net credit/debit) - Current Cost to Close
        
        Example for a credit spread:
        - Sold options for $1000, bought options for $300 = $700 net credit (initial_premium_sold)
        - Current cost to close is $200 (current_open_premium)
        - P&L = $700 - $200 = $500 profit
        
        Example for a debit spread:
        - Sold options for $300, bought options for $1000 = -$700 net debit (initial_premium_sold is negative)
        - Current value to sell is $500 (current_open_premium)
        - P&L = -$700 - $500 = -$1200 loss (or $500 - $700 = -$200 if position reversed)
        """
        return self.initial_premium_sold - self.current_open_premium
    
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

class TrailingStopState(Base):
    """Trailing stop state model"""
    __tablename__ = "TrailingStopState"
    
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id = mapped_column(Integer, ForeignKey("Bot.id"), nullable=False)
    activation_threshold = mapped_column(Float, nullable=False)
    trailing_percentage = mapped_column(Float, nullable=False)
    is_active = mapped_column(Boolean, default=False)
    entry_value = mapped_column(Float, nullable=True)
    high_water_mark = mapped_column(Float, nullable=True)
    created_at = mapped_column(DateTime, default=datetime.utcnow)
    updated_at = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    bot = relationship("Bot", back_populates="trailing_stop_state")
    
    def __repr__(self):
        status = "Active" if self.is_active else "Inactive"
        return f"<TrailingStop Bot:{self.bot_id} ({status})>"
    
    @property
    def status_badge_class(self):
        return "success" if self.is_active else "secondary"
    
    @property
    def status_text(self):
        return "Active" if self.is_active else "Inactive"

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
    
    # Relationships
    position = relationship("Position", back_populates="orders")
    
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

# Analytics helper functions
def get_dashboard_stats():
    """Get dashboard statistics"""
    db = SessionLocal()
    try:
        stats = {
            'total_bots': db.query(Bot).count(),
            'active_bots': db.query(Bot).filter(Bot.enabled == True, Bot.paused == False).count(),
            'paused_bots': db.query(Bot).filter(Bot.paused == True).count(),
            'active_positions': db.query(Position).filter(Position.active == True).count(),
            'total_accounts': db.query(BrokerageAccount).count(),
            'trailing_stops': db.query(TrailingStopState).count(),
            'active_trailing_stops': db.query(TrailingStopState).filter(TrailingStopState.is_active == True).count(),
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

def upsert_trailing_stop(bot_id: int, activation_threshold: float, trailing_percentage: float, is_active: Optional[bool] = None):
    """Create or update a trailing stop configuration for a bot."""
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.id == bot_id).first()
        if not bot:
            return False, "Bot not found"
        ts = bot.trailing_stop_state
        if ts is None:
            ts = TrailingStopState(
                bot_id=bot.id,
                activation_threshold=activation_threshold,
                trailing_percentage=trailing_percentage,
                is_active=is_active if is_active is not None else False
            )
            db.add(ts)
        else:
            ts.activation_threshold = activation_threshold
            ts.trailing_percentage = trailing_percentage
            # Only set is_active if explicitly provided (preserve existing state when just updating config)
            if is_active is not None:
                ts.is_active = bool(is_active)
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
