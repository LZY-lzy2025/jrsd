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
from flask import Flask, Response

# 配置日志
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 全局变量存储最新的 M3U 内容，默认空
current_playlist_content = "#EXTM3U\n"

app = Flask(__name__)

class LiveMonitor:
    def __init__(self):
        # 你的源地址
        self.source_url = os.getenv('SOURCE_URL', "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5")
        self.ua = UserAgent()
        self.headers = {
            'User-Agent': self.ua.random,
            'Referer': 'https://www.jrs21.com/',
        }

    def fetch_source_js(self):
        try:
            # 添加时间戳防止缓存
            timestamp = int(time.time() * 1000)
            url_with_ts = f"{self.source_url}&_={timestamp}"
            logger.info(f"正在获取最新比赛列表...")
            resp = requests.get(url_with_ts, headers=self.headers, timeout=15)
            resp.encoding = 'utf-8'
            if resp.status_code == 200:
                return resp.text
            return None
        except Exception as e:
            logger.error(f"获取源出错: {e}")
            return None

    def parse_js_to_html(self, js_content):
        pattern = re.compile(r"document\.write\('(.*?)'\);")
        matches = pattern.findall(js_content)
        return "".join(matches)

    def extract_matches(self, html_content):
        soup = BeautifulSoup(html_content, 'lxml')
        matches = []
        game_items = soup.find_all('ul', class_='item')
        
        for item in game_items:
            try:
                league_tag = item.find('li', class_='lab_events')
                league = league_tag.get_text(strip=True) if league_tag else "未知"
                
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
        # 1. 匹配 m3u8
        m3u8_pattern = re.compile(r"['\"](http[^'\"]+?\.m3u8.*?)['\"]")
        direct_match = m3u8_pattern.search(html)
        if direct_match: return direct_match.group(1)

        # 2. Iframe 穿透
        soup = BeautifulSoup(html, 'lxml')
        iframe = soup.find('iframe')
        if iframe:
            src = iframe.get('src')
            if src:
                if not src.startswith('http'): src = urllib.parse.urljoin(base_url, src)
                try:
                    with requests.Session() as s:
                        r = s.get(src, headers=self.headers, timeout=5)
                        iframe_match = m3u8_pattern.search(r.text)
                        if iframe_match: return iframe_match.group(1)
                except: pass
        return None

    def update_playlist(self):
        global current_playlist_content
        logger.info("开始执行更新任务...")
        
        js_code = self.fetch_source_js()
        if not js_code: return

        html = self.parse_js_to_html(js_code)
        matches = self.extract_matches(html)
        
        if not matches:
            logger.warning("未提取到比赛")
            return

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
                    time.sleep(0.2)
                except: continue
        
        # 生成新内容
        new_content = "#EXTM3U\n"
        for s in valid_streams:
            new_content += f'#EXTINF:-1 group-title="{s["group"]}", {s["name"]}\n'
            new_content += f"{s['url']}\n"
        
        current_playlist_content = new_content
        logger.info(f"更新完成，当前包含 {len(valid_streams)} 个有效源")

# --- Web 路由 ---
@app.route('/')
def home():
    return "JRS Monitor is Running. Access /playlist.m3u to get the subscription."

@app.route('/playlist.m3u')
def playlist():
    # 返回 m3u8 类型的内容
    return Response(current_playlist_content, mimetype='audio/x-mpegurl')

# --- 定时任务线程 ---
def run_schedule():
    monitor = LiveMonitor()
    # 启动时先跑一次
    monitor.update_playlist()
    
    interval = int(os.getenv('FETCH_INTERVAL', 300))
    schedule.every(interval).seconds.do(monitor.update_playlist)
    
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    # 启动后台线程
    t = threading.Thread(target=run_schedule)
    t.daemon = True
    t.start()
    
    # 启动 Web 服务，端口 8080
    port = int(os.getenv('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
