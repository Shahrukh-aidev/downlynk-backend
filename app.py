from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time
import random
import urllib.request
import tarfile
import shutil
import re
import json
import urllib.parse
import logging
import requests
import subprocess
from bs4 import BeautifulSoup
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = "/tmp/downloads"
PROGRESS_DIR = "/tmp/progress"
COOKIES_FILE = "/tmp/cookies.txt"

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(PROGRESS_DIR, exist_ok=True)

# ------------------------ Auto Update yt-dlp ------------------------
def update_yt_dlp():
    """Update yt-dlp to latest version on startup"""
    try:
        logger.info("Checking for yt-dlp updates...")
        result = subprocess.run(['pip', 'install', '--upgrade', 'yt-dlp'], 
                              capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            logger.info("yt-dlp updated successfully")
        else:
            logger.warning(f"yt-dlp update warning: {result.stderr}")
    except Exception as e:
        logger.error(f"Failed to update yt-dlp: {e}")

# Run update on startup
update_yt_dlp()

# ------------------------ Progress Helpers ------------------------
def save_progress(file_id, data):
    try:
        filepath = os.path.join(PROGRESS_DIR, f"{file_id}.json")
        with open(filepath, 'w') as f:
            json.dump({**data, "timestamp": time.time()}, f)
    except Exception as e:
        logger.error(f"save_progress error: {e}")

def load_progress(file_id):
    try:
        filepath = os.path.join(PROGRESS_DIR, f"{file_id}.json")
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"load_progress error: {e}")
    return {
        "status": "Initializing...",
        "percent": "0%",
        "speed": "0 B/s",
        "eta": "Unknown"
    }

def delete_progress(file_id):
    try:
        filepath = os.path.join(PROGRESS_DIR, f"{file_id}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        logger.error(f"delete_progress error: {e}")

def clean_ansi(text):
    if not text:
        return "0%"
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', str(text))

def progress_hook(d, file_id):
    try:
        if d['status'] == 'downloading':
            percent = clean_ansi(d.get('_percent_str', '0%')).strip()
            speed = clean_ansi(d.get('_speed_str', 'Unknown')).strip()
            eta = clean_ansi(d.get('_eta_str', 'Unknown')).strip()
            save_progress(file_id, {
                "status": "Downloading...",
                "percent": percent,
                "speed": speed,
                "eta": eta
            })
        elif d['status'] == 'finished':
            save_progress(file_id, {
                "status": "Processing video...",
                "percent": "100%",
                "speed": "0 B/s",
                "eta": "00:00"
            })
    except Exception as e:
        logger.error(f"progress_hook error: {e}")

# ------------------------ FFmpeg Setup ------------------------
def setup_ffmpeg():
    ffmpeg_dir = "/tmp/ffmpeg"
    ffmpeg_bin = os.path.join(ffmpeg_dir, "ffmpeg")
    ffprobe_bin = os.path.join(ffmpeg_dir, "ffprobe")

    if os.path.exists(ffmpeg_bin) and os.path.exists(ffprobe_bin):
        return ffmpeg_bin, ffprobe_bin

    logger.info("Downloading FFmpeg...")
    os.makedirs(ffmpeg_dir, exist_ok=True)

    try:
        url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        tar_path = "/tmp/ffmpeg.tar.xz"
        urllib.request.urlretrieve(url, tar_path)

        with tarfile.open(tar_path, "r:xz") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    basename = os.path.basename(member.name)
                    if basename == "ffmpeg":
                        tar.extract(member, "/tmp")
                        shutil.move(os.path.join("/tmp", member.name), ffmpeg_bin)
                    elif basename == "ffprobe":
                        tar.extract(member, "/tmp")
                        shutil.move(os.path.join("/tmp", member.name), ffprobe_bin)

        os.chmod(ffmpeg_bin, 0o755)
        os.chmod(ffprobe_bin, 0o755)
        os.remove(tar_path)
        logger.info("FFmpeg ready")
        return ffmpeg_bin, ffprobe_bin

    except Exception as e:
        logger.error(f"FFmpeg download failed: {e}")
        return None, None

FFMPEG_PATH, FFPROBE_PATH = setup_ffmpeg()

# ------------------------ Cookies Setup ------------------------
def setup_cookies():
    """Setup cookies from environment or local file"""
    yt_cookies = os.environ.get('YT_COOKIES', '')
    
    # Try environment variable first (Netscape format)
    if yt_cookies and len(yt_cookies) > 10:
        try:
            # Check if it's base64 encoded
            if yt_cookies.startswith('cookies='):
                yt_cookies = yt_cookies.replace('cookies=', '')
            
            with open(COOKIES_FILE, 'w') as f:
                f.write(yt_cookies)
            logger.info("Cookies loaded from environment")
            return True
        except Exception as e:
            logger.error(f"Failed to write cookies from env: {e}")
    
    # Try local file
    if os.path.exists("cookies.txt"):
        try:
            shutil.copy("cookies.txt", COOKIES_FILE)
            logger.info("Cookies loaded from local file")
            return True
        except Exception as e:
            logger.error(f"Failed to copy cookies: {e}")
    
    logger.warning("No cookies found - some sites may not work")
    return False

def validate_cookies():
    """Validate that cookies file is properly formatted"""
    if not os.path.exists(COOKIES_FILE):
        return False
    
    try:
        with open(COOKIES_FILE, 'r') as f:
            content = f.read()
            # Check if it looks like Netscape format
            if '# Netscape HTTP Cookie File' in content or '\t' in content:
                return True
            # Check if it's JSON cookies
            if content.strip().startswith('[') or content.strip().startswith('{'):
                return True
    except:
        pass
    return False

setup_cookies()

# ------------------------ User Agents & Headers ------------------------
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
]

