FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN pip install poetry

# Copy Poetry files
COPY pyproject.toml poetry.lock* ./

# Configure Poetry
RUN poetry config virtualenvs.create false

# Install dependencies
RUN poetry install --no-root --only=main

# Copy application code
COPY src/ ./src/
COPY .env* ./

# Expose port
EXPOSE 5000

# Set environment variables
ENV FLASK_APP=src/looptrader_web/app.py
ENV FLASK_DEBUG=False
ENV PORT=5000

# Run the application
CMD ["poetry", "run", "python", "src/looptrader_web/app.py"]
