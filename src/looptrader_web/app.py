"""
LoopTrader Web Interface with AdminLTE
A comprehensive web dashboard for managing LoopTrader Pro bots
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
import os
from datetime import datetime, timedelta
from sqlalchemy import text

# Import our database models
from models.database import (
    get_db, Bot, Position, TrailingStopState, Order, BrokerageAccount,
    get_dashboard_stats, get_recent_positions, get_bots_by_account,
    pause_all_bots, resume_all_bots, close_all_positions, close_position_by_bot,
    SessionLocal, test_connection, update_bot, upsert_trailing_stop, delete_trailing_stop
)

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
        return render_template('dashboard.html', stats=stats, recent_positions=recent_positions, db_status=db_status)
    except Exception as e:
        flash(f'Error loading dashboard: {str(e)}', 'danger')
        return render_template('dashboard.html', stats={}, recent_positions=[])

# Bot management routes
@app.route('/bots')
@login_required
def bots():
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
        return render_template('bots/list.html', bots_by_account={}, total_bots=0, active_bots=0, paused_bots=0, total_accounts=0, current_filter=None,
                               all_total_bots=0, all_active_bots=0, all_inactive_bots=0)

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
        # Support various truthy values; if field absent treat as False
        values = request.form.getlist('is_active')  # may contain one or more values
        truthy = {'on', 'true', '1', 'yes', 'y'}
        is_active = any(v.lower() in truthy for v in values)
        if activation_threshold is None or trailing_percentage is None:
            raise ValueError("Activation threshold and trailing percentage are required")
        # Debug print to container logs for verification
        print(f"[TrailingStopUpdate] bot_id={bot_id} activation={activation_threshold} trailing={trailing_percentage} values={values} parsed_active={is_active}")
        ok, msg = upsert_trailing_stop(bot_id, activation_threshold, trailing_percentage, is_active=is_active)
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
            
            query = db.query(Position).order_by(Position.opened_datetime.desc())
            
            if account_filter:
                query = query.filter(Position.account_id == account_filter)
            
            if status_filter == 'active':
                query = query.filter(Position.active == True)
            elif status_filter == 'closed':
                query = query.filter(Position.active == False)
            
            positions = query.all()
            accounts = db.query(BrokerageAccount).all()
            
            return render_template('positions/list.html', positions=positions, accounts=accounts)
        finally:
            db.close()
    except Exception as e:
        flash(f'Error loading positions: {str(e)}', 'danger')
        return render_template('positions/list.html', positions=[], accounts=[])

@app.route('/trailing')
@login_required
def trailing_stops():
    try:
        db = SessionLocal()
        try:
            trailing_stops = db.query(TrailingStopState).all()
            # Also need bots collection for stats panel in template
            bots = db.query(Bot).all()
            return render_template('trailing_stops/list.html', trailing_stops=trailing_stops, bots=bots)
        finally:
            db.close()
    except Exception as e:
        flash(f'Error loading trailing stops: {str(e)}', 'danger')
        return render_template('trailing_stops/list.html', trailing_stops=[], bots=[])

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

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('errors/500.html'), 500

# Health check endpoint
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
