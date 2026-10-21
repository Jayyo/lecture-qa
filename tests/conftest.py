import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# The app reads its configuration at import time
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='lecture-qa-test-')
os.environ.setdefault('OPENAI_API_KEY', 'test-key')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

needs_ffmpeg = pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg not installed')


@pytest.fixture
def make_video(tmp_path):
    """Create a short MP4 with a tone, optionally without audio."""
    def make(seconds=1, audio=True, name='clip.mp4'):
        path = tmp_path / name
        cmd = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=black:s=64x64:d={seconds}']
        if audio:
            cmd += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-shortest']
        cmd += ['-pix_fmt', 'yuv420p', '-y', str(path)]
        subprocess.run(cmd, check=True)
        return path
    return make
