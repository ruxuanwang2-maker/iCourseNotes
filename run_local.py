"""Load local configuration before importing the course-processing runtime."""
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
from dotenv import dotenv_values, load_dotenv

values = dotenv_values(ROOT / '.env')
required = ('StuId', 'UISPsw', 'DB_ENCRYPTION_KEY', 'COURSE_IDS')
missing = [name for name in required if not values.get(name) or str(values[name]).startswith('your_')]
if missing:
    print('Please complete and save .env: ' + ', '.join(missing), flush=True)
    sys.exit(2)
if not any(values.get(name) for name in ('DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'GEMINI_API_KEY')):
    print('Please configure an LLM API key in .env.', flush=True)
    sys.exit(2)
load_dotenv(ROOT / '.env', override=True)
os.environ.setdefault('HF_HOME', str(ROOT / 'work' / 'huggingface'))
os.environ.setdefault('PYTHONUTF8', '1')
gpu_runtime = ROOT / 'work' / 'cuda-runtime'
if os.environ.get('ASR_DEVICE', 'cpu').startswith('cuda') and gpu_runtime.is_dir():
    # Keep the desktop's CPU packages intact; only this course process uses CUDA.
    sys.path.insert(0, str(gpu_runtime))

# Cairo is bundled with the Codex desktop dependency runtime on this machine.
native = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin'
_dll_handle = None
if os.name == 'nt' and native.is_dir():
    os.environ['PATH'] = str(native) + os.pathsep + os.environ.get('PATH', '')
    os.environ['CAIROCFFI_DLL_DIRECTORIES'] = str(native)
    _dll_handle = os.add_dll_directory(str(native))

ffmpeg_bins = list((ROOT / 'work' / 'ffmpeg').glob('*/bin'))
if ffmpeg_bins:
    os.environ['PATH'] = str(ffmpeg_bins[0]) + os.pathsep + os.environ.get('PATH', '')
if not shutil.which('ffmpeg'):
    import imageio_ffmpeg
    binary = Path(imageio_ffmpeg.get_ffmpeg_exe())
    tools = ROOT / 'work' / 'bin'
    tools.mkdir(parents=True, exist_ok=True)
    target = tools / 'ffmpeg.exe'
    if not target.exists():
        shutil.copy2(binary, target)
    os.environ['PATH'] = str(tools) + os.pathsep + os.environ.get('PATH', '')

import main
if '--check' in sys.argv:
    print('Configuration and main runtime imports OK.', flush=True)
    sys.exit(0)
# Keep a process-lifetime OS lock so the website and PowerShell launcher
# cannot accidentally run the same subscription batch at the same time.
lock_path = ROOT / 'data' / 'local-control' / 'runtime.lock'
lock_path.parent.mkdir(parents=True, exist_ok=True)
_runtime_lock = lock_path.open('a+b')
_runtime_lock.seek(0)
if os.name == 'nt':
    import msvcrt
    if lock_path.stat().st_size == 0:
        _runtime_lock.write(b'0')
        _runtime_lock.flush()
    _runtime_lock.seek(0)
    try:
        msvcrt.locking(_runtime_lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print('Another iCourseNotes instance is already running.', flush=True)
        sys.exit(3)
else:
    import fcntl
    try:
        fcntl.flock(_runtime_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('Another iCourseNotes instance is already running.', flush=True)
        sys.exit(3)
try:
    main.run()
except KeyboardInterrupt:
    print('Stopped by user.', flush=True)
except Exception as exc:
    # Keep credentials and signed URLs out of terminal diagnostics.
    print('[Fatal] Run failed: ' + type(exc).__name__, flush=True)
    sys.exit(1)