def get_impersonation_headers(referer_url=None):
    """Generate headers that mimic real browser behavior"""
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'sec-ch-ua': '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'Cache-Control': 'max-age=0',
    }
    
    if referer_url:
        try:
            parsed = urllib.parse.urlparse(referer_url)
            headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
            headers['Origin'] = f"{parsed.scheme}://{parsed.netloc}"
        except:
            pass
    
    return headers

# ------------------------ LinkedIn Extraction ------------------------
def extract_linkedin_video_url(post_url):
    """
    Extract video URL from LinkedIn post using multiple methods
    """
    try:
        session = requests.Session()
        session.headers.update(get_impersonation_headers(post_url))
        
        # Load cookies if available
        if os.path.exists(COOKIES_FILE):
            try:
                with open(COOKIES_FILE, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('#') or not line or '\t' not in line:
                            continue
                        parts = line.split('\t')
                        if len(parts) >= 7:
                            domain, _, path, secure, expires, name, value = parts[:7]
                            if 'linkedin' in domain:
                                session.cookies.set(name, value, domain=domain, path=path)
            except Exception as e:
                logger.error(f"Error loading LinkedIn cookies: {e}")
        
        # Try to get the post page
        resp = session.get(post_url, timeout=15, allow_redirects=True)
        
        if resp.status_code != 200:
            logger.error(f"LinkedIn page fetch failed: {resp.status_code}")
            return None
        
        # Method 1: Look for data-sources in HTML
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Find video tags
        video_tags = soup.find_all('video')
        for video in video_tags:
            if video.get('src'):
                return video['src']
            # Check data-sources attribute
            data_sources = video.get('data-sources', '')
            if data_sources:
                try:
                    sources = json.loads(data_sources.replace('&quot;', '"'))
                    if sources and len(sources) > 0:
                        # Get highest quality
                        best_source = max(sources, key=lambda x: x.get('height', 0))
                        return best_source.get('src')
                except:
                    pass
        
        # Method 2: Look for mp4 in scripts
        mp4_matches = re.findall(r'(https?://[^"\']+\.mp4[^"\']*)', resp.text)
        if mp4_matches:
            return mp4_matches[0].replace('\\u0026', '&')
        
        # Method 3: Look for progressive URLs
        progressive_matches = re.findall(r'(https?://[^"\']*progressive[^"\']*)', resp.text)
        if progressive_matches:
            return progressive_matches[0].replace('\\u0026', '&')
        
        # Method 4: Look for artifacts in JSON-LD
        json_ld_scripts = soup.find_all('script', type='application/ld+json')
        for script in json_ld_scripts:
            try:
                data = json.loads(script.string)
                if 'video' in data:
                    video_data = data['video']
                    if 'contentUrl' in video_data:
                        return video_data['contentUrl']
                    if 'embedUrl' in video_data:
                        return video_data['embedUrl']
            except:
                continue
        
        logger.info("No LinkedIn video URL found in page")
        return None
        
    except Exception as e:
        logger.error(f"LinkedIn extraction error: {e}")
        return None

# ------------------------ Facebook Extraction ------------------------
def extract_facebook_video_url(reel_url):
    """
    Attempt to extract a direct video URL from a Facebook Reel/Video page.
    """
    try:
        session = requests.Session()
        session.headers.update(get_impersonation_headers(reel_url))
        
        # Load cookies
        if os.path.exists(COOKIES_FILE):
            try:
                with open(COOKIES_FILE, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('#') or not line or '\t' not in line:
                            continue
                        parts = line.split('\t')
                        if len(parts) >= 7:
                            domain, _, path, secure, expires, name, value = parts[:7]
                            if 'facebook' in domain:
                                session.cookies.set(name, value, domain=domain, path=path)
            except Exception as e:
                logger.error(f"Error loading Facebook cookies: {e}")
        
        # Try original URL
        resp = session.get(reel_url, timeout=15)
        if resp.status_code != 200:
            # Try mobile version
            mobile_url = reel_url.replace('www.facebook.com', 'm.facebook.com')
            resp = session.get(mobile_url, headers=get_impersonation_headers(mobile_url), timeout=15)
            if resp.status_code != 200:
                logger.info(f"Facebook page fetch failed: {resp.status_code}")
                return None
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Method 1: og:video
        meta = soup.find('meta', property='og:video')
        if meta and meta.get('content'):
            return meta['content']
        
        # Method 2: og:video:url
        meta = soup.find('meta', property='og:video:url')
        if meta and meta.get('content'):
            return meta['content']
        
        # Method 3: <video> tag
        video_tag = soup.find('video')
        if video_tag and video_tag.get('src'):
            return video_tag['src']
        
        # Method 4: Search scripts for video URLs
        scripts = soup.find_all('script')
        for script in scripts:
            if not script.string:
                continue
            # Look for "playable_url"
            match = re.search(r'"playable_url"\s*:\s*"([^"]+)"', script.string)
            if match:
                return match.group(1).replace('\\/', '/').replace('\\u0026', '&')
            # Look for "browser_native_hd_url"
            match = re.search(r'"browser_native_hd_url"\s*:\s*"([^"]+)"', script.string)
            if match:
                return match.group(1).replace('\\/', '/').replace('\\u0026', '&')
            # Look for "browser_native_sd_url"
            match = re.search(r'"browser_native_sd_url"\s*:\s*"([^"]+)"', script.string)
            if match:
                return match.group(1).replace('\\/', '/').replace('\\u0026', '&')
            # Look for any .mp4 URL
            match = re.search(r'(https?://[^"\']+\.mp4[^"\']*)', script.string)
            if match:
                return match.group(1).replace('\\u0026', '&')
        
        logger.info("No direct video URL found in Facebook page")
        return None
        
    except Exception as e:
        logger.error(f"Facebook extraction error: {e}")
        return None

# ------------------------ Helper Functions ------------------------
def cleanup_file(filepath, file_id=None, delay=120):
    def delete():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
            if file_id:
                delete_progress(file_id)
        except Exception as e:
            logger.error(f"cleanup_file error: {e}")
    threading.Thread(target=delete, daemon=True).start()

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

def get_base_opts(referer_url=None, force_generic=False, use_cookies=True):
    """Universal extractor options – works for ALL yt-dlp supported sites"""
    headers = get_impersonation_headers(referer_url)
    
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 120,
        'retries': 20,
        'fragment_retries': 20,
        'skip_unavailable_fragments': True,
        'http_headers': headers,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'cookiesfrombrowser': None,  # Disabled for server environment
    }
    
    if use_cookies and os.path.exists(COOKIES_FILE) and validate_cookies():
        opts['cookiefile'] = COOKIES_FILE
    
    if force_generic:
        opts['extractor_args'] = {
            'generic': {'hls': True, 'dash': True, 'pcm': True}
        }
    else:
        # Multiple YouTube client fallbacks for better success rate
        opts['extractor_args'] = {
            'generic': {'hls': True, 'dash': True, 'pcm': True},
            'youtube': {
                'player_client': ['android_vr', 'ios', 'tv_embedded', 'web', 'android', 'mweb'],
                'player_skip': ['webpage', 'configs', 'js'],
            },
            'dailymotion': {'geo_bypass': True},
            'facebook': {'api_key': None},
        }
    
    return opts

def get_ydl_opts(output_path=None, quality='720p', format_type='video',
                 file_id=None, referer_url=None, force_generic=False):
    opts = get_base_opts(referer_url, force_generic)
    
    if file_id:
        opts['progress_hooks'] = [lambda d: progress_hook(d, file_id)]
    
    if FFMPEG_PATH and FFPROBE_PATH:
        opts['ffmpeg_location'] = FFMPEG_PATH
        opts['ffprobe_location'] = FFPROBE_PATH
    
    if format_type == 'audio':
        opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
        if output_path:
            opts['outtmpl'] = output_path + '.%(ext)s'
        return opts
    
    quality_map = {
        '4k':    'bestvideo[height<=2160]+bestaudio/best',
        '1440p': 'bestvideo[height<=1440]+bestaudio/best[height<=1440]/best',
        '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
        '720p':  'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
        '480p':  'bestvideo[height<=480]+bestaudio/best[height<=480]/best',
        '360p':  'bestvideo[height<=360]+bestaudio/best[height<=360]/best',
        'best':  'bestvideo+bestaudio/best',
    }
    
    opts['format'] = quality_map.get(quality, quality_map['720p'])
    opts['merge_output_format'] = 'mp4'
    opts['concurrent_fragment_downloads'] = 3
    
    if output_path:
        opts['outtmpl'] = output_path + '.%(ext)s'
    
    return opts

# ------------------------ Routes ------------------------
@app.route('/')
def home():
    return jsonify({
        "status": "Universal Downloader Active",
        "version": "7.0.0",
        "capabilities": "All yt-dlp supported platforms + LinkedIn + Facebook fallback",
        "features": "Auto-update yt-dlp, LinkedIn manual extraction, Multi-client YouTube"
    })

@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "cookies_valid": validate_cookies(),
        "ffmpeg_ready": FFMPEG_PATH is not None
    })

