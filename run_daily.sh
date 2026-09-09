#!/bin/bash
cd /opt/SMtracker

# Pastikan folder debug tersedia
mkdir -p /opt/SMtracker/debug

# Format penamaan log file
TIMESTAMP=$(date +"%Y%m%d")
LOGFILE="/opt/SMtracker/debug/daily_$TIMESTAMP.log"

echo "=== Memulai Backfill Harian SMtracker pada $(date) ===" >> $LOGFILE

# Deteksi Virtual Environment (jika venv SMtracker aktif)
if [ -f "venv/bin/python3" ]; then
    PYTHON_CMD="venv/bin/python3"
elif [ -f ".venv/bin/python3" ]; then
    PYTHON_CMD=".venv/bin/python3"
else
    PYTHON_CMD="python3"
fi

# Jalankan skrip (default: narik watchlist, mundur 3 hari)
$PYTHON_CMD backfill_daily.py --universe watchlist --days 3 >> $LOGFILE 2>&1

echo "=== Selesai pada $(date) ===" >> $LOGFILE

