# app.py
import os
import uuid
import threading
import shutil
import subprocess
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import Flask, request, jsonify, send_file, render_template, redirect, url_for, session
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
CORS(app)

# ---------- Configuration ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env_file():
    env_path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path, 'r', encoding='utf-8') as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()
app.secret_key = os.environ.get('SECRET_KEY', 'clipforge-dev-secret-change-me')


def get_env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value and str(value).strip():
            return value
    return default


YOUTUBE_CLIENT_ID = get_env('YOUTUBE_CLIENT_ID', 'YOUTUBE_ClENT_ID', default=None)
YOUTUBE_CLIENT_SECRET = get_env('YOUTUBE_CLIENT_SECRET', 'YOUTUBE_ClENT_SECRET', default=None)
YOUTUBE_REDIRECT_URI = get_env('YOUTUBE_REDIRECT_URI', default='https://clipforge-xg13.onrender.com/oauth/youtube/callback')
YOUTUBE_SCOPES = 'https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly'
OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
TEMP_DIR = os.path.join(BASE_DIR, 'temp')
UPLOAD_DIR = os.path.join(TEMP_DIR, 'uploads')
MUSIC_LIBRARY_DIR = os.path.join(BASE_DIR, 'music_library')
DB_PATH = os.path.join(BASE_DIR, 'app.db')

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(MUSIC_LIBRARY_DIR, exist_ok=True)

jobs = {}

# ---------- Database ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            video_path TEXT,
            music_path TEXT,
            output_path TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            segments_json TEXT DEFAULT '[]',
            title TEXT DEFAULT '',
            description TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            thumbnail_path TEXT,
            publish_status TEXT DEFAULT 'not_published',
            published_platforms TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS social_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            account_name TEXT NOT NULL,
            profile_url TEXT,
            access_token TEXT,
            refresh_token TEXT,
            status TEXT NOT NULL DEFAULT 'connected',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            job_id TEXT,
            platform TEXT NOT NULL,
            message TEXT,
            scheduled_for TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS social_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            job_id TEXT,
            platform TEXT NOT NULL,
            message TEXT,
            media_url TEXT,
            scheduled_for TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    conn.commit()
    conn.close()


def ensure_db_schema():
    conn = get_db()
    job_columns = [row['name'] for row in conn.execute('PRAGMA table_info(jobs)').fetchall()]
    for column_name, column_sql in {
        'title': 'ALTER TABLE jobs ADD COLUMN title TEXT DEFAULT \"\"',
        'description': 'ALTER TABLE jobs ADD COLUMN description TEXT DEFAULT \"\"',
        'tags': 'ALTER TABLE jobs ADD COLUMN tags TEXT DEFAULT \"\"',
        'thumbnail_path': 'ALTER TABLE jobs ADD COLUMN thumbnail_path TEXT',
        'publish_status': 'ALTER TABLE jobs ADD COLUMN publish_status TEXT DEFAULT \"not_published\"',
        'published_platforms': 'ALTER TABLE jobs ADD COLUMN published_platforms TEXT DEFAULT \"\"',
    }.items():
        if column_name not in job_columns:
            try:
                conn.execute(column_sql)
            except Exception:
                pass

    social_columns = [row['name'] for row in conn.execute('PRAGMA table_info(social_accounts)').fetchall()]
    for column_name, column_sql in {
        'refresh_token': 'ALTER TABLE social_accounts ADD COLUMN refresh_token TEXT',
    }.items():
        if column_name not in social_columns:
            try:
                conn.execute(column_sql)
            except Exception:
                pass
    conn.commit(); conn.close()


init_db()
ensure_db_schema()


def get_valid_youtube_token(user_id):
    conn = get_db()
    account = conn.execute(
        'SELECT * FROM social_accounts WHERE user_id = ? AND platform = ? ORDER BY id DESC LIMIT 1',
        (user_id, 'youtube')
    ).fetchone()
    conn.close()

    if not account:
        return None, None

    access_token = account['access_token']
    refresh_token = account['refresh_token']
    if not access_token:
        return None, refresh_token

    token_check = requests.get(
        'https://www.googleapis.com/oauth2/v1/tokeninfo',
        params={'access_token': access_token},
        timeout=30
    )
    if token_check.status_code == 200:
        return access_token, refresh_token

    if not refresh_token or not YOUTUBE_CLIENT_ID or not YOUTUBE_CLIENT_SECRET:
        return None, refresh_token

    refresh_response = requests.post(
        'https://oauth2.googleapis.com/token',
        data={
            'client_id': YOUTUBE_CLIENT_ID,
            'client_secret': YOUTUBE_CLIENT_SECRET,
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token'
        },
        timeout=30
    )
    if refresh_response.status_code >= 400:
        return None, refresh_token

    refresh_data = refresh_response.json()
    new_access_token = refresh_data.get('access_token')
    if not new_access_token:
        return None, refresh_token

    conn = get_db()
    conn.execute(
        'UPDATE social_accounts SET access_token = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND platform = ?',
        (new_access_token, user_id, 'youtube')
    )
    conn.commit(); conn.close()
    return new_access_token, refresh_token


def _get_connected_social_account(user_id, platform):
    conn = get_db()
    account = conn.execute(
        'SELECT * FROM social_accounts WHERE user_id = ? AND platform = ? ORDER BY id DESC LIMIT 1',
        (user_id, platform)
    ).fetchone()
    conn.close()
    if not account:
        return None
    return dict(account)


def _resolve_media_source(user_id, job_id, media_url=None):
    if media_url and str(media_url).strip():
        return str(media_url).strip()

    if not job_id:
        return None

    conn = get_db()
    row = conn.execute(
        'SELECT output_path FROM jobs WHERE id = ? AND user_id = ?',
        (job_id, user_id)
    ).fetchone()
    conn.close()
    if row and row['output_path']:
        return row['output_path']

    job = jobs.get(job_id)
    if job and job.get('output_path'):
        return job.get('output_path')
    return None


