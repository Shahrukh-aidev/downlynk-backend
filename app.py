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
from datetime import datetime

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Railway requires /tmp for write access
DOWNLOAD_FOLDER = "/tmp/downloads"
PROGRESS_DIR = "/tmp/progress" 
COOKIES_FILE = "/tmp/cookies.txt"

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(PROGRESS_DIR, exist_ok=True)

# ========== FILE-BASED PROGRESS (Required for Railway multi-worker) ==========
def save_progress(file_id, data):
    """Save progress to shared file system"""
    try:
        filepath = os.path.join(PROGRESS_DIR, f"{file_id}.json")
        with open(filepath, 'w') as f:
            json.dump({**data, "timestamp": time.time()}, f)
    except Exception as e:
        print(f"Progress save error: {e}")

def load_progress(file_id):
    """Load progress from file (works across all workers)"""
    try:
        filepath = os.path.join(PROGRESS_DIR, f"{file_id}.json")
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                return json.load(f)
    except:
        pass
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
    except:
        pass

def clean_ansi(text):
    """Remove color codes from yt-dlp output"""
    if not text:
        return "0%"
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', str(text))

def progress_hook(d, file_id):
    """Real-time progress capture"""
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
        print(f"Hook error: {e}")

# ========== FFMPEG SETUP ==========
def setup_ffmpeg():
    ffmpeg_dir = "/tmp/ffmpeg"
    ffmpeg_bin = os.path.join(ffmpeg_dir, "ffmpeg")
    ffprobe_bin = os.path.join(ffmpeg_dir, "ffprobe")
    
    if os.path.exists(ffmpeg_bin) and os.path.exists(ffprobe_bin):
        return ffmpeg_bin, ffprobe_bin
    
    print("📥 Downloading FFmpeg...")
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
        print("✅ FFmpeg ready")
        return ffmpeg_bin, ffprobe_bin
        
    except Exception as e:
        print(f"⚠️ FFmpeg error: {e}")
        return None, None

FFMPEG_PATH, FFPROBE_PATH = setup_ffmpeg()

# ========== COOKIES SETUP ==========
def setup_cookies():
    # Try env var first (Railway Secret)
    yt_cookies = os.environ.get('YT_COOKIES', '')
    if yt_cookies and len(yt_cookies) > 50:
        try:
            with open(COOKIES_FILE, 'w') as f:
                f.write(yt_cookies)
            print("✅ Cookies from env")
            return
        except:
            pass
    
    # Try URL
    cookies_url = os.environ.get('COOKIES_URL', '')
    if cookies_url:
        try:
            urllib.request.urlretrieve(cookies_url, COOKIES_FILE)
            print("✅ Cookies from URL")
            return
        except:
            pass
    
    # Fallback to local file
    if os.path.exists("cookies.txt"):
        try:
            shutil.copy("cookies.txt", COOKIES_FILE)
            print("✅ Cookies from file")
            return
        except:
            pass
    
    print("⚠️ No cookies")

setup_cookies()

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

def cleanup_file(filepath, file_id=None, delay=120):
    def delete():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
            if file_id:
                delete_progress(file_id)
        except: 
            pass
    threading.Thread(target=delete, daemon=True).start()

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

def get_base_opts(referer_url=None):
    """
    Universal extractor options - supports YouTube, movies, any site
    """
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'cross-site',
        'Cache-Control': 'no-cache',
    }
    
    # Set referer for movie sites (crucial for hindimovies.to, etc)
    if referer_url:
        try:
            parsed = urllib.parse.urlparse(referer_url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"
            headers['Referer'] = referer
        except:
            pass
    
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 120,  # Longer for slow movie servers
        'retries': 20,
        'fragment_retries': 20,
        'skip_unavailable_fragments': True,
        'http_headers': headers,
        'geo_bypass': True,
        'geo_bypass_country': 'US',
        # CRITICAL: Enable generic extractor for unknown sites
        'extractor_args': {
            'generic': {
                'hls': True,
                'dash': True,
                'pcm': True,
            },
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
                'player_skip': ['webpage', 'config'],
            }
        },
    }
    
    if os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
        
    return opts

