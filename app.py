import os
from flask import Flask, render_template, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
YOUTUBE_CHANNEL_ID = os.getenv('YOUTUBE_CHANNEL_ID')


import time

# Simple in-memory cache
CACHE = {
    'guest_leaderboard': {'data': None, 'timestamp': 0},
    'top_videos_leaderboard': {'data': None, 'timestamp': 0}
}
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

def get_top_videos_leaderboard(api_key, channel_id, limit=10):
    # Step 1: Get all video IDs from the channel (up to 50 due to API limit)
    search_url = (
        f'https://www.googleapis.com/youtube/v3/search?key={api_key}'
        f'&channelId={channel_id}&part=snippet,id&order=date&maxResults=50&type=video'
    )
    search_response = requests.get(search_url)
    search_data = search_response.json()
    video_ids = [item['id']['videoId'] for item in search_data.get('items', [])]

    if not video_ids:
        return []

    # Step 2: Get video statistics for all videos
    videos_url = (
        f'https://www.googleapis.com/youtube/v3/videos?key={api_key}'
        f'&id={"%2C".join(video_ids)}&part=snippet,statistics'
    )
    videos_response = requests.get(videos_url)
    videos_data = videos_response.json()
    videos = videos_data.get('items', [])

    # Step 3: Sort videos by view count descending and return top N
    leaderboard = sorted(
        videos,
        key=lambda x: int(x['statistics'].get('viewCount', 0)),
        reverse=True
    )[:limit]
    return leaderboard

