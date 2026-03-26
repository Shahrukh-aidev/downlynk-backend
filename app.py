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
from datetime import datetime

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ✅ CRITICAL: Use /tmp for Railway's ephemeral storage
DOWNLOAD_FOLDER = "/tmp/downloads"
COOKIES_FILE = "/tmp/cookies.txt"  # Also put cookies in /tmp
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Thread-safe progress storage
download_progress = {}
progress_lock = threading.Lock()

def clean_ansi(text):
    """Remove terminal color codes from yt-dlp output"""
    if not text:
        return "0%"
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', str(text))

def progress_hook(d, file_id):
    """Capture yt-dlp progress updates"""
    try:
        with progress_lock:
            if d['status'] == 'downloading':
                percent = clean_ansi(d.get('_percent_str', '0%')).strip()
                speed = clean_ansi(d.get('_speed_str', 'Unknown')).strip()
                eta = clean_ansi(d.get('_eta_str', 'Unknown')).strip()
                
                download_progress[file_id] = {
                    "status": "Downloading...",
                    "percent": percent,
                    "speed": speed,
                    "eta": eta,
                    "timestamp": time.time()
                }
            elif d['status'] == 'finished':
                download_progress[file_id] = {
                    "status": "Processing file (merging audio/video)...",
                    "percent": "100%",
                    "speed": "0 B/s",
                    "eta": "00:00",
                    "timestamp": time.time()
                }
    except Exception as e:
        print(f"Progress hook error: {e}")

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
        print(f"✅ FFmpeg ready")
        return ffmpeg_bin, ffprobe_bin
        
    except Exception as e:
        print(f"⚠️ FFmpeg download failed: {e}")
        return None, None

FFMPEG_PATH, FFPROBE_PATH = setup_ffmpeg()

# ========== COOKIES SETUP ==========
def setup_cookies():
    # Method 1: From Environment Variable (Railway Secret)
    yt_cookies = os.environ.get('YT_COOKIES', '')
    if yt_cookies and len(yt_cookies) > 10:
        try:
            with open(COOKIES_FILE, 'w') as f:
                f.write(yt_cookies)
            print("✅ Cookies loaded from env var")
            return
        except Exception as e:
            print(f"⚠️ Failed to write cookies from env: {e}")
    
    # Method 2: From URL (GitHub Gist, etc.)
    cookies_url = os.environ.get('COOKIES_URL', '')
    if cookies_url:
        try:
            urllib.request.urlretrieve(cookies_url, COOKIES_FILE)
            print("✅ Cookies downloaded from URL")
            return
        except Exception as e:
            print(f"⚠️ Failed to download cookies: {e}")
    
    # Method 3: Check if cookies.txt exists in repo (committed file)
    if os.path.exists("cookies.txt"):
        try:
            shutil.copy("cookies.txt", COOKIES_FILE)
            print("✅ Cookies copied from repo")
            return
        except Exception as e:
            print(f"⚠️ Failed to copy cookies: {e}")
    
    print("⚠️ No cookies configured")

setup_cookies()

# ========== USER AGENTS ==========
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

def cleanup_file(filepath, file_id=None, delay=120):
    """Delete file after delay and clean up progress"""
    def delete():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                print(f"🗑️ Cleaned up: {filepath}")
        except Exception as e:
            print(f"Cleanup error: {e}")
        finally:
            if file_id:
                with progress_lock:
                    if file_id in download_progress:
                        del download_progress[file_id]
    threading.Thread(target=delete, daemon=True).start()

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

def get_base_opts():
    opts = {
        'quiet': True,  # Keep True, hooks work independently
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 60,
        'retries': 10,
        'fragment_retries': 10,
        'http_headers': {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept-Language': 'en-US,en;q=0.9',
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
                'player_skip': ['webpage', 'config'],
            }
        },
    }
    
    # Check multiple cookie locations
    if os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    elif os.path.exists('/tmp/cookies.txt'):
        opts['cookiefile'] = '/tmp/cookies.txt'
    elif os.path.exists('cookies.txt'):
        opts['cookiefile'] = 'cookies.txt'
        
    return opts

def get_ydl_opts(output_path=None, quality='720p', format_type='video', file_id=None):
    opts = get_base_opts()
    
    # ✅ CRITICAL: Attach progress hook
    if file_id:
        opts['progress_hooks'] = [lambda d: progress_hook(d, file_id)]
        # Enable verbose to ensure hooks fire (only for debugging if needed)
        # opts['verbose'] = True
    
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
    '240p':  'bestvideo[height<=240]+bestaudio/best[height<=240]/best',
}

    opts.update({
        'format': quality_map.get(quality, quality_map['720p']),
        'merge_output_format': 'mp4',
        'concurrent_fragment_downloads': 2,  # Reduced for stability
    })

    if output_path:
        opts['outtmpl'] = output_path + '.%(ext)s'

    return opts