@app.route('/progress/<file_id>', methods=['GET'])
def get_progress(file_id):
    return jsonify(load_progress(file_id))

@app.route('/info', methods=['POST', 'OPTIONS'])
def get_info():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    
    data = request.get_json()
    url = (data or {}).get('url', '').strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    
    platform_name = "Universal"
    is_facebook = False
    is_linkedin = False
    
    if 'youtube.com' in url or 'youtu.be' in url:
        platform_name = "YouTube"
    elif 'facebook.com' in url or 'fb.watch' in url:
        platform_name = "Facebook"
        is_facebook = True
    elif 'linkedin.com' in url:
        platform_name = "LinkedIn"
        is_linkedin = True
    elif 'dailymotion.com' in url:
        platform_name = "Dailymotion"
    elif 'vimeo.com' in url:
        platform_name = "Vimeo"
    elif 'twitter.com' in url or 'x.com' in url:
        platform_name = "Twitter/X"
    elif 'instagram.com' in url:
        platform_name = "Instagram"
    elif 'tiktok.com' in url:
        platform_name = "TikTok"
    
    info = None
    last_error = None
    
    # Try 1: Platform-specific extractor
    try:
        opts = get_base_opts(referer_url=url, force_generic=False)
        opts['skip_download'] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        last_error = str(e)
        logger.info(f"Platform extractor failed: {e}")
    
    # Try 2: Generic extractor (fallback)
    if not info:
        try:
            logger.info("Retrying with generic extractor...")
            opts = get_base_opts(referer_url=url, force_generic=True)
            opts['skip_download'] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            last_error = str(e)
            logger.info(f"Generic extractor also failed: {e}")
    
    # Try 3: Manual extraction for specific platforms
    if not info:
        direct_url = None
        if is_linkedin:
            logger.info("Attempting manual LinkedIn extraction...")
            direct_url = extract_linkedin_video_url(url)
        elif is_facebook:
            logger.info("Attempting manual Facebook extraction...")
            direct_url = extract_facebook_video_url(url)
        
        if direct_url:
            logger.info(f"Manual extraction found direct URL: {direct_url[:100]}...")
            try:
                opts = get_base_opts(referer_url=direct_url, force_generic=True)
                opts['skip_download'] = True
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(direct_url, download=False)
                    # Override title if it's generic
                    if info and (not info.get('title') or info.get('title') == 'video'):
                        info['title'] = f"{platform_name} Video"
            except Exception as e:
                last_error = str(e)
                logger.info(f"Manual extraction fallback failed: {e}")
    
    # Process results
    if info:
        try:
            formats = info.get('formats', [])
            qualities_set = set()
            for f in formats:
                h = f.get('height')
                if h and f.get('vcodec') != 'none':
                    if h >= 2160: qualities_set.add('4k')
                    elif h >= 1440: qualities_set.add('1440p')
                    elif h >= 1080: qualities_set.add('1080p')
                    elif h >= 720: qualities_set.add('720p')
                    elif h >= 480: qualities_set.add('480p')
                    elif h >= 360: qualities_set.add('360p')
            
            if not qualities_set:
                qualities_set = {'720p', '480p', '360p'}
            
            order = ['4k', '1440p', '1080p', '720p', '480p', '360p']
            sorted_qualities = [q for q in order if q in qualities_set]
            
            return jsonify({
                "title": str(info.get('title', f'{platform_name} Video')),
                "duration": info.get('duration') or 0,
                "thumbnail": str(info.get('thumbnail', '')),
                "uploader": str(info.get('uploader', 'Unknown')),
                "platform": platform_name,
                "qualities": sorted_qualities,
                "has_audio": True
            })
        except Exception as e:
            logger.error(f"Error processing info: {e}")
            return jsonify({"error": "Failed to process video info"}), 400
    
    # All attempts failed
    if last_error:
        if 'drm' in last_error.lower():
            return jsonify({
                "error": "❌ DRM Protected",
                "details": "This content uses encryption and cannot be downloaded."
            }), 400
        elif 'sign in' in last_error.lower() or 'login' in last_error.lower():
            return jsonify({
                "error": "❌ Login Required",
                "details": f"This {platform_name} content requires authentication. Please update cookies."
            }), 400
        elif 'unsupported url' in last_error.lower():
            return jsonify({
                "error": "❌ Unsupported URL",
                "details": "This site is not supported. Try YouTube, Vimeo, Dailymotion, Twitter, Instagram, TikTok, LinkedIn, or direct MP4 links."
            }), 400
        else:
            return jsonify({
                "error": f"❌ Cannot extract video: {last_error[:150]}"
            }), 400
    
    return jsonify({"error": "Unknown error"}), 400

