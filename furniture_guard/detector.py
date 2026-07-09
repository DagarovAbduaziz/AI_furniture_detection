"""
Mebel Sexi Chiqish Nazorat Tizimi — Production versiya
=======================================================
✅ Internet yo'q bo'lsa rasmlarni saqlaydi, keyin yuboradi
✅ Kamera uzilib qolsa avtomatik qayta ulanadi
✅ Svet o'chib yonsa avtomatik ishga tushadi (systemd bilan)
✅ Barcha xatolar logga yoziladi

Ishga tushirish:
    python detector.py
"""

import cv2
import time
import json
import requests
import threading
import logging
from datetime import datetime
from pathlib import Path
from collections import deque

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False

# ══════════════════════════════════════════════
#  SOZLAMALAR
# ══════════════════════════════════════════════
CONFIG = {
    # Kamera
    "camera_source": "rtsp://admin:1FaSa1988@192.168.8.5:554/Streaming/Channels/101",

    # Telegram
    "telegram_token":   "8794822676:AAFWS7qDJ1Kj4QbqESxSE60hJhcSJ5EPWKc",
    "telegram_chat_id": "8441789662",

    # Model — o'z modelingiz bo'lsa shu yo'lni yozing
    "model_path": "mebel_model.pt",

    # Chiqish zonasi — None = avtomatik (o'ng yarmi)
    "exit_zone": (8, 485, 1125, 1438),

    "furniture_classes": {
    0 : "divan",
    1 : 'kreslo',
    2 : 'pufik',
    3 : 'burchak'
    },

    # Sezgirlik
    "confidence_threshold": 0.45,

    # Bir xil obekt uchun qayta xabar yubormaslik (soniya)
    "alert_cooldown_seconds": 30,

    # Kadrlar orasidagi kutish (ms)
    "frame_delay_ms": 100,

    # Offline navbat sozlamalari
    "offline_save_dir":    "offline_queue",   # internet yo'qda saqlanadigan joy
    "max_offline_queue":   200,               # maksimal saqlash (rasm soni)
    "retry_interval_sec":  60,                # internetni tekshirish oralig'i (soniya)

    # Log fayli
    "log_file": "detector.log",

    # Ekran ko'rsatish (server da False qiling)
    "show_window": True,
}

# ══════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════
def setup_logging(log_file: str):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ]
    )

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════
#  OFFLINE NAVBAT — internet yo'qda saqlash
# ══════════════════════════════════════════════
class OfflineQueue:
    """
    Internet yo'q bo'lganda rasmlarni diskka saqlaydi.
    Internet kelganda avtomatik yuboradi.
    """
    def __init__(self, save_dir: str, max_size: int):
        self.dir = Path(save_dir)
        self.dir.mkdir(exist_ok=True)
        self.max_size = max_size
        self._lock = threading.Lock()

    def push(self, img_bytes: bytes, caption: str):
        """Rasmni navbatga qo'shish"""
        with self._lock:
            # Navbat to'lib qolmasin
            existing = sorted(self.dir.glob("*.jpg"))
            if len(existing) >= self.max_size:
                # Eng eskisini o'chirish
                existing[0].unlink(missing_ok=True)
                meta = existing[0].with_suffix(".json")
                meta.unlink(missing_ok=True)
                log.warning(f"Offline navbat to'ldi, eng eski o'chirildi")

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            img_path  = self.dir / f"{ts}.jpg"
            meta_path = self.dir / f"{ts}.json"

            img_path.write_bytes(img_bytes)
            meta_path.write_text(
                json.dumps({"caption": caption, "time": ts}, ensure_ascii=False),
                encoding="utf-8"
            )
            log.info(f"📥 Offline saqlandi: {img_path.name}")

    def pending(self) -> list[tuple[Path, Path]]:
        """Yuborilmagan rasmlar ro'yxati"""
        with self._lock:
            pairs = []
            for img in sorted(self.dir.glob("*.jpg")):
                meta = img.with_suffix(".json")
                if meta.exists():
                    pairs.append((img, meta))
            return pairs

    def remove(self, img_path: Path):
        """Muvaffaqiyatli yuborilgandan keyin o'chirish"""
        with self._lock:
            img_path.unlink(missing_ok=True)
            img_path.with_suffix(".json").unlink(missing_ok=True)

    def count(self) -> int:
        with self._lock:
            return len(list(self.dir.glob("*.jpg")))


