#!/usr/bin/env python3
"""AI 服务控制面板后端 (多线程并发 + 跨平台 Windows/macOS + 局域网访问 + 精准显存监控)"""

import subprocess
import json
import os
import signal
import sys
import time
import threading
import socket
import shutil
import platform
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 9090

IS_WIN = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'

def get_lan_ip():
    """获取本机在局域网中的真实物理 IPv4 地址（优先 192.168.* / 10.* / 172.16-31.*，过滤 TUN/VPN 虚拟网卡）"""
    candidates = []
    
    # 方式 1: 扫描本机网络接口
    try:
        if IS_WIN:
            r = subprocess.run("ipconfig", shell=True, capture_output=True, text=True, errors='replace')
            for line in r.stdout.splitlines():
                if "IPv4" in line or "IP Address" in line:
                    parts = line.split(":")
                    if len(parts) > 1:
                        ip = parts[1].replace("(Preferred)", "").strip()
                        if ip and not ip.startswith("127."):
                            candidates.append(ip)
        else:
            r = subprocess.run("ifconfig 2>/dev/null", shell=True, capture_output=True, text=True, errors='replace')
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    parts = line.split()
                    if len(parts) > 1:
                        ip = parts[1]
                        if ip and not ip.startswith("127."):
                            candidates.append(ip)
    except Exception:
        pass

    # 方式 2: UDP 路由探测补充
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip not in candidates:
            candidates.append(ip)
    except Exception:
        pass

    # 过滤与优先级：优先选择常见的真实物理局域网段，排除 198.18.* (Clash TUN 虚拟段)
    real_lan = []
    for ip in candidates:
        if ip.startswith("192.168."):
            return ip
        elif ip.startswith("10."):
            real_lan.append(ip)
        elif ip.startswith("172."):
            parts = ip.split(".")
            if len(parts) == 4 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
                real_lan.append(ip)

    if real_lan:
        return real_lan[0]

    for ip in candidates:
        if not ip.startswith("198.18.") and not ip.startswith("169.254."):
            return ip

    return candidates[0] if candidates else "127.0.0.1"

# ===== Open WebUI 自动安装状态与任务管理 =====
WEBUI_INSTALL_LOCK = threading.Lock()
WEBUI_INSTALL_TASK = {
    "active": False,
    "step": "idle",
    "progress": 0,
    "log": [],
    "done": False,
    "error": "",
    "message": ""
}

def _is_webui_installed():
    """检测当前机器是否已安装 Open WebUI 运行环境"""
    if IS_WIN:
        venv_path = os.path.expandvars(r"%USERPROFILE%\open-webui-venv")
        exe_path = os.path.join(venv_path, "Scripts", "open-webui.exe")
        if os.path.exists(exe_path):
            return True
        return shutil.which("open-webui") is not None
    else:
        venv_path = os.path.expanduser("~/open-webui-venv")
        bin_path = os.path.join(venv_path, "bin", "open-webui")
        if os.path.exists(bin_path):
            return True
        return shutil.which("open-webui") is not None

