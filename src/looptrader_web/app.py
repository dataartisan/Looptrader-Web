"""
LoopTrader Web Interface with AdminLTE
A comprehensive web dashboard for managing LoopTrader Pro bots
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
import os
import requests
import json
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from dotenv import load_dotenv
import pytz

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Load environment variables from .env file
load_dotenv()

# Import our database models
from models.database import (
    get_db, Bot, Position, TrailingStopState, Order, OrderLeg, Instrument, BrokerageAccount,
    get_dashboard_stats, get_recent_positions, get_bots_by_account,
    pause_all_bots, resume_all_bots, close_all_positions, close_position_by_bot,
    SessionLocal, test_connection, update_bot, upsert_trailing_stop, delete_trailing_stop,
    build_schwab_cache_for_positions
)
from sqlalchemy.orm import joinedload
from sqlalchemy import text

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Add timezone filter for templates
@app.template_filter('to_est')
def to_est(utc_dt):
    """Convert UTC datetime to EST/EDT"""
    if utc_dt is None:
        return "N/A"
    
    utc_tz = pytz.UTC
    est_tz = pytz.timezone('America/New_York')
    
    # If the datetime is naive, assume it's UTC
    if utc_dt.tzinfo is None:
        utc_dt = utc_tz.localize(utc_dt)
    
    # Convert to EST/EDT
    est_dt = utc_dt.astimezone(est_tz)
    return est_dt.strftime('%Y-%m-%d %H:%M EST')

# Configure Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access the dashboard.'
login_manager.login_message_category = 'info'

# Simple User class for authentication
class User(UserMixin):
    def __init__(self, username):
        self.id = username
        self.username = username

# Load credentials from environment
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')  # plain text fallback
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH')  # hashed optional

@login_manager.user_loader
def load_user(user_id):
    if user_id == ADMIN_USERNAME:
        return User(ADMIN_USERNAME)
    return None

# Template context processor for common variables
@app.context_processor
def inject_template_vars():
    return {
        'app_name': 'LoopTrader Pro',
        'current_year': datetime.now().year
    }

def get_spx_price():
    """Fetch current SPX spot price using Schwab API"""
    try:
        import schwab
        from schwab.auth import client_from_token_file
        
        # Determine the correct path for token.json
        if os.path.exists('/app/token.json'):
            token_path = '/app/token.json'
        else:
            app_root = os.path.dirname(os.path.abspath(__file__))
            token_path = os.path.join(app_root, 'token.json')
        
        # Initialize Schwab client
        client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Get SPX quote from Schwab
        response = client.get_quote('$SPX')
        
        if response.status_code == 200:
            data = response.json()
            
            # Extract quote data
            if '$SPX' in data and 'quote' in data['$SPX']:
                quote = data['$SPX']['quote']
                
                price = quote.get('lastPrice', 0)
                close_price = quote.get('closePrice', price)
                open_price = quote.get('openPrice', close_price)
                
                # Calculate change and change percent
                change = price - close_price
                change_percent = (change / close_price * 100) if close_price > 0 else 0
                
                # Determine market state based on market hours
                # Schwab provides 52WeekHigh/Low but not market state directly
                # We'll determine based on time
                now = datetime.now()
                market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
                market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
                
                if market_open <= now <= market_close and now.weekday() < 5:
                    market_state = 'REGULAR'
                else:
                    market_state = 'CLOSED'
                
                # Convert to US/Central timezone (handles DST automatically)
                now_utc = datetime.now(timezone.utc)
                central_tz = pytz.timezone('US/Central')
                central_time = now_utc.astimezone(central_tz)
                # Get the current timezone abbreviation (CST or CDT)
                tz_abbr = central_time.strftime('%Z')
                timestamp = central_time.strftime(f'%Y-%m-%d %I:%M %p {tz_abbr}')
                
                return {
                    'price': round(price, 2),
                    'change': round(change, 2),
                    'change_percent': round(change_percent, 2),
                    'market_state': market_state,
                    'previous_close': round(close_price, 2),
                    'timestamp': timestamp
                }
        
        print(f"Failed to get SPX quote: {response.status_code}")
        
    except Exception as e:
        print(f"Error fetching SPX price from Schwab: {e}")
        import traceback
        traceback.print_exc()
    
    # Return default values if API fails
    now_utc = datetime.now(timezone.utc)
    central_tz = pytz.timezone('US/Central')
    central_time = now_utc.astimezone(central_tz)
    tz_abbr = central_time.strftime('%Z')
    timestamp = central_time.strftime(f'%Y-%m-%d %I:%M %p {tz_abbr}')
    
    return {
        'price': 'N/A',
        'change': 'N/A',
        'change_percent': 'N/A',
        'market_state': 'UNKNOWN',
        'previous_close': 'N/A',
        'timestamp': timestamp
    }

# Authentication routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')

        valid = False
        if username == ADMIN_USERNAME:
            if ADMIN_PASSWORD_HASH:
                try:
                    valid = check_password_hash(ADMIN_PASSWORD_HASH, password)
                except Exception:
                    valid = False
            elif ADMIN_PASSWORD is not None:
                valid = (password == ADMIN_PASSWORD)
        if valid:
            user = User(ADMIN_USERNAME)
            login_user(user, remember=True)
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials', 'danger')
    
    return render_template('auth/login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out successfully', 'info')
    return redirect(url_for('login'))

# Dashboard routes
@app.route('/')
@login_required
def dashboard():
    try:
        stats = get_dashboard_stats()
        try:
            ok, _ = test_connection()
            db_status = 'connected' if ok else 'error'
        except Exception:
            db_status = 'error'
        recent_positions = get_recent_positions(5)
        spx_data = get_spx_price()
        balance_data = get_schwab_account_balance()
        return render_template('dashboard.html', stats=stats, recent_positions=recent_positions, db_status=db_status, spx_data=spx_data, balance_data=balance_data)
    except Exception as e:
        flash(f'Error loading dashboard: {str(e)}', 'danger')
        now_utc = datetime.now(timezone.utc)
        cst_time = now_utc - timedelta(hours=6)
        timestamp = cst_time.strftime('%Y-%m-%d %I:%M %p CST')
        return render_template('dashboard.html', stats={}, recent_positions=[], db_status='error', spx_data={'price': 'N/A', 'change': 'N/A', 'change_percent': 'N/A', 'market_state': 'UNKNOWN', 'previous_close': 'N/A', 'timestamp': timestamp}, balance_data={'total_balance': 'N/A', 'error': 'Dashboard load error'})

# Bot management routes
@app.route('/bots')
@login_required
def bots():
    db = SessionLocal()
    try:
        # Eager load all relationships to prevent lazy loading errors
        bots_query = (db.query(Bot)
                     .options(
                         joinedload(Bot.positions),
                         joinedload(Bot.trailing_stop_state)
                     ))
        
        accounts_query = (db.query(BrokerageAccount)
                         .options(joinedload(BrokerageAccount.positions)))
        
        bots = bots_query.all()
        accounts = accounts_query.all()
        accounts_index = {a.account_id: a for a in accounts}
        
        # Force evaluate all relationships while session is active
        for bot in bots:
            _ = list(bot.positions)  # Force loading
            if bot.trailing_stop_state:
                _ = bot.trailing_stop_state.id
        
        for account in accounts:
            _ = list(account.positions)  # Force loading
        
        # Build bots_by_account structure
        class NoAccount:
            def __init__(self):
                self.id = -1
                self.name = "No Account"
                self.account_id = -1
                self.active_positions = 0  # Pre-computed value

        no_account_placeholder = NoAccount()
        bots_by_account = {}

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
                            bots_by_account.setdefault(no_account_placeholder, []).append(bot)
                        else:
                            bots_by_account.setdefault(account, []).append(bot)
            else:
                bots_by_account.setdefault(no_account_placeholder, []).append(bot)

        # Pre-compute account active_positions while session is still open
        for account in bots_by_account.keys():
            if hasattr(account, 'positions'):  # Real account, not NoAccount
                # Compute and store as a simple attribute (not property)
                account._computed_active_positions = len([p for p in account.positions if p.active])
            # else: NoAccount already has active_positions = 0

        # Deduplicate and sort
        for acct, bot_list in list(bots_by_account.items()):
            unique = {b.id: b for b in bot_list}
            sorted_bots = sorted(unique.values(), key=lambda b: b.id)
            bots_by_account[acct] = sorted_bots

        # Unfiltered counts (before any filter) - compute while session is active
        all_total_bots = sum(len(blist) for blist in bots_by_account.values())
        all_active_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.enabled and not b.paused)
        all_inactive_bots = sum(1 for blist in bots_by_account.values() for b in blist if (not b.enabled) or b.paused)

        flt = request.args.get('filter')  # 'active' | 'inactive' | None
        if flt in ('active', 'inactive'):
            filtered = {}
            for account, blist in bots_by_account.items():
                if flt == 'active':
                    subset = [b for b in blist if b.enabled and not b.paused]
                else:  # inactive
                    subset = [b for b in blist if (not b.enabled) or b.paused]
                if subset:
                    filtered[account] = subset
            bots_by_account = filtered

        total_bots = sum(len(blist) for blist in bots_by_account.values())
        active_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.enabled and not b.paused)
        paused_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.paused)

        return render_template('bots/list.html',
                               bots_by_account=bots_by_account,
                               total_bots=total_bots,
                               active_bots=active_bots,
                               paused_bots=paused_bots,
                               total_accounts=len(bots_by_account),
                               current_filter=flt,
                               all_total_bots=all_total_bots,
                               all_active_bots=all_active_bots,
                               all_inactive_bots=all_inactive_bots)
    except Exception as e:
        flash(f'Error loading bots: {str(e)}', 'danger')
        return render_template('bots/list.html', 
                               bots_by_account={}, 
                               total_bots=0, 
                               active_bots=0, 
                               paused_bots=0, 
                               total_accounts=0, 
                               current_filter=None,
                               all_total_bots=0, 
                               all_active_bots=0, 
                               all_inactive_bots=0)
    finally:
        db.close()

@app.route('/bots/<int:bot_id>')
@login_required
def bot_detail(bot_id):
    try:
        db = SessionLocal()
        try:
            bot = db.query(Bot).filter(Bot.id == bot_id).first()
            if not bot:
                flash('Bot not found', 'danger')
                return redirect(url_for('bots'))
            
            # Get bot positions
            positions = db.query(Position).filter(Position.bot_id == bot_id).order_by(Position.opened_datetime.desc()).all()
            
            return render_template('bots/detail.html', bot=bot, positions=positions)
        finally:
            db.close()
    except Exception as e:
        flash(f'Error loading bot: {str(e)}', 'danger')
        return redirect(url_for('bots'))

# Bot action routes
@app.route('/bots/<int:bot_id>/pause', methods=['POST'])
@login_required
def pause_bot(bot_id):
    try:
        db = SessionLocal()
        try:
            bot = db.query(Bot).filter(Bot.id == bot_id).first()
            if bot:
                bot.paused = True
                db.commit()
                return jsonify({'success': True, 'message': 'Bot paused successfully'})
            else:
                return jsonify({'success': False, 'message': 'Bot not found'})
        finally:
            db.close()
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/bots/<int:bot_id>/resume', methods=['POST'])
@login_required
def resume_bot(bot_id):
    try:
        db = SessionLocal()
        try:
            bot = db.query(Bot).filter(Bot.id == bot_id).first()
            if bot:
                bot.paused = False
                # Set state to SLEEPING when resuming from pause
                if bot.state.upper() == 'INITIALIZING':
                    bot.state = 'SLEEPING'
                db.commit()
                return jsonify({'success': True, 'message': 'Bot resumed successfully'})
            else:
                return jsonify({'success': False, 'message': 'Bot not found'})
        finally:
            db.close()
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/bots/<int:bot_id>/enable', methods=['POST'])
@login_required
def enable_bot(bot_id):
    try:
        db = SessionLocal()
        try:
            bot = db.query(Bot).filter(Bot.id == bot_id).first()
            if bot:
                bot.enabled = True
                bot.paused = False
                db.commit()
                return jsonify({'success': True, 'message': 'Bot enabled successfully'})
            else:
                return jsonify({'success': False, 'message': 'Bot not found'})
        finally:
            db.close()
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/bots/<int:bot_id>/update', methods=['POST'])
@login_required
def update_bot_route(bot_id):
    try:
        name = request.form.get('name')
        enabled = request.form.get('enabled')
        paused = request.form.get('paused')
        enabled_val = None if enabled is None else (enabled.lower() == 'true')
        paused_val = None if paused is None else (paused.lower() == 'true')
        ok, msg = update_bot(bot_id, name=name, enabled=enabled_val, paused=paused_val)
        if ok:
            flash('Bot updated', 'success')
        else:
            flash(f'Update failed: {msg}', 'danger')
        return redirect(url_for('bot_detail', bot_id=bot_id))
    except Exception as e:
        flash(f'Error updating bot: {e}', 'danger')
        return redirect(url_for('bot_detail', bot_id=bot_id))

@app.route('/bots/<int:bot_id>/trailing-stop', methods=['POST'])
@login_required
def upsert_trailing_stop_route(bot_id):
    try:
        activation_threshold = request.form.get('activation_threshold', type=float)
        trailing_mode = request.form.get('trailing_mode', 'percentage')
        trailing_percentage = request.form.get('trailing_percentage', type=float)
        trailing_dollar_amount = request.form.get('trailing_dollar_amount', type=float)
        
        if activation_threshold is None:
            raise ValueError("Activation threshold is required")
        
        if trailing_mode == 'percentage' and trailing_percentage is None:
            raise ValueError("Trailing percentage is required for percentage mode")
        
        if trailing_mode == 'dollar' and trailing_dollar_amount is None:
            raise ValueError("Trailing dollar amount is required for dollar mode")
        
        # Debug print to container logs for verification
        if trailing_mode == 'percentage':
            print(f"[TrailingStopUpdate] bot_id={bot_id} activation={activation_threshold} trailing={trailing_percentage}% mode=percentage")
        else:
            print(f"[TrailingStopUpdate] bot_id={bot_id} activation={activation_threshold} trailing=${trailing_dollar_amount} mode=dollar")
        
        # Call upsert with mode-specific parameters
        ok, msg = upsert_trailing_stop(
            bot_id, 
            activation_threshold, 
            trailing_percentage=trailing_percentage if trailing_mode == 'percentage' else None,
            trailing_dollar_amount=trailing_dollar_amount if trailing_mode == 'dollar' else None,
            trailing_mode=trailing_mode
        )
        
        if ok:
            mode_label = f"{trailing_percentage}%" if trailing_mode == 'percentage' else f"${trailing_dollar_amount}"
            flash(f'Trailing stop saved ({trailing_mode}: {mode_label})', 'success')
        else:
            flash(f'Failed to save trailing stop: {msg}', 'danger')
    except Exception as e:
        flash(f'Error saving trailing stop: {e}', 'danger')
    return redirect(url_for('bot_detail', bot_id=bot_id))

@app.route('/bots/<int:bot_id>/trailing-stop/delete', methods=['POST'])
@login_required
def delete_trailing_stop_route(bot_id):
    try:
        ok, msg = delete_trailing_stop(bot_id)
        if ok:
            flash('Trailing stop removed', 'success')
        else:
            flash(msg, 'warning')
    except Exception as e:
        flash(f'Error deleting trailing stop: {e}', 'danger')
    return redirect(url_for('bot_detail', bot_id=bot_id))

# Bulk action routes
@app.route('/pauseall', methods=['POST'])
@login_required
def pause_all():
    try:
        count = pause_all_bots()
        return jsonify({'success': True, 'count': count, 'message': f'{count} bots paused successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/resumeall', methods=['POST'])
@login_required
def resume_all():
    try:
        count = resume_all_bots()
        return jsonify({'success': True, 'count': count, 'message': f'{count} bots resumed successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/closeall', methods=['POST'])
@login_required
def close_all():
    try:
        count = close_all_positions()
        return jsonify({'success': True, 'count': count, 'message': f'{count} positions closed successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/pause_selected', methods=['POST'])
@login_required
def pause_selected():
    try:
        bot_ids = request.form.getlist('bot_ids')
        if not bot_ids:
            return jsonify({'success': False, 'message': 'No bots selected'})
        
        # Convert to integers and pause each bot
        count = 0
        db = SessionLocal()
        try:
            for bot_id in bot_ids:
                try:
                    bot_id_int = int(bot_id)
                    bot = db.query(Bot).filter(Bot.id == bot_id_int).first()
                    if bot and not bot.paused:
                        bot.paused = True
                        count += 1
                except ValueError:
                    continue  # Skip invalid bot IDs
            
            db.commit()
            return jsonify({'success': True, 'count': count, 'message': f'{count} bots paused successfully'})
        finally:
            db.close()
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/resume_selected', methods=['POST'])
@login_required
def resume_selected():
    try:
        bot_ids = request.form.getlist('bot_ids')
        if not bot_ids:
            return jsonify({'success': False, 'message': 'No bots selected'})
        
        # Convert to integers and resume each bot
        count = 0
        db = SessionLocal()
        try:
            for bot_id in bot_ids:
                try:
                    bot_id_int = int(bot_id)
                    bot = db.query(Bot).filter(Bot.id == bot_id_int).first()
                    if bot and bot.paused:
                        bot.paused = False
                        # Set state to SLEEPING when resuming from pause
                        if bot.state.upper() == 'INITIALIZING':
                            bot.state = 'SLEEPING'
                        count += 1
                except ValueError:
                    continue  # Skip invalid bot IDs
            
            db.commit()
            return jsonify({'success': True, 'count': count, 'message': f'{count} bots resumed successfully'})
        finally:
            db.close()
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# Account management routes
@app.route('/accounts')
@login_required
def accounts():
    try:
        db = SessionLocal()
        try:
            accounts = db.query(BrokerageAccount).all()
            # Get Schwab account details
            schwab_accounts = get_schwab_accounts_detail()
            return render_template('accounts/list.html', accounts=accounts, schwab_accounts=schwab_accounts)
        finally:
            db.close()
    except Exception as e:
        print(f"Error loading accounts: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error loading accounts: {str(e)}', 'danger')
        # Provide empty schwab_accounts in case of error
        schwab_accounts = {'accounts': [], 'error': 'Failed to load Schwab data'}
        return render_template('accounts/list.html', accounts=[], schwab_accounts=schwab_accounts)

# Position management routes
@app.route('/positions')
@login_required
def positions():
    """Display all positions with P&L calculations matching looptrader-pro /positions command"""
    try:
        db = SessionLocal()
        try:
            from sqlalchemy.orm import joinedload
            from datetime import datetime, timezone
            
            # Get filter parameters
            account_filter = request.args.get('account')
            status_filter = request.args.get('status')
            active_only = request.args.get('active_only')
            
            # Query positions matching looptrader-pro's /positions command approach
            # Get all bots first, then get active position for each bot
            # This ensures we only process positions that have valid opening orders
            bots = db.query(Bot).all()
            accounts = db.query(BrokerageAccount).all()
            
            # Collect valid positions by querying through bots (matches looptrader-pro)
            valid_positions = []
            position_data = []  # Enhanced data with entry/current prices, strikes, etc.
            
            for bot in bots:
                try:
                    # Get active position for this bot
                    db_position = db.query(Position).options(
                        joinedload(Position.orders).joinedload(Order.orderLegCollection).joinedload(OrderLeg.instrument),
                        joinedload(Position.bot)
                    ).filter(
                        Position.bot_id == bot.id,
                        Position.active == True
                    ).first()
                    
                    # Apply active filter
                    if active_only == 'true' and (db_position is None or not db_position.active):
                        continue
                    
                    # If we want all positions, also check closed ones
                    if active_only != 'true' and db_position is None:
                        # Try to get any position for this bot (including closed)
                        db_position = db.query(Position).options(
                            joinedload(Position.orders).joinedload(Order.orderLegCollection).joinedload(OrderLeg.instrument),
                            joinedload(Position.bot)
                        ).filter(
                            Position.bot_id == bot.id
                        ).order_by(Position.opened_datetime.desc()).first()
                    
                    if db_position is None:
                        continue
                    
                    # Apply account filter
                    if account_filter and str(db_position.account_id) != str(account_filter):
                        continue
                    
                    # Validate position has opening order with orderLegCollection and price (matches looptrader-pro)
                    opening_order = next(
                        (order for order in db_position.orders if hasattr(order, 'isOpenPosition') and order.isOpenPosition),
                        None
                    )
                    
                    if opening_order is None or not opening_order.orderLegCollection or opening_order.price is None:
                        if db_position.active:
                            logger.warning(f"Bot {bot.id} ({bot.name}) has position {db_position.id} but no valid opening order, skipping")
                        continue
                    
                    # Position is valid, add to list
                    valid_positions.append(db_position)
                    
                    # Extract additional data for display (matching looptrader-pro format)
                    # Get strikes from symbols
                    position_strikes = []
                    for leg in opening_order.orderLegCollection:
                        if leg.instrument and leg.instrument.symbol:
                            symbol = leg.instrument.symbol
                            try:
                                if len(symbol) >= 7:
                                    strike_part = symbol[-7:]
                                    strike_value = float(strike_part) / 1000
                                    position_strikes.append(f"${strike_value:.0f}")
                            except (ValueError, IndexError):
                                continue
                    
                    strikes_str = "/".join(position_strikes) if position_strikes else "N/A"
                    
                    # Calculate entry price per contract
                    quantity = opening_order.quantity if opening_order.quantity else opening_order.filledQuantity or 1
                    entry_price_per_contract = abs(opening_order.price) if opening_order.price else 0.0
                    
                    # Calculate duration
                    duration_text = "Unknown"
                    entry_time_str = "Unknown"
                    if db_position.opened_datetime:
                        entry_time = db_position.opened_datetime
                        if entry_time.tzinfo is None:
                            entry_time = entry_time.replace(tzinfo=timezone.utc)
                        now = datetime.now(timezone.utc)
                        duration = now - entry_time
                        hours = int(duration.total_seconds() / 3600)
                        minutes = int((duration.total_seconds() % 3600) / 60)
                        duration_text = f"{hours}h {minutes}m"
                        entry_time_str = entry_time.strftime("%H:%M ET")
                    
                    # Get account name
                    account_name = "Unknown"
                    for account in accounts:
                        if account.account_id == db_position.account_id:
                            account_name = account.name
                            break
                    
                    # Store enhanced position data
                    position_data.append({
                        'position': db_position,
                        'bot_id': bot.id,
                        'bot_name': bot.name,
                        'account_name': account_name,
                        'account_id': db_position.account_id,
                        'strikes': strikes_str,
                        'entry_price_per_contract': entry_price_per_contract,
                        'entry_time': entry_time_str,
                        'duration': duration_text,
                        'quantity': quantity
                    })
                    
                except Exception as e:
                    logger.error(f"Error processing bot {bot.id} ({bot.name}): {e}", exc_info=True)
                    continue
            
            logger.info(f"Found {len(valid_positions)} valid positions")
            
            # Build Schwab cache for active positions to get real-time market values
            # This matches looptrader-pro /positions command approach
            active_positions = [p for p in valid_positions if p.active]
            if active_positions:
                try:
                    from models.database import build_schwab_cache_for_positions
                    schwab_cache = build_schwab_cache_for_positions(active_positions)
                    
                    # Inject cache into each position for P&L calculation
                    for position in valid_positions:
                        position._schwab_cache = schwab_cache
                except Exception as e:
                    logger.error(f"Error building Schwab cache: {e}", exc_info=True)
                    # Continue without cache - positions will use fallback calculation
            
            # Calculate per-account and overall summaries (matching looptrader-pro)
            from collections import defaultdict
            account_groups = defaultdict(list)
            for pos_data in position_data:
                account_name = pos_data['account_name']
                account_groups[account_name].append(pos_data)
            
            account_summaries = {}
            total_pnl = 0.0
            total_count = 0
            total_winning = 0
            total_losing = 0
            total_pnl_pct_sum = 0.0  # For calculating average percentage
            
            for account_name, positions_list in account_groups.items():
                account_pnl = 0.0
                account_positions_active = [p for p in positions_list if p['position'].active]
                for p in account_positions_active:
                    try:
                        account_pnl += p['position'].current_pnl
                    except Exception as e:
                        logger.warning(f"Error calculating P&L for position {p['position'].id}: {e}")
                        continue
                
                account_avg_pct = 0.0
                if account_positions_active:
                    pct_sum = 0.0
                    pct_count = 0
                    for p in account_positions_active:
                        try:
                            pct_sum += p['position'].current_pnl_percent
                            pct_count += 1
                        except Exception as e:
                            logger.warning(f"Error calculating P&L % for position {p['position'].id}: {e}")
                            continue
                    account_avg_pct = (pct_sum / pct_count) if pct_count > 0 else 0.0
                
                account_winning = 0
                account_losing = 0
                for p in account_positions_active:
                    try:
                        if p['position'].current_pnl > 0:
                            account_winning += 1
                        elif p['position'].current_pnl < 0:
                            account_losing += 1
                    except Exception as e:
                        logger.warning(f"Error checking win/loss for position {p['position'].id}: {e}")
                        continue
                
                account_summaries[account_name] = {
                    'pnl': account_pnl,
                    'avg_pct': account_avg_pct,
                    'winning': account_winning,
                    'losing': account_losing,
                    'count': len(account_positions_active)
                }
                
                total_pnl += account_pnl
                total_count += len(account_positions_active)
                total_winning += account_winning
                total_losing += account_losing
                for p in account_positions_active:
                    try:
                        total_pnl_pct_sum += p['position'].current_pnl_percent
                    except Exception as e:
                        logger.warning(f"Error calculating total P&L % for position {p['position'].id}: {e}")
                        continue
            
            # Calculate overall average P&L percentage
            avg_pnl_pct = (total_pnl_pct_sum / total_count) if total_count > 0 else 0.0
            
            # Sort positions by P&L percentage (worst to best, matching looptrader-pro)
            def get_sort_key(x):
                if not x['position'].active:
                    return float('inf')
                try:
                    return x['position'].current_pnl_percent
                except Exception as e:
                    logger.warning(f"Error getting P&L % for sorting position {x['position'].id}: {e}")
                    return 0.0
            position_data.sort(key=get_sort_key)
            
            # Pass the active_only flag to template for button styling
            # Ensure active_only is a boolean
            active_only_bool = (active_only == 'true') if active_only else False
            
            return render_template('positions/list.html', 
                                 positions=valid_positions,
                                 position_data=position_data,  # Enhanced data
                                 accounts=accounts, 
                                 bots=bots,
                                 account_summaries=account_summaries,
                                 total_pnl=total_pnl,
                                 total_count=total_count,
                                 total_winning=total_winning,
                                 total_losing=total_losing,
                                 avg_pnl_pct=avg_pnl_pct,
                                 active_only=active_only_bool)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error in positions route: {str(e)}", exc_info=True)
        flash(f'Error loading positions: {str(e)}', 'danger')
        return render_template('positions/list.html', 
                             positions=[], 
                             position_data=[],
                             accounts=[], 
                             bots=[],
                             account_summaries={},
                             total_pnl=0.0,
                             total_count=0,
                             total_winning=0,
                             total_losing=0,
                             avg_pnl_pct=0.0,
                             active_only=False)

@app.route('/risk')
@login_required
def risk():
    """Display portfolio-level risk metrics matching looptrader-pro's /risk command"""
    try:
        db = SessionLocal()
        try:
            from sqlalchemy.orm import joinedload
            
            # Query positions matching looptrader-pro's /risk command approach
            # Get all bots first, then get active position for each bot
            # This ensures we only process positions that have valid opening orders
            bots = db.query(Bot).all()
            accounts = db.query(BrokerageAccount).all()
            
            # Collect valid active positions by querying through bots
            # This matches looptrader-pro's approach: bot_repo.get_active_position_by_bot(bot.id)
            active_positions = []
            for bot in bots:
                try:
                    # Get active position for this bot
                    db_position = db.query(Position).options(
                        joinedload(Position.orders).joinedload(Order.orderLegCollection).joinedload(OrderLeg.instrument),
                        joinedload(Position.bot)
                    ).filter(
                        Position.bot_id == bot.id,
                        Position.active == True
                    ).first()
                    
                    if db_position is None or not db_position.active:
                        continue
                    
                    # Validate position has opening order with orderLegCollection (matches looptrader-pro)
                    opening_order = next(
                        (order for order in db_position.orders if hasattr(order, 'isOpenPosition') and order.isOpenPosition),
                        None
                    )
                    
                    if opening_order is None or not opening_order.orderLegCollection:
                        logger.warning(f"Bot {bot.id} ({bot.name}) has position {db_position.id} but no valid opening order, skipping")
                        continue
                    
                    # Position is valid, add to list
                    active_positions.append(db_position)
                    
                except Exception as e:
                    logger.error(f"Error processing bot {bot.id} ({bot.name}): {e}", exc_info=True)
                    continue
            
            position_count = len(active_positions)
            logger.info(f"Found {position_count} valid active positions (queried through {len(bots)} bots)")
            
            # Build Schwab cache to avoid multiple API calls
            if position_count > 0:
                from models.database import build_schwab_cache_for_positions
                schwab_cache = build_schwab_cache_for_positions(active_positions)
                logger.debug(f"Schwab cache built with {len(schwab_cache)} position entries")
                
                # Inject cache into each position
                for position in active_positions:
                    position._schwab_cache = schwab_cache
            else:
                schwab_cache = {}
            
            # Initialize Schwab client once for all positions
            schwab_client = None
            try:
                import os
                import schwab
                from schwab.auth import client_from_token_file
                
                token_path = os.path.join('/app', 'token.json')
                if not os.path.exists(token_path):
                    app_root = os.path.dirname(os.path.dirname(__file__))
                    token_path = os.path.join(app_root, 'token.json')
                
                if os.path.exists(token_path):
                    api_key = os.getenv('SCHWAB_API_KEY')
                    app_secret = os.getenv('SCHWAB_APP_SECRET')
                    
                    if api_key and app_secret:
                        schwab_client = client_from_token_file(token_path, api_key, app_secret)
                        logger.debug("Schwab client initialized for live Greeks")
                    else:
                        logger.warning("Missing SCHWAB credentials")
                else:
                    logger.warning(f"No token.json found at {token_path}")
            except Exception as e:
                logger.error(f"Failed to initialize Schwab client: {e}", exc_info=True)
            
            # Initialize aggregates matching looptrader-pro's /risk command
            total_delta = 0.0
            total_gamma = 0.0
            total_theta = 0.0
            total_vega = 0.0
            total_notional_risk = 0.0  # Max risk from spread widths (matches looptrader-pro)
            total_premium_open = 0.0  # Current market value (cost to close)
            total_pnl = 0.0
            total_cost_basis = 0.0  # Initial investment (for percentage)
            
            best_position = None
            worst_position = None
            best_pnl_pct = float('-inf')
            worst_pnl_pct = float('inf')
            
            # Underlying concentration: track count per underlying (matches looptrader-pro)
            underlying_concentration = {}
            
            # Batch fetch Greeks for all positions in a single API call
            from models.database import get_greeks_for_all_positions
            greeks_cache = get_greeks_for_all_positions(active_positions, schwab_client)
            logger.info(f"Fetched Greeks for {len(greeks_cache)} positions in batched API call")
            
            for pos in active_positions:
                try:
                    # Get opening order (already validated above)
                    opening_order = next(
                        (o for o in pos.orders if hasattr(o, 'isOpenPosition') and o.isOpenPosition),
                        None
                    )
                    
                    if not opening_order or not opening_order.orderLegCollection:
                        logger.warning(f"Position {pos.id} missing opening order, skipping")
                        continue
                    
                    # Initial premium (signed: positive for credit, negative for debit)
                    initial_premium = pos.initial_premium_sold
                    # Current market value (cost to close position)
                    current_open_premium = pos.current_open_premium
                    # Cost basis for percentage calculation (always positive)
                    cost_basis = abs(initial_premium)
                    
                    total_premium_open += current_open_premium
                    total_cost_basis += cost_basis
                    
                    # Calculate notional risk (spread width * quantity * 100) matching looptrader-pro
                    # Extract strikes from order legs to calculate spread width
                    # Note: Instrument model doesn't have strikePrice, so we parse from symbol
                    strikes = []
                    for leg in opening_order.orderLegCollection:
                        if leg.instrument and leg.instrument.symbol:
                            # Parse strike from symbol: SPX_12345678C00500000 -> 5000.0
                            # Format: SYMBOL_YYYYMMDDCPPPPPPPP where PPPPPPPP is strike * 1000
                            symbol = leg.instrument.symbol
                            try:
                                if len(symbol) >= 7:
                                    strike_part = symbol[-7:]  # Last 7 characters
                                    strike_value = float(strike_part) / 1000
                                    strikes.append(strike_value)
                            except (ValueError, IndexError):
                                # If parsing fails, try to get from description or skip
                                logger.warning(f"Could not parse strike from symbol {symbol}")
                                continue
                    
                    position_notional_risk = 0.0
                    if len(strikes) >= 2:
                        # For spreads, max risk is the width of the spread
                        spread_width = abs(max(strikes) - min(strikes))
                        quantity = opening_order.quantity if opening_order.quantity else opening_order.filledQuantity or 1
                        position_notional_risk = spread_width * quantity * 100
                    elif len(strikes) == 1:
                        # For single legs (naked options), use strike as notional
                        quantity = opening_order.quantity if opening_order.quantity else opening_order.filledQuantity or 1
                        position_notional_risk = strikes[0] * quantity * 100
                    
                    total_notional_risk += position_notional_risk
                    
                    # Greeks from batched API call (already fetched above)
                    greeks = greeks_cache.get(pos.id, {'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0})
                    logger.debug(f"Position {pos.id}: Greeks = Δ{greeks['delta']:.2f}, Γ{greeks['gamma']:.3f}, Θ{greeks['theta']:.2f}, V{greeks['vega']:.2f}, Notional=${position_notional_risk:.2f}")
                    
                    total_delta += greeks['delta']
                    total_gamma += greeks['gamma']
                    total_theta += greeks['theta']
                    total_vega += greeks['vega']
                    
                    # P&L
                    pnl = pos.current_pnl
                    pnl_pct = pos.current_pnl_percent
                    total_pnl += pnl
                    
                    # Track best/worst
                    if pnl_pct > best_pnl_pct:
                        best_pnl_pct = pnl_pct
                        best_position = (pos.bot.name if pos.bot else f"Position {pos.id}", pnl, pnl_pct)
                    
                    if pnl_pct < worst_pnl_pct:
                        worst_pnl_pct = pnl_pct
                        worst_position = (pos.bot.name if pos.bot else f"Position {pos.id}", pnl, pnl_pct)
                    
                    # Underlying concentration (matches looptrader-pro: just count positions)
                    # Determine underlying symbol from first leg
                    underlying_symbol = "UNKNOWN"
                    if opening_order.orderLegCollection:
                        first_leg = opening_order.orderLegCollection[0]
                        if first_leg.instrument:
                            # Extract underlying from symbol (remove option suffixes)
                            symbol = first_leg.instrument.symbol if hasattr(first_leg.instrument, 'symbol') else ""
                            if symbol:
                                # Symbol format: SPX_12345678C00500000 -> SPX
                                underlying_symbol = symbol.split("_")[0] if "_" in symbol else symbol
                    
                    if underlying_symbol not in underlying_concentration:
                        underlying_concentration[underlying_symbol] = 0
                    underlying_concentration[underlying_symbol] += 1
                    
                except Exception as e:
                    logger.error(f"Error calculating metrics for position {pos.id}: {e}", exc_info=True)
                    continue  # Skip this position but continue with others
            
            logger.debug(f"Total premium_open = ${total_premium_open:.2f}, Cost_basis = ${total_cost_basis:.2f}, Total Greeks: Δ{total_delta:.2f}, Γ{total_gamma:.3f}, Θ{total_theta:.2f}, V{total_vega:.2f}")
            
            # Group by account (for aggregate mode)
            account_metrics = {}
            for account in accounts:
                account_positions = [p for p in active_positions if p.account_id == account.account_id]
                account_premium_open = 0.0
                account_cost_basis = 0.0
                account_notional_risk = 0.0
                account_delta = 0.0
                account_gamma = 0.0
                account_theta = 0.0
                account_vega = 0.0
                account_pnl = 0.0
                account_underlyings = {}  # Track underlying concentration per account
                
                for p in account_positions:
                    try:
                        # Use the same pattern as portfolio totals
                        initial_prem = p.initial_premium_sold
                        current_open = p.current_open_premium
                        account_premium_open += current_open
                        account_cost_basis += abs(initial_prem)
                        
                        # Calculate notional risk for this position
                        opening_order = next(
                            (o for o in p.orders if hasattr(o, 'isOpenPosition') and o.isOpenPosition),
                            None
                        )
                        if opening_order and opening_order.orderLegCollection:
                            strikes = []
                            for leg in opening_order.orderLegCollection:
                                if leg.instrument and leg.instrument.symbol:
                                    # Parse strike from symbol (same as portfolio totals)
                                    symbol = leg.instrument.symbol
                                    try:
                                        if len(symbol) >= 7:
                                            strike_part = symbol[-7:]
                                            strike_value = float(strike_part) / 1000
                                            strikes.append(strike_value)
                                    except (ValueError, IndexError):
                                        continue
                            
                            if len(strikes) >= 2:
                                spread_width = abs(max(strikes) - min(strikes))
                                quantity = opening_order.quantity if opening_order.quantity else opening_order.filledQuantity or 1
                                account_notional_risk += spread_width * quantity * 100
                            elif len(strikes) == 1:
                                quantity = opening_order.quantity if opening_order.quantity else opening_order.filledQuantity or 1
                                account_notional_risk += strikes[0] * quantity * 100
                            
                            # Track underlying concentration for this account
                            if opening_order.orderLegCollection:
                                first_leg = opening_order.orderLegCollection[0]
                                if first_leg.instrument:
                                    symbol = first_leg.instrument.symbol if hasattr(first_leg.instrument, 'symbol') else ""
                                    if symbol:
                                        underlying_symbol = symbol.split("_")[0] if "_" in symbol else symbol
                                        if underlying_symbol not in account_underlyings:
                                            account_underlyings[underlying_symbol] = 0
                                        account_underlyings[underlying_symbol] += 1
                        
                        # Use cached Greeks to avoid duplicate broker calls
                        greeks = greeks_cache.get(p.id, {'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0})
                        account_delta += greeks['delta']
                        account_gamma += greeks['gamma']
                        account_theta += greeks['theta']
                        account_vega += greeks['vega']
                        
                        account_pnl += p.current_pnl
                    except Exception as e:
                        logger.error(f"Error calculating account metrics for position {p.id}: {e}", exc_info=True)
                
                logger.debug(f"Account {account.name}: {len(account_positions)} positions, open=${account_premium_open:.2f}, cost_basis=${account_cost_basis:.2f}, notional=${account_notional_risk:.2f}, Δ{account_delta:.2f}")
                
                account_metrics[account.account_id] = {
                    'name': account.name,
                    'position_count': len(account_positions),
                    'premium_open': account_premium_open,
                    'cost_basis': account_cost_basis,
                    'delta': account_delta,
                    'gamma': account_gamma,
                    'theta': account_theta,
                    'vega': account_vega,
                    'notional_risk': account_notional_risk,  # Actual notional risk (spread width)
                    'pnl': account_pnl,
                    'underlying_concentration': account_underlyings
                }
            
            # Calculate total P&L percentage using cost basis (following looptrader-pro pattern)
            total_pnl_pct = (total_pnl / total_cost_basis) * 100 if total_cost_basis > 0.01 else 0.0
            
            # Format underlying concentration for template (matches looptrader-pro: just count)
            # Template expects list of tuples: [(symbol, count), ...]
            underlying_list = sorted(underlying_concentration.items(), key=lambda x: x[1], reverse=True)
            
            warnings = []
            if position_count > 0 and total_cost_basis == 0:
                warnings.append('Warning: Positions found but cost basis is $0 - check order data')
            if schwab_client is None:
                warnings.append('Warning: Greeks fetched from broker API may be unavailable - check Schwab credentials')
            
            # Read aggregate parameter from request (for per-account breakdown toggle)
            aggregate = request.args.get('aggregate') == 'true'
            
            return render_template(
                'risk/risk.html',
                aggregate=aggregate,
                position_count=position_count,
                total_delta=total_delta,
                total_gamma=total_gamma,
                total_theta=total_theta,
                total_vega=total_vega,
                total_notional_risk=total_notional_risk,  # Use actual notional risk (spread width)
                total_premium=total_premium_open,
                total_cost_basis=total_cost_basis,
                total_pnl=total_pnl,
                total_pnl_pct=total_pnl_pct,
                best_position=best_position,
                worst_position=worst_position,
                underlying_concentration=underlying_list,
                warnings=warnings,
                account_metrics=account_metrics
            )
        finally:
            db.close()
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Error in risk route: {str(e)}", exc_info=True)
        flash(f'Error loading risk data: {str(e)}', 'danger')
        
        # Return template with error information instead of silently showing "no positions"
        return render_template(
            'risk/risk.html',
            aggregate=False,
            position_count=0,
            total_delta=0.0,
            total_gamma=0.0,
            total_theta=0.0,
            total_vega=0.0,
            total_notional_risk=0.0,
            total_premium=0.0,
            total_cost_basis=0.0,
            total_pnl=0.0,
            total_pnl_pct=0.0,
            best_position=None,
            worst_position=None,
            underlying_concentration=[],
            warnings=[f'Error loading risk data: {str(e)}'],  # Show error in warnings
            account_metrics={}
        )


###############################################################################
# ANALYTICS HELPER FUNCTIONS
###############################################################################

def is_market_closed(check_date):
    """Check if market is closed (weekend or US stock market holiday)"""
    from datetime import date
    
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

def get_next_trading_day(start_date):
    """Get the next trading day after start_date"""
    from datetime import timedelta
    next_day = start_date + timedelta(days=1)
    while is_market_closed(next_day):
        next_day += timedelta(days=1)
    return next_day

###############################################################################
# ANALYTICS ROUTES
###############################################################################

@app.route('/analytics')
@login_required
def analytics():
    """Main analytics page with Greek exposure analysis"""
    return render_template('analytics/analytics.html')


@app.route('/analytics/gex', methods=['POST'])
@login_required
def analytics_gex():
    """Calculate Gamma Exposure (GEX) for a given ticker"""
    print("=" * 50)
    print("ANALYTICS GEX ENDPOINT CALLED")
    print("=" * 50)
    try:
        from datetime import date
        
        data = request.get_json()
        print(f"Received data: {data}")
        ticker = data.get('ticker', 'SPX').upper()
        detail = data.get('detail', False)
        show_all = data.get('show_all', False)
        strike_range = data.get('strike_range')
        print(f"Ticker: {ticker}, Detail: {detail}, Show All: {show_all}, Strike Range: {strike_range}")
        
        # Check if market is closed and determine target date
        today = date.today()
        if is_market_closed(today):
            target_date = get_next_trading_day(today)
            print(f"Market is CLOSED today ({today.strftime('%A, %Y-%m-%d')})")
            print(f"Using next trading day: {target_date.strftime('%A, %Y-%m-%d')}")
        else:
            target_date = today
            print(f"Market is OPEN today ({today.strftime('%A, %Y-%m-%d')})")
        
        # Initialize Schwab client
        import schwab
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            token_path = os.path.join(app_root, 'token.json')
        
        schwab_client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Fetch option chains with target date
        symbols_to_try = [f'${ticker}.X', ticker, f'${ticker}']
        chain_response = None
        
        for symbol in symbols_to_try:
            print(f"Trying symbol: {symbol} for date {target_date}")
            try:
                chain_response = schwab_client.get_option_chain(
                    symbol=symbol,
                    from_date=target_date,
                    to_date=target_date
                )
                if chain_response.status_code == 200:
                    print(f"Success with symbol: {symbol}")
                    break
            except Exception as e:
                print(f"Failed with symbol {symbol}: {e}")
                continue
        
        if not chain_response or chain_response.status_code != 200:
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        chain_data = chain_response.json()
        
        if chain_data.get('status') == 'FAILED':
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        # Get underlying price
        spot_price = chain_data.get('underlyingPrice', 0)
        
        # Calculate strike range
        if strike_range:
            strike_min = spot_price - float(strike_range)
            strike_max = spot_price + float(strike_range)
        elif detail:
            # Detail mode: ±15%
            strike_min = spot_price * 0.85
            strike_max = spot_price * 1.15
        else:
            # Default: ±50 points
            strike_min = spot_price - 50
            strike_max = spot_price + 50
        
        # Process option chain and calculate GEX
        gex_data = {}
        expiration_map = chain_data.get('callExpDateMap', {})
        
        # Get first expiration if not show_all
        if not show_all and expiration_map:
            first_exp = sorted(expiration_map.keys())[0]
            expiration_map = {first_exp: expiration_map[first_exp]}
        
        for exp_date, strikes in expiration_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    gamma = option.get('gamma', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if gamma and volume:
                        # GEX formula: gamma × volume × 100 × spot²
                        gex = gamma * volume * 100 * (spot_price ** 2)
                        
                        if strike not in gex_data:
                            gex_data[strike] = {'call': 0, 'put': 0}
                        gex_data[strike]['call'] += gex
        
        # Process puts
        put_exp_map = chain_data.get('putExpDateMap', {})
        if not show_all and put_exp_map:
            first_exp = sorted(put_exp_map.keys())[0]
            put_exp_map = {first_exp: put_exp_map[first_exp]}
        
        for exp_date, strikes in put_exp_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    gamma = option.get('gamma', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if gamma and volume:
                        # GEX formula: gamma × volume × 100 × spot² (negative for puts)
                        gex = -1 * gamma * volume * 100 * (spot_price ** 2)
                        
                        if strike not in gex_data:
                            gex_data[strike] = {'call': 0, 'put': 0}
                        gex_data[strike]['put'] += gex
        
        # Sort and limit to top 50 strikes
        sorted_strikes = sorted(gex_data.items(), key=lambda x: abs(x[1]['call'] + x[1]['put']), reverse=True)[:50]
        
        # Build chart data and identify key strikes
        chart_data = []
        total_gex = 0
        strikes_above_spot = []  # Strikes above current price
        strikes_below_spot = []  # Strikes below current price
        
        for strike, exposure in sorted_strikes:
            net_gex = exposure['call'] + exposure['put']
            total_gex += net_gex
            chart_data.append({
                'strike': strike,
                'exposure': net_gex
            })
            
            # Separate strikes above and below spot for proper identification
            if strike > spot_price:
                strikes_above_spot.append({'strike': strike, 'exposure': net_gex})
            elif strike < spot_price:
                strikes_below_spot.append({'strike': strike, 'exposure': net_gex})
        
        # Find resistance: Strike ABOVE spot with highest absolute GEX (typically positive)
        max_resistance_strike = {'strike': 0, 'exposure': 0}
        if strikes_above_spot:
            max_resistance_strike = max(strikes_above_spot, key=lambda x: abs(x['exposure']))
        
        # Find support: Strike BELOW spot with highest absolute GEX (typically negative)
        max_support_strike = {'strike': 0, 'exposure': 0}
        if strikes_below_spot:
            max_support_strike = max(strikes_below_spot, key=lambda x: abs(x['exposure']))
        
        # Sort chart data by strike
        chart_data.sort(key=lambda x: x['strike'])
        
        # Find zero GEX (flip point)
        zero_gex_strike = None
        for i in range(len(chart_data) - 1):
            if chart_data[i]['exposure'] < 0 and chart_data[i+1]['exposure'] > 0:
                zero_gex_strike = (chart_data[i]['strike'] + chart_data[i+1]['strike']) / 2
                break
        
        # Key levels (top 3 resistance and support)
        positive_levels = [{'strike': d['strike'], 'exposure': d['exposure']} 
                          for d in chart_data if d['exposure'] > 0]
        negative_levels = [{'strike': d['strike'], 'exposure': d['exposure']} 
                          for d in chart_data if d['exposure'] < 0]
        
        positive_levels.sort(key=lambda x: x['exposure'], reverse=True)
        negative_levels.sort(key=lambda x: abs(x['exposure']), reverse=True)
        
        key_levels = positive_levels[:3] + negative_levels[:3]
        
        # Enhanced Market interpretation
        interpretation = []
        
        # 1. Basic GEX Levels
        if zero_gex_strike:
            interpretation.append(f"🎯 Zero GEX flip point at ${zero_gex_strike:.0f}")
        
        if max_resistance_strike['strike'] > 0:
            gex_sign = "positive" if max_resistance_strike['exposure'] > 0 else "negative"
            interpretation.append(f"📈 Key resistance level at ${max_resistance_strike['strike']:.0f} (${abs(max_resistance_strike['exposure'])/1e9:.2f}B {gex_sign} GEX)")
        
        if max_support_strike['strike'] > 0:
            gex_sign = "positive" if max_support_strike['exposure'] > 0 else "negative"
            interpretation.append(f"📉 Key support level at ${max_support_strike['strike']:.0f} (${abs(max_support_strike['exposure'])/1e9:.2f}B {gex_sign} GEX)")
        # 2. Market Regime Context
        if zero_gex_strike:
            if spot_price > zero_gex_strike:
                interpretation.append(f"✅ POSITIVE GAMMA REGIME: Spot (${spot_price:.0f}) is above flip point (${zero_gex_strike:.0f})")
                interpretation.append("Market makers are long gamma — their hedging dampens volatility (sell rallies, buy dips)")
                interpretation.append(f"⚠️ If spot breaks below ${zero_gex_strike:.0f}, market transitions to negative gamma with expanding volatility")
            else:
                interpretation.append(f"⚠️ NEGATIVE GAMMA REGIME: Spot (${spot_price:.0f}) is below flip point (${zero_gex_strike:.0f})")
                interpretation.append("Market makers are short gamma — their hedging amplifies volatility (sell dips, buy rallies)")
                interpretation.append(f"📈 If spot breaks above ${zero_gex_strike:.0f}, volatility may compress in positive gamma zone")
        
        # 3. Expected Volatility and Range
        if max_resistance_strike['strike'] > 0 and max_support_strike['strike'] > 0:
            range_width = abs(max_resistance_strike['strike'] - max_support_strike['strike'])
            upper_bound = max(max_resistance_strike['strike'], max_support_strike['strike'])
            lower_bound = min(max_resistance_strike['strike'], max_support_strike['strike'])
            
            interpretation.append(f"📊 Expected intraday range: ${lower_bound:.0f}–${upper_bound:.0f} ({range_width:.0f} pts)")
            
            if range_width < 100:
                interpretation.append(f"🔒 Narrow range ({range_width:.0f} pts) implies compressed volatility and mean-reversion bias")
            else:
                interpretation.append(f"📏 Wide range ({range_width:.0f} pts) allows for directional movement")
        
        # 4. Liquidity and Pinning Zones
        if max_resistance_strike['strike'] > 0 and max_support_strike['strike'] > 0:
            if abs(spot_price - max_resistance_strike['strike']) < 30:
                interpretation.append(f"📍 Price near resistance (${max_resistance_strike['strike']:.0f}) — watch for pinning effects and upside caps")
            elif abs(spot_price - max_support_strike['strike']) < 30:
                interpretation.append(f"📍 Price near support (${max_support_strike['strike']:.0f}) — watch for pinning effects and downside protection")
            else:
                interpretation.append(f"🎯 Price between support/resistance — high dealer gamma exposure creates liquidity magnets at extremes")
        
        # 5. Trading Implications
        interpretation.append("💡 TRADING IMPLICATIONS:")
        
        if zero_gex_strike and spot_price > zero_gex_strike:
            # Positive gamma regime
            if max_resistance_strike['strike'] > 0 and abs(spot_price - max_resistance_strike['strike']) < 50:
                interpretation.append(f"• Directional Bias: Neutral-to-slightly bearish (capped by resistance at ${max_resistance_strike['strike']:.0f})")
            else:
                interpretation.append("• Directional Bias: Neutral (positive gamma supports mean reversion)")
            
            interpretation.append("• Volatility Bias: Expect low realized volatility unless walls are breached")
            interpretation.append("• Strategy: Favor mean-reversion trades (iron condors, credit spreads)")
            interpretation.append(f"• Risk Management: Tight stops if spot breaks below ${zero_gex_strike:.0f} flip point")
        else:
            # Negative gamma regime or no flip point
            interpretation.append("• Directional Bias: Higher directional risk in negative gamma")
            interpretation.append("• Volatility Bias: Expect elevated realized volatility")
            interpretation.append("• Strategy: Consider long gamma trades (buying options, straddles)")
            interpretation.append("• Risk Management: Wider stops to accommodate volatility expansion")
        
        # 6. Summary
        if max_resistance_strike['strike'] > 0 and max_support_strike['strike'] > 0 and zero_gex_strike:
            summary = f"📋 SUMMARY: "
            if spot_price > zero_gex_strike:
                summary += f"Positive gamma regime with controlled volatility. "
            else:
                summary += f"Negative gamma regime with elevated volatility. "
            
            summary += f"Price expected to gravitate between ${min(max_resistance_strike['strike'], max_support_strike['strike']):.0f}–${max(max_resistance_strike['strike'], max_support_strike['strike']):.0f}. "
            
            if total_gex > 0:
                summary += f"Net positive GEX (${total_gex/1e9:.2f}B) suggests downside support."
            elif total_gex < 0:
                summary += f"Net negative GEX (${total_gex/1e9:.2f}B) suggests upside resistance."
            
            interpretation.append(summary)
        
        # Get expiration info
        exp_date_str = sorted(chain_data.get('callExpDateMap', {}).keys())[0] if chain_data.get('callExpDateMap') else 'N/A'
        dte = chain_data.get('daysToExpiration', 0)
        
        return jsonify({
            'ticker': ticker,
            'spot_price': spot_price,
            'expiration': exp_date_str.split(':')[0] if ':' in exp_date_str else exp_date_str,
            'dte': dte,
            'total_exposure': total_gex,
            'chart_data': chart_data,
            'key_levels': key_levels,
            'interpretation': interpretation
        })
        
    except Exception as e:
        import traceback
        print(f"Error in GEX calculation: {str(e)}")
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/analytics/vex', methods=['POST'])
@login_required
def analytics_vex():
    """Calculate Vega Exposure (VEX) for a given ticker"""
    try:
        from datetime import date
        
        data = request.get_json()
        ticker = data.get('ticker', 'SPX').upper()
        detail = data.get('detail', False)
        show_all = data.get('show_all', False)
        strike_range = data.get('strike_range')
        
        # Check if market is closed and determine target date
        today = date.today()
        if is_market_closed(today):
            target_date = get_next_trading_day(today)
            print(f"VEX: Market CLOSED, using {target_date.strftime('%A, %Y-%m-%d')}")
        else:
            target_date = today
            print(f"VEX: Market OPEN, using {today.strftime('%A, %Y-%m-%d')}")
        
        # Initialize Schwab client
        import schwab
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            token_path = os.path.join(app_root, 'token.json')
        
        schwab_client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Fetch option chains with target date
        symbols_to_try = [f'${ticker}.X', ticker, f'${ticker}']
        chain_response = None
        
        for symbol in symbols_to_try:
            try:
                chain_response = schwab_client.get_option_chain(
                    symbol=symbol,
                    from_date=target_date,
                    to_date=target_date
                )
                if chain_response.status_code == 200:
                    break
            except:
                continue
        
        if not chain_response or chain_response.status_code != 200:
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        chain_data = chain_response.json()
        
        if chain_data.get('status') == 'FAILED':
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        spot_price = chain_data.get('underlyingPrice', 0)
        
        # Calculate strike range
        if strike_range:
            strike_min = spot_price - float(strike_range)
            strike_max = spot_price + float(strike_range)
        elif detail:
            strike_min = spot_price * 0.85
            strike_max = spot_price * 1.15
        else:
            strike_min = spot_price - 50
            strike_max = spot_price + 50
        
        # Process VEX
        vex_data = {}
        expiration_map = chain_data.get('callExpDateMap', {})
        
        if not show_all and expiration_map:
            first_exp = sorted(expiration_map.keys())[0]
            expiration_map = {first_exp: expiration_map[first_exp]}
        
        for exp_date, strikes in expiration_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    vega = option.get('vega', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if vega and volume:
                        # VEX formula: vega × volume × 100
                        vex = vega * volume * 100
                        
                        if strike not in vex_data:
                            vex_data[strike] = 0
                        vex_data[strike] += vex
        
        # Process puts
        put_exp_map = chain_data.get('putExpDateMap', {})
        if not show_all and put_exp_map:
            first_exp = sorted(put_exp_map.keys())[0]
            put_exp_map = {first_exp: put_exp_map[first_exp]}
        
        for exp_date, strikes in put_exp_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    vega = option.get('vega', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if vega and volume:
                        vex = vega * volume * 100
                        
                        if strike not in vex_data:
                            vex_data[strike] = 0
                        vex_data[strike] += vex
        
        # Sort and limit
        sorted_strikes = sorted(vex_data.items(), key=lambda x: abs(x[1]), reverse=True)[:50]
        
        chart_data = []
        total_vex = 0
        
        for strike, exposure in sorted_strikes:
            total_vex += exposure
            chart_data.append({
                'strike': strike,
                'exposure': exposure
            })
        
        chart_data.sort(key=lambda x: x['strike'])
        
        # Key levels
        key_levels = sorted(sorted_strikes, key=lambda x: abs(x[1]), reverse=True)[:5]
        key_levels = [{'strike': k[0], 'exposure': k[1]} for k in key_levels]
        
        # Enhanced VEX Interpretation
        interpretation = []
        
        # Find highest VEX strike
        highest_vex_strike = key_levels[0]['strike'] if key_levels else None
        highest_vex_value = key_levels[0]['exposure'] if key_levels else 0
        
        # Calculate VEX concentration metrics
        if highest_vex_strike:
            interpretation.append(f"🎯 Highest VEX concentration at ${highest_vex_strike:.0f} (${highest_vex_value/1e6:.2f}M)")
            interpretation.append(f"💰 Total VEX: ${total_vex/1e6:.2f}M - shows volatility exposure distribution")
        
        # Determine vega regime and pinning effects
        if highest_vex_strike:
            distance_from_spot = abs(spot_price - highest_vex_strike)
            
            if distance_from_spot < 30:
                interpretation.append(f"📍 PINNING ALERT: Spot (${spot_price:.0f}) is very close to highest VEX level (${highest_vex_strike:.0f})")
                interpretation.append("High vega exposure creates strong pinning force — dealers' volatility hedging flows will resist price movement")
                interpretation.append("Expect spot to gravitate toward this strike, especially approaching expiration")
            elif distance_from_spot < 100:
                interpretation.append(f"⚠️ Spot (${spot_price:.0f}) is within VEX influence zone around ${highest_vex_strike:.0f}")
                interpretation.append("Moderate pinning effects — dealers hedging vega exposure may dampen volatility")
            
        # Analyze VEX distribution pattern
        if len(key_levels) >= 3:
            vex_range = max([k['strike'] for k in key_levels]) - min([k['strike'] for k in key_levels])
            
            interpretation.append(f"📊 VEX DISTRIBUTION:")
            if vex_range < 100:
                interpretation.append(f"• Concentrated VEX ({vex_range:.0f} pts range) — strong pinning force in tight band")
                interpretation.append("• Implied volatility should remain stable and compressed")
            else:
                interpretation.append(f"• Dispersed VEX ({vex_range:.0f} pts range) — weaker pinning, more directional freedom")
        
        # Volatility regime assessment
        interpretation.append("📈 VOLATILITY IMPLICATIONS:")
        
        if highest_vex_strike and abs(spot_price - highest_vex_strike) < 50:
            interpretation.append(f"• High vega-exposure regime near ${highest_vex_strike:.0f}")
            interpretation.append("• Dealers' vega hedging flows are suppressing directional volatility")
            interpretation.append("• Implied vol should remain stable — expect low realized volatility")
            interpretation.append("• IV crush risk near expiration as vega exposure unwinds")
        else:
            interpretation.append("• Moderate vega-exposure regime")
            interpretation.append("• Less pinning pressure — spot has more freedom to move")
            interpretation.append("• IV may expand if price moves away from VEX concentrations")
        
        # Trading strategy recommendations
        interpretation.append("💡 TRADING STRATEGIES:")
        
        if highest_vex_strike and abs(spot_price - highest_vex_strike) < 30:
            # Strong pinning near highest VEX
            interpretation.append(f"• PRIMARY: Sell premium around ${highest_vex_strike:.0f} (iron condors, credit spreads)")
            interpretation.append("• Exploit pinning effects and stable IV")
            interpretation.append(f"• Define risk outside VEX concentration zone (>${highest_vex_strike + 50:.0f} or <${highest_vex_strike - 50:.0f})")
            interpretation.append("• Fade breakout attempts — mean reversion bias is strong")
        else:
            # Away from VEX concentration
            interpretation.append("• Reduced pinning effects — consider directional trades")
            interpretation.append("• Monitor for IV expansion if price continues away from VEX levels")
            interpretation.append("• Long volatility trades may benefit if spot breaks out of range")
        
        # Risk warnings based on VEX positioning
        interpretation.append("⚠️ RISK FACTORS:")
        if total_vex > 50e6:  # >$50M VEX
            interpretation.append(f"• Very high VEX (${total_vex/1e6:.2f}M) — strong dealer hedging flows active")
            interpretation.append("• Price and IV pinning effects are maximum")
            interpretation.append("• Breakouts may be violent if pinning breaks")
        
        # Summary
        if highest_vex_strike:
            summary = f"📋 SUMMARY: "
            if abs(spot_price - highest_vex_strike) < 30:
                summary += f"High vega-exposure regime with spot pinned near ${highest_vex_strike:.0f}. "
                summary += "Dealers' vega hedging flows are suppressing volatility. "
                summary += "Expect narrow ranges and stable IV. "
                summary += "Favor premium selling strategies within the VEX concentration zone."
            else:
                summary += f"Spot (${spot_price:.0f}) is away from main VEX level (${highest_vex_strike:.0f}). "
                summary += "Reduced pinning effects allow for more directional movement. "
                summary += "Monitor for IV changes as price moves relative to VEX concentrations."
            
            interpretation.append(summary)
        
        exp_date_str = sorted(chain_data.get('callExpDateMap', {}).keys())[0] if chain_data.get('callExpDateMap') else 'N/A'
        dte = chain_data.get('daysToExpiration', 0)
        
        return jsonify({
            'ticker': ticker,
            'spot_price': spot_price,
            'expiration': exp_date_str.split(':')[0] if ':' in exp_date_str else exp_date_str,
            'dte': dte,
            'total_exposure': total_vex,
            'chart_data': chart_data,
            'key_levels': key_levels,
            'interpretation': interpretation
        })
        
    except Exception as e:
        import traceback
        print(f"Error in VEX calculation: {str(e)}")
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/analytics/dex', methods=['POST'])
@login_required
def analytics_dex():
    """Calculate Delta Exposure (DEX) for a given ticker"""
    try:
        from datetime import date
        
        data = request.get_json()
        ticker = data.get('ticker', 'SPX').upper()
        detail = data.get('detail', False)
        show_all = data.get('show_all', False)
        strike_range = data.get('strike_range')
        
        # Check if market is closed
        today = date.today()
        target_date = get_next_trading_day(today) if is_market_closed(today) else today
        
        # Initialize Schwab client
        import schwab
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            token_path = os.path.join(app_root, 'token.json')
        
        schwab_client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Fetch option chains with target date
        symbols_to_try = [f'${ticker}.X', ticker, f'${ticker}']
        chain_response = None
        
        for symbol in symbols_to_try:
            try:
                chain_response = schwab_client.get_option_chain(
                    symbol=symbol,
                    from_date=target_date,
                    to_date=target_date
                )
                if chain_response.status_code == 200:
                    break
            except:
                continue
        
        if not chain_response or chain_response.status_code != 200:
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        chain_data = chain_response.json()
        
        if chain_data.get('status') == 'FAILED':
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        spot_price = chain_data.get('underlyingPrice', 0)
        
        # Calculate strike range
        if strike_range:
            strike_min = spot_price - float(strike_range)
            strike_max = spot_price + float(strike_range)
        elif detail:
            strike_min = spot_price * 0.85
            strike_max = spot_price * 1.15
        else:
            strike_min = spot_price - 50
            strike_max = spot_price + 50
        
        # Process DEX
        dex_data = {}
        expiration_map = chain_data.get('callExpDateMap', {})
        
        if not show_all and expiration_map:
            first_exp = sorted(expiration_map.keys())[0]
            expiration_map = {first_exp: expiration_map[first_exp]}
        
        for exp_date, strikes in expiration_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    delta = option.get('delta', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if delta and volume:
                        # DEX formula: delta × volume × 100 × spot
                        dex = delta * volume * 100 * spot_price
                        
                        if strike not in dex_data:
                            dex_data[strike] = {'call': 0, 'put': 0}
                        dex_data[strike]['call'] += dex
        
        # Process puts
        put_exp_map = chain_data.get('putExpDateMap', {})
        if not show_all and put_exp_map:
            first_exp = sorted(put_exp_map.keys())[0]
            put_exp_map = {first_exp: put_exp_map[first_exp]}
        
        for exp_date, strikes in put_exp_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    delta = option.get('delta', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if delta and volume:
                        # DEX formula (puts are negative delta)
                        dex = delta * volume * 100 * spot_price
                        
                        if strike not in dex_data:
                            dex_data[strike] = {'call': 0, 'put': 0}
                        dex_data[strike]['put'] += dex
        
        # Sort and limit
        sorted_strikes = sorted(dex_data.items(), key=lambda x: abs(x[1]['call'] + x[1]['put']), reverse=True)[:50]
        
        chart_data = []
        total_dex = 0
        
        for strike, exposure in sorted_strikes:
            net_dex = exposure['call'] + exposure['put']
            total_dex += net_dex
            chart_data.append({
                'strike': strike,
                'exposure': net_dex
            })
        
        chart_data.sort(key=lambda x: x['strike'])
        
        # Key levels
        positive_levels = [{'strike': d['strike'], 'exposure': d['exposure']} 
                          for d in chart_data if d['exposure'] > 0]
        negative_levels = [{'strike': d['strike'], 'exposure': d['exposure']} 
                          for d in chart_data if d['exposure'] < 0]
        
        positive_levels.sort(key=lambda x: x['exposure'], reverse=True)
        negative_levels.sort(key=lambda x: abs(x['exposure']), reverse=True)
        
        key_levels = positive_levels[:3] + negative_levels[:3]
        
        # Enhanced DEX Interpretation
        interpretation = []
        
        # 1. Net DEX Positioning (Directional Bias)
        interpretation.append("📊 DELTA EXPOSURE ANALYSIS:")
        
        net_dex_billion = total_dex / 1e9
        if total_dex > 5e9:  # >$5B
            interpretation.append(f"🟢 STRONG BULLISH DELTA: Net +${net_dex_billion:.2f}B")
            interpretation.append("Dealers are short delta (long calls, short puts) — forced to buy on dips")
            bias = "bullish"
        elif total_dex > 0:
            interpretation.append(f"🟢 Bullish Delta: Net +${net_dex_billion:.2f}B")
            interpretation.append("Moderate bullish positioning — dealers have positive delta exposure")
            bias = "neutral-bullish"
        elif total_dex < -5e9:  # <-$5B
            interpretation.append(f"🔴 STRONG BEARISH DELTA: Net ${net_dex_billion:.2f}B")
            interpretation.append("Dealers are long delta (short calls, long puts) — forced to sell on rallies")
            bias = "bearish"
        elif total_dex < 0:
            interpretation.append(f"🔴 Bearish Delta: Net ${net_dex_billion:.2f}B")
            interpretation.append("Moderate bearish positioning — dealers have negative delta exposure")
            bias = "neutral-bearish"
        else:
            interpretation.append("⚖️ BALANCED DELTA: Net ~$0")
            interpretation.append("Neutral dealer positioning — no strong directional bias")
            bias = "neutral"
        
        # 2. Key Delta Concentration Zones
        interpretation.append("🎯 KEY DELTA ZONES:")
        
        if positive_levels:
            top_bull_strike = positive_levels[0]['strike']
            top_bull_dex = positive_levels[0]['exposure']
            interpretation.append(f"• Strongest bullish zone: ${top_bull_strike:.0f} (${top_bull_dex/1e9:.2f}B DEX)")
            
            if abs(spot_price - top_bull_strike) < 30:
                interpretation.append(f"  → Spot is AT this bullish zone — expect dip buying support")
            elif spot_price < top_bull_strike:
                interpretation.append(f"  → Spot is BELOW — this level acts as upside magnet")
        
        if negative_levels:
            top_bear_strike = negative_levels[0]['strike']
            top_bear_dex = negative_levels[0]['exposure']
            interpretation.append(f"• Strongest bearish zone: ${top_bear_strike:.0f} (${abs(top_bear_dex)/1e9:.2f}B DEX)")
            
            if abs(spot_price - top_bear_strike) < 30:
                interpretation.append(f"  → Spot is AT this bearish zone — expect rally selling pressure")
            elif spot_price > top_bear_strike:
                interpretation.append(f"  → Spot is ABOVE — this level acts as downside magnet")
        
        # 3. Dealer Hedging Flow Implications
        interpretation.append("🔄 DEALER HEDGING FLOWS:")
        
        if total_dex > 5e9:
            interpretation.append("• Dealers are SHORT delta → must BUY on dips to stay hedged")
            interpretation.append("• Creates automatic dip-buying pressure (bullish feedback loop)")
            interpretation.append("• Rallies may accelerate as dealers chase rising prices")
        elif total_dex > 0:
            interpretation.append("• Dealers have modest bullish delta → mild dip-buying tendency")
            interpretation.append("• Moderate support on pullbacks")
        elif total_dex < -5e9:
            interpretation.append("• Dealers are LONG delta → must SELL on rallies to stay hedged")
            interpretation.append("• Creates automatic rally-selling pressure (bearish feedback loop)")
            interpretation.append("• Declines may accelerate as dealers chase falling prices")
        elif total_dex < 0:
            interpretation.append("• Dealers have modest bearish delta → mild rally-selling tendency")
            interpretation.append("• Moderate resistance on bounces")
        else:
            interpretation.append("• Balanced dealer positioning → minimal directional hedging flows")
        
        # 4. Combined with GEX/VEX Context
        interpretation.append("🧩 MULTI-GREEK CONTEXT:")
        
        if positive_levels and negative_levels:
            dex_range = abs(positive_levels[0]['strike'] - negative_levels[0]['strike'])
            
            if total_dex > 5e9:
                interpretation.append(f"• Bullish DEX ({net_dex_billion:.2f}B) suggests dealers will support dips")
                interpretation.append(f"• If combined with positive GEX and high VEX near ${positive_levels[0]['strike']:.0f}:")
                interpretation.append("  → Creates TRIPLE ANCHOR (gamma + vega + delta pinning)")
                interpretation.append("  → Expect strong mean-reversion and dip-buying absorption")
            elif total_dex < -5e9:
                interpretation.append(f"• Bearish DEX ({net_dex_billion:.2f}B) suggests dealers will sell rallies")
                interpretation.append(f"• If combined with negative GEX zone:")
                interpretation.append("  → Amplified downside risk on breaks")
        
        # 5. Trading Implications
        interpretation.append("💡 TRADING IMPLICATIONS:")
        
        if bias == "bullish":
            interpretation.append("• Directional Bias: BULLISH — favor long delta strategies")
            interpretation.append("• Dealers' forced buying on dips creates support")
            interpretation.append("• Strategy: Buy dips, long call spreads, bull put spreads")
            interpretation.append("• Risk: Bearish reversal if DEX flips negative")
        elif bias == "neutral-bullish":
            interpretation.append("• Directional Bias: Neutral-to-Bullish")
            interpretation.append("• Strategy: Sell put premium, bullish risk reversals")
            interpretation.append("• Watch for DEX to strengthen or weaken")
        elif bias == "bearish":
            interpretation.append("• Directional Bias: BEARISH — favor short delta strategies")
            interpretation.append("• Dealers' forced selling on rallies creates resistance")
            interpretation.append("• Strategy: Sell rallies, put spreads, bear call spreads")
            interpretation.append("• Risk: Bullish reversal if DEX flips positive")
        elif bias == "neutral-bearish":
            interpretation.append("• Directional Bias: Neutral-to-Bearish")
            interpretation.append("• Strategy: Sell call premium, bearish risk reversals")
            interpretation.append("• Watch for DEX to strengthen or weaken")
        else:
            interpretation.append("• Directional Bias: NEUTRAL — no strong delta edge")
            interpretation.append("• Strategy: Non-directional (iron condors, straddles)")
        
        # 6. Summary
        summary = f"📋 SUMMARY: "
        
        if total_dex > 5e9:
            summary += f"Strong bullish delta regime (${net_dex_billion:.2f}B). "
            summary += "Dealers' hedging flows will support dips and amplify rallies. "
            if positive_levels:
                summary += f"Key support zone at ${positive_levels[0]['strike']:.0f}. "
            summary += "Favor bullish strategies and buy dips."
        elif total_dex > 0:
            summary += f"Bullish delta bias (${net_dex_billion:.2f}B). "
            summary += "Moderate dip-buying support from dealer hedging. "
        elif total_dex < -5e9:
            summary += f"Strong bearish delta regime (${net_dex_billion:.2f}B). "
            summary += "Dealers' hedging flows will resist rallies and amplify declines. "
            if negative_levels:
                summary += f"Key resistance zone at ${negative_levels[0]['strike']:.0f}. "
            summary += "Favor bearish strategies and sell rallies."
        elif total_dex < 0:
            summary += f"Bearish delta bias (${net_dex_billion:.2f}B). "
            summary += "Moderate rally-selling pressure from dealer hedging. "
        else:
            summary += "Balanced delta exposure — no strong directional bias from dealer positioning."
        
        interpretation.append(summary)
        
        exp_date_str = sorted(chain_data.get('callExpDateMap', {}).keys())[0] if chain_data.get('callExpDateMap') else 'N/A'
        dte = chain_data.get('daysToExpiration', 0)
        
        return jsonify({
            'ticker': ticker,
            'spot_price': spot_price,
            'expiration': exp_date_str.split(':')[0] if ':' in exp_date_str else exp_date_str,
            'dte': dte,
            'total_exposure': total_dex,
            'chart_data': chart_data,
            'key_levels': key_levels,
            'interpretation': interpretation
        })
        
    except Exception as e:
        import traceback
        print(f"Error in DEX calculation: {str(e)}")
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/analytics/chex', methods=['POST'])
@login_required
def analytics_chex():
    """Calculate Charm Exposure (CHEX) for a given ticker"""
    try:
        from datetime import date
        
        data = request.get_json()
        ticker = data.get('ticker', 'SPX').upper()
        detail = data.get('detail', False)
        show_all = data.get('show_all', False)
        strike_range = data.get('strike_range')
        
        # Check if market is closed
        today = date.today()
        target_date = get_next_trading_day(today) if is_market_closed(today) else today
        
        # Initialize Schwab client
        import schwab
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            token_path = os.path.join(app_root, 'token.json')
        
        schwab_client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Fetch option chains with target date
        symbols_to_try = [f'${ticker}.X', ticker, f'${ticker}']
        chain_response = None
        
        for symbol in symbols_to_try:
            try:
                chain_response = schwab_client.get_option_chain(
                    symbol=symbol,
                    from_date=target_date,
                    to_date=target_date
                )
                if chain_response.status_code == 200:
                    break
            except:
                continue
        
        if not chain_response or chain_response.status_code != 200:
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        chain_data = chain_response.json()
        
        if chain_data.get('status') == 'FAILED':
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        spot_price = chain_data.get('underlyingPrice', 0)
        dte = chain_data.get('daysToExpiration', 1)  # Avoid division by zero
        
        if dte == 0:
            dte = 1
        
        # Calculate strike range
        if strike_range:
            strike_min = spot_price - float(strike_range)
            strike_max = spot_price + float(strike_range)
        elif detail:
            strike_min = spot_price * 0.85
            strike_max = spot_price * 1.15
        else:
            strike_min = spot_price - 50
            strike_max = spot_price + 50
        
        # Process CHEX
        chex_data = {}
        expiration_map = chain_data.get('callExpDateMap', {})
        
        if not show_all and expiration_map:
            first_exp = sorted(expiration_map.keys())[0]
            expiration_map = {first_exp: expiration_map[first_exp]}
        
        for exp_date, strikes in expiration_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    delta = option.get('delta', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if delta and volume:
                        # CHEX formula: (delta × volume × 100 × spot) / DTE
                        chex = (delta * volume * 100 * spot_price) / dte
                        
                        if strike not in chex_data:
                            chex_data[strike] = 0
                        chex_data[strike] += chex
        
        # Process puts
        put_exp_map = chain_data.get('putExpDateMap', {})
        if not show_all and put_exp_map:
            first_exp = sorted(put_exp_map.keys())[0]
            put_exp_map = {first_exp: put_exp_map[first_exp]}
        
        for exp_date, strikes in put_exp_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    delta = option.get('delta', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if delta and volume:
                        chex = (delta * volume * 100 * spot_price) / dte
                        
                        if strike not in chex_data:
                            chex_data[strike] = 0
                        chex_data[strike] += chex
        
        # Sort and limit
        sorted_strikes = sorted(chex_data.items(), key=lambda x: abs(x[1]), reverse=True)[:50]
        
        chart_data = []
        total_chex = 0
        
        for strike, exposure in sorted_strikes:
            total_chex += exposure
            chart_data.append({
                'strike': strike,
                'exposure': exposure
            })
        
        chart_data.sort(key=lambda x: x['strike'])
        
        # Key levels
        key_levels = sorted(sorted_strikes, key=lambda x: abs(x[1]), reverse=True)[:5]
        key_levels = [{'strike': k[0], 'exposure': k[1]} for k in key_levels]
        
        # Interpretation
        interpretation = []
        interpretation.append(f"Charm shows time-based delta decay pressure over {dte} days")
        if total_chex > 0:
            interpretation.append(f"Net positive charm (${total_chex/1e6:.2f}M) - delta will increase over time")
        elif total_chex < 0:
            interpretation.append(f"Net negative charm (${abs(total_chex)/1e6:.2f}M) - delta will decrease over time")
        
        if key_levels:
            interpretation.append(f"Highest time decay pressure at ${key_levels[0]['strike']:.0f}")
        
        exp_date_str = sorted(chain_data.get('callExpDateMap', {}).keys())[0] if chain_data.get('callExpDateMap') else 'N/A'
        
        return jsonify({
            'ticker': ticker,
            'spot_price': spot_price,
            'expiration': exp_date_str.split(':')[0] if ':' in exp_date_str else exp_date_str,
            'dte': dte,
            'total_exposure': total_chex,
            'chart_data': chart_data,
            'key_levels': key_levels,
            'interpretation': interpretation
        })
        
    except Exception as e:
        import traceback
        print(f"Error in CHEX calculation: {str(e)}")
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/analytics/analyze', methods=['POST'])
@login_required
def analytics_analyze():
    """Comprehensive analysis with all Greeks"""
    try:
        from datetime import date
        
        data = request.get_json()
        ticker = data.get('ticker', 'SPX').upper()
        
        # Check if market is closed
        today = date.today()
        target_date = get_next_trading_day(today) if is_market_closed(today) else today
        
        # Initialize Schwab client
        import schwab
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            token_path = os.path.join(app_root, 'token.json')
        
        schwab_client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Fetch option chains with target date
        symbols_to_try = [f'${ticker}.X', ticker, f'${ticker}']
        chain_response = None
        
        for symbol in symbols_to_try:
            try:
                chain_response = schwab_client.get_option_chain(
                    symbol=symbol,
                    from_date=target_date,
                    to_date=target_date
                )
                if chain_response.status_code == 200:
                    break
            except:
                continue
        
        if not chain_response or chain_response.status_code != 200:
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        chain_data = chain_response.json()
        
        if chain_data.get('status') == 'FAILED':
            return jsonify({'error': 'Failed to fetch option chain'}), 400
        
        spot_price = chain_data.get('underlyingPrice', 0)
        dte = chain_data.get('daysToExpiration', 1)
        
        # Calculate all Greeks for default range (±50 pts)
        strike_min = spot_price - 50
        strike_max = spot_price + 50
        
        # Initialize accumulators
        total_gex = 0
        total_vex = 0
        total_dex = 0
        total_chex = 0
        total_call_volume = 0
        total_put_volume = 0
        
        gex_max = {'strike': 0, 'value': 0}
        vex_max = {'strike': 0, 'value': 0}
        dex_max = {'strike': 0, 'value': 0}
        chex_max = {'strike': 0, 'value': 0}
        
        # Process calls
        expiration_map = chain_data.get('callExpDateMap', {})
        if expiration_map:
            first_exp = sorted(expiration_map.keys())[0]
            expiration_map = {first_exp: expiration_map[first_exp]}
        
        for exp_date, strikes in expiration_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    gamma = option.get('gamma', 0)
                    vega = option.get('vega', 0)
                    delta = option.get('delta', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if volume:
                        total_call_volume += volume
                        
                        # GEX
                        if gamma:
                            gex = gamma * volume * 100 * (spot_price ** 2)
                            total_gex += gex
                            if abs(gex) > abs(gex_max['value']):
                                gex_max = {'strike': strike, 'value': gex}
                        
                        # VEX
                        if vega:
                            vex = vega * volume * 100
                            total_vex += vex
                            if abs(vex) > abs(vex_max['value']):
                                vex_max = {'strike': strike, 'value': vex}
                        
                        # DEX
                        if delta:
                            dex = delta * volume * 100 * spot_price
                            total_dex += dex
                            if abs(dex) > abs(dex_max['value']):
                                dex_max = {'strike': strike, 'value': dex}
                        
                        # CHEX
                        if delta and dte:
                            chex = (delta * volume * 100 * spot_price) / dte
                            total_chex += chex
                            if abs(chex) > abs(chex_max['value']):
                                chex_max = {'strike': strike, 'value': chex}
        
        # Process puts
        put_exp_map = chain_data.get('putExpDateMap', {})
        if put_exp_map:
            first_exp = sorted(put_exp_map.keys())[0]
            put_exp_map = {first_exp: put_exp_map[first_exp]}
        
        for exp_date, strikes in put_exp_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    gamma = option.get('gamma', 0)
                    vega = option.get('vega', 0)
                    delta = option.get('delta', 0)
                    volume = option.get('totalVolume', 0)
                    
                    if volume:
                        total_put_volume += volume
                        
                        # GEX (negative for puts)
                        if gamma:
                            gex = -1 * gamma * volume * 100 * (spot_price ** 2)
                            total_gex += gex
                        
                        # VEX
                        if vega:
                            vex = vega * volume * 100
                            total_vex += vex
                        
                        # DEX (delta is already negative for puts)
                        if delta:
                            dex = delta * volume * 100 * spot_price
                            total_dex += dex
                        
                        # CHEX
                        if delta and dte:
                            chex = (delta * volume * 100 * spot_price) / dte
                            total_chex += chex
        
        # Build summaries
        gex_summary = f"<strong>Total GEX:</strong> ${total_gex/1e9:.2f}B<br>"
        gex_summary += f"<strong>Peak at:</strong> ${gex_max['strike']:.0f} (${gex_max['value']/1e9:.2f}B)"
        
        vex_summary = f"<strong>Total VEX:</strong> ${total_vex/1e6:.2f}M<br>"
        vex_summary += f"<strong>Peak at:</strong> ${vex_max['strike']:.0f} (${vex_max['value']/1e6:.2f}M)"
        
        dex_summary = f"<strong>Total DEX:</strong> ${total_dex/1e6:.2f}M<br>"
        dex_summary += f"<strong>Peak at:</strong> ${dex_max['strike']:.0f} (${dex_max['value']/1e6:.2f}M)"
        
        chex_summary = f"<strong>Total CHEX:</strong> ${total_chex/1e6:.2f}M<br>"
        chex_summary += f"<strong>Peak at:</strong> ${chex_max['strike']:.0f} (${chex_max['value']/1e6:.2f}M)"
        
        # Calculate GEX flip point (zero-gamma level)
        # Find the strike where GEX crosses from positive to negative
        gex_by_strike = {}
        
        # Recalculate GEX by strike to find flip point
        for exp_date, strikes in expiration_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    gamma = option.get('gamma', 0)
                    volume = option.get('totalVolume', 0)
                    if gamma and volume:
                        gex = gamma * volume * 100 * (spot_price ** 2)
                        if strike not in gex_by_strike:
                            gex_by_strike[strike] = 0
                        gex_by_strike[strike] += gex
        
        for exp_date, strikes in put_exp_map.items():
            for strike_key, options in strikes.items():
                strike = float(strike_key.split(':')[0])
                if strike < strike_min or strike > strike_max:
                    continue
                
                for option in options:
                    gamma = option.get('gamma', 0)
                    volume = option.get('totalVolume', 0)
                    if gamma and volume:
                        gex = -1 * gamma * volume * 100 * (spot_price ** 2)
                        if strike not in gex_by_strike:
                            gex_by_strike[strike] = 0
                        gex_by_strike[strike] += gex
        
        # Find flip point (strike where GEX changes sign)
        sorted_strikes = sorted(gex_by_strike.items())
        flip_point = spot_price  # default to spot
        
        for i in range(len(sorted_strikes) - 1):
            current_strike, current_gex = sorted_strikes[i]
            next_strike, next_gex = sorted_strikes[i + 1]
            
            # Check if GEX crosses zero between these strikes
            if (current_gex > 0 and next_gex < 0) or (current_gex < 0 and next_gex > 0):
                # Flip point is between current_strike and next_strike
                flip_point = (current_strike + next_strike) / 2
                break
        
        # Find call and put walls (highest absolute GEX)
        call_wall_strike = 0
        call_wall_gex = 0
        put_wall_strike = 0
        put_wall_gex = 0
        
        for strike, gex in gex_by_strike.items():
            if gex > call_wall_gex and strike > spot_price:
                call_wall_strike = strike
                call_wall_gex = gex
            
            if abs(gex) > abs(put_wall_gex) and gex < 0 and strike < spot_price:
                put_wall_strike = strike
                put_wall_gex = gex
        
        # INSTITUTIONAL-GRADE ANALYSIS SUMMARY
        interpretation = []
        
        interpretation.append("═══════════════════════════════════════════════════════")
        interpretation.append("📊 DEALER FLOW ANALYSIS SUMMARY — " + ticker + f" (Spot ${spot_price:.0f})")
        interpretation.append("═══════════════════════════════════════════════════════")
        interpretation.append("")
        
        # Key Levels
        interpretation.append("🎯 KEY EXPOSURE LEVELS:")
        interpretation.append(f"• Zero-GEX Flip: ${flip_point:.0f} → {'Positive gamma regime' if spot_price > flip_point else 'Negative gamma regime'}")
        interpretation.append(f"• Call Wall: ${call_wall_strike:.0f} (${call_wall_gex/1e9:.2f}B GEX)")
        interpretation.append(f"• Put Wall: ${put_wall_strike:.0f} (${abs(put_wall_gex)/1e9:.2f}B GEX)")
        interpretation.append(f"• Highest VEX: ${vex_max['strike']:.0f} (${vex_max['value']/1e6:.2f}M)")
        interpretation.append(f"• Net DEX: ${total_dex/1e9:+.2f}B")
        interpretation.append(f"• Net CHEX: ${total_chex/1e9:+.2f}B")
        interpretation.append("")
        
        # Market Context
        interpretation.append("🌐 MARKET CONTEXT:")
        
        # Check if all exposures cluster near a level
        vex_concentration = abs(vex_max['strike'] - spot_price)
        dex_concentration = abs(dex_max['strike'] - spot_price)
        
        if vex_concentration < 30 and dex_concentration < 30 and abs(put_wall_strike - spot_price) < 30:
            interpretation.append(f"• Dealer positioning remains structurally supportive, with all exposures (GEX, VEX, DEX, CHEX) clustered near ${int(round(spot_price, -1))}.")
            interpretation.append("• This creates a low-volatility, mean-reverting environment with a mild bullish drift driven by time-decay hedging flows.")
        else:
            interpretation.append("• Dealer exposures are dispersed across multiple levels, indicating potential for directional moves.")
        
        interpretation.append("• Broader vol markets remain subdued, and no major macro catalysts are disrupting dealer equilibrium.")
        interpretation.append("")
        
        # Flow Dynamics
        interpretation.append("🔄 FLOW DYNAMICS:")
        
        # Gamma + Charm analysis
        if total_gex > 0 and total_chex > 0:
            interpretation.append("• Positive gamma and charm indicate continued buy-side hedging support.")
        elif total_gex > 0:
            interpretation.append("• Positive gamma environment provides downside support via dealer hedging.")
        elif total_gex < 0:
            interpretation.append("• Negative gamma environment amplifies volatility via dealer hedging flows.")
        
        # Vega analysis
        if vex_max['strike'] and abs(vex_max['strike'] - spot_price) < 30:
            interpretation.append(f"• Vega exposure concentrated at ${vex_max['strike']:.0f} suppresses implied volatility, reinforcing vol compression.")
        
        # Delta analysis
        if total_dex > 5e9:
            interpretation.append(f"• Delta exposure remains positive (${total_dex/1e9:.2f}B), biasing flows upward but limiting runaway rallies via dealer supply into strength.")
        elif total_dex < -5e9:
            interpretation.append(f"• Delta exposure is negative (${total_dex/1e9:.2f}B), biasing flows downward with dealer resistance on bounces.")
        elif total_dex > 0:
            interpretation.append(f"• Modest bullish delta (${total_dex/1e9:.2f}B) provides mild upward bias.")
        else:
            interpretation.append(f"• Balanced delta exposure (${total_dex/1e9:.2f}B) suggests neutral dealer positioning.")
        
        # Gamma flip implications
        if call_wall_strike > 0:
            interpretation.append(f"• A break above ${call_wall_strike:.0f} could trigger dealer buybacks and a momentum extension.")
        
        if flip_point > 0:
            interpretation.append(f"• Below ${flip_point:.0f}, flows flip short gamma, amplifying volatility.")
        
        interpretation.append("")
        
        # Tactical Outlook
        interpretation.append("💡 TACTICAL OUTLOOK:")
        
        # Determine bias
        if total_gex > 0 and total_dex > 0 and total_vex > 0:
            bias = "Bullish drift, low volatility"
        elif total_gex < 0 and total_dex < 0:
            bias = "Bearish, elevated volatility"
        elif total_gex > 0 and abs(total_dex) < 5e9:
            bias = "Range-bound, low volatility"
        elif total_dex > 5e9:
            bias = "Bullish bias"
        elif total_dex < -5e9:
            bias = "Bearish bias"
        else:
            bias = "Neutral"
        
        interpretation.append(f"• <strong>Bias:</strong> {bias}")
        interpretation.append(f"• <strong>Support:</strong> ${put_wall_strike:.0f} | <strong>Resistance:</strong> ${call_wall_strike:.0f}")
        interpretation.append(f"• <strong>Volatility Outlook:</strong> {'Suppressed' if total_gex > 0 and vex_concentration < 30 else 'Elevated' if total_gex < 0 else 'Moderate'}")
        
        # Expected range
        if call_wall_strike > 0 and put_wall_strike > 0:
            interpretation.append(f"• <strong>Expected Range:</strong> ${put_wall_strike:.0f}–${call_wall_strike:.0f} unless gamma flip triggers volatility expansion")
        
        # Trade ideas
        interpretation.append("")
        interpretation.append("📈 TRADE IDEAS:")
        
        if total_gex > 0 and abs(total_dex) < 10e9 and vex_concentration < 30:
            # Low vol, range-bound
            interpretation.append("• Short-vol structures (iron condors, credit spreads) or delta-neutral call spreads with time-decay tailwinds")
            interpretation.append(f"• Sell {put_wall_strike:.0f}-{call_wall_strike:.0f} iron condor for premium decay")
        elif total_dex > 5e9 and total_gex > 0:
            # Bullish with support
            interpretation.append("• Buy dips for long delta exposure")
            interpretation.append("• Bull call spreads or sell put spreads below support")
        elif total_dex < -5e9:
            # Bearish
            interpretation.append("• Sell rallies, bear put spreads")
            interpretation.append("• Long volatility if approaching gamma flip")
        elif total_gex < 0:
            # High vol environment
            interpretation.append("• Long straddles/strangles to capture volatility expansion")
            interpretation.append(f"• Avoid short premium positions near ${flip_point:.0f} flip point")
        else:
            interpretation.append("• Non-directional strategies (butterflies, calendars)")
        
        interpretation.append("")
        interpretation.append("═══════════════════════════════════════════════════════")
        
        # Volume imbalance (moved to bottom)
        put_call_ratio = total_put_volume / total_call_volume if total_call_volume > 0 else 0
        volume_summary = f"Put/Call Volume: {put_call_ratio:.2f} ({total_put_volume:,.0f}/{total_call_volume:,.0f})"
        
        return jsonify({
            'ticker': ticker,
            'spot_price': spot_price,
            'dte': dte,
            'flip_point': flip_point,
            'call_wall': call_wall_strike,
            'put_wall': put_wall_strike,
            'gex_summary': gex_summary,
            'vex_summary': vex_summary,
            'dex_summary': dex_summary,
            'chex_summary': chex_summary,
            'volume_summary': volume_summary,
            'interpretation': interpretation
        })
        
    except Exception as e:
        import traceback
        print(f"Error in comprehensive analysis: {str(e)}")
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/trailing')

@login_required
def trailing_stops():
    try:
        db = SessionLocal()
        try:
            # Only get trailing stops that have associated bots (filter out orphaned records)
            trailing_stops = db.query(TrailingStopState).join(Bot).all()
            # Also need bots collection for stats panel in template
            bots = db.query(Bot).all()
            return render_template('trailing_stops/list.html', trailing_stops=trailing_stops, bots=bots)
        finally:
            db.close()
    except Exception as e:
        flash(f'Error loading trailing stops: {str(e)}', 'danger')
        return render_template('trailing_stops/list.html', trailing_stops=[], bots=[])

@app.route('/trailing/add', methods=['GET', 'POST'])
@login_required
def add_trailing_stops():
    try:
        db = SessionLocal()
        try:
            if request.method == 'POST':
                action = request.form.get('action')
                selected_bots = request.form.getlist('selected_bots')
                
                if action == 'smarttrail':
                    # Handle smarttrail action
                    from services.smarttrail import SmartTrailService
                    
                    # Parse form data
                    smarttrail_target = request.form.get('smarttrail_target', 'all')
                    tier_thresholds = request.form.getlist('tier_thresholds[]')
                    trailing_percentage = request.form.get('smarttrail_trailing_percentage', type=float, default=10.0)
                    strategy_group_name = request.form.get('strategy_group_name', '').strip()
                    
                    # Validate inputs
                    if not tier_thresholds:
                        flash('Please enter at least one tier activation threshold', 'danger')
                    else:
                        try:
                            tier_activation_thresholds = [float(t) for t in tier_thresholds]
                            
                            # Validate tier thresholds
                            for threshold in tier_activation_thresholds:
                                if not (0 <= threshold <= 200):
                                    flash(f'Activation thresholds must be between 0 and 200%. Got: {threshold}%', 'danger')
                                    return redirect(url_for('add_trailing_stops'))
                            
                            # Validate trailing percentage
                            if not (0 < trailing_percentage <= 100):
                                flash(f'Trailing percentage must be between 0 and 100%. Got: {trailing_percentage}%', 'danger')
                                return redirect(url_for('add_trailing_stops'))
                            
                            # Determine target parameters
                            bot_id = None
                            selected_bot_ids = None
                            strategy_group = None
                            
                            if smarttrail_target == 'selected':
                                if not selected_bots:
                                    flash('Please select at least one bot for Smart Trail', 'danger')
                                    return redirect(url_for('add_trailing_stops'))
                                selected_bot_ids = [int(bid) for bid in selected_bots]
                            elif smarttrail_target == 'strategy':
                                if not strategy_group_name:
                                    flash('Please enter a strategy group name', 'danger')
                                    return redirect(url_for('add_trailing_stops'))
                                strategy_group = [strategy_group_name]
                            
                            # Apply smarttrail
                            service = SmartTrailService()
                            result = service.apply_tiered_trails(
                                tier_activation_thresholds=tier_activation_thresholds,
                                trailing_percentage=trailing_percentage,
                                bot_id=bot_id,
                                selected_bot_ids=selected_bot_ids,
                                strategy_group=strategy_group
                            )
                            
                            if result['success']:
                                # Build success message
                                tier_summary_lines = []
                                if result.get('tier_summary'):
                                    for tier_key, count in sorted(result['tier_summary'].items()):
                                        tier_summary_lines.append(f"{tier_key}: {count} position(s)")
                                
                                message = f"✅ Smart Trail applied successfully!\n\n"
                                message += f"Positions processed: {result['positions_processed']}\n"
                                message += f"Total positions found: {result.get('total_positions', 0)}\n"
                                
                                if tier_summary_lines:
                                    message += f"\nTier distribution:\n"
                                    message += "\n".join(f"  • {line}" for line in tier_summary_lines)
                                
                                if result.get('errors'):
                                    message += f"\n\n⚠️ Errors: {len(result['errors'])} position(s) failed"
                                
                                flash(message, 'success')
                            else:
                                flash(f"❌ Smart Trail failed: {result.get('message', 'Unknown error')}", 'danger')
                            
                            # Redirect back to prevent form resubmission
                            return redirect(url_for('add_trailing_stops'))
                            
                        except ValueError as e:
                            flash(f'Invalid input: {str(e)}', 'danger')
                        except Exception as e:
                            flash(f'Error applying Smart Trail: {str(e)}', 'danger')
                            import traceback
                            print(traceback.format_exc())
                
                elif not selected_bots:
                    flash('Please select at least one bot', 'warning')
                elif action == 'add':
                    # Handle bulk trailing stop creation
                    activation_threshold = request.form.get('activation_threshold', type=float)
                    trailing_mode = request.form.get('trailing_mode', 'percentage')
                    trailing_percentage = request.form.get('trailing_percentage', type=float)
                    trailing_dollar_amount = request.form.get('trailing_dollar_amount', type=float)
                    
                    if activation_threshold is None:
                        flash('Activation threshold is required', 'danger')
                    elif trailing_mode == 'percentage' and trailing_percentage is None:
                        flash('Trailing percentage is required for percentage mode', 'danger')
                    elif trailing_mode == 'dollar' and trailing_dollar_amount is None:
                        flash('Trailing dollar amount is required for dollar mode', 'danger')
                    else:
                        success_count = 0
                        error_count = 0
                        
                        for bot_id in selected_bots:
                            try:
                                # Pass mode-specific parameters
                                ok, msg = upsert_trailing_stop(
                                    int(bot_id), 
                                    activation_threshold, 
                                    trailing_percentage=trailing_percentage if trailing_mode == 'percentage' else None,
                                    trailing_dollar_amount=trailing_dollar_amount if trailing_mode == 'dollar' else None,
                                    trailing_mode=trailing_mode
                                )
                                if ok:
                                    success_count += 1
                                else:
                                    error_count += 1
                                    print(f"Failed to add trailing stop for bot {bot_id}: {msg}")
                            except Exception as e:
                                error_count += 1
                                print(f"Error adding trailing stop for bot {bot_id}: {e}")
                        
                        if success_count > 0:
                            mode_label = f"{trailing_percentage}%" if trailing_mode == 'percentage' else f"${trailing_dollar_amount}"
                            flash(f'Successfully added/updated trailing stops for {success_count} bot(s) with {mode_label} trailing {trailing_mode}', 'success')
                        if error_count > 0:
                            flash(f'Failed to add trailing stops for {error_count} bot(s)', 'warning')
                        
                        # Redirect back to prevent form resubmission
                        return redirect(url_for('add_trailing_stops'))
                            
                elif action == 'remove':
                    # Handle bulk trailing stop removal
                    success_count = 0
                    error_count = 0
                    
                    for bot_id in selected_bots:
                        try:
                            ok, msg = delete_trailing_stop(int(bot_id))
                            if ok:
                                success_count += 1
                            else:
                                error_count += 1
                                print(f"Failed to remove trailing stop for bot {bot_id}: {msg}")
                        except Exception as e:
                            error_count += 1
                            print(f"Error removing trailing stop for bot {bot_id}: {e}")
                    
                    if success_count > 0:
                        flash(f'Successfully removed trailing stops for {success_count} bot(s)', 'success')
                    if error_count > 0:
                        flash(f'Failed to remove trailing stops for {error_count} bot(s)', 'warning')
                    
                    # Redirect back to prevent form resubmission
                    return redirect(url_for('add_trailing_stops'))
            
            # Get all bots for display
            bots = db.query(Bot).order_by(Bot.name).all()
            
            return render_template('trailing_stops/add.html', bots=bots)
        finally:
            db.close()
    except Exception as e:
        flash(f'Error loading manage trailing stops page: {str(e)}', 'danger')
        return render_template('trailing_stops/add.html', bots=[])

# API endpoints for AJAX calls
@app.route('/api/stats')
@login_required
def api_stats():
    try:
        stats = get_dashboard_stats()
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/bots')
@login_required
def api_bots():
    try:
        db = SessionLocal()
        try:
            bots = db.query(Bot).all()
            return jsonify([{
                'id': bot.id,
                'name': bot.name,
                'status': bot.status_text,
                'state': bot.state,
                'enabled': bot.enabled,
                'paused': bot.paused
            } for bot in bots])
        finally:
            db.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/spx')
@login_required
def api_spx():
    try:
        spx_data = get_spx_price()
        return jsonify(spx_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# AJAX routes for trailing stop management
@app.route('/update_trailing_stop', methods=['POST'])
@login_required
def update_trailing_stop():
    try:
        bot_id = request.form.get('bot_id', type=int)
        activation_threshold = request.form.get('activation_threshold', type=float)
        trailing_mode = request.form.get('trailing_mode', 'percentage')
        trailing_percentage = request.form.get('trailing_percentage', type=float)
        trailing_dollar_amount = request.form.get('trailing_dollar_amount', type=float)
        
        if not bot_id or activation_threshold is None:
            return jsonify({'success': False, 'message': 'Missing required parameters'})
        
        # Validate mode-specific requirements
        if trailing_mode == 'percentage' and trailing_percentage is None:
            return jsonify({'success': False, 'message': 'Trailing percentage required for percentage mode'})
        if trailing_mode == 'dollar' and trailing_dollar_amount is None:
            return jsonify({'success': False, 'message': 'Trailing dollar amount required for dollar mode'})
        
        # Don't pass is_active parameter - let the database function handle activation state
        ok, msg = upsert_trailing_stop(
            bot_id, 
            activation_threshold, 
            trailing_percentage=trailing_percentage if trailing_mode == 'percentage' else None,
            trailing_dollar_amount=trailing_dollar_amount if trailing_mode == 'dollar' else None,
            trailing_mode=trailing_mode
        )
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/remove_trailing_stop', methods=['POST'])
@login_required
def remove_trailing_stop():
    try:
        bot_id = request.form.get('bot_id', type=int)
        
        if not bot_id:
            return jsonify({'success': False, 'message': 'Missing bot ID'})
        
        ok, msg = delete_trailing_stop(bot_id)
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('errors/500.html'), 500

# Health check endpoint
@app.route('/debug/positions')
def debug_positions():
    try:
        db = SessionLocal()
        try:
            # Get all positions with the same query as positions page
            query = db.query(Position).order_by(Position.opened_datetime.desc())
            positions = query.all()
            
            result = {
                'total_positions': len(positions),
                'position_details': []
            }
            
            for pos in positions[:5]:  # First 5 positions
                try:
                    pos_data = {
                        'id': pos.id,
                        'active': pos.active,
                        'opened_datetime': str(pos.opened_datetime),
                        'closed_datetime': str(pos.closed_datetime),
                        'status_text': pos.status_text,
                        'status_badge_class': pos.status_badge_class,
                        'duration_text': pos.duration_text,
                        'bot_name': pos.bot.name if pos.bot else 'No Bot',
                        'orders_count': len(pos.orders) if pos.orders else 0
                    }
                    result['position_details'].append(pos_data)
                except Exception as e:
                    result['position_details'].append({
                        'id': pos.id,
                        'error': str(e)
                    })
            
            return jsonify(result)
        finally:
            db.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/debug/bots-error')
def debug_bots_error():
    """Debug the exact error in the bots route"""
    import traceback
    try:
        # Step 1: Test get_bots_by_account function
        print("Step 1: Testing get_bots_by_account...")
        bots_by_account = get_bots_by_account()
        print(f"Got {len(bots_by_account)} accounts with bots")
        
        # Step 2: Test counting operations
        print("Step 2: Testing count operations...")
        all_total_bots = sum(len(blist) for blist in bots_by_account.values())
        print(f"Total bots: {all_total_bots}")
        
        # Step 3: Test bot property access
        print("Step 3: Testing bot property access...")
        for i, (account, bot_list) in enumerate(bots_by_account.items()):
            print(f"Account {i}: {account}")
            for j, bot in enumerate(bot_list[:2]):  # Test first 2 bots
                try:
                    print(f"  Bot {j}: ID={bot.id}, name={bot.name}")
                    print(f"    enabled={bot.enabled}, paused={bot.paused}")
                    print(f"    Testing positions access...")
                    total_pos = bot.total_positions  # This might fail
                    print(f"    total_positions={total_pos}")
                    active_pos = bot.active_positions_count  # This might fail
                    print(f"    active_positions_count={active_pos}")
                except Exception as bot_error:
                    print(f"    ERROR accessing bot properties: {bot_error}")
                    print(f"    Error type: {type(bot_error).__name__}")
                    print(f"    Full traceback: {traceback.format_exc()}")
                    return jsonify({
                        'error_location': 'bot_property_access',
                        'bot_id': bot.id,
                        'error': str(bot_error),
                        'error_type': type(bot_error).__name__,
                        'traceback': traceback.format_exc()
                    }), 500
        
        # Step 4: Test list comprehension operations (like in the original route)
        print("Step 4: Testing list comprehension operations...")
        try:
            all_active_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.enabled and not b.paused)
            print(f"Active bots count: {all_active_bots}")
        except Exception as comp_error:
            print(f"ERROR in list comprehension: {comp_error}")
            return jsonify({
                'error_location': 'list_comprehension',
                'error': str(comp_error),
                'error_type': type(comp_error).__name__,
                'traceback': traceback.format_exc()
            }), 500
        
        return jsonify({
            'status': 'SUCCESS',
            'message': 'All bot operations completed without lazy loading errors',
            'total_bots': all_total_bots,
            'active_bots': all_active_bots
        })
        
    except Exception as e:
        print(f"ERROR in debug route: {e}")
        print(f"Full traceback: {traceback.format_exc()}")
        return jsonify({
            'error_location': 'general',
            'error': str(e),
            'error_type': type(e).__name__,
            'traceback': traceback.format_exc()
        }), 500

@app.route('/debug/bot-states')
def debug_bot_states():
    """Show detailed bot states to understand why bots appear inactive"""
    try:
        bots_by_account = get_bots_by_account()
        
        bot_details = []
        enabled_count = 0
        paused_count = 0
        total_count = 0
        
        for account, bot_list in bots_by_account.items():
            for bot in bot_list:
                total_count += 1
                enabled = bot.enabled
                paused = bot.paused
                
                if enabled:
                    enabled_count += 1
                if paused:
                    paused_count += 1
                
                bot_details.append({
                    "id": bot.id,
                    "name": getattr(bot, 'name', 'Unknown'),
                    "enabled": enabled,
                    "paused": paused,
                    "active": enabled and not paused,
                    "account": account.name if hasattr(account, 'name') else str(account)
                })
        
        active_count = enabled_count - paused_count
        
        return jsonify({
            "total_bots": total_count,
            "enabled_bots": enabled_count,
            "paused_bots": paused_count,
            "active_bots": active_count,
            "bot_details": bot_details
        })
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "error_type": type(e).__name__
        }), 500

@app.route('/debug/bots-template')
def debug_bots_template():
    """Debug what gets passed to the bots template"""
    try:
        bots_by_account = get_bots_by_account()
        # Unfiltered counts (before any filter)
        all_total_bots = sum(len(blist) for blist in bots_by_account.values())
        all_active_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.enabled and not b.paused)
        all_inactive_bots = sum(1 for blist in bots_by_account.values() for b in blist if (not b.enabled) or b.paused)

        flt = request.args.get('filter')  # 'active' | 'inactive' | None
        if flt in ('active', 'inactive'):
            filtered = {}
            for account, blist in bots_by_account.items():
                if flt == 'active':
                    subset = [b for b in blist if b.enabled and not b.paused]
                else:  # inactive
                    subset = [b for b in blist if (not b.enabled) or b.paused]
                if subset:
                    filtered[account] = subset
            bots_by_account = filtered

        total_bots = sum(len(blist) for blist in bots_by_account.values())
        active_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.enabled and not b.paused)
        paused_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.paused)

        # Convert to serializable format for debugging
        debug_data = {
            "filter_applied": flt,
            "all_total_bots": all_total_bots,
            "all_active_bots": all_active_bots,
            "all_inactive_bots": all_inactive_bots,
            "filtered_total_bots": total_bots,
            "filtered_active_bots": active_bots,
            "filtered_paused_bots": paused_bots,
            "total_accounts": len(bots_by_account),
            "accounts_with_bots": []
        }
        
        for account, bot_list in bots_by_account.items():
            account_info = {
                "account_name": getattr(account, 'name', str(account)),
                "account_id": getattr(account, 'account_id', 'unknown'),
                "bot_count": len(bot_list),
                "bots": []
            }
            
            for bot in bot_list[:3]:  # Show first 3 bots per account
                account_info["bots"].append({
                    "id": bot.id,
                    "name": getattr(bot, 'name', 'Unknown'),
                    "enabled": bot.enabled,
                    "paused": bot.paused,
                    "is_active": bot.enabled and not bot.paused
                })
            
            debug_data["accounts_with_bots"].append(account_info)

        return jsonify(debug_data)
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "error_type": type(e).__name__
        }), 500

@app.route('/debug/simple-bots')
def debug_simple_bots():
    """Simple debug route that doesn't require authentication"""
    try:
        bots_by_account = get_bots_by_account()
        
        result = {
            "total_accounts": len(bots_by_account),
            "total_bots": sum(len(blist) for blist in bots_by_account.values()),
            "active_bots": sum(1 for blist in bots_by_account.values() for b in blist if b.enabled and not b.paused),
            "inactive_bots": sum(1 for blist in bots_by_account.values() for b in blist if (not b.enabled) or b.paused),
            "accounts": []
        }
        
        for account, bot_list in bots_by_account.items():
            account_info = {
                "name": getattr(account, 'name', str(account)),
                "bot_count": len(bot_list),
                "active_bots": sum(1 for b in bot_list if b.enabled and not b.paused),
                "inactive_bots": sum(1 for b in bot_list if (not b.enabled) or b.paused)
            }
            result["accounts"].append(account_info)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "error_type": type(e).__name__
        }), 500

@app.route('/debug/bots-page-simulation')
def debug_bots_page_simulation():
    """Simulate the exact logic of the bots page to identify the error"""
    try:
        # Simulate the exact bots() route logic
        bots_by_account = get_bots_by_account()
        
        # Unfiltered counts (before any filter)
        all_total_bots = sum(len(blist) for blist in bots_by_account.values())
        all_active_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.enabled and not b.paused)
        all_inactive_bots = sum(1 for blist in bots_by_account.values() for b in blist if (not b.enabled) or b.paused)

        flt = request.args.get('filter')  # 'active' | 'inactive' | None
        
        original_accounts = len(bots_by_account)
        
        if flt in ('active', 'inactive'):
            filtered = {}
            for account, blist in bots_by_account.items():
                if flt == 'active':
                    subset = [b for b in blist if b.enabled and not b.paused]
                else:  # inactive
                    subset = [b for b in blist if (not b.enabled) or b.paused]
                if subset:
                    filtered[account] = subset
            bots_by_account = filtered

        total_bots = sum(len(blist) for blist in bots_by_account.values())
        active_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.enabled and not b.paused)
        paused_bots = sum(1 for blist in bots_by_account.values() for b in blist if b.paused)

        # Test template rendering logic
        template_data = {
            "filter_applied": flt,
            "original_accounts": original_accounts,
            "filtered_accounts": len(bots_by_account),
            "all_total_bots": all_total_bots,
            "all_active_bots": all_active_bots,
            "all_inactive_bots": all_inactive_bots,
            "total_bots": total_bots,
            "active_bots": active_bots,
            "paused_bots": paused_bots,
            "total_accounts": len(bots_by_account),
            "will_show_no_bots_message": len(bots_by_account) == 0,
            "accounts": []
        }
        
        # Test accessing bot properties that might cause template errors
        for account, bot_list in bots_by_account.items():
            account_info = {
                "account_name": getattr(account, 'name', 'Unknown'),
                "account_id": getattr(account, 'account_id', 'Unknown'),
                "bot_count": len(bot_list),
                "sample_bots": []
            }
            
            # Test first few bots to see if any properties cause issues
            for bot in bot_list[:2]:
                try:
                    bot_info = {
                        "id": bot.id,
                        "name": getattr(bot, 'name', 'Unknown'),
                        "enabled": bot.enabled,
                        "paused": bot.paused,
                        "total_positions": bot.total_positions,  # This uses cached values
                        "active_positions_count": bot.active_positions_count,  # This uses cached values
                        "remaining_position_slots": bot.remaining_position_slots,  # This might cause issues
                        "account_name": bot.account_name  # This uses cached values
                    }
                    account_info["sample_bots"].append(bot_info)
                except Exception as bot_error:
                    account_info["sample_bots"].append({
                        "id": getattr(bot, 'id', 'unknown'),
                        "error": str(bot_error),
                        "error_type": type(bot_error).__name__
                    })
            
            template_data["accounts"].append(account_info)

        return jsonify(template_data)
        
    except Exception as e:
        import traceback
        return jsonify({
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()
        }), 500

# Pricing routes
@app.route('/pricing')
@login_required
def pricing():
    """Display the options pricing page"""
    return render_template('pricing/index.html')

@app.route('/fetch-options-data', methods=['POST'])
@login_required
def fetch_options_data():
    """Fetch 0 DTE options data using Schwab API"""
    try:
        data = request.get_json()
        calls_delta = data.get('calls_delta')
        puts_delta = data.get('puts_delta')
        
        # Validate input
        if not calls_delta or not puts_delta:
            return jsonify({
                'success': False,
                'message': 'Both calls and puts delta values are required'
            }), 400
            
        if not (0 < calls_delta <= 1):
            return jsonify({
                'success': False,
                'message': 'Calls delta must be between 0 and 1'
            }), 400
            
        if not (-1 <= puts_delta < 0):
            return jsonify({
                'success': False,
                'message': 'Puts delta must be between -1 and 0'
            }), 400
        
        # Load Schwab token
        token_data = load_schwab_token()
        if not token_data:
            return jsonify({
                'success': False,
                'message': 'Failed to load Schwab token. Please check token.json file.'
            }), 500
        
        # Fetch options data
        options_data = get_0dte_options(token_data, calls_delta, puts_delta)
        
        return jsonify({
            'success': True,
            'data': options_data
        })
        
    except Exception as e:
        print(f"Error fetching options data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'message': f'Error fetching options data: {str(e)}'
        }), 500

@app.route('/fetch-gex-data', methods=['POST'])
@login_required
def fetch_gex_data():
    """Fetch GEX (Gamma Exposure) data for SPX options"""
    try:
        # Load Schwab token
        token_data = load_schwab_token()
        if not token_data:
            return jsonify({
                'success': False,
                'message': 'Failed to load Schwab token. Please check token.json file.'
            }), 500
        
        # Calculate GEX data
        gex_data = calculate_gex_levels(token_data)
        
        return jsonify({
            'success': True,
            'data': gex_data
        })
        
    except Exception as e:
        print(f"Error fetching GEX data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'message': f'Error fetching GEX data: {str(e)}'
        }), 500