def _parse_schedule_epoch(scheduled_for):
    if not scheduled_for:
        return None
    try:
        dt = datetime.fromisoformat(str(scheduled_for).replace('Z', '+00:00'))
    except ValueError:
        try:
            dt = datetime.strptime(str(scheduled_for), '%Y-%m-%dT%H:%M')
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _media_extension_is_video(path):
    if not path:
        return False
    ext = os.path.splitext(str(path).lower())[1]
    return ext in {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'}


def _publish_to_x(user_id, job_id, message, media_url=None, scheduled_for=None, action='publish'):
    account = _get_connected_social_account(user_id, 'x')
    if not account or not account.get('access_token'):
        return {'error': 'X account not connected or access token missing.'}, 400

    if action == 'schedule':
        schedule_epoch = _parse_schedule_epoch(scheduled_for)
        if not schedule_epoch:
            return {'error': 'A valid schedule time is required when scheduling an X post.'}, 400
        return {'message': 'X post scheduled successfully.', 'status': 'scheduled', 'scheduled_for': scheduled_for}, 200

    headers = {
        'Authorization': f'Bearer {account["access_token"]}',
        'Content-Type': 'application/json',
    }

    media_source = _resolve_media_source(user_id, job_id, media_url)
    media_payload = None
    if media_source and os.path.exists(media_source):
        try:
            with open(media_source, 'rb') as media_file:
                media_bytes = media_file.read()
            upload_urls = [
                'https://api.x.com/2/media/upload',
                'https://api.twitter.com/2/media/upload'
            ]
            upload_response = None
            for upload_url in upload_urls:
                try:
                    upload_response = requests.post(
                        upload_url,
                        headers={'Authorization': f'Bearer {account["access_token"]}', 'Content-Type': 'application/octet-stream'},
                        data=media_bytes,
                        timeout=180,
                    )
                    if upload_response.status_code < 400:
                        break
                except Exception:
                    continue

            if upload_response is not None and upload_response.status_code < 400:
                upload_data = upload_response.json() if upload_response.content else {}
                media_payload = {'media': {'media_ids': [upload_data.get('media_id_string') or upload_data.get('media_id')]}}
        except Exception:
            media_payload = None

    payload = {'text': message}
    if media_payload:
        payload.update(media_payload)

    endpoint_candidates = [
        'https://api.x.com/2/tweets',
        'https://api.twitter.com/2/tweets'
    ]

    response = None
    for endpoint in endpoint_candidates:
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=120)
            if response.status_code < 400:
                break
        except Exception:
            continue

    if response is None:
        return {'error': 'X request failed while posting.'}, 500

    try:
        json_response = response.json() if response.content else {}
    except ValueError:
        json_response = {}

    if response.status_code >= 400:
        error_obj = json_response.get('errors', [{}])[0] if isinstance(json_response, dict) else {}
        detail = error_obj.get('message') if isinstance(error_obj, dict) else None
        return {'error': detail or 'X post failed.', 'status_code': response.status_code, 'response': json_response}, response.status_code

    return {'message': 'Posted to X.', 'status': 'published', 'result': json_response}, 200


def _publish_to_facebook(user_id, job_id, message, media_url=None, scheduled_for=None, action='publish'):
    account = _get_connected_social_account(user_id, 'facebook')
    if not account or not account.get('access_token'):
        return {'error': 'Facebook account not connected or access token missing.'}, 400

    page_id = (account.get('profile_url') or '').strip() or (account.get('account_name') or '').strip()
    if not page_id:
        return {'error': 'Facebook Page ID is required. Add the Page ID when connecting the account.'}, 400

    media_source = _resolve_media_source(user_id, job_id, media_url)
    if not media_source:
        return {'error': 'No valid Facebook media source was found for this job.'}, 400

    access_token = account['access_token']
    is_video = _media_extension_is_video(media_source)
    endpoint = f'https://graph.facebook.com/v20.0/{page_id}/videos' if is_video else f'https://graph.facebook.com/v20.0/{page_id}/photos'

    payload = {'access_token': access_token}
    if is_video:
        payload['description'] = message
        payload['title'] = 'ClipForge Highlight'
    else:
        payload['caption'] = message

    if action == 'schedule':
        schedule_epoch = _parse_schedule_epoch(scheduled_for)
        if not schedule_epoch:
            return {'error': 'A valid schedule time is required when scheduling a Facebook post.'}, 400
        payload['published'] = 'false'
        payload['scheduled_publish_time'] = schedule_epoch

    try:
        if str(media_source).startswith(('http://', 'https://')):
            payload['url'] = media_source
            response = requests.post(endpoint, data=payload, timeout=180)
        else:
            with open(media_source, 'rb') as media_file:
                response = requests.post(
                    endpoint,
                    data=payload,
                    files={'source': (os.path.basename(media_source), media_file, 'video/mp4' if is_video else 'image/jpeg')},
                    timeout=180
                )
    except Exception as exc:
        return {'error': f'Facebook upload exception: {str(exc)}'}, 500

    try:
        json_response = response.json() if response.content else {}
    except ValueError:
        json_response = {}

    if response.status_code >= 400:
        facebook_error = json_response.get('error', {}) if isinstance(json_response, dict) else {}
        message_text = facebook_error.get('message') if isinstance(facebook_error, dict) else None
        if not message_text:
            message_text = response.text[:500] or 'Facebook post failed'
        return {'error': message_text, 'status_code': response.status_code}, response.status_code

    if action == 'schedule':
        return {'message': 'Facebook post scheduled successfully.', 'status': 'scheduled', 'result': json_response}, 200
    return {'message': 'Published to Facebook.', 'status': 'published', 'result': json_response}, 200


def _publish_to_instagram(user_id, job_id, message, media_url=None, scheduled_for=None, action='publish'):
    account = _get_connected_social_account(user_id, 'instagram')
    if not account or not account.get('access_token'):
        return {'error': 'Instagram account not connected or access token missing.'}, 400

    access_token = account['access_token']
    try:
        me_response = requests.get(
            'https://graph.facebook.com/v20.0/me',
            params={
                'fields': 'instagram_business_account{id,username}',
                'access_token': access_token,
            },
            timeout=60,
        )
        me_data = me_response.json() if me_response.content else {}
    except Exception as exc:
        return {'error': f'Instagram account lookup failed: {str(exc)}'}, 500

    if me_response.status_code >= 400:
        detail = me_data.get('error', {}).get('message') if isinstance(me_data, dict) else None
        return {'error': detail or 'Instagram account lookup failed'}, me_response.status_code

    instagram_account = me_data.get('instagram_business_account') if isinstance(me_data, dict) else None
    if not instagram_account:
        return {'error': 'Your Facebook account is not linked to an Instagram Business account.'}, 400

    ig_user_id = str(instagram_account.get('id') or account.get('profile_url') or '').strip()
    if not ig_user_id:
        return {'error': 'Instagram Business account ID is required. Reconnect the account using a valid token.'}, 400

    media_source = _resolve_media_source(user_id, job_id, media_url)
    if not media_source or not str(media_source).startswith(('http://', 'https://')):
        return {'error': 'Instagram requires a public media URL. Please provide media_url or host the file publicly before posting.'}, 400

    is_video = _media_extension_is_video(media_source)
    media_key = 'video_url' if is_video else 'image_url'
    creation_response = requests.post(
        f'https://graph.facebook.com/v20.0/{ig_user_id}/media',
        data={
            'access_token': access_token,
            media_key: media_source,
            'caption': message,
        },
        timeout=180,
    )

    try:
        creation_data = creation_response.json() if creation_response.content else {}
    except ValueError:
        creation_data = {}

    if creation_response.status_code >= 400:
        detail = creation_data.get('error', {}).get('message') if isinstance(creation_data, dict) else None
        return {'error': detail or 'Instagram media creation failed'}, creation_response.status_code

    creation_id = creation_data.get('id')
    if not creation_id:
        return {'error': 'Instagram media container was not created.'}, 400

    if action == 'schedule':
        schedule_epoch = _parse_schedule_epoch(scheduled_for)
        if not schedule_epoch:
            return {'error': 'A valid schedule time is required when scheduling an Instagram post.'}, 400
        publish_response = requests.post(
            f'https://graph.facebook.com/v20.0/{ig_user_id}/media_publish',
            data={'creation_id': creation_id, 'access_token': access_token, 'scheduled_publish_time': schedule_epoch},
            timeout=180,
        )
    else:
        publish_response = requests.post(
            f'https://graph.facebook.com/v20.0/{ig_user_id}/media_publish',
            data={'creation_id': creation_id, 'access_token': access_token},
            timeout=180,
        )

    try:
        publish_data = publish_response.json() if publish_response.content else {}
    except ValueError:
        publish_data = {}

    if publish_response.status_code >= 400:
        detail = publish_data.get('error', {}).get('message') if isinstance(publish_data, dict) else None
        return {'error': detail or 'Instagram publish failed'}, publish_response.status_code

    if action == 'schedule':
        return {'message': 'Instagram post scheduled successfully.', 'status': 'scheduled', 'result': publish_data}, 200
    return {'message': 'Published to Instagram.', 'status': 'published', 'result': publish_data}, 200


