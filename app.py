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

# Configure logging for better debugging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Use Railway's writable /tmp
DOWNLOAD_FOLDER = "/tmp/downloads"
PROGRESS_DIR = "/tmp/progress"
COOKIES_FILE = "/tmp/cookies.txt"

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(PROGRESS_DIR, exist_ok=True)

# ------------------------ Progress Helpers ------------------------
def save_progress(file_id, data):
    """Save download progress to a JSON file."""
    try:
        filepath = os.path.join(PROGRESS_DIR, f"{file_id}.json")
        with open(filepath, 'w') as f:
            json.dump({**data, "timestamp": time.time()}, f)
    except Exception as e:
        logger.error(f"save_progress error: {e}")

def load_progress(file_id):
    """Load progress from JSON file (shared across workers)."""
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
    """Remove progress file after download finishes or fails."""
    try:
        filepath = os.path.join(PROGRESS_DIR, f"{file_id}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        logger.error(f"delete_progress error: {e}")

def clean_ansi(text):
    """Strip ANSI escape codes from yt‑dlp output."""
    if not text:
        return "0%"
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', str(text))

def progress_hook(d, file_id):
    """yt‑dlp progress callback – saves progress to file."""
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
    """Load cookies from environment variable or local file."""
    yt_cookies = os.environ.get('YT_COOKIES', '')
    if yt_cookies and len(yt_cookies) > 10:
        try:
            with open(COOKIES_FILE, 'w') as f:
                f.write(yt_cookies)
            logger.info("Cookies loaded from environment variable")
            return
        except Exception as e:
            logger.error(f"Failed to write cookies from env: {e}")

    if os.path.exists("cookies.txt"):
        try:
            shutil.copy("cookies.txt", COOKIES_FILE)
            logger.info("Cookies loaded from local file")
            return
        except Exception as e:
            logger.error(f"Failed to copy cookies: {e}")

    logger.warning("No cookies found. Some videos may require login.")

setup_cookies()

# ------------------------ Constants ------------------------
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

# ------------------------ Helper Functions ------------------------
def cleanup_file(filepath, file_id=None, delay=120):
    """Delete file after delay and clean up progress."""
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
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

def get_base_opts(referer_url=None, force_generic=False):
    """Universal extractor options – works for any yt‑dlp supported site."""
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

    if referer_url:
        try:
            parsed = urllib.parse.urlparse(referer_url)
            headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
        except Exception:
            pass

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
        'geo_bypass_country': 'US',
    }

    if os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE

    # Configure extractor arguments
    if force_generic:
        opts['extractor_args'] = {
            'generic': {'hls': True, 'dash': True, 'pcm': True}
        }
    else:
        opts['extractor_args'] = {
            'generic': {'hls': True, 'dash': True, 'pcm': True},
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
                'player_skip': ['webpage', 'config'],
            },
            'dailymotion': {'geo_bypass': True},
            'facebook': {'api_key': None},
        }

    return opts

def get_ydl_opts(output_path=None, quality='720p', format_type='video',
                 file_id=None, referer_url=None, force_generic=False):
    """Return yt‑dlp options for the requested format/quality."""
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
            # Ensure no double dot when the extension is added later
            opts['outtmpl'] = output_path + '.%(ext)s'
        return opts

    # Video quality mapping (including 1440p)
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

def should_retry_with_generic(error_msg):
    """Decide whether to fall back to generic extractor."""
    error_lower = str(error_msg).lower()
    patterns = [
        'cannot parse', 'no video formats found', 'unable to extract',
        'formats not found', 'unsupported url', 'this video is unavailable',
        'content too short', 'http error 403', 'http error 404',
        'not available', 'geo restricted', 'sign in',
    ]
    return any(p in error_lower for p in patterns)

# ------------------------ Routes ------------------------
@app.route('/')
def home():
    return jsonify({
        "status": "Universal Downloader Active",
        "version": "6.1.0",
        "capabilities": "All yt-dlp supported platforms",
        "modes": "Platform-specific + Generic fallback"
    })

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