def get_schwab_account_balance():
    """Get total account balance from Schwab API"""
    try:
        # Load Schwab token
        token_data = load_schwab_token()
        if not token_data:
            return {'total_balance': 'N/A', 'error': 'Token not available'}
        
        import schwab
        
        # Create Schwab client with token file path
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            # Fallback for local development
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            token_path = os.path.join(app_root, 'token.json')
        
        client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Get account numbers
        accounts_response = client.get_account_numbers()
        
        if accounts_response.status_code != 200:
            print(f"Failed to get account numbers: {accounts_response.status_code}")
            return {'total_balance': 'N/A', 'error': 'Failed to get accounts'}
        
        accounts_data = accounts_response.json()
        
        total_balance = 0
        account_count = 0
        
        # Iterate through accounts and sum balances
        for account in accounts_data:
            account_hash = account.get('hashValue')
            if account_hash:
                # Get account details without requesting specific fields
                account_response = client.get_account(account_hash)
                
                if account_response.status_code == 200:
                    account_details = account_response.json()
                    
                    # Extract balance information
                    securities_account = account_details.get('securitiesAccount', {})
                    current_balances = securities_account.get('currentBalances', {})
                    
                    # Get total value (liquidation value is most comprehensive)
                    account_value = current_balances.get('liquidationValue', 0)
                    if account_value:
                        total_balance += float(account_value)
                        account_count += 1
                else:
                    print(f"Failed to get account details for {account_hash}: {account_response.status_code}")
        
        return {
            'total_balance': f"${total_balance:,.2f}",
            'account_count': account_count,
            'error': None
        }
        
    except Exception as e:
        print(f"Error fetching account balance: {e}")
        import traceback
        traceback.print_exc()
        return {'total_balance': 'N/A', 'error': str(e)}

