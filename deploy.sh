#!/bin/bash

echo "🚕 NYC Taxi Analytics Dashboard - 2024 ONLY"
echo "============================================="

# Check available memory
echo "💾 Checking system memory..."
if command -v free &> /dev/null; then
    free -h
elif command -v vm_stat &> /dev/null; then
    vm_stat | head -5
fi

# Clean up any existing database
if [ -f "taxi_data.db" ]; then
    echo "🗑️  Removing old database..."
    rm taxi_data.db
fi

# Check for 2024 files specifically
echo "📂 Checking for 2024 parquet files..."
if ls /Users/sudhirerahul/Desktop/Python/fhvhv_2023_2024/*2024*.parquet 1> /dev/null 2>&1; then
    echo "✅ Found 2024 parquet files:"
    ls -la /Users/sudhirerahul/Desktop/Python/fhvhv_2023_2024/*2024*.parquet | wc -l | awk '{print "   " $1 " files found"}'
else
    echo "❌ No 2024 parquet files found!"
    echo "   Looking for files with '2024' in the name..."
    echo "   Available files:"
    ls /Users/sudhirerahul/Desktop/Python/fhvhv_2023_2024/*.parquet 2>/dev/null | head -10
    echo ""
fi

# Create virtual environment if needed
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "🔄 Activating virtual environment..."
source venv/bin/activate

# Install packages if needed
echo "📥 Checking/installing packages..."
pip install Flask==3.0.0 Flask-CORS==4.0.0 pandas pyarrow --quiet

# Set Python memory optimization
export PYTHONHASHSEED=0
export PYTHONOPTIMIZE=1

echo "🚀 Starting NYC Taxi Analytics Dashboard..."
echo "⚠️  Processing only 2024 files to avoid memory issues"
echo "🌐 Open your browser to: http://localhost:8000"
echo "============================================="

# Run with memory optimizations
python -O backend3.py