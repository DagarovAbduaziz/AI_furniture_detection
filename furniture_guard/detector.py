"""
Mebel Sexi Chiqish Nazorat Tizimi
===================================
✅ Internet yo'qda rasmlarni saqlaydi, kelganda yuboradi
✅ Barcha xatolar detector.log ga yoziladi
✅ Serverda ekransiz ishlaydi (show_window = False)
"""

import cv2
import json
import time
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
    print("⚠️  ultralytics o'rnatilmagan. pip install ultralytics")


# ══════════════════════════════════════════════
#  SOZLAMALAR
# ══════════════════════════════════════════════
CONFIG = {
    "camera_source": 'rtsp://admin:1FaSa1988@192.168.8.5:554/Streaming/Channels/101',

    "telegram_token": "8794822676:AAFWS7qDJ1Kj4QbqESxSE60hJhcSJ5EPWKc",
    "telegram_chat_ids": [
        "112678336",
        "8441789662"
    ],

    "exit_zone": (8, 485, 1125, 1438),

    "furniture_classes": {
        0: "divan",
        1: "kreslo",
        2: "pufik"
        # 3: "odam",
        # 4: "mashina",
    },

    "confidence_threshold":   0.70,
    "alert_cooldown_seconds": 40,
    "frame_delay_ms": 1,

    "save_alert_images": True,
    "save_folder":       "alerts",

    # Offline navbat
    "offline_queue_dir": "offline_queue",
    "offline_max_count": 200,
    "offline_retry_sec": 60,

    "log_file":    "detector.log",
    "show_window": False,   # serverda False qiling
}


# ══════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════
#  OFFLINE NAVBAT
# ══════════════════════════════════════════════
class OfflineQueue:
    def __init__(self, folder: str, max_count: int):
        self.dir = Path(folder)
        self.dir.mkdir(exist_ok=True)
        self.max_count = max_count
        self._lock = threading.Lock()

    def push(self, img_bytes: bytes, caption: str, chat_ids: list):
        with self._lock:
            existing = sorted(self.dir.glob("*.jpg"))
            while len(existing) >= self.max_count:
                old = existing.pop(0)
                old.unlink(missing_ok=True)
                old.with_suffix(".json").unlink(missing_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            self.dir.joinpath(f"{ts}.jpg").write_bytes(img_bytes)
            self.dir.joinpath(f"{ts}.json").write_text(
                json.dumps({"caption": caption, "chat_ids": chat_ids}, ensure_ascii=False),
                encoding="utf-8"
            )
            log.info(f"📥 Offline saqlandi ({self.count()} ta navbatda)")

    def pop_all(self):
        with self._lock:
            result = []
            for img_path in sorted(self.dir.glob("*.jpg")):
                meta_path = img_path.with_suffix(".json")
                if not meta_path.exists():
                    continue
                try:
                    img_bytes = img_path.read_bytes()
                    meta      = json.loads(meta_path.read_text(encoding="utf-8"))
                    result.append((img_bytes, meta.get("caption", ""), meta.get("chat_ids", []), img_path))
                except Exception:
                    pass
            return result

    def update_chat_ids(self, img_path: Path, chat_ids: list, caption: str):
        """Faqat hali yetib bormagan chat_id'larni qoldirib, json'ni yangilaydi"""
        with self._lock:
            meta_path = img_path.with_suffix(".json")
            meta_path.write_text(
                json.dumps({"caption": caption, "chat_ids": chat_ids}, ensure_ascii=False),
                encoding="utf-8"
            )

    def remove(self, img_path: Path):
        with self._lock:
            img_path.unlink(missing_ok=True)
            img_path.with_suffix(".json").unlink(missing_ok=True)

    def count(self) -> int:
        return len(list(self.dir.glob("*.jpg")))

# ══════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════
class TelegramAlert:
    def __init__(self, token: str, chat_ids: list, queue: OfflineQueue, retry_sec: int):
        self.token    = token
        self.chat_ids = chat_ids
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.queue    = queue
        self._last_alerts: dict[int, float] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._retry_loop, args=(retry_sec,), daemon=True).start()

    def _internet_ok(self) -> bool:
        try:
            requests.get("https://api.telegram.org", timeout=5)
            return True
        except Exception:
            return False

    def _send_photo_to_one(self, chat_id: str, img_bytes: bytes, caption: str) -> bool:
        try:
            resp = requests.post(
                f"{self.base_url}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("alert.jpg", img_bytes, "image/jpeg")},
                timeout=15
            )
            if resp.status_code == 200:
                log.info(f"✅ Telegram xabari yuborildi ({chat_id})")
                return True
            else:
                log.warning(f"❌ Telegram xato ({chat_id}): {resp.text[:100]}")
                return False
        except Exception as e:
            log.warning(f"❌ Telegram ulanish xatosi ({chat_id}): {e}")
            return False

    def _retry_loop(self, retry_sec: int):
        while True:
            time.sleep(retry_sec)
            pending = self.queue.pop_all()
            if not pending:
                continue
            if not self._internet_ok():
                log.info(f"📵 Internet yo'q — {len(pending)} ta rasm kutmoqda")
                continue
            log.info(f"🌐 {len(pending)} ta navbatdagi rasm yuborilmoqda...")
            for img_bytes, caption, chat_ids, img_path in pending:
                retry_caption = caption + "\n⏰ <i>Kechikib yuborildi</i>"
                still_failed = []
                for cid in chat_ids:
                    if self._send_photo_to_one(cid, img_bytes, retry_caption):
                        time.sleep(1)
                    else:
                        still_failed.append(cid)
                if still_failed:
                    # faqat hali yetmaganlar uchun navbatda qoladi
                    self.queue.update_chat_ids(img_path, still_failed, caption)
                else:
                    self.queue.remove(img_path)

    def can_send_alert(self, object_id: int, cooldown: int) -> bool:
        with self._lock:
            now  = time.time()
            last = self._last_alerts.get(object_id, 0)
            if now - last >= cooldown:
                self._last_alerts[object_id] = now
                return True
            return False

    def send_photo_alert(self, img_bytes: bytes, caption: str):
        internet = self._internet_ok()
        failed_ids = []
        if internet:
            for cid in self.chat_ids:
                if not self._send_photo_to_one(cid, img_bytes, caption):
                    failed_ids.append(cid)
        else:
            failed_ids = list(self.chat_ids)

        if failed_ids:
            log.info(f"📵 {len(failed_ids)} ta chatga yetmadi — navbatga qo'shildi")
            self.queue.push(img_bytes, caption, failed_ids)

    def send_text(self, message: str):
        for chat_id in self.chat_ids:
            try:
                requests.post(
                    f"{self.base_url}/sendMessage",
                    data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                    timeout=10
                )
            except Exception as e:
                log.warning(f"send_text xato ({chat_id}): {e}")