def calculate_total_premium_opened():
    """Calculate total premium opened (initial premium sold from all active positions)"""
    try:
        db = SessionLocal()
        
        # Get all active positions with their orders
        from sqlalchemy.orm import joinedload
        active_positions = db.query(Position).options(
            joinedload(Position.orders)
        ).filter(Position.active == True).all()
        
        total_premium_opened = 0.0
        
        for position in active_positions:
            total_premium_opened += position.initial_premium_sold
        
        return total_premium_opened
        
    except Exception as e:
        print(f"Error calculating total premium opened: {e}")
        import traceback
        traceback.print_exc()
        return 0.0
    finally:
        db.close()

def calculate_account_premium_metrics(account_number, liquidation_value=None):
    """Calculate premium metrics for a specific account matching looptrader-pro logic
    
    Args:
        account_number: Account number string
        liquidation_value: Optional account Net Liquidation Value for percentage calculation
    """
    try:
        db = SessionLocal()
        
        # Find the account_id for this account number using the same pattern as build_schwab_cache_for_positions
        # Match by checking if account_id is contained in or at the end of account_number
        account = None
        all_accounts = db.query(BrokerageAccount).all()
        account_number_str = str(account_number)
        
        # First try to find exact/endswith matches (more specific)
        for broker_account in all_accounts:
            account_id_str = str(broker_account.account_id)
            # Prefer endswith matches (more specific) over contains matches
            if account_number_str.endswith(account_id_str):
                account = broker_account
                break
        
        # If no endswith match, try contains match (less specific, but matches build_schwab_cache pattern)
        if not account:
            for broker_account in all_accounts:
                account_id_str = str(broker_account.account_id)
                if account_id_str in account_number_str:
                    account = broker_account
                    break
        
        if not account:
            print(f"calculate_account_premium_metrics: No BrokerageAccount found for account_number {account_number}")
            return {
                'premium_opened': 0.0,
                'current_open_premium': 0.0,
                'profit_loss': 0.0,
                'profit_loss_percent': 0.0
            }
        
        # Get active positions for this account with eager loading
        from sqlalchemy.orm import joinedload
        active_positions = db.query(Position).options(
            joinedload(Position.orders).joinedload(Order.orderLegCollection).joinedload(OrderLeg.instrument),
            joinedload(Position.bot)
        ).filter(
            Position.active == True,
            Position.account_id == account.account_id
        ).all()
        
        # Build Schwab cache for accurate real-time P&L calculation (matches looptrader-pro)
        # This cache provides current market values from Schwab API, ensuring accuracy
        from models.database import build_schwab_cache_for_positions
        schwab_cache = build_schwab_cache_for_positions(active_positions)
        
        # Inject cache into positions BEFORE accessing current_open_premium or current_pnl
        # This ensures position.current_open_premium uses real-time quotes when available
        for position in active_positions:
            position._schwab_cache = schwab_cache
        
        premium_opened = 0.0
        current_open_premium = 0.0
        total_pnl = 0.0
        
        # Calculate using looptrader-pro logic (matches /positions command calculation)
        # CRITICAL: Use direct summation, NOT derived calculation
        # Use abs() for premium_opened to get cost basis (always positive) - matches looptrader-pro
        for position in active_positions:
            premium_opened += abs(position.initial_premium_sold)  # Cost basis (always positive)
            current_open_premium += position.current_open_premium  # Direct summation
            total_pnl += position.current_pnl  # Uses looptrader-pro calculation (matches /positions)
        
        # Calculate percentage based on account NLV if provided, otherwise use premium
        if liquidation_value and liquidation_value > 0.01:
            # Use NLV as the base for percentage calculation
            profit_loss_percent = (total_pnl / liquidation_value * 100)
        elif premium_opened > 0.01:
            # Fallback to premium-based calculation if NLV not available
            profit_loss_percent = (total_pnl / premium_opened * 100)
        else:
            profit_loss_percent = 0.0
        
        return {
            'premium_opened': premium_opened,
            'current_open_premium': current_open_premium,  # Direct summation, not derived
            'profit_loss': total_pnl,
            'profit_loss_percent': profit_loss_percent
        }
        
    except Exception as e:
        print(f"Error calculating account premium metrics for {account_number}: {e}")
        import traceback
        traceback.print_exc()
        return {
            'premium_opened': 0.0,
            'current_open_premium': 0.0,
            'profit_loss': 0.0,
            'profit_loss_percent': 0.0
        }
    finally:
        db.close()

