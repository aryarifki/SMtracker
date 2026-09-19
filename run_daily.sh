#!/bin/bash
cd /opt/SMtracker

# Pastikan folder debug tersedia
mkdir -p /opt/SMtracker/debug

# Format penamaan log file
TIMESTAMP=$(date +"%Y%m%d")
LOG_BACKFILL="/opt/SMtracker/debug/backfill_$TIMESTAMP.log"
LOG_SCANNER="/opt/SMtracker/debug/scanner_$TIMESTAMP.log"
LOG_AUDITOR="/opt/SMtracker/debug/auditor_$TIMESTAMP.log"

# Deteksi Virtual Environment (jika venv SMtracker aktif)
if [ -f "venv/bin/python3" ]; then
    PYTHON_CMD="venv/bin/python3"
elif [ -f ".venv/bin/python3" ]; then
    PYTHON_CMD=".venv/bin/python3"
else
    PYTHON_CMD="python3"
fi

# ==========================================
# 1. BACKFILL DATA (IDX + STOCKBIT)
# ==========================================
echo "=== [1/3] Memulai Backfill Harian (Universe: ALL) pada $(date) ===" >> $LOG_BACKFILL
# PERBAIKAN: Universe diubah menjadi 'all' agar menarik 962 saham
 $PYTHON_CMD backfill_daily.py --universe all --days 3 >> $LOG_BACKFILL 2>&1
echo "=== Selesai Backfill pada $(date) ===" >> $LOG_BACKFILL

# ==========================================
# 2. SCANNER SINYAL (Signal v1.2)
# ==========================================
echo "=== [2/3] Memulai Scanner AI & Smart Money pada $(date) ===" >> $LOG_SCANNER
 $PYTHON_CMD scanner.py >> $LOG_SCANNER 2>&1
echo "=== Selesai Scanner pada $(date) ===" >> $LOG_SCANNER

# ==========================================
# 3. AUDITOR (Cek WIN/LOSS Sinyal Kemarin)
# ==========================================
echo "=== [3/3] Memulai Audit Sinyal pada $(date) ===" >> $LOG_AUDITOR
 $PYTHON_CMD auditor.py >> $LOG_AUDITOR 2>&1
echo "=== Selesai Audit pada $(date) ===" >> $LOG_AUDITOR