ensure_db_schema()


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login_page'))
        return view_func(*args, **kwargs)
    return wrapped


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login_page'))
        user = get_user_by_id(session['user_id'])
        if not user or user['role'] != 'admin':
            return redirect(url_for('dashboard'))
        return view_func(*args, **kwargs)
    return wrapped


def get_user_by_email(email):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE email = ?', (email.lower(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def ensure_default_admin():
    if not get_user_by_email('admin@clipforge.com'):
        conn = get_db()
        conn.execute(
            'INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)',
            ('admin', 'admin@clipforge.com', generate_password_hash('admin123'), 'admin')
        )
        conn.commit(); conn.close()
        print('Default admin created: admin@clipforge.com / admin123')


ensure_default_admin()


def save_job_record(job_id, user_id, video_path, music_path, segments_json='[]', status='pending', output_path=None):
    conn = get_db()
    conn.execute('''
        INSERT INTO jobs (id, user_id, video_path, music_path, output_path, status, segments_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            user_id = excluded.user_id,
            video_path = excluded.video_path,
            music_path = excluded.music_path,
            output_path = excluded.output_path,
            status = excluded.status,
            segments_json = excluded.segments_json,
            updated_at = CURRENT_TIMESTAMP
    ''', (job_id, user_id, video_path, music_path, output_path, status, segments_json))
    conn.commit()
    conn.close()


def update_job_status(job_id, status, output_path=None, error=None):
    conn = get_db()
    conn.execute('''
        UPDATE jobs SET status = ?, output_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
    ''', (status, output_path, job_id))
    conn.commit()
    conn.close()


def cleanup_old_uploads():
    """Delete uploaded source files older than 24 hours from the temp upload folder."""
    if not os.path.exists(UPLOAD_DIR):
        return

    now = time.time()
    expired = []
    for file_name in os.listdir(UPLOAD_DIR):
        file_path = os.path.join(UPLOAD_DIR, file_name)
        if os.path.isfile(file_path):
            age_seconds = now - os.path.getmtime(file_path)
            if age_seconds > 86400:
                expired.append(file_path)

    for file_path in expired:
        try:
            os.remove(file_path)
            print(f"Deleted expired temp upload: {file_path}")
        except Exception as exc:
            print(f"Failed to delete expired upload {file_path}: {exc}")


cleanup_old_uploads()

# ---------- Music Library ----------
MUSIC_LIBRARY = [
    {"id": "none", "name": "No Music", "file": None},
]


def scan_music_library():
    if os.path.exists(MUSIC_LIBRARY_DIR):
        for file in os.listdir(MUSIC_LIBRARY_DIR):
            if file.endswith('.mp3'):
                exists = False
                for m in MUSIC_LIBRARY:
                    if m.get('file') == file:
                        exists = True
                        break
                if not exists:
                    name = file.replace('.mp3', '').replace('_', ' ').title()
                    MUSIC_LIBRARY.append({
                        "id": file.replace('.mp3', ''),
                        "name": f"🎵 {name}",
                        "file": file
                    })


scan_music_library()


def get_music_path(music_id):
    if not music_id or music_id == 'none':
        return None

    music_info = None
    for m in MUSIC_LIBRARY:
        if m.get('id') == music_id:
            music_info = m
            break

    if not music_info or not music_info.get('file'):
        return None

    music_file = music_info['file']
    music_path = os.path.join(MUSIC_LIBRARY_DIR, music_file)

    if not os.path.exists(music_path):
        return None

    return music_path


# ---------- FFmpeg helpers ----------
def run_ffmpeg(cmd, description="Running FFmpeg"):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error_msg = result.stderr[:500] if result.stderr else "Unknown error"
        print(f"FFmpeg failed: {error_msg}")
        raise Exception(f"FFmpeg error: {error_msg}")
    return result.stdout + result.stderr


def get_video_info(video_path):
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries',
               'format=duration:stream=width,height,r_frame_rate',
               '-of', 'json', video_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            duration = float(data.get('format', {}).get('duration', 300))

            width, height, fps = 1920, 1080, 30
            for stream in data.get('streams', []):
                if stream.get('codec_type') == 'video':
                    width = int(stream.get('width', 1920))
                    height = int(stream.get('height', 1080))
                    fps_str = stream.get('r_frame_rate', '30/1')
                    if '/' in fps_str:
                        num, den = fps_str.split('/')
                        fps = float(num) / float(den) if float(den) > 0 else 30
                    else:
                        fps = float(fps_str)
            return duration, width, height, fps
    except Exception as e:
        print(f"Could not get video info: {e}")
    return 300, 1920, 1080, 30


def trim_segment_mobile_compatible(input_path, start, end, output_path):
    """Trim a segment with mobile-friendly encoding settings."""
    duration = end - start
    cmd = [
        'ffmpeg',
        '-ss', str(start),
        '-i', input_path,
        '-t', str(duration),
        '-c:v', 'libx264',
        '-preset', 'medium',
        '-crf', '22',
        '-profile:v', 'baseline',
        '-level', '3.0',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-ar', '44100',
        '-movflags', '+faststart',
        '-y',
        output_path
    ]
    run_ffmpeg(cmd, f"Trimming segment: {start:.1f}s to {end:.1f}s (mobile compatible)")


def concat_videos_mobile_compatible(clip_paths, output_path):
    """Concatenate multiple videos with mobile-compatible settings."""
    if len(clip_paths) == 1:
        cmd = [
            'ffmpeg',
            '-i', clip_paths[0],
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '22',
            '-profile:v', 'baseline',
            '-level', '3.0',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-movflags', '+faststart',
            '-y',
            output_path
        ]
        run_ffmpeg(cmd, "Re-encoding for mobile compatibility")
        return

    list_path = os.path.join(TEMP_DIR, f'list_{uuid.uuid4().hex}.txt')
    with open(list_path, 'w') as f:
        for p in clip_paths:
            abs_path = os.path.abspath(p).replace('\\', '/')
            f.write(f"file '{abs_path}'\n")

    cmd = [
        'ffmpeg',
        '-f', 'concat',
        '-safe', '0',
        '-i', list_path,
        '-c:v', 'libx264',
        '-preset', 'medium',
        '-crf', '22',
        '-profile:v', 'baseline',
        '-level', '3.0',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-ar', '44100',
        '-movflags', '+faststart',
        '-y',
        output_path
    ]
    run_ffmpeg(cmd, f"Concatenating {len(clip_paths)} clips (mobile compatible)")
    os.remove(list_path)


