from flask import Flask, request, jsonify, Response, stream_with_context, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time
import random

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = "downloads"
COOKIES_FILE = "cookies.txt"  # YouTube cookies file
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

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
                mp3_path = filepath.replace('.m4a', '.mp3').replace('.webm', '.mp3')
                if os.path.exists(mp3_path) and mp3_path != filepath:
                    os.remove(mp3_path)
        except: pass
    threading.Thread(target=delete, daemon=True).start()

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

def get_ydl_opts(output_path=None, quality='best', format_type='video'):
    user_agent = random.choice(USER_AGENTS)
    
    # ✅ CRITICAL FIX: Use iOS client + PoToken bypass
    common_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 60,
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 5,
        'http_headers': {
            'User-Agent': user_agent,
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
        },
        # ✅ KEY FIX: Use iOS client which bypasses most restrictions
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android'],
                'player_skip': ['webpage', 'config', 'js'],
                'skip': ['dash', 'hls'],
            },
        },
        'concurrent_fragment_downloads': 4,
    }
    
    # Add cookies if file exists
    if os.path.exists(COOKIES_FILE):
        common_opts['cookiefile'] = COOKIES_FILE
    
    if format_type == 'audio':
        opts = {
            **common_opts,
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        if output_path:
            opts['outtmpl'] = output_path + '.%(ext)s'
        return opts
    
    quality_map = {
        '4k': 'bestvideo[height<=2160]+bestaudio/best[height<=2160]',
        '1440p': 'bestvideo[height<=1440]+bestaudio/best[height<=1440]',
        '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        '720p': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
        '480p': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
        '360p': 'bestvideo[height<=360]+bestaudio/best[height<=360]',
        '240p': 'bestvideo[height<=240]+bestaudio/best[height<=240]',
        'best': 'bestvideo+bestaudio/best',
        'medium': 'best[height<=720]',
        'low': 'best[height<=480]',
    }
    
    fmt = quality_map.get(quality, quality_map['best'])
    
    opts = {
        **common_opts,
        'format': fmt,
        'merge_output_format': 'mp4',
    }
    if output_path:
        opts['outtmpl'] = output_path + '.%(ext)s'
    
    return opts

@app.route('/')
def home():
    return jsonify({
        "status": "Downlynk backend is running!", 
        "version": "2.3.0",
        "features": ["240p-4K video", "Audio extraction", "YouTube iOS bypass", "Cookie support"]
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
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'http_headers': {'User-Agent': random.choice(USER_AGENTS)},
            'extractor_args': {
                'youtube': {'player_client': ['ios']},
            },
        }
        
        if os.path.exists(COOKIES_FILE):
            ydl_opts['cookiefile'] = COOKIES_FILE
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            formats = info.get('formats', [])
            available_qualities = set()
            
            for f in formats:
                if f.get('vcodec') != 'none' and f.get('height'):
                    h = f.get('height')
                    if h >= 2160: available_qualities.add('4k')
                    elif h >= 1440: available_qualities.add('1440p')
                    elif h >= 1080: available_qualities.add('1080p')
                    elif h >= 720: available_qualities.add('720p')
                    elif h >= 480: available_qualities.add('480p')
                    elif h >= 360: available_qualities.add('360p')
                    elif h >= 240: available_qualities.add('240p')
            
            has_audio = any(f.get('acodec') != 'none' for f in formats)
            
            return jsonify({
                "title": info.get('title', 'Unknown'),
                "duration": info.get('duration', 0),
                "thumbnail": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'Unknown'),
                "platform": info.get('extractor_key', 'Unknown'),
                "available_qualities": sorted(list(available_qualities), 
                    key=lambda x: int(x.replace('k', '000').replace('p', '')), reverse=True),
                "has_audio": has_audio,
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/download', methods=['POST', 'OPTIONS'])
def download_video():
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    data = request.get_json()
    url = (data or {}).get('url', '').strip()
    quality = (data or {}).get('quality', 'best')
    format_type = (data or {}).get('format', 'video')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    valid_qualities = ['4k', '1440p', '1080p', '720p', '480p', '360p', '240p', 'best', 'medium', 'low', 'audio']
    if quality not in valid_qualities:
        quality = 'best'
    
    if quality == 'audio':
        format_type = 'audio'

    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_FOLDER, file_id)

    try:
        max_retries = 3
        last_error = None
        
        for attempt in range(max_retries):
            try:
                opts = get_ydl_opts(output_path, quality, format_type)
                
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get('title', 'video')
                    break
                    
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                else:
                    raise last_error

        # Find file
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
        
        mime_types = {
            'mp4': 'video/mp4', 'webm': 'video/webm', 'mkv': 'video/x-matroska',
            'm4a': 'audio/mp4', 'mp3': 'audio/mpeg',
        }
        mimetype = mime_types.get(ext, 'application/octet-stream')
        
        download_filename = f"{safe_title}.{ext}" if format_type != 'audio' else f"{safe_title}.mp3"

        def generate():
            with open(downloaded_file, 'rb') as f:
                while True:
                    chunk = f.read(1024 * 512)
                    if not chunk:
                        break
                    yield chunk
            cleanup_file(downloaded_file, delay=60 if format_type == 'audio' else 300)

        return Response(
            stream_with_context(generate()),
            mimetype=mimetype,
            headers={
                'Content-Disposition': f'attachment; filename="{download_filename}"',
                'Content-Length': str(file_size),
                'Access-Control-Allow-Origin': '*',
            }
        )

    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        if '403' in err:
            return jsonify({"error": "YouTube blocked this download. Try: 1) Audio only mode, 2) 720p quality, 3) Different video"}), 400
        elif 'sign in' in err.lower() or 'login' in err.lower():
            return jsonify({"error": "This video requires YouTube login. Try a different video."}), 400
        elif 'not available' in err:
            return jsonify({"error": "Video not available or is private."}), 400
        elif 'copyright' in err.lower():
            return jsonify({"error": "Copyright blocked."}), 400
        elif 'age' in err.lower():
            return jsonify({"error": "Age-restricted video."}), 400
        else:
            return jsonify({"error": f"Download failed: {err}"}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/app')
def serve_frontend():
    return send_from_directory('.', 'index.html')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
