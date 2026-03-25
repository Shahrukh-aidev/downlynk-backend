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
                # Also remove .mp3 if it exists (for audio conversions)
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
    """
    Generate yt-dlp options based on quality and format type.
    
    Quality options: 4k, 1440p, 1080p, 720p, 480p, 360p, 240p, best, medium, low
    Format options: video, audio
    """
    
    # ✅ AUDIO-ONLY FORMAT
    if format_type == 'audio':
        opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': 60,
            'retries': 10,
            'fragment_retries': 10,
            'file_access_retries': 5,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            },
            'extractor_args': {
                'youtube': {'player_client': ['android', 'web']},
            },
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': output_path + '.%(ext)s' if output_path else '%(title)s.%(ext)s',
        }
        return opts
    
    # ✅ VIDEO QUALITY MAP (240p to 4K)
    quality_map = {
        '4k': 'bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160][ext=webm]+bestaudio/best[height<=2160]',
        '1440p': 'bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440][ext=webm]+bestaudio/best[height<=1440]',
        '1080p': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080][ext=webm]+bestaudio/best[height<=1080]',
        '720p': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720][ext=webm]+bestaudio/best[height<=720]',
        '480p': 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480][ext=webm]+bestaudio/best[height<=480]',
        '360p': 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360][ext=webm]+bestaudio/best[height<=360]',
        '240p': 'bestvideo[height<=240][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=240][ext=webm]+bestaudio/best[height<=240]',
        # Legacy support
        'best': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
        'medium': 'bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720]',
        'low': 'bestvideo[height<=480][ext=mp4]+bestaudio/best[height<=480]',
    }
    
    # Get format string, fallback to best if unknown quality
    fmt = quality_map.get(quality, quality_map['best'])
    
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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        },
        'extractor_args': {
            'youtube': {'player_client': ['android', 'web']},
        },
        'merge_output_format': 'mp4',
        'concurrent_fragment_downloads': 4,
        'outtmpl': output_path + '.%(ext)s' if output_path else '%(title)s.%(ext)s',
    }
    return opts

@app.route('/')
def home():
    return jsonify({
        "status": "Downlynk backend is running!", 
        "version": "2.1.0",
        "features": ["240p-4K video", "Audio extraction", "1000+ sites"]
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
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # ✅ Get available formats for quality display
            formats = info.get('formats', [])
            available_qualities = set()
            
            for f in formats:
                if f.get('vcodec') != 'none' and f.get('height'):
                    h = f.get('height')
                    if h >= 2160:
                        available_qualities.add('4k')
                    elif h >= 1440:
                        available_qualities.add('1440p')
                    elif h >= 1080:
                        available_qualities.add('1080p')
                    elif h >= 720:
                        available_qualities.add('720p')
                    elif h >= 480:
                        available_qualities.add('480p')
                    elif h >= 360:
                        available_qualities.add('360p')
                    elif h >= 240:
                        available_qualities.add('240p')
            
            # Check for audio
            has_audio = any(f.get('acodec') != 'none' for f in formats)
            
            return jsonify({
                "title": info.get('title', 'Unknown'),
                "duration": info.get('duration', 0),
                "thumbnail": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'Unknown'),
                "platform": info.get('extractor_key', 'Unknown'),
                "available_qualities": sorted(list(available_qualities), key=lambda x: int(x.replace('k', '000').replace('p', '')) if x.replace('k', '').replace('p', '').isdigit() else 0, reverse=True),
                "has_audio": has_audio,
                "original_url": url,
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
    format_type = (data or {}).get('format', 'video')  # 'video' or 'audio'

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Validate quality parameter
    valid_qualities = ['4k', '1440p', '1080p', '720p', '480p', '360p', '240p', 'best', 'medium', 'low', 'audio']
    if quality not in valid_qualities:
        quality = 'best'
    
    # If quality is 'audio', force format_type to audio
    if quality == 'audio':
        format_type = 'audio'

    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_FOLDER, file_id)

    try:
        with yt_dlp.YoutubeDL(get_ydl_opts(output_path, quality, format_type)) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')

        # Find downloaded file (handle both video and audio conversions)
        downloaded_file = None
        expected_extensions = ['mp4', 'webm', 'mkv', 'avi', 'mov', 'm4a', 'mp3']
        
        for ext in expected_extensions:
            candidate = output_path + '.' + ext
            if os.path.exists(candidate):
                downloaded_file = candidate
                break

        # Fallback: search for any file starting with file_id
        if not downloaded_file:
            for f in os.listdir(DOWNLOAD_FOLDER):
                if f.startswith(file_id):
                    downloaded_file = os.path.join(DOWNLOAD_FOLDER, f)
                    break

        if not downloaded_file:
            return jsonify({"error": "File not found after download"}), 500

        # Prepare filename
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
        ext = downloaded_file.split('.')[-1]
        file_size = os.path.getsize(downloaded_file)
        
        # Determine mime type
        mime_types = {
            'mp4': 'video/mp4',
            'webm': 'video/webm',
            'mkv': 'video/x-matroska',
            'avi': 'video/x-msvideo',
            'mov': 'video/quicktime',
            'm4a': 'audio/mp4',
            'mp3': 'audio/mpeg',
        }
        mimetype = mime_types.get(ext, 'application/octet-stream')

        # ✅ Chunked streaming
        def generate():
            with open(downloaded_file, 'rb') as f:
                while True:
                    chunk = f.read(1024 * 512)  # 512KB chunks
                    if not chunk:
                        break
                    yield chunk
            # Cleanup after streaming (shorter delay for audio)
            cleanup_file(downloaded_file, delay=60 if format_type == 'audio' else 300)

        # Set filename for download
        if format_type == 'audio':
            download_filename = f"{safe_title}.mp3"
        else:
            download_filename = f"{safe_title}.{ext}"

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
        # Friendly error messages
        if 'not available' in err or 'Private video' in err:
            return jsonify({"error": "This video is not available or is private."}), 400
        elif 'copyright' in err.lower() or 'removed' in err.lower():
            return jsonify({"error": "This video cannot be downloaded due to copyright restrictions."}), 400
        elif 'age' in err.lower():
            return jsonify({"error": "This video is age-restricted."}), 400
        elif 'sign in' in err.lower() or 'login' in err.lower():
            return jsonify({"error": "This video requires login. Try a different URL."}), 400
        elif 'Unsupported URL' in err:
            return jsonify({"error": "This URL is not supported."}), 400
        else:
            return jsonify({"error": f"Download failed: {err}"}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
