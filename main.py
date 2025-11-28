import re
import requests
import urllib.parse
import base64
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
        self.max_records = 200  # 增加日志保留条数

    def emit(self, record):
        try:
            log_entry = self.format(record)
            self.log_records.append(log_entry)
            if len(self.log_records) > self.max_records:
                self.log_records.pop(0)
        except Exception:
            self.handleError(record)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%H:%M:%S')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

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
                        data_play = a.get('data-play')
                        href = a.get('href')
                        
                        # 同时收集 data-play 和 href，增加成功率
                        candidates = []
                        if data_play and data_play.startswith('http'):
                            candidates.append(data_play)
                        if href and href.startswith('http') and href != "javascript:void(0)":
                            candidates.append(href)
                            
                        # 去重
                        candidates = list(set(candidates))
                        
                        if candidates:
                            links.append({'title': title, 'urls': candidates})

                if links:
                    matches.append({'name': match_name, 'time': time_val, 'links': links})
            except:
                continue
        return matches

    def deep_decode(self, html, current_url, depth=0):
        if depth > 2: # 增加递归深度到 2
            return None

        # 1. 直接匹配 .m3u8
        m3u8_pattern = re.compile(r"['\"]((?:http[s]?://|/)[^'\"]+?\.m3u8(?:[^'\"]*)?)['\"]")
        direct_match = m3u8_pattern.search(html)
        if direct_match:
            found_url = direct_match.group(1)
            if found_url.startswith('/'):
                found_url = urllib.parse.urljoin(current_url, found_url)
            if found_url.startswith('http'):
                return found_url

        # 2. 匹配播放器参数
        player_pattern = re.compile(r"(?:source|file|video|url)\s*[:=]\s*['\"](http[^'\"]+)['\"]")
        player_match = player_pattern.search(html)
        if player_match:
            return player_match.group(1)

        # 3. Base64 解码
        b64_candidates = re.findall(r"['\"]([a-zA-Z0-9+/=]{30,})['\"]", html)
        for cand in b64_candidates:
            try:
                decoded_bytes = base64.b64decode(cand)
                decoded_str = decoded_bytes.decode('utf-8', errors='ignore')
                if '.m3u8' in decoded_str and decoded_str.strip().startswith('http'):
                    return decoded_str.strip()
            except:
                pass

        # 4. Iframe 挖掘
        soup = BeautifulSoup(html, 'lxml')
        iframes = soup.find_all('iframe')
        for iframe in iframes:
            src = iframe.get('src')
            if src:
                if not src.startswith('http'):
                    src = urllib.parse.urljoin(current_url, src)
                
                logger.info(f"    {'  '*depth}↳ 尝试 iframe: {src[:40]}...")
                try:
                    sub_headers = self.headers.copy()
                    sub_headers['Referer'] = current_url
                    
                    with requests.Session() as s:
                        # 缩短超时，快速失败
                        r = s.get(src, headers=sub_headers, timeout=5)
                        if r.status_code == 200:
                            result = self.deep_decode(r.text, src, depth=depth+1)
                            if result: return result
                except requests.exceptions.NameResolutionError:
                    logger.warning(f"    {'  '*depth}DNS解析失败: {urllib.parse.urlparse(src).netloc}")
                except Exception:
                    # 忽略子线路错误，继续尝试下一个
                    pass
        
        return None

    def update_playlist(self):
        global current_playlist_content
        
        if self.is_running:
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
                        # link['urls'] 是一个列表，包含了 data-play 和 href
                        found_for_this_link = False
                        
                        for target_url in link['urls']:
                            if found_for_this_link: break # 如果该线路已经找到源，就不试备用链接了
                            
                            try:
                                final_url = None
                                # 如果直接是 m3u8
                                if '.m3u8' in target_url:
                                    final_url = target_url
                                else:
                                    logger.info(f"解析: {match['name']} ({link['title']}) -> {urllib.parse.urlparse(target_url).netloc}")
                                    try:
                                        resp = requests.get(target_url, headers=self.headers, timeout=6)
                                        if resp.status_code == 200:
                                            final_url = self.deep_decode(resp.text, target_url)
                                    except requests.exceptions.ConnectionError:
                                        logger.warning(f"  连接失败，尝试下一个候选地址...")
                                        continue
                                    except Exception as e:
                                        logger.warning(f"  请求异常: {str(e)[:50]}")
                                        continue
                                
                                if final_url:
                                    logger.info(f"  ✅ 成功: {final_url[:50]}...")
                                    valid_streams.append({
                                        'group': "JRS直播",
                                        'name': f"{match['time']} {match['name']} - {link['title']}",
                                        'url': final_url
                                    })
                                    found_for_this_link = True
                                else:
                                    # logger.info(f"  ❌ 此地址未发现源")
                                    pass
                                    
                                time.sleep(0.1)
                            except Exception:
                                continue
                
                new_content = "#EXTM3U\n"
                for s in valid_streams:
                    new_content += f'#EXTINF:-1 group-title="{s["group"]}", {s["name"]}\n'
                    new_content += f"{s['url']}\n"
                
                current_playlist_content = new_content
                self.stream_count = len(valid_streams)
                logger.info(f"更新成功! 找到 {self.stream_count} 个有效源")
            else:
                logger.warning("未获取到比赛数据")

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
    <meta charset="utf-8">
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
        .logs { background: #2d2d2d; color: #ccc; padding: 15px; border-radius: 6px; height: 500px; overflow-y: scroll; font-family: monospace; font-size: 11px; line-height: 1.4; }
        .log-entry { margin-bottom: 4px; border-bottom: 1px solid #444; padding-bottom: 2px; word-break: break-all; }
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
                <span class="stat-value" style="color: {% if monitor.stream_count > 0 %}green{% else %}red{% endif %}">
                    {{ monitor.stream_count }}
                </span>
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
        <h2>实时日志 (最近200条)</h2>
        <div class="logs">
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
        safe_logs = list(web_log_handler.log_records)[::-1]
        return render_template_string(DEBUG_HTML, monitor=monitor, logs=safe_logs)
    except Exception as e:
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
    time.sleep(3)
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
    app.run(host='0.0.0.0', port=port, threaded=True)