@app.route('/download', methods=['POST', 'OPTIONS'])
def download_video():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    
    data = request.get_json()
    url = (data or {}).get('url', '').strip()
    quality = (data or {}).get('quality', '720p')
    format_type = (data or {}).get('format', 'video')
    file_id = (data or {}).get('file_id', str(uuid.uuid4()))
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    
    is_facebook = 'facebook.com' in url or 'fb.watch' in url
    is_linkedin = 'linkedin.com' in url
    is_youtube = 'youtube.com' in url or 'youtu.be' in url
    
    output_path = os.path.join(DOWNLOAD_FOLDER, file_id)
    save_progress(file_id, {
        "status": "Starting...",
        "percent": "0%",
        "speed": "0 B/s",
        "eta": "Unknown"
    })
    
    info = None
    last_error = None
    direct_url = None
    
    # For LinkedIn and Facebook, try manual extraction first if yt-dlp fails
    if is_linkedin:
        logger.info("Attempting LinkedIn manual extraction for download...")
        direct_url = extract_linkedin_video_url(url)
    elif is_facebook:
        logger.info("Attempting Facebook manual extraction for download...")
        direct_url = extract_facebook_video_url(url)
    
    urls_to_try = [url]
    if direct_url:
        urls_to_try.insert(0, direct_url)  # Try direct URL first
    
    for try_url in urls_to_try:
        # Try 1: Platform-specific
        if not info:
            try:
                opts = get_ydl_opts(output_path, quality, format_type, file_id,
                                  referer_url=try_url, force_generic=False)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(try_url, download=True)
                    break
            except Exception as e:
                last_error = str(e)
                logger.info(f"Platform download failed for {try_url[:50]}: {e}")
        
        # Try 2: Generic fallback
        if not info:
            try:
                logger.info("Retrying download with generic extractor...")
                save_progress(file_id, {
                    "status": "Retrying with universal method...",
                    "percent": "5%",
                    "speed": "0 B/s",
                    "eta": "Unknown"
                })
                opts = get_ydl_opts(output_path, quality, format_type, file_id,
                                  referer_url=try_url, force_generic=True)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(try_url, download=True)
                    break
            except Exception as e:
                last_error = str(e)
                logger.info(f"Generic download also failed for {try_url[:50]}: {e}")
    
    if not info:
        delete_progress(file_id)
        if last_error:
            if 'drm' in last_error.lower():
                msg = "❌ DRM Protected: This content is encrypted and cannot be downloaded."
            elif 'no video formats found' in last_error.lower():
                msg = "❌ No video formats found. The video may be geo-blocked, private, or requires login."
            elif 'sign in' in last_error.lower() or 'login' in last_error.lower():
                msg = "❌ Login required. This video requires authentication cookies."
            elif 'unsupported url' in last_error.lower():
                msg = "❌ Unsupported URL. Try: YouTube, Vimeo, Dailymotion, Twitter, Instagram, TikTok, LinkedIn."
            elif '403' in last_error:
                msg = "❌ Access denied (403). The site is blocking downloads."
            elif '404' in last_error:
                msg = "❌ Video not found (404). Check if the URL is correct."
            else:
                msg = f"❌ Download failed: {last_error[:200]}"
            return jsonify({"error": msg}), 400
        return jsonify({"error": "❌ Download failed for unknown reason"}), 400
    
    try:
        title = info.get('title', 'video')
        
        # Find downloaded file
        downloaded_file = None
        for ext in ['mp4', 'webm', 'mkv', 'm4a', 'mp3', 'mov']:
            candidate = f"{output_path}.{ext}"
            if os.path.exists(candidate):
                downloaded_file = candidate
                break
        
        if not downloaded_file:
            for f in os.listdir(DOWNLOAD_FOLDER):
                if f.startswith(file_id):
                    downloaded_file = os.path.join(DOWNLOAD_FOLDER, f)
                    break
        
        if not downloaded_file:
            raise Exception("File not found after download")
        
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
        if not safe_title:
            safe_title = "download"
        
        ext = downloaded_file.split('.')[-1]
        file_size = os.path.getsize(downloaded_file)
        
        save_progress(file_id, {
            "status": "Complete",
            "percent": "100%",
            "speed": "0 B/s",
            "eta": "00:00"
        })
        
        mime_map = {
            'mp4': 'video/mp4', 'webm': 'video/webm', 'mov': 'video/quicktime',
            'mkv': 'video/x-matroska', 'mp3': 'audio/mpeg', 'm4a': 'audio/mp4'
        }
        mimetype = mime_map.get(ext, 'application/octet-stream')
        dl_name = f"{safe_title}.{ext}"
        
        def generate():
            with open(downloaded_file, 'rb') as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            cleanup_file(downloaded_file, file_id=file_id, delay=60)
        
        return Response(
            stream_with_context(generate()),
            mimetype=mimetype,
            headers={
                'Content-Disposition': f'attachment; filename="{dl_name}"',
                'Content-Length': str(file_size),
                'X-Accel-Buffering': 'no',
            }
        )
    
    except Exception as e:
        logger.exception("Error serving file")
        delete_progress(file_id)
        return jsonify({"error": f"❌ Server error: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
