import re
import requests
import urllib.parse
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
import logging
import time
import schedule
import os
import threading
import datetime
# 引入 request 以防万一
from flask import Flask, Response, render_template_string, redirect, url_for, request

# --- 日志系统配置 ---
class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.log_records = []
        self.max_records = 100 

    def emit(self, record):
        try:
            log_entry = self.format(record)
            self.log_records.append(log_entry)
            # 保持列表长度，移除旧的
            if len(self.log_records) > self.max_records:
                self.log_records.pop(0)
        except Exception:
            self.handleError(record)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%H:%M:%S')

# 控制台输出
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# 内存输出（给网页看）
web_log_handler = ListHandler()
web_log_handler.setFormatter(formatter)
logger.addHandler(web_log_handler)

# --- 全局变量 ---
current_playlist_content = "#EXTM3U\n"
app = Flask(__name__)

# --- 核心逻辑类 ---
class LiveMonitor:
    def __init__(self):
        self.source_url = os.getenv('SOURCE_URL', "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5")
        self.ua = UserAgent()
        self.headers = {
            'User-Agent': self.ua.random,
            'Referer': 'https://www.jrs21.com/',
        }
        self.last_update_time = "尚未运行"
        self.next_update_time = "计算中..."
        self.match_count = 0
        self.stream_count = 0
        self.is_running = False
        self.last_error = None

    def fetch_source_js(self):
        try:
            timestamp = int(time.time() * 1000)
            url_with_ts = f"{self.source_url}&_={timestamp}"
            logger.info(f"正在请求源数据...")
            resp = requests.get(url_with_ts, headers=self.headers, timeout=15)
            resp.encoding = 'utf-8'
            if resp.status_code == 200:
                return resp.text
            err_msg = f"源站返回错误代码: {resp.status_code}"
            logger.error(err_msg)
            self.last_error = err_msg
            return None
        except Exception as e:
            err_msg = f"网络请求失败: {str(e)}"
            logger.error(err_msg)
            self.last_error = err_msg
            return None

    def parse_js_to_html(self, js_content):
        try:
            pattern = re.compile(r"document\.write\('(.*?)'\);")
            matches = pattern.findall(js_content)
            return "".join(matches)
        except Exception as e:
            logger.error(f"JS解析失败: {e}")
            return ""

    def extract_matches(self, html_content):
        soup = BeautifulSoup(html_content, 'lxml')
        matches = []
        game_items = soup.find_all('ul', class_='item')
        
        for item in game_items:
            try:
                league = item.find('li', class_='lab_events').get_text(strip=True)
                time_val = item.find('li', class_='lab_time').get_text(strip=True)
                home_team = item.find('li', class_='lab_team_home').find('strong').get_text(strip=True)
                away_team = item.find('li', class_='lab_team_away').find('strong').get_text(strip=True)
                match_name = f"[{league}] {home_team} vs {away_team}"
                
                links = []
                channel_li = item.find('li', class_='lab_channel')
                if channel_li:
                    a_tags = channel_li.find_all('a', class_='item')
                    for a in a_tags:
                        title = a.get_text(strip=True)
                        href = a.get('data-play') or a.get('href')
                        if href and href.startswith('http'):
                            links.append({'title': title, 'url': href})
                if links:
                    matches.append({'name': match_name, 'time': time_val, 'links': links})
            except:
                continue
        return matches

    def decode_stream(self, html, base_url):
        try:
            m3u8_pattern = re.compile(r"['\"](http[^'\"]+?\.m3u8.*?)['\"]")
            direct_match = m3u8_pattern.search(html)
            if direct_match: return direct_match.group(1)

            soup = BeautifulSoup(html, 'lxml')
            iframe = soup.find('iframe')
            if iframe:
                src = iframe.get('src')
                if src:
                    if not src.startswith('http'): src = urllib.parse.urljoin(base_url, src)
                    with requests.Session() as s:
                        r = s.get(src, headers=self.headers, timeout=5)
                        iframe_match = m3u8_pattern.search(r.text)
                        if iframe_match: return iframe_match.group(1)
        except Exception:
            pass
        return None

    def update_playlist(self):
        global current_playlist_content
        
        if self.is_running:
            logger.warning("任务正在运行中，跳过本次触发")
            return
            
        self.is_running = True
        self.last_error = None
        start_time = time.time()
        
        logger.info(">>> 开始执行更新任务")
        
        try:
            js_code = self.fetch_source_js()
            if js_code:
                html = self.parse_js_to_html(js_code)
                matches = self.extract_matches(html)
                self.match_count = len(matches)
                logger.info(f"解析到 {self.match_count} 场比赛")
                
                valid_streams = []
                for match in matches:
                    for link in match['links']:
                        try:
                            target_url = link['url']
                            final_url = None
                            if '.m3u8' in target_url:
                                final_url = target_url
                            else:
                                resp = requests.get(target_url, headers=self.headers, timeout=8)
                                if resp.status_code == 200:
                                    final_url = self.decode_stream(resp.text, target_url)
                            
                            if final_url:
                                valid_streams.append({
                                    'group': "JRS直播",
                                    'name': f"{match['time']} {match['name']} - {link['title']}",
                                    'url': final_url
                                })
                            time.sleep(0.1)
                        except: continue
                
                new_content = "#EXTM3U\n"
                for s in valid_streams:
                    new_content += f'#EXTINF:-1 group-title="{s["group"]}", {s["name"]}\n'
                    new_content += f"{s['url']}\n"
                
                current_playlist_content = new_content
                self.stream_count = len(valid_streams)
                logger.info(f"更新成功! 有效源: {self.stream_count}")
            else:
                logger.warning("未获取到JS代码，可能接口变动或IP被限制")
                if not self.last_error:
                    self.last_error = "无法获取JS代码"

        except Exception as e:
            logger.error(f"致命错误: {str(e)}")
            self.last_error = str(e)
        finally:
            self.is_running = False
            self.last_update_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            interval = int(os.getenv('FETCH_INTERVAL', 300))
            next_time = datetime.datetime.now() + datetime.timedelta(seconds=interval)
            self.next_update_time = next_time.strftime("%H:%M:%S")
            logger.info(f"<<< 任务结束，耗时 {time.time() - start_time:.2f}秒")

