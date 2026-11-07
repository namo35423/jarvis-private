#!/usr/bin/env python3
"""Switch speech stacks without deleting memory. Restart the brain afterwards."""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
mode=sys.argv[1] if len(sys.argv)>1 else ''
if mode not in {'thai','english'}:
 raise SystemExit('Usage: scripts/language_mode.py thai|english')
root=Path.home()/'.jarvis';root.mkdir(exist_ok=True)
path=root/'config.json'
data=json.loads(path.read_text()) if path.exists() else {}
if path.exists():
 backup=root/('config.before-'+mode+'-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json')
 shutil.copy2(path,backup);print('Configuration backed up to',backup)
data['language']='th' if mode=='thai' else 'en'
data.setdefault('stt',{}).update(engine='whisper' if mode=='thai' else 'parakeet',model='mlx-community/whisper-large-v3-turbo' if mode=='thai' else 'mlx-community/parakeet-tdt-0.6b-v3',language='auto' if mode=='thai' else 'en')
data.setdefault('voice',{}).update(engine='macos' if mode=='thai' else 'kokoro',voice='bm_george',thai_voice='Kanya',english_voice='Samantha',speed=1.0 if mode=='thai' else 1.3)
data.setdefault('claude',{})['mode']='off'
data.setdefault('safety',{})['confirm_risky']=True
path.write_text(json.dumps(data,ensure_ascii=False,indent=2))
print('Configured',mode,'mode. Run bin/jarvis restart. Memory and timers are unchanged.')
