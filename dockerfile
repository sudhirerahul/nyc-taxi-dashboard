# dockerfile
FROM python:3.11-slim

# System deps for geopandas / pyproj / shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential g++ gcc \
    libspatialindex-dev \
    libgeos-dev \
    libproj-dev proj-data proj-bin \
    gdal-bin \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY backend3.py ./
# If you have static files/templates, copy those too
# COPY templates/ templates/
# COPY static/ static/

# Render provides $PORT; Flask must bind to 0.0.0.0:$PORT
ENV PORT=8000
ENV DB_PATH=/data/taxi_data.db

EXPOSE 8000

CMD ["python", "backend3.py"]