def _webui_installer_worker():
    global WEBUI_INSTALL_TASK
    with WEBUI_INSTALL_LOCK:
        WEBUI_INSTALL_TASK = {
            "active": True,
            "step": "正在准备安装环境...",
            "progress": 5,
            "log": ["[1/4] 开始初始化 Open WebUI 独立虚拟环境..."],
            "done": False,
            "error": "",
            "message": "正在创建虚拟环境..."
        }

    try:
        py_bin = sys.executable
        if IS_WIN:
            venv_dir = os.path.expandvars(r"%USERPROFILE%\open-webui-venv")
            pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe")
            webui_exe = os.path.join(venv_dir, "Scripts", "open-webui.exe")
        else:
            venv_dir = os.path.expanduser("~/open-webui-venv")
            pip_exe = os.path.join(venv_dir, "bin", "pip")
            webui_exe = os.path.join(venv_dir, "bin", "open-webui")

        if not os.path.exists(venv_dir):
            with WEBUI_INSTALL_LOCK:
                WEBUI_INSTALL_TASK["log"].append(f"[1/4] 执行: {py_bin} -m venv {venv_dir}")
                WEBUI_INSTALL_TASK["progress"] = 15
            subprocess.run([py_bin, "-m", "venv", venv_dir], check=True, capture_output=True, text=True)

        with WEBUI_INSTALL_LOCK:
            WEBUI_INSTALL_TASK["step"] = "正在配置国内高速镜像源与升级 pip..."
            WEBUI_INSTALL_TASK["progress"] = 30
            WEBUI_INSTALL_TASK["log"].append("[2/4] 配置清华 PyPI 高速源并升级 pip...")

        mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
        trusted_host = "pypi.tuna.tsinghua.edu.cn"

        with WEBUI_INSTALL_LOCK:
            WEBUI_INSTALL_TASK["step"] = "正在下载并安装 open-webui 依赖库 (国内镜像加速)..."
            WEBUI_INSTALL_TASK["progress"] = 45
            WEBUI_INSTALL_TASK["log"].append(f"[3/4] 执行: {pip_exe} install -i {mirror} --trusted-host {trusted_host} open-webui")

        cmd = [pip_exe, "install", "-i", mirror, "--trusted-host", trusted_host, "open-webui"]
        env = os.environ.copy()
        env.pop('HTTP_PROXY', None)
        env.pop('HTTPS_PROXY', None)
        env.pop('http_proxy', None)
        env.pop('https_proxy', None)
        env['no_proxy'] = '*'
        env['NO_PROXY'] = '*'

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, errors='replace')
        for line in process.stdout:
            line_str = line.strip()
            if line_str:
                with WEBUI_INSTALL_LOCK:
                    if len(WEBUI_INSTALL_TASK["log"]) > 120:
                        WEBUI_INSTALL_TASK["log"] = WEBUI_INSTALL_TASK["log"][-80:]
                    WEBUI_INSTALL_TASK["log"].append(line_str)
                    if "Downloading" in line_str or "Installing" in line_str or "Collecting" in line_str:
                        if WEBUI_INSTALL_TASK["progress"] < 90:
                            WEBUI_INSTALL_TASK["progress"] += 1

        process.wait()
        if process.returncode != 0:
            raise Exception(f"pip install 退出异常，返回码: {process.returncode}")

        with WEBUI_INSTALL_LOCK:
            WEBUI_INSTALL_TASK["step"] = "安装完成，正在校验可执行文件..."
            WEBUI_INSTALL_TASK["progress"] = 96
            WEBUI_INSTALL_TASK["log"].append("[4/4] 验证安装成果...")

        if os.path.exists(webui_exe) or shutil.which("open-webui"):
            with WEBUI_INSTALL_LOCK:
                WEBUI_INSTALL_TASK["active"] = False
                WEBUI_INSTALL_TASK["progress"] = 100
                WEBUI_INSTALL_TASK["step"] = "部署完成"
                WEBUI_INSTALL_TASK["message"] = "Open WebUI 已成功部署！点击启动即可使用。"
                WEBUI_INSTALL_TASK["log"].append("✅ Open WebUI 安装就绪，环境校验通过！")
                WEBUI_INSTALL_TASK["done"] = True
        else:
            raise Exception("安装完成但在对应虚拟环境中未找到 open-webui 可执行文件")

    except Exception as e:
        with WEBUI_INSTALL_LOCK:
            WEBUI_INSTALL_TASK["active"] = False
            WEBUI_INSTALL_TASK["done"] = True
            WEBUI_INSTALL_TASK["error"] = str(e)
            WEBUI_INSTALL_TASK["step"] = "部署失败"
            WEBUI_INSTALL_TASK["message"] = f"部署失败: {str(e)}"
            WEBUI_INSTALL_TASK["log"].append(f"❌ 部署发生异常: {str(e)}")

# ===== 硬件算力评测与模型匹配度计算 =====
def _evaluate_hardware_tier(mem_gb, os_name, cpu):
    try:
        mem = float(mem_gb)
    except Exception:
        mem = 32.0

    if mem < 12.0:
        return {
            "tier_level": 1,
            "tier_name": "8GB 极速入门配置",
            "badge_color": "#2ea043",
            "tag": "8GB 轻量",
            "desc": "适合运行 1.5B ~ 3B 极速小模型，轻度问答、摘要与日常对话流畅秒回",
            "recommended_combo": {
                "title": "8GB 极速轻量装机搭档",
                "chat_model": "qwen2.5:3b",
                "chat_model_title": "Qwen2.5:3B (轻量高智商中文)",
                "embed_model": "bge-m3",
                "embed_model_title": "BGE-M3 (高精度语义向量)",
                "models": ["qwen2.5:3b", "bge-m3"],
                "est_vram": "2.8 GB"
            }
        }
    elif mem < 24.0:
        return {
            "tier_level": 2,
            "tier_name": "16GB 主流全能配置",
            "badge_color": "#58a6ff",
            "tag": "16GB 主流",
            "desc": "可完美驾驭 7B ~ 8B 主力模型，覆盖企业文案创作、代码补全与常规数据分析",
            "recommended_combo": {
                "title": "16GB 生产力标配搭档",
                "chat_model": "qwen2.5:7b",
                "chat_model_title": "Qwen2.5:7B (中文高智力全能)",
                "embed_model": "bge-m3",
                "embed_model_title": "BGE-M3 (高精度语义向量)",
                "models": ["qwen2.5:7b", "bge-m3"],
                "est_vram": "5.8 GB"
            }
        }
    elif mem < 48.0:
        return {
            "tier_level": 3,
            "tier_name": "32GB 专业旗舰配置",
            "badge_color": "#d29922",
            "tag": "32GB 旗舰 (本机最佳)",
            "desc": "完美驾驭 14B 中文最强大模型满血推理，智力极高，适合商业策划、代码开发与企业知识库",
            "recommended_combo": {
                "title": "32GB 黄金装机全家桶",
                "chat_model": "qwen2.5:14b",
                "chat_model_title": "Qwen2.5:14B (中文最强大模型)",
                "embed_model": "bge-m3",
                "embed_model_title": "BGE-M3 (高精度知识库向量模型)",
                "models": ["qwen2.5:14b", "bge-m3"],
                "est_vram": "11.4 GB"
            }
        }
    else:
        return {
            "tier_level": 4,
            "tier_name": "64GB+ 超级工作站配置",
            "badge_color": "#a371f7",
            "tag": "64GB+ 极限",
            "desc": "可满血跑动 32B ~ 70B 顶尖大模型，具备媲美商用云端 GPT-4o 级别的超强逻辑长链推理",
            "recommended_combo": {
                "title": "64GB 满血王炸搭档",
                "chat_model": "qwen2.5:32b",
                "chat_model_title": "Qwen2.5:32B (顶尖智力高阶模型)",
                "embed_model": "bge-m3",
                "embed_model_title": "BGE-M3 (高精度知识库向量模型)",
                "models": ["qwen2.5:32b", "bge-m3"],
                "est_vram": "22.5 GB"
            }
        }

