# LoopTrader Web Interface

Professional AdminLTE-based web interface for LoopTrader Pro bot management.

![LoopTrader Web Dashboard](https://img.shields.io/badge/Admin-LTE_3.2-blue.svg)
![Flask](https://img.shields.io/badge/Flask-3.1.2-green.svg)
![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)

## Features

- **🤖 Bot Management**: Pause/resume, enable/disable bots with real-time controls
- **📊 Position Monitoring**: Track active and closed positions across all accounts
- **🛑 Trailing Stop Management**: Configure and monitor trailing stop orders
- **📈 Strategy Overview**: View and manage trading strategies
- **⚡ Real-time Dashboard**: Live statistics and system status monitoring
- **🎨 Professional UI**: AdminLTE-based responsive design
- **🔐 Secure Authentication**: Login system with session management
- **🔗 Database Integration**: Direct connection to existing LoopTrader Pro PostgreSQL

## Screenshots

The interface provides a professional admin dashboard with:
- Real-time bot status monitoring
- Interactive controls for bot management
- Position tracking with filtering capabilities
- Trailing stop configuration interface
- System health monitoring

## Quick Start

### Method 1: Using the Startup Script (Recommended)

```bash
cd /Users/objectis/Documents/Trader/looptrader-web
./start.sh
```

The application will be available at: http://localhost:3000

### Method 2: Manual Installation

```bash
# Install dependencies
poetry install --no-root

# Run the application
PORT=3000 poetry run python src/looptrader_web/app.py
```

### Method 3: Docker Deployment

```bash
# Build and run with Docker
docker-compose up -d

# Or build manually
docker build -t looptrader-web .
docker run -p 3000:5000 looptrader-web
```

## Demo Login

- **Username**: `admin`
- **Password**: `configureinyourenv`

## Configuration

The application uses environment variables for configuration:

```env
DATABASE_URL=postgresql://admin:yourlooptraderpassword@localhost:5432/looptrader
FLASK_DEBUG=True
SECRET_KEY=dev-secret-key-change-in-production
ADMIN_USERNAME=Admin login username
ADMIN_PASSWORD=Plain admin password (development only) OR
ADMIN_PASSWORD_HASH=Hashed password generated via `from werkzeug.security import generate_password_hash` (prefer in production). If hash is set it takes precedence over `ADMIN_PASSWORD`.
ADMIN_NAME=Admin
ADMIN_EMAIL=admin@looptrader.com
PORT=3000
ADMIN_NAME=Admin
ADMIN_EMAIL=admin@looptrader.com
PORT=3000
```

## Database Connection

The web interface connects directly to your existing LoopTrader Pro PostgreSQL database:
- **Host**: localhost
- **Port**: 5432
- **Database**: looptrader
- **Username**: admin
- **Password**: yourlooptraderpassword

Make sure the LoopTrader Pro PostgreSQL container is running before starting the web interface.

## API Endpoints

- `GET /` - Dashboard with statistics
- `GET /bots` - Bot management interface
- `GET /positions` - Position monitoring
- `GET /trailing-stops` - Trailing stop management
- `GET /strategies` - Strategy overview
- `POST /bots/{id}/toggle_pause` - Pause/resume bot
- `POST /bots/{id}/toggle_enabled` - Enable/disable bot
- `GET /api/health` - System health check
- `GET /api/stats` - Dashboard statistics

## Development

### Project Structure

```
looptrader-web/
├── src/looptrader_web/
│   ├── app.py                 # Main Flask application
│   ├── models/
│   │   ├── __init__.py
│   │   └── database.py        # Database models and connections
│   ├── templates/             # Jinja2 templates
│   │   ├── base.html         # Base AdminLTE template
│   │   ├── dashboard.html    # Main dashboard
│   │   ├── auth/             # Authentication templates
│   │   ├── bots/             # Bot management templates
│   │   ├── positions/        # Position monitoring templates
│   │   └── trailing_stops/   # Trailing stop templates
│   └── static/               # Static assets (CSS, JS, images)
├── pyproject.toml            # Poetry dependencies
├── Dockerfile               # Docker configuration
├── docker-compose.yml       # Docker Compose setup
├── start.sh                 # Easy startup script
└── README.md               # This file
```

### Adding New Features

1. Add routes to `src/looptrader_web/app.py`
2. Create corresponding templates in `src/looptrader_web/templates/`
3. Update database models in `src/looptrader_web/models/database.py` if needed
4. Add any new dependencies to `pyproject.toml`

### Running Tests

```bash
poetry run pytest
```

## Deployment

### Production Deployment

1. Update environment variables in `.env`
2. Set `FLASK_DEBUG=False`
3. Use a production WSGI server like Gunicorn:

```bash
poetry run gunicorn -w 4 -b 0.0.0.0:5000 src.looptrader_web.app:app
```

### Docker Production

```bash
# Build for production
docker build -t looptrader-web:latest .

# Run with production settings
docker run -d \
  -p 3000:5000 \
  -e FLASK_DEBUG=False \
  -e SECRET_KEY=your-production-secret \
  looptrader-web:latest
```

## Troubleshooting

### Common Issues

1. **Port 5000 already in use**: Use `PORT=3000` or disable AirPlay Receiver in macOS System Preferences
2. **Database connection failed**: Ensure LoopTrader Pro PostgreSQL container is running
3. **Permission denied**: Make sure `start.sh` is executable: `chmod +x start.sh`

### Logs

Application logs are displayed in the terminal when running in development mode. For production, configure proper logging.

## Security

- Change the default SECRET_KEY in production
- Use proper authentication system (current is demo only)
- Configure HTTPS for production deployment
- Restrict database access to authorized hosts only

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

This project is part of the LoopTrader Pro ecosystem.

## Support

For issues and questions:
- Check the troubleshooting section
- Review application logs
- Ensure all dependencies are properly installed
- Verify database connectivity

---

**Note**: This web interface is designed to work alongside your existing LoopTrader Pro installation and connects to the same PostgreSQL database for real-time data access.
