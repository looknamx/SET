import cv2
import numpy as np
import mss
import interception
import keyboard
import time
import win32gui
import os
import sys
import configparser
import threading
import customtkinter as ctk
import gc
import hashlib
import requests
from datetime import datetime
from collections import deque
from PIL import Image
from ultralytics import YOLO
from bot_utils import (
    clamp_float,
    clamp_int,
    configure_keyboard_device,
    configure_working_mouse_device,
    download_file,
    game_is_active,
    get_safe_window_rect,
    get_window_debug_info,
    get_window_handle,
    interception_driver_ready,
    parse_bool,
    safe_click,
    safe_move_to,
    safe_press,
)
from config_manager import (
    get_config_dir,
    load_master,
    load_profile,
    migrate_legacy_config,
    save_profile,
)
from updater import (
    download_app_update,
    download_validated_model,
    fetch_latest_version,
    fetch_model_manifest,
    fetch_release_manifest,
    is_newer_version,
    launch_updater,
)
from runtime_systems import (
    EngagementTimer,
    PreflightResult,
    SmartTargetManager,
    StuckRecoveryManager,
    evaluate_worker_health,
    load_with_single_recovery,
    select_due_skill,
    select_potion_action,
)

# =========================================================
# 🌟 ตั้งค่า Auto-Update 
# =========================================================
CURRENT_VERSION = "2.7.2"
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/looknamx/SET/main/version.txt"
GITHUB_MANIFEST_URL = "https://raw.githubusercontent.com/looknamx/SET/main/release_manifest.json"
GITHUB_MODEL_MANIFEST_URL = "https://raw.githubusercontent.com/looknamx/SET/main/model_manifest.json"
GITHUB_DOWNLOAD_URL = "https://github.com/looknamx/SET/releases/latest/download/150469.exe" 
GITHUB_MODEL_URL = "https://github.com/looknamx/SET/releases/latest/download/best.pt" 
GITHUB_EXE_SHA256 = ""
GITHUB_MODEL_SHA256 = ""

APP_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)
CONFIG_DIR = os.path.abspath(
    os.environ.get("AI_LOOKNAM_CONFIG_DIR")
    or get_config_dir(
        APP_DIR,
        frozen=getattr(sys, "frozen", False),
        local_app_data=os.environ.get("LOCALAPPDATA"),
    )
)


def app_path(filename):
    return os.path.join(APP_DIR, filename)


def config_path(filename):
    destination = os.path.join(CONFIG_DIR, filename)
    if migrate_legacy_config(app_path(filename), destination):
        print(f">> Migrated config to: {destination}")
    return destination


def bundled_path(filename):
    bundle_dir = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(bundle_dir, filename)

# =========================================================
# 📝 ค่าคงที่สำหรับ Potion
# =========================================================
HP_START_X, HP_END_X, HP_Y = 47, 173, 87
SP_START_X, SP_END_X, SP_Y = 48, 173, 103
HP_COLOR, HP_COLOR_RED = (156, 173, 222), (222, 148, 173)
SP_COLOR, SP_COLOR_RED = (156, 181, 238), (222, 148, 173)
TOLERANCE = 40
key_lock = threading.Lock()

def is_color_match(r, g, b, target_color, tol):
    tr, tg, tb = target_color
    return (abs(int(r) - int(tr)) <= tol and abs(int(g) - int(tg)) <= tol and abs(int(b) - int(tb)) <= tol)

class RedirectText(object):
    def __init__(self, text_widget, app):
        self.text_widget = text_widget; self.app = app
    def write(self, string):
        if string.strip(): self.app.after(0, self._write, string + "\n")
    def _write(self, string):
        self.text_widget.insert("end", string); self.text_widget.see("end") 
        lines = self.text_widget.get("0.0", "end-1c").count('\n')
        if lines > 200: self.text_widget.delete("0.0", f"{lines - 150}.0")
    def flush(self): pass

# =========================================================
# 🎨 1. ตั้งค่าหน้าต่างโปรแกรม (CustomTkinter)
# =========================================================
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

config = configparser.ConfigParser()
config_file = config_path('config_Profile_1.ini')

class BotApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"AI looknam Promax v{CURRENT_VERSION}")
        self.geometry("500x850") 
        self.resizable(False, False)

        self.running = False
        self.run_event = None
        self.worker_threads = []
        self.shutdown_event = threading.Event()
        self.model = None
        self.template_dead = None
        self.potion_widgets = []
        self.skill_widgets = []
        self.ui_scale = 1.0 
        self.active_profile = "Profile 1"
        self._log_times = {}
        self.release_manifest = {}
        self.model_manifest = {}
        self.model_validated = False
        self.vitals_lock = threading.Lock()
        self.latest_vitals = {"hp": None, "sp": None, "updated": 0.0}
        self.combat_active = False
        self.worker_heartbeats = {}
        self.worker_errors = {}
        self.worker_error_times = {}
        self.run_started_at = 0.0
        self._watchdog_alert_times = {}
        self._preflight_running = False

        self.load_master_config()
        
        ctk.set_widget_scaling(self.ui_scale)
        ctk.set_window_scaling(self.ui_scale)
        
        self.load_config_file()
        self.build_ui()
        
        sys.stdout = RedirectText(self.log_textbox, self)
        threading.Thread(target=self.hotkey_listener, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.init_bot_systems)

    def load_master_config(self):
        self.master_file = config_path('master.ini')
        self.master_cfg, backup_path = load_master(self.master_file)
        if backup_path:
            print(f">> Invalid master config was backed up to: {backup_path}")
        self.ui_scale = clamp_float(self.master_cfg['GLOBAL'].get('UIScale', '1.0'), 1.0, 0.75, 1.0)
        self.active_profile = self.master_cfg['GLOBAL'].get('ActiveProfile', 'Profile 1')
        if self.active_profile not in ('Profile 1', 'Profile 2', 'Profile 3'):
            self.active_profile = 'Profile 1'
            self.master_cfg['GLOBAL']['ActiveProfile'] = self.active_profile
            save_profile(self.master_cfg, self.master_file)

    def load_config_file(self):
        global config, config_file
        config_file = config_path(f"config_{self.active_profile.replace(' ', '_')}.ini")
        config, backup_path = load_profile(config_file)
        if backup_path:
            print(f">> Invalid config was backed up to: {backup_path}")
        
        if config.has_section('NOTIFY'): config.remove_section('NOTIFY')
        if config.has_section('DEBUFF'): config.remove_section('DEBUFF')
        if config.has_option('HOTKEYS', 'ToggleYolo'):
            config['HOTKEYS'] = {'MasterToggle': config['HOTKEYS'].get('MasterToggle', 'ctrl+f1')}
            
        if not config.has_option('TELEPORT', 'EnableAutoTpStuck'):
            config['TELEPORT']['EnableAutoTpStuck'] = 'False'
            config['TELEPORT']['AutoTpStuckSec'] = '10.0'
            
        self.update_runtime_vars()

    def update_runtime_vars(self):
        self.game_title = config['GENERAL'].get('GameWindowTitle', 'Ragnarok')
        self.offset_px = clamp_int(config['GENERAL'].get('OffsetPx', '120'), 120, 0, 400)
        
        self.stop_on_death = parse_bool(config['GENERAL'].get('StopOnDeath'), True)
        self.yolo_enabled = parse_bool(config['GENERAL'].get('YoloEnabled'), True)
        self.buff_enabled = parse_bool(config['GENERAL'].get('BuffEnabled'), True)
        self.potion_enabled = parse_bool(config['GENERAL'].get('PotionEnabled'), True)
        self.skill_rotation_enabled = parse_bool(config['GENERAL'].get('SkillRotationEnabled'), False)
        self.attack_click = parse_bool(config['GENERAL'].get('AttackClick'), False)
        self.attack_interval = clamp_float(config['GENERAL'].get('AttackIntervalSec', '0.8'), 0.8, 0.1, 10.0)
        self.rare_item_action = config['GENERAL'].get('RareItemAction', 'Log').strip().title()
        if self.rare_item_action not in ('Log', 'Stop', 'Key'):
            self.rare_item_action = 'Log'
        self.rare_item_key = config['GENERAL'].get('RareItemKey', '').strip().lower()
        self.mouse_device = config['GENERAL'].get('MouseDevice', 'Auto').strip()
        self.keyboard_device = config['GENERAL'].get('KeyboardDevice', 'Auto').strip()
        
        self.conf_monster = clamp_float(config['AI_CONFIDENCE'].get('ConfMonster', '0.4'), 0.4, 0.05, 1.0)
        self.conf_rare = clamp_float(config['AI_CONFIDENCE'].get('ConfRareItem', '0.8'), 0.8, 0.05, 1.0)
            
        self.hk_master = config['HOTKEYS'].get('MasterToggle', 'ctrl+f1')
        
        self.enable_teleport = parse_bool(config['TELEPORT'].get('EnableTeleport'), False)
        self.teleport_mode = config['TELEPORT'].get('TeleportMode', 'Fly Wing')
        if self.teleport_mode not in ('Fly Wing', 'Skill'):
            self.teleport_mode = 'Fly Wing'
        self.teleport_key = config['TELEPORT'].get('TeleportKey', 'f8')
        self.teleport_wait = clamp_float(config['TELEPORT'].get('WaitTimeSec', '1.0'), 1.0, 0.2, 120.0)
        
        self.auto_tp_stuck = parse_bool(config['TELEPORT'].get('EnableAutoTpStuck'), False)
        self.auto_tp_stuck_sec = clamp_float(config['TELEPORT'].get('AutoTpStuckSec', '10.0'), 10.0, 1.0, 300.0)

        self.smart_target_enabled = parse_bool(config['SMART_TARGET'].get('Enabled'), True)
        self.target_blacklist_sec = clamp_float(config['SMART_TARGET'].get('BlacklistSec'), 15.0, 1.0, 300.0)
        self.target_edge_margin = clamp_float(config['SMART_TARGET'].get('EdgeMarginPct'), 8.0, 0.0, 40.0) / 100.0

        self.stuck_move_enabled = parse_bool(config['STUCK_RECOVERY'].get('MoveBeforeTeleport'), True)
        self.stuck_attempts_before_tp = clamp_int(config['STUCK_RECOVERY'].get('AttemptsBeforeTeleport'), 2, 1, 10)
        self.stuck_failure_window = clamp_float(config['STUCK_RECOVERY'].get('FailureWindowSec'), 30.0, 5.0, 600.0)
        self.target_progress_px = clamp_int(config['STUCK_RECOVERY'].get('ProgressDistancePx'), 50, 10, 500)

        self.watchdog_enabled = parse_bool(config['WATCHDOG'].get('Enabled'), True)
        self.watchdog_worker_timeout = clamp_float(config['WATCHDOG'].get('WorkerTimeoutSec'), 12.0, 3.0, 120.0)
        self.watchdog_game_timeout = clamp_float(config['WATCHDOG'].get('GameMissingTimeoutSec'), 30.0, 5.0, 600.0)
        self.watchdog_max_errors = clamp_int(config['WATCHDOG'].get('MaxLoopErrors'), 5, 1, 100)
        self.discord_webhook_url = config['WATCHDOG'].get('DiscordWebhookURL', '').strip()
        self.notify_cooldown = clamp_float(config['WATCHDOG'].get('NotifyCooldownSec'), 60.0, 5.0, 3600.0)
        
        self.buff_settings = {}
        if 'AUTO_BUFF_ITEM' in config:
            for key, value in config['AUTO_BUFF_ITEM'].items():
                if value.strip(): self.buff_settings[key.strip().lower()] = clamp_float(value.strip(), 60.0, 1.0, 3600.0)
                
        self.potions_list = []
        if 'POTIONS' in config:
            for k, v in config['POTIONS'].items():
                parts = v.split(',')
                if len(parts) == 5:
                    self.potions_list.append({
                        "type": parts[0] if parts[0] in ("HP", "SP") else "HP",
                        "pct": str(clamp_int(parts[1], 50, 1, 100)),
                        "key": parts[2].strip().lower(),
                        "dly": str(clamp_int(parts[3], 50, 20, 60000)),
                        "en": parts[4].strip().lower() == 'true'
                    })

        self.skills_list = []
        if 'SKILL_ROTATION' in config:
            for _, value in config['SKILL_ROTATION'].items():
                parts = value.split(',')
                if len(parts) == 5:
                    self.skills_list.append({
                        "key": parts[0].strip().lower(),
                        "cooldown": str(clamp_float(parts[1], 1.0, 0.1, 3600.0)),
                        "min_sp": str(clamp_int(parts[2], 0, 0, 100)),
                        "target_only": parts[3].strip().lower() == 'true',
                        "en": parts[4].strip().lower() == 'true',
                    })

    def check_and_download_model(self):
        model_file = app_path("best.pt")
        try:
            manifest = fetch_model_manifest(GITHUB_MODEL_MANIFEST_URL)
            if is_newer_version(manifest["min_app_version"], CURRENT_VERSION):
                raise ValueError(
                    f"model requires app v{manifest['min_app_version']} or newer"
                )
            self.model_manifest = manifest
            print(f">> Latest AI model: {manifest['model_version']}")
        except Exception as e:
            print(f">> Model manifest unavailable; using compatibility mode: {e}")
        try:
            self.release_manifest = fetch_release_manifest(GITHUB_MANIFEST_URL)
        except Exception as e:
            print(f">> Release manifest unavailable; using compatibility mode: {e}")
        should_download = not os.path.exists(model_file)
        if getattr(sys, "frozen", False) and not should_download and self._expected_model_hash():
            should_download = self._model_sha256().lower() != self._expected_model_hash().lower()
            if should_download:
                print(">> A newer AI model is available")
        if should_download:
            self.after(0, lambda: self.lbl_status_main.configure(text="สถานะ: 📥 กำลังโหลดโมเดล AI...", text_color="#FFB000"))
            print(">> 📥 ไม่พบไฟล์โมเดล AI ในเครื่อง กำลังดาวน์โหลดอัตโนมัติ... (อาจใช้เวลา 1-2 นาที ห้ามปิดโปรแกรม)")
            self.update() 
            try:
                self._download_model("model is missing or outdated")
                print(">> ✅ ดาวน์โหลดไฟล์ best.pt สำเร็จ!")
                return True
            except Exception as e:
                print(f">> ❌ โหลดโมเดลล้มเหลว (เช็คอินเทอร์เน็ต หรือลิงก์ GITHUB_MODEL_URL): {e}")
                self.lbl_status_main.configure(text="สถานะ: 🔴 โหลด AI ล้มเหลว", text_color="red")
                return False
        return True

    def _expected_model_hash(self):
        return (
            self.model_manifest.get("sha256")
            or self.release_manifest.get("model_sha256")
            or GITHUB_MODEL_SHA256
            or None
        )

    def _model_download_url(self):
        return self.model_manifest.get("download_url") or GITHUB_MODEL_URL

    def _download_model(self, reason):
        model_file = app_path("best.pt")
        self.lbl_status_main.configure(text="Status: downloading AI model...", text_color="#FFB000")
        print(f">> Downloading AI model: {reason}")
        actual_hash = download_validated_model(
            self._model_download_url(),
            model_file,
            self._expected_model_hash(),
            self._validate_model_path,
        )
        print(f">> AI model downloaded (SHA256 {actual_hash[:12]}...)")
        return actual_hash

    def _load_and_validate_model(self):
        return self._validate_model_path(app_path("best.pt"))

    def _validate_model_path(self, model_path):
        model = YOLO(model_path)
        model(np.zeros((160, 160, 3), dtype=np.uint8), verbose=False, conf=0.1)
        return model

    def load_model_with_recovery(self):
        if not self.check_and_download_model():
            return False
        def recover_model(first_error):
            print(f">> AI model load/inference failed: {first_error}")
            self.model = None
            self.model_validated = False
            gc.collect()
            self._download_model("load or inference validation failed")

        try:
            self.model, recovered = load_with_single_recovery(
                self._load_and_validate_model, recover_model
            )
            self.model_validated = True
            if recovered:
                print(">> AI model recovered successfully after downloading a fresh copy")
            return True
        except Exception as error:
            self.model = None
            self.model_validated = False
            print(f">> AI model is unavailable after recovery: {error}")
            return False

    def init_bot_systems(self):
        self.lbl_status_main.configure(text="สถานะ: 🟡 กำลังเตรียม AI...", text_color="orange")
        self.update()
        if not self.load_model_with_recovery():
            self.lbl_status_main.configure(text="Status: AI model unavailable", text_color="red")
            return
        try:
            keyboard_device, keyboard_hwid = configure_keyboard_device(self.keyboard_device)
            print(f">> Keyboard device: {keyboard_device} ({keyboard_hwid})")
            mouse_device, mouse_hwid = configure_working_mouse_device(self.mouse_device)
            print(f">> Mouse device: {mouse_device} ({mouse_hwid})")
            img = cv2.imread(bundled_path('dead_button.png'), cv2.IMREAD_UNCHANGED)
            if img is not None and img.shape[2] == 4: self.template_dead = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            else: self.template_dead = img
            self.lbl_status_main.configure(text="สถานะ: 🟢 พร้อมใช้งาน", text_color="white")
            print(f"✅ โหลด AI สำเร็จ! (โหลดข้อมูลจาก: {self.active_profile})")
            self.after(2000, lambda: self.check_for_updates(silent=True))
        except Exception as e:
            self.lbl_status_main.configure(text="สถานะ: 🔴 โหลด AI ล้มเหลว", text_color="red")
            print(f"❌ Error AI: {e}")

    def force_update_ai(self):
        if self.running:
            print(">> ⚠️ กรุณากดหยุดบอท (Master Toggle) ก่อนทำการอัปเดต AI ครับ!")
            self.lbl_status_main.configure(text="สถานะ: ⚠️ ปิดบอทก่อนอัปเดต", text_color="orange")
            return
        self.btn_force_ai.configure(text="⏳ กำลังดาวน์โหลด...", state="disabled", fg_color="#555555")
        threading.Thread(target=self._process_force_update_ai, daemon=True).start()

    def _process_force_update_ai(self):
        model_file = app_path("best.pt")
        try:
            manifest = fetch_model_manifest(GITHUB_MODEL_MANIFEST_URL)
            if is_newer_version(manifest["min_app_version"], CURRENT_VERSION):
                raise RuntimeError(
                    f"AI model requires app v{manifest['min_app_version']} or newer"
                )
            self.model_manifest = manifest
            if os.path.exists(model_file):
                self.model = None; gc.collect(); time.sleep(1)
                print(">> เตรียมแทนที่โมเดลเดิมเมื่อดาวน์โหลดไฟล์ใหม่สำเร็จ")
            self.after(0, lambda: self.lbl_status_main.configure(text="สถานะ: 📥 กำลังโหลดโมเดล AI...", text_color="#FFB000"))
            print(">> 📥 กำลังดาวน์โหลดโมเดล AI ใหม่ล่าสุดจาก GitHub... (ห้ามปิดโปรแกรม)")
            self._download_model("manual update")
            print(">> ✅ ดาวน์โหลดไฟล์ best.pt ใหม่สำเร็จ!")
            self.after(0, lambda: self.btn_force_ai.configure(text="✅ อัปเดต AI สำเร็จ", fg_color="green"))
            self.after(0, self.reload_ai_model)
        except Exception as e:
            print(f">> ❌ อัปเดตโมเดลล้มเหลว: {e}")
            self.after(0, lambda: self.btn_force_ai.configure(text="❌ ล้มเหลว (คลิกเพื่อลองใหม่)", fg_color="#FF3333", state="normal"))
            self.after(4000, lambda: self.btn_force_ai.configure(text="🔄 บังคับอัปเดตโมเดล AI (best.pt)", fg_color="#8B0000", state="normal", hover_color="#6B0000"))

    def reload_ai_model(self):
        self.lbl_status_main.configure(text="สถานะ: 🟡 กำลังเตรียม AI...", text_color="orange")
        try:
            self.model = self._load_and_validate_model()
            self.model_validated = True
            self.lbl_status_main.configure(text="สถานะ: 🟢 พร้อมใช้งาน", text_color="white")
            print(">> ✅ นำ AI ตัวใหม่เข้าสู่ระบบพร้อมใช้งานแล้ว!")
            self.after(3000, lambda: self.btn_force_ai.configure(text="🔄 บังคับอัปเดตโมเดล AI (best.pt)", fg_color="#8B0000", state="normal", hover_color="#6B0000"))
        except Exception as e:
            self.lbl_status_main.configure(text="สถานะ: 🔴 โหลด AI ล้มเหลว", text_color="red")
            print(f"❌ Error AI: {e}")
            self.after(3000, lambda: self.btn_force_ai.configure(text="🔄 บังคับอัปเดตโมเดล AI (best.pt)", fg_color="#8B0000", state="normal", hover_color="#6B0000"))

    def on_profile_change(self, choice):
        if self.running:
            self.toggle_master()
            print(">> 🛑 หยุดการทำงานอัตโนมัติ เพื่อสลับโปรไฟล์")
            
        if not self.save_config(silent=True):
            self.profile_var.set(self.active_profile)
            return
        self.active_profile = choice
        self.master_cfg['GLOBAL']['ActiveProfile'] = self.active_profile
        save_profile(self.master_cfg, self.master_file)
        
        self.load_config_file() 
        self.refresh_ui_vars()  
        print(f">> 🔄 สลับและโหลดการตั้งค่าของ [{self.active_profile}] สำเร็จ!")

    def refresh_ui_vars(self):
        self.var_game_title.set(self.game_title)
        self.var_offset.set(str(self.offset_px))
        self.var_conf_monster.set(str(self.conf_monster))
        self.var_conf_rare.set(str(self.conf_rare))
        self.var_hk_master.set(self.hk_master)
        
        self.var_stop_on_death.set(self.stop_on_death)
        self.var_yolo_en.set(self.yolo_enabled)
        self.var_buff_en.set(self.buff_enabled)
        self.var_pot_en.set(self.potion_enabled)
        self.var_skill_rotation.set(self.skill_rotation_enabled)
        self.var_attack_click.set(self.attack_click)
        self.var_attack_interval.set(str(self.attack_interval))
        self.var_rare_action.set(self.rare_item_action)
        self.var_rare_key.set(self.rare_item_key)
        self.var_mouse_device.set(self.mouse_device)
        self.var_keyboard_device.set(self.keyboard_device)
        self.toggle_yolo_switch()
        self.toggle_buff_switch()
        self.toggle_pot_switch()
        
        self.var_enable_teleport.set(self.enable_teleport)
        self.var_teleport_mode.set(self.teleport_mode)
        self.var_teleport_key.set(self.teleport_key)
        self.var_teleport_wait.set(str(self.teleport_wait))
        
        self.var_auto_tp_stuck.set(self.auto_tp_stuck)
        self.var_auto_tp_stuck_sec.set(str(self.auto_tp_stuck_sec))
        self.var_smart_target.set(self.smart_target_enabled)
        self.var_target_blacklist.set(str(self.target_blacklist_sec))
        self.var_target_edge.set(str(int(self.target_edge_margin * 100)))
        self.var_stuck_move.set(self.stuck_move_enabled)
        self.var_stuck_attempts.set(str(self.stuck_attempts_before_tp))
        self.var_stuck_window.set(str(self.stuck_failure_window))
        self.var_target_progress.set(str(self.target_progress_px))
        self.var_watchdog.set(self.watchdog_enabled)
        self.var_watchdog_worker_timeout.set(str(self.watchdog_worker_timeout))
        self.var_watchdog_game_timeout.set(str(self.watchdog_game_timeout))
        self.var_watchdog_errors.set(str(self.watchdog_max_errors))
        self.var_webhook.set(self.discord_webhook_url)
        
        for widget in self.buff_rows_container.winfo_children(): widget.destroy()
        self.buff_ui_rows.clear()
        for key, val in self.buff_settings.items(): self.add_buff_ui_row(key, str(val))
        
        self.render_potions()
        self.render_skills()
        self.update_hotkey_display()

    def set_ui_scale(self, scale_val):
        self.ui_scale = scale_val
        ctk.set_widget_scaling(scale_val)
        ctk.set_window_scaling(scale_val)
        self.master_cfg['GLOBAL']['UIScale'] = str(scale_val)
        with open(self.master_file, 'w', encoding='utf-8') as f: self.master_cfg.write(f)
        print(f">> 📏 ปรับขนาดหน้าจอเป็น: {int(scale_val*100)}%")

    def build_ui(self):
        self.main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.main_frame.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        self.header_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent", height=30)
        self.header_frame.pack(fill="x", side="top", pady=(0, 5))
        
        self.profile_var = ctk.StringVar(value=self.active_profile)
        self.profile_menu = ctk.CTkComboBox(self.header_frame, values=["Profile 1", "Profile 2", "Profile 3"], variable=self.profile_var, width=120, command=self.on_profile_change)
        self.profile_menu.pack(side="left", padx=5)
        
        self.scale_frame = ctk.CTkFrame(self.header_frame, fg_color="transparent")
        self.scale_frame.pack(side="right")
        ctk.CTkButton(self.scale_frame, text="a", width=25, height=25, fg_color="#333333", hover_color="#444444", command=lambda: self.set_ui_scale(0.75)).pack(side="left", padx=2)
        ctk.CTkButton(self.scale_frame, text="A", width=25, height=25, fg_color="#333333", hover_color="#444444", command=lambda: self.set_ui_scale(1.0)).pack(side="left", padx=2)

        self.tabview = ctk.CTkTabview(self.main_frame, width=460, height=730)
        self.tabview.pack(padx=0, pady=0)

        self.tab_main = self.tabview.add("🎮 ควบคุม")
        self.tab_config = self.tabview.add("⚙️ ตั้งค่า")
        self.tab_potion = self.tabview.add("💊 ยา & บัพ") 
        self.tab_camera = self.tabview.add("📺 กล้อง")

        # ==================== TAB 1: ควบคุม ====================
        
        self.var_yolo_en = ctk.BooleanVar(value=self.yolo_enabled)
        self.var_buff_en = ctk.BooleanVar(value=self.buff_enabled)
        self.var_pot_en = ctk.BooleanVar(value=self.potion_enabled)

        sw_frame = ctk.CTkFrame(self.tab_main, fg_color="transparent")
        sw_frame.pack(side="top", fill="x", padx=10, pady=(5, 10))
        
        ctk.CTkSwitch(sw_frame, text="ตาวิเศษ (YOLO)", variable=self.var_yolo_en, command=self.toggle_yolo_switch, font=("Arial", 12)).pack(side="left", expand=True)
        ctk.CTkSwitch(sw_frame, text="ออโต้บัพ", variable=self.var_buff_en, command=self.toggle_buff_switch, font=("Arial", 12)).pack(side="left", expand=True)
        ctk.CTkSwitch(sw_frame, text="ปั้มยา", variable=self.var_pot_en, command=self.toggle_pot_switch, font=("Arial", 12)).pack(side="left", expand=True)

        ctk.CTkLabel(self.tab_main, text="System Log (ประวัติการทำงาน)", font=("Arial", 14, "bold"), text_color="#A0A0A0").pack(side="top", anchor="w", padx=20, pady=(0, 5))
        self.log_textbox = ctk.CTkTextbox(self.tab_main, width=420, height=290, fg_color="#000000", text_color="#00FF00", font=("Consolas", 12))
        self.log_textbox.pack(side="top", padx=20, pady=5)
        self.lbl_hotkeys = ctk.CTkLabel(self.tab_main, text="", font=("Arial", 13), text_color="#888888", justify="left")
        self.lbl_hotkeys.pack(side="top", pady=(10, 10))
        self.btn_preflight = ctk.CTkButton(
            self.tab_main,
            text="ตรวจสอบระบบ",
            width=160,
            height=28,
            fg_color="#2E7D32",
            hover_color="#256528",
            command=self.run_preflight_ui,
        )
        self.btn_preflight.pack(side="top", pady=(0, 8))
        self.update_hotkey_display() 
        self.btn_update = ctk.CTkButton(self.tab_main, text="🔄 ตรวจสอบเวอร์ชันใหม่", width=160, height=28, fg_color="#3B8ED0", command=self.check_for_updates)
        self.btn_update.pack(side="bottom", pady=(0, 20))

        # ==================== TAB 2: ตั้งค่า ====================
        self.scroll_cfg = ctk.CTkScrollableFrame(self.tab_config, width=400, height=680)
        self.scroll_cfg.pack(fill="both", expand=True)

        self.var_game_title = ctk.StringVar(value=self.game_title); self.var_offset = ctk.StringVar(value=str(self.offset_px))
        self.var_conf_monster = ctk.StringVar(value=str(self.conf_monster)); self.var_conf_rare = ctk.StringVar(value=str(self.conf_rare))
        self.var_hk_master = ctk.StringVar(value=self.hk_master)
        
        self.var_stop_on_death = ctk.BooleanVar(value=self.stop_on_death) 
        self.var_attack_click = ctk.BooleanVar(value=self.attack_click)
        self.var_attack_interval = ctk.StringVar(value=str(self.attack_interval))
        self.var_rare_action = ctk.StringVar(value=self.rare_item_action)
        self.var_rare_key = ctk.StringVar(value=self.rare_item_key)
        self.var_mouse_device = ctk.StringVar(value=self.mouse_device)
        self.var_keyboard_device = ctk.StringVar(value=self.keyboard_device)
        
        self.var_enable_teleport = ctk.BooleanVar(value=self.enable_teleport); self.var_teleport_mode = ctk.StringVar(value=self.teleport_mode); self.var_teleport_key = ctk.StringVar(value=self.teleport_key); self.var_teleport_wait = ctk.StringVar(value=str(self.teleport_wait))
        
        self.var_auto_tp_stuck = ctk.BooleanVar(value=self.auto_tp_stuck)
        self.var_auto_tp_stuck_sec = ctk.StringVar(value=str(self.auto_tp_stuck_sec))
        self.var_smart_target = ctk.BooleanVar(value=self.smart_target_enabled)
        self.var_target_blacklist = ctk.StringVar(value=str(self.target_blacklist_sec))
        self.var_target_edge = ctk.StringVar(value=str(int(self.target_edge_margin * 100)))
        self.var_stuck_move = ctk.BooleanVar(value=self.stuck_move_enabled)
        self.var_stuck_attempts = ctk.StringVar(value=str(self.stuck_attempts_before_tp))
        self.var_stuck_window = ctk.StringVar(value=str(self.stuck_failure_window))
        self.var_target_progress = ctk.StringVar(value=str(self.target_progress_px))
        self.var_watchdog = ctk.BooleanVar(value=self.watchdog_enabled)
        self.var_watchdog_worker_timeout = ctk.StringVar(value=str(self.watchdog_worker_timeout))
        self.var_watchdog_game_timeout = ctk.StringVar(value=str(self.watchdog_game_timeout))
        self.var_watchdog_errors = ctk.StringVar(value=str(self.watchdog_max_errors))
        self.var_webhook = ctk.StringVar(value=self.discord_webhook_url)
        self.var_skill_rotation = ctk.BooleanVar(value=self.skill_rotation_enabled)

        ctk.CTkLabel(self.scroll_cfg, text="1. ตั้งค่าพื้นฐาน", font=("Arial", 16, "bold")).pack(anchor="w", pady=(10,5))
        self.create_input_row(self.scroll_cfg, "หน้าต่างเกม:", self.var_game_title)
        self.create_input_row(self.scroll_cfg, "ขอบจอ (px):", self.var_offset)
        self.create_input_row(self.scroll_cfg, "Mouse device (Auto/10-19):", self.var_mouse_device)
        self.create_input_row(self.scroll_cfg, "Keyboard device (Auto/0-9):", self.var_keyboard_device)
        
        self.sw_stop_death = ctk.CTkSwitch(self.scroll_cfg, text="หยุดบอทอัตโนมัติเมื่อตัวละครตาย", variable=self.var_stop_on_death, font=("Arial", 13), progress_color="#FF3333")
        self.sw_stop_death.pack(anchor="w", padx=20, pady=(5, 10))

        self.sw_attack_click = ctk.CTkSwitch(self.scroll_cfg, text="Click monster after targeting", variable=self.var_attack_click, font=("Arial", 13))
        self.sw_attack_click.pack(anchor="w", padx=20, pady=(2, 5))
        self.create_input_row(self.scroll_cfg, "Attack interval (sec):", self.var_attack_interval)

        f_rare = ctk.CTkFrame(self.scroll_cfg, fg_color="transparent"); f_rare.pack(fill="x", pady=2)
        ctk.CTkLabel(f_rare, text="Rare item action:", width=145, anchor="e").pack(side="left", padx=5)
        ctk.CTkComboBox(f_rare, values=["Log", "Stop", "Key"], variable=self.var_rare_action, width=195).pack(side="left", padx=5)
        self.create_input_row(self.scroll_cfg, "Rare item key:", self.var_rare_key)

        ctk.CTkLabel(self.scroll_cfg, text="2. ความแม่นยำ AI (0.1-1.0)", font=("Arial", 16, "bold")).pack(anchor="w", pady=(15,5))
        self.create_input_row(self.scroll_cfg, "Monster:", self.var_conf_monster)
        self.create_input_row(self.scroll_cfg, "Rare Item:", self.var_conf_rare)
        
        self.btn_force_ai = ctk.CTkButton(self.scroll_cfg, text="🔄 บังคับอัปเดตโมเดล AI (best.pt)", fg_color="#8B0000", hover_color="#6B0000", height=32, command=self.force_update_ai)
        self.btn_force_ai.pack(pady=(10, 5))

        ctk.CTkLabel(self.scroll_cfg, text="3. ตั้งค่าปุ่มลัด (Hotkeys)", font=("Arial", 16, "bold")).pack(anchor="w", pady=(15,5))
        self.create_input_row(self.scroll_cfg, "ปุ่มลัดหลัก (Master):", self.var_hk_master)

        ctk.CTkLabel(self.scroll_cfg, text="4. ค้นหา & แก้ติดขัด (Teleport)", font=("Arial", 16, "bold")).pack(anchor="w", pady=(15,5))
        
        self.sw_teleport = ctk.CTkSwitch(self.scroll_cfg, text="[ค้นหา] วิงเมื่อหามอนสเตอร์ไม่เจอ", variable=self.var_enable_teleport, font=("Arial", 13), progress_color="#FFB000")
        self.sw_teleport.pack(anchor="w", padx=20, pady=(2, 5))
        
        self.sw_tp_stuck = ctk.CTkSwitch(self.scroll_cfg, text="[ติดขัด] วิงหนีเมื่อตีมอนนานเกินไป", variable=self.var_auto_tp_stuck, font=("Arial", 13), progress_color="#FF3333")
        self.sw_tp_stuck.pack(anchor="w", padx=20, pady=(2, 10))

        f_tp_mode = ctk.CTkFrame(self.scroll_cfg, fg_color="transparent"); f_tp_mode.pack(fill="x", pady=2)
        ctk.CTkLabel(f_tp_mode, text="รูปแบบ (Mode):", width=145, anchor="e").pack(side="left", padx=5)
        ctk.CTkComboBox(f_tp_mode, values=["Fly Wing", "Skill"], variable=self.var_teleport_mode, width=195).pack(side="left", padx=5)
        
        self.create_input_row(self.scroll_cfg, "ปุ่มกด (Key):", self.var_teleport_key)
        self.create_input_row(self.scroll_cfg, "เวลารอค้นหา (วิ):", self.var_teleport_wait)
        self.create_input_row(self.scroll_cfg, "เวลาติดขัด (วิ):", self.var_auto_tp_stuck_sec)

        self.btn_save = ctk.CTkButton(self.scroll_cfg, text="💾 บันทึกการตั้งค่า (Save)", height=40, font=("Arial", 14, "bold"), command=self.save_config)
        self.btn_save.pack(pady=30)

        # ==================== TAB 3: ปั้มยา (POTION) ====================
        ctk.CTkLabel(self.scroll_cfg, text="5. Smart Target", font=("Arial", 16, "bold")).pack(anchor="w", pady=(15, 5))
        ctk.CTkSwitch(self.scroll_cfg, text="เปิด Smart Target และ blacklist", variable=self.var_smart_target).pack(anchor="w", padx=20, pady=4)
        self.create_input_row(self.scroll_cfg, "Blacklist (sec):", self.var_target_blacklist)
        self.create_input_row(self.scroll_cfg, "Edge margin (%):", self.var_target_edge)
        ctk.CTkSwitch(self.scroll_cfg, text="ขยับหนีก่อน teleport", variable=self.var_stuck_move).pack(anchor="w", padx=20, pady=4)
        self.create_input_row(self.scroll_cfg, "Failures before TP:", self.var_stuck_attempts)
        self.create_input_row(self.scroll_cfg, "Failure window (sec):", self.var_stuck_window)
        self.create_input_row(self.scroll_cfg, "Progress distance:", self.var_target_progress)

        ctk.CTkLabel(self.scroll_cfg, text="6. Watchdog", font=("Arial", 16, "bold")).pack(anchor="w", pady=(15, 5))
        ctk.CTkSwitch(self.scroll_cfg, text="เปิด Watchdog", variable=self.var_watchdog).pack(anchor="w", padx=20, pady=4)
        self.create_input_row(self.scroll_cfg, "Worker timeout:", self.var_watchdog_worker_timeout)
        self.create_input_row(self.scroll_cfg, "Game missing timeout:", self.var_watchdog_game_timeout)
        self.create_input_row(self.scroll_cfg, "Max loop errors:", self.var_watchdog_errors)
        self.create_input_row(self.scroll_cfg, "Discord webhook:", self.var_webhook, show_char="*")
        ctk.CTkButton(
            self.scroll_cfg,
            text="บันทึก Smart Target / Watchdog",
            height=36,
            command=self.save_config,
        ).pack(pady=(15, 25))

        self.scroll_pot = ctk.CTkScrollableFrame(self.tab_potion, fg_color="transparent")
        self.scroll_pot.pack(fill="both", expand=True)

        ctk.CTkLabel(self.scroll_pot, text="[ AUTO BUFF ]", text_color="#FFD700", font=("Arial", 14, "bold")).pack(pady=(10,5))
        self.buff_rows_container = ctk.CTkFrame(self.scroll_pot, fg_color="transparent")
        self.buff_rows_container.pack(fill="x", padx=5)
        self.buff_ui_rows = []
        for key, val in self.buff_settings.items():
            self.add_buff_ui_row(key, str(val))
        ctk.CTkButton(
            self.scroll_pot,
            text="+ เพิ่มปุ่มบัพ",
            width=120,
            height=28,
            fg_color="#4b4b4b",
            hover_color="#3b3b3b",
            command=lambda: self.add_buff_ui_row("", ""),
        ).pack(pady=(5, 15))

        ctk.CTkFrame(self.scroll_pot, height=1, fg_color="#444444").pack(fill="x", padx=15, pady=(0, 10))
        ctk.CTkLabel(self.scroll_pot, text="[ SKILL ROTATION ]", text_color="#66B2FF", font=("Arial", 14, "bold")).pack(pady=(10, 5))
        ctk.CTkSwitch(self.scroll_pot, text="เปิด Skill Rotation", variable=self.var_skill_rotation).pack(anchor="w", padx=15, pady=5)
        skill_head = ctk.CTkFrame(self.scroll_pot, fg_color="transparent")
        skill_head.pack(fill="x", padx=2)
        skill_head.grid_columnconfigure((0, 1, 2, 3, 4, 5), weight=1)
        for index, text in enumerate(["เปิด", "ปุ่ม", "CD(s)", "SP>=", "มีเป้า", "ลบ"]):
            ctk.CTkLabel(skill_head, text=text, font=("Arial", 11, "bold")).grid(row=0, column=index)
        self.skill_container = ctk.CTkFrame(self.scroll_pot, fg_color="transparent", height=1)
        self.skill_container.pack(fill="x", pady=2)
        self.render_skills()
        ctk.CTkButton(
            self.scroll_pot,
            text="+ เพิ่มสกิล",
            width=120,
            height=28,
            fg_color="#2b2b2b",
            hover_color="#444444",
            command=self.add_new_skill_row,
        ).pack(pady=(5, 15))

        ctk.CTkFrame(self.scroll_pot, height=1, fg_color="#444444").pack(fill="x", padx=15, pady=(0, 10))
        ctk.CTkLabel(self.scroll_pot, text="[ ระบบฟื้นฟู HP / SP ]", text_color="#d4af37", font=("Arial", 14, "bold")).pack(pady=(10,5))
        head = ctk.CTkFrame(self.scroll_pot, fg_color="transparent"); head.pack(fill="x", padx=2)
        head.grid_columnconfigure((0,1,2,3,4,5), weight=1)
        for i, text in enumerate(["เปิด", "ชนิด", "< %", "ปุ่ม", "ดีเลย์(ms)", "ลบ"]): ctk.CTkLabel(head, text=text, font=("Arial", 12, "bold")).grid(row=0, column=i)
        self.pot_container = ctk.CTkFrame(self.scroll_pot, fg_color="transparent"); self.pot_container.pack(fill="x", pady=2)
        ctk.CTkButton(self.scroll_pot, text="+ เพิ่มไอเทม", width=120, height=28, fg_color="#2b2b2b", hover_color="#444444", command=self.add_new_potion_row).pack(pady=15)
        self.render_potions()
        ctk.CTkButton(self.scroll_pot, text="💾 บันทึกการตั้งค่ายา", height=35, font=("Arial", 13, "bold"), fg_color="#A65D1A", hover_color="#CC7722", command=self.save_config).pack(pady=10)

        # ==================== TAB 4: กล้อง (YOLO) ====================
        ctk.CTkLabel(self.tab_camera, text="Live Preview", font=("Arial", 20, "bold")).pack(pady=10)
        self.camera_label = ctk.CTkLabel(self.tab_camera, text="[รอการเชื่อมต่อภาพจากเกม]", width=420, height=320, fg_color="#1E1E1E", corner_radius=10)
        self.camera_label.pack(pady=10)

        # ==================== Status Bar ====================
        self.status_bar = ctk.CTkFrame(self, height=35, fg_color="#111111", corner_radius=0)
        self.status_bar.pack(fill="x", side="bottom")
        self.lbl_yolo_ind = ctk.CTkLabel(self.status_bar, text="👁️ เปิด", font=("Arial", 12), text_color="#00FFCC"); self.lbl_yolo_ind.pack(side="left", padx=8, pady=2)
        self.lbl_buff_ind = ctk.CTkLabel(self.status_bar, text="💪 เปิด", font=("Arial", 12), text_color="#FFD700"); self.lbl_buff_ind.pack(side="left", padx=8, pady=2)
        self.lbl_pot_ind = ctk.CTkLabel(self.status_bar, text="💊 เปิด", font=("Arial", 12), text_color="#FF6666"); self.lbl_pot_ind.pack(side="left", padx=8, pady=2)
        self.lbl_hp_status = ctk.CTkLabel(self.status_bar, text="HP: --%", font=("Arial", 12, "bold"), text_color="#FF6666"); self.lbl_hp_status.pack(side="left", padx=10, pady=2)
        self.lbl_sp_status = ctk.CTkLabel(self.status_bar, text="SP: --%", font=("Arial", 12, "bold"), text_color="#66B2FF"); self.lbl_sp_status.pack(side="left", padx=10, pady=2)
        self.lbl_status_main = ctk.CTkLabel(self.status_bar, text="สถานะ: รอโหลด...", font=("Arial", 12, "bold"), text_color="orange"); self.lbl_status_main.pack(side="right", padx=10, pady=2)

    def render_skills(self):
        if not hasattr(self, "skill_container"):
            return
        for widget in self.skill_container.winfo_children():
            widget.destroy()
        self.skill_widgets.clear()
        for index, skill in enumerate(self.skills_list):
            self.create_skill_row(index, skill)

    def create_skill_row(self, index, data):
        row = ctk.CTkFrame(self.skill_container, fg_color="transparent")
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure((0, 1, 2, 3, 4, 5), weight=1)
        enabled = ctk.BooleanVar(value=data["en"])
        key = ctk.StringVar(value=data["key"])
        cooldown = ctk.StringVar(value=str(data["cooldown"]))
        min_sp = ctk.StringVar(value=str(data["min_sp"]))
        target_only = ctk.BooleanVar(value=data["target_only"])
        ctk.CTkSwitch(row, text="", variable=enabled, width=30, switch_width=28, switch_height=14).grid(row=0, column=0)
        ctk.CTkEntry(row, textvariable=key, width=45, justify="center", height=22).grid(row=0, column=1, padx=2)
        ctk.CTkEntry(row, textvariable=cooldown, width=52, justify="center", height=22).grid(row=0, column=2, padx=2)
        ctk.CTkEntry(row, textvariable=min_sp, width=45, justify="center", height=22).grid(row=0, column=3, padx=2)
        ctk.CTkCheckBox(row, text="", variable=target_only, width=24).grid(row=0, column=4)
        ctk.CTkButton(row, text="X", width=22, height=22, fg_color="#ff4c4c", hover_color="#cc0000", command=lambda idx=index: self.delete_skill_row(idx)).grid(row=0, column=5)
        self.skill_widgets.append({
            "en": enabled,
            "key": key,
            "cooldown": cooldown,
            "min_sp": min_sp,
            "target_only": target_only,
        })

    def save_skills_to_list(self):
        self.skills_list = [{
            "en": widget["en"].get(),
            "key": widget["key"].get().strip().lower(),
            "cooldown": widget["cooldown"].get().strip(),
            "min_sp": widget["min_sp"].get().strip(),
            "target_only": widget["target_only"].get(),
        } for widget in self.skill_widgets]

    def add_new_skill_row(self):
        self.save_skills_to_list()
        self.skills_list.append({"key": "", "cooldown": "1.0", "min_sp": "10", "target_only": True, "en": True})
        self.render_skills()

    def delete_skill_row(self, index):
        self.save_skills_to_list()
        if 0 <= index < len(self.skills_list):
            self.skills_list.pop(index)
        self.render_skills()

    def render_potions(self):
        for widget in self.pot_container.winfo_children(): widget.destroy()
        self.potion_widgets.clear()
        for i, pot in enumerate(self.potions_list): self.create_potion_row(i, pot)
    def create_potion_row(self, index, data):
        f = ctk.CTkFrame(self.pot_container, fg_color="transparent"); f.pack(fill="x", pady=2); f.grid_columnconfigure((0,1,2,3,4,5), weight=1)
        en_var = ctk.BooleanVar(value=data["en"]); type_var = ctk.StringVar(value=data["type"]); pct_var = ctk.StringVar(value=str(data["pct"])); key_var = ctk.StringVar(value=data["key"]); del_var = ctk.StringVar(value=str(data["dly"]))
        ctk.CTkSwitch(f, text="", variable=en_var, width=30, switch_width=28, switch_height=14).grid(row=0, column=0)
        ctk.CTkComboBox(f, values=["HP", "SP"], variable=type_var, width=60, height=22).grid(row=0, column=1, padx=2)
        ctk.CTkEntry(f, textvariable=pct_var, width=45, justify="center", height=22).grid(row=0, column=2, padx=2)
        ctk.CTkEntry(f, textvariable=key_var, width=45, justify="center", height=22).grid(row=0, column=3, padx=2)
        ctk.CTkEntry(f, textvariable=del_var, width=55, justify="center", height=22).grid(row=0, column=4, padx=2)
        ctk.CTkButton(f, text="X", width=22, height=22, fg_color="#ff4c4c", hover_color="#cc0000", command=lambda idx=index: self.delete_potion_row(idx)).grid(row=0, column=5)
        self.potion_widgets.append({"en": en_var, "type": type_var, "pct": pct_var, "key": key_var, "del": del_var})
    def add_new_potion_row(self): self.save_potions_to_list(); self.potions_list.append({"type": "HP", "pct": "50", "key": "", "dly": "50", "en": True}); self.render_potions()
    def delete_potion_row(self, index): self.save_potions_to_list(); 0 <= index < len(self.potions_list) and self.potions_list.pop(index); self.render_potions()
    def save_potions_to_list(self): self.potions_list = [{"en": w["en"].get(), "type": w["type"].get(), "pct": w["pct"].get(), "key": w["key"].get(), "dly": w["del"].get()} for w in self.potion_widgets]

    def check_for_updates(self, silent=False):
        if "YOUR_USERNAME" in GITHUB_VERSION_URL: return
        if not silent: self.btn_update.configure(text="⏳ กำลังตรวจสอบ...", fg_color="#FFB000", state="disabled")
        threading.Thread(target=self._process_update_check, args=(silent,), daemon=True).start()
    def _process_update_check(self, silent):
        try:
            try:
                self.release_manifest = fetch_release_manifest(GITHUB_MANIFEST_URL)
                latest_version = self.release_manifest["version"]
            except Exception:
                latest_version = fetch_latest_version(GITHUB_VERSION_URL)
            if is_newer_version(latest_version, CURRENT_VERSION): self.after(0, lambda: self.prompt_update(latest_version))
            else:
                if not silent: self.after(0, lambda: self.btn_update.configure(text=f"✅ ล่าสุดแล้ว (v{CURRENT_VERSION})", fg_color="green", state="normal")); self.after(3000, lambda: self.btn_update.configure(text="🔄 ตรวจสอบเวอร์ชันใหม่", fg_color="#3B8ED0"))
        except Exception as e:
            print(f">> Update check failed: {e}")
            if not silent: self.after(0, lambda: self.btn_update.configure(text="❌ ล้มเหลว", fg_color="#FF3333", state="normal")); self.after(3000, lambda: self.btn_update.configure(text="🔄 ตรวจสอบเวอร์ชันใหม่", fg_color="#3B8ED0"))
    def prompt_update(self, latest_version): self.btn_update.configure(width=300, text=f"🚀 พบ v{latest_version} คลิกติดตั้ง!", fg_color="#CC00CC", state="normal", command=self.download_and_install)
    def download_and_install(self): self.btn_update.configure(text="📥 ห้ามปิดโปรแกรม!", fg_color="#FFB000", state="disabled"); threading.Thread(target=self._process_download, daemon=True).start()
    def _process_download(self):
        try:
            if not getattr(sys, 'frozen', False):
                raise RuntimeError("App update is available only in the packaged EXE")
            curr_exe = sys.executable
            batch_path, _ = download_app_update(
                GITHUB_DOWNLOAD_URL,
                curr_exe,
                self.release_manifest.get("exe_sha256") or GITHUB_EXE_SHA256 or None,
            )
            self.after(0, lambda: self.btn_update.configure(text="✅ รีสตาร์ท...", fg_color="green"))
            launch_updater(batch_path)
            os._exit(0)
        except Exception as e:
            print(f">> App update download failed: {e}")
            self.after(0, lambda: self.btn_update.configure(text="❌ ดาวน์โหลดล้มเหลว", fg_color="#FF3333", state="normal"))

    def update_hotkey_display(self):
        self.lbl_hotkeys.configure(text=f"📌 Hotkey หลัก:\n• [{self.hk_master.upper()}] เริ่ม/หยุด ทำงานทั้งหมด (Master Toggle)")
        
    def add_buff_ui_row(self, k, v):
        r = ctk.CTkFrame(self.buff_rows_container, fg_color="transparent"); r.pack(fill="x", pady=2)
        kv, vv = ctk.StringVar(value=k), ctk.StringVar(value=v)
        ctk.CTkLabel(r, text="ปุ่ม:").pack(side="left", padx=5); ctk.CTkEntry(r, textvariable=kv, width=50, justify="center").pack(side="left", padx=5)
        ctk.CTkLabel(r, text="วิ:").pack(side="left", padx=5); ctk.CTkEntry(r, textvariable=vv, width=60, justify="center").pack(side="left", padx=5)
        ctk.CTkButton(r, text="ลบ", width=40, fg_color="#CC0000", command=lambda f=r: self.remove_buff_ui_row(f)).pack(side="right", padx=5)
        self.buff_ui_rows.append({'frame': r, 'key_var': kv, 'val_var': vv})
    def remove_buff_ui_row(self, f): f.destroy(); self.buff_ui_rows = [row for row in self.buff_ui_rows if row['frame'] != f]
    def create_input_row(self, p, l, v, show_char=""): 
        f = ctk.CTkFrame(p, fg_color="transparent"); f.pack(fill="x", pady=2)
        ctk.CTkLabel(f, text=l, width=145, anchor="e").pack(side="left", padx=5); ctk.CTkEntry(f, textvariable=v, width=195, show=show_char).pack(side="left", padx=5)

    def save_config(self, silent=False):
        global config, config_file
        config['GENERAL'] = {
            'GameWindowTitle': self.var_game_title.get(), 
            'OffsetPx': self.var_offset.get(),
            'StopOnDeath': str(self.var_stop_on_death.get()),
            'YoloEnabled': str(self.var_yolo_en.get()),
            'BuffEnabled': str(self.var_buff_en.get()),
            'PotionEnabled': str(self.var_pot_en.get()),
            'SkillRotationEnabled': str(self.var_skill_rotation.get()),
            'AttackClick': str(self.var_attack_click.get()),
            'AttackIntervalSec': self.var_attack_interval.get().strip(),
            'RareItemAction': self.var_rare_action.get().strip(),
            'RareItemKey': self.var_rare_key.get().strip().lower(),
            'MouseDevice': self.var_mouse_device.get().strip(),
            'KeyboardDevice': self.var_keyboard_device.get().strip()
        }
        config['AI_CONFIDENCE'] = {'ConfMonster': self.var_conf_monster.get().strip(), 'ConfRareItem': self.var_conf_rare.get().strip()}
        config['HOTKEYS'] = {'MasterToggle': self.var_hk_master.get().strip().lower()}
        
        config['TELEPORT'] = {
            'EnableTeleport': str(self.var_enable_teleport.get()), 
            'TeleportMode': self.var_teleport_mode.get(), 
            'TeleportKey': self.var_teleport_key.get().strip().lower(), 
            'WaitTimeSec': self.var_teleport_wait.get().strip(),
            'EnableAutoTpStuck': str(self.var_auto_tp_stuck.get()),
            'AutoTpStuckSec': self.var_auto_tp_stuck_sec.get().strip()
        }

        config['SMART_TARGET'] = {
            'Enabled': str(self.var_smart_target.get()),
            'BlacklistSec': self.var_target_blacklist.get().strip(),
            'EdgeMarginPct': self.var_target_edge.get().strip(),
        }
        config['STUCK_RECOVERY'] = {
            'MoveBeforeTeleport': str(self.var_stuck_move.get()),
            'AttemptsBeforeTeleport': self.var_stuck_attempts.get().strip(),
            'FailureWindowSec': self.var_stuck_window.get().strip(),
            'ProgressDistancePx': self.var_target_progress.get().strip(),
        }
        config['WATCHDOG'] = {
            'Enabled': str(self.var_watchdog.get()),
            'WorkerTimeoutSec': self.var_watchdog_worker_timeout.get().strip(),
            'GameMissingTimeoutSec': self.var_watchdog_game_timeout.get().strip(),
            'MaxLoopErrors': self.var_watchdog_errors.get().strip(),
            'DiscordWebhookURL': self.var_webhook.get().strip(),
            'NotifyCooldownSec': str(self.notify_cooldown),
        }
        
        self.save_potions_to_list(); config.remove_section('POTIONS'); config.add_section('POTIONS')
        for i, p in enumerate(self.potions_list): config['POTIONS'][str(i)] = f"{p['type']},{p['pct']},{p['key']},{p['dly']},{p['en']}"
        if 'AUTO_BUFF_ITEM' in config: config.remove_section('AUTO_BUFF_ITEM')
        config.add_section('AUTO_BUFF_ITEM')
        for row in self.buff_ui_rows:
            k, v = row['key_var'].get().strip().lower(), row['val_var'].get().strip()
            if k and v: config['AUTO_BUFF_ITEM'][k] = v

        self.save_skills_to_list()
        if 'SKILL_ROTATION' in config:
            config.remove_section('SKILL_ROTATION')
        config.add_section('SKILL_ROTATION')
        for index, skill in enumerate(self.skills_list):
            if not skill['key']:
                continue
            config['SKILL_ROTATION'][str(index)] = (
                f"{skill['key']},{skill['cooldown']},{skill['min_sp']},"
                f"{skill['target_only']},{skill['en']}"
            )
            
        try:
            save_profile(config, config_file)
        except OSError as error:
            print(f">> Config save failed ({config_file}): {error}")
            if not silent:
                self.btn_save.configure(text="Save failed", fg_color="#FF3333")
            return False
        self.update_runtime_vars(); self.update_hotkey_display() 
        
        if not silent:
            self.btn_save.configure(text="✅ บันทึกสำเร็จ!", fg_color="green")
            self.after(2000, lambda: self.btn_save.configure(text="💾 บันทึกการตั้งค่า (Save)", fg_color=["#3B8ED0", "#1F6AA5"]))
            print(f">> 💾 บันทึกข้อมูลลง {self.active_profile} สำเร็จ!")

        print(f">> Config file: {config_file}")
        return True

    def check_key_pressed(self, k):
        try: return k and keyboard.is_pressed(k)
        except Exception: return False
        
    def hotkey_listener(self):
        was_pressed = False
        while not self.shutdown_event.is_set():
            is_pressed = self.check_key_pressed(self.hk_master)
            if is_pressed and not was_pressed:
                self.after(0, self.toggle_master)
            was_pressed = is_pressed
            time.sleep(0.05)

    def on_close(self):
        try:
            self.save_config(silent=True)
        except Exception as error:
            print(f">> Config save on exit failed: {error}")
        self.shutdown_event.set()
        self.stop_bot("application closed", update_ui=False)
        self.destroy()

    def stop_bot(self, reason="stopped", update_ui=True):
        if self.run_event is not None:
            self.run_event.set()
        self.running = False
        self.combat_active = False
        if update_ui:
            self.lbl_status_main.configure(text="Status: stopped", text_color="#FF5555")
            print(f">> Bot stopped: {reason}")

    def stop_run_if_current(self, run_event, reason):
        if self.run_event is run_event:
            self.stop_bot(reason)

    def _model_sha256(self):
        digest = hashlib.sha256()
        with open(app_path("best.pt"), "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def run_preflight(self, require_active=True):
        results = []
        required_sections = (
            "GENERAL",
            "AI_CONFIDENCE",
            "TELEPORT",
            "SMART_TARGET",
            "STUCK_RECOVERY",
            "WATCHDOG",
        )
        missing_sections = [section for section in required_sections if not config.has_section(section)]
        results.append(PreflightResult(
            "Config",
            not missing_sections,
            "profile is valid" if not missing_sections else f"missing: {', '.join(missing_sections)}",
        ))

        driver_ready = interception_driver_ready()
        results.append(PreflightResult(
            "Interception driver",
            driver_ready,
            "driver context is ready" if driver_ready else "driver is unavailable",
        ))
        if driver_ready:
            try:
                keyboard_device, keyboard_hwid = configure_keyboard_device(self.keyboard_device)
                results.append(PreflightResult(
                    "Keyboard", bool(keyboard_hwid),
                    f"device {keyboard_device}: {keyboard_hwid or 'HWID unavailable'}",
                ))
            except Exception as error:
                results.append(PreflightResult("Keyboard", False, str(error)))
            try:
                mouse_device, mouse_detail = configure_working_mouse_device(self.mouse_device)
                results.append(PreflightResult("Mouse", True, f"device {mouse_device}: {mouse_detail}"))
            except Exception as error:
                results.append(PreflightResult("Mouse", False, str(error)))

        model_file = app_path("best.pt")
        model_exists = os.path.isfile(model_file) and os.path.getsize(model_file) > 0
        if not self.model_validated or self.model is None:
            self.model_validated = self.load_model_with_recovery()
            model_exists = os.path.isfile(model_file) and os.path.getsize(model_file) > 0
        results.append(PreflightResult(
            "AI model file", model_exists, model_file if model_exists else "best.pt is missing",
        ))
        results.append(PreflightResult(
            "AI inference",
            self.model_validated and self.model is not None,
            "load and test inference passed" if self.model_validated else "recovery failed",
        ))
        expected_hash = self._expected_model_hash()
        if model_exists and expected_hash:
            hash_matches = self._model_sha256().lower() == expected_hash.lower()
            results.append(PreflightResult(
                "AI model hash", hash_matches,
                "release model" if hash_matches else "custom model or release hash mismatch",
                required=False,
            ))

        window_info = get_window_debug_info(self.game_title)
        window_found = bool(window_info["game_hwnd"])
        results.append(PreflightResult(
            "Game window", window_found,
            window_info["game_title"] if window_found else f"not found: {self.game_title}",
        ))
        rect = self.get_window_rect() if window_found else None
        results.append(PreflightResult(
            "Capture area", bool(rect),
            f"{rect['width']}x{rect['height']}" if rect else "window is too small or unavailable",
        ))
        if require_active:
            results.append(PreflightResult(
                "Foreground", bool(window_info["active"]),
                "game is active" if window_info["active"] else f"active window: {window_info['foreground_title']}",
            ))

        print(">> Preflight check")
        for result in results:
            level = "PASS" if result.passed else ("FAIL" if result.required else "WARN")
            print(f">> [{level}] {result.name}: {result.detail}")
        return results

    def run_preflight_ui(self):
        if self._preflight_running or self.running:
            return
        self._preflight_running = True
        self.btn_preflight.configure(state="disabled", text="กำลังตรวจสอบ...")
        try:
            results = self.run_preflight(require_active=False)
            passed = all(result.passed for result in results if result.required)
            self.lbl_status_main.configure(
                text="Status: system ready" if passed else "Status: preflight failed",
                text_color="#55FF55" if passed else "#FF5555",
            )
        finally:
            self._preflight_running = False
            self.btn_preflight.configure(state="normal", text="ตรวจสอบระบบ")

    def start_bot(self):
        preflight = self.run_preflight(require_active=True)
        if not all(result.passed for result in preflight if result.required):
            self.lbl_status_main.configure(text="Status: preflight failed", text_color="#FF5555")
            return
        window_info = get_window_debug_info(self.game_title)
        print(f">> Input target: {window_info['game_title']!r} (HWND {window_info['game_hwnd']})")

        self.run_event = threading.Event()
        self.running = True
        self.combat_active = False
        self.run_started_at = time.monotonic()
        self.worker_heartbeats = {
            "bot": self.run_started_at,
            "potion": self.run_started_at,
            "skill": self.run_started_at,
        }
        self.worker_errors = {"bot": 0, "potion": 0, "skill": 0}
        self.worker_error_times = {
            "bot": deque(),
            "potion": deque(),
            "skill": deque(),
            "input": deque(),
        }
        self.lbl_status_main.configure(text="Status: running", text_color="#55FF55")
        print(">> Bot started")
        self.worker_threads = [
            threading.Thread(target=self.bot_main_loop, args=(self.run_event,), daemon=True),
            threading.Thread(target=self.potion_loop, args=(self.run_event,), daemon=True),
            threading.Thread(target=self.skill_rotation_loop, args=(self.run_event,), daemon=True),
        ]
        if self.watchdog_enabled:
            self.worker_threads.append(
                threading.Thread(target=self.watchdog_loop, args=(self.run_event,), daemon=True)
            )
        for worker in self.worker_threads:
            worker.start()

    def toggle_master(self):
        if self.running:
            self.stop_bot("master toggle")
        else:
            self.start_bot()

    def toggle_yolo_switch(self): 
        self.yolo_enabled = self.var_yolo_en.get()
        self.lbl_yolo_ind.configure(text="👁️ เปิด" if self.yolo_enabled else "👁️ ปิด", text_color="#00FFCC" if self.yolo_enabled else "#7A7A7A")
        print(f">> 👁️ ตาวิเศษ (YOLO): {'ON' if self.yolo_enabled else 'OFF'}")
        if not self.yolo_enabled:
            blank = Image.new("RGB", (420, 320), (30, 30, 30)); ctk_b = ctk.CTkImage(light_image=blank, dark_image=blank, size=(420, 320))
            self.camera_label.configure(image=ctk_b, text="❌ ตาวิเศษถูกปิดการทำงาน"); self.camera_label.image = ctk_b 

    def toggle_buff_switch(self): 
        self.buff_enabled = self.var_buff_en.get()
        self.lbl_buff_ind.configure(text="💪 เปิด" if self.buff_enabled else "💪 ปิด", text_color="#FFD700" if self.buff_enabled else "#7A7A7A")
        print(f">> 💪 ออโต้บัพ: {'ON' if self.buff_enabled else 'OFF'}")

    def toggle_pot_switch(self): 
        self.potion_enabled = self.var_pot_en.get()
        self.lbl_pot_ind.configure(text="💊 เปิด" if self.potion_enabled else "💊 ปิด", text_color="#FF6666" if self.potion_enabled else "#7A7A7A")
        print(f">> 💊 ปั้มยา: {'ON' if self.potion_enabled else 'OFF'}")

    def human_press(self, key_str):
        try:
            success = safe_press(self.game_title, key_str, key_lock)
            if not success:
                self.log_throttled("inactive_game", ">> Skip key press: game window is not active")
            return success
        except Exception as e:
            self.record_worker_error("input", e)
            self.log_throttled("key_press_error", f">> Key press error: {e}")
            return False

    def human_click(self, x, y):
        try:
            success = safe_click(self.game_title, x, y, input_lock=key_lock)
            if not success:
                self.log_throttled("inactive_game", ">> Skip click: game window is not active")
            return success
        except Exception as e:
            self.record_worker_error("input", e)
            self.log_throttled("mouse_click_error", f">> Mouse click error: {e}")
            return False

    def log_throttled(self, key, message, interval=5.0):
        now = time.monotonic()
        if now - self._log_times.get(key, 0.0) >= interval:
            self._log_times[key] = now
            print(message)

    def touch_worker(self, name):
        self.worker_heartbeats[name] = time.monotonic()

    def record_worker_error(self, name, error):
        now = time.monotonic()
        self.worker_heartbeats[name] = now
        error_times = self.worker_error_times.setdefault(name, deque())
        error_times.append(now)
        while error_times and now - error_times[0] > 30.0:
            error_times.popleft()
        self.worker_errors[name] = len(error_times)
        self.log_throttled(
            f"{name}_loop_error",
            f">> {name} loop error ({self.worker_errors[name]}): {error}",
            interval=2.0,
        )

    def potion_loop(self, run_event):
        last_tracker = {}; last_ui_hp, last_ui_sp = -1, -1
        hp_samples, sp_samples = deque(maxlen=3), deque(maxlen=3)
        low_counts = {}
        hp_len, sp_len = HP_END_X - HP_START_X, SP_END_X - SP_START_X
        with mss.mss() as sct:
            while not run_event.is_set():
                try:
                    time.sleep(0.02)
                    self.touch_worker("potion")
                    if not game_is_active(self.game_title):
                        time.sleep(0.2)
                        continue
                    hwnd = get_window_handle(self.game_title)
                    if not hwnd: continue
                    r = win32gui.GetWindowRect(hwnd)
                    
                    if (r[2]-r[0]) < HP_END_X or (r[3]-r[1]) < SP_Y:
                        continue
                    
                    img_hp = np.array(sct.grab({"top": int(r[1]+HP_Y), "left": int(r[0]+HP_START_X), "width": hp_len, "height": 1}))
                    img_sp = np.array(sct.grab({"top": int(r[1]+SP_Y), "left": int(r[0]+SP_START_X), "width": sp_len, "height": 1}))
                    px_hp = 0
                    for x in range(hp_len-1, -1, -1):
                        b,g,r_val,_ = img_hp[0][x]
                        if is_color_match(r_val,g,b, HP_COLOR, TOLERANCE) or is_color_match(r_val,g,b, HP_COLOR_RED, TOLERANCE): px_hp = x + 1; break
                    raw_hp = int((px_hp/hp_len)*100)
                    px_sp = 0
                    for x in range(sp_len-1, -1, -1):
                        b,g,r_val,_ = img_sp[0][x]
                        if is_color_match(r_val,g,b, SP_COLOR, TOLERANCE) or is_color_match(r_val,g,b, SP_COLOR_RED, TOLERANCE): px_sp = x + 1; break
                    raw_sp = int((px_sp/sp_len)*100)
                    if raw_hp > 1: hp_samples.append(raw_hp)
                    if raw_sp > 1: sp_samples.append(raw_sp)
                    pct_hp = int(np.median(hp_samples)) if hp_samples else raw_hp
                    pct_sp = int(np.median(sp_samples)) if sp_samples else raw_sp
                    if pct_hp != last_ui_hp: self.after(0, lambda v=pct_hp: self.lbl_hp_status.configure(text=f"HP: {v}%")); last_ui_hp = pct_hp
                    if pct_sp != last_ui_sp: self.after(0, lambda v=pct_sp: self.lbl_sp_status.configure(text=f"SP: {v}%")); last_ui_sp = pct_sp
                    with self.vitals_lock:
                        self.latest_vitals = {"hp": pct_hp, "sp": pct_sp, "updated": time.monotonic()}
                    
                    if not self.potion_enabled: continue

                    action = select_potion_action(
                        list(self.potions_list), pct_hp, pct_sp, last_tracker, time.monotonic()
                    )
                    if action:
                        _, potion, tracker_key = action
                        low_counts[tracker_key] = low_counts.get(tracker_key, 0) + 1
                        if low_counts[tracker_key] >= 2 and self.human_press(potion["key"]):
                            last_tracker[tracker_key] = time.monotonic()
                            low_counts[tracker_key] = 0
                except Exception as e:
                    self.record_worker_error("potion", e)
                    time.sleep(0.2)

    def skill_rotation_loop(self, run_event):
        last_cast = {}
        last_global_cast = 0.0
        while not run_event.is_set():
            try:
                time.sleep(0.05)
                self.touch_worker("skill")
                if not self.skill_rotation_enabled or not game_is_active(self.game_title):
                    continue
                with self.vitals_lock:
                    sp_percent = self.latest_vitals["sp"]
                now = time.monotonic()
                if now - last_global_cast < 0.15:
                    continue
                action = select_due_skill(
                    list(self.skills_list), sp_percent, self.combat_active, last_cast, now
                )
                if not action:
                    continue
                _, skill, tracker_key = action
                if self.human_press(skill["key"]):
                    last_cast[tracker_key] = time.monotonic()
                    last_global_cast = last_cast[tracker_key]
                    print(f">> Skill rotation: [{skill['key'].upper()}]")
            except Exception as error:
                self.record_worker_error("skill", error)
                time.sleep(0.2)

    def capture_alert_screenshot(self):
        try:
            monitor = self.get_window_rect()
            if not monitor:
                return None
            alert_dir = app_path("alerts")
            os.makedirs(alert_dir, exist_ok=True)
            filename = datetime.now().strftime("alert-%Y%m%d-%H%M%S.png")
            path = os.path.join(alert_dir, filename)
            with mss.mss() as sct:
                image = cv2.cvtColor(np.array(sct.grab(monitor)), cv2.COLOR_BGRA2BGR)
            return path if cv2.imwrite(path, image) else None
        except Exception as error:
            self.log_throttled("alert_screenshot", f">> Alert screenshot failed: {error}")
            return None

    def send_notification(self, message, with_screenshot=True):
        now = time.monotonic()
        notification_key = message.split(":", 1)[0]
        if now - self._watchdog_alert_times.get(notification_key, 0.0) < self.notify_cooldown:
            return None
        self._watchdog_alert_times[notification_key] = now
        screenshot = self.capture_alert_screenshot() if with_screenshot else None
        print(f">> ALERT: {message}")
        if not self.discord_webhook_url:
            return screenshot
        try:
            content = f"AI looknam Promax v{CURRENT_VERSION}: {message}"
            if screenshot:
                with open(screenshot, "rb") as image_file:
                    response = requests.post(
                        self.discord_webhook_url,
                        data={"content": content},
                        files={"file": (os.path.basename(screenshot), image_file, "image/png")},
                        timeout=10,
                    )
            else:
                response = requests.post(
                    self.discord_webhook_url, json={"content": content}, timeout=10
                )
            response.raise_for_status()
            print(">> Discord notification sent")
        except Exception as error:
            self.log_throttled("webhook_error", f">> Discord notification failed: {error}")
        return screenshot

    def watchdog_alert(self, run_event, reason):
        screenshot = self.send_notification(f"Watchdog: {reason}")
        if screenshot:
            print(f">> Watchdog screenshot: {screenshot}")
        run_event.set()
        self.after(0, self.stop_run_if_current, run_event, f"watchdog: {reason}")

    def watchdog_loop(self, run_event):
        game_missing_since = None
        invalid_vitals_since = None
        while not run_event.wait(1.0):
            try:
                now = time.monotonic()
                for worker_name, error_times in list(self.worker_error_times.items()):
                    while error_times and now - error_times[0] > 30.0:
                        error_times.popleft()
                    self.worker_errors[worker_name] = len(error_times)
                health_error = evaluate_worker_health(
                    now,
                    {
                        worker_name: self.worker_heartbeats.get(worker_name, self.run_started_at)
                        for worker_name in ("bot", "potion", "skill")
                    },
                    self.worker_errors,
                    self.watchdog_worker_timeout,
                    self.watchdog_max_errors,
                )
                if health_error:
                    self.watchdog_alert(run_event, health_error)
                    return

                if not get_window_handle(self.game_title):
                    game_missing_since = game_missing_since or now
                    if now - game_missing_since >= self.watchdog_game_timeout:
                        self.watchdog_alert(run_event, "game window disappeared")
                        return
                else:
                    game_missing_since = None

                if self.model is None or not self.model_validated:
                    self.watchdog_alert(run_event, "AI model is unavailable")
                    return

                if self.potion_enabled and game_is_active(self.game_title):
                    with self.vitals_lock:
                        vitals_updated = self.latest_vitals["updated"]
                        hp_percent = self.latest_vitals["hp"]
                        sp_percent = self.latest_vitals["sp"]
                    vitals_reference = vitals_updated or self.run_started_at
                    if now - vitals_reference > self.watchdog_worker_timeout * 2:
                        self.watchdog_alert(run_event, "HP/SP reading is stale")
                        return
                    if hp_percent in (None, 0) and sp_percent in (None, 0):
                        invalid_vitals_since = invalid_vitals_since or now
                        if now - invalid_vitals_since > self.watchdog_worker_timeout * 2:
                            self.watchdog_alert(run_event, "HP/SP bars cannot be read")
                            return
                    else:
                        invalid_vitals_since = None
                else:
                    invalid_vitals_since = None
            except Exception as error:
                self.record_worker_error("watchdog", error)

    def get_window_rect(self):
        return get_safe_window_rect(self.game_title, self.offset_px)
        
    def update_camera_ui(self, img_arr):
        try:
            rgb = cv2.cvtColor(img_arr, cv2.COLOR_BGR2RGB); pil = Image.fromarray(rgb)
            ctk_img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(420, 320)); self.camera_label.configure(image=ctk_img, text=""); self.camera_label.image = ctk_img
        except Exception as e:
            print(f">> Camera preview error: {e}")

    def bot_main_loop(self, run_event):
        now = time.monotonic()
        target_manager = SmartTargetManager(
            blacklist_seconds=self.target_blacklist_sec if self.smart_target_enabled else 1.0,
            edge_margin_ratio=self.target_edge_margin if self.smart_target_enabled else 0.0,
        )
        stuck_manager = StuckRecoveryManager(
            attempts_before_teleport=self.stuck_attempts_before_tp,
            failure_window_seconds=self.stuck_failure_window,
        )
        engagement_timer = EngagementTimer(missing_reset_seconds=0.5)
        last_buff_cast = {}
        last_death_check = last_move_log = last_rare_alert = last_preview = 0.0
        last_tp_check = target_started = last_attack = now
        target_progress_anchor = None
        death_hits = 0
        target_hits = 0
        rare_hits = 0
        pause_started = None
        recovery_grace_until = 0.0

        with mss.mss() as sct:
            while not run_event.is_set():
                try:
                    self.touch_worker("bot")
                    if not game_is_active(self.game_title):
                        if pause_started is None:
                            pause_started = time.monotonic()
                            self.after(0, lambda: self.lbl_status_main.configure(text="Status: paused (game inactive)", text_color="orange"))
                        time.sleep(0.2)
                        continue

                    current_time = time.monotonic()
                    if pause_started is not None:
                        paused_for = current_time - pause_started
                        last_tp_check += paused_for
                        target_started += paused_for
                        engagement_timer.shift(paused_for)
                        last_attack += paused_for
                        last_death_check += paused_for
                        last_rare_alert += paused_for
                        last_buff_cast = {key: value + paused_for for key, value in last_buff_cast.items()}
                        pause_started = None
                        self.after(0, lambda: self.lbl_status_main.configure(text="Status: running", text_color="#55FF55"))

                    monitor = self.get_window_rect()
                    if not monitor:
                        time.sleep(0.5)
                        continue
                    center_x = monitor["left"] + monitor["width"] // 2
                    center_y = monitor["top"] + monitor["height"] // 2 - 1

                    if self.buff_enabled:
                        for key, cooldown in list(self.buff_settings.items()):
                            if current_time - last_buff_cast.get(key, 0.0) >= cooldown:
                                safe_move_to(self.game_title, center_x, center_y)
                                time.sleep(0.2)
                                if self.human_press(key):
                                    print(f">> Buff: [{key.upper()}]")
                                    last_buff_cast[key] = time.monotonic()
                                    last_tp_check = time.monotonic()

                    img_bgr = cv2.cvtColor(np.array(sct.grab(monitor)), cv2.COLOR_BGRA2BGR)

                    if self.template_dead is not None and current_time - last_death_check >= 0.75:
                        is_dead = False
                        if (img_bgr.shape[0] >= self.template_dead.shape[0] and
                                img_bgr.shape[1] >= self.template_dead.shape[1]):
                            score = np.max(cv2.matchTemplate(img_bgr, self.template_dead, cv2.TM_CCOEFF_NORMED))
                            is_dead = score >= 0.8
                        death_hits = death_hits + 1 if is_dead else 0
                        last_death_check = current_time
                        if death_hits >= 2:
                            print(">> Character death confirmed")
                            threading.Thread(
                                target=self.send_notification,
                                args=("Character death confirmed",),
                                daemon=True,
                            ).start()
                            death_hits = 0
                            if self.stop_on_death:
                                run_event.set()
                                self.after(0, self.stop_run_if_current, run_event, "death detected")
                                break

                    if not self.yolo_enabled:
                        time.sleep(0.1)
                        continue

                    results = self.model(img_bgr, verbose=False, conf=min(self.conf_monster, self.conf_rare))
                    if current_time - last_preview >= 0.1:
                        self.after(0, self.update_camera_ui, results[0].plot())
                        last_preview = current_time

                    monsters = []
                    rare_detected = None
                    for result in results:
                        for box in result.boxes:
                            name = self.model.names[int(box.cls[0])]
                            confidence = float(box.conf[0])
                            local_x = int(box.xywh[0][0])
                            local_y = int(box.xywh[0][1])
                            screen_x = monitor["left"] + local_x
                            screen_y = monitor["top"] + local_y
                            if name == "rare_item" and confidence >= self.conf_rare:
                                rare_detected = (screen_x, screen_y, confidence)
                            elif name == "MONS" and confidence >= self.conf_monster:
                                monsters.append((screen_x, screen_y, confidence))

                    rare_hits = rare_hits + 1 if rare_detected else 0
                    if rare_detected and rare_hits >= 2 and current_time - last_rare_alert >= 15.0:
                        _, _, confidence = rare_detected
                        print(f">> Rare item detected ({confidence * 100:.1f}%)")
                        threading.Thread(
                            target=self.send_notification,
                            args=(f"Rare item detected ({confidence * 100:.1f}%)",),
                            daemon=True,
                        ).start()
                        last_rare_alert = current_time
                        if self.rare_item_action == "Stop":
                            run_event.set()
                            self.after(0, self.stop_run_if_current, run_event, "rare item detected")
                            break
                        if self.rare_item_action == "Key" and self.rare_item_key:
                            self.human_press(self.rare_item_key)

                    target = None
                    if monsters:
                        decision = target_manager.select(
                            monsters, (center_x, center_y), monitor["width"], monitor["height"], current_time
                        )
                        target = decision.target
                        if target is not None and decision.is_new:
                            target_hits = 1
                            target_progress_anchor = (target[0], target[1])
                        elif target is not None:
                            target_hits += 1
                            if target_progress_anchor is None:
                                target_progress_anchor = (target[0], target[1])
                            progress_sq = (target[0] - target_progress_anchor[0]) ** 2 + (
                                target[1] - target_progress_anchor[1]
                            ) ** 2
                            if progress_sq >= self.target_progress_px ** 2:
                                target_progress_anchor = (target[0], target[1])
                        if target is not None:
                            last_tp_check = current_time
                        else:
                            target_hits = 0
                            target_progress_anchor = None
                    else:
                        target_manager.clear_lock()
                        target_hits = 0
                    engagement_seconds = engagement_timer.observe(target is not None, current_time)

                    self.combat_active = target is not None

                    if target is not None:
                        if target_hits < 2:
                            continue
                        target_x, target_y, _ = target
                        if self.auto_tp_stuck and engagement_seconds >= self.auto_tp_stuck_sec:
                            target_manager.mark_failed(target, current_time)
                            recovery = stuck_manager.register_failure(
                                target, (center_x, center_y), monitor, current_time
                            )
                            print(
                                f">> Target timeout; blacklisted for {self.target_blacklist_sec:.0f}s "
                                f"(recovery {recovery.recent_failures}/{self.stuck_attempts_before_tp})"
                            )
                            if recovery.action == "reposition" and self.stuck_move_enabled and recovery.escape_point:
                                if self.human_click(*recovery.escape_point):
                                    print(f">> Stuck recovery: reposition to {recovery.escape_point}")
                                    time.sleep(0.5)
                                    recovery_grace_until = current_time + max(2.0, self.teleport_wait)
                            elif recovery.action == "teleport" and self.auto_tp_stuck:
                                safe_move_to(self.game_title, center_x, center_y)
                                time.sleep(0.1)
                                if self.human_press(self.teleport_key):
                                    if self.teleport_mode == "Skill":
                                        time.sleep(0.2)
                                        self.human_press("enter")
                                    print(">> Stuck recovery: teleport")
                            last_tp_check = target_started = time.monotonic()
                            engagement_timer.reset()
                            target_hits = 0
                            target_progress_anchor = None
                            self.combat_active = False
                            continue

                        if current_time - last_move_log >= 1.5:
                            print(f">> Target locked at X:{target_x}, Y:{target_y}")
                            last_move_log = current_time
                        safe_move_to(self.game_title, target_x, target_y)
                        if self.attack_click and current_time - last_attack >= self.attack_interval:
                            if self.human_click(target_x, target_y):
                                last_attack = time.monotonic()
                        time.sleep(0.05)
                    else:
                        safe_move_to(self.game_title, center_x, center_y)
                        if (
                            self.enable_teleport
                            and current_time >= recovery_grace_until
                            and current_time - last_tp_check >= self.teleport_wait
                        ):
                            print(f">> No monster for {self.teleport_wait:.1f}s; teleporting")
                            if self.human_press(self.teleport_key):
                                if self.teleport_mode == "Skill":
                                    time.sleep(0.2)
                                    self.human_press("enter")
                                last_tp_check = target_started = time.monotonic()
                                time.sleep(0.2)
                except Exception as e:
                    self.record_worker_error("bot", e)
                    time.sleep(0.5)

if __name__ == "__main__":
    app = BotApp(); app.mainloop()