def _get_model_match(model_name, mem_gb):
    try:
        mem = float(mem_gb)
    except Exception:
        mem = 32.0

    name_lower = model_name.lower()
    
    if "32b" in name_lower or "70b" in name_lower or "72b" in name_lower:
        est_vram = 21.5
        if mem >= 48:
            return "gold", "⭐ 黄金旗舰推荐", est_vram
        elif mem >= 28:
            return "heavy", "🟡 占用偏高 (约21G)", est_vram
        else:
            return "insufficient", "🔴 显存不足 (不建议)", est_vram
            
    elif "14b" in name_lower or "12b" in name_lower or "11b" in name_lower:
        est_vram = 10.8
        if 24 <= mem < 48:
            return "gold", "⭐ 本机黄金推荐", est_vram
        elif mem >= 48:
            return "smooth", "🟢 流畅运行", est_vram
        elif mem >= 14:
            return "heavy", "🟡 显存较吃紧 (约10G)", est_vram
        else:
            return "insufficient", "🔴 显存不足 (不建议)", est_vram
            
    elif "7b" in name_lower or "8b" in name_lower or "6.7b" in name_lower:
        est_vram = 5.8
        if 12 <= mem < 24:
            return "gold", "⭐ 本机黄金推荐", est_vram
        elif mem >= 24:
            return "smooth", "🟢 极速流畅", est_vram
        else:
            return "heavy", "🟡 显存较高 (约6G)", est_vram
            
    elif "embed" in name_lower or "bge" in name_lower:
        est_vram = 1.2
        return "smooth", "🟢 极速向量 (知识库专用)", est_vram
        
    else:
        est_vram = 2.4
        if mem < 12:
            return "gold", "⭐ 本机黄金推荐", est_vram
        else:
            return "smooth", "🟢 超轻极速", est_vram

# ===== 全局异步下载任务管理器 =====
PULL_LOCK = threading.Lock()
PULL_TASK = {
    "active": False,
    "model": "",
    "status": "idle",       # "idle", "pulling", "verifying", "success", "error", "cancelled"
    "percent": 0.0,
    "completed_bytes": 0,
    "total_bytes": 0,
    "completed_str": "",
    "total_str": "",
    "speed": "",
    "eta": "",
    "error": "",
    "done": False,
    "message": ""
}
PULL_CANCEL_FLAG = False
CURRENT_PULL_RESP = None

def _batch_pull_worker(models):
    global PULL_CANCEL_FLAG
    PULL_CANCEL_FLAG = False
    for idx, m in enumerate(models):
        if PULL_CANCEL_FLAG:
            break
        _pull_worker(m)
        time.sleep(1)

def format_bytes(num):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"

