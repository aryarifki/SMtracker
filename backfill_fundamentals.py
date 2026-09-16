#!/usr/bin/env python3
"""Skrip untuk menarik data Fundamental, Corporate Actions, dan Governance dari IDX (Super Safe)."""

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
    parser = argparse.ArgumentParser(description="Backfill IDX Fundamental & Governance Data (Super Safe Mode)")
    parser.add_argument("--details", action="store_true", help="Ambil Company Details (Direksi, Komisaris, Pemegang Saham)")
    parser.add_argument("--ratios", action="store_true", help="Ambil Financial Ratios")
    parser.add_argument("--actions", action="store_true", help="Ambil Corporate Actions")
    parser.add_argument("--year", type=int, default=None, help="Tahun laporan keuangan (contoh: 2024). Jika tidak diisi, akan ditanyakan.")
    parser.add_argument("--quarter", type=int, default=4, help="Kuartal laporan keuangan (default: 4)")
    
    args = parser.parse_args()
    
    storage.init_db()
    current_year = datetime.date.today().year
    
    if args.details:
        print("=" * 60)
        print("🏢 Mengambil Company Details (Governance)...")
        print("=" * 60)
        idx_api.ingest_all_company_details()
        
    if args.ratios:
        # Jika tahun tidak diisi via argumen, tanyakan secara interaktif
        year_to_fetch = args.year
        if year_to_fetch is None:
            try:
                input_year = input(f"Masukkan tahun laporan keuangan (contoh: {current_year}): ").strip()
                year_to_fetch = int(input_year) if input_year else current_year
            except ValueError:
                print("❌ Input tahun tidak valid. Membatalkan pengambilan ratios.")
                return
        
        print("=" * 60)
        print(f"📊 Mengambil Financial Ratios (Year: {year_to_fetch}, Quarter: {args.quarter})...")
        print("=" * 60)
        idx_api.ingest_financial_ratios(year=year_to_fetch, quarter=args.quarter)
        
    if args.actions:
        print("=" * 60)
        print("📝 Mengambil Corporate Actions...")
        print("=" * 60)
        idx_api.ingest_corporate_actions()
        
    if not (args.details or args.ratios or args.actions):
        print("Tidak ada argumen yang diberikan. Mengambil SEMUA data fundamental...")
        print("=" * 60)
        print("🏢 Mengambil Company Details (Governance)...")
        print("=" * 60)
        idx_api.ingest_all_company_details()
        
        # Input tahun interaktif jika dijalankan tanpa argumen
        try:
            input_year = input(f"Masukkan tahun laporan keuangan (contoh: {current_year}): ").strip()
            year_to_fetch = int(input_year) if input_year else current_year
        except ValueError:
            print("❌ Input tahun tidak valid. Membatalkan pengambilan ratios.")
            return
            
        print("=" * 60)
        print(f"📊 Mengambil Financial Ratios (Year: {year_to_fetch}, Quarter: 4)...")
        print("=" * 60)
        idx_api.ingest_financial_ratios(year=year_to_fetch, quarter=4)
            
        print("=" * 60)
        print("📝 Mengambil Corporate Actions...")
        print("=" * 60)
        idx_api.ingest_corporate_actions()

if __name__ == "__main__":
    main()
