FROM python:3.12-slim

# Railway sets PORT env var; app must bind to 0.0.0.0:$PORT
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy the entire Atropos repo
COPY . /app/

# Make the atropos script executable
RUN chmod +x /app/atropos

# Create the data directory (Railway volume mounts here)
RUN mkdir -p /data/.atropos

# Expose the dashboard port
EXPOSE 8787

# Start: init config + run dashboard
CMD ["python", "atropos", "dashboard", "--port", "8787"]
