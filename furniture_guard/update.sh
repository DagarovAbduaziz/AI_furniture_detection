#!/bin/bash
# ══════════════════════════════════════════════
#  Mebel detektor yangilash skripti
#  Ishlatish: ./update.sh
# ══════════════════════════════════════════════

set -e

echo "🔄 Yangilash boshlandi..."

# 1. Oxirgi kodni olish
git pull origin main

# 2. Kutubxonalarni yangilash (kerak bo'lsa)
pip3 install -r requirements.txt --quiet 2>/dev/null || true

# 3. Xizmatni qayta ishga tushirish
echo "🔄 Xizmat qayta ishga tushirilmoqda..."
sudo systemctl restart mebel-detektor

# 4. Holat tekshirish
sleep 3
sudo systemctl status mebel-detektor --no-pager -l

echo ""
echo "✅ Yangilash tugadi!"
echo "   Log ko'rish: journalctl -u mebel-detektor -f"