def mix_audio_mobile_compatible(video_path, music_path, output_path, mute_original=False):
    """Mix audio with mobile-compatible settings."""
    video_duration, _, _, _ = get_video_info(video_path)

    probe_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', video_path]
    result = subprocess.run(probe_cmd, capture_output=True, text=True)
    has_audio = False
    if result.returncode == 0:
        data = json.loads(result.stdout)
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'audio':
                has_audio = True
                break

    music_duration = get_video_info(music_path)[0]
    trimmed_music = False

    print(f"\n🎵 Audio Mixing (Mobile Compatible):")
    print(f"   Video duration: {video_duration:.1f}s")
    print(f"   Music duration: {music_duration:.1f}s")
    print(f"   Mute Original: {mute_original}")
    print(f"   Video has audio: {has_audio}")

    if music_duration > video_duration:
        print(f"   ✂️ Trimming music to match video ({video_duration:.1f}s)")
        trimmed_path = music_path + '.trimmed.mp3'
        cmd = [
            'ffmpeg',
            '-i', music_path,
            '-t', str(video_duration),
            '-c', 'copy',
            '-y',
            trimmed_path
        ]
        run_ffmpeg(cmd, "Trimming music")
        music_path = trimmed_path
        trimmed_music = True

    if mute_original:
        print("   🔇 Muting original audio, using music only")
        cmd = [
            'ffmpeg',
            '-i', video_path,
            '-i', music_path,
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '22',
            '-profile:v', 'baseline',
            '-level', '3.0',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-map', '0:v:0',
            '-map', '1:a:0',
            '-shortest',
            '-movflags', '+faststart',
            '-y',
            output_path
        ]
    elif has_audio:
        print("   🎵 Mixing original audio with music")
        cmd = [
            'ffmpeg',
            '-i', video_path,
            '-i', music_path,
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '22',
            '-profile:v', 'baseline',
            '-level', '3.0',
            '-pix_fmt', 'yuv420p',
            '-filter_complex',
            '[0:a]volume=0.7[a0];[1:a]volume=0.3[a1];[a0][a1]amix=inputs=2:duration=shortest',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-movflags', '+faststart',
            '-y',
            output_path
        ]
    else:
        print("   🎵 No original audio, using music only")
        cmd = [
            'ffmpeg',
            '-i', video_path,
            '-i', music_path,
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '22',
            '-profile:v', 'baseline',
            '-level', '3.0',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-map', '0:v:0',
            '-map', '1:a:0',
            '-shortest',
            '-movflags', '+faststart',
            '-y',
            output_path
        ]

    run_ffmpeg(cmd, "Mixing audio (mobile compatible)")

    if trimmed_music and os.path.exists(music_path):
        os.remove(music_path)

    print(f"✅ Audio mixing complete (mobile compatible)!")


def generate_segments(video_path, target_duration=60):
    duration, _, _, _ = get_video_info(video_path)

    if duration <= target_duration:
        return [{"start": 0.0, "end": round(duration, 2)}]

    if target_duration <= 30:
        num_segments = 4
    elif target_duration <= 60:
        num_segments = 6
    elif target_duration <= 120:
        num_segments = 8
    else:
        num_segments = 10

    segment_duration = target_duration / num_segments
    step = duration / (num_segments + 1)

    segments = []
    for i in range(num_segments):
        center = step * (i + 1)
        start = max(0, center - segment_duration / 2)
        end = min(duration, center + segment_duration / 2)
        if end - start > 0.5:
            segments.append({"start": round(start, 2), "end": round(end, 2)})

    return segments


def cleanup_uploaded_files(job_id):
    """Delete uploaded video and music files after processing."""
    job = jobs.get(job_id)
    if not job:
        return

    video_path = job.get('video_path')
    if video_path and os.path.exists(video_path):
        try:
            os.remove(video_path)
            print(f"Deleted uploaded video: {video_path}")
        except Exception as e:
            print(f"Could not delete video: {e}")

    music_path = job.get('music_path')
    if music_path and os.path.exists(music_path):
        try:
            os.remove(music_path)
            print(f"Deleted uploaded music: {music_path}")
        except Exception as e:
            print(f"Could not delete music: {e}")


def process_video(job_id, segments, mute=False, music_id=None):
    job = jobs.get(job_id)
    if not job:
        return

    video_path = job['video_path']
    uploaded_music = job.get('music_path')

    try:
        clip_dir = os.path.join(TEMP_DIR, f"clips_{job_id}")
        os.makedirs(clip_dir, exist_ok=True)

        print(f"\nProcessing {len(segments)} segments (Mobile Compatible)...")

        clip_paths = []
        for i, seg in enumerate(segments):
            start = seg['start']
            end = seg['end']
            clip_path = os.path.join(clip_dir, f"clip_{i}.mp4")
            print(f"   Segment {i+1}: {start:.1f}s - {end:.1f}s")
            trim_segment_mobile_compatible(video_path, start, end, clip_path)
            clip_paths.append(clip_path)

        merged_path = os.path.join(TEMP_DIR, f"merged_{job_id}.mp4")
        print(f"Concatenating {len(clip_paths)} clips...")
        concat_videos_mobile_compatible(clip_paths, merged_path)

        music_path = None
        if uploaded_music and os.path.exists(uploaded_music):
            music_path = uploaded_music
        elif music_id and music_id != 'none':
            music_path = get_music_path(music_id)

        output_path = os.path.join(OUTPUT_DIR, f"highlight_{job_id}.mp4")

        if music_path and os.path.exists(music_path):
            print("Adding background music...")
            mix_audio_mobile_compatible(merged_path, music_path, output_path, mute)
            os.remove(merged_path)
        else:
            print("No music selected, re-encoding for mobile compatibility...")
            cmd = [
                'ffmpeg',
                '-i', merged_path,
                '-c:v', 'libx264',
                '-preset', 'medium',
                '-crf', '22',
                '-profile:v', 'baseline',
                '-level', '3.0',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-movflags', '+faststart',
                '-y',
                output_path
            ]
            run_ffmpeg(cmd, "Re-encoding for mobile compatibility")
            os.remove(merged_path)

        shutil.rmtree(clip_dir)
        cleanup_uploaded_files(job_id)

        job['output_path'] = output_path
        job['status'] = 'done'
        jobs[job_id] = job

        conn = get_db()
        conn.execute('UPDATE jobs SET status = ?, output_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', ('done', output_path, job_id))
        conn.commit(); conn.close()

        file_size = os.path.getsize(output_path) / 1024 / 1024
        print(f"\nVideo saved (Mobile Compatible): {output_path}")
        print(f"   File size: {file_size:.1f} MB")
        print(f"   Duration: {get_video_info(output_path)[0]:.1f}s")
        print(f"   Codec: H.264/AAC (Universal compatibility)")
        print("Uploaded files have been cleaned up")

    except Exception as e:
        print(f"Processing error: {e}")
        import traceback
        traceback.print_exc()

        cleanup_uploaded_files(job_id)
        job['status'] = 'error'
        job['error'] = str(e)
        jobs[job_id] = job

        conn = get_db()
        conn.execute('UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', ('error', job_id))
        conn.commit(); conn.close()

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html', is_logged_in=bool(session.get('user_id')), username=session.get('username'))


