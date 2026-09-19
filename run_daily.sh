#!/bin/bash
cd /root/SMtracker

# Pastikan folder debug tersedia
mkdir -p /root/SMtracker/debug

# Format penamaan log file
TIMESTAMP=$(date +"%Y%m%d")
LOG_BACKFILL="/root/SMtracker/debug/backfill_$TIMESTAMP.log"
LOG_ETL_FLOW="/root/SMtracker/debug/etl_flow_$TIMESTAMP.log"
LOG_SCANNER="/root/SMtracker/debug/scanner_$TIMESTAMP.log"
LOG_AUDITOR="/root/SMtracker/debug/auditor_$TIMESTAMP.log"

# Deteksi Virtual Environment
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
echo "=== [1/4] Memulai Backfill Harian (Universe: ALL) pada $(date) ===" >> $LOG_BACKFILL
 $PYTHON_CMD backfill_daily.py --universe all --days 3 >> $LOG_BACKFILL 2>&1
echo "=== Selesai Backfill pada $(date) ===" >> $LOG_BACKFILL

# ==========================================
# 2. ETL FOREIGN FLOW (HMM, Network, Z-Score)
# ==========================================
echo "=== [2/4] Memulai ETL Foreign Flow Analytics pada $(date) ===" >> $LOG_ETL_FLOW
 $PYTHON_CMD etl_foreign_flow.py --universe all --window 20 30 60 90 180 >> $LOG_ETL_FLOW 2>&1
echo "=== Selesai ETL Flow pada $(date) ===" >> $LOG_ETL_FLOW

# ==========================================
# 3. SCANNER SINYAL (Signal v1.2)
# ==========================================
echo "=== [3/4] Memulai Scanner AI & Smart Money pada $(date) ===" >> $LOG_SCANNER
 $PYTHON_CMD scanner.py >> $LOG_SCANNER 2>&1
echo "=== Selesai Scanner pada $(date) ===" >> $LOG_SCANNER

# ==========================================
# 4. AUDITOR (Cek WIN/LOSS Sinyal Kemarin)
# ==========================================
echo "=== [4/4] Memulai Audit Sinyal pada $(date) ===" >> $LOG_AUDITOR
 $PYTHON_CMD auditor.py >> $LOG_AUDITOR 2>&1
echo "=== Selesai Audit pada $(date) ===" >> $LOG_AUDITOR