def get_schwab_account_positions(account_hash):
    """Get detailed positions for a specific account including current market values"""
    try:
        # Load Schwab token
        token_data = load_schwab_token()
        if not token_data:
            return None
        
        import schwab
        
        # Create Schwab client
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            token_path = os.path.join(app_root, 'token.json')
        
        client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Get account with positions
        account_response = client.get_account(account_hash, fields=['positions'])
        
        if account_response.status_code == 200:
            account_data = account_response.json()
            securities_account = account_data.get('securitiesAccount', {})
            positions = securities_account.get('positions', [])
            
            option_positions = []
            for position in positions:
                instrument = position.get('instrument', {})
                
                # Check if this is an option position
                if instrument.get('assetType') == 'OPTION':
                    option_positions.append({
                        'symbol': instrument.get('symbol', ''),
                        'description': instrument.get('description', ''),
                        'quantity': position.get('longQuantity', 0) - position.get('shortQuantity', 0),
                        'market_value': position.get('marketValue', 0),
                        'average_price': position.get('averagePrice', 0),
                        'current_day_pnl': position.get('currentDayProfitLoss', 0),
                        'underlying_symbol': instrument.get('underlyingSymbol', '')
                    })
            
            return option_positions
        else:
            print(f"Failed to get positions for account {account_hash}: {account_response.status_code}")
            return None
            
    except Exception as e:
        print(f"Error getting Schwab account positions: {e}")
        import traceback
        traceback.print_exc()
        return None