@app.route('/progress/<file_id>', methods=['GET'])
def get_progress(file_id):
    """Return current download progress."""
    return jsonify(load_progress(file_id))

@app.route('/info', methods=['POST', 'OPTIONS'])
def get_info():
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    data = request.get_json()
    url = (data or {}).get('url', '').strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Friendly platform name
    platform_name = "Universal"
    if 'youtube.com' in url or 'youtu.be' in url:
        platform_name = "YouTube"
    elif 'facebook.com' in url or 'fb.watch' in url:
        platform_name = "Facebook"
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

    try:
        # Try with platform‑specific extractor first
        opts = get_base_opts(referer_url=url, force_generic=False)
        opts['skip_download'] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                if should_retry_with_generic(str(e)):
                    logger.info(f"Platform extractor failed, trying generic: {e}")
                    opts = get_base_opts(referer_url=url, force_generic=True)
                    opts['skip_download'] = True
                    with yt_dlp.YoutubeDL(opts) as ydl2:
                        info = ydl2.extract_info(url, download=False)
                else:
                    raise e

            if not info:
                raise Exception("No video found")

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

            # If no heights found, assume standard qualities (generic extractor)
            if not qualities_set:
                qualities_set = {'720p', '480p', '360p'}

            # Keep the order logical
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
        err = str(e)
        if 'drm' in err.lower():
            return jsonify({
                "error": "❌ DRM Protected",
                "details": "Netflix, Prime Video, Disney+ etc. cannot be downloaded due to encryption."
            }), 400
        elif 'unsupported url' in err.lower():
            return jsonify({
                "error": "❌ Unsupported URL",
                "details": "This site is not supported. Try YouTube, Vimeo, Dailymotion, Twitter, Instagram, TikTok, or direct video links."
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
        # First attempt with platform‑specific extractor
        try:
            opts = get_ydl_opts(output_path, quality, format_type, file_id,
                                referer_url=url, force_generic=False)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            # Retry with generic extractor if appropriate
            if should_retry_with_generic(str(e)):
                logger.info(f"Download retry with generic: {e}")
                save_progress(file_id, {
                    "status": "Retrying with universal method...",
                    "percent": "5%",
                    "speed": "0 B/s",
                    "eta": "Unknown"
                })
                opts = get_ydl_opts(output_path, quality, format_type, file_id,
                                    referer_url=url, force_generic=True)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            else:
                raise e

        if not info:
            raise Exception("Download failed")

        title = info.get('title', 'video')

        # Locate the downloaded file
        downloaded_file = None
        # First check expected extensions
        for ext in ['mp4', 'webm', 'mkv', 'm4a', 'mp3', 'mov']:
            candidate = f"{output_path}.{ext}"
            if os.path.exists(candidate):
                downloaded_file = candidate
                break

        # Fallback: scan download folder for files starting with file_id
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
                    chunk = f.read(1024 * 1024)  # 1 MB chunks
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
        # Provide user‑friendly messages
        if 'drm' in err.lower():
            msg = "❌ DRM Protected: This content is encrypted and cannot be downloaded."
        elif 'no video formats found' in err.lower():
            msg = "❌ No video formats found. The video may be geo-blocked, private, or requires login."
        elif 'cannot parse' in err.lower():
            msg = "❌ Cannot parse video data. The site may have changed their layout."
        elif 'unsupported url' in err.lower():
            msg = "❌ Unsupported URL. Try: YouTube, Vimeo, Dailymotion, Twitter, Instagram, TikTok."
        elif '403' in err:
            msg = "❌ Access denied (403). The site is blocking downloads."
        elif '404' in err:
            msg = "❌ Video not found (404). The URL may be invalid or expired."
        elif 'sign in' in err.lower() or 'login' in err.lower():
            msg = "❌ Login required. Add cookies in Railway dashboard for private videos."
        else:
            msg = f"❌ {err[:200]}"

        delete_progress(file_id)
        return jsonify({"error": msg}), 400

    except Exception as e:
        delete_progress(file_id)
        logger.exception("Unexpected download error")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