def get_guest_leaderboard(api_key, channel_id, limit=10):
    from collections import defaultdict
    import time
    import re
    # Step 1: Get uploads playlist ID
    channel_url = f'https://www.googleapis.com/youtube/v3/channels?part=contentDetails&id={channel_id}&key={api_key}'
    channel_resp = requests.get(channel_url)
    channel_data = channel_resp.json()
    try:
        uploads_playlist_id = channel_data['items'][0]['contentDetails']['relatedPlaylists']['uploads']
    except Exception:
        return []
    # Step 2: Iterate through all playlistItems to get all video IDs and titles
    video_ids = []
    video_titles = {}
    next_page_token = ''
    while True:
        playlist_url = (
            f'https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={uploads_playlist_id}&maxResults=50&key={api_key}'
            + (f'&pageToken={next_page_token}' if next_page_token else '')
        )
        playlist_resp = requests.get(playlist_url)
        playlist_data = playlist_resp.json()
        for item in playlist_data.get('items', []):
            if 'resourceId' in item['snippet'] and 'videoId' in item['snippet']['resourceId']:
                vid = item['snippet']['resourceId']['videoId']
                video_ids.append(vid)
                video_titles[vid] = item['snippet']['title']
        next_page_token = playlist_data.get('nextPageToken')
        if not next_page_token:
            break
        time.sleep(0.1)
    if not video_ids:
        return []
    # Step 3: Fetch video statistics in batches of 50
    video_stats = {}
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i:i+50]
        videos_url = (
            f'https://www.googleapis.com/youtube/v3/videos?key={api_key}'
            f'&id={"%2C".join(batch_ids)}&part=statistics'
        )
        videos_response = requests.get(videos_url)
        videos_data = videos_response.json()
        for v in videos_data.get('items', []):
            video_stats[v['id']] = int(v['statistics'].get('viewCount', 0))
        time.sleep(0.1)
    # Step 4: Deduplicate episodes by episode number (e.g., #394), keep highest view count
    import re
    episode_map = {}  # episode_number (str) -> (videoId, title, viewCount)
    for vid in video_ids:
        title = video_titles[vid]
        view_count = video_stats.get(vid, 0)
        # Extract episode number, e.g. #394
        ep_match = re.search(r'#(\d+)', title)
        if ep_match:
            ep_num = ep_match.group(0)  # e.g. '#394'
            # Keep only the highest-view video for this episode number
            if ep_num not in episode_map or view_count > episode_map[ep_num][2]:
                episode_map[ep_num] = (vid, title, view_count)
        else:
            # If no episode number, treat as unique
            episode_map[vid] = (vid, title, view_count)
    # Step 5: Process deduplicated episodes for guest appearances
    guest_views = defaultdict(lambda: {'total_views': 0, 'episodes': []})
    for vid, title, view_count in episode_map.values():
        # Extract guest list: between first and last dash if at least two dashes, else after first dash
        dash_positions = [m.start() for m in re.finditer('-', title)]
        if len(dash_positions) >= 2:
            guest_part = title[dash_positions[0]+1:dash_positions[-1]].strip()
        elif len(dash_positions) == 1:
            guest_part = title[dash_positions[0]+1:].strip()
        else:
            guest_part = title
        raw_guests = re.split(r'[+&,\-]', guest_part)
        guests = []
        # Manual alias mapping for special cases
        guest_aliases = {
            'TONY CARUSO': 'ADAM RAY',
            'ELAINE': 'ADAM RAY',
        }
        # List of known non-guest/event/venue phrases to ignore
        ignore_guest_terms = set([
            'NIGHT ONE', 'NIGHT TWO', 'MADISON SQUARE GARDEN', 'LIVE FROM THE YOUTUBE THEATER', '[10 YEAR ANNIVERSARY]', '10 YEAR ANNIVERSARY', 'LIVE FROM', 'THE YOUTUBE THEATER'
        ])
        for g in raw_guests:
            g = g.strip()
            if not g:
                continue
            match = re.match(r'.*\(([^\)]+)\)', g)
            if match:
                guest_name = match.group(1).strip()
            else:
                guest_name = g
            # Apply alias mapping
            guest_name = guest_aliases.get(guest_name.upper(), guest_name)
            # Filter out known event/venue terms (case-insensitive)
            if guest_name.strip().upper() in ignore_guest_terms:
                continue
            guests.append(guest_name)
        for guest_name in guests:
            # If already processed this episode for this guest, skip
            if any(ep['videoId'] == vid for ep in guest_views[guest_name]['episodes']):
                continue
            # Count this episode if guest name appears anywhere in the title (case-insensitive)
            if guest_name.lower() in title.lower() or any(alias.lower() in title.lower() for alias, real in guest_aliases.items() if real == guest_name):
                guest_views[guest_name]['total_views'] += view_count
                guest_views[guest_name]['episodes'].append({
                    'title': title,
                    'videoId': vid,
                    'viewCount': view_count
                })
    leaderboard = sorted([
        {'guest': guest, 'total_views': data['total_views'], 'episodes': data['episodes']}
        for guest, data in guest_views.items()
    ], key=lambda x: x['total_views'], reverse=True)[:limit]
    return leaderboard


@app.route('/api/leaderboard')
def api_leaderboard():
    now = time.time()
    cache = CACHE['top_videos_leaderboard']
    if cache['data'] is not None and now - cache['timestamp'] < CACHE_TTL_SECONDS:
        data = cache['data']
    else:
        data = get_top_videos_leaderboard(YOUTUBE_API_KEY, YOUTUBE_CHANNEL_ID)
        cache['data'] = data
        cache['timestamp'] = now
    if not data:
        return jsonify({'error': 'No videos found'}), 404
    leaderboard = [
        {
            'title': v['snippet']['title'],
            'videoId': v['id'],
            'viewCount': v['statistics']['viewCount'],
            'thumbnail': v['snippet']['thumbnails']['high']['url'],
            'description': v['snippet']['description']
        }
        for v in data
    ]
    return jsonify(leaderboard)

@app.route('/api/guest-leaderboard')
@app.route('/api/guest-leaderboard')
def api_guest_leaderboard():
    now = time.time()
    cache = CACHE['guest_leaderboard']
    if cache['data'] is not None and now - cache['timestamp'] < CACHE_TTL_SECONDS:
        data = cache['data']
    else:
        # Always get the FULL leaderboard (all guests, all episodes)
        data = get_guest_leaderboard(YOUTUBE_API_KEY, YOUTUBE_CHANNEL_ID, limit=10000)
        cache['data'] = data
        cache['timestamp'] = now
    return jsonify(data)