def calculate_current_open_premium_from_schwab():
    """Calculate current open premium using real Schwab API market values
    
    IMPORTANT: Matches looptrader-pro spread calculation logic by keeping signed values.
    For spreads, Schwab reports:
    - Short legs: negative market value (liability)
    - Long legs: positive market value (asset)
    - Net spread value = sum of both legs (preserves spread mechanics)
    """
    try:
        # Load Schwab token
        token_data = load_schwab_token()
        if not token_data:
            return 0.0
        
        import schwab
        
        # Create Schwab client
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            token_path = os.path.join(app_root, 'token.json')
        
        client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Get account numbers
        accounts_response = client.get_account_numbers()
        if accounts_response.status_code != 200:
            return 0.0
        
        accounts_data = accounts_response.json()
        total_option_market_value = 0.0
        
        # Get positions for each account
        for account in accounts_data:
            account_hash = account.get('hashValue')
            if account_hash:
                # Get account with positions
                account_response = client.get_account(account_hash, fields=['positions'])
                
                if account_response.status_code == 200:
                    account_data = account_response.json()
                    securities_account = account_data.get('securitiesAccount', {})
                    positions = securities_account.get('positions', [])
                    
                    # Sum all option market values (keeping signs for spread calculation)
                    account_net_value = 0.0
                    for position in positions:
                        instrument = position.get('instrument', {})
                        
                        # Check if this is an option position
                        if instrument.get('assetType') == 'OPTION':
                            market_value = float(position.get('marketValue', 0))
                            # Keep sign: negative for shorts, positive for longs
                            # This allows spreads to net properly
                            account_net_value += market_value
                    
                    # After netting all positions (spreads properly calculated),
                    # take absolute value for "cost to close"
                    total_option_market_value += abs(account_net_value)
        
        return total_option_market_value
        
    except Exception as e:
        print(f"Error calculating current open premium from Schwab: {e}")
        import traceback
        traceback.print_exc()
        return 0.0

