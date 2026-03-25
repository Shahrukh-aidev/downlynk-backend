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

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ✅ AUTO-DOWNLOAD FFMPEG FOR RAILWAY/RENDER (fixes 1080p/4K/audio)
def setup_ffmpeg():
    """Download static FFmpeg binary if system doesn't have it"""
    ffmpeg_dir = "/tmp/ffmpeg"
    ffmpeg_bin = os.path.join(ffmpeg_dir, "ffmpeg")
    ffprobe_bin = os.path.join(ffmpeg_dir, "ffprobe")
    
    # If already downloaded, return paths
    if os.path.exists(ffmpeg_bin) and os.path.exists(ffprobe_bin):
        return ffmpeg_bin, ffprobe_bin
    
    print("📥 FFmpeg not found in system, downloading static binary...")
    os.makedirs(ffmpeg_dir, exist_ok=True)
    
    try:
        # Reliable static build for Linux x64
        url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        tar_path = "/tmp/ffmpeg.tar.xz"
        
        # Download
        urllib.request.urlretrieve(url, tar_path)
        
        # Extract only ffmpeg and ffprobe
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
        
        # Make executable
        os.chmod(ffmpeg_bin, 0o755)
        os.chmod(ffprobe_bin, 0o755)
        os.remove(tar_path)
        
        print(f"✅ FFmpeg ready at: {ffmpeg_bin}")
        return ffmpeg_bin, ffprobe_bin
        
    except Exception as e:
        print(f"⚠️ FFmpeg download failed: {e}")
        return None, None

# Initialize FFmpeg paths globally
FFMPEG_PATH, FFPROBE_PATH = setup_ffmpeg()

# ✅ Write cookies from environment variable on startup
def setup_cookies():
    yt_cookies = os.environ.get('YT_COOKIES', '')
    if yt_cookies and len(yt_cookies) > 10:
        with open(COOKIES_FILE, 'w') as f:
            f.write(yt_cookies)
        print("✅ YouTube cookies loaded from environment")
    else:
        print("⚠️ No YouTube cookies found - some videos may be blocked")

setup_cookies()

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
]

def cleanup_file(filepath, delay=300):
    def delete():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except: pass
    threading.Thread(target=delete, daemon=True).start()

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

def get_base_opts():
    opts = {
        'quiet': True,
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
                'player_client': ['ios', 'android', 'web'],
                'player_skip': ['webpage', 'config'],
            }
        },
    }
    # ✅ Use cookies if available
    if os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    return opts

def get_ydl_opts(output_path=None, quality='720p', format_type='video'):
    opts = get_base_opts()
    
    # ✅ CRITICAL: Tell yt-dlp where to find FFmpeg (fixes 1080p/4K/audio)
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
        '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
        '720p':  'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
        '480p':  'bestvideo[height<=480]+bestaudio/best[height<=480]/best',
        '360p':  'bestvideo[height<=360]+bestaudio/best[height<=360]/best',
        'best':  'bestvideo+bestaudio/best',
    }

    opts.update({
        'format': quality_map.get(quality, quality_map['720p']),
        'merge_output_format': 'mp4',
        'concurrent_fragment_downloads': 4,
    })

    if output_path:
        opts['outtmpl'] = output_path + '.%(ext)s'

    return opts

@app.route('/')
def home():
    cookies_status = "loaded" if os.path.exists(COOKIES_FILE) else "missing"
    ffmpeg_status = "available" if FFMPEG_PATH else "missing"
    return jsonify({
        "status": "Downlynk backend is running!",
        "version": "3.0.1",
        "cookies": cookies_status,
        "ffmpeg": ffmpeg_status,
    })

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

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

            qualities = set()
            for f in formats:
                h = f.get('height')
                if h and f.get('vcodec') != 'none':
                    if h >= 2160: qualities.add('4k')
                    elif h >= 1080: qualities.add('1080p')
                    elif h >= 720: qualities.add('720p')
                    elif h >= 480: qualities.add('480p')
                    elif h >= 360: qualities.add('360p')

            order = ['4k', '1080p', '720p', '480p', '360p']
            sorted_qualities = [q for q in order if q in qualities]

            return jsonify({
                "title": info.get('title', 'Unknown'),
                "duration": info.get('duration', 0),
                "thumbnail": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'Unknown'),
                "platform": info.get('extractor_key', 'Unknown'),
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

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_FOLDER, file_id)

    try:
        opts = get_ydl_opts(output_path, quality, format_type)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')

        # Find downloaded file
        downloaded_file = None
        for ext in ['mp4', 'webm', 'mkv', 'm4a', 'mp3']:
            candidate = output_path + '.' + ext
            if os.path.exists(candidate):
                downloaded_file = candidate
                break

        if not downloaded_file:
            for f in os.listdir(DOWNLOAD_FOLDER):
                if f.startswith(file_id):
                    downloaded_file = os.path.join(DOWNLOAD_FOLDER, f)
                    break

        if not downloaded_file:
            return jsonify({"error": "File not found after download"}), 500

        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
        ext = downloaded_file.split('.')[-1]
        file_size = os.path.getsize(downloaded_file)

        mime_map = {
            'mp4': 'video/mp4', 'webm': 'video/webm',
            'mkv': 'video/x-matroska', 'mp3': 'audio/mpeg', 'm4a': 'audio/mp4'
        }
        mimetype = mime_map.get(ext, 'application/octet-stream')
        dl_name = f"{safe_title}.{'mp3' if format_type == 'audio' else ext}"

        def generate():
            with open(downloaded_file, 'rb') as f:
                while True:
                    chunk = f.read(512 * 1024)  # 512KB chunks
                    if not chunk:
                        break
                    yield chunk
            cleanup_file(downloaded_file, delay=120)

        return Response(
            stream_with_context(generate()),
            mimetype=mimetype,
            headers={
                'Content-Type': 'application/octet-stream',
                'Content-Disposition': f'attachment; filename="{dl_name}"',
                'Content-Length': str(file_size),
                'Access-Control-Allow-Origin': '*',
            }
        )

    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        if '403' in err or 'blocked' in err.lower():
            msg = "YouTube blocked this request. Add YouTube cookies to fix this."
        elif 'age' in err.lower() or 'sign in' in err.lower():
            msg = "This video requires age verification. Add YouTube cookies to download it."
        elif 'private' in err.lower() or 'not available' in err.lower():
            msg = "This video is private or unavailable."
        elif 'copyright' in err.lower():
            msg = "This video is blocked due to copyright."
        else:
            msg = f"Download failed: {err[:200]}"
        return jsonify({"error": msg}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
