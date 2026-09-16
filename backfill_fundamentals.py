#!/usr/bin/env python3
"""Skrip untuk menarik data Fundamental, Corporate Actions, dan Governance dari IDX."""

import argparse
import sys
import datetime
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
    parser.add_argument("--year", type=int, default=datetime.date.today().year, help="Tahun laporan keuangan")
    parser.add_argument("--quarter", type=int, default=4, help="Kuartal laporan keuangan")
    parser.add_argument("--all-years", action="store_true", help="Ambil Financial Ratios untuk 3 tahun terakhir + tahun berjalan")
    parser.add_argument("--concurrency", type=int, default=5, help="Jumlah concurrent request untuk details")
    
    args = parser.parse_args()
    
    storage.init_db()
    current_year = datetime.date.today().year
    
    if args.details:
        print("=" * 60)
        print("🏢 Mengambil Company Details (Governance)...")
        print("=" * 60)
        idx_api.ingest_all_company_details(concurrency=args.concurrency)
        
    if args.ratios:
        if args.all_years:
            years_to_fetch = [current_year - 3, current_year - 2, current_year - 1, current_year]
            for y in years_to_fetch:
                print("=" * 60)
                print(f"📊 Mengambil Financial Ratios (Year: {y}, Quarter: 4)...")
                print("=" * 60)
                idx_api.ingest_financial_ratios(year=y, quarter=4)
        else:
            print("=" * 60)
            print(f"📊 Mengambil Financial Ratios (Year: {args.year}, Quarter: {args.quarter})...")
            print("=" * 60)
            idx_api.ingest_financial_ratios(year=args.year, quarter=args.quarter)
        
    if args.actions:
        print("=" * 60)
        print("📝 Mengambil Corporate Actions...")
        print("=" * 60)
        idx_api.ingest_corporate_actions()
        
    if not (args.details or args.ratios or args.actions):
        print("Tidak ada argumen yang diberikan. Mengambil SEMUA data fundamental...")
        idx_api.ingest_all_company_details(concurrency=args.concurrency)
        years_to_fetch = [current_year - 3, current_year - 2, current_year - 1, current_year]
        for y in years_to_fetch:
            print("=" * 60)
            print(f"📊 Mengambil Financial Ratios (Year: {y}, Quarter: 4)...")
            print("=" * 60)
            idx_api.ingest_financial_ratios(year=y, quarter=4)
            
        print("=" * 60)
        print("📝 Mengambil Corporate Actions...")
        print("=" * 60)
        idx_api.ingest_corporate_actions()

if __name__ == "__main__":
    main()