def calculate_current_open_premium():
    """Calculate current open premium using real Schwab API data first, fallback to estimation"""
    try:
        # Try to get real market data from Schwab first
        schwab_market_value = calculate_current_open_premium_from_schwab()
        if schwab_market_value > 0:
            return schwab_market_value
        
        # Fallback to database calculation if Schwab data unavailable
        db = SessionLocal()
        
        # Get all active positions with their orders
        from sqlalchemy.orm import joinedload
        active_positions = db.query(Position).options(
            joinedload(Position.orders)
        ).filter(Position.active == True).all()
        
        current_open_premium = 0.0
        
        for position in active_positions:
            current_open_premium += position.current_open_premium
        
        return current_open_premium
        
    except Exception as e:
        print(f"Error calculating current open premium: {e}")
        import traceback
        traceback.print_exc()
        return 0.0
    finally:
        if 'db' in locals():
            db.close()

def calculate_total_open_premium():
    """Legacy function - now returns calculate_total_premium_opened for backward compatibility"""
    return calculate_total_premium_opened()

def get_schwab_accounts_detail():
    """Get detailed account information from Schwab API including individual account balances"""
    try:
        # Load Schwab token
        token_data = load_schwab_token()
        if not token_data:
            return {'accounts': [], 'error': 'Token not available'}
        
        import schwab
        
        # Create Schwab client with token file path
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            # Fallback for local development
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            token_path = os.path.join(app_root, 'token.json')
        
        client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Get account numbers
        accounts_response = client.get_account_numbers()
        
        if accounts_response.status_code != 200:
            print(f"Failed to get account numbers: {accounts_response.status_code}")
            return {'accounts': [], 'error': 'Failed to get accounts'}
        
        accounts_data = accounts_response.json()
        detailed_accounts = []
        
        # Iterate through accounts and get detailed information
        for account in accounts_data:
            account_hash = account.get('hashValue')
            account_number = account.get('accountNumber')
            if account_hash:
                # Get account details
                account_response = client.get_account(account_hash)
                
                if account_response.status_code == 200:
                    account_details = account_response.json()
                    
                    # Extract account information
                    securities_account = account_details.get('securitiesAccount', {})
                    current_balances = securities_account.get('currentBalances', {})
                    
                    # Get account type and other details
                    account_type = securities_account.get('type', 'Unknown')
                    
                    # Get balance information
                    liquidation_value = current_balances.get('liquidationValue', 0)
                    cash_balance = current_balances.get('cashBalance', 0)
                    buying_power = current_balances.get('buyingPower', 0)
                    
                    # Get PNL information for today
                    day_trading_buying_power = current_balances.get('dayTradingBuyingPower', 0)
                    equity = current_balances.get('equity', 0)
                    long_market_value = current_balances.get('longMarketValue', 0)
                    short_market_value = current_balances.get('shortMarketValue', 0)
                    
                    # Calculate options-based P&L for this account
                    # Pass liquidation_value so percentage is calculated based on NLV
                    account_liquidation_value = float(liquidation_value) if liquidation_value else 0
                    account_metrics = calculate_account_premium_metrics(account_number, liquidation_value=account_liquidation_value)
                    
                    detailed_accounts.append({
                        'account_hash': account_hash,
                        'account_number': account_number,
                        'account_type': account_type,
                        'liquidation_value': account_liquidation_value,
                        'cash_balance': float(cash_balance) if cash_balance else 0,
                        'buying_power': float(buying_power) if buying_power else 0,
                        'todays_pnl': account_metrics['profit_loss'],
                        'todays_pnl_percent': account_metrics['profit_loss_percent'],
                        'formatted_liquidation_value': f"${float(liquidation_value):,.2f}" if liquidation_value else "$0.00",
                        'formatted_cash_balance': f"${float(cash_balance):,.2f}" if cash_balance else "$0.00",
                        'formatted_buying_power': f"${float(buying_power):,.2f}" if buying_power else "$0.00",
                        'formatted_todays_pnl': f"${account_metrics['profit_loss']:,.2f}",
                        'formatted_todays_pnl_percent': f"{account_metrics['profit_loss_percent']:+.2f}%"
                    })
                else:
                    print(f"Failed to get account details for {account_hash}: {account_response.status_code}")
        
        # Calculate totals
        total_value = sum(acc['liquidation_value'] for acc in detailed_accounts)
        
        # Calculate totals matching looptrader-pro's /positions command calculation
        # This ensures the accounts page shows the same values as the Telegram command
        db = SessionLocal()
        active_positions = db.query(Position).filter_by(active=True).all()
        
        # Build Schwab cache for accurate real-time P&L calculation (matches looptrader-pro)
        # This cache provides current market values from Schwab API, ensuring accuracy
        schwab_cache = build_schwab_cache_for_positions(active_positions)
        
        # Inject Schwab cache into positions BEFORE accessing current_open_premium or current_pnl
        # This ensures position.current_open_premium uses real-time quotes when available
        for pos in active_positions:
            pos._schwab_cache = schwab_cache
        
        # Calculate totals by summing from each position (matches looptrader-pro's approach)
        # Total premium opened: use abs() for cost basis (always positive)
        # This matches how looptrader-pro calculates entry_credit for credit spreads
        total_premium_opened = sum(abs(pos.initial_premium_sold) for pos in active_positions)
        current_open_premium = sum(pos.current_open_premium for pos in active_positions)
        current_profit_loss = sum(pos.current_pnl for pos in active_positions)
        db.close()
        
        # Calculate percentage using total account NLV instead of premium
        # This shows P&L as a percentage of total account value
        current_profit_loss_percent = (current_profit_loss / total_value * 100) if total_value > 0.01 else 0.0
        
        return {
            'accounts': detailed_accounts,
            'total_value': f"${total_value:,.2f}",
            'total_premium_opened': f"${total_premium_opened:,.2f}",
            'current_open_premium': f"${current_open_premium:,.2f}",
            'current_profit_loss': f"${current_profit_loss:,.2f}",
            'current_profit_loss_percent': f"{current_profit_loss_percent:+.2f}%",
            'account_count': len(detailed_accounts),
            'error': None
        }
        
    except Exception as e:
        print(f"Error fetching account details: {e}")
        import traceback
        traceback.print_exc()
        return {'accounts': [], 'error': str(e)}

