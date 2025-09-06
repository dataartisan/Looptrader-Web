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
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

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
        return len(self.positions) if self.positions else 0
    
    @property
    def active_positions(self):
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
        return len([p for p in self.positions if p.active])

    @property
    def total_positions(self):
        return len(self.positions)
    
    @property
    def account_name(self):
        """Get account name for this bot"""
        if self.positions:
            # Get account from most recent position
            recent_position = max(self.positions, key=lambda p: p.opened_datetime)
            db = SessionLocal()
            try:
                account = db.query(BrokerageAccount).filter(BrokerageAccount.account_id == recent_position.account_id).first()
                return account.name if account else "Unknown"
            finally:
                db.close()
        return "No Account"
    
    @property
    def account_id_value(self):
        """Get account ID for this bot"""
        if self.positions:
            recent_position = max(self.positions, key=lambda p: p.opened_datetime)
            return recent_position.account_id
        return None

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
            days = delta.days
            hours = delta.seconds // 3600
            if days > 0:
                return f"{days}d {hours}h"
            else:
                return f"{hours}h"
        else:
            delta = self.closed_datetime - self.opened_datetime
            return f"{delta.days}d {delta.seconds // 3600}h"
    
    @property
    def account_name(self):
        """Get account name for this position"""
        db = SessionLocal()
        try:
            account = db.query(BrokerageAccount).filter(BrokerageAccount.account_id == self.account_id).first()
            return account.name if account else "Unknown"
        finally:
            db.close()

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
        positions = db.query(Position).order_by(Position.opened_datetime.desc()).limit(limit).all()
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
        bots = (db.query(Bot)
                  .options(joinedload(Bot.positions), joinedload(Bot.trailing_stop_state))
                  .all())
        accounts_index = {a.account_id: a for a in db.query(BrokerageAccount).all()}

        class NoAccount:
            def __init__(self):
                self.id = -1
                self.name = "No Account"
                self.account_id = -1
                self.positions = []
            def __repr__(self):
                return "<NoAccount>"
            @property
            def total_positions(self):
                return 0
            @property
            def active_positions_count(self):
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
            bots_by_account[acct] = sorted(unique.values(), key=lambda b: b.id)

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
            # Always set is_active explicitly so unchecking the box persists
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
