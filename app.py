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

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Dictionary to store real-time progress for each download
download_progress = {}

# Helper to remove terminal color codes from yt-dlp output
def clean_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

# Custom hook to track download progress
def progress_hook(d, file_id):
    if d['status'] == 'downloading':
        percent = clean_ansi(d.get('_percent_str', '0%')).strip()
        speed = clean_ansi(d.get('_speed_str', '0 B/s')).strip()
        eta = clean_ansi(d.get('_eta_str', 'Unknown')).strip()
        
        download_progress[file_id] = {
            "status": "Downloading...",
            "percent": percent,
            "speed": speed,
            "eta": eta
        }
    elif d['status'] == 'finished':
        download_progress[file_id] = {
            "status": "Processing file (Merging audio/video)...",
            "percent": "100%",
            "speed": "0 B/s",
            "eta": "00:00"
        }

# ========== FFMPEG SETUP ==========
def setup_ffmpeg():
    """Download static FFmpeg binary if system doesn't have it"""
    ffmpeg_dir = "/tmp/ffmpeg"
    ffmpeg_bin = os.path.join(ffmpeg_dir, "ffmpeg")
    ffprobe_bin = os.path.join(ffmpeg_dir, "ffprobe")
    
    if os.path.exists(ffmpeg_bin) and os.path.exists(ffprobe_bin):
        return ffmpeg_bin, ffprobe_bin
    
    print("📥 FFmpeg not found in system, downloading static binary...")
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
        print(f"✅ FFmpeg ready at: {ffmpeg_bin}")
        return ffmpeg_bin, ffprobe_bin
        
    except Exception as e:
        print(f"⚠️ FFmpeg download failed: {e}")
        return None, None

FFMPEG_PATH, FFPROBE_PATH = setup_ffmpeg()

# ========== COOKIES DOWNLOAD (Secret Gist) ==========
def setup_cookies():
    cookies_url = os.environ.get('COOKIES_URL', '')
    if cookies_url:
        try:
            print("📥 Downloading cookies from private URL...")
            urllib.request.urlretrieve(cookies_url, COOKIES_FILE)
            print("✅ YouTube cookies loaded securely from Gist!")
        except Exception as e:
            print(f"⚠️ Failed to download cookies: {e}")
    else:
        print("⚠️ No COOKIES_URL found - some videos may be blocked")

# Run the cookie download on startup
setup_cookies()

# ========== YT-DLP SETUP ==========
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
]

def cleanup_file(filepath, file_id=None, delay=300):
    def delete():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
            # Clean up memory dictionary
            if file_id and file_id in download_progress:
                del download_progress[file_id]
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
                'player_client': ['android', 'ios', 'tv', 'web'],
                'player_skip': ['webpage', 'config'],
            }
        },
    }
    if os.path.exists('cookies.txt'):
        opts['cookiefile'] = 'cookies.txt'
    elif os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    return opts

def get_ydl_opts(output_path=None, quality='720p', format_type='video', file_id=None):
    opts = get_base_opts()
    
    # Attach the progress hook if a file_id is provided
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

# ========== ROUTES ==========
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

# New endpoint to fetch progress
@app.route('/progress/<file_id>', methods=['GET'])
def get_progress(file_id):
    # Default to 0% if the download hasn't registered in the hook yet
    data = download_progress.get(file_id, {
        "status": "Starting download...",
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
            # We don't download, just grab metadata
            info = ydl.extract_info(url, download=False)
            
            # YouTube returns a list of formats
            formats = info.get('formats', [])
            qualities_list = []
            
            for f in formats:
                h = f.get('height')
                # Check for height and ensure there is a video codec
                if h and f.get('vcodec') != 'none':
                    if h >= 2160 and '4k' not in qualities_list: qualities_list.append('4k')
                    elif h >= 1080 and '1080p' not in qualities_list: qualities_list.append('1080p')
                    elif h >= 720 and '720p' not in qualities_list: qualities_list.append('720p')
                    elif h >= 480 and '480p' not in qualities_list: qualities_list.append('480p')
                    elif h >= 360 and '360p' not in qualities_list: qualities_list.append('360p')

            # Sort qualities from high to low
            order = ['4k', '1080p', '720p', '480p', '360p']
            sorted_qualities = [q for q in order if q in qualities_list]

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
    
    # Grab the file_id generated by the frontend, or generate one if missing
    file_id = (data or {}).get('file_id', str(uuid.uuid4()))

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    output_path = os.path.join(DOWNLOAD_FOLDER, file_id)
    
    # Initialize progress tracker
    download_progress[file_id] = {
        "status": "Starting download...",
        "percent": "0%",
        "speed": "0 B/s",
        "eta": "Unknown"
    }

    try:
        # Pass file_id into options
        opts = get_ydl_opts(output_path, quality, format_type, file_id)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')

        downloaded_file = None
        for ext in ['mp4', 'webm', 'mkv', 'm4a', 'mp3']:
            candidate = output_path + '.' + ext
            if os.path.exists(candidate):
                downloaded_file = candidate
                break

        if not downloaded_file:
            # Fallback search if yt-dlp added a weird extension
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
                    chunk = f.read(512 * 1024)
                    if not chunk:
                        break
                    yield chunk
            # Once stream is finished, trigger the file and memory cleanup
            cleanup_file(downloaded_file, file_id=file_id, delay=120)

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
            msg = "YouTube blocked this request. Update YouTube cookies to fix this."
        elif 'age' in err.lower() or 'sign in' in err.lower():
            msg = "This video requires age verification. Update YouTube cookies to download it."
        elif 'private' in err.lower() or 'not available' in err.lower():
            msg = "This video is private or unavailable."
        elif 'copyright' in err.lower():
            msg = "This video is blocked due to copyright."
        else:
            msg = f"Download failed: {err[:200]}"
        
        # Clear failed progress from memory
        if file_id in download_progress:
            del download_progress[file_id]
            
        return jsonify({"error": msg}), 400
        
    except Exception as e:
        if file_id in download_progress:
            del download_progress[file_id]
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