@app.route('/signup', methods=['GET', 'POST'])
def signup_page():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''

        if not username or not email or not password:
            return render_template('signup.html', error='All fields are required.')

        if len(password) < 6:
            return render_template('signup.html', error='Password must be at least 6 characters long.')

        if get_user_by_email(email):
            return render_template('signup.html', error='This email is already registered.')

        conn = get_db()
        conn.execute(
            'INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)',
            (username, email, generate_password_hash(password), 'user')
        )
        conn.commit(); conn.close()

        user = get_user_by_email(email)
        session['user_id'] = user['id']
        session['username'] = user['username']
        return redirect(url_for('dashboard'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''

        user = get_user_by_email(email)
        if not user or not check_password_hash(user['password_hash'], password):
            return render_template('login.html', error='Invalid email or password.')

        session['user_id'] = user['id']
        session['username'] = user['username']
        return redirect(url_for('dashboard'))

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


@app.route('/dashboard')
@login_required
def dashboard():
    user = get_user_by_id(session['user_id'])
    conn = get_db()
    social_accounts = conn.execute(
        'SELECT * FROM social_accounts WHERE user_id = ? ORDER BY created_at DESC',
        (user['id'],)
    ).fetchall()
    scheduled_posts = conn.execute(
        'SELECT * FROM scheduled_posts WHERE user_id = ? ORDER BY scheduled_for DESC',
        (user['id'],)
    ).fetchall()
    social_posts = conn.execute(
        'SELECT * FROM social_posts WHERE user_id = ? ORDER BY created_at DESC',
        (user['id'],)
    ).fetchall()
    jobs_rows = conn.execute(
        'SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC',
        (user['id'],)
    ).fetchall()
    conn.close()
    return render_template('dashboard.html', user=user, social_accounts=social_accounts, scheduled_posts=scheduled_posts, social_posts=social_posts, jobs=jobs_rows)


@app.route('/admin')
@admin_required
def admin_page():
    conn = get_db()
    users = conn.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    jobs_rows = conn.execute('SELECT * FROM jobs ORDER BY created_at DESC').fetchall()
    social_rows = conn.execute('SELECT * FROM social_accounts ORDER BY created_at DESC').fetchall()
    scheduled_rows = conn.execute('SELECT * FROM scheduled_posts ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('admin.html', users=users, jobs=jobs_rows, social_accounts=social_rows, scheduled_posts=scheduled_rows)


@app.route('/api/music-library')
def get_music_library():
    scan_music_library()
    return jsonify(MUSIC_LIBRARY)


@app.route('/api/upload', methods=['POST'])
def upload():
    if not session.get('user_id'):
        return jsonify({'error': 'Login required'}), 401

    video = request.files.get('video')
    music = request.files.get('music')

    if not video:
        return jsonify({'error': 'No video'}), 400

    cleanup_old_uploads()
    job_id = str(uuid.uuid4())
    video_path = os.path.join(UPLOAD_DIR, f"{job_id}_video.mp4")
    video.save(video_path)

    music_path = None
    if music:
        music_path = os.path.join(UPLOAD_DIR, f"{job_id}_music.mp3")
        music.save(music_path)

    jobs[job_id] = {
        'video_path': video_path,
        'music_path': music_path,
        'segments': [],
        'suggested': [],
        'output_path': None,
        'status': 'pending',
        'user_id': session['user_id']
    }

    save_job_record(job_id, session['user_id'], video_path, music_path, '[]', 'pending', None)
    return jsonify({'id': job_id})


@app.route('/api/auto-generate/<job_id>', methods=['POST'])
def auto_generate(job_id):
    if not session.get('user_id'):
        return jsonify({'error': 'Login required'}), 401

    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    data = request.get_json() or {}
    target_duration = data.get('targetDuration', 60)
    mute = data.get('mute', False)
    music_id = data.get('musicId', None)

    try:
        suggested = generate_segments(job['video_path'], target_duration)
        job['suggested'] = suggested
        job['segments'] = suggested
        threading.Thread(target=process_video, args=(job_id, suggested, mute, music_id)).start()
        return jsonify({'message': 'Processing started', 'segments': suggested})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/generate/<job_id>', methods=['POST'])
def generate(job_id):
    if not session.get('user_id'):
        return jsonify({'error': 'Login required'}), 401

    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    data = request.get_json()
    segments = data.get('segments')
    mute = data.get('mute', False)
    music_id = data.get('musicId', None)

    if not segments or len(segments) == 0:
        return jsonify({'error': 'No segments provided'}), 400

    job['segments'] = segments
    job['status'] = 'processing'
    update_job_status(job_id, 'processing')
    jobs[job_id] = job

    threading.Thread(target=process_video, args=(job_id, segments, mute, music_id)).start()
    return jsonify({'message': 'Processing started'})


@app.route('/api/status/<job_id>')
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'id': job_id,
        'status': job['status'],
        'error': job.get('error'),
        'outputPath': job.get('output_path')
    })


@app.route('/api/download/<job_id>')
def download(job_id):
    job = jobs.get(job_id)
    if not job or job['status'] != 'done' or not job.get('output_path'):
        return jsonify({'error': 'Video not ready'}), 400

    if not os.path.exists(job['output_path']):
        return jsonify({'error': 'File not found'}), 404

    return send_file(job['output_path'], as_attachment=True, download_name=f"highlight_{job_id}.mp4")


@app.route('/auth/youtube')
@login_required
def youtube_oauth_start():
    if not YOUTUBE_CLIENT_ID:
        return jsonify({'error': 'YOUTUBE_CLIENT_ID is not configured.'}), 500

    params = {
        'client_id': YOUTUBE_CLIENT_ID,
        'redirect_uri': YOUTUBE_REDIRECT_URI,
        'response_type': 'code',
        'scope': YOUTUBE_SCOPES,
        'access_type': 'offline',
        'prompt': 'consent'
    }
    auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params)
    return redirect(auth_url)


@app.route('/debug/youtube')
@login_required
def debug_youtube_status():
    user_id = session.get('user_id')
    conn = get_db()
    account = conn.execute(
        'SELECT * FROM social_accounts WHERE user_id = ? AND platform = ? ORDER BY id DESC LIMIT 1',
        (user_id, 'youtube')
    ).fetchone()
    conn.close()

    token = dict(account).get('access_token') if account else None
    env_status = {
        'YOUTUBE_CLIENT_ID': bool(YOUTUBE_CLIENT_ID),
        'YOUTUBE_CLIENT_SECRET': bool(YOUTUBE_CLIENT_SECRET),
        'YOUTUBE_REDIRECT_URI': YOUTUBE_REDIRECT_URI,
        'YOUTUBE_ClENT_ID': bool(os.environ.get('YOUTUBE_ClENT_ID')),
        'YOUTUBE_ClENT_SECRET': bool(os.environ.get('YOUTUBE_ClENT_SECRET'))
    }

    result = {
        'ok': False,
        'user_id': user_id,
        'env': env_status,
        'db_account_found': bool(account),
        'token_present': bool(token),
        'token_prefix': token[:12] if token else None,
        'checks': []
    }

    if not YOUTUBE_CLIENT_ID or not YOUTUBE_CLIENT_SECRET:
        result['checks'].append({'name': 'OAuth client config', 'ok': False, 'error': 'Missing YOUTUBE_CLIENT_ID or YOUTUBE_CLIENT_SECRET'})
    else:
        result['checks'].append({'name': 'OAuth client config', 'ok': True})

    if not account or not token:
        result['checks'].append({'name': 'Stored YouTube token', 'ok': False, 'error': 'No saved YouTube access token found in DB'})
    else:
        result['checks'].append({'name': 'Stored YouTube token', 'ok': True})

    if token:
        try:
            token_response = requests.get(
                'https://www.googleapis.com/oauth2/v1/tokeninfo',
                params={'access_token': token},
                timeout=30
            )
            body = token_response.json() if token_response.text.strip() else {}
            if token_response.status_code >= 400:
                result['checks'].append({'name': 'Token validation', 'ok': False, 'status_code': token_response.status_code, 'error': body.get('error') or body.get('error_description') or token_response.text[:500]})
            else:
                result['checks'].append({'name': 'Token validation', 'ok': True, 'details': body})
        except Exception as exc:
            result['checks'].append({'name': 'Token validation', 'ok': False, 'error': str(exc)})

    if result['checks']:
        result['ok'] = all(item.get('ok', False) for item in result['checks'])

    return jsonify(result)