def load_schwab_token():
    """Load Schwab token from token.json in the app root directory"""
    try:
        # Look for token.json in the app root directory (accessible in Docker)
        token_path = os.path.join('/app', 'token.json')
        
        # Fallback to local development path if not in Docker
        if not os.path.exists(token_path):
            # For local development, look in the project root
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            token_path = os.path.join(app_root, 'token.json')
        
        if not os.path.exists(token_path):
            print(f"Token file not found at: {token_path}")
            print(f"Please place token.json in the project root directory")
            return None
            
        print(f"Loading token from: {token_path}")
        with open(token_path, 'r') as f:
            token_data = json.load(f)
            
        # Handle nested token structure (schwab-py format)
        if 'token' in token_data:
            token_info = token_data['token']
        else:
            token_info = token_data
            
        # Validate required fields
        if not all(key in token_info for key in ['access_token', 'refresh_token']):
            print(f"Token file missing required fields. Found keys: {list(token_info.keys())}")
            return None
            
        print(f"Successfully loaded token from: {token_path}")
        return token_data  # Return the full token data structure
        
    except Exception as e:
        print(f"Error loading token: {e}")
        import traceback
        traceback.print_exc()
        return None

def calculate_gex_levels(token_data):
    """
    Calculate Gamma Exposure (GEX) levels for SPX options
    GEX = Gamma * Open Interest * Contract Multiplier * Spot Price^2
    """
    try:
        import schwab
        from datetime import date, datetime, timedelta
        
        def is_market_closed(check_date):
            """Check if market is closed (weekend or US stock market holiday)"""
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
        
        def get_next_trading_day(start_date):
            """Get the next trading day after start_date"""
            next_day = start_date + timedelta(days=1)
            while is_market_closed(next_day):
                next_day += timedelta(days=1)
            return next_day
        
        # Create Schwab client
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            token_path = os.path.join(app_root, 'token.json')
        
        client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Get current SPX price
        spx_response = client.get_quote('$SPX')
        spx_price = 5800  # Default fallback
        
        if spx_response.status_code == 200:
            spx_data = spx_response.json()
            if '$SPX' in spx_data and 'quote' in spx_data['$SPX']:
                spx_price = spx_data['$SPX']['quote'].get('lastPrice', 5800)
        
        # Check if market is open today, if not use next trading day
        today = date.today()
        
        if is_market_closed(today):
            target_date = get_next_trading_day(today)
            print(f"Market is CLOSED today ({today.strftime('%A, %Y-%m-%d')})")
            print(f"Using next trading day: {target_date.strftime('%A, %Y-%m-%d')}")
        else:
            target_date = today
            print(f"Market is OPEN today ({today.strftime('%A, %Y-%m-%d')})")
        
        print(f"Fetching SPX options for GEX calculation, date: {target_date}")
        print(f"Current SPX Price: {spx_price}")
        
        # Define strike range around current price (we'll filter after fetching)
        # +/- 50 points of current SPX price
        strike_range_min = spx_price - 50
        strike_range_max = spx_price + 50
        
        print(f"Will filter strikes from {strike_range_min} to {strike_range_max}")
        
        # Fetch SPX options chain for target date
        # Try different symbol formats for SPX
        symbols_to_try = ['$SPX.X', 'SPX', '$SPX']
        
        for symbol in symbols_to_try:
            print(f"Trying symbol: {symbol}")
            response = client.get_option_chain(
                symbol=symbol,
                from_date=target_date,
                to_date=target_date
            )
            
            if response.status_code == 200:
                print(f"Success with symbol: {symbol}")
                break
            else:
                print(f"Failed with symbol {symbol}: {response.status_code}")
        
        if response.status_code != 200:
            print(f"All symbols failed. Last response: {response.status_code}")
            print(f"API Response Text: {response.text[:500] if hasattr(response, 'text') else 'No response'}")
            raise Exception(f"Failed to fetch options data with all symbols: {response.status_code}")
        
        options_data = response.json()
        print(f"========== GEX DEBUG START ==========")
        print(f"Options data keys: {options_data.keys()}")
        
        # Find the nearest expiration date
        call_exp_dates = list(options_data.get('callExpDateMap', {}).keys())
        put_exp_dates = list(options_data.get('putExpDateMap', {}).keys())
        
        print(f"Call expiration dates found: {len(call_exp_dates)}")
        print(f"Put expiration dates found: {len(put_exp_dates)}")
        
        if call_exp_dates:
            print(f"First 3 call exp dates: {call_exp_dates[:3]}")
        if put_exp_dates:
            print(f"First 3 put exp dates: {put_exp_dates[:3]}")
        
        all_exp_dates = sorted(set(call_exp_dates + put_exp_dates))
        
        if not all_exp_dates:
            import sys
            print(f"ERROR: No expiration dates found in response!", flush=True)
            print(f"This likely means market is closed and no options returned for today", flush=True)
            print(f"Trying to fetch next available expiration...", flush=True)
            sys.stdout.flush()
            
            # Try fetching without date restriction to get nearest available
            for symbol in symbols_to_try:
                print(f"Trying {symbol} without date filter for next expiration", flush=True)
                sys.stdout.flush()
                response = client.get_option_chain(symbol=symbol)
                
                if response.status_code == 200:
                    options_data = response.json()
                    call_exp_dates = list(options_data.get('callExpDateMap', {}).keys())
                    put_exp_dates = list(options_data.get('putExpDateMap', {}).keys())
                    all_exp_dates = sorted(set(call_exp_dates + put_exp_dates))
                    
                    if all_exp_dates:
                        print(f"SUCCESS! Found expirations without date filter: {all_exp_dates[:3]}", flush=True)
                        print(f"Total expirations available: {len(all_exp_dates)}", flush=True)
                        sys.stdout.flush()
                        break
                else:
                    print(f"Failed fetching without date filter: {response.status_code}", flush=True)
                    sys.stdout.flush()
            
            if not all_exp_dates:
                raise Exception("No expiration dates found in options chain even without date filter")
        
        # Get the nearest expiration (first one in sorted list)
        nearest_expiration = all_exp_dates[0]
        print(f"All available expirations: {all_exp_dates[:5]}")  # Show first 5
        print(f"Using nearest expiration: {nearest_expiration}")
        
        # Parse the expiration date to show it nicely
        # Format is typically "2025-10-04:1" where :1 means it's a daily expiration
        exp_date_str = nearest_expiration.split(':')[0]
        
        # Calculate GEX by strike - only for nearest expiration
        gex_by_strike = {}
        contract_multiplier = 100
        
        # Process calls for nearest expiration only - filter by strike range
        call_map = options_data.get('callExpDateMap', {})
        print(f"Looking for calls in expiration: {nearest_expiration}", flush=True)
        print(f"Available call expirations in map: {list(call_map.keys())[:3]}", flush=True)
        
        if nearest_expiration in call_map:
            strikes = call_map[nearest_expiration]
            print(f"Processing {len(strikes)} call strikes for {nearest_expiration}", flush=True)
            
            strikes_in_range = 0
            strikes_with_data = 0
            first_strike_sampled = False
            
            for strike_str, option_list in strikes.items():
                strike = float(strike_str)
                
                # Filter: only process strikes within +/- 50 of current SPX
                if not (strike_range_min <= strike <= strike_range_max):
                    continue
                
                strikes_in_range += 1
                
                # Debug: sample first strike in range
                if not first_strike_sampled and option_list:
                    sample = option_list[0]
                    print(f"Sample call option at strike {strike}:", flush=True)
                    print(f"  gamma={sample.get('gamma')}, OI={sample.get('openInterest')}, volume={sample.get('totalVolume')}", flush=True)
                    first_strike_sampled = True
                
                for option in option_list:
                    gamma = option.get('gamma', 0)
                    open_interest = option.get('openInterest', 0)
                    volume = option.get('totalVolume', 0)
                    
                    # For 0 DTE, open interest may be 0, so use volume as fallback
                    effective_oi = open_interest if open_interest > 0 else volume
                    
                    if gamma and effective_oi:
                        strikes_with_data += 1
                        # Calls: negative GEX (dealers are short calls = long gamma)
                        # But we show from dealer perspective who is SHORT gamma
                        gex = gamma * effective_oi * contract_multiplier * spx_price * spx_price / 1_000_000_000
                        
                        if strike not in gex_by_strike:
                            gex_by_strike[strike] = {'call_gex': 0, 'put_gex': 0, 'total_gex': 0}
                        
                        gex_by_strike[strike]['call_gex'] -= gex  # Negative for calls
            
            print(f"Found {strikes_in_range} call strikes in ±50 range", flush=True)
            print(f"Found {strikes_with_data} calls with gamma AND open interest", flush=True)
        else:
            print(f"WARNING: {nearest_expiration} not found in call_map!", flush=True)
            print(f"Available expirations: {list(call_map.keys())}", flush=True)
        
        # Process puts for nearest expiration only - filter by strike range
        put_map = options_data.get('putExpDateMap', {})
        print(f"Looking for puts in expiration: {nearest_expiration}", flush=True)
        print(f"Available put expirations in map: {list(put_map.keys())[:3]}", flush=True)
        
        if nearest_expiration in put_map:
            strikes = put_map[nearest_expiration]
            print(f"Processing {len(strikes)} put strikes for {nearest_expiration}", flush=True)
            
            strikes_in_range = 0
            strikes_with_data = 0
            first_strike_sampled = False
            
            for strike_str, option_list in strikes.items():
                strike = float(strike_str)
                
                # Filter: only process strikes within +/- 50 of current SPX
                if not (strike_range_min <= strike <= strike_range_max):
                    continue
                
                strikes_in_range += 1
                
                # Debug: sample first strike in range
                if not first_strike_sampled and option_list:
                    sample = option_list[0]
                    print(f"Sample put option at strike {strike}:", flush=True)
                    print(f"  gamma={sample.get('gamma')}, OI={sample.get('openInterest')}, volume={sample.get('totalVolume')}", flush=True)
                    first_strike_sampled = True
                
                for option in option_list:
                    gamma = option.get('gamma', 0)
                    open_interest = option.get('openInterest', 0)
                    volume = option.get('totalVolume', 0)
                    
                    # For 0 DTE, open interest may be 0, so use volume as fallback
                    effective_oi = open_interest if open_interest > 0 else volume
                    
                    if gamma and effective_oi:
                        strikes_with_data += 1
                        # Puts: positive GEX (dealers are short puts = short gamma)
                        gex = gamma * effective_oi * contract_multiplier * spx_price * spx_price / 1_000_000_000
                        
                        if strike not in gex_by_strike:
                            gex_by_strike[strike] = {'call_gex': 0, 'put_gex': 0, 'total_gex': 0}
                        
                        gex_by_strike[strike]['put_gex'] += gex  # Positive for puts
            
            print(f"Found {strikes_in_range} put strikes in ±50 range", flush=True)
            print(f"Found {strikes_with_data} puts with gamma AND open interest", flush=True)
        else:
            print(f"WARNING: {nearest_expiration} not found in put_map!", flush=True)
            print(f"Available expirations: {list(put_map.keys())}", flush=True)
        
        # Calculate total GEX and prepare chart data
        strikes = []
        call_gex_values = []
        put_gex_values = []
        total_gex_values = []
        
        # Sort strikes and filter to reasonable range around current price
        all_strikes = sorted(gex_by_strike.keys())
        
        print(f"Total strikes found in gex_by_strike: {len(all_strikes)}", flush=True)
        print(f"SPX Price: {spx_price}", flush=True)
        print(f"Strike range: {strike_range_min} to {strike_range_max}", flush=True)
        
        if not all_strikes:
            import sys
            print(f"ERROR: No strikes found!", flush=True)
            print(f"gex_by_strike is empty: {len(gex_by_strike)}", flush=True)
            print(f"Checking if data was actually in the expiration maps...", flush=True)
            print(f"Calls available: {len(call_map.get(nearest_expiration, {}))}", flush=True)
            print(f"Puts available: {len(put_map.get(nearest_expiration, {}))}", flush=True)
            sys.stdout.flush()
            raise Exception("No option strikes found in the ±50 point range. Market may be closed or no options available.")
        
        # Strikes are already filtered to ±50 range during processing
        for strike in all_strikes:
            gex_data = gex_by_strike[strike]
            total_gex = gex_data['call_gex'] + gex_data['put_gex']
            gex_data['total_gex'] = total_gex
            
            strikes.append(strike)
            call_gex_values.append(round(gex_data['call_gex'], 2))
            put_gex_values.append(round(gex_data['put_gex'], 2))
            total_gex_values.append(round(total_gex, 2))
        
        print(f"Strikes in ±50 range: {len(strikes)}")
        
        if not strikes:
            raise Exception(f"No strikes found in range {strike_range_min} to {strike_range_max}. Current SPX: {spx_price}")
        
        # Find key levels
        max_positive_gex = max(total_gex_values) if total_gex_values else 0
        max_negative_gex = min(total_gex_values) if total_gex_values else 0
        
        max_positive_strike = strikes[total_gex_values.index(max_positive_gex)] if max_positive_gex > 0 else None
        max_negative_strike = strikes[total_gex_values.index(max_negative_gex)] if max_negative_gex < 0 else None
        
        # Find zero gamma (flip point)
        zero_gamma_strike = None
        for i in range(len(total_gex_values) - 1):
            if total_gex_values[i] * total_gex_values[i + 1] < 0:  # Sign change
                zero_gamma_strike = strikes[i]
                break
        
        # Enhanced Market interpretation
        interpretation = []
        
        # 1. Basic GEX Levels
        if zero_gamma_strike:
            interpretation.append(f"🎯 Zero GEX flip point at ${zero_gamma_strike:.0f}")
        
        if max_positive_strike:
            interpretation.append(f"📞 Strongest call wall at ${max_positive_strike:.0f} ({max_positive_gex:.2f}B GEX)")
        
        if max_negative_strike:
            interpretation.append(f"📉 Strongest put wall at ${max_negative_strike:.0f} ({abs(max_negative_gex):.2f}B GEX)")
        
        # 2. Market Regime Context
        if zero_gamma_strike:
            if spx_price > zero_gamma_strike:
                regime = "positive gamma"
                interpretation.append(f"✅ POSITIVE GAMMA REGIME: Spot (${spx_price:.0f}) is above flip point (${zero_gamma_strike:.0f})")
                interpretation.append("Market makers are long gamma — their hedging dampens volatility (sell rallies, buy dips)")
                interpretation.append(f"⚠️ If spot breaks below ${zero_gamma_strike:.0f}, market transitions to negative gamma with expanding volatility")
            else:
                regime = "negative gamma"
                interpretation.append(f"⚠️ NEGATIVE GAMMA REGIME: Spot (${spx_price:.0f}) is below flip point (${zero_gamma_strike:.0f})")
                interpretation.append("Market makers are short gamma — their hedging amplifies volatility (sell dips, buy rallies)")
                interpretation.append(f"📈 If spot breaks above ${zero_gamma_strike:.0f}, volatility may compress in positive gamma zone")
        
        # 3. Expected Volatility and Range
        if max_positive_strike and max_negative_strike:
            range_width = abs(max_positive_strike - max_negative_strike)
            upper_bound = max(max_positive_strike, max_negative_strike)
            lower_bound = min(max_positive_strike, max_negative_strike)
            
            interpretation.append(f"📊 Expected intraday range: ${lower_bound:.0f}–${upper_bound:.0f} ({range_width:.0f} pts)")
            
            if range_width < 100:
                interpretation.append(f"🔒 Narrow range ({range_width:.0f} pts) implies compressed volatility and mean-reversion bias")
            else:
                interpretation.append(f"📏 Wide range ({range_width:.0f} pts) allows for directional movement")
        
        # 4. Liquidity and Pinning Zones
        if max_positive_strike and max_negative_strike:
            if abs(spx_price - max_positive_strike) < 30:
                interpretation.append(f"📍 Price near call wall (${max_positive_strike:.0f}) — watch for pinning effects and resistance")
            elif abs(spx_price - max_negative_strike) < 30:
                interpretation.append(f"📍 Price near put wall (${max_negative_strike:.0f}) — watch for pinning effects and support")
            else:
                interpretation.append(f"🎯 Price between walls — high dealer gamma exposure creates liquidity magnets at extremes")
        
        # 5. Trading Implications
        interpretation.append("💡 TRADING IMPLICATIONS:")
        
        if zero_gamma_strike and spx_price > zero_gamma_strike:
            # Positive gamma regime
            if max_positive_strike and abs(spx_price - max_positive_strike) < 50:
                interpretation.append(f"• Directional Bias: Neutral-to-slightly bearish (capped by call wall at ${max_positive_strike:.0f})")
            else:
                interpretation.append("• Directional Bias: Neutral (positive gamma supports mean reversion)")
            
            interpretation.append("• Volatility Bias: Expect low realized volatility unless walls are breached")
            interpretation.append("• Strategy: Favor mean-reversion trades (iron condors, credit spreads)")
            interpretation.append(f"• Risk Management: Tight stops if spot breaks below ${zero_gamma_strike:.0f} flip point")
        else:
            # Negative gamma regime or no flip point
            interpretation.append("• Directional Bias: Higher directional risk in negative gamma")
            interpretation.append("• Volatility Bias: Expect elevated realized volatility")
            interpretation.append("• Strategy: Consider long gamma trades (buying options, straddles)")
            interpretation.append("• Risk Management: Wider stops to accommodate volatility expansion")
        
        # 6. Summary
        total_net_gex = sum(total_gex_values)
        if max_positive_strike and max_negative_strike and zero_gamma_strike:
            summary = f"📋 SUMMARY: "
            if spx_price > zero_gamma_strike:
                summary += f"Positive gamma regime with controlled volatility. "
            else:
                summary += f"Negative gamma regime with elevated volatility. "
            
            summary += f"Price expected to gravitate between ${min(max_positive_strike, max_negative_strike):.0f}–${max(max_positive_strike, max_negative_strike):.0f}. "
            
            if total_net_gex > 0:
                summary += f"Net positive GEX ({total_net_gex:.2f}B) suggests downside support."
            elif total_net_gex < 0:
                summary += f"Net negative GEX ({total_net_gex:.2f}B) suggests upside resistance."
            
            interpretation.append(summary)
        
        return {
            'strikes': strikes,
            'call_gex': call_gex_values,
            'put_gex': put_gex_values,
            'total_gex': total_gex_values,
            'current_price': round(spx_price, 2),
            'max_positive_gex': round(max_positive_gex, 2),
            'max_negative_gex': round(max_negative_gex, 2),
            'max_positive_strike': max_positive_strike,
            'max_negative_strike': max_negative_strike,
            'zero_gamma_strike': zero_gamma_strike,
            'expiration_date': exp_date_str,
            'expiration_full': nearest_expiration,
            'interpretation': interpretation
        }
        
    except Exception as e:
        print(f"Error calculating GEX levels: {e}")
        import traceback
        traceback.print_exc()
        raise

