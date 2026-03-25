from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)


def cleanup_file(filepath, delay=300):
    def delete():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except:
            pass
    threading.Thread(target=delete, daemon=True).start()


@app.route('/')
def home():
    return jsonify({"status": "Downlynk backend is running!", "version": "1.0.1"})


@app.route('/health')
def health():
    return jsonify({"status": "ok"})


# 🔥 COMMON YTDLP SETTINGS (reused)
def get_ydl_opts(output_path=None):
    return {
        'format': 'bv*+ba/b',  # fallback-safe
        'outtmpl': output_path + '.%(ext)s' if output_path else None,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'retries': 10,
        'fragment_retries': 10,
        'sleep_interval': 2,

        # 🧠 Pretend you're human
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        },

        # 🔥 Modern YouTube fix
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web']
            }
        },

        # 🔐 OPTIONAL (add cookies.txt if you have it)
        # 'cookiefile': 'cookies.txt',

        'merge_output_format': 'mp4',
    }


@app.route('/info', methods=['POST'])
def get_info():
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


@app.route('/download', methods=['POST'])
def download_video():
    data = request.get_json()
    url = (data or {}).get('url', '').strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_FOLDER, file_id)

    try:
        ydl_opts = get_ydl_opts(output_path)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')

        # find file
        downloaded_file = None
        for ext in ['mp4', 'webm', 'mkv', 'avi', 'mov']:
            candidate = output_path + '.' + ext
            if os.path.exists(candidate):
                downloaded_file = candidate
                break

        if not downloaded_file:
            return jsonify({"error": "Download failed — file not found"}), 500

        cleanup_file(downloaded_file)

        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
        ext = downloaded_file.split('.')[-1]

        return send_file(
            downloaded_file,
            as_attachment=True,
            download_name=f"{safe_title}.{ext}",
            mimetype='application/octet-stream'
        )

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Download failed: {str(e)}"}), 400

    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