@app.route('/oauth/youtube/callback')
@login_required
def youtube_oauth_callback():
    code = request.args.get('code')
    error = request.args.get('error')
    if error:
        return redirect('/dashboard?oauth_error=' + error)
    if not code:
        return redirect('/dashboard?oauth_error=no_code')

    if not YOUTUBE_CLIENT_ID or not YOUTUBE_CLIENT_SECRET:
        return redirect('/dashboard?oauth_error=missing_credentials')

    token_response = requests.post(
        'https://oauth2.googleapis.com/token',
        data={
            'code': code,
            'client_id': YOUTUBE_CLIENT_ID,
            'client_secret': YOUTUBE_CLIENT_SECRET,
            'redirect_uri': YOUTUBE_REDIRECT_URI,
            'grant_type': 'authorization_code'
        },
        timeout=30
    )

    token_data = token_response.json()
    access_token = token_data.get('access_token')
    refresh_token = token_data.get('refresh_token')

    if not access_token:
        return redirect('/dashboard?oauth_error=token_failed')

    channel_response = requests.get(
        'https://www.googleapis.com/youtube/v3/channels',
        params={'part': 'snippet', 'mine': 'true'},
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=30
    )
    channel_data = channel_response.json()
    channel_name = 'YouTube Channel'
    if channel_data.get('items'):
        channel_name = channel_data['items'][0].get('snippet', {}).get('title', channel_name)

    conn = get_db()
    existing = conn.execute(
        'SELECT id FROM social_accounts WHERE user_id = ? AND platform = ? ORDER BY id DESC LIMIT 1',
        (session['user_id'], 'youtube')
    ).fetchone()

    if existing:
        conn.execute(
            '''
            UPDATE social_accounts
            SET account_name = ?, profile_url = 'https://www.youtube.com', access_token = ?, refresh_token = ?, status = 'connected', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (channel_name, access_token, refresh_token, existing['id'])
        )
    else:
        conn.execute(
            '''
            INSERT INTO social_accounts (user_id, platform, account_name, profile_url, access_token, refresh_token, status)
            VALUES (?, 'youtube', ?, 'https://www.youtube.com', ?, ?, 'connected')
            ''',
            (session['user_id'], channel_name, access_token, refresh_token)
        )
    conn.commit(); conn.close()

    return redirect('/dashboard?oauth_success=youtube')


def _publish_youtube_video(user_id, job_id, title=None, description=None, tags_raw=None, schedule_for=None, publish_mode='publish', privacy_status='public', payload_file=None, debug_mode=False):
    if not job_id:
        return {'error': 'job_id is required'}, 400

    job = jobs.get(job_id)
    if not job or not job.get('output_path'):
        conn = get_db()
        job_row = conn.execute('SELECT * FROM jobs WHERE id = ? AND user_id = ?', (job_id, user_id)).fetchone()
        conn.close()
        if not job_row or not job_row['output_path']:
            return {'error': 'Video not ready'}, 400
        job = dict(job_row)

    if isinstance(tags_raw, str):
        tag_list = [tag.strip() for tag in tags_raw.split(',') if tag.strip()]
    else:
        tag_list = [str(tag).strip() for tag in (tags_raw or []) if str(tag).strip()]

    title = (title or job.get('title') or 'ClipForge Highlight').strip() or 'ClipForge Highlight'
    description = (description or job.get('description') or 'Generated by ClipForge').strip() or 'Generated by ClipForge'

    conn = get_db()
    conn.execute(
        'UPDATE jobs SET title = ?, description = ?, tags = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        (title, description, ','.join(tag_list), job_id)
    )
    conn.commit(); conn.close()

    if publish_mode == 'schedule':
        if not schedule_for:
            return {'error': 'Schedule time is required if you choose schedule.'}, 400
        conn = get_db()
        conn.execute(
            'INSERT INTO scheduled_posts (user_id, job_id, platform, message, scheduled_for, status) VALUES (?, ?, ?, ?, ?, ?)',
            (user_id, job_id, 'youtube', f'{title} - {description}', schedule_for, 'pending')
        )
        conn.execute(
            'UPDATE jobs SET publish_status = ?, published_platforms = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            ('scheduled', 'youtube', job_id)
        )
        conn.commit(); conn.close()
        return {'message': 'YouTube publish scheduled.', 'schedule_for': schedule_for}, 200

    conn = get_db()
    account = conn.execute(
        'SELECT * FROM social_accounts WHERE user_id = ? AND platform = ? ORDER BY id DESC LIMIT 1',
        (user_id, 'youtube')
    ).fetchone()
    conn.close()

    if not account:
        return {'error': 'YouTube account not connected'}, 400

    access_token, _ = get_valid_youtube_token(user_id)
    if not access_token:
        return {'error': 'YouTube authentication expired. Please reconnect your YouTube account.'}, 401

    video_path = job['output_path']
    if not os.path.exists(video_path):
        return {'error': 'Output file not found'}, 404

    thumbnail_path = None
    if payload_file and getattr(payload_file, 'filename', None):
        thumbnail_ext = os.path.splitext(payload_file.filename)[1] or '.jpg'
        thumbnail_path = os.path.join(TEMP_DIR, f"thumb_{job_id}{thumbnail_ext}")
        payload_file.save(thumbnail_path)
        conn = get_db()
        conn.execute('UPDATE jobs SET thumbnail_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (thumbnail_path, job_id))
        conn.commit(); conn.close()

    privacy_choice = privacy_status if privacy_status in {'public', 'private', 'unlisted'} else 'public'
    metadata = {
        'snippet': {
            'title': title,
            'description': description or 'Generated by ClipForge',
            'tags': tag_list or ['clipforge', 'highlight', 'video'],
            'categoryId': '22'
        },
        'status': {
            'privacyStatus': privacy_choice
        }
    }

    debug_payload = {
        'job_id': job_id,
        'user_id': user_id,
        'video_path': video_path,
        'video_exists': os.path.exists(video_path),
        'token_present': bool(access_token),
        'token_prefix': access_token[:12] if access_token else None,
        'title': title,
        'description': description,
        'tags': tag_list,
        'thumbnail_path': thumbnail_path
    }

    try:
        metadata_payload = json.dumps(metadata)
        init_headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json; charset=UTF-8',
            'X-Upload-Content-Type': 'video/mp4',
            'X-Upload-Content-Length': str(os.path.getsize(video_path))
        }

        init_response = requests.post(
            'https://www.googleapis.com/upload/youtube/v3/videos?part=snippet,status&uploadType=resumable',
            headers=init_headers,
            data=metadata_payload,
            timeout=120
        )

        response_text = init_response.text.strip()
        try:
            result = init_response.json() if response_text else {}
        except ValueError:
            result = {}

        if init_response.status_code >= 400:
            google_error = result.get('error', {}) if isinstance(result, dict) else {}
            message = google_error.get('message') if isinstance(google_error, dict) else None
            if not message:
                message = response_text[:500] or 'YouTube upload failed'
            payload = {'error': message, 'status_code': init_response.status_code}
            if debug_mode:
                payload['debug'] = {**debug_payload, 'response_status': init_response.status_code, 'response_body': response_text[:1000], 'response_json': result}
            return payload, init_response.status_code

        upload_url = init_response.headers.get('Location') or init_response.headers.get('location')
        if not upload_url:
            payload = {'error': 'YouTube did not return an upload Location header', 'status_code': init_response.status_code, 'response_body': response_text[:1000]}
            if debug_mode:
                payload['debug'] = {**debug_payload, 'response_status': init_response.status_code, 'response_body': response_text[:1000], 'response_json': result}
            return payload, 500

        with open(video_path, 'rb') as video_file:
            file_data = video_file.read()

        put_response = requests.put(
            upload_url,
            headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'video/mp4'},
            data=file_data,
            timeout=300
        )

        put_text = put_response.text.strip()
        try:
            final_result = put_response.json() if put_text else {}
        except ValueError:
            final_result = {}

        if put_response.status_code >= 400:
            google_error = final_result.get('error', {}) if isinstance(final_result, dict) else {}
            message = google_error.get('message') if isinstance(google_error, dict) else None
            if not message:
                message = put_text[:500] or 'YouTube upload failed'
            payload = {'error': message, 'status_code': put_response.status_code}
            if debug_mode:
                payload['debug'] = {**debug_payload, 'response_status': put_response.status_code, 'response_body': put_text[:1000], 'response_json': final_result}
            return payload, put_response.status_code

        video_id = final_result.get('id') or None
        if thumbnail_path and video_id:
            try:
                with open(thumbnail_path, 'rb') as thumb_file:
                    thumb_bytes = thumb_file.read()
                thumb_mime = 'image/jpeg'
                if thumbnail_path.lower().endswith('.png'):
                    thumb_mime = 'image/png'
                thumb_response = requests.post(
                    f'https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}',
                    headers={'Authorization': f'Bearer {access_token}', 'Content-Type': thumb_mime},
                    data=thumb_bytes,
                    timeout=120
                )
                if thumb_response.status_code >= 400:
                    print(f'Thumbnail upload warning: {thumb_response.status_code} - {thumb_response.text[:500]}')
            except Exception as exc:
                print(f'Thumbnail upload exception: {exc}')

        conn = get_db()
        conn.execute(
            'UPDATE jobs SET publish_status = ?, published_platforms = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            ('published', 'youtube', job_id)
        )
        conn.commit(); conn.close()

        payload = {'message': 'Published to YouTube', 'status': 'published', 'result': final_result}
        if debug_mode:
            payload['debug'] = {**debug_payload, 'response_status': put_response.status_code, 'response_body': put_text[:1000], 'response_json': final_result}
        return payload, 200
    except Exception as exc:
        payload = {'error': f'YouTube upload exception: {str(exc)}'}
        if debug_mode:
            payload['debug'] = {**debug_payload, 'exception_type': type(exc).__name__, 'exception_message': str(exc)}
        return payload, 500


@app.route('/api/youtube/publish', methods=['POST'])
def youtube_publish_api():
    if not session.get('user_id'):
        return jsonify({'error': 'Login required'}), 401

    if request.content_type and 'multipart/form-data' in request.content_type:
        data = request.form
        payload_file = request.files.get('thumbnail')
        debug_mode = bool(data.get('debug'))
        job_id = data.get('job_id')
        title = (data.get('title') or 'ClipForge Highlight').strip()
        description = (data.get('description') or '').strip()
        tags_raw = data.get('tags') or ''
        schedule_for = (data.get('schedule_for') or '').strip()
        publish_mode = (data.get('publish_mode') or 'publish').strip().lower()
        privacy_status = (data.get('privacy_status') or 'public').strip().lower()
    else:
        data = request.get_json() or {}
        payload_file = None
        debug_mode = bool(data.get('debug')) or request.args.get('debug') == '1'
        job_id = data.get('job_id')
        title = (data.get('title') or 'ClipForge Highlight').strip()
        description = (data.get('description') or '').strip()
        tags_raw = data.get('tags') or ''
        schedule_for = (data.get('schedule_for') or '').strip()
        publish_mode = (data.get('publish_mode') or 'publish').strip().lower()
        privacy_status = (data.get('privacy_status') or 'public').strip().lower()

    payload, status_code = _publish_youtube_video(
        session['user_id'],
        job_id,
        title=title,
        description=description,
        tags_raw=tags_raw,
        schedule_for=schedule_for,
        publish_mode=publish_mode,
        privacy_status=privacy_status,
        payload_file=payload_file,
        debug_mode=debug_mode,
    )
    return jsonify(payload), status_code


@app.route('/api/social-accounts', methods=['GET', 'POST'])
@login_required
def social_accounts_api():
    user_id = session['user_id']
    if request.method == 'POST':
        data = request.get_json() or {}
        platform = (data.get('platform') or '').strip().lower()
        account_name = (data.get('account_name') or '').strip()
        profile_url = (data.get('profile_url') or '').strip()
        access_token = (data.get('access_token') or '').strip()
        page_or_user_id = (data.get('page_or_user_id') or '').strip()

        if not platform or not account_name:
            return jsonify({'error': 'Platform and account name are required.'}), 400

        if platform in {'instagram', 'facebook', 'x'} and not access_token:
            return jsonify({'error': 'An access token is required to connect this platform.'}), 400

        if platform in {'instagram', 'facebook'} and not (profile_url or page_or_user_id):
            return jsonify({'error': 'Add the Page/User ID or profile URL for this account connection.'}), 400

        profile_value = profile_url or page_or_user_id or None

        conn = get_db()
        existing = conn.execute(
            'SELECT id FROM social_accounts WHERE user_id = ? AND platform = ? ORDER BY id DESC LIMIT 1',
            (user_id, platform)
        ).fetchone()

        if existing:
            conn.execute(
                '''
                UPDATE social_accounts
                SET account_name = ?, profile_url = ?, access_token = ?, status = 'connected', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''',
                (account_name, profile_value, access_token or None, existing['id'])
            )
        else:
            conn.execute(
                'INSERT INTO social_accounts (user_id, platform, account_name, profile_url, access_token, status) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, platform, account_name, profile_value, access_token or None, 'connected')
            )
        conn.commit(); conn.close()
        return jsonify({'message': 'Social account connected.'})

    conn = get_db()
    rows = conn.execute('SELECT * FROM social_accounts WHERE user_id = ? ORDER BY created_at DESC', (user_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/social-posts', methods=['GET', 'POST'])
@login_required
def social_posts_api():
    user_id = session['user_id']
    if request.method == 'POST':
        data = request.get_json() or {}
        platform = (data.get('platform') or '').strip().lower()
        message = (data.get('message') or '').strip()
        job_id = data.get('job_id')
        media_url = (data.get('media_url') or '').strip()
        scheduled_for = (data.get('scheduled_for') or '').strip()
        action = (data.get('action') or 'schedule').strip().lower()

        if not platform or not message:
            return jsonify({'error': 'Platform and message are required.'}), 400

        if platform == 'youtube':
            if action == 'schedule':
                payload, status_code = _publish_youtube_video(
                    user_id,
                    job_id,
                    title=(data.get('title') or '').strip() or message,
                    description=(data.get('description') or '').strip() or message,
                    tags_raw=(data.get('tags') or ''),
                    schedule_for=scheduled_for,
                    publish_mode='schedule',
                    privacy_status=(data.get('privacy_status') or 'public').strip().lower(),
                )
                if status_code >= 400:
                    return jsonify(payload), status_code
                conn = get_db()
                conn.execute(
                    'INSERT INTO social_posts (user_id, job_id, platform, message, media_url, scheduled_for, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (user_id, job_id, platform, message, media_url or None, scheduled_for or None, 'pending')
                )
                conn.commit(); conn.close()
                return jsonify({'message': payload.get('message', 'YouTube publish scheduled.'), 'status': 'scheduled', 'schedule_for': scheduled_for})

            payload, status_code = _publish_youtube_video(
                user_id,
                job_id,
                title=(data.get('title') or '').strip() or message,
                description=(data.get('description') or '').strip() or message,
                tags_raw=(data.get('tags') or ''),
                privacy_status=(data.get('privacy_status') or 'public').strip().lower(),
            )
            if status_code >= 400:
                return jsonify(payload), status_code

            conn = get_db()
            conn.execute(
                'INSERT INTO social_posts (user_id, job_id, platform, message, media_url, scheduled_for, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (user_id, job_id, platform, message, media_url or None, scheduled_for or None, 'published')
            )
            conn.commit(); conn.close()
            return jsonify({'message': payload.get('message', 'YouTube post published successfully.'), 'status': 'published'})

        if platform == 'x':
            payload, status_code = _publish_to_x(
                user_id,
                job_id,
                message,
                media_url=media_url,
                scheduled_for=scheduled_for,
                action=action,
            )
            if status_code >= 400:
                return jsonify(payload), status_code

            conn = get_db()
            conn.execute(
                'INSERT INTO social_posts (user_id, job_id, platform, message, media_url, scheduled_for, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (user_id, job_id, platform, message, media_url or None, scheduled_for or None, 'published' if action == 'publish' else 'pending')
            )
            conn.commit(); conn.close()
            return jsonify(payload)

        if platform == 'facebook':
            payload, status_code = _publish_to_facebook(
                user_id,
                job_id,
                message,
                media_url=media_url,
                scheduled_for=scheduled_for,
                action=action,
            )
            if status_code >= 400:
                return jsonify(payload), status_code

            conn = get_db()
            conn.execute(
                'INSERT INTO social_posts (user_id, job_id, platform, message, media_url, scheduled_for, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (user_id, job_id, platform, message, media_url or None, scheduled_for or None, 'published' if action == 'publish' else 'pending')
            )
            conn.commit(); conn.close()
            return jsonify(payload)

        if platform == 'instagram':
            payload, status_code = _publish_to_instagram(
                user_id,
                job_id,
                message,
                media_url=media_url,
                scheduled_for=scheduled_for,
                action=action,
            )
            if status_code >= 400:
                return jsonify(payload), status_code

            conn = get_db()
            conn.execute(
                'INSERT INTO social_posts (user_id, job_id, platform, message, media_url, scheduled_for, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (user_id, job_id, platform, message, media_url or None, scheduled_for or None, 'published' if action == 'publish' else 'pending')
            )
            conn.commit(); conn.close()
            return jsonify(payload)

        status = 'published' if action == 'publish' else 'pending'
        if action == 'publish':
            if job_id:
                conn = get_db()
                row = conn.execute('SELECT published_platforms FROM jobs WHERE id = ? AND user_id = ?', (job_id, user_id)).fetchone()
                if row:
                    current = row['published_platforms'] or ''
                    values = [p.strip() for p in current.split(',') if p.strip()]
                    if platform not in values:
                        values.append(platform)
                    conn.execute('UPDATE jobs SET publish_status = ?, published_platforms = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', ('published', ','.join(values), job_id))
                    conn.commit()
                conn.close()

        conn = get_db()
        conn.execute(
            'INSERT INTO social_posts (user_id, job_id, platform, message, media_url, scheduled_for, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (user_id, job_id, platform, message, media_url or None, scheduled_for or None, status)
        )
        conn.commit(); conn.close()

        if action == 'schedule':
            return jsonify({'message': f'{platform.title()} post scheduled successfully.', 'status': 'scheduled'})
        return jsonify({'message': f'{platform.title()} post published successfully.', 'status': 'published'})

    conn = get_db()
    rows = conn.execute('SELECT * FROM social_posts WHERE user_id = ? ORDER BY created_at DESC', (user_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/schedule-post', methods=['POST'])
@login_required
def schedule_post_api():
    user_id = session['user_id']
    data = request.get_json() or {}
    platform = (data.get('platform') or '').strip().lower()
    message = (data.get('message') or '').strip()
    scheduled_for = (data.get('scheduled_for') or '').strip()
    job_id = data.get('job_id')

    if not platform or not message or not scheduled_for:
        return jsonify({'error': 'Platform, message, and schedule time are required.'}), 400

    conn = get_db()
    conn.execute(
        'INSERT INTO scheduled_posts (user_id, job_id, platform, message, scheduled_for, status) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, job_id, platform, message, scheduled_for, 'pending')
    )
    conn.commit(); conn.close()
    return jsonify({'message': 'Post scheduled.'})


@app.route('/api/cleanup/<job_id>', methods=['DELETE'])
def cleanup_job(job_id):
    """Manually cleanup a job's files."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    output_path = job.get('output_path')
    if output_path and os.path.exists(output_path):
        try:
            os.remove(output_path)
            print(f"Deleted output video: {output_path}")
        except Exception as e:
            print(f"Could not delete output: {e}")

    cleanup_uploaded_files(job_id)
    del jobs[job_id]

    return jsonify({'message': 'Cleanup complete'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))
    print("\n" + "="*60)
    print("🎬 ClipForge Server Starting...")
    print("="*60)
    print(f"\n📁 Music Library: {MUSIC_LIBRARY_DIR}/ ({len(MUSIC_LIBRARY)-1} tracks)")
    print(f"📁 Temp Uploads: {UPLOAD_DIR}/")
    print(f"📁 Output: {OUTPUT_DIR}/")
    print(f"🌐 Binding to: 0.0.0.0:{port}")
    print("\n📌 Features:")
    print("   ✅ Login and signup")
    print("   ✅ Admin section")
    print("   ✅ Social account tracking")
    print("   ✅ Scheduled post recording")
    print("   ✅ Video trimming (H.264/AAC)")
    print("   ✅ Mobile compatible output")
    print("   ✅ Background music (library + custom upload)")
    print("   ✅ Mute original audio")
    print("   ✅ Auto-segment generation")
    print("   ✅ Manual timeline editing")
    print("   ✅ Temp uploads deleted after 24 hours")
    print("\n🎯 Video Settings (Universal Compatibility):")
    print("   📹 Codec: H.264 (Baseline Profile)")
    print("   🎵 Audio: AAC (128kbps)")
    print("   📱 Compatible: iPhone, Android, Web")
    print("\n" + "="*60)
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