def get_ydl_opts(output_path=None, quality='720p', format_type='video', file_id=None, referer_url=None):
    opts = get_base_opts(referer_url)
    
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

    # Universal quality selection
    quality_map = {
        '4k':    'bestvideo[height<=2160]+bestaudio/best',
        '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
        '720p':  'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
        '480p':  'bestvideo[height<=480]+bestaudio/best[height<=480]/best',
        '360p':  'bestvideo[height<=360]+bestaudio/best[height<=360]/best',
        '240p':  'bestvideo[height<=240]+bestaudio/best[height<=240]/best',
        'best':  'bestvideo+bestaudio/best',
    }

    opts['format'] = quality_map.get(quality, quality_map['720p'])
    opts['merge_output_format'] = 'mp4'
    opts['concurrent_fragment_downloads'] = 3

    if output_path:
        opts['outtmpl'] = output_path + '.%(ext)s'

    return opts

@app.route('/')
def home():
    return jsonify({
        "status": "Universal Downloader Ready",
        "version": "5.0.0",
        "capabilities": "All Sites (YouTube, Movies, TV, Any URL)",
        "ffmpeg": "available" if FFMPEG_PATH else "missing"
    })

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

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

    try:
        opts = get_base_opts(referer_url=url)
        opts['skip_download'] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not info:
                raise Exception("No video found")

            formats = info.get('formats', [])
            qualities_set = set()
            
            for f in formats:
                h = f.get('height')
                if h and f.get('vcodec') != 'none':
                    if h >= 2160: qualities_set.add('4k')
                    elif h >= 1080: qualities_set.add('1080p')
                    elif h >= 720: qualities_set.add('720p')
                    elif h >= 480: qualities_set.add('480p')
                    elif h >= 360: qualities_set.add('360p')
                    elif h >= 240: qualities_set.add('240p')

            # For generic sites with no format info, assume standard qualities
            if not qualities_set:
                qualities_set = {'720p', '480p', '360p'}

            order = ['4k', '1080p', '720p', '480p', '360p', '240p']
            sorted_qualities = [q for q in order if q in qualities_set]

            return jsonify({
                "title": str(info.get('title', 'Video')),
                "duration": info.get('duration') or 0,
                "thumbnail": str(info.get('thumbnail', '')),
                "uploader": str(info.get('uploader', 'Unknown')),
                "platform": str(info.get('extractor_key', 'Universal')),
                "qualities": sorted_qualities,
                "has_audio": True
            })
            
    except Exception as e:
        err = str(e)
        if 'Unsupported URL' in err:
            return jsonify({
                "error": "Site not supported or DRM protected",
                "details": "Try YouTube, Vimeo, Dailymotion, or non-DRM streaming sites"
            }), 400
        return jsonify({"error": err}), 400

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

    output_path = os.path.join(DOWNLOAD_FOLDER, file_id)
    
    save_progress(file_id, {
        "status": "Starting...",
        "percent": "0%",
        "speed": "0 B/s",
        "eta": "Unknown"
    })

    try:
        opts = get_ydl_opts(output_path, quality, format_type, file_id, referer_url=url)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise Exception("Download failed")
            title = info.get('title', 'video')

        # Find file
        downloaded_file = None
        for ext in ['mp4', 'webm', 'mkv', 'm4a', 'mp3', 'mov']:
            candidate = f"{output_path}.{ext}"
            if os.path.exists(candidate):
                downloaded_file = candidate
                break
        
        if not downloaded_file:
            files = os.listdir(DOWNLOAD_FOLDER)
            for f in files:
                if f.startswith(file_id):
                    downloaded_file = os.path.join(DOWNLOAD_FOLDER, f)
                    break

        if not downloaded_file:
            raise Exception("File not found")

        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip() or "download"
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

    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        msg = "Download failed"
        
        if 'Unsupported URL' in err:
            msg = "❌ Site not supported. Uses DRM or custom player."
        elif '403' in err:
            msg = "❌ Access blocked (403). Site uses bot protection."
        elif 'DRM' in err or 'drm' in err:
            msg = "❌ DRM protected (Netflix, Prime, etc). Cannot download."
        elif '404' in err:
            msg = "❌ Video not found. URL may be expired."
        elif 'sign in' in err.lower() or 'login' in err.lower():
            msg = "❌ Requires login. Try adding cookies in Railway dashboard."
        else:
            msg = f"❌ Error: {err[:150]}"
        
        delete_progress(file_id)
        return jsonify({"error": msg}), 400
        
    except Exception as e:
        delete_progress(file_id)
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
