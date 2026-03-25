from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

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

def get_ydl_opts(output_path=None, quality='best'):
    # ✅ Quality selector
    if quality == 'best':
        fmt = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
    elif quality == 'medium':
        fmt = 'bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720]'
    elif quality == 'low':
        fmt = 'bestvideo[height<=480][ext=mp4]+bestaudio/best[height<=480]'
    else:
        fmt = 'best'

    opts = {
        'format': fmt,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 60,
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 5,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        },
        # ✅ Fixes YouTube + TikTok + Instagram
        'extractor_args': {
            'youtube': {'player_client': ['android', 'web']},
        },
        'merge_output_format': 'mp4',
        # ✅ Speed boost
        'concurrent_fragment_downloads': 4,
    }
    if output_path:
        opts['outtmpl'] = output_path + '.%(ext)s'
    return opts

@app.route('/')
def home():
    return jsonify({"status": "Downlynk backend is running!", "version": "2.0.0"})

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
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                "title": info.get('title', 'Unknown'),
                "duration": info.get('duration', 0),
                "thumbnail": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'Unknown'),
                "platform": info.get('extractor_key', 'Unknown'),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/download', methods=['POST', 'OPTIONS'])
def download_video():
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    data = request.get_json()
    url = (data or {}).get('url', '').strip()
    quality = (data or {}).get('quality', 'best')  # ✅ quality from frontend

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_FOLDER, file_id)

    try:
        with yt_dlp.YoutubeDL(get_ydl_opts(output_path, quality)) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')

        # Find downloaded file
        downloaded_file = None
        for ext in ['mp4', 'webm', 'mkv', 'avi', 'mov', 'm4a']:
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

        # ✅ Chunked streaming — fixes connection reset
        def generate():
            with open(downloaded_file, 'rb') as f:
                while True:
                    chunk = f.read(1024 * 512)  # 512KB chunks
                    if not chunk:
                        break
                    yield chunk
            cleanup_file(downloaded_file, delay=60)

        return Response(
            stream_with_context(generate()),
            mimetype='application/octet-stream',
            headers={
                'Content-Disposition': f'attachment; filename="{safe_title}.{ext}"',
                'Content-Length': str(file_size),
                'Access-Control-Allow-Origin': '*',
            }
        )

    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        # ✅ Friendly error messages
        if 'not available' in err:
            return jsonify({"error": "This video is not available or is private."}), 400
        elif 'copyright' in err.lower():
            return jsonify({"error": "This video cannot be downloaded due to copyright."}), 400
        elif 'age' in err.lower():
            return jsonify({"error": "This video is age-restricted."}), 400
        else:
            return jsonify({"error": f"Download failed: {err}"}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
