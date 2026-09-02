#!/bin/bash
echo "Memulai proses backup pada $(date)..."

# 1. Ekspor database terbaru ke dalam file SQL
su - postgres -c "pg_dump bandarmology" > /opt/SMtracker/backup_database.sql

# 2. Upload file tersebut ke Google Drive (ke dalam folder bernama 'SMtracker_Backup')
rclone copy /opt/SMtracker/backup_database.sql gdrive:SMtracker_Backup/

echo "Backup berhasil dikirim ke Google Drive!"
