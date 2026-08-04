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
from collections import deque
from PIL import Image
from ultralytics import YOLO
from bot_utils import (
    clamp_float,
    clamp_int,
    configure_working_mouse_device,
    download_file,
    game_is_active,
    get_safe_window_rect,
    get_window_debug_info,
    get_window_handle,
    parse_bool,
    parse_device_id,
    safe_click,
    safe_move_to,
    safe_press,
    select_locked_target,
)
from config_manager import load_master, load_profile, save_profile
from updater import (
    download_app_update,
    fetch_latest_version,
    fetch_release_manifest,
    is_newer_version,
    launch_updater,
)

# =========================================================
# 🌟 ตั้งค่า Auto-Update 
# =========================================================
CURRENT_VERSION = "2.6.2"
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/looknamx/SET/main/version.txt"
GITHUB_MANIFEST_URL = "https://raw.githubusercontent.com/looknamx/SET/main/release_manifest.json"
GITHUB_DOWNLOAD_URL = "https://github.com/looknamx/SET/releases/latest/download/150469.exe" 
GITHUB_MODEL_URL = "https://github.com/looknamx/SET/releases/latest/download/best.pt" 
GITHUB_EXE_SHA256 = ""
GITHUB_MODEL_SHA256 = ""

APP_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)


def app_path(filename):
    return os.path.join(APP_DIR, filename)


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
config_file = app_path('config_Profile_1.ini')

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
        self.ui_scale = 1.0 
        self.active_profile = "Profile 1"
        self._log_times = {}
        self.release_manifest = {}

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
        self.master_file = app_path('master.ini')
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
        config_file = app_path(f"config_{self.active_profile.replace(' ', '_')}.ini")
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

    def check_and_download_model(self):
        model_file = app_path("best.pt")
        try:
            self.release_manifest = fetch_release_manifest(GITHUB_MANIFEST_URL)
        except Exception as e:
            print(f">> Release manifest unavailable; using compatibility mode: {e}")
        if not os.path.exists(model_file):
            self.after(0, lambda: self.lbl_status_main.configure(text="สถานะ: 📥 กำลังโหลดโมเดล AI...", text_color="#FFB000"))
            print(">> 📥 ไม่พบไฟล์โมเดล AI ในเครื่อง กำลังดาวน์โหลดอัตโนมัติ... (อาจใช้เวลา 1-2 นาที ห้ามปิดโปรแกรม)")
            self.update() 
            try:
                expected_hash = self.release_manifest.get("model_sha256") or GITHUB_MODEL_SHA256 or None
                download_file(GITHUB_MODEL_URL, model_file, expected_hash)
                print(">> ✅ ดาวน์โหลดไฟล์ best.pt สำเร็จ!")
                return True
            except Exception as e:
                print(f">> ❌ โหลดโมเดลล้มเหลว (เช็คอินเทอร์เน็ต หรือลิงก์ GITHUB_MODEL_URL): {e}")
                self.lbl_status_main.configure(text="สถานะ: 🔴 โหลด AI ล้มเหลว", text_color="red")
                return False
        return True

    def init_bot_systems(self):
        self.lbl_status_main.configure(text="สถานะ: 🟡 กำลังเตรียม AI...", text_color="orange")
        self.update()
        if not self.check_and_download_model(): return
        try:
            self.model = YOLO(app_path('best.pt'))
            keyboard_device = parse_device_id(self.keyboard_device, 0, 9)
            if keyboard_device is not None:
                interception.set_devices(keyboard=keyboard_device)
                print(f">> Keyboard device: {keyboard_device} (manual setting)")
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
            if os.path.exists(model_file):
                self.model = None; gc.collect(); time.sleep(1)
                print(">> เตรียมแทนที่โมเดลเดิมเมื่อดาวน์โหลดไฟล์ใหม่สำเร็จ")
            self.after(0, lambda: self.lbl_status_main.configure(text="สถานะ: 📥 กำลังโหลดโมเดล AI...", text_color="#FFB000"))
            print(">> 📥 กำลังดาวน์โหลดโมเดล AI ใหม่ล่าสุดจาก GitHub... (ห้ามปิดโปรแกรม)")
            expected_hash = self.release_manifest.get("model_sha256") or GITHUB_MODEL_SHA256 or None
            download_file(GITHUB_MODEL_URL, model_file, expected_hash)
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
            self.model = YOLO(app_path('best.pt'))
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
            
        self.save_config(silent=True) 
        self.active_profile = choice
        self.master_cfg['GLOBAL']['ActiveProfile'] = self.active_profile
        with open(self.master_file, 'w', encoding='utf-8') as f: self.master_cfg.write(f)
        
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
        
        for widget in self.buff_rows_container.winfo_children(): widget.destroy()
        self.buff_ui_rows.clear()
        for key, val in self.buff_settings.items(): self.add_buff_ui_row(key, str(val))
        
        self.render_potions()
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
        
        self.save_potions_to_list(); config.remove_section('POTIONS'); config.add_section('POTIONS')
        for i, p in enumerate(self.potions_list): config['POTIONS'][str(i)] = f"{p['type']},{p['pct']},{p['key']},{p['dly']},{p['en']}"
        if 'AUTO_BUFF_ITEM' in config: config.remove_section('AUTO_BUFF_ITEM')
        config.add_section('AUTO_BUFF_ITEM')
        for row in self.buff_ui_rows:
            k, v = row['key_var'].get().strip().lower(), row['val_var'].get().strip()
            if k and v: config['AUTO_BUFF_ITEM'][k] = v
            
        save_profile(config, config_file)
        self.update_runtime_vars(); self.update_hotkey_display() 
        
        if not silent:
            self.btn_save.configure(text="✅ บันทึกสำเร็จ!", fg_color="green")
            self.after(2000, lambda: self.btn_save.configure(text="💾 บันทึกการตั้งค่า (Save)", fg_color=["#3B8ED0", "#1F6AA5"]))
            print(f">> 💾 บันทึกข้อมูลลง {self.active_profile} สำเร็จ!")

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
        self.shutdown_event.set()
        self.stop_bot("application closed", update_ui=False)
        self.destroy()

    def stop_bot(self, reason="stopped", update_ui=True):
        if self.run_event is not None:
            self.run_event.set()
        self.running = False
        if update_ui:
            self.lbl_status_main.configure(text="Status: stopped", text_color="#FF5555")
            print(f">> Bot stopped: {reason}")

    def stop_run_if_current(self, run_event, reason):
        if self.run_event is run_event:
            self.stop_bot(reason)

    def start_bot(self):
        if not self.model:
            return
        if not game_is_active(self.game_title):
            window_info = get_window_debug_info(self.game_title)
            print(
                ">> Game window is not active: "
                f"configured={self.game_title!r}, matched={window_info['game_title']!r}, "
                f"foreground={window_info['foreground_title']!r}"
            )
            self.lbl_status_main.configure(text="Status: open game window first", text_color="orange")
            return
        window_info = get_window_debug_info(self.game_title)
        print(f">> Input target: {window_info['game_title']!r} (HWND {window_info['game_hwnd']})")

        self.run_event = threading.Event()
        self.running = True
        self.lbl_status_main.configure(text="Status: running", text_color="#55FF55")
        print(">> Bot started")
        self.worker_threads = [
            threading.Thread(target=self.bot_main_loop, args=(self.run_event,), daemon=True),
            threading.Thread(target=self.potion_loop, args=(self.run_event,), daemon=True),
        ]
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
            self.log_throttled("key_press_error", f">> Key press error: {e}")
            return False

    def human_click(self, x, y):
        try:
            success = safe_click(self.game_title, x, y, input_lock=key_lock)
            if not success:
                self.log_throttled("inactive_game", ">> Skip click: game window is not active")
            return success
        except Exception as e:
            self.log_throttled("mouse_click_error", f">> Mouse click error: {e}")
            return False

    def log_throttled(self, key, message, interval=5.0):
        now = time.monotonic()
        if now - self._log_times.get(key, 0.0) >= interval:
            self._log_times[key] = now
            print(message)

    def potion_loop(self, run_event):
        last_tracker = {}; last_ui_hp, last_ui_sp = -1, -1
        hp_samples, sp_samples = deque(maxlen=3), deque(maxlen=3)
        low_counts = {}
        hp_len, sp_len = HP_END_X - HP_START_X, SP_END_X - SP_START_X
        with mss.mss() as sct:
            while not run_event.is_set():
                try:
                    time.sleep(0.02)
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
                    
                    if not self.potion_enabled: continue
                    
                    t = time.time()
                    for idx, pot in enumerate(list(self.potions_list)):
                        if not pot["en"]: continue
                        target_pct, key, dly, pot_type = int(pot["pct"]), pot["key"], int(pot["dly"])/1000.0, pot["type"]
                        tk = f"p_{idx}_{key}"
                        is_low = ((pot_type == "HP" and 2 <= pct_hp < target_pct) or
                                  (pot_type == "SP" and 2 <= pct_sp < target_pct))
                        low_counts[tk] = low_counts.get(tk, 0) + 1 if is_low else 0
                        if low_counts[tk] >= 2 and t - last_tracker.get(tk, 0) >= dly:
                            if self.human_press(key):
                                last_tracker[tk] = time.time()
                                low_counts[tk] = 0
                except Exception as e:
                    print(f">> Potion loop error: {e}")
                    time.sleep(0.2)

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
        last_buff_cast = {}
        last_death_check = last_move_log = last_rare_alert = last_preview = 0.0
        last_tp_check = target_started = last_attack = now
        locked_target = None
        death_hits = 0
        target_hits = 0
        rare_hits = 0
        pause_started = None

        with mss.mss() as sct:
            while not run_event.is_set():
                try:
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
                        last_rare_alert = current_time
                        if self.rare_item_action == "Stop":
                            run_event.set()
                            self.after(0, self.stop_run_if_current, run_event, "rare item detected")
                            break
                        if self.rare_item_action == "Key" and self.rare_item_key:
                            self.human_press(self.rare_item_key)

                    target = None
                    if monsters:
                        target, is_new_target = select_locked_target(
                            monsters,
                            locked_target,
                            (center_x, center_y),
                            monitor["width"],
                        )
                        if is_new_target:
                            target_started = current_time
                            target_hits = 1
                        else:
                            target_hits += 1
                        locked_target = (target[0], target[1])
                        last_tp_check = current_time
                    else:
                        locked_target = None
                        target_hits = 0
                        target_started = current_time

                    if target is not None:
                        if target_hits < 2:
                            continue
                        target_x, target_y, _ = target
                        if self.auto_tp_stuck and current_time - target_started >= self.auto_tp_stuck_sec:
                            print(f">> Target timeout after {self.auto_tp_stuck_sec:.1f}s; teleporting")
                            safe_move_to(self.game_title, center_x, center_y)
                            time.sleep(0.1)
                            if self.human_press(self.teleport_key):
                                if self.teleport_mode == "Skill":
                                    time.sleep(0.2)
                                    self.human_press("enter")
                                last_tp_check = target_started = time.monotonic()
                                locked_target = None
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
                        if self.enable_teleport and current_time - last_tp_check >= self.teleport_wait:
                            print(f">> No monster for {self.teleport_wait:.1f}s; teleporting")
                            if self.human_press(self.teleport_key):
                                if self.teleport_mode == "Skill":
                                    time.sleep(0.2)
                                    self.human_press("enter")
                                last_tp_check = target_started = time.monotonic()
                                time.sleep(0.2)
                except Exception as e:
                    self.log_throttled("bot_loop_error", f">> Bot loop error: {e}", interval=2.0)
                    time.sleep(0.5)

if __name__ == "__main__":
    app = BotApp(); app.mainloop()