def get_0dte_options(token_data, calls_delta_target, puts_delta_target):
    """
    Fetch 0 DTE options data for SPX and find strikes closest to target deltas
    """
    try:
        import schwab
        from datetime import date
        
        # Create Schwab client with token file path (schwab-py will handle the token format)
        token_path = '/app/token.json'
        if not os.path.exists(token_path):
            # Fallback for local development
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            token_path = os.path.join(app_root, 'token.json')
        
        client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ.get('SCHWAB_API_KEY'),
            app_secret=os.environ.get('SCHWAB_APP_SECRET'),
            enforce_enums=False
        )
        
        # Check if market is closed and determine target date
        today = date.today()
        if is_market_closed(today):
            target_date = get_next_trading_day(today)
            print(f"Market is CLOSED today ({today.strftime('%A, %Y-%m-%d')})")
            print(f"Fetching options for next trading day: {target_date.strftime('%A, %Y-%m-%d')}")
        else:
            target_date = today
            print(f"Market is OPEN today ({today.strftime('%A, %Y-%m-%d')})")
            print(f"Fetching 0 DTE options for: {target_date}")
        
        # Fetch SPX options chain for target date
        # Try different symbol formats for SPX
        symbols_to_try = ['$SPX.X', 'SPX', '$SPX']
        
        for symbol in symbols_to_try:
            print(f"Trying symbol: {symbol}")
            response = client.get_option_chain(
                symbol=symbol,
                from_date=target_date,
                to_date=target_date
            )
            
            if response.status_code == 200:
                print(f"Success with symbol: {symbol}")
                break
            else:
                print(f"Failed with symbol {symbol}: {response.status_code} - {response.text}")
        
        if response.status_code != 200:
            print(f"All symbols failed. Last response: {response.status_code}")
            print(f"API Response Text: {response.text}")
            raise Exception(f"Failed to fetch options data with all symbols: {response.status_code} - {response.text}")
            
        options_data = response.json()
        
        # Find closest delta matches
        calls_result = find_closest_delta_option(
            options_data, 'call', calls_delta_target
        )
        
        puts_result = find_closest_delta_option(
            options_data, 'put', puts_delta_target
        )
        
        return {
            'calls': calls_result,
            'puts': puts_result
        }
        
    except Exception as e:
        print(f"Error in get_0dte_options: {e}")
        import traceback
        traceback.print_exc()
        raise

def find_closest_delta_option(options_data, option_type, target_delta):
    """Find the option with delta closest to the target"""
    try:
        # Navigate the options data structure
        option_map = options_data.get('callExpDateMap' if option_type == 'call' else 'putExpDateMap', {})
        
        best_match = None
        best_delta_diff = float('inf')
        
        for exp_date, strikes in option_map.items():
            for strike, option_list in strikes.items():
                for option in option_list:
                    delta = option.get('delta', 0)
                    if delta is None:
                        continue
                        
                    delta_diff = abs(delta - target_delta)
                    
                    if delta_diff < best_delta_diff:
                        best_delta_diff = delta_diff
                        best_match = {
                            'strike': strike,
                            'price': option.get('last', option.get('mark', 0)),
                            'delta': delta,
                            'bid': option.get('bid', 0),
                            'ask': option.get('ask', 0),
                            'volume': option.get('totalVolume', 0)
                        }
        
        return best_match
        
    except Exception as e:
        print(f"Error finding closest delta option: {e}")
        return {
            'strike': 'Error',
            'price': 'Error',
            'delta': 'Error'
        }

@app.route('/health')
def health_check():
    try:
        # Test database connection
        db = SessionLocal()
        try:
            db.execute(text('SELECT 1'))
            return jsonify({'status': 'healthy', 'database': 'connected'})
        finally:
            db.close()
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

if __name__ == '__main__':
    # Development server
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
