"""
LoopTrader Web Interface with AdminLTE
A comprehensive web dashboard for managing LoopTrader Pro bots
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
import os
import requests
from datetime import datetime
from datetime import datetime, timedelta
from sqlalchemy import text

# Import our database models
from models.database import (
    get_db, Bot, Position, TrailingStopState, Order, BrokerageAccount,
    get_dashboard_stats, get_recent_positions, get_bots_by_account,
    pause_all_bots, resume_all_bots, close_all_positions, close_position_by_bot,
    SessionLocal, test_connection, update_bot, upsert_trailing_stop, delete_trailing_stop
)
from sqlalchemy.orm import joinedload
from sqlalchemy import text

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

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
    """Fetch current SPX spot price from Yahoo Finance API"""
    try:
        # Using Yahoo Finance API - free and reliable
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ESPX"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
                result = data['chart']['result'][0]
                if 'meta' in result and 'regularMarketPrice' in result['meta']:
                    meta = result['meta']
                    price = meta['regularMarketPrice']
                    change = meta.get('regularMarketChange', 0)
                    change_percent = meta.get('regularMarketChangePercent', 0)
                    market_state = meta.get('marketState', 'UNKNOWN')
                    previous_close = meta.get('previousClose', price)
                    
                    return {
                        'price': round(price, 2),
                        'change': round(change, 2),
                        'change_percent': round(change_percent * 100, 2),
                        'market_state': market_state,
                        'previous_close': round(previous_close, 2),
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')
                    }
    except Exception as e:
        print(f"Error fetching SPX price: {e}")
    
    # Return default values if API fails
    return {
        'price': 'N/A',
        'change': 'N/A',
        'change_percent': 'N/A',
        'market_state': 'UNKNOWN',
        'previous_close': 'N/A',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')
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
        return render_template('dashboard.html', stats=stats, recent_positions=recent_positions, db_status=db_status, spx_data=spx_data)
    except Exception as e:
        flash(f'Error loading dashboard: {str(e)}', 'danger')
        return render_template('dashboard.html', stats={}, recent_positions=[], db_status='error', spx_data={'price': 'N/A', 'change': 'N/A', 'change_percent': 'N/A', 'market_state': 'UNKNOWN', 'previous_close': 'N/A', 'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')})

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

@app.route('/bots/<int:bot_id>/close-position', methods=['POST'])
@login_required
def close_bot_position(bot_id):
    try:
        success = close_position_by_bot(bot_id)
        if success:
            return jsonify({'success': True, 'message': 'Position closed successfully'})
        else:
            return jsonify({'success': False, 'message': 'No active position found for this bot'})
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
        trailing_percentage = request.form.get('trailing_percentage', type=float)
        if activation_threshold is None or trailing_percentage is None:
            raise ValueError("Activation threshold and trailing percentage are required")
        # Debug print to container logs for verification
        print(f"[TrailingStopUpdate] bot_id={bot_id} activation={activation_threshold} trailing={trailing_percentage}")
        # Don't pass is_active parameter - let the database function handle activation state
        ok, msg = upsert_trailing_stop(bot_id, activation_threshold, trailing_percentage)
        if ok:
            flash('Trailing stop saved', 'success')
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
            return render_template('accounts/list.html', accounts=accounts)
        finally:
            db.close()
    except Exception as e:
        flash(f'Error loading accounts: {str(e)}', 'danger')
        return render_template('accounts/list.html', accounts=[])

# Position management routes
@app.route('/positions')
@login_required
def positions():
    try:
        db = SessionLocal()
        try:
            # Get filter parameters
            account_filter = request.args.get('account')
            status_filter = request.args.get('status')
            active_only = request.args.get('active_only')  # Add support for active_only param
            
            query = db.query(Position).order_by(Position.opened_datetime.desc())
            
            if account_filter:
                query = query.filter(Position.account_id == account_filter)
            
            if status_filter == 'active' or active_only == 'true':
                query = query.filter(Position.active == True)
            elif status_filter == 'closed':
                query = query.filter(Position.active == False)
            
            positions = query.all()
            accounts = db.query(BrokerageAccount).all()
            
            # Debug: Print to logs to see what we're getting
            print(f"DEBUG: Found {len(positions)} positions")
            if positions:
                print(f"DEBUG: First position: ID={positions[0].id}, Active={positions[0].active}")
                # Test template properties that might be causing issues
                try:
                    print(f"DEBUG: First position status_text: {positions[0].status_text}")
                    print(f"DEBUG: First position status_badge_class: {positions[0].status_badge_class}")
                    print(f"DEBUG: First position duration_text: {positions[0].duration_text}")
                except Exception as e:
                    print(f"DEBUG: Error accessing position properties: {e}")
            print(f"DEBUG: Active only filter: {active_only}")
            
            # Pass the active_only flag to template for button styling
            return render_template('positions/list.html', positions=positions, accounts=accounts, active_only=(active_only == 'true'))
        finally:
            db.close()
    except Exception as e:
        flash(f'Error loading positions: {str(e)}', 'danger')
        return render_template('positions/list.html', positions=[], accounts=[], active_only=False)

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
                
                if not selected_bots:
                    flash('Please select at least one bot', 'warning')
                elif action == 'add':
                    # Handle bulk trailing stop creation
                    activation_threshold = request.form.get('activation_threshold', type=float)
                    trailing_percentage = request.form.get('trailing_percentage', type=float)
                    
                    if activation_threshold is None or trailing_percentage is None:
                        flash('Activation threshold and trailing percentage are required', 'danger')
                    else:
                        success_count = 0
                        error_count = 0
                        
                        for bot_id in selected_bots:
                            try:
                                # Don't pass is_active parameter - let the database function handle activation state
                                ok, msg = upsert_trailing_stop(int(bot_id), activation_threshold, trailing_percentage)
                                if ok:
                                    success_count += 1
                                else:
                                    error_count += 1
                                    print(f"Failed to add trailing stop for bot {bot_id}: {msg}")
                            except Exception as e:
                                error_count += 1
                                print(f"Error adding trailing stop for bot {bot_id}: {e}")
                        
                        if success_count > 0:
                            flash(f'Successfully added/updated trailing stops for {success_count} bot(s)', 'success')
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
        trailing_percentage = request.form.get('trailing_percentage', type=float)
        
        if not bot_id or activation_threshold is None or trailing_percentage is None:
            return jsonify({'success': False, 'message': 'Missing required parameters'})
        
        # Don't pass is_active parameter - let the database function handle activation state
        ok, msg = upsert_trailing_stop(bot_id, activation_threshold, trailing_percentage)
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
