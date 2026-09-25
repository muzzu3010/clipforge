import json
import os
import sqlite3
from urllib.parse import urlencode

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'app.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_latest_youtube_account(user_id=None):
    conn = get_db()
    if user_id is None:
        row = conn.execute(
            "SELECT * FROM social_accounts WHERE platform = 'youtube' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM social_accounts WHERE user_id = ? AND platform = 'youtube' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def check_env():
    env = {
        'YOUTUBE_CLIENT_ID': bool(os.environ.get('YOUTUBE_CLIENT_ID')),
        'YOUTUBE_CLIENT_SECRET': bool(os.environ.get('YOUTUBE_CLIENT_SECRET')),
        'YOUTUBE_REDIRECT_URI': os.environ.get('YOUTUBE_REDIRECT_URI'),
        'YOUTUBE_ClENT_ID': bool(os.environ.get('YOUTUBE_ClENT_ID')),
        'YOUTUBE_ClENT_SECRET': bool(os.environ.get('YOUTUBE_ClENT_SECRET')),
    }
    return env


def validate_access_token(token):
    if not token:
        return {'ok': False, 'error': 'No access token found'}

    url = 'https://www.googleapis.com/oauth2/v1/tokeninfo'
    try:
        resp = requests.get(url, params={'access_token': token}, timeout=30)
        data = resp.json() if resp.text.strip() else {}
        if resp.status_code >= 400:
            return {
                'ok': False,
                'status_code': resp.status_code,
                'error': data.get('error') or data.get('error_description') or resp.text[:500],
            }

        return {
            'ok': True,
            'status_code': resp.status_code,
            'data': data,
        }
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


def validate_youtube_channel(token):
    if not token:
        return {'ok': False, 'error': 'No token available'}

    url = 'https://www.googleapis.com/youtube/v3/channels'
    params = {'part': 'snippet,statistics', 'mine': 'true'}
    headers = {'Authorization': f'Bearer {token}'}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        data = resp.json() if resp.text.strip() else {}
        if resp.status_code >= 400:
            return {
                'ok': False,
                'status_code': resp.status_code,
                'error': data.get('error') or resp.text[:500],
            }
        return {'ok': True, 'status_code': resp.status_code, 'data': data}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


def main():
    print('=== YouTube Debug Check ===')
    env = check_env()
    print('Environment variables:')
    for key, value in env.items():
        print(f'  {key}: {value}')

    account = get_latest_youtube_account()
    print('\nLatest YouTube account record:')
    print(json.dumps(account, indent=2, default=str) if account else 'No YouTube account found in database.')

    if account and account.get('access_token'):
        token = account.get('access_token')
        print('\nValidating access token...')
        token_check = validate_access_token(token)
        print(json.dumps(token_check, indent=2, default=str))

        print('\nChecking YouTube channel access...')
        channel_check = validate_youtube_channel(token)
        print(json.dumps(channel_check, indent=2, default=str))
    else:
        print('\nNo access token is stored for YouTube. Run the OAuth flow before testing upload.')

    print('\nSummary:')
    ok = all([env['YOUTUBE_CLIENT_ID'], env['YOUTUBE_CLIENT_SECRET'], env['YOUTUBE_REDIRECT_URI']])
    print(f'  Basic OAuth credentials configured: {ok}')
    if account and account.get('access_token'):
        print(f'  YouTube token stored in DB: True')
    else:
        print(f'  YouTube token stored in DB: False')


if __name__ == '__main__':
    main()