def _pull_worker(model):
    global PULL_TASK, PULL_CANCEL_FLAG, CURRENT_PULL_RESP
    with PULL_LOCK:
        PULL_TASK["active"] = True
        PULL_TASK["model"] = model
        PULL_TASK["status"] = "正在连接 Ollama..."
        PULL_TASK["percent"] = 0.0
        PULL_TASK["completed_bytes"] = 0
        PULL_TASK["total_bytes"] = 0
        PULL_TASK["completed_str"] = ""
        PULL_TASK["total_str"] = ""
        PULL_TASK["speed"] = ""
        PULL_TASK["eta"] = ""
        PULL_TASK["error"] = ""
        PULL_TASK["done"] = False
        PULL_TASK["message"] = f"准备下载 {model}..."
        PULL_CANCEL_FLAG = False

    last_time = time.time()
    last_bytes = 0

    try:
        req = urllib.request.Request(
            'http://127.0.0.1:11434/api/pull',
            data=json.dumps({"name": model, "stream": True}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            CURRENT_PULL_RESP = resp
            for raw_line in resp:
                if PULL_CANCEL_FLAG:
                    with PULL_LOCK:
                        PULL_TASK["status"] = "已取消"
                        PULL_TASK["message"] = f"已取消下载 {model}"
                        PULL_TASK["active"] = False
                        PULL_TASK["done"] = True
                    return

                if not raw_line:
                    continue

                try:
                    data = json.loads(raw_line.decode('utf-8'))
                except Exception:
                    continue

                if "error" in data:
                    with PULL_LOCK:
                        PULL_TASK["error"] = data["error"]
                        PULL_TASK["status"] = "下载失败"
                        PULL_TASK["message"] = f"下载错误: {data['error']}"
                        PULL_TASK["active"] = False
                        PULL_TASK["done"] = True
                    return

                status = data.get("status", "")
                completed = data.get("completed", 0)
                total = data.get("total", 0)

                now = time.time()
                elapsed = now - last_time
                speed_str = ""
                eta_str = ""
                if elapsed >= 0.7 and completed > last_bytes:
                    speed = (completed - last_bytes) / elapsed
                    speed_str = f"{format_bytes(speed)}/s"
                    if speed > 0 and total > completed:
                        rem_seconds = int((total - completed) / speed)
                        mins, secs = divmod(rem_seconds, 60)
                        eta_str = f"剩余 {mins}分{secs}秒" if mins > 0 else f"剩余 {secs}秒"
                    last_time = now
                    last_bytes = completed

                percent = 0.0
                if total > 0:
                    percent = round((completed / total) * 100, 1)

                with PULL_LOCK:
                    PULL_TASK["status"] = status
                    if percent > 0:
                        PULL_TASK["percent"] = percent
                    if completed > 0:
                        PULL_TASK["completed_bytes"] = completed
                        PULL_TASK["completed_str"] = format_bytes(completed)
                    if total > 0:
                        PULL_TASK["total_bytes"] = total
                        PULL_TASK["total_str"] = format_bytes(total)
                    if speed_str:
                        PULL_TASK["speed"] = speed_str
                    if eta_str:
                        PULL_TASK["eta"] = eta_str

                    if status == "success":
                        PULL_TASK["percent"] = 100.0
                        PULL_TASK["status"] = "下载完成"
                        PULL_TASK["message"] = f"模型 {model} 已成功安装"
                        PULL_TASK["active"] = False
                        PULL_TASK["done"] = True
                        return

        with PULL_LOCK:
            PULL_TASK["percent"] = 100.0
            PULL_TASK["status"] = "下载完成"
            PULL_TASK["message"] = f"模型 {model} 已成功安装"
            PULL_TASK["active"] = False
            PULL_TASK["done"] = True

    except Exception as e:
        if PULL_CANCEL_FLAG:
            with PULL_LOCK:
                PULL_TASK["status"] = "已取消"
                PULL_TASK["message"] = "下载已取消"
                PULL_TASK["active"] = False
                PULL_TASK["done"] = True
        else:
            with PULL_LOCK:
                PULL_TASK["status"] = "失败"
                PULL_TASK["error"] = str(e)
                PULL_TASK["message"] = f"下载异常中断: {str(e)}"
                PULL_TASK["active"] = False
                PULL_TASK["done"] = True
    finally:
        CURRENT_PULL_RESP = None


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DASHBOARD_DIR, **kwargs)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/status':
            self._json(self._get_status())
        elif path == '/api/models':
            self._json(self._get_models())
        elif path == '/api/system':
            self._json(self._get_system_info())
        elif path == '/api/online-models':
            self._json(self._get_online_models())
        elif path == '/api/model/pull/status':
            with PULL_LOCK:
                self._json(dict(PULL_TASK))
        elif path == '/api/webui/install/status':
            with WEBUI_INSTALL_LOCK:
                self._json(dict(WEBUI_INSTALL_TASK))
        else:
            super().do_GET()

    def _start_ollama(self):
        if IS_WIN:
            try:
                subprocess.Popen(["cmd", "/c", "start", "/B", "ollama", "serve"], creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
                return True, "已发送 Ollama 启动指令 (Windows)"
            except Exception as e:
                return False, f"Ollama 启动失败: {str(e)}"
        else:
            self._run_shell("brew services start ollama 2>&1 || nohup ollama serve > /tmp/ollama.log 2>&1 &")
            return True, "已发送 Ollama 启动指令"

    def _stop_ollama(self):
        if IS_WIN:
            self._run_shell('taskkill /F /IM ollama.exe /T 2>nul & taskkill /F /IM "ollama app.exe" /T 2>nul')
            return True, "Ollama 服务已停止 (Windows)"
        else:
            self._run_shell("brew services stop ollama 2>&1; pkill -x ollama 2>/dev/null")
            return True, "Ollama 服务已停止"

    def _start_webui(self):
        if IS_WIN:
            venv_act = os.path.expandvars(r"%USERPROFILE%\open-webui-venv\Scripts\activate.bat")
            cmd = (
                f'if exist "{venv_act}" (call "{venv_act}") && '
                'set HTTP_PROXY= && set HTTPS_PROXY= && set http_proxy= && set https_proxy= && set ALL_PROXY= && set all_proxy= && '
                'set OLLAMA_BASE_URL=http://127.0.0.1:11434 && '
                'set no_proxy=* && set NO_PROXY=* && '
                'start /B open-webui serve > "%USERPROFILE%\\open-webui.log" 2>&1'
            )
            os.system(f'cmd /c "{cmd}"')
            return True, "Open WebUI 正在后台启动... (Windows)"
        else:
            cmd = (
                "source ~/open-webui-venv/bin/activate 2>/dev/null || true; "
                "unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy && "
                "export OLLAMA_BASE_URL='http://127.0.0.1:11434' && "
                "export no_proxy='*' && export NO_PROXY='*' && "
                "nohup open-webui serve > ~/open-webui.log 2>&1 &"
            )
            os.system(f"bash -c '{cmd}'")
            return True, "Open WebUI 正在后台启动..."

    def _stop_webui(self):
        if IS_WIN:
            self._run_shell('taskkill /F /IM open-webui.exe /T 2>nul')
            self._run_shell('wmic process where "commandline like \'%open-webui%\'" call terminate 2>nul')
            return True, "Open WebUI 已停止 (Windows)"
        else:
            os.system("pkill -f 'open-webui' 2>/dev/null")
            return True, "Open WebUI 已停止"

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == '/api/ollama/start':
            ok, msg = self._start_ollama()
            self._json({"ok": ok, "msg": msg})
        elif path == '/api/ollama/stop':
            ok, msg = self._stop_ollama()
            self._json({"ok": ok, "msg": msg})
        elif path == '/api/webui/start':
            ok, msg = self._start_webui()
            self._json({"ok": ok, "msg": msg})
        elif path == '/api/webui/stop':
            ok, msg = self._stop_webui()
            self._json({"ok": ok, "msg": msg})
        elif path == '/api/all/start':
            self._start_ollama()
            self._start_webui()
            self._json({"ok": True, "msg": "全部服务正在启动中..."})
        elif path == '/api/all/stop':
            self._stop_webui()
            self._stop_ollama()
            self._json({"ok": True, "msg": "所有服务已停止，内存已完全释放"})
        elif path == '/api/model/pull':
            model = body.get('model', '').strip()
            if not model:
                self._json({"ok": False, "msg": "模型名不能为空"})
                return

            with PULL_LOCK:
                if PULL_TASK["active"]:
                    self._json({"ok": False, "msg": f"正在下载模型 {PULL_TASK['model']}，请等待完成或取消"})
                    return

            t = threading.Thread(target=_pull_worker, args=(model,), daemon=True)
            t.start()
            self._json({"ok": True, "msg": f"已开始后台下载 {model}"})

        elif path == '/api/webui/install':
            with WEBUI_INSTALL_LOCK:
                if WEBUI_INSTALL_TASK["active"]:
                    self._json({"ok": False, "msg": "正在执行 Open WebUI 自动部署，请稍候"})
                    return
            t = threading.Thread(target=_webui_installer_worker, daemon=True)
            t.start()
            self._json({"ok": True, "msg": "已启动 Open WebUI 一键全自动后台部署"})

        elif path == '/api/model/batch-pull':
            models = body.get('models', [])
            if not models:
                self._json({"ok": False, "msg": "批量安装模型列表不能为空"})
                return
            with PULL_LOCK:
                if PULL_TASK["active"]:
                    self._json({"ok": False, "msg": f"当前正在下载 {PULL_TASK['model']}，请等待完成"})
                    return
            t = threading.Thread(target=_batch_pull_worker, args=(models,), daemon=True)
            t.start()
            self._json({"ok": True, "msg": f"已开始排队拉取模型组合: {', '.join(models)}"})

        elif path == '/api/model/pull/cancel':
            global PULL_CANCEL_FLAG, CURRENT_PULL_RESP
            PULL_CANCEL_FLAG = True
            if CURRENT_PULL_RESP:
                try:
                    CURRENT_PULL_RESP.close()
                except Exception:
                    pass
            self._json({"ok": True, "msg": "已发送取消请求"})

        elif path == '/api/model/delete':
            model = body.get('model', '').strip()
            if not model:
                self._json({"ok": False, "msg": "模型名不能为空"})
                return
            result = self._run_shell(f"ollama rm {model} 2>&1")
            self._json({"ok": True, "msg": f"已删除模型: {model}"})

        elif path == '/api/model/load':
            model = body.get('model', '').strip()
            if not model:
                self._json({"ok": False, "msg": "模型名不能为空"})
                return
            try:
                load_req = urllib.request.Request(
                    'http://127.0.0.1:11434/api/generate',
                    data=json.dumps({"model": model, "keep_alive": -1, "stream": False}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )
                with urllib.request.urlopen(load_req, timeout=45) as resp:
                    self._json({"ok": True, "msg": f"模型 {model} 已开启运行并载入显存"})
            except urllib.error.URLError:
                self._json({"ok": False, "msg": "Ollama 服务未启动，请先启动 Ollama"})
            except Exception as e:
                self._json({"ok": False, "msg": f"开启运行失败: {str(e)}"})

        elif path == '/api/model/unload':
            model = body.get('model', '').strip()
            if model:
                # 卸载关闭指定的单一模型
                try:
                    unload_req = urllib.request.Request(
                        'http://127.0.0.1:11434/api/generate',
                        data=json.dumps({"model": model, "keep_alive": 0, "stream": False}).encode('utf-8'),
                        headers={'Content-Type': 'application/json'}
                    )
                    with urllib.request.urlopen(unload_req, timeout=5) as resp:
                        self._json({"ok": True, "msg": f"模型 {model} 已关闭运行，显存已释放"})
                except urllib.error.URLError:
                    self._json({"ok": False, "msg": "Ollama 服务未启动"})
                except Exception as e:
                    self._json({"ok": False, "msg": f"关闭模型失败: {str(e)}"})
            else:
                # 释放全部模型显存
                unloaded = []
                try:
                    req = urllib.request.Request('http://127.0.0.1:11434/api/ps')
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        ps_data = json.loads(resp.read().decode('utf-8'))
                        for rm in ps_data.get('models', []):
                            m_name = rm.get('name')
                            unload_req = urllib.request.Request(
                                'http://127.0.0.1:11434/api/generate',
                                data=json.dumps({"model": m_name, "keep_alive": 0, "stream": False}).encode('utf-8'),
                                headers={'Content-Type': 'application/json'}
                            )
                            urllib.request.urlopen(unload_req, timeout=3)
                            unloaded.append(m_name)
                except Exception:
                    pass
                if unloaded:
                    self._json({"ok": True, "msg": f"已释放模型显存: {', '.join(unloaded)}"})
                else:
                    self._json({"ok": True, "msg": "当前没有已驻留内存的模型"})

        elif path == '/api/model/test':
            model = body.get('model', '').strip()
            prompt = body.get('prompt', '你好，请用一句话介绍你自己，并说明你的核心优势。').strip()
            if not model:
                self._json({"ok": False, "msg": "模型名不能为空"})
                return

            start_t = time.time()
            try:
                req = urllib.request.Request(
                    'http://127.0.0.1:11434/api/generate',
                    data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    res_data = json.loads(resp.read().decode('utf-8'))
                    duration = round(time.time() - start_t, 2)
                    output_text = res_data.get('response', '')
                    self._json({
                        "ok": True,
                        "msg": output_text,
                        "time": duration,
                        "model": model
                    })
            except urllib.error.URLError:
                self._json({"ok": False, "msg": "Ollama 服务未启动，请先启动 Ollama"})
            except Exception as e:
                duration = round(time.time() - start_t, 2)
                self._json({"ok": False, "msg": f"推理超时或出错: {str(e)}", "time": duration})

        elif path == '/api/open-webui':
            os.system("open http://localhost:8080")
            self._json({"ok": True, "msg": "已打开默认浏览器"})
        else:
            self._json({"ok": False, "msg": "未知接口操作"})

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return {}

    def _json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _run_shell(self, cmd, timeout=300):
        try:
            env = os.environ.copy()
            env.pop('HTTP_PROXY', None)
            env.pop('HTTPS_PROXY', None)
            env.pop('http_proxy', None)
            env.pop('https_proxy', None)
            env['no_proxy'] = '*'
            env['NO_PROXY'] = '*'
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env, errors='replace')
            return r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            return "命令超时"
        except Exception as e:
            return str(e)

    def _is_running(self, pattern):
        if IS_WIN:
            try:
                r = subprocess.run(f'tasklist /FI "IMAGENAME eq {pattern}.exe"', shell=True, capture_output=True, text=True, errors='replace')
                if pattern.lower() in r.stdout.lower():
                    return True
                r2 = subprocess.run(f'wmic process where "CommandLine like \'%{pattern}%\'" get ProcessId 2>nul', shell=True, capture_output=True, text=True, errors='replace')
                for line in r2.stdout.splitlines():
                    if line.strip().isdigit():
                        return True
                return False
            except Exception:
                return False
        else:
            try:
                result = subprocess.run(f"pgrep -f '{pattern}'", shell=True, capture_output=True, text=True, errors='replace')
                return result.returncode == 0
            except Exception:
                return False

    def _get_status(self):
        ollama_running = False
        loaded_models = []
        ollama_vram = 0.0

        try:
            req = urllib.request.Request('http://127.0.0.1:11434/api/ps')
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                ollama_running = True
                ps_data = json.loads(resp.read().decode('utf-8'))
                for rm in ps_data.get('models', []):
                    vram_gb = round(rm.get('size_vram', 0) / (1024**3), 1)
                    ollama_vram += vram_gb
                    loaded_models.append({
                        "name": rm.get('name', ''),
                        "vram": vram_gb
                    })
        except Exception:
            ollama_running = self._is_running('ollama')

        ollama_mem = round(ollama_vram, 1)
        if ollama_running and ollama_mem == 0:
            if IS_WIN:
                try:
                    r = subprocess.run('tasklist /FI "IMAGENAME eq ollama.exe" /FO CSV /NH', shell=True, capture_output=True, text=True, errors='replace')
                    for line in r.stdout.splitlines():
                        parts = line.split('","')
                        if len(parts) >= 5:
                            mem_str = parts[4].replace('"', '').replace(' K', '').replace(',', '').strip()
                            if mem_str.isdigit():
                                ollama_mem += round(int(mem_str) / 1024 / 1024, 1)
                except Exception:
                    pass
            else:
                try:
                    r = subprocess.run("pgrep -x ollama | xargs -I {} ps -p {} -o rss= 2>/dev/null", shell=True, capture_output=True, text=True)
                    if r.stdout.strip():
                        total_kb = sum(int(x.strip()) for x in r.stdout.strip().split() if x.strip().isdigit())
                        ollama_mem = round(total_kb / 1024 / 1024, 1)
                except Exception:
                    pass

        webui_running = self._is_running('open-webui')
        webui_mem = 0.0
        if webui_running:
            if IS_WIN:
                try:
                    r = subprocess.run('tasklist /FI "IMAGENAME eq open-webui.exe" /FO CSV /NH', shell=True, capture_output=True, text=True, errors='replace')
                    for line in r.stdout.splitlines():
                        parts = line.split('","')
                        if len(parts) >= 5:
                            mem_str = parts[4].replace('"', '').replace(' K', '').replace(',', '').strip()
                            if mem_str.isdigit():
                                webui_mem += round(int(mem_str) / 1024 / 1024, 1)
                except Exception:
                    pass
            else:
                try:
                    r = subprocess.run("pgrep -f 'open-webui' | xargs -I {} ps -p {} -o rss= 2>/dev/null", shell=True, capture_output=True, text=True)
                    if r.stdout.strip():
                        total_kb = sum(int(x.strip()) for x in r.stdout.strip().split() if x.strip().isdigit())
                        webui_mem = round(total_kb / 1024 / 1024, 1)
                except Exception:
                    pass

        total_mem = round(ollama_mem + webui_mem, 1)
        webui_installed = _is_webui_installed()

        return {
            "ollama": {
                "running": ollama_running,
                "mem": ollama_mem,
                "loaded_models": loaded_models
            },
            "webui": {
                "installed": webui_installed,
                "running": webui_running,
                "mem": webui_mem
            },
            "total_mem": total_mem
        }

    def _get_models(self):
        # 抓取当前已驻留显存的运行中模型
        running_map = {}
        try:
            req_ps = urllib.request.Request('http://127.0.0.1:11434/api/ps')
            with urllib.request.urlopen(req_ps, timeout=1.5) as resp:
                ps_data = json.loads(resp.read().decode('utf-8'))
                for rm in ps_data.get('models', []):
                    m_name = rm.get('name', '')
                    vram_gb = round(rm.get('size_vram', 0) / (1024**3), 1)
                    running_map[m_name] = vram_gb
        except Exception:
            pass

        def _match_running(m_name):
            if not m_name:
                return False, 0.0
            if m_name in running_map:
                return True, running_map[m_name]
            if f"{m_name}:latest" in running_map:
                return True, running_map[f"{m_name}:latest"]
            if m_name.endswith(':latest') and m_name[:-7] in running_map:
                return True, running_map[m_name[:-7]]
            for rk, rv in running_map.items():
                if rk.startswith(m_name) or m_name.startswith(rk):
                    return True, rv
            return False, 0.0

        try:
            req = urllib.request.Request('http://127.0.0.1:11434/api/tags')
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                models = []
                for m in data.get('models', []):
                    name = m.get('name', '')
                    size_gb = round(m.get('size', 0) / (1024**3), 1)
                    details = m.get('details', {})
                    is_running, vram = _match_running(name)
                    models.append({
                        "name": name,
                        "size": f"{size_gb} GB",
                        "param_size": details.get('parameter_size', ''),
                        "quant": details.get('quantization_level', ''),
                        "family": details.get('family', ''),
                        "running": is_running,
                        "vram": vram
                    })
                return models
        except Exception:
            try:
                r = subprocess.run("ollama list 2>/dev/null", shell=True, capture_output=True, text=True)
                lines = r.stdout.strip().splitlines()
                if len(lines) <= 1 or not lines[0].strip():
                    return []
                models = []
                for line in lines[1:]:
                    parts = line.split()
                    if parts:
                        models.append({"name": parts[0], "size": parts[2] if len(parts) > 2 else "N/A"})
                return models
            except Exception:
                return []

    def _get_system_info(self):
        # 1. CPU
        cpu = "Apple Silicon" if IS_MAC else "CPU"
        try:
            if IS_WIN:
                r = subprocess.run("wmic cpu get name", shell=True, capture_output=True, text=True, errors='replace')
                lines = [line.strip() for line in r.stdout.splitlines() if line.strip() and not line.lower().startswith('name')]
                cpu = lines[0] if lines else (platform.processor() or "Windows CPU")
            elif IS_MAC:
                cpu = self._run_shell("sysctl -n machdep.cpu.brand_string 2>/dev/null").strip() or "Apple Silicon"
            else:
                cpu = platform.processor() or "Linux CPU"
        except Exception:
            cpu = platform.processor() or "CPU"

        # 2. 内存总量
        mem_total = "32"
        try:
            if IS_WIN:
                r = subprocess.run("wmic OS get TotalVisibleMemorySize", shell=True, capture_output=True, text=True, errors='replace')
                for line in r.stdout.splitlines():
                    if line.strip().isdigit():
                        mem_total = str(round(int(line.strip()) / 1024 / 1024))
                        break
            elif IS_MAC:
                mem_bytes_str = self._run_shell("sysctl -n hw.memsize 2>/dev/null").strip()
                if mem_bytes_str.isdigit():
                    mem_total = str(round(int(mem_bytes_str) / (1024**3)))
            else:
                with open('/proc/meminfo', 'r') as f:
                    for line in f:
                        if line.startswith('MemTotal:'):
                            kb = int(line.split()[1])
                            mem_total = str(round(kb / 1024 / 1024))
                            break
        except Exception:
            pass

        # 3. 磁盘空间 (使用标准库 shutil.disk_usage，100% 跨平台免命令调用)
        disk = "N/A"
        try:
            total, used, free = shutil.disk_usage(DASHBOARD_DIR)
            free_gb = round(free / (1024**3), 1)
            used_pct = round((used / total) * 100)
            disk = f"{free_gb} GB 可用 (已用{used_pct}%)"
        except Exception:
            pass

        # 4. 网络访问地址 (支持局域网设备/手机连接)
        lan_ip = get_lan_ip()
        network = {
            "lan_ip": lan_ip,
            "port": PORT,
            "local_url": f"http://localhost:{PORT}",
            "lan_url": f"http://{lan_ip}:{PORT}" if lan_ip != "127.0.0.1" else None
        }

        # 5. 系统平台标识与硬件算力档位评估
        os_name = "macOS" if IS_MAC else ("Windows" if IS_WIN else "Linux")
        hardware_tier = _evaluate_hardware_tier(mem_total, os_name, cpu)

        return {
            "os": os_name,
            "cpu": cpu,
            "mem_total_gb": mem_total,
            "disk": disk,
            "network": network,
            "hardware_tier": hardware_tier
        }

    def _get_online_models(self):
        mem_str = "32"
        try:
            if IS_WIN:
                r = subprocess.run("wmic OS get TotalVisibleMemorySize", shell=True, capture_output=True, text=True, errors='replace')
                for line in r.stdout.splitlines():
                    if line.strip().isdigit():
                        mem_str = str(round(int(line.strip()) / 1024 / 1024))
                        break
            elif IS_MAC:
                mem_bytes_str = self._run_shell("sysctl -n hw.memsize 2>/dev/null").strip()
                if mem_bytes_str.isdigit():
                    mem_str = str(round(int(mem_bytes_str) / (1024**3)))
        except Exception:
            pass

        tier = _evaluate_hardware_tier(mem_str, "macOS" if IS_MAC else "Windows", "")

        raw_categories = {
            "⭐ 本机黄金推荐": [
                {"name": tier["recommended_combo"]["chat_model"], "desc": f"{tier['recommended_combo']['chat_model_title']} - 算力最佳平衡首选"},
                {"name": tier["recommended_combo"]["embed_model"], "desc": f"{tier['recommended_combo']['embed_model_title']} - 知识库必备语义底座"}
            ],
            "对话聊天": [
                {"name": "qwen2.5:14b", "desc": "中文全能，128K上下文 (32GB首选)"},
                {"name": "qwen2.5:32b", "desc": "高智力全能，接近GPT-4o (32GB极限)"},
                {"name": "qwen2.5:7b", "desc": "轻量高速中文，响应飞快"},
                {"name": "llama3.1:8b", "desc": "Meta旗舰英文与多语言"},
                {"name": "gemma3:12b", "desc": "Google最新多模态语言旗舰"},
                {"name": "mistral:7b", "desc": "欧系开源经典小钢炮"},
            ],
            "代码编程": [
                {"name": "qwen2.5-coder:14b", "desc": "编程专用主力，代码生成极强 (推荐)"},
                {"name": "qwen2.5-coder:7b", "desc": "轻量级代码补全与调试"},
                {"name": "deepseek-coder:6.7b", "desc": "DeepSeek代码基座"},
            ],
            "推理思考": [
                {"name": "deepseek-r1:14b", "desc": "蒸馏版满血逻辑推理 (推荐)"},
                {"name": "deepseek-r1:8b", "desc": "轻量级数学与复杂逻辑推理"},
                {"name": "deepseek-r1:32b", "desc": "高阶复杂长链推理 (适合深度思考)"},
            ],
            "视觉多模态": [
                {"name": "qwen2.5vl:7b", "desc": "阿里最新视觉语言多模态"},
                {"name": "llama3.2-vision:11b", "desc": "Meta官方图文理解模型"},
                {"name": "minicpm-v:8b", "desc": "OCR与高精度图文识别"},
            ],
            "嵌入向量": [
                {"name": "bge-m3", "desc": "多语言长文本向量检索 (知识库首选)"},
                {"name": "nomic-embed-text", "desc": "高性价比轻量语义嵌入"},
            ]
        }

        processed_categories = {}
        for cat, list_m in raw_categories.items():
            processed_categories[cat] = []
            for m in list_m:
                match_level, match_text, est_vram = _get_model_match(m["name"], mem_str)
                m_copy = dict(m)
                m_copy["match_level"] = match_level
                m_copy["match_text"] = match_text
                m_copy["est_vram"] = est_vram
                processed_categories[cat].append(m_copy)

        return {
            "tier": tier,
            "categories": processed_categories
        }

def main():
    server = ThreadingHTTPServer(('0.0.0.0', PORT), DashboardHandler)
    server.daemon_threads = True
    lan_ip = get_lan_ip()
    print("=" * 60)
    print("  AI 控制面板后台服务已启动 (支持跨平台与局域网多端访问)")
    print(f"  > 本地访问:    http://localhost:{PORT}")
    if lan_ip != "127.0.0.1":
        print(f"  > 局域网访问:  http://{lan_ip}:{PORT} (手机/平板/同网电脑)")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()

if __name__ == '__main__':
    main()