monitor = LiveMonitor()

# --- HTML 模板 ---
DEBUG_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>JRS Monitor Debug</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background: #f4f4f9; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 20px; }
        h1 { color: #333; font-size: 24px; }
        h2 { border-bottom: 2px solid #eee; padding-bottom: 10px; font-size: 18px; color: #555; }
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }
        .stat-item { background: #f8f9fa; padding: 15px; border-radius: 6px; text-align: center; }
        .stat-value { font-size: 24px; font-weight: bold; color: #007bff; display: block; }
        .stat-label { font-size: 12px; color: #666; text-transform: uppercase; }
        .btn { display: inline-block; background: #28a745; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; }
        .btn:hover { background: #218838; }
        .btn-refresh { cursor: pointer; border: none; font-size: 16px; }
        .logs { background: #2d2d2d; color: #ccc; padding: 15px; border-radius: 6px; height: 300px; overflow-y: scroll; font-family: monospace; font-size: 12px; }
        .log-entry { margin-bottom: 5px; border-bottom: 1px solid #444; padding-bottom: 2px; }
        .status-running { color: orange; font-weight: bold; animation: blink 1s infinite; }
        .error-msg { color: red; background: #ffeeee; padding: 10px; border-radius: 5px; }
        @keyframes blink { 50% { opacity: 0.5; } }
    </style>
</head>
<body>
    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <h1>🛠️ JRS 监控仪表盘</h1>
            <a href="/trigger_update" class="btn btn-refresh">🔄 立即刷新</a>
        </div>
        
        {% if monitor.is_running %}
            <p class="status-running">⚠️ 后台任务正在运行中，请稍候...</p>
        {% endif %}

        {% if monitor.last_error %}
            <div class="error-msg">❌ 最近错误: {{ monitor.last_error }}</div>
        {% endif %}

        <div class="stat-grid">
            <div class="stat-item">
                <span class="stat-value">{{ monitor.match_count }}</span>
                <span class="stat-label">发现比赛</span>
            </div>
            <div class="stat-item">
                <span class="stat-value">{{ monitor.stream_count }}</span>
                <span class="stat-label">有效源</span>
            </div>
            <div class="stat-item">
                <span class="stat-value">{{ monitor.next_update_time }}</span>
                <span class="stat-label">下次自动更新</span>
            </div>
        </div>
        <p style="text-align:right; color:#888; font-size:12px;">最后更新: {{ monitor.last_update_time }}</p>
    </div>

    <div class="card">
        <h2>订阅地址</h2>
        <a href="/playlist.m3u" target="_blank">{{ request.url_root }}playlist.m3u</a>
    </div>

    <div class="card">
        <h2>实时日志 (最近100条)</h2>
        <div class="logs">
            <!-- 直接遍历 Python 传入的 reversed 列表 -->
            {% for log in logs %}
            <div class="log-entry">{{ log }}</div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""

# --- Flask 路由 ---
@app.route('/')
def home():
    return redirect(url_for('debug_page'))

@app.route('/debug')
def debug_page():
    try:
        # 关键修复：创建列表副本并在此处反转，防止模板渲染时列表发生变化
        # list() 创建副本，[::-1] 进行反转
        safe_logs = list(web_log_handler.log_records)[::-1]
        
        return render_template_string(
            DEBUG_HTML, 
            monitor=monitor, 
            logs=safe_logs
        )
    except Exception as e:
        # 如果页面渲染出错，返回简单的错误文本，方便调试
        return f"Error rendering page: {str(e)}", 500

@app.route('/trigger_update')
def trigger_update():
    if not monitor.is_running:
        threading.Thread(target=monitor.update_playlist).start()
    return redirect(url_for('debug_page'))

@app.route('/playlist.m3u')
def playlist():
    return Response(current_playlist_content, mimetype='audio/x-mpegurl')

# --- 定时调度 ---
def run_schedule():
    monitor.update_playlist()
    interval = int(os.getenv('FETCH_INTERVAL', 300))
    schedule.every(interval).seconds.do(monitor.update_playlist)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    t = threading.Thread(target=run_schedule)
    t.daemon = True
    t.start()
    
    port = int(os.getenv('PORT', 8080))
    # 增加 threaded=True，虽然是默认的，但显式写出以确保
    app.run(host='0.0.0.0', port=port, threaded=True)
