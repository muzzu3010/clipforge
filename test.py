# test_ffmpeg.py
import subprocess
import os

print("=" * 60)
print("Testing FFmpeg features")
print("=" * 60)

# Test 1: Check FFmpeg is installed
print("\n1. Checking FFmpeg...")
result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
if result.returncode == 0:
    print("✅ FFmpeg found")
else:
    print("❌ FFmpeg not found")
    exit()

# Test 2: Create a music file
print("\n2. Creating test music file...")
cmd = ['ffmpeg', '-f', 'lavfi', '-i', 'aevalsrc=0.3*sin(2*PI*440*t)', '-t', '5', '-acodec', 'libmp3lame', '-y', 'test_music.mp3']
result = subprocess.run(cmd, capture_output=True, text=True)
if result.returncode == 0 and os.path.exists('test_music.mp3'):
    print("✅ Music file created: test_music.mp3")
    file_size = os.path.getsize('test_music.mp3')
    print(f"   File size: {file_size} bytes")
else:
    print(f"❌ Failed: {result.stderr}")
    exit()

# Test 3: Create a test video
print("\n3. Creating test video...")
cmd = ['ffmpeg', '-f', 'lavfi', '-i', 'color=c=red:size=320x240:duration=10', '-f', 'lavfi', '-i', 'aevalsrc=0.3*sin(2*PI*440*t)', '-c:v', 'libx264', '-c:a', 'aac', '-y', 'test_video.mp4']
result = subprocess.run(cmd, capture_output=True, text=True)
if result.returncode == 0 and os.path.exists('test_video.mp4'):
    print("✅ Test video created: test_video.mp4")
else:
    print(f"❌ Failed: {result.stderr}")
    exit()

# Test 4: Mix audio
print("\n4. Testing audio mixing...")
cmd = [
    'ffmpeg',
    '-i', 'test_video.mp4',
    '-i', 'test_music.mp3',
    '-filter_complex', '[0:a]volume=0.7[a0];[1:a]volume=0.3[a1];[a0][a1]amix=inputs=2:duration=longest',
    '-c:v', 'copy',
    '-y', 'test_mixed.mp4'
]
result = subprocess.run(cmd, capture_output=True, text=True)
if result.returncode == 0 and os.path.exists('test_mixed.mp4'):
    print("✅ Audio mixing successful")
    file_size = os.path.getsize('test_mixed.mp4')
    print(f"   File size: {file_size} bytes")
else:
    print(f"❌ Failed: {result.stderr}")

print("\n" + "=" * 60)
print("Test complete! Check the generated files:")
print("  - test_music.mp3 (should play audio)")
print("  - test_video.mp4 (red screen with beep)")
print("  - test_mixed.mp4 (red screen with mixed audio)")
print("=" * 60)