@app.route('/api/episodes')
def api_episodes():
    # Use the uploads playlist to get ALL videos, not just recent ones
    api_key = YOUTUBE_API_KEY
    channel_id = YOUTUBE_CHANNEL_ID
    import time
    # Step 1: Get uploads playlist ID
    channel_url = f'https://www.googleapis.com/youtube/v3/channels?part=contentDetails&id={channel_id}&key={api_key}'
    channel_resp = requests.get(channel_url)
    channel_data = channel_resp.json()
    try:
        uploads_playlist_id = channel_data['items'][0]['contentDetails']['relatedPlaylists']['uploads']
    except Exception:
        return jsonify({'error': 'Could not find uploads playlist for channel'}), 500
    # Step 2: Iterate through all playlistItems
    video_items = []
    next_page_token = ''
    while True:
        playlist_url = (
            f'https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={uploads_playlist_id}&maxResults=50&key={api_key}'
            + (f'&pageToken={next_page_token}' if next_page_token else '')
        )
        playlist_resp = requests.get(playlist_url)
        playlist_data = playlist_resp.json()
        video_items += [
            {
                'title': item['snippet']['title'],
                'videoId': item['snippet']['resourceId']['videoId']
            }
            for item in playlist_data.get('items', []) if 'resourceId' in item['snippet'] and 'videoId' in item['snippet']['resourceId']
        ]
        next_page_token = playlist_data.get('nextPageToken')
        if not next_page_token:
            break
        time.sleep(0.1)
    return jsonify(video_items)

@app.route('/')
def index():
    return render_template('index.html')

import re

@app.route('/api/episode-crosscheck')
def api_episode_crosscheck():
    # Get all episodes
    episodes_resp = app.test_client().get('/api/episodes')
    episodes_data = episodes_resp.get_json()
    # Get all guest leaderboard episodes
    guests_resp = app.test_client().get('/api/guest-leaderboard')
    guest_data = guests_resp.get_json()
    # Range to check
    min_ep = 148
    max_ep = 714
    expected_eps = set(str(i) for i in range(min_ep, max_ep + 1))
    # Episodes tab check
    episode_numbers = []
    for ep in episodes_data:
        matches = re.findall(r'#(\d+)', ep['title'])
        episode_numbers.extend(matches)
    episode_counts = {}
    for epn in episode_numbers:
        episode_counts[epn] = episode_counts.get(epn, 0) + 1
    found_eps = set(episode_counts.keys())
    missing_eps = sorted(list(expected_eps - found_eps), key=int)
    duplicate_eps = sorted([epn for epn, count in episode_counts.items() if count > 1], key=int)
    # Guest tab check
    guest_episode_numbers = []
    for guest in guest_data:
        for ep in guest['episodes']:
            matches = re.findall(r'#(\d+)', ep['title'])
            guest_episode_numbers.extend(matches)
    guest_counts = {}
    for epn in guest_episode_numbers:
        guest_counts[epn] = guest_counts.get(epn, 0) + 1
    guest_found_eps = set(guest_counts.keys())
    guest_missing_eps = sorted(list(expected_eps - guest_found_eps), key=int)
    guest_duplicate_eps = sorted([epn for epn, count in guest_counts.items() if count > 1], key=int)
    return jsonify({
        'episodes_tab': {
            'total_found': len(found_eps),
            'missing': missing_eps,
            'duplicates': duplicate_eps
        },
        'guest_tab': {
            'total_found': len(guest_found_eps),
            'missing': guest_missing_eps,
            'duplicates': guest_duplicate_eps
        }
    })

if __name__ == "__main__":
    app.run(debug=True)


if __name__ == '__main__':
    app.run(debug=True)