# ══════════════════════════════════════════════
#  CHIQISH ZONASI
# ══════════════════════════════════════════════
def box_in_exit_zone(box_xyxy, zone_xyxy, overlap_threshold=0.3) -> bool:
    bx1, by1, bx2, by2 = box_xyxy
    zx1, zy1, zx2, zy2 = zone_xyxy
    ix1, iy1 = max(bx1, zx1), max(by1, zy1)
    ix2, iy2 = min(bx2, zx2), min(by2, zy2)
    if ix2 <= ix1 or iy2 <= iy1:
        return False
    intersection = (ix2 - ix1) * (iy2 - iy1)
    box_area = (bx2 - bx1) * (by2 - by1)
    if box_area == 0:
        return False
    return (intersection / box_area) >= overlap_threshold


# ══════════════════════════════════════════════
#  ASOSIY DETEKTOR
# ══════════════════════════════════════════════
class FurnitureGuard:
    def __init__(self, config: dict):
        self.cfg = config
        self.queue = OfflineQueue(config["offline_queue_dir"], config["offline_max_count"])
        self.telegram = TelegramAlert(
            config["telegram_token"], config["telegram_chat_ids"],
            self.queue, config["offline_retry_sec"]
        )
        self.alert_log: list[dict] = []
        Path(config["save_folder"]).mkdir(exist_ok=True)

        log.info("🔄 Model yuklanmoqda...")
        if YOLO_AVAILABLE:
            self.model = YOLO("detection.pt")
            self.model.to("cpu")
            log.info(f"✅ Model yuklandi: {list(self.model.names.values())}")
        else:
            self.model = None

    def draw_ui(self, frame, detections: list):
        h, w = frame.shape[:2]
        zone = self.cfg["exit_zone"]
        cv2.rectangle(frame, (zone[0], zone[1]), (zone[2], zone[3]), (0, 100, 255), 2)
        cv2.putText(frame, "CHIQISH ZONASI", (zone[0] + 5, zone[1] + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 100, 255), 1)
        for det in detections:
            x1, y1, x2, y2 = [int(v) for v in det["box"]]
            in_zone   = det["in_exit_zone"]
            color     = (0, 50, 220) if in_zone else (50, 200, 50)
            thickness = 3 if in_zone else 2
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)
            tag = f"{det['label']} {det['confidence']:.0%}"
            if in_zone:
                tag += " ⚠ CHIQYAPTI!"
            (tw, th), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
            cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), color, -1)
            cv2.putText(frame, tag, (x1 + 2, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        cv2.putText(frame, ts, (10, h - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        pending = self.queue.count()
        status  = f"📵 OFFLINE ({pending} navbatda)" if pending > 0 else "🌐 ONLINE"
        s_color = (0, 100, 255) if pending > 0 else (0, 200, 80)
        cv2.putText(frame, status, (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, s_color, 1)
        cv2.putText(frame, f"Topilgan mebel: {len(detections)}", (10, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        return frame

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
                xyxy    = box.xyxy[0].tolist()
                conf    = float(box.conf[0])
                in_zone = box_in_exit_zone(xyxy, self.cfg["exit_zone"])
                detections.append({
                    "label":        self.cfg["furniture_classes"][cls_id],
                    "class_id":     cls_id,
                    "confidence":   conf,
                    "box":          xyxy,
                    "in_exit_zone": in_zone,
                })
        return detections

    def handle_alerts(self, frame, detections: list):
        for det in detections:
            if not det["in_exit_zone"]:
                continue
            if not self.telegram.can_send_alert(det["class_id"], self.cfg["alert_cooldown_seconds"]):
                continue
            now    = datetime.now()
            ts_str = now.strftime("%Y-%m-%d")
            caption = (
                f"🚨 <b>MEBEL SEXI OGOHLANTIRISH!</b>\n\n"
                f"📦 Mebel: <b>{det['label']}</b>\n"
                f"📊 Ishonch darajasi: {det['confidence']:.0%}\n"
                f"📍 Joyi: Chiqish zonasi\n"
                f"🕐 Vaqt: {ts_str}"
            )
            _, img_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            img_bytes  = img_buf.tobytes()
            if self.cfg["save_alert_images"]:
                fname = now.strftime(
                    f"{self.cfg['save_folder']}/%Y%m%d_%H%M%S_{det['label'].replace('/', '_')}.jpg"
                )
                Path(fname).write_bytes(img_bytes)
            threading.Thread(
                target=self.telegram.send_photo_alert,
                args=(img_bytes, caption), daemon=True
            ).start()
            self.alert_log.append({"time": ts_str, "furniture": det["label"], "confidence": det["confidence"]})
            log.info(f"🚨 OGOHLANTIRISH: {det['label']} chiqish zonasida!")

    def _open_camera(self, src):
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            return None, None
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        frame = None
        for _ in range(10):
            ret, f = cap.read()
            if ret and f is not None:
                frame = f
                break
            time.sleep(0.1)
        if frame is None:
            cap.release()
            return None, None
        return cap, frame

    def run(self):
        src      = self.cfg["camera_source"]
        show_win = self.cfg["show_window"]
        win_name = "Mebel Nazorat Tizimi  |  q = chiqish"
        detections        = []
        frame_count       = 0
        fps_history       = deque(maxlen=30)
        t_prev            = time.time()
        consecutive_fails = 0
        MAX_FAILS         = 2

        log.info(f"🔄 Kameraga ulanmoqda: {src}")
        cap, frame = self._open_camera(src)
        if cap is None:
            log.error(f"❌ Kameraga ulanib bo'lmadi: {src}")
            return

        h, w = frame.shape[:2]
        fps  = cap.get(cv2.CAP_PROP_FPS) or 15

        if self.cfg["exit_zone"] is None:
            self.cfg["exit_zone"] = (w // 2, 0, w, h)

        if show_win:
            cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(win_name, w, h)

        log.info(f"🎥 Kamera: {w}x{h} @ {fps:.0f}FPS")
        log.info(f"🚪 Chiqish zonasi: {self.cfg['exit_zone']}")
        # self.telegram.send_text(
        #     "🟢 <b>Mebel nazorat tizimi ishga tushdi</b>\n"
        #     f"Kamera: {w}x{h}\n"
        #     f"Vaqt: {datetime.now().strftime('%H:%M:%S')}"
        # )
        log.info("▶  Kuzatish boshlandi")

        while True:
            ret, new_frame = cap.read()
            if not ret or new_frame is None:
                consecutive_fails += 1
                if consecutive_fails < MAX_FAILS:
                    time.sleep(0.05)
                else:
                    log.warning(f"⚠️  Qayta ulanmoqda...")
                    cap.release()
                    time.sleep(2)
                    cap, recovered = self._open_camera(src)
                    if cap is None:
                        time.sleep(5)
                        cap, recovered = self._open_camera(src)
                    if cap is not None and recovered is not None:
                        frame = recovered
                        consecutive_fails = 0
                        log.info("✅ Kamera qayta ulandi")
                    else:
                        time.sleep(3)
            else:
                frame = new_frame
                consecutive_fails = 0

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
                    avg_fps      = sum(fps_history) / len(fps_history)
                    status_color = (0, 220, 80) if consecutive_fails == 0 else (0, 100, 255)
                    status_txt   = "● JONLI" if consecutive_fails == 0 else f"● XATO ({consecutive_fails})"
                    cv2.putText(display, f"FPS: {avg_fps:.1f}  {status_txt}", (10, 40),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, status_color, 1)
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
        log.info(f"📊 Jami ogohlantirishlar: {len(self.alert_log)}")
        self.telegram.send_text(
            f"🔴 <b>Tizim to'xtatildi</b>\n"
            f"Jami ogohlantirishlar: {len(self.alert_log)}\n"
            f"Vaqt: {datetime.now().strftime('%H:%M:%S')}"
        )


if __name__ == "__main__":
    guard = FurnitureGuard(CONFIG)
    guard.run()