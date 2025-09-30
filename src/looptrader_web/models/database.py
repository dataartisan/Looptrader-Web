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
        """Calculate the initial premium sold (opening transactions)"""
        try:
            total_premium = 0.0
            for order in self.orders:
                if (order.status and 'FILLED' in order.status.upper() and 
                    order.price is not None and order.filledQuantity is not None):
                    # For opening transactions (selling premium), we count positive values
                    # Assuming selling options generates positive premium
                    order_premium = float(order.price) * float(order.filledQuantity) * 100
                    total_premium += order_premium
            return total_premium
        except Exception as e:
            print(f"Error calculating initial premium for position {self.id}: {e}")
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
        """Get current market value from Schwab API if available"""
        # For now, return None to use the account-level Schwab API data
        # Individual position-level market data would require mapping positions
        # to specific option contracts in Schwab
        return None
    
    @property 
    def current_open_premium(self):
        """Calculate the current open premium (estimated cost to close position)"""
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
                return market_value
            
            # Improved fallback estimation based on position analysis
            net_contracts = position_details['net_contracts']
            total_cost_basis = position_details['total_cost_basis']
            
            if abs(net_contracts) < 0.01:
                return 0.0
            
            # Calculate time-based decay estimate using position age
            from datetime import datetime, timezone
            position_age_days = (datetime.now(timezone.utc) - self.opened_datetime).days
            
            if position_details['is_short']:  # Short position
                # For short positions: estimate current cost to buy back
                avg_sell_price = total_cost_basis / (abs(net_contracts) * 100) if net_contracts != 0 else 0
                
                # Time decay benefit for short options (theta positive)
                # More aggressive decay for shorter timeframes
                if position_age_days <= 1:
                    decay_factor = 0.9  # 10% decay in first day
                elif position_age_days <= 7:
                    decay_factor = 0.7  # 30% decay by end of week
                elif position_age_days <= 30:
                    decay_factor = 0.4  # 60% decay by month
                else:
                    decay_factor = 0.1  # 90% decay for older positions
                    
                estimated_current_price = max(avg_sell_price * decay_factor, avg_sell_price * 0.05)
                return abs(net_contracts) * estimated_current_price * 100
                
            elif position_details['is_long']:  # Long position
                # For long positions: estimate current value (what you could sell for)
                avg_buy_price = abs(total_cost_basis) / (abs(net_contracts) * 100) if net_contracts != 0 else 0
                
                # Time decay cost for long options (theta negative)
                if position_age_days <= 1:
                    decay_factor = 0.8  # 20% decay in first day
                elif position_age_days <= 7:
                    decay_factor = 0.5  # 50% decay by end of week
                elif position_age_days <= 30:
                    decay_factor = 0.2  # 80% decay by month
                else:
                    decay_factor = 0.05  # 95% decay for older positions
                    
                estimated_current_price = max(avg_buy_price * decay_factor, avg_buy_price * 0.01)
                return abs(net_contracts) * estimated_current_price * 100
            
            return 0.0
                
        except Exception as e:
            print(f"Error calculating current open premium for position {self.id}: {e}")
            return 0.0
    
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