# ══════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════
class TelegramAlert:
    def __init__(self, token: str, chat_id: str, queue: OfflineQueue, retry_interval: int):
        self.token    = token
        self.chat_id  = chat_id
        self.queue    = queue
        self.base_url = f"https://api.telegram.org/bot8794822676:AAFWS7qDJ1Kj4QbqESxSE60hJhcSJ5EPWKc"
        self._last_alerts: dict[int, float] = {}
        self._lock    = threading.Lock()
        self._online  = False

        # Orqa fonda navbatni yuboruvchi thread
        t = threading.Thread(target=self._retry_loop, args=(retry_interval,), daemon=True)
        t.start()

    def _is_configured(self) -> bool:
        return (self.token   != "8794822676:AAFWS7qDJ1Kj4QbqESxSE60hJhcSJ5EPWKc" and
                self.chat_id != "8441789662")

    def _check_internet(self) -> bool:
        """Internetni tekshirish"""
        try:
            requests.get("https://api.telegram.org", timeout=5)
            return True
        except Exception:
            return False

    def _send_photo_now(self, img_bytes: bytes, caption: str) -> bool:
        """To'g'ridan Telegramga yuborish"""
        if not self._is_configured():
            log.info(f"[DEMO] {caption[:60]}")
            return True
        try:
            resp = requests.post(
                f"{self.base_url}/sendPhoto",
                data={"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("alert.jpg", img_bytes, "image/jpeg")},
                timeout=15
            )
            return resp.status_code == 200
        except Exception as e:
            log.warning(f"Telegram xato: {e}")
            return False

    def _retry_loop(self, interval: int):
        """
        Orqa fonda doim ishlaydi.
        Internet kelganda offline navbatdagi rasmlarni yuboradi.
        """
        while True:
            time.sleep(interval)
            pending = self.queue.pending()
            if not pending:
                continue

            if not self._check_internet():
                log.info(f"📵 Internet yo'q — {len(pending)} ta rasm kutmoqda")
                self._online = False
                continue

            self._online = True
            log.info(f"🌐 Internet bor — {len(pending)} ta navbatdagi rasm yuborilmoqda")

            for img_path, meta_path in pending:
                try:
                    meta      = json.loads(meta_path.read_text(encoding="utf-8"))
                    img_bytes = img_path.read_bytes()
                    caption   = meta.get("caption", "Mebel ogohlantirish")
                    caption  += f"\n⏰ <i>Kechikib yuborildi: {meta.get('time','')}</i>"

                    if self._send_photo_now(img_bytes, caption):
                        self.queue.remove(img_path)
                        log.info(f"✅ Navbatdan yuborildi: {img_path.name}")
                        time.sleep(1)   # Telegram rate limit
                    else:
                        log.warning(f"❌ Yuborishda xato: {img_path.name}")
                        break
                except Exception as e:
                    log.error(f"Navbat xatosi: {e}")
                    break

    def can_send(self, obj_id: int, cooldown: int) -> bool:
        with self._lock:
            now  = time.time()
            last = self._last_alerts.get(obj_id, 0)
            if now - last >= cooldown:
                self._last_alerts[obj_id] = now
                return True
            return False

    def send_alert(self, img_bytes: bytes, caption: str):
        """
        Internet bo'lsa darhol yuboradi.
        Yo'q bo'lsa offline navbatga qo'yadi.
        """
        if not self._is_configured():
            log.info(f"[DEMO] {caption[:60]}")
            return

        # Avval internet bor yoki yo'qligini tekshiramiz
        if self._check_internet():
            self._online = True
            ok = self._send_photo_now(img_bytes, caption)
            if ok:
                log.info("✅ Telegram xabari yuborildi")
                return
            # Yuborishda xato — navbatga
            log.warning("Yuborishda xato — offline navbatga qo'shildi")

        # Internet yo'q yoki xato — saqlash
        self._online = False
        self.queue.push(img_bytes, caption)
        log.info(f"📵 Internet yo'q — navbatda: {self.queue.count()} ta rasm")

    def send_text(self, text: str):
        if not self._is_configured():
            return
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception:
            pass


# ══════════════════════════════════════════════
#  YORDAMCHI FUNKSIYALAR
# ══════════════════════════════════════════════
def box_in_exit_zone(box_xyxy, zone_xyxy, threshold=0.3) -> bool:
    bx1, by1, bx2, by2 = box_xyxy
    zx1, zy1, zx2, zy2 = zone_xyxy
    ix1, iy1 = max(bx1, zx1), max(by1, zy1)
    ix2, iy2 = min(bx2, zx2), min(by2, zy2)
    if ix2 <= ix1 or iy2 <= iy1:
        return False
    intersection = (ix2 - ix1) * (iy2 - iy1)
    box_area = max((bx2-bx1) * (by2-by1), 1)
    return (intersection / box_area) >= threshold


# ══════════════════════════════════════════════
#  ASOSIY DETEKTOR
# ══════════════════════════════════════════════
class FurnitureGuard:
    def __init__(self, config: dict):
        self.cfg = config

        # Offline navbat va Telegram
        self.queue = OfflineQueue(
            config["offline_save_dir"],
            config["max_offline_queue"]
        )
        self.telegram = TelegramAlert(
            config["telegram_token"],
            config["telegram_chat_id"],
            self.queue,
            config["retry_interval_sec"]
        )

        Path("alerts").mkdir(exist_ok=True)

        # Model yuklash
        model_path = Path(config["model_path"])
        if YOLO_AVAILABLE:
            self.model = YOLO("mebel_model.pt")  # nano — eng tez, kichik
            print("✅ Model yuklandi")
            log.info(f"✅ Sinf nomlari: {list(self.model.names.values())}")
        else:
            log.error("ultralytics o'rnatilmagan")
            self.model = None

    def _open_camera(self, src):
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            return None, None
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        frame = None
        for _ in range(15):
            ret, f = cap.read()
            if ret and f is not None:
                frame = f
                break
            time.sleep(0.1)
        if frame is None:
            cap.release()
            return None, None
        return cap, frame

    def process_frame(self, frame) -> list:
        if self.model is None:
            return []
        results = self.model(
            frame,
            conf=self.cfg["confidence_threshold"],
            classes=list(self.cfg["furniture_classes"].keys()),
            verbose=False
        )
        detections = []
        for result in results:
            for box in result.boxes:
                cls_id = int(box.cls[0])
                if cls_id not in self.cfg["furniture_classes"]:
                    continue

                xyxy = box.xyxy[0].tolist()
                conf = float(box.conf[0])
                in_zone = box_in_exit_zone(xyxy, self.cfg["exit_zone"])

                detections.append({
                    "label": self.cfg["furniture_classes"][cls_id],
                    "class_id": cls_id,
                    "confidence": conf,
                    "box": xyxy,
                    "in_exit_zone": in_zone,
                })
        return detections

    def draw_ui(self, frame, detections):
        h, w = frame.shape[:2]
        zone = self.cfg["exit_zone"]

        cv2.rectangle(frame, (zone[0], zone[1]), (zone[2], zone[3]), (0, 100, 255), 2)
        cv2.putText(frame, "CHIQISH ZONASI", (zone[0]+5, zone[1]+20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 100, 255), 1)

        for det in detections:
            x1, y1, x2, y2 = [int(v) for v in det["box"]]
            color = (0, 50, 220) if det["in_exit_zone"] else (50, 200, 50)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            tag = f"{det['label']} {det['confidence']:.0%}"
            if det["in_exit_zone"]:
                tag += " ⚠ CHIQYAPTI"
            cv2.putText(frame, tag, (x1, y1-5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1)

        # Holat paneli
        online  = self.telegram._online
        pending = self.queue.count()
        ts      = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")

        status_color = (0, 200, 80) if online else (0, 100, 255)
        status_txt   = "🌐 ONLINE" if online else f"📵 OFFLINE ({pending} navbatda)"
        cv2.putText(frame, ts,         (10, h-30), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200,200,200), 1)
        cv2.putText(frame, status_txt, (10, h-10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, status_color,  1)
        cv2.putText(frame, f"Topilgan: {len(detections)}", (10, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,255,255), 1)
        return frame

    def handle_alerts(self, frame, detections):
        for det in detections:
            if not det["in_exit_zone"]:
                continue
            if not self.telegram.can_send(det["class_id"], self.cfg["alert_cooldown_seconds"]):
                continue

            now    = datetime.now()
            ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
            caption = (
                f"🚨 <b>MEBEL SEXI OGOHLANTIRISH!</b>\n\n"
                f"📦 Mebel: <b>{det['label']}</b>\n"
                f"📊 Ishonch: {det['confidence']:.0%}\n"
                f"🕐 Vaqt: {ts_str}"
            )

            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            img_bytes = buf.tobytes()

            # Diskka ham saqlash
            fname = now.strftime(f"alerts/%Y%m%d_%H%M%S_{det['label']}.jpg")
            Path(fname).write_bytes(img_bytes)

            # Yuborish yoki navbatga qo'yish
            threading.Thread(
                target=self.telegram.send_alert,
                args=(img_bytes, caption),
                daemon=True
            ).start()

            log.info(f"🚨 OGOHLANTIRISH: {det['label']} chiqish zonasida!")

    def run(self):
        src      = self.cfg["camera_source"]
        show_win = self.cfg["show_window"]
        win_name = "Mebel Nazorat  |  q=chiqish"

        log.info(f"🔄 Kameraga ulanmoqda: {src}")

        # Ulanish urinishlari
        cap, frame = None, None
        for attempt in range(1, 6):
            cap, frame = self._open_camera(src)
            if cap is not None:
                break
            log.warning(f"Ulanib bo'lmadi ({attempt}/5), 5s kutilmoqda...")
            time.sleep(5)

        if cap is None:
            log.error(f"❌ Kameraga ulanib bo'lmadi: {src}")
            return

        h, w = frame.shape[:2]

        if self.cfg["exit_zone"] is None:
            self.cfg["exit_zone"] = (w // 2, 0, w, h)
            log.info(f"exit_zone avtomatik: {self.cfg['exit_zone']}")

        if show_win:
            cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(win_name, w, h)

        log.info(f"🎥 Kamera: {w}x{h}")
        log.info(f"🚪 Chiqish zonasi: {self.cfg['exit_zone']}")

        self.telegram.send_text(
            "🟢 <b>Mebel nazorat tizimi ishga tushdi</b>\n"
            f"Vaqt: {datetime.now().strftime('%H:%M:%S')}"
        )

        frame_count      = 0
        fps_history      = deque(maxlen=30)
        t_prev           = time.time()
        detections       = []
        consecutive_fail = 0
        MAX_FAIL         = 5

        log.info("▶  Kuzatish boshlandi")

        while True:
            ret, new_frame = cap.read()

            if not ret or new_frame is None:
                consecutive_fail += 1
                if consecutive_fail >= MAX_FAIL:
                    log.warning(f"⚠️  {consecutive_fail} ta kadr o'qilmadi — qayta ulanmoqda...")
                    cap.release()
                    time.sleep(3)
                    cap, recovered = self._open_camera(src)
                    if cap is not None and recovered is not None:
                        frame = recovered
                        consecutive_fail = 0
                        log.info("✅ Kamera qayta ulandi")
                    else:
                        log.warning("Kamera hali ulanmadi, 5s kutilmoqda...")
                        time.sleep(5)
                time.sleep(0.05)
            else:
                frame = new_frame
                consecutive_fail = 0

            frame_count += 1

            if frame_count % 3 == 0:
                detections = self.process_frame(frame)
                self.handle_alerts(frame, detections)
                t_now = time.time()
                fps_history.append(1.0 / max(t_now - t_prev, 0.001))
                t_prev = t_now

            if show_win:
                display = self.draw_ui(frame.copy(), detections)
                if fps_history:
                    avg_fps = sum(fps_history) / len(fps_history)
                    cv2.putText(display, f"FPS: {avg_fps:.1f}", (10, 40),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 100), 1)
                cv2.imshow(win_name, display)
                key = cv2.waitKey(self.cfg["frame_delay_ms"]) & 0xFF
                if key == ord('q'):
                    break
                elif key == ord('s'):
                    fname = f"screenshot_{datetime.now().strftime('%H%M%S')}.jpg"
                    cv2.imwrite(fname, frame)
                    log.info(f"📸 Screenshot: {fname}")
            else:
                time.sleep(self.cfg["frame_delay_ms"] / 1000)

        cap.release()
        if show_win:
            cv2.destroyAllWindows()

        self.telegram.send_text(
            f"🔴 <b>Tizim to'xtatildi</b>\n"
            f"Vaqt: {datetime.now().strftime('%H:%M:%S')}"
        )
        log.info("Tizim to'xtatildi")


# ══════════════════════════════════════════════
#  ISHGA TUSHIRISH
# ══════════════════════════════════════════════
if __name__ == "__main__":
    setup_logging(CONFIG["log_file"])
    log.info("=" * 50)
    log.info("  MEBEL NAZORAT TIZIMI ISHGA TUSHDI")
    log.info("=" * 50)
    guard = FurnitureGuard(CONFIG)
    guard.run()