# ========== ROUTES ==========
@app.route('/')
def home():
    cookies_status = "loaded" if os.path.exists(COOKIES_FILE) else "missing"
    ffmpeg_status = "available" if FFMPEG_PATH else "missing"
    return jsonify({
        "status": "Downlynk backend is running!",
        "version": "3.1.0",
        "cookies": cookies_status,
        "ffmpeg": ffmpeg_status,
        "time": datetime.now().isoformat()
    })

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

@app.route('/progress/<file_id>', methods=['GET'])
def get_progress(file_id):
    """Return current progress for a download"""
    with progress_lock:
        data = download_progress.get(file_id, {
            "status": "Initializing...",
            "percent": "0%",
            "speed": "0 B/s",
            "eta": "Unknown"
        })
    return jsonify(data)

@app.route('/info', methods=['POST', 'OPTIONS'])
def get_info():
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    data = request.get_json()
    url = (data or {}).get('url', '').strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        opts = get_base_opts()
        opts['skip_download'] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
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

            order = ['4k', '1080p', '720p', '480p', '360p', '240p']
            sorted_qualities = [q for q in order if q in qualities_set]

            return jsonify({
                "title": str(info.get('title', 'Unknown')),
                "duration": info.get('duration') or 0,
                "thumbnail": str(info.get('thumbnail', '') or ''),
                "uploader": str(info.get('uploader', 'Unknown')),
                "platform": str(info.get('extractor_key', 'Unknown')),
                "qualities": sorted_qualities,
                "has_audio": True,
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 400

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
    
    # Initialize progress
    with progress_lock:
        download_progress[file_id] = {
            "status": "Starting download...",
            "percent": "0%",
            "speed": "0 B/s",
            "eta": "Unknown",
            "timestamp": time.time()
        }

    try:
        opts = get_ydl_opts(output_path, quality, format_type, file_id)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')

        # Find the downloaded file
        downloaded_file = None
        expected_ext = 'mp3' if format_type == 'audio' else 'mp4'
        
        # Check primary expected file
        primary_file = f"{output_path}.{expected_ext}"
        if os.path.exists(primary_file):
            downloaded_file = primary_file
        else:
            # Check all possible extensions
            for ext in ['mp4', 'webm', 'mkv', 'm4a', 'mp3']:
                candidate = f"{output_path}.{ext}"
                if os.path.exists(candidate):
                    downloaded_file = candidate
                    break
        
        # Fallback: search directory
        if not downloaded_file:
            for f in os.listdir(DOWNLOAD_FOLDER):
                if f.startswith(file_id):
                    downloaded_file = os.path.join(DOWNLOAD_FOLDER, f)
                    break

        if not downloaded_file:
            raise Exception("File not found after download")

        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip() or "video"
        ext = downloaded_file.split('.')[-1]
        file_size = os.path.getsize(downloaded_file)
        
        # Update progress to complete
        with progress_lock:
            download_progress[file_id] = {
                "status": "Complete",
                "percent": "100%",
                "speed": "0 B/s",
                "eta": "00:00",
                "timestamp": time.time()
            }

        mime_map = {
            'mp4': 'video/mp4', 'webm': 'video/webm',
            'mkv': 'video/x-matroska', 'mp3': 'audio/mpeg', 'm4a': 'audio/mp4'
        }
        mimetype = mime_map.get(ext, 'application/octet-stream')
        dl_name = f"{safe_title}.{ext}"

        def generate():
            with open(downloaded_file, 'rb') as f:
                while True:
                    chunk = f.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    yield chunk
            # Cleanup after streaming
            cleanup_file(downloaded_file, file_id=file_id, delay=60)

        return Response(
            stream_with_context(generate()),
            mimetype=mimetype,
            headers={
                'Content-Disposition': f'attachment; filename="{dl_name}"',
                'Content-Length': str(file_size),
                'X-Accel-Buffering': 'no',  # Disable nginx buffering for streaming
            }
        )

    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        if '403' in err or 'blocked' in err.lower():
            msg = "YouTube blocked this request. Check cookies."
        elif 'age' in err.lower() or 'sign in' in err.lower():
            msg = "Age verification required. Update YouTube cookies."
        elif 'private' in err.lower():
            msg = "This video is private."
        elif 'copyright' in err.lower():
            msg = "Copyright blocked."
        else:
            msg = f"Download failed: {err[:200]}"
        
        with progress_lock:
            if file_id in download_progress:
                del download_progress[file_id]
        return jsonify({"error": msg}), 400
        
    except Exception as e:
        with progress_lock:
            if file_id in download_progress:
                del download_progress[file_id]
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
