#!/usr/bin/env python3
"""Skrip untuk menarik data Fundamental, Corporate Actions, dan Governance dari IDX."""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from idx_bandarmology import idx_api, storage

def main():
    parser = argparse.ArgumentParser(description="Backfill IDX Fundamental & Governance Data")
    parser.add_argument("--details", action="store_true", help="Ambil Company Details (Direksi, Komisaris, Pemegang Saham)")
    parser.add_argument("--ratios", action="store_true", help="Ambil Financial Ratios")
    parser.add_argument("--actions", action="store_true", help="Ambil Corporate Actions")
    parser.add_argument("--concurrency", type=int, default=5, help="Jumlah concurrent request untuk details")
    
    args = parser.parse_args()
    
    storage.init_db()
    
    if args.details:
        print("=" * 60)
        print("🏢 Mengambil Company Details (Governance)...")
        print("=" * 60)
        idx_api.ingest_all_company_details(concurrency=args.concurrency)
        
    if args.ratios:
        print("=" * 60)
        print("📊 Mengambil Financial Ratios...")
        print("=" * 60)
        # Ambil data tahun 2024 kuartal 4 (sesuaikan jika perlu)
        idx_api.ingest_financial_ratios(year=2024, quarter=4)
        
    if args.actions:
        print("=" * 60)
        print("📝 Mengambil Corporate Actions...")
        print("=" * 60)
        idx_api.ingest_corporate_actions()
        
    if not (args.details or args.ratios or args.actions):
        print("Tidak ada argumen yang diberikan. Mengambil SEMUA data fundamental...")
        idx_api.ingest_all_company_details(concurrency=args.concurrency)
        idx_api.ingest_financial_ratios(year=2024, quarter=4)
        idx_api.ingest_corporate_actions()

if __name__ == "__main__":
    main()